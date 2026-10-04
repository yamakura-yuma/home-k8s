"""headroom の中継 (just/share-relay.Caddyfile と just/share-relay.sh) の試験。

標準ライブラリだけ: python3 -B -m unittest discover -s just -p test_share_relay.py
稼働中のクラスタにもホストの docker にも触れない:
  - Caddyfile は caddy を 127.0.0.1 の空きポートで起動し、upstream は偽の HTTP サーバー (本物の headroom ではない)
  - share-relay.sh は PATH の先頭に置いた偽の docker・kubectl・curl で、引数と作るファイルだけを見る
`caddy` が無い環境では Caddyfile の試験を飛ばす (`just ci` は devShells.ci に caddy を入れて必ず走らせる)。
"""
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

JUST = Path(__file__).resolve().parent
CADDYFILE = JUST / "share-relay.Caddyfile"
SCRIPT = JUST / "share-relay.sh"
FAKE_KUBECTL = JUST / "fake_kubectl.py"
TOKEN = "0123456789abcdef0123456789abcdef"
ALLOWED = ["/dashboard", "/health", "/stats", "/stats-history", "/stats-lifetime", "/transformations/feed", "/favicon.ico"]
# 許可リストの外。headroom のプロキシ本体・設定の変更・リセット (トークンがあっても通さない)
DENIED = [
    ("POST", "/v1/messages"), ("GET", "/v1/messages"), ("POST", "/stats/reset"), ("GET", "/stats/reset"),
    ("POST", "/settings"), ("GET", "/settings"), ("POST", "/dashboard/settings"), ("GET", "/dashboard/settings"),
    ("POST", "/cache/clear"), ("GET", "/cache/clear"), ("POST", "/dashboard"), ("PUT", "/stats"), ("DELETE", "/health"),
    ("GET", "/"), ("GET", "/stats/"), ("GET", "/dashboard/"), ("GET", "//v1/messages"), ("GET", "/dashboard/../v1/messages"),
    ("GET", "/dashboard/%2e%2e/v1/messages"), ("OPTIONS", "/dashboard"),
    # #53: caddy の path は大文字小文字を区別しないが、許可リストは区別する (headroom は区別する)
    *[("GET", variant) for path in ALLOWED for variant in (path.upper(), path[:2].upper() + path[2:], path[:-1] + path[-1].upper())],
    ("GET", "/Health"), ("GET", "/DASHBOARD"), ("GET", "/Favicon.ico"), ("GET", "/favicon.ICO"), ("GET", "/Stats-History"),
    ("GET", "/health%0a"), ("GET", "/%48ealth"),
    # #75: 重ねたスラッシュ・%2F が許可リストの経路に正規化されて通らない (許可リストは送られた経路との全体一致。share Pod の caddy と同じ、#54)
    *[("GET", variant) for path in ALLOWED for variant in ("/" + path, path + "/", "/%2F" + path[1:])],
    ("GET", "//health"), ("GET", "/%2Fhealth"), ("GET", "/transformations%2Ffeed"), ("GET", "//transformations/feed"), ("GET", "/transformations//feed"),
    ("GET", "/%2e/%2Fhealth"), ("GET", "/health%2F"), ("GET", "/%2Fdashboard"),
    # #75: ドットセグメントも許可リストの経路に正規化して通さない
    ("GET", "/x/../health"), ("GET", "/./health"), ("GET", "/%2e/health"), ("GET", "/stats/../stats-history?days=7"),
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
        for path in ALLOWED:
            self.assertEqual(request(self.port, "GET", path, self.auth()), 200, path)
        self.assertEqual(len(self.upstream.seen), len(ALLOWED))
        for method, path, headers in self.upstream.seen:
            self.assertIn(path, ALLOWED)
            self.assertNotIn("X-Share-Relay-Token", headers, "トークンを upstream に渡した")
            self.assertEqual(headers["Host"], f"127.0.0.1:{self.upstream.server_port}")

    def test_head_is_not_allowed(self):
        # #48: 実機の headroom は許可リストの経路の HEAD に 404 を返し、Anthropic へ素通しするとみられる。HEAD は中継で止める
        for path in ALLOWED:
            self.assertEqual(request(self.port, "HEAD", path, self.auth()), 404, path)
        self.assertEqual(self.upstream.seen, [], "HEAD が upstream に届いた")

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
        self.stdin_log = self.tmp / "stdin.log"
        self.write_stub("kubectl", f"""
FAKE_KUBECTL_LOG="{self.log}" FAKE_KUBECTL_STDIN="{self.stdin_log}" exec python3 "{FAKE_KUBECTL}" "$@"
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

    def manifests(self):
        """kubectl の標準入力に渡った Secret の manifest (動詞, manifest)。"""
        entries = [json.loads(line) for line in self.stdin_log.read_text().splitlines()] if self.stdin_log.exists() else []
        return [(e["verb"], e["manifest"]) for e in entries if e["manifest"].get("kind") == "Secret"]

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

    def test_secret_is_put_without_last_applied_annotation(self):
        # #49: apply は data (トークン) を kubectl.kubernetes.io/last-applied-configuration の注釈に残す。replace・create で入れる
        self.assertEqual(self.up().returncode, 0)
        token = self.token_file.read_text()
        self.assertFalse([c for c in self.calls() if " apply " in c and "secret" in c], "Secret を apply した")
        ((verb, manifest),) = self.manifests()
        self.assertEqual(verb, "create", "無いときは create")
        self.assertNotIn("annotations", manifest["metadata"])
        self.assertEqual(manifest["metadata"]["name"], "share-host")
        self.assertEqual(sorted(manifest["data"]), ["SHARE_RELAY_ADDR", "SHARE_RELAY_TOKEN"])
        self.assertNotIn(token, "\n".join(self.calls()), "トークンが kubectl の引数に出た")

    def test_existing_secret_is_replaced_not_applied(self):
        # 以前の apply が注釈を残していても、replace は metadata を置き換えるので消える。replace --force (消して作り直す) は使わない
        self.assertEqual(self.up(FAKE_EXISTING="share-host").returncode, 0)
        ((verb, manifest),) = self.manifests()
        self.assertEqual(verb, "replace")
        self.assertNotIn("annotations", manifest["metadata"])
        self.assertFalse([c for c in self.calls() if "--force" in c])

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
