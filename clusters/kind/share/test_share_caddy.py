"""share Pod の Caddyfile (clusters/kind/share/Caddyfile) の経路の統合試験。

標準ライブラリだけ: python3 -B -m unittest discover -s clusters/kind/share -p test_share_caddy.py

稼働中のクラスタにもホストにも触れない。caddy と認証サービス (share_auth.py、AUTH_SOURCE=file:) を 127.0.0.1 の空きポートで
起動し、upstream (grafana・headroom の中継・backstage) は偽の HTTP サーバー (標準ライブラリ)。
確かめるのは拒否: 認証なしは全経路 401、認証があっても許可リストの外は 404 で upstream に届かない、
delete・期限切れは次の要求から 401 (Backstage の cookie 経路でも)、特権 viewer に追加の経路が通らない。

`caddy` が無い環境では飛ばす (`just ci` は devShells.ci に caddy を入れて必ず走らせる)。
"""
import base64
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
AUTH_SERVICE = HERE / "share_auth.py"
RELAY_TOKEN = "0123456789abcdef0123456789abcdef"
GRAFANA_BASIC = base64.b64encode(b"viewer:viewer-secret").decode()
SESSION_KEY = "test-session-key"
ITERATIONS = 100_000  # share_auth.MIN_ITERATIONS。試験を速くする
PASSWORDS = {"alice": "alice-password", "root": "root-password"}

GRAFANA, HEADROOM, BACKSTAGE = "grafana", "headroom", "backstage"

# 対象ごとの (通る要求, 通らない要求)。通らない要求は認証があっても 404 で、upstream に届かない
GRAFANA_ALLOWED = [("GET", "/"), ("GET", "/dashboards"), ("GET", "/d/abc/x?orgId=1"), ("POST", "/api/ds/query"), ("GET", "/goto/abc")]
GRAFANA_DENIED = [
    (method, path)
    for method in ["GET", "POST", "PUT", "PATCH"]
    for path in ["/profile/password", "/profile/password/", "/api/user/password", "/api/user/password/",
                 "/PROFILE/password", "//profile/password", "/profile/%70assword", "/api/user/password?x=1"]
]
HEADROOM_ALLOWED = ["/dashboard", "/health", "/stats", "/stats-history", "/stats-lifetime", "/transformations/feed", "/favicon.ico"]
HEADROOM_DENIED = [
    ("POST", "/v1/messages"), ("GET", "/v1/messages"), ("POST", "/stats/reset"), ("GET", "/stats/reset"),
    ("POST", "/settings"), ("GET", "/settings"), ("POST", "/dashboard/settings"), ("POST", "/cache/clear"),
    ("POST", "/dashboard"), ("PUT", "/stats"), ("DELETE", "/health"), ("GET", "/stats/"),
    ("GET", "//v1/messages"), ("GET", "/dashboard/../v1/messages"), ("GET", "/dashboard/%2e%2e/v1/messages"),
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
]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
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
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def wait_listening(proc, port, what):
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{what} が終了した: " + proc.stderr.read().decode())
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
                SHARE_GRAFANA_BASIC=GRAFANA_BASIC,
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
    def write_credentials(cls, **overrides):
        entries = {
            "alice": credential(PASSWORDS["alice"], expires_at=time.time() + 3600),
            "root": credential(PASSWORDS["root"], privileged=True),
        }
        entries.update(overrides)
        entries = {name: entry for name, entry in entries.items() if entry is not None}
        cls.source.write_text(json.dumps({"credentials": entries, "session_key": SESSION_KEY}), encoding="utf-8")

    def setUp(self):
        self.write_credentials()
        self.addCleanup(self.write_credentials)
        self.upstreams = {GRAFANA: self.grafana, HEADROOM: self.headroom_relay, BACKSTAGE: self.backstage}
        for upstream in self.upstreams.values():
            upstream.seen.clear()

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
        env = caddy_env(self.tmp, SHARE_GRAFANA_BASIC=GRAFANA_BASIC, SHARE_RELAY_TOKEN=RELAY_TOKEN, SHARE_RELAY_ADDR="172.18.0.1:8788")
        r = subprocess.run(["caddy", "validate", "--config", str(CADDYFILE), "--adapter", "caddyfile"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_unset_secrets_do_not_start(self):
        # 中継のトークンや中継の宛先が空のまま、許可リストだけで立ち上がらない (認証が抜けた状態を作らない)
        base = {"SHARE_GRAFANA_BASIC": GRAFANA_BASIC, "SHARE_RELAY_TOKEN": RELAY_TOKEN, "SHARE_RELAY_ADDR": "172.18.0.1:8788"}
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

    def test_auth_service_down_is_closed(self):
        # 認証サービスに届かなければ 401 でも 200 でもなく、通さない (502)。別の caddy を、閉じたポートの認証サービスで立てる
        port = free_port()
        dead = free_port()
        env = caddy_env(self.tmp / "down", SHARE_AUTH_ADDR=f"127.0.0.1:{dead}", SHARE_GRAFANA_BASIC=GRAFANA_BASIC, SHARE_RELAY_TOKEN=RELAY_TOKEN,
                        SHARE_RELAY_ADDR="127.0.0.1:1", SHARE_GRAFANA_UPSTREAM=f"127.0.0.1:{self.grafana.server_port}",
                        SHARE_GRAFANA_PORT=str(port), SHARE_HEADROOM_PORT=str(free_port()), SHARE_BACKSTAGE_PORT=str(free_port()))
        proc = subprocess.Popen(["caddy", "run", "--config", str(CADDYFILE), "--adapter", "caddyfile"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            wait_listening(proc, port, "caddy")
            status, _ = request(port, "GET", "/", basic("alice", PASSWORDS["alice"]))
            self.assertIn(status, (401, 502, 503))
            self.assertEqual(self.seen(GRAFANA), [])
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
