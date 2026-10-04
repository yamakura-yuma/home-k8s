"""share Pod の Caddyfile (clusters/kind/share/Caddyfile) の経路の統合試験。

標準ライブラリだけ: python3 -B -m unittest discover -s clusters/kind/share -p test_share_caddy.py

稼働中のクラスタにもホストにも触れない。caddy と認証サービス (share_auth.py、AUTH_SOURCE=file:) を 127.0.0.1 の空きポートで
起動し、upstream (grafana・headroom の中継・backstage) は偽の HTTP サーバー (標準ライブラリ)。
確かめるのは拒否: 認証なしは全経路 401、認証があっても許可リストの外は 404 で upstream に届かない、
delete・期限切れは次の要求から 401 (Backstage の cookie 経路でも)、特権 viewer に追加の経路が通らない。
#58: Grafana に渡す viewer の鍵は、認証サービスが Secret share-grafana (試験では JSON の viewer_value) を実行時に読んで渡す。
パスワードを変えると caddy・認証サービスを立てたまま次の要求から新しい鍵になり、無い・空のあいだは Grafana の経路だけ 503、人の Authorization は Grafana に届かない。
#64: Secret が揃っていない Pod (起動スクリプト entrypoint.sh が全拒否の Caddyfile.closed を選ぶ) は、3 つの経路がすべて 503 で、
資格情報が正しくても upstream に届かない。Secret ができて Pod が作り直されたあと (環境変数が揃う) は、同じポートで通常どおり動く。

`caddy` が無い環境では飛ばす (`just ci` は devShells.ci に caddy を入れて必ず走らせる)。
"""
import base64
import errno
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import share_auth  # noqa: E402

CADDYFILE = HERE / "Caddyfile"
ENTRYPOINT = HERE / "entrypoint.sh"
AUTH_SERVICE = HERE / "share_auth.py"
RELAY_TOKEN = "0123456789abcdef0123456789abcdef"
VIEWER_VALUE = "viewer-secret"
GRAFANA_BASIC = base64.b64encode(f"viewer:{VIEWER_VALUE}".encode()).decode()
SESSION_KEY = "test-session-key"
ITERATIONS = 100_000  # share_auth.MIN_ITERATIONS。試験を速くする
PASSWORDS = {"alice": "alice-password", "root": "root-password"}

GRAFANA, HEADROOM, BACKSTAGE = "grafana", "headroom", "backstage"

# 対象ごとの (通る要求, 通らない要求)。通らない要求は認証があっても 404 で、upstream に届かない
GRAFANA_ALLOWED = [
    ("GET", "/"), ("GET", "/dashboards"), ("GET", "/d/abc/x?orgId=1"), ("POST", "/api/ds/query"), ("GET", "/goto/abc"),
    # プロフィール・設定の読み取りは通す (画面が使う。書き込みだけを GRAFANA_DENIED で止める)
    ("GET", "/api/user"), ("GET", "/api/user/preferences"), ("GET", "/api/user/orgs"),
    ("GET", "/apis/preferences.grafana.app/v1alpha1/namespaces/default/preferences"),
    ("GET", "/apis/collections.grafana.app/v1alpha1/namespaces/default/stars"),
    ("GET", "/apis/userstorage.grafana.app/v0alpha1/namespaces/default/user-storage/service:abc"),
]
GRAFANA_DENIED = [
    (method, path)
    for method in ["GET", "POST", "PUT", "PATCH"]
    for path in ["/profile/password", "/profile/password/", "/api/user/password", "/api/user/password/",
                 "/PROFILE/password", "//profile/password", "/profile/%70assword", "/api/user/password?x=1"]
] + [
    # #51: viewer は自分のプロフィールを書き換えられる。ログイン名を変えると caddy が付ける Basic が効かなくなり、共有が壊れる。
    # /api/user 以下と preferences の新しい API は、書き込みを通さない (読み取りは GRAFANA_ALLOWED)
    (method, path)
    for method in ["PUT", "POST", "PATCH", "DELETE"]
    for path in ["/api/user", "/api/user/", "/api/user?x=1", "/api/USER", "/API/user", "/api/%75ser", "/api/user%2f", "/api/user/preferences",
                 "/api/user/preferences/", "/api/user/PREFERENCES", "/api/%75ser/preferences", "/api/user/using/1",
                 "/api/user/stars/dashboard/uid/abc", "/api/user/auth-tokens/rotate", "/api/user/revoke-auth-token",
                 "/apis/preferences.grafana.app/v1alpha1/namespaces/default/preferences/user-x",
                 "/apis/preferences.grafana.app/v1/namespaces/default/preferences",
                 "/APIS/preferences.grafana.app/v1alpha1/namespaces/default/preferences/user-x"]
] + [
    # #54: 星 (collections) と画面の保存領域 (userstorage) の新しい API も、Grafana 13.2.3 で viewer の PUT・DELETE が通った。
    # 読み取りだけ通す。表記揺れ (大文字・二重スラッシュ・%エンコード) も同じ表で止める
    (method, path)
    for method in ["PUT", "POST", "PATCH", "DELETE"]
    for path in ["/apis/collections.grafana.app/v1alpha1/namespaces/default/stars/user-x",
                 "/apis/collections.grafana.app/v1alpha1/namespaces/default/stars/user-x/update/dashboard.grafana.app/Dashboard/abc",
                 "/apis/collections.grafana.app/v1alpha1/namespaces/default/stars",
                 "/APIS/collections.grafana.app/v1alpha1/namespaces/default/stars/user-x",
                 "/apis/COLLECTIONS.grafana.app/v1alpha1/namespaces/default/stars/user-x",
                 "//apis/collections.grafana.app/v1alpha1/namespaces/default/stars/user-x",
                 "/apis/collections.grafana.app//v1alpha1/namespaces/default/stars/user-x",
                 "/apis/%63ollections.grafana.app/v1alpha1/namespaces/default/stars/user-x",
                 "/apis/userstorage.grafana.app/v0alpha1/namespaces/default/user-storage",
                 "/apis/userstorage.grafana.app/v0alpha1/namespaces/default/user-storage/service:abc",
                 "/apis/userstorage.grafana.app/v0alpha1/namespaces/default/user-storage/service%3Aabc",
                 "/APIS/userstorage.grafana.app/v0alpha1/namespaces/default/user-storage/service:abc",
                 "//apis/userstorage.grafana.app/v0alpha1/namespaces/default/user-storage/service:abc",
                 "/apis/%75serstorage.grafana.app/v0alpha1/namespaces/default/user-storage/service:abc"]
] + [
    # #54: パスの正規化の抜けを固定する。重ねたスラッシュ・%2F は、許可していない経路に正規化されない
    (method, path)
    for method in ["PUT", "POST", "PATCH", "DELETE"]
    for path in ["//api/user", "///api/user", "/api//user", "/api%2Fuser", "/%2Fapi/user", "//api/user/preferences", "/api/user%2Fpreferences",
                 "//apis/preferences.grafana.app/v1/namespaces/default/preferences/user-x", "/apis%2Fpreferences.grafana.app/v1/namespaces/default/preferences/user-x"]
]
HEADROOM_ALLOWED = ["/dashboard", "/health", "/stats", "/stats-history", "/stats-lifetime", "/transformations/feed", "/favicon.ico"]
HEADROOM_DENIED = [
    # #53: caddy の path は大文字小文字を区別しないが、許可リストは区別する (headroom は区別する)
    *[("GET", variant) for path in HEADROOM_ALLOWED for variant in (path.upper(), path[:2].upper() + path[2:], path[:-1] + path[-1].upper())],
    ("GET", "/Health"), ("GET", "/DASHBOARD"), ("GET", "/Favicon.ico"), ("GET", "/favicon.ICO"), ("GET", "/Stats-History"),
    ("GET", "/health%0a"), ("GET", "/%48ealth"),
    # #54: 重ねたスラッシュ・%2F が許可リストの経路に正規化されて通らない (許可リストは全体一致)
    *[("GET", variant) for path in HEADROOM_ALLOWED for variant in ("/" + path, path + "/", "/%2F" + path[1:])],
    ("GET", "//health"), ("GET", "/%2Fhealth"), ("GET", "/transformations%2Ffeed"), ("GET", "//transformations/feed"), ("GET", "/transformations//feed"),
    ("GET", "/%2e/%2Fhealth"), ("GET", "/health%2F"), ("GET", "/%2Fdashboard"),
    ("POST", "/v1/messages"), ("GET", "/v1/messages"), ("POST", "/stats/reset"), ("GET", "/stats/reset"),
    ("POST", "/settings"), ("GET", "/settings"), ("POST", "/dashboard/settings"), ("POST", "/cache/clear"),
    ("POST", "/dashboard"), ("PUT", "/stats"), ("DELETE", "/health"), ("GET", "/stats/"),
    ("GET", "//v1/messages"), ("GET", "/dashboard/../v1/messages"), ("GET", "/dashboard/%2e%2e/v1/messages"),
    # #54: ドットセグメントも許可リストの経路に正規化して通さない (許可は送られた生の経路との全体一致)
    ("GET", "/x/../health"), ("GET", "/./health"), ("GET", "/%2e/health"), ("GET", "/stats/../stats-history?days=7"),
    # #48: 実機の headroom は許可リストの経路の HEAD に 404 を返し、Anthropic へ素通しするとみられる。HEAD は通さない
    *[("HEAD", path) for path in HEADROOM_ALLOWED],
]
BACKSTAGE_ALLOWED = [
    ("GET", "/"), ("HEAD", "/"), ("GET", "/api/catalog/entities"), ("HEAD", "/api/techdocs/static/docs/x/index.html"),
    ("GET", "/api/proxyx"), ("POST", "/api/auth/guest/refresh"), ("POST", "/api/catalog/entities/by-refs"),
]
BACKSTAGE_DENIED = [
    ("GET", "/api/proxy"), ("GET", "/api/proxy/"), ("GET", "/api/proxy/grafana/api/org"), ("HEAD", "/api/proxy/grafana/api/org"),
    ("POST", "/api/proxy/grafana/api/org"), ("GET", "/api/PROXY/grafana/api/org"),
    ("POST", "/api/catalog/locations"), ("POST", "/api/catalog/refresh"), ("POST", "/"), ("PUT", "/api/catalog/entities"),
    ("DELETE", "/api/catalog/entities/by-uid/x"), ("POST", "/api/auth"),
    # #53: 拒否の /api/proxy は表記揺れも止める (Backstage の経路は大文字小文字を区別しない)。POST の許可は完全一致
    ("GET", "/API/proxy/grafana/api/org"), ("GET", "/Api/Proxy"), ("POST", "/api/PROXY/grafana/api/org"), ("GET", "/api/proxy%2fgrafana"),
    ("POST", "/api/catalog/Entities/by-refs"), ("POST", "/api/catalog/entities/by-refs/x"),
]


_reserved = []  # free_port が予約したポートのソケット。プロセスが終わるまで持つ


def free_port():
    """空きポートを予約して番号を返す。開いて閉じるだけだと、閉じてから caddy が bind するまでの間に、
    並行する試験 (別の just ci も) が同じ番号を引く (#60)。予約のソケットは bind だけして listen せず、持ち続ける:
    ほかの bind は EADDRINUSE、bind(0) は選ばない。SO_REUSEADDR の待ち受け (caddy・http.server) は上に bind できる。"""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    _reserved.append(s)
    return s.getsockname()[1]


def request(port, method, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        resp.read()
        return resp.status, resp
    finally:
        conn.close()


def basic(name, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{name}:{password}".encode()).decode()}


class Upstream(BaseHTTPRequestHandler):
    """200 を返し、受けた要求 (メソッド・パス・ヘッダ) を記録する。Grafana・Backstage らしい Location・Set-Cookie も返す。"""

    def _record(self):
        self.server.seen.append((self.command, self.path, dict(self.headers)))
        self.server.raw.append(self.headers.items())  # 同じ名前のヘッダが複数あるときも見るため
        self.send_response(200)
        self.send_header("Location", "http://localhost:3000/after")
        self.send_header("Set-Cookie", "backstage-auth=abc; Path=/; Domain=localhost; HttpOnly")
        self.send_header("Content-Length", "2")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b"ok")

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _record

    def log_message(self, *args):
        pass


def start_upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    server.seen = []
    server.raw = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def wait_listening(proc, port, what):
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{what} が終了した: " + (proc.stderr.read().decode() if proc.stderr else Path(proc.log_path).read_text()))
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"{what} が待ち受けない")


def caddy_env(tmp, **extra):
    return {**os.environ, "XDG_CONFIG_HOME": str(tmp / "config"), "XDG_DATA_HOME": str(tmp / "data"), **extra}


def credential(password, *, privileged=False, expires_at=None):
    return {"hash": share_auth.hash_password(password, iterations=ITERATIONS), "expires_at": expires_at,
            "privileged": privileged, "created_at": time.time()}


class FreePort(unittest.TestCase):
    """#60: free_port は番号を返したあとも予約を持つ。ほかの試験・別の just ci の bind に取られず、caddy は bind できる。"""

    def test_ports_do_not_repeat(self):
        ports = [free_port() for _ in range(50)]
        self.assertEqual(len(set(ports)), len(ports))

    def test_a_plain_bind_to_a_reserved_port_fails(self):
        with socket.socket() as s:
            with self.assertRaises(OSError) as cm:
                s.bind(("127.0.0.1", free_port()))
        self.assertEqual(cm.exception.errno, errno.EADDRINUSE)

    def test_a_server_can_listen_on_a_reserved_port(self):
        # caddy (Go) も http.server も SO_REUSEADDR で待ち受ける
        port = free_port()
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            s.listen()
            socket.create_connection(("127.0.0.1", port), timeout=2).close()


class AuthStub(BaseHTTPRequestHandler):
    """認証サービスの代役。いつも 200 を返し、Grafana の鍵 (X-Share-Grafana-Authorization) は付けない (古い版・不具合の想定)。
    聞かれた X-Share-Target を記録する。"""

    def do_GET(self):
        self.server.targets.append(self.headers.get("X-Share-Target"))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        pass


@unittest.skipUnless(shutil.which("caddy"), "caddy が無い")
class ShareCaddy(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.source = cls.tmp / "credentials.json"
        cls.write_credentials()
        auth_port = free_port()
        cls.auth = subprocess.Popen(
            [sys.executable, "-B", str(AUTH_SERVICE)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env={**os.environ, "AUTH_SOURCE": f"file:{cls.source}", "AUTH_LISTEN": f"127.0.0.1:{auth_port}",
                 "AUTH_CACHE_TTL": "0", "AUTH_VERIFY_TTL": "0"})  # 0 で、delete・期限切れが次の要求から効くかを確かめる
        wait_listening(cls.auth, auth_port, "認証サービス")
        cls.grafana, cls.headroom_relay, cls.backstage = start_upstream(), start_upstream(), start_upstream()
        cls.ports = {GRAFANA: free_port(), HEADROOM: free_port(), BACKSTAGE: free_port()}
        cls.caddy = subprocess.Popen(
            ["caddy", "run", "--config", str(CADDYFILE), "--adapter", "caddyfile"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=caddy_env(
                cls.tmp,
                SHARE_AUTH_ADDR=f"127.0.0.1:{auth_port}",
                SHARE_RELAY_TOKEN=RELAY_TOKEN,
                SHARE_RELAY_ADDR=f"127.0.0.1:{cls.headroom_relay.server_port}",
                SHARE_GRAFANA_UPSTREAM=f"127.0.0.1:{cls.grafana.server_port}",
                SHARE_BACKSTAGE_UPSTREAM=f"127.0.0.1:{cls.backstage.server_port}",
                SHARE_GRAFANA_PORT=str(cls.ports[GRAFANA]),
                SHARE_HEADROOM_PORT=str(cls.ports[HEADROOM]),
                SHARE_BACKSTAGE_PORT=str(cls.ports[BACKSTAGE]),
            ))
        for port in cls.ports.values():
            wait_listening(cls.caddy, port, "caddy")

    @classmethod
    def tearDownClass(cls):
        for proc in (cls.caddy, cls.auth):
            proc.terminate()
            proc.wait(timeout=10)
            proc.stderr.close()
        for upstream in (cls.grafana, cls.headroom_relay, cls.backstage):
            upstream.shutdown()
            upstream.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def write_credentials(cls, viewer_value=VIEWER_VALUE, **overrides):
        """viewer_value は Secret share-grafana の写し。None なら Secret が無い状態 (キーを書かない)。"""
        entries = {
            "alice": credential(PASSWORDS["alice"], expires_at=time.time() + 3600),
            "root": credential(PASSWORDS["root"], privileged=True),
        }
        entries.update(overrides)
        entries = {name: entry for name, entry in entries.items() if entry is not None}
        data = {"credentials": entries, "session_key": SESSION_KEY}
        if viewer_value is not None:
            data["viewer_value"] = viewer_value
        cls.source.write_text(json.dumps(data), encoding="utf-8")

    def setUp(self):
        self.write_credentials()
        self.addCleanup(self.write_credentials)
        self.upstreams = {GRAFANA: self.grafana, HEADROOM: self.headroom_relay, BACKSTAGE: self.backstage}
        for upstream in self.upstreams.values():
            upstream.seen.clear()
            upstream.raw.clear()

    def call(self, target, method, path, headers=None):
        return request(self.ports[target], method, path, headers)

    def seen(self, target):
        return self.upstreams[target].seen

    def all_requests(self):
        """全対象の (対象, メソッド, パス)。許可リストの内も外も。"""
        return (
            [(GRAFANA, m, p) for m, p in GRAFANA_ALLOWED + GRAFANA_DENIED]
            + [(HEADROOM, m, p) for m, p in [("GET", p) for p in HEADROOM_ALLOWED] + HEADROOM_DENIED]
            + [(BACKSTAGE, m, p) for m, p in BACKSTAGE_ALLOWED + BACKSTAGE_DENIED]
        )

    def test_caddyfile_validates(self):
        env = caddy_env(self.tmp, SHARE_RELAY_TOKEN=RELAY_TOKEN, SHARE_RELAY_ADDR="172.18.0.1:8788")
        r = subprocess.run(["caddy", "validate", "--config", str(CADDYFILE), "--adapter", "caddyfile"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_unset_secrets_do_not_start(self):
        # 中継のトークンや中継の宛先が空のまま、許可リストだけで立ち上がらない (認証が抜けた状態を作らない)
        base = {"SHARE_RELAY_TOKEN": RELAY_TOKEN, "SHARE_RELAY_ADDR": "172.18.0.1:8788"}
        for missing in base:
            env = caddy_env(self.tmp, **{**base, missing: ""})
            r = subprocess.run(["caddy", "validate", "--config", str(CADDYFILE), "--adapter", "caddyfile"], env=env, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, f"{missing} が空でも起動できる")

    def test_no_credentials_is_401_on_every_route(self):
        bad = [
            {},
            basic("alice", "wrong"),
            basic("nobody", "x"),
            basic("", ""),
            {"Authorization": "Basic !!!not-base64"},
            {"Authorization": "Bearer abc"},
            {"Cookie": "share_session=alice.deadbeef"},
            {"Cookie": "share_session="},
            {"X-Share-Relay-Token": RELAY_TOKEN},
        ]
        for target, method, path in self.all_requests():
            for headers in bad:
                status, resp = self.call(target, method, path, headers)
                self.assertEqual(status, 401, (target, method, path, headers))
                self.assertIn("Basic", resp.getheader("WWW-Authenticate", ""), (target, method, path))
        for target in self.upstreams:
            self.assertEqual(self.seen(target), [], f"認証なしの要求が {target} の upstream に届いた")

    def test_allowlist_decides_after_authentication(self):
        # 同じ表を、期限付き (alice) と特権 (root) の両方に当てる。特権だけ通る経路・通らない経路があってはいけない
        results = {}
        for name in PASSWORDS:
            headers = basic(name, PASSWORDS[name])
            results[name] = [(target, method, path, self.call(target, method, path, headers)[0]) for target, method, path in self.all_requests()]
        self.assertEqual(results["alice"], results["root"], "特権 viewer と期限付きで通る経路が違う")
        denied = {(GRAFANA, m, p) for m, p in GRAFANA_DENIED} | {(HEADROOM, m, p) for m, p in HEADROOM_DENIED} | {(BACKSTAGE, m, p) for m, p in BACKSTAGE_DENIED}
        allowed = {(GRAFANA, m, p) for m, p in GRAFANA_ALLOWED} | {(HEADROOM, "GET", p) for p in HEADROOM_ALLOWED} | {(BACKSTAGE, m, p) for m, p in BACKSTAGE_ALLOWED}
        for target, method, path, status in results["alice"]:
            key = (target, method, path)
            if key in denied:
                self.assertEqual(status, 404, key)
            else:
                self.assertIn(key, allowed)
                self.assertEqual(status, 200, key)

    def test_denied_routes_never_reach_upstream(self):
        for name in PASSWORDS:
            for target, denied in [(GRAFANA, GRAFANA_DENIED), (HEADROOM, HEADROOM_DENIED), (BACKSTAGE, BACKSTAGE_DENIED)]:
                for method, path in denied:
                    self.call(target, method, path, basic(name, PASSWORDS[name]))
            for target in self.upstreams:
                self.assertEqual(self.seen(target), [], f"許可リスト外の要求が {target} の upstream に届いた ({name})")

    def test_grafana_gets_viewer_key_not_the_persons_credentials(self):
        status, resp = self.call(GRAFANA, "GET", "/dashboards", {**basic("alice", PASSWORDS["alice"]), "Cookie": "share_session=x; a=b"})
        self.assertEqual(status, 200)
        (_, path, headers), = self.seen(GRAFANA)
        self.assertEqual(path, "/dashboards")
        self.assertEqual(headers["Authorization"], f"Basic {GRAFANA_BASIC}")
        self.assertNotIn("Cookie", headers)
        # 鍵を運んだヘッダ・認証サービスへの問い合わせ用のヘッダは Grafana に渡さない
        self.assertNotIn("X-Share-Grafana-Authorization", headers)
        self.assertNotIn("X-Share-Target", headers)
        self.assertEqual(resp.getheader("Location"), "/after", "Location: http://localhost:3000/ を相対パスに書き換える")

    def test_headroom_goes_through_relay_with_token_only(self):
        status, _ = self.call(HEADROOM, "GET", "/stats-history?days=7", {**basic("alice", PASSWORDS["alice"]), "Cookie": "a=b"})
        self.assertEqual(status, 200)
        (_, path, headers), = self.seen(HEADROOM)
        self.assertEqual(path, "/stats-history?days=7")
        self.assertEqual(headers["X-Share-Relay-Token"], RELAY_TOKEN)
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)

    def test_headroom_head_is_not_allowed(self):
        # #48: 実機の headroom は HEAD に 404 を返す。許可リストの HEAD は中継にも headroom にも渡さない
        for path in HEADROOM_ALLOWED:
            self.assertEqual(self.call(HEADROOM, "HEAD", path, basic("alice", PASSWORDS["alice"]))[0], 404, path)
        self.assertEqual(self.seen(HEADROOM), [])

    def test_headroom_ignores_a_token_header_sent_by_the_client(self):
        # 人が付けた X-Share-Relay-Token は上書きされる。中継のトークンを人の要求から指定させない
        self.call(HEADROOM, "GET", "/health", {**basic("alice", PASSWORDS["alice"]), "X-Share-Relay-Token": "attacker"})
        self.assertEqual(self.seen(HEADROOM)[0][2]["X-Share-Relay-Token"], RELAY_TOKEN)

    def test_backstage_strips_basic_and_domain_and_sets_session_cookie(self):
        status, resp = self.call(BACKSTAGE, "GET", "/", basic("alice", PASSWORDS["alice"]))
        self.assertEqual(status, 200)
        (_, _, headers), = self.seen(BACKSTAGE)
        self.assertNotIn("Authorization", headers, "Basic の資格情報を Backstage に渡した")
        cookies = resp.getheaders()
        set_cookies = [value for key, value in cookies if key.lower() == "set-cookie"]
        self.assertEqual(len([c for c in set_cookies if c.startswith("share_session=")]), 1, set_cookies)
        upstream_cookie = [c for c in set_cookies if c.startswith("backstage-auth=")]
        self.assertEqual(len(upstream_cookie), 1, set_cookies)
        self.assertNotIn("Domain=", upstream_cookie[0])
        session = [c for c in set_cookies if c.startswith("share_session=")][0]
        for attr in ("HttpOnly", "Secure", "SameSite=Lax"):
            self.assertIn(attr, session)

    def session_cookie(self, name="alice"):
        _, resp = self.call(BACKSTAGE, "GET", "/", basic(name, PASSWORDS[name]))
        return [v for k, v in resp.getheaders() if k.lower() == "set-cookie" and v.startswith("share_session=")][0].split(";")[0]

    def test_backstage_bearer_calls_pass_with_the_cookie_and_keep_bearer(self):
        cookie = self.session_cookie()
        self.seen(BACKSTAGE).clear()
        status, resp = self.call(BACKSTAGE, "GET", "/api/catalog/entities", {"Cookie": cookie + "; backstage-auth=abc", "Authorization": "Bearer tok"})
        self.assertEqual(status, 200)
        (_, _, headers), = self.seen(BACKSTAGE)
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertTrue(all(k.lower() != "set-cookie" or not v.startswith("share_session=") for k, v in resp.getheaders()), "cookie で通った要求に cookie を付け直さない")
        # Bearer だけ (cookie なし) は通らない
        self.assertEqual(self.call(BACKSTAGE, "GET", "/api/catalog/entities", {"Authorization": "Bearer tok"})[0], 401)

    def test_cookie_does_not_widen_the_allowlist(self):
        cookie = self.session_cookie()
        for method, path in BACKSTAGE_DENIED:
            self.assertEqual(self.call(BACKSTAGE, method, path, {"Cookie": cookie})[0], 404, (method, path))
        self.assertEqual(self.seen(BACKSTAGE)[1:], [], "許可リスト外の要求が届いた")

    def test_tampered_cookie_is_401(self):
        cookie = self.session_cookie()
        name, _, signature = cookie.partition("=")[2].rpartition(".")
        for value in [f"{name}.{'0' * len(signature)}", f"root.{signature}", f"{name}.{signature[:-1]}", signature, f"{name}."]:
            self.assertEqual(self.call(BACKSTAGE, "GET", "/", {"Cookie": f"share_session={value}"})[0], 401, value)

    def test_removed_credential_is_401_from_the_next_request(self):
        for name in PASSWORDS:
            headers = basic(name, PASSWORDS[name])
            cookie = self.session_cookie(name)
            for target in (GRAFANA, HEADROOM, BACKSTAGE):
                self.assertEqual(self.call(target, "GET", "/health" if target == HEADROOM else "/", headers)[0], 200, (name, target))
            self.write_credentials(**{name: None})
            for target in (GRAFANA, HEADROOM, BACKSTAGE):
                self.assertEqual(self.call(target, "GET", "/health" if target == HEADROOM else "/", headers)[0], 401, (name, target, "Basic"))
            self.assertEqual(self.call(BACKSTAGE, "GET", "/", {"Cookie": cookie})[0], 401, (name, "削除後も cookie が通った"))
            self.write_credentials()

    def test_expired_credential_is_401_from_the_next_request(self):
        headers = basic("alice", PASSWORDS["alice"])
        cookie = self.session_cookie()
        self.assertEqual(self.call(GRAFANA, "GET", "/", headers)[0], 200)
        self.write_credentials(alice=credential(PASSWORDS["alice"], expires_at=time.time() - 1))
        for target in (GRAFANA, HEADROOM, BACKSTAGE):
            self.assertEqual(self.call(target, "GET", "/", headers)[0], 401, target)
        self.assertEqual(self.call(BACKSTAGE, "GET", "/", {"Cookie": cookie})[0], 401, "期限切れの後も cookie が通った")
        self.write_credentials(alice=credential(PASSWORDS["alice"], expires_at=None))  # 同じ名前を作り直す
        self.assertEqual(self.call(BACKSTAGE, "GET", "/", {"Cookie": cookie})[0], 401, "同じ名前の作り直しで古い cookie が通った")

    def grafana_authorizations(self):
        return [headers.get("Authorization") for _, _, headers in self.seen(GRAFANA)]

    def test_viewer_value_change_reaches_grafana_from_the_next_request(self):
        # #58: caddy も認証サービスも立てたまま、Secret share-grafana の写し (試験では JSON の viewer_value) を書き換えると、
        # 次の要求から新しい鍵が Grafana に渡る (Pod の作り直し = Quick Tunnel の URL の変更は要らない)。cookie で通った要求にも同じ鍵が付く
        alice = basic("alice", PASSWORDS["alice"])
        for value in (VIEWER_VALUE, "rotated-1", "rotated 2:with colon", VIEWER_VALUE):
            self.write_credentials(viewer_value=value)
            cookie = self.session_cookie()  # write_credentials は salt を作り直す (古い cookie は効かない) ので、書いたあとに取る
            self.seen(GRAFANA).clear()
            for headers in (alice, {"Cookie": cookie}):
                self.assertEqual(self.call(GRAFANA, "GET", "/dashboards", headers)[0], 200, (value, headers))
            key = base64.b64encode(f"viewer:{value}".encode()).decode()
            self.assertEqual(self.grafana_authorizations(), [f"Basic {key}"] * 2, value)
        self.assertIsNone(self.caddy.poll(), "caddy が立て直された")
        self.assertIsNone(self.auth.poll(), "認証サービスが立て直された")

    def test_grafana_is_denied_while_the_viewer_value_is_missing_or_empty(self):
        # #58: Secret share-grafana が無い・空のあいだ、Grafana の経路は認証が通った要求にも 503 (許可リストの内も外も)。通さず、鍵なしで渡すこともしない。
        # 認証が通らない要求は Secret の有無によらず 401 (有無を探らせない)。headroom・backstage の経路には関わらない
        for viewer_value, what in ((None, "Secret が無い"), ("", "値が空")):
            self.write_credentials(viewer_value=viewer_value)
            authenticated = [basic("alice", PASSWORDS["alice"]), basic("root", PASSWORDS["root"]), {"Cookie": self.session_cookie()}]
            self.seen(GRAFANA).clear()
            for method, path in GRAFANA_ALLOWED + GRAFANA_DENIED:
                for headers in authenticated:
                    self.assertEqual(self.call(GRAFANA, method, path, headers)[0], 503, (what, method, path, headers))
                for headers in ({}, basic("alice", "wrong"), {"Authorization": "Bearer attacker"}):
                    self.assertEqual(self.call(GRAFANA, method, path, headers)[0], 401, (what, method, path, headers))
            self.assertEqual(self.seen(GRAFANA), [], f"{what}のとき Grafana の upstream に届いた")
            self.assertEqual(self.call(HEADROOM, "GET", "/health", authenticated[0])[0], 200, what)
            self.assertEqual(self.call(BACKSTAGE, "GET", "/", authenticated[0])[0], 200, what)
        # Secret が揃えば、次の要求から通る (再起動なし)
        self.write_credentials()
        authenticated = [basic("alice", PASSWORDS["alice"])]
        self.seen(GRAFANA).clear()
        self.assertEqual(self.call(GRAFANA, "GET", "/dashboards", authenticated[0])[0], 200)
        self.assertEqual(self.grafana_authorizations(), [f"Basic {GRAFANA_BASIC}"])

    def test_a_persons_credentials_never_reach_grafana(self):
        # #58: 人が付けた Authorization (Basic・Bearer・同じ名前を複数本・小文字の名前)・Cookie・Proxy-Authorization と、鍵を運ぶヘッダの偽物は、
        # Grafana に届かない。届くのは認証サービスの応答から組んだ viewer の Basic の 1 本だけ。
        # 人が X-Share-Target を偽っても、認証サービスへの問い合わせでは header_up が置き換える (対象を偽って鍵を外せない。Grafana には資格情報でなく素通り)
        evil = "Basic " + base64.b64encode(b"admin:admin").decode()
        cookie = self.session_cookie()
        cases = {
            "basic": {**basic("alice", PASSWORDS["alice"]), "X-Share-Grafana-Authorization": evil, "X-Share-Target": "headroom"},
            "root": {**basic("root", PASSWORDS["root"]), "Cookie": "share_session=x; a=b", "Proxy-Authorization": evil},
            "cookie and bearer": {"Cookie": cookie, "Authorization": "Bearer attacker-token", "X-Share-Grafana-Authorization": evil},
            "lowercase names": {"authorization": basic("alice", PASSWORDS["alice"])["Authorization"], "x-share-grafana-authorization": evil},
        }
        watched = {"authorization", "cookie", "proxy-authorization", "x-share-grafana-authorization"}

        def watched_headers(raw):
            return [(name.lower(), value) for name, value in raw if name.lower() in watched]

        for name, headers in cases.items():
            self.grafana.raw.clear()
            self.assertEqual(self.call(GRAFANA, "GET", "/dashboards", headers)[0], 200, name)
            (raw,) = self.grafana.raw
            self.assertEqual(watched_headers(raw), [("authorization", f"Basic {GRAFANA_BASIC}")], name)
        # 同じ名前のヘッダが複数本ある要求 (最初の Basic で通り、残りも全部置き換わる)
        self.grafana.raw.clear()
        conn = http.client.HTTPConnection("127.0.0.1", self.ports[GRAFANA], timeout=10)
        try:
            conn.putrequest("GET", "/dashboards")
            conn.putheader("Authorization", basic("alice", PASSWORDS["alice"])["Authorization"])
            conn.putheader("Authorization", "Bearer second")
            conn.putheader("X-Share-Grafana-Authorization", evil)
            conn.putheader("X-Share-Grafana-Authorization", evil)
            conn.endheaders()
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 200)
        finally:
            conn.close()
        (raw,) = self.grafana.raw
        self.assertEqual(watched_headers(raw), [("authorization", f"Basic {GRAFANA_BASIC}")], "複数本の Authorization")
        # 認証が通らないなら、偽の鍵のヘッダがあっても 401 で、Grafana に届かない
        self.grafana.seen.clear()
        for headers in ({"X-Share-Grafana-Authorization": evil}, {"Authorization": evil, "X-Share-Grafana-Authorization": evil},
                        {"Cookie": "share_session=alice.deadbeef", "X-Share-Grafana-Authorization": evil}):
            self.assertEqual(self.call(GRAFANA, "GET", "/dashboards", headers)[0], 401, headers)
        self.assertEqual(self.seen(GRAFANA), [])

    def test_a_forged_key_header_does_not_open_grafana_while_the_viewer_value_is_missing(self):
        # #58: 認証サービスが鍵を付けられないとき、人が X-Share-Grafana-Authorization を付けても、caddy はそれを鍵として通さない (人の値を先に消す)
        evil = "Basic " + base64.b64encode(b"admin:admin").decode()
        self.write_credentials(viewer_value=None)
        for headers in ({**basic("alice", PASSWORDS["alice"]), "X-Share-Grafana-Authorization": evil},
                        {"Cookie": self.session_cookie(), "Authorization": "Bearer attacker", "X-Share-Grafana-Authorization": evil}):
            self.assertEqual(self.call(GRAFANA, "GET", "/dashboards", headers)[0], 503, headers)
        self.assertEqual(self.seen(GRAFANA), [])

    def test_the_viewer_key_goes_only_to_grafana(self):
        # #58: viewer の鍵 (Basic) は headroom の中継にも backstage にも渡らない。人が付けた鍵のヘッダも、その 2 つには届かない
        evil = "Basic " + base64.b64encode(b"admin:admin").decode()
        alice = {**basic("alice", PASSWORDS["alice"]), "X-Share-Grafana-Authorization": evil}
        self.assertEqual(self.call(HEADROOM, "GET", "/health", alice)[0], 200)
        self.assertEqual(self.call(BACKSTAGE, "GET", "/", alice)[0], 200)
        for upstream in (self.headroom_relay, self.backstage):
            (raw,) = upstream.raw
            for name, value in raw:
                self.assertNotIn("share-grafana-authorization", name.lower())
                self.assertNotIn(GRAFANA_BASIC, value, name)
                self.assertNotIn(evil, value, name)

    def test_caddy_denies_grafana_when_the_auth_answer_has_no_key(self):
        # #58 の二重の止め: 認証サービスが 200 を返しても鍵を付けなかった (古い版・不具合) とき、caddy は人の Authorization を残したまま渡さず 503。
        # 人が偽の鍵・偽の対象を付けても同じ。各ポートは自分の対象を認証サービスに名乗る (X-Share-Target は header_up が人の値を置き換える)
        stub = ThreadingHTTPServer(("127.0.0.1", 0), AuthStub)
        stub.targets = []
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        self.addCleanup(stub.server_close)
        self.addCleanup(stub.shutdown)
        ports = self.closed_caddy("nokey", f"127.0.0.1:{stub.server_port}")
        evil = "Basic " + base64.b64encode(b"admin:admin").decode()
        for headers in ({"Authorization": "Bearer attacker"},
                        {"Authorization": "Bearer attacker", "X-Share-Grafana-Authorization": evil, "X-Share-Target": "headroom"}):
            self.assertEqual(request(ports[GRAFANA], "GET", "/dashboards", headers)[0], 503, headers)
        self.assertEqual(self.seen(GRAFANA), [], "鍵が無いのに Grafana の upstream に届いた")
        self.assertEqual(request(ports[HEADROOM], "GET", "/health", {"X-Share-Target": "grafana"})[0], 200)
        self.assertEqual(request(ports[BACKSTAGE], "GET", "/", {"X-Share-Target": "grafana"})[0], 200)
        self.assertEqual(stub.targets, ["grafana", "grafana", "headroom", "backstage"])

    def closed_caddy(self, name, auth_addr):
        """認証サービスに届かない設定の caddy を、別に立てる。(3 対象のポート) を返す。終わったら止める。"""
        ports = {GRAFANA: free_port(), HEADROOM: free_port(), BACKSTAGE: free_port()}
        env = caddy_env(self.tmp / name, SHARE_AUTH_ADDR=auth_addr, SHARE_RELAY_TOKEN=RELAY_TOKEN,
                        SHARE_RELAY_ADDR=f"127.0.0.1:{self.headroom_relay.server_port}",
                        SHARE_GRAFANA_UPSTREAM=f"127.0.0.1:{self.grafana.server_port}",
                        SHARE_BACKSTAGE_UPSTREAM=f"127.0.0.1:{self.backstage.server_port}",
                        SHARE_GRAFANA_PORT=str(ports[GRAFANA]), SHARE_HEADROOM_PORT=str(ports[HEADROOM]), SHARE_BACKSTAGE_PORT=str(ports[BACKSTAGE]))
        # 認証サービスに聞けない要求は 1 つごとにエラーを stderr に書く。読まない PIPE だと詰まって caddy が止まるので、ファイルに流す
        log_path = self.tmp / f"{name}.log"
        with open(log_path, "w") as log:
            proc = subprocess.Popen(["caddy", "run", "--config", str(CADDYFILE), "--adapter", "caddyfile"], env=env, stdout=subprocess.DEVNULL, stderr=log)
        proc.log_path = log_path
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.terminate)
        for port in ports.values():
            wait_listening(proc, port, "caddy")
        return ports

    def assert_everything_closed(self, ports):
        """全対象の全経路が、資格情報があっても (認証サービスに聞けないので) 通らず、upstream に届かない。"""
        # 正しい Basic・正しい名前の cookie・何も無し。認証サービスが答えられないとき、どれも通ってはいけない
        headers = [{}, basic("alice", PASSWORDS["alice"]), basic("root", PASSWORDS["root"]),
                   {"Cookie": "share_session=alice.deadbeef"}, {"Authorization": "Bearer abc"}]
        for target, method, path in self.all_requests():
            for h in headers:
                status, _ = request(ports[target], method, path, h)
                self.assertIn(status, (401, 502, 503), (target, method, path, h))
        for target in self.upstreams:
            self.assertEqual(self.seen(target), [], f"認証サービスに届かないのに {target} の upstream に届いた")

    def test_auth_service_down_is_closed(self):
        # 認証サービスに届かなければ 401 でも 200 でもなく、通さない (502)。別の caddy を、閉じたポートの認証サービスで立てる
        self.assert_everything_closed(self.closed_caddy("down", f"127.0.0.1:{free_port()}"))

    def test_empty_auth_addr_is_closed(self):
        # #52: SHARE_AUTH_ADDR が空でも caddy は起動するが、全経路が通らない (安全側。caddy は宛先なしで 503 を返す)。
        # Deployment が env を渡し損ねたときに当たる。許可リストの内も外も、Basic があっても通らない
        self.assert_everything_closed(self.closed_caddy("empty", ""))


@unittest.skipUnless(shutil.which("caddy"), "caddy が無い")
class ShareCaddyEntrypoint(unittest.TestCase):
    """#64: Deployment と同じ起動 (entrypoint.sh) で caddy を立てる。Secret の値 (環境変数) の有無で、全拒否か通常かが決まる。

    認証サービスは正しい資格情報を持っている。全拒否のときは、それがあっても通らない (認証サービスに聞くことすらしない)。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.source = cls.tmp / "credentials.json"
        cls.source.write_text(json.dumps({"credentials": {"alice": credential(PASSWORDS["alice"])}, "session_key": SESSION_KEY,
                                          "viewer_value": VIEWER_VALUE}), encoding="utf-8")
        cls.auth_port = free_port()
        cls.auth = subprocess.Popen(
            [sys.executable, "-B", str(AUTH_SERVICE)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env={**os.environ, "AUTH_SOURCE": f"file:{cls.source}", "AUTH_LISTEN": f"127.0.0.1:{cls.auth_port}", "AUTH_CACHE_TTL": "0", "AUTH_VERIFY_TTL": "0"})
        wait_listening(cls.auth, cls.auth_port, "認証サービス")
        cls.grafana, cls.headroom_relay, cls.backstage = start_upstream(), start_upstream(), start_upstream()

    @classmethod
    def tearDownClass(cls):
        cls.auth.terminate()
        cls.auth.wait(timeout=10)
        cls.auth.stderr.close()
        for upstream in (cls.grafana, cls.headroom_relay, cls.backstage):
            upstream.shutdown()
            upstream.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.upstreams = {GRAFANA: self.grafana, HEADROOM: self.headroom_relay, BACKSTAGE: self.backstage}
        for upstream in self.upstreams.values():
            upstream.seen.clear()
            upstream.raw.clear()
        self.ports = {GRAFANA: free_port(), HEADROOM: free_port(), BACKSTAGE: free_port()}

    def start(self, name, **secret_env):
        """entrypoint.sh から caddy を起動する (self.ports で待ち受ける)。secret_env は Pod の環境変数になる Secret の値。"""
        env = caddy_env(
            self.tmp / name,
            SHARE_CADDY_DIR=str(HERE), SHARE_AUTH_ADDR=f"127.0.0.1:{self.auth_port}",
            SHARE_GRAFANA_UPSTREAM=f"127.0.0.1:{self.grafana.server_port}", SHARE_BACKSTAGE_UPSTREAM=f"127.0.0.1:{self.backstage.server_port}",
            SHARE_GRAFANA_PORT=str(self.ports[GRAFANA]), SHARE_HEADROOM_PORT=str(self.ports[HEADROOM]), SHARE_BACKSTAGE_PORT=str(self.ports[BACKSTAGE]))
        for var in ("SHARE_RELAY_TOKEN", "SHARE_RELAY_ADDR"):
            env.pop(var, None)
        env.update(secret_env)
        log_path = self.tmp / f"{name}.log"
        with open(log_path, "w") as log:
            proc = subprocess.Popen(["sh", str(ENTRYPOINT)], env=env, stdout=subprocess.DEVNULL, stderr=log)
        proc.log_path = log_path
        for port in self.ports.values():
            wait_listening(proc, port, "caddy")
        return proc

    def stop(self, proc):
        proc.terminate()
        proc.wait(timeout=10)

    complete = {"SHARE_RELAY_TOKEN": RELAY_TOKEN}

    def secrets(self):
        return {**self.complete, "SHARE_RELAY_ADDR": f"127.0.0.1:{self.headroom_relay.server_port}"}

    def all_requests(self):
        return (
            [(GRAFANA, m, p) for m, p in GRAFANA_ALLOWED + GRAFANA_DENIED]
            + [(HEADROOM, m, p) for m, p in [("GET", p) for p in HEADROOM_ALLOWED] + HEADROOM_DENIED]
            + [(BACKSTAGE, m, p) for m, p in BACKSTAGE_ALLOWED + BACKSTAGE_DENIED]
        )

    def assert_everything_denied(self):
        # 正しい Basic (alice) も、署名の合わない cookie も、何も無しも、許可リストの内も外も、3 つの経路のどれも 503。upstream には 1 つも届かない
        headers = [{}, basic("alice", PASSWORDS["alice"]), {"Cookie": "share_session=alice.deadbeef"}, {"Authorization": "Bearer abc"}, {"X-Share-Relay-Token": RELAY_TOKEN}]
        for target, method, path in self.all_requests():
            for h in headers:
                status, _ = request(self.ports[target], method, path, h)
                self.assertEqual(status, 503, (target, method, path, h))
        for target, upstream in self.upstreams.items():
            self.assertEqual(upstream.seen, [], f"Secret が揃っていないのに {target} の upstream に届いた")

    def test_no_secret_denies_every_route(self):
        # share-host が無い (optional の参照で環境変数が 1 つも無い) Pod
        proc = self.start("none")
        self.addCleanup(self.stop, proc)
        self.assert_everything_denied()

    def test_empty_secret_values_deny_every_route(self):
        # Secret はあるが値が空 (中継のトークンのファイルが空だった、など)。空のトークンで通さない
        for name, env in {"empty-all": {k: "" for k in [*self.complete, "SHARE_RELAY_ADDR"]}, "empty-token": {**self.secrets(), "SHARE_RELAY_TOKEN": ""}}.items():
            proc = self.start(name, **env)
            try:
                self.assert_everything_denied()
            finally:
                self.stop(proc)

    def test_a_missing_secret_value_denies_every_route(self):
        # 2 つ (中継の宛先・トークン) のどちらが欠けても全拒否。一部だけ揃った状態で、揃った分の経路だけが開かない
        for missing in [*self.complete, "SHARE_RELAY_ADDR"]:
            proc = self.start(f"missing-{missing}", **{k: v for k, v in self.secrets().items() if k != missing})
            try:
                self.assert_everything_denied()
            finally:
                self.stop(proc)

    def test_closed_startup_says_why(self):
        proc = self.start("why")
        self.addCleanup(self.stop, proc)
        self.assertIn("全経路を 503 で拒否", proc.log_path.read_text())

    def test_after_the_secrets_exist_the_recreated_pod_serves_normally(self):
        # Secret ができて Pod が作り直された (環境変数が揃った) あとは、同じポートで通常どおり: 認証なしは 401、許可リストの内は 200・外は 404、
        # Grafana には viewer の鍵が渡る (Basic は認証サービスが Secret share-grafana から組み立てる。caddy の環境変数には viewer のパスワードが無い)
        closed = self.start("before")
        self.assert_everything_denied()
        self.stop(closed)
        proc = self.start("after", **self.secrets())
        self.addCleanup(self.stop, proc)
        for target, path in [(GRAFANA, "/api/search"), (HEADROOM, "/health"), (BACKSTAGE, "/")]:
            self.assertEqual(request(self.ports[target], "GET", path)[0], 401, (target, path))
        self.assertEqual(request(self.ports[GRAFANA], "GET", "/profile/password", basic("alice", PASSWORDS["alice"]))[0], 404)
        self.assertEqual(request(self.ports[GRAFANA], "GET", "/api/search", basic("alice", PASSWORDS["alice"]))[0], 200)
        (_, _, headers), = self.grafana.seen
        self.assertEqual(headers["Authorization"], f"Basic {GRAFANA_BASIC}")
        self.assertEqual(request(self.ports[HEADROOM], "GET", "/health", basic("alice", PASSWORDS["alice"]))[0], 200)
        (_, _, headers), = self.headroom_relay.seen
        self.assertEqual(headers["X-Share-Relay-Token"], RELAY_TOKEN)
        self.assertEqual(request(self.ports[BACKSTAGE], "GET", "/", basic("alice", PASSWORDS["alice"]))[0], 200)


if __name__ == "__main__":
    unittest.main()
