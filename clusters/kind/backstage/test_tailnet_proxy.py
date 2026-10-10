"""Backstage の tailnet の入口の proxy (clusters/kind/backstage/tailnet-proxy/Caddyfile) の試験 (just ci)。

標準ライブラリだけ: python3 -B -m unittest discover -s clusters/kind/backstage -v

caddy を 127.0.0.1 の空きポートで起動し、upstream (Backstage) は偽の HTTP サーバー。クラスタにもネットワークにも出ない。
確かめること: ゲストのサインインの経路 (/api/auth/guest の下) は表記を変えても 403 で upstream に届かない、
ほかの経路 (OIDC のサインイン・カタログ・画面) はそのまま届く。
Backstage (express) は経路の大文字小文字を区別せず、provider の名前の %エンコードを解くので、表記揺れも止める必要がある。

`caddy` が無い環境では飛ばす (`just ci` は devShells.ci に caddy を入れて必ず走らせる)。
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "share"))
from test_share_caddy import caddy_env, free_port, request, start_upstream, wait_listening  # noqa: E402

CADDYFILE = (HERE / "tailnet-proxy" / "Caddyfile").read_text(encoding="utf-8")

GUEST = [
    "/api/auth/guest/refresh",
    "/api/auth/guest",
    "/api/auth/guest?x=1",
    "/api/auth/guest/refresh?optional=1",
    "/api/auth/Guest/refresh",
    "/API/AUTH/GUEST/refresh",
    "/api/auth/%67uest/refresh",
    "/api/auth/gu%65st/refresh",
    "/api/auth/%47UEST/refresh",
    "//api/auth/guest/refresh",
    "/api//auth/guest/refresh",
    "/api/auth//guest/refresh",
    "/api/auth/./guest/refresh",
    "/api/auth/x/../guest/refresh",
]
PASSED = [
    "/",
    "/catalog/default/component/sample-api",
    "/api/auth/oidc/start?env=production",
    "/api/auth/oidc/handler/frame?code=x&state=y",
    "/api/auth/oidc/refresh",
    "/api/catalog/entities",
    "/api/techdocs/static/docs/default/component/home-k8s/index.html",
]


@unittest.skipUnless(shutil.which("caddy"), "caddy が無い")
class TailnetProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.upstream = start_upstream()
        cls.port = free_port()
        # 待ち受けと upstream だけを手元に置き換える (経路の書き方はそのまま)
        assert CADDYFILE.count(":8080 {") == 1 and CADDYFILE.count("reverse_proxy backstage:7007") == 1
        config = cls.tmp / "Caddyfile"
        config.write_text(
            CADDYFILE.replace(":8080 {", f":{cls.port} {{").replace(
                "reverse_proxy backstage:7007", f"reverse_proxy 127.0.0.1:{cls.upstream.server_port}"
            ),
            encoding="utf-8",
        )
        cls.caddy = subprocess.Popen(
            ["caddy", "run", "--config", str(config), "--adapter", "caddyfile"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=caddy_env(cls.tmp),
        )
        wait_listening(cls.caddy, cls.port, "caddy")

    @classmethod
    def tearDownClass(cls):
        cls.caddy.terminate()
        cls.caddy.wait(timeout=10)
        cls.caddy.stderr.close()
        cls.upstream.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.upstream.seen.clear()

    def test_guest_sign_in_is_refused_in_any_spelling(self):
        for method in ("GET", "POST"):
            for path in GUEST:
                with self.subTest(method=method, path=path):
                    status, _ = request(self.port, method, path)
                    self.assertEqual(status, 403)
        self.assertEqual(self.upstream.seen, [], "ゲストの経路が Backstage に届いた")

    def test_other_routes_reach_backstage_unchanged(self):
        for path in PASSED:
            with self.subTest(path=path):
                status, _ = request(self.port, "GET", path)
                self.assertEqual(status, 200)
        self.assertEqual([p for _, p, _ in self.upstream.seen], PASSED)


class Manifest(unittest.TestCase):
    def test_service_is_cluster_ip_only(self):
        # tailnet からだけ通す入口。NodePort にすると localhost にもう 1 つ口ができる
        text = (HERE / "tailnet-proxy" / "proxy.yaml").read_text(encoding="utf-8")
        self.assertIn("  type: ClusterIP\n", text)
        self.assertNotIn("NodePort\n", text.split("kind: Service", 1)[1])


if __name__ == "__main__":
    unittest.main()
