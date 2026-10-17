"""Funnel (インターネットへの公開) の試験 (just ci)。docs/cluster/guest-access.md の「実装 (g2b)」。

標準ライブラリだけ: python3 -B -m unittest discover -s clusters/kind/tailscale -v

確かめること:
- Funnel の注釈 (tailscale.com/funnel) が付いているのは、Keycloak・Grafana (観測スタック)・Backstage の 3 つの Ingress だけ。
  tailnet のポリシーの nodeAttrs で tag:k8s の proxy のどれもが Funnel を使える状態なので、どこに付けたかをここで数える。
  3 つは経路を絞る proxy を指し、Keycloak の名前 (issuer) は変えない
- Keycloak の入口の proxy (clusters/kind/auth/keycloak-front): インターネットからの要求 (Tailscale-Funnel-Request あり) は
  realm home-k8s と /resources/ だけが Keycloak に届き、/admin/・realm master・client の登録・/ ・metrics・health は表記を変えても 404。
  届いた要求も、生の path を寛容に解いた (%エンコード・バックスラッシュ・matrix parameter・スラッシュの重なり・ドットセグメント・大文字小文字) 先が
  許可リストの中にあること。tailnet からの要求 (ヘッダなし) は全部そのまま届く
- Grafana の入口の proxy (clusters/kind/observability/tailnet-proxy): インターネットからはパスワードのログイン (POST /login) と
  パスワードのリセットを表記を変えても拒み、Authorization ヘッダを Grafana に渡さない。OIDC のログイン (/login/generic_oauth) と画面は届く。
  tailnet からは今までどおり

caddy を 127.0.0.1 の空きポートで起動し、upstream は偽の HTTP サーバー。クラスタにもネットワークにも出ない。
caddy の起動と偽の upstream は Backstage の試験 (clusters/kind/backstage/test_tailnet_proxy.py) のものを使う。
`caddy` が無い環境では飛ばす (`just ci` は devShells.ci に caddy を入れて必ず走らせる)。
"""
import http.client
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "clusters/kind/backstage"))
from test_tailnet_proxy import caddy_env, free_port, start_upstream, wait_listening  # noqa: E402

FUNNEL = {"Tailscale-Funnel-Request": "?1"}

# Funnel を付ける Ingress (manifest) と、その先の Service。ほかには付けない
FUNNELED = {
    "clusters/kind/tailscale/ingress/keycloak.yaml": "keycloak-front",
    "clusters/kind/tailscale/ingress/grafana.yaml": "grafana-tailnet",
    "clusters/kind/tailscale/ingress/backstage.yaml": "backstage-tailnet",
}


def request(port, method, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        # path は生のまま送る (http.client は path を書き換えない)
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


class Caddy:
    """Caddyfile の待ち受けと upstream だけを手元に置き換えて起動する (経路の書き方はそのまま)。"""

    def __init__(self, caddyfile, upstream_addr):
        text = caddyfile.read_text(encoding="utf-8")
        assert text.count(":8080 {") == 1 and text.count(f"reverse_proxy {upstream_addr}") >= 1
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream = start_upstream()
        self.port = free_port()
        config = self.tmp / "Caddyfile"
        config.write_text(
            text.replace(":8080 {", f":{self.port} {{").replace(
                f"reverse_proxy {upstream_addr}", f"reverse_proxy 127.0.0.1:{self.upstream.server_port}"
            ),
            encoding="utf-8",
        )
        self.proc = subprocess.Popen(
            ["caddy", "run", "--config", str(config), "--adapter", "caddyfile"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=caddy_env(self.tmp),
        )
        wait_listening(self.proc, self.port, "caddy")

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.proc.stderr.close()
        self.upstream.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)


def lenient(raw_path):
    """upstream が path をいちばん寛容に読んだときの行き先。%エンコードを解き切り、バックスラッシュをスラッシュに、
    matrix parameter (;…) を落とし、スラッシュの重なりとドットセグメントを畳み、小文字にする。"""
    path = raw_path.split("?", 1)[0]
    while (decoded := unquote(path)) != path:
        path = decoded
    path = path.replace("\\", "/")
    path = "/".join(seg.split(";", 1)[0] for seg in path.split("/"))
    path = posixpath.normpath("/" + path.lstrip("/"))  # normpath は先頭の // を残す
    return path.lower()


def in_allowlist(raw_path):
    path = lenient(raw_path)
    if path.startswith("/realms/home-k8s/clients-registrations"):
        return False
    return path == "/realms/home-k8s" or path.startswith("/realms/home-k8s/") or path.startswith("/resources/")


class FunnelIngresses(unittest.TestCase):
    def test_only_three_ingresses_have_funnel(self):
        found = {}
        for f in sorted((ROOT / "clusters").rglob("*.y*ml")):
            for m in re.finditer(r"^\s*tailscale\.com/funnel:\s*\"?([A-Za-z]+)\"?\s*$", f.read_text(encoding="utf-8"), re.M):
                found[str(f.relative_to(ROOT))] = m.group(1)
        self.assertEqual(found, {p: "true" for p in FUNNELED}, "Funnel を付けてよいのは Keycloak・Grafana (観測スタック)・Backstage の Ingress だけ")
        self.assertEqual(len(found), 3)

    def test_funneled_ingresses_point_at_the_filtering_proxies(self):
        for path, service in FUNNELED.items():
            with self.subTest(path=path):
                text = (ROOT / path).read_text(encoding="utf-8")
                self.assertRegex(text, rf"defaultBackend:\n\s+service:\n\s+name: {service}\n\s+port:\n\s+number: 80\n")
                self.assertNotIn("rules:", text)  # 経路を Ingress で分けると proxy を通らない経路ができる

    def test_keycloak_hostname_is_unchanged(self):
        # issuer https://keycloak.<tailnet>.ts.net/realms/home-k8s はこの名前に結び付く
        text = (ROOT / "clusters/kind/tailscale/ingress/keycloak.yaml").read_text(encoding="utf-8")
        self.assertRegex(text, r"tls:\n\s+- hosts:\n\s+- keycloak\n")
        self.assertIn("hostname: https://keycloak.taild2b611.ts.net\n", (ROOT / "clusters/kind/auth/keycloak/keycloak.yaml").read_text(encoding="utf-8"))

    def test_proxy_services_are_cluster_ip_only(self):
        for d in ("clusters/kind/auth/keycloak-front", "clusters/kind/observability/tailnet-proxy"):
            with self.subTest(d=d):
                text = (ROOT / d / "proxy.yaml").read_text(encoding="utf-8")
                self.assertIn("  type: ClusterIP\n", text)
                self.assertNotIn("NodePort", text.split("kind: Service", 1)[1])


KEYCLOAK_ALLOWED = [
    "/realms/home-k8s",
    "/realms/home-k8s/",
    "/realms/home-k8s/.well-known/openid-configuration",
    "/realms/home-k8s/protocol/openid-connect/auth?client_id=grafana&redirect_uri=https%3A%2F%2Fgrafana.taild2b611.ts.net%2Flogin%2Fgeneric_oauth",
    "/realms/home-k8s/protocol/openid-connect/token",
    "/realms/home-k8s/protocol/openid-connect/certs",
    "/realms/home-k8s/login-actions/authenticate?session_code=x&execution=y",
    "/realms/home-k8s/login-actions/required-action?execution=CONFIGURE_TOTP",
    "/realms/home-k8s/account",
    "/realms/home-k8s/account/credentials",
    "/resources/abcde/login/keycloak.v2/css/styles.css",
    "/resources/abcde/common/keycloak/web_modules/@patternfly/x.js",
]
KEYCLOAK_DENIED = [
    # 管理 (Admin Console と REST API)
    "/admin/",
    "/admin",
    "/admin/master/console/",
    "/admin/realms/home-k8s/users",
    "/ADMIN/realms/home-k8s/users",
    "/%61dmin/realms/home-k8s/users",
    "//admin/realms/home-k8s/users",
    "/./admin/",
    "/resources/../admin/",
    "/resources/%2e%2e/admin/",
    "/resources/..%2fadmin/",
    "/resources/..%2Fadmin/",
    "/resources/%2e%2e%2fadmin/",
    "/resources/..%5cadmin/",
    "/resources/..\\admin/",
    "/resources/%252e%252e/admin/",
    "/resources/..;/admin/",
    "/resources;/../admin/",
    # realm master
    "/realms/master/",
    "/realms/master/protocol/openid-connect/token",
    "/realms/MASTER/protocol/openid-connect/token",
    "/realms/%6daster/account",
    "/realms//master/account",
    "/realms/home-k8s/../master/account",
    "/realms/home-k8s/%2e%2e/master/account",
    "/realms/home-k8s/..%2fmaster/account",
    "/realms/home-k8s/x/../../master/account",
    "/realms/home-k8s/.%2e/master/account",
    "/realms/home-k8s;/../master/account",
    "/realms/home-k8s/%252e%252e/master/account",
    "/realms/home-k8s/..%5cmaster/account",
    # realm home-k8s の中でも client の登録の API
    "/realms/home-k8s/clients-registrations/default",
    "/realms/home-k8s/clients-registrations/openid-connect",
    "/realms/home-k8s/Clients-Registrations/openid-connect",
    "/realms/home-k8s/%63lients-registrations/openid-connect",
    "/realms/home-k8s//clients-registrations/openid-connect",
    "/realms/home-k8s/./clients-registrations/openid-connect",
    # 似た名前の realm・ほかの経路
    "/realms/home-k8s-evil/account",
    "/realms/home-k8sx",
    "/",
    "/metrics",
    "/health",
    "/health/ready",
    "/q/metrics",
    "/js/keycloak.js",
    "/robots.txt",
]


@unittest.skipUnless(shutil.which("caddy"), "caddy が無い")
class KeycloakFront(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.caddy = Caddy(ROOT / "clusters/kind/auth/keycloak-front/Caddyfile", "keycloak-service:8080")
        cls.upstream = cls.caddy.upstream

    @classmethod
    def tearDownClass(cls):
        cls.caddy.close()

    def setUp(self):
        self.upstream.seen.clear()

    def test_internet_reaches_only_realm_home_k8s_and_resources(self):
        for path in KEYCLOAK_ALLOWED:
            with self.subTest(path=path):
                self.assertEqual(request(self.caddy.port, "GET", path, FUNNEL), 200)
        self.assertEqual([p for _, p, _ in self.upstream.seen], KEYCLOAK_ALLOWED)

    def test_internet_is_refused_admin_master_and_others_in_any_spelling(self):
        for method in ("GET", "POST", "PUT", "DELETE"):
            for path in KEYCLOAK_DENIED:
                with self.subTest(method=method, path=path):
                    self.assertEqual(request(self.caddy.port, method, path, FUNNEL), 404)
        self.assertEqual(self.upstream.seen, [], "許可リストの外が Keycloak に届いた")

    def test_whatever_reaches_keycloak_from_internet_stays_in_allowlist(self):
        # 許可リストの外の書き方を、許可リストの前に付けてみる。届いたものは寛容に読んでも許可リストの中
        for prefix in ("/realms/home-k8s/", "/resources/x/", "/realms/home-k8s/account/"):
            for path in KEYCLOAK_DENIED:
                with self.subTest(path=prefix + path):
                    request(self.caddy.port, "GET", prefix + path.lstrip("/"), FUNNEL)
        for _, path, _ in self.upstream.seen:
            self.assertTrue(in_allowlist(path), f"許可リストの外として読める path が届いた: {path}")

    def test_spoofed_funnel_header_values_are_still_internet(self):
        # tailscale serve は外から来た Tailscale-Funnel-Request を消してから付ける。値によらず、あればインターネット扱い
        for value in ("?1", "?0", "1", "true", "x"):
            with self.subTest(value=value):
                self.assertEqual(request(self.caddy.port, "GET", "/admin/", {"Tailscale-Funnel-Request": value}), 404)

    def test_tailnet_reaches_everything_unchanged(self):
        paths = ["/admin/master/console/", "/admin/realms/home-k8s/users", "/realms/master/protocol/openid-connect/token", "/"] + KEYCLOAK_ALLOWED
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(request(self.caddy.port, "GET", path), 200)
        self.assertEqual([p for _, p, _ in self.upstream.seen], paths)

    def test_forwarded_headers_from_tailscale_are_kept(self):
        # Keycloak は proxy.headers: xforwarded。https のまま渡らないと http の URL を作る
        headers = {**FUNNEL, "X-Forwarded-Proto": "https", "X-Forwarded-Host": "keycloak.taild2b611.ts.net", "X-Forwarded-For": "203.0.113.7"}
        self.assertEqual(request(self.caddy.port, "GET", "/realms/home-k8s/account", headers), 200)
        seen = self.upstream.seen[-1][2]
        self.assertEqual(seen.get("X-Forwarded-Proto"), "https")
        self.assertEqual(seen.get("X-Forwarded-Host"), "keycloak.taild2b611.ts.net")
        self.assertTrue(seen.get("X-Forwarded-For", "").startswith("203.0.113.7"))


GRAFANA_LOGIN_FORM = [
    "/login",
    "/login?x=1",
    "/LOGIN",
    "/Login",
    "/%6cogin",
    "//login",
    "/./login",
    "/x/../login",
    "/login/",
    "/login;x",
    "/%2flogin",
]
GRAFANA_PASSWORD_RESET = [
    "/api/user/password/send-reset-email",
    "/api/user/password/reset",
    "/API/USER/PASSWORD/reset",
    "/api//user/password/reset",
    "/user/password/send-reset-email",
    "/user/password/reset?code=x",
]
GRAFANA_PASSED = [
    "/",
    "/login",  # GET: Keycloak のボタンを出す画面
    "/login/generic_oauth",
    "/login/generic_oauth?code=x&state=y",
    "/api/search?query=",
    "/d/abc/node",
    "/public/build/app.js",
]


@unittest.skipUnless(shutil.which("caddy"), "caddy が無い")
class GrafanaTailnet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.caddy = Caddy(ROOT / "clusters/kind/observability/tailnet-proxy/Caddyfile", "grafana:80")
        cls.upstream = cls.caddy.upstream

    @classmethod
    def tearDownClass(cls):
        cls.caddy.close()

    def setUp(self):
        self.upstream.seen.clear()

    def test_internet_cannot_post_the_login_form_in_any_spelling(self):
        for method in ("POST", "PUT"):
            for path in GRAFANA_LOGIN_FORM:
                with self.subTest(method=method, path=path):
                    self.assertIn(request(self.caddy.port, method, path, {**FUNNEL, "Content-Type": "application/json"}), (403, 404))
        self.assertEqual(self.upstream.seen, [], "パスワードのログインが Grafana に届いた")

    def test_internet_cannot_reset_passwords(self):
        for method in ("GET", "POST"):
            for path in GRAFANA_PASSWORD_RESET:
                with self.subTest(method=method, path=path):
                    self.assertEqual(request(self.caddy.port, method, path, FUNNEL), 404)
        self.assertEqual(self.upstream.seen, [])

    def test_internet_basic_auth_is_dropped(self):
        for path in GRAFANA_PASSED:
            with self.subTest(path=path):
                self.assertEqual(request(self.caddy.port, "GET", path, {**FUNNEL, "Authorization": "Basic YWRtaW46eA=="}), 200)
        self.assertEqual([p for _, p, _ in self.upstream.seen], GRAFANA_PASSED)
        for _, _, headers in self.upstream.seen:
            self.assertNotIn("Authorization", headers)

    def test_tailnet_keeps_password_login_and_basic_auth(self):
        self.assertEqual(request(self.caddy.port, "POST", "/login", {"Content-Type": "application/json"}), 200)
        self.assertEqual(request(self.caddy.port, "GET", "/api/user", {"Authorization": "Basic dmlld2VyOng="}), 200)
        self.assertEqual(self.upstream.seen[-1][2].get("Authorization"), "Basic dmlld2VyOng=")


@unittest.skipUnless(shutil.which("caddy"), "caddy が無い")
class BackstageGuestFromInternet(unittest.TestCase):
    """Backstage の入口の proxy はゲストのサインインを tailnet からも拒む。インターネットからの要求でも同じこと。"""

    def test_guest_sign_in_is_refused_from_internet(self):
        caddy = Caddy(ROOT / "clusters/kind/backstage/tailnet-proxy/Caddyfile", "backstage:7007")
        try:
            for path in ("/api/auth/guest/refresh", "/API/AUTH/GUEST/refresh", "/api/auth/%67uest/refresh", "/api/auth/x/../guest/refresh"):
                with self.subTest(path=path):
                    self.assertEqual(request(caddy.port, "POST", path, FUNNEL), 403)
            self.assertEqual(caddy.upstream.seen, [])
            self.assertEqual(request(caddy.port, "GET", "/api/auth/oidc/start?env=production", FUNNEL), 200)
        finally:
            caddy.close()


if __name__ == "__main__":
    unittest.main()
