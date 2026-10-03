"""headroom の中継 (just/share-relay.Caddyfile と just/share-relay.sh) の試験。

標準ライブラリだけ: python3 -B -m unittest discover -s just -p test_share_relay.py
稼働中のクラスタにもホストの docker にも触れない:
  - Caddyfile は caddy を 127.0.0.1 の空きポートで起動し、upstream は偽の HTTP サーバー (本物の headroom ではない)
  - share-relay.sh は PATH の先頭に置いた偽の docker・kubectl・curl で、引数と作るファイルだけを見る
`caddy` が無い環境では Caddyfile の試験を飛ばす (`just ci` は devShells.ci に caddy を入れて必ず走らせる)。
"""
import http.client
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

JUST = Path(__file__).resolve().parent
CADDYFILE = JUST / "share-relay.Caddyfile"
SCRIPT = JUST / "share-relay.sh"
TOKEN = "0123456789abcdef0123456789abcdef"
ALLOWED = ["/dashboard", "/health", "/stats", "/stats-history", "/stats-lifetime", "/transformations/feed", "/favicon.ico"]
# 許可リストの外。headroom のプロキシ本体・設定の変更・リセット (トークンがあっても通さない)
DENIED = [
    ("POST", "/v1/messages"), ("GET", "/v1/messages"), ("POST", "/stats/reset"), ("GET", "/stats/reset"),
    ("POST", "/settings"), ("GET", "/settings"), ("POST", "/dashboard/settings"), ("GET", "/dashboard/settings"),
    ("POST", "/cache/clear"), ("GET", "/cache/clear"), ("POST", "/dashboard"), ("PUT", "/stats"), ("DELETE", "/health"),
    ("GET", "/"), ("GET", "/stats/"), ("GET", "/dashboard/"), ("GET", "//v1/messages"), ("GET", "/dashboard/../v1/messages"),
    ("GET", "/dashboard/%2e%2e/v1/messages"), ("OPTIONS", "/dashboard"),
]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(port, method, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


class Upstream(BaseHTTPRequestHandler):
    """どんな要求にも 200 を返し、受けた要求を記録する。404 が返れば、それは caddy が返したもの。"""

    def _record(self):
        self.server.seen.append((self.command, self.path, dict(self.headers)))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b"ok")

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_OPTIONS = _record

    def log_message(self, *args):
        pass


def start_caddy(tmp, upstream_port, token):
    port = free_port()
    env = {
        **os.environ,
        "SHARE_RELAY_BIND": "127.0.0.1",
        "SHARE_RELAY_PORT": str(port),
        "SHARE_RELAY_TOKEN": token,
        "SHARE_RELAY_UPSTREAM": f"127.0.0.1:{upstream_port}",
        "XDG_CONFIG_HOME": str(tmp / "config"),
        "XDG_DATA_HOME": str(tmp / "data"),
    }
    proc = subprocess.Popen(["caddy", "run", "--config", str(CADDYFILE), "--adapter", "caddyfile"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("caddy が終了した: " + proc.stderr.read().decode())
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return proc, port
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("caddy が待ち受けない")


@unittest.skipUnless(shutil.which("caddy"), "caddy が無い")
class Relay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        cls.upstream.seen = []
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.caddy, cls.port = start_caddy(cls.tmp, cls.upstream.server_port, TOKEN)

    @classmethod
    def tearDownClass(cls):
        cls.caddy.terminate()
        cls.caddy.wait(timeout=10)
        cls.caddy.stderr.close()
        cls.upstream.shutdown()
        cls.upstream.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.upstream.seen.clear()

    def auth(self):
        return {"X-Share-Relay-Token": TOKEN}

    def test_caddyfile_validates(self):
        env = {**os.environ, "SHARE_RELAY_BIND": "127.0.0.1", "SHARE_RELAY_PORT": "8788", "SHARE_RELAY_TOKEN": TOKEN,
               "XDG_CONFIG_HOME": str(self.tmp / "config"), "XDG_DATA_HOME": str(self.tmp / "data")}
        r = subprocess.run(["caddy", "validate", "--config", str(CADDYFILE), "--adapter", "caddyfile"],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_empty_token_does_not_start(self):
        # トークンが空のとき、認証が抜けた状態で立ち上がらない (Caddy が設定を読めず終了する)
        env = {**os.environ, "SHARE_RELAY_BIND": "127.0.0.1", "SHARE_RELAY_PORT": "8788", "SHARE_RELAY_TOKEN": "",
               "XDG_CONFIG_HOME": str(self.tmp / "config"), "XDG_DATA_HOME": str(self.tmp / "data")}
        r = subprocess.run(["caddy", "validate", "--config", str(CADDYFILE), "--adapter", "caddyfile"],
                           env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)

    def test_no_token_is_401_on_every_route(self):
        wrong = [{}, {"X-Share-Relay-Token": "wrong"}, {"X-Share-Relay-Token": ""}, {"X-Share-Relay-Token": TOKEN[:-1]},
                 {"X-Share-Relay-Token": TOKEN + "0"}, {"Authorization": f"Bearer {TOKEN}"}]
        routes = [("GET", p) for p in ALLOWED] + DENIED
        for headers in wrong:
            for method, path in routes:
                self.assertEqual(request(self.port, method, path, headers), 401, (method, path, headers))
        self.assertEqual(request(self.port, "GET", f"/dashboard?X-Share-Relay-Token={TOKEN}"), 401, "クエリのトークンは受けない")
        self.assertEqual(self.upstream.seen, [], "認証なしの要求が upstream に届いた")

    def test_token_allows_read_only_dashboard_routes(self):
        for method in ["GET", "HEAD"]:
            for path in ALLOWED:
                self.assertEqual(request(self.port, method, path, self.auth()), 200, (method, path))
        self.assertEqual(len(self.upstream.seen), 2 * len(ALLOWED))
        for method, path, headers in self.upstream.seen:
            self.assertIn(path, ALLOWED)
            self.assertNotIn("X-Share-Relay-Token", headers, "トークンを upstream に渡した")
            self.assertEqual(headers["Host"], f"127.0.0.1:{self.upstream.server_port}")

    def test_query_string_passes_through_on_allowed_route(self):
        self.assertEqual(request(self.port, "GET", "/stats-history?days=7", self.auth()), 200)
        self.assertEqual(self.upstream.seen[0][1], "/stats-history?days=7")

    def test_token_does_not_open_anything_outside_the_allowlist(self):
        for method, path in DENIED:
            self.assertEqual(request(self.port, method, path, self.auth()), 404, (method, path))
        self.assertEqual(self.upstream.seen, [], "許可リスト外の要求が upstream に届いた")


class RelayScript(unittest.TestCase):
    """share-relay.sh を、偽の docker・kubectl・curl で実行する。実物の docker・kubectl・クラスタには触れない。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        self.state = self.tmp / "state" / "share"
        self.token_file = self.state / "relay-token"
        self.write_stub("docker", f"""
echo "docker $*" >> "{self.log}"
if [ "$1 $2" = "network inspect" ]; then printf '%b' "${{FAKE_GATEWAYS-fc00:f853:ccd:e793::1\\n172.18.0.1\\n}}"; exit 0; fi
if [ "$1" = run ]; then echo "env SHARE_RELAY_BIND=$SHARE_RELAY_BIND SHARE_RELAY_PORT=$SHARE_RELAY_PORT" >> "{self.log}"; fi
""")
        self.write_stub("kubectl", f"""
echo "kubectl $*" >> "{self.log}"
case "$*" in *apply*) cat >/dev/null ;; *create*) echo "kind: Stub" ;; esac
""")
        self.write_stub("curl", "echo 401")

    def write_stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(0o755)

    def run_script(self, *args, **env):
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                              env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", **env})

    def up(self, **env):
        return self.run_script("up", str(self.token_file), str(JUST), "kind-test", "8788", **env)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_up_starts_container_on_the_ipv4_gateway(self):
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stderr)
        run = [c for c in self.calls() if c.startswith("docker run")]
        self.assertEqual(len(run), 1)
        for expected in ["--name home-k8s-share-relay", "--network host", "--restart unless-stopped",
                         "-e SHARE_RELAY_BIND -e SHARE_RELAY_PORT -e SHARE_RELAY_TOKEN",
                         f"-v {self.state}/relay.Caddyfile:/etc/caddy/Caddyfile:ro", "caddy:2.11.6-alpine"]:
            self.assertIn(expected, run[0])
        self.assertIn("env SHARE_RELAY_BIND=172.18.0.1 SHARE_RELAY_PORT=8788", self.calls())
        self.assertNotIn(self.token_file.read_text(), run[0], "トークンがコマンドラインに出た")

    def test_up_creates_token_file_and_secret(self):
        self.assertEqual(self.up().returncode, 0)
        token = self.token_file.read_text()
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertEqual(self.token_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state / "relay.Caddyfile").read_text(), CADDYFILE.read_text())
        secret = [c for c in self.calls() if "create secret generic share-host" in c]
        self.assertEqual(len(secret), 1)
        self.assertIn("-n share", secret[0])
        self.assertIn("--context kind-test", secret[0])
        self.assertIn("SHARE_RELAY_ADDR=172.18.0.1:8788", secret[0])
        self.assertIn(f"SHARE_RELAY_TOKEN={self.token_file}", secret[0])
        self.assertNotIn(token, secret[0], "トークンが kubectl の引数に出た")

    def test_up_again_keeps_the_token(self):
        self.up()
        first = self.token_file.read_text()
        self.up()
        self.assertEqual(self.token_file.read_text(), first)
        self.assertEqual(len([c for c in self.calls() if c.startswith("docker run")]), 2)
        self.assertEqual(len([c for c in self.calls() if c == "docker rm -f home-k8s-share-relay"]), 2, "起動前に古いコンテナを消す")

    def test_up_refuses_without_a_gateway(self):
        r = self.up(FAKE_GATEWAYS="")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([c for c in self.calls() if c.startswith("docker run")], [])

    def test_up_refuses_an_empty_token(self):
        # 空のトークンでは docker run まで進まない
        self.state.mkdir(parents=True)
        self.token_file.write_text("\n")
        r = self.up()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([c for c in self.calls() if c.startswith("docker run")], [])

    def test_up_fails_when_the_relay_does_not_answer(self):
        self.write_stub("curl", "echo 000")
        r = self.up()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("待ち受けていない", r.stderr)

    def test_down_removes_the_container_and_keeps_the_token(self):
        self.up()
        before = self.token_file.read_text()
        self.log.write_text("")
        r = self.run_script("down")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls(), ["docker rm -f home-k8s-share-relay"])
        self.assertEqual(self.token_file.read_text(), before)

    def test_bad_action_stops_with_usage(self):
        r = self.run_script("nope")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)


if __name__ == "__main__":
    unittest.main()
