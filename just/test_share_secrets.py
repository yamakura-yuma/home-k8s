"""share Pod の Secret を作るスクリプト (share-secrets.sh) と、Secret を作る処理すべて (#49)、URL を引く関数 (share-urls.sh) の試験。

標準ライブラリだけ: python3 -B -m unittest discover -s just -p test_share_secrets.py
PATH の先頭に置いた偽の kubectl (fake_kubectl.py)・docker・curl・openssl で、引数と標準入力だけを見る。
稼働中のクラスタにも、ホストの docker にも触れない。
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない

JUST = Path(__file__).resolve().parent
FAKE_KUBECTL = JUST / "fake_kubectl.py"
VIEWER_PASSWORD = "viewer-pw-0123456789"
FAKE_RANDOM = "ab" * 32  # 偽の openssl rand -hex 32 の出力
SECRET_SCRIPTS = ["grafana-secrets.sh", "grafana-viewer-rotate.sh", "share-relay.sh", "share-secrets.sh"]


def decode(manifest):
    return {key: base64.b64decode(value).decode() for key, value in manifest["data"].items()}


class FakeEnv(unittest.TestCase):
    """偽の kubectl・docker・curl・openssl を PATH の先頭に置く。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        self.stdin_log = self.tmp / "stdin.log"
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        self.write_stub("kubectl", f'exec python3 "{FAKE_KUBECTL}" "$@"')
        self.write_stub("curl", "echo 200")
        self.write_stub("openssl", f"echo {FAKE_RANDOM}")
        self.viewer_file = self.tmp / "viewer-password"
        self.viewer_file.write_text(VIEWER_PASSWORD)

    def write_stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\n" + body + "\n")
        path.chmod(0o755)

    def run_script(self, script, *args, **env):
        return subprocess.run(
            ["bash", str(JUST / script), *args], capture_output=True, text=True, cwd=self.tmp,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_KUBECTL_LOG": str(self.log),
                 "FAKE_KUBECTL_STDIN": str(self.stdin_log), "FAKE_LOGS": str(self.logs), "FAKE_EXISTING": "",
                 "HOME": str(self.tmp), **env})

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def entries(self):
        return [json.loads(line) for line in self.stdin_log.read_text().splitlines()] if self.stdin_log.exists() else []

    def secrets(self):
        """標準入力で入った Secret を {名前: (動詞, 平文の data)} に。"""
        return {e["manifest"]["metadata"]["name"]: (e["verb"], decode(e["manifest"]))
                for e in self.entries() if e["manifest"].get("kind") == "Secret"}


class ShareSecrets(FakeEnv):
    def run_share_secrets(self, **env):
        return self.run_script("share-secrets.sh", str(self.viewer_file), "kind-test", **env)

    def test_creates_the_three_secrets(self):
        r = self.run_share_secrets()
        self.assertEqual(r.returncode, 0, r.stderr)
        created = {e["manifest"]["metadata"]["name"]: e["manifest"] for e in self.entries()
                   if e["verb"] in ("create", "create-secret", "replace")}
        self.assertEqual(sorted(created), ["share-credentials", "share-grafana", "share-session-key"])
        self.assertEqual(created["share-credentials"].get("data", {}), {}, "資格情報は空で作る (拒否が既定)")
        self.assertEqual(decode(created["share-grafana"]), {"viewer-password": VIEWER_PASSWORD})
        self.assertEqual(list(decode(created["share-session-key"])), ["key"])
        self.assertEqual(decode(created["share-session-key"])["key"], FAKE_RANDOM)
        for call in self.calls():
            self.assertIn("--context kind-test", call)

    def test_never_uses_apply_for_secrets(self):
        # apply は data を last-applied-configuration の注釈に残す (#49)
        self.assertEqual(self.run_share_secrets().returncode, 0)
        self.assertEqual([c for c in self.calls() if " apply " in c and "secret" in c.lower()], [])
        for e in self.entries():
            if e["manifest"].get("kind") == "Secret":
                self.assertNotEqual(e["verb"], "apply")
                self.assertNotIn("annotations", e["manifest"]["metadata"])

    def test_existing_credentials_and_key_are_never_replaced(self):
        # just up の打ち直しで、人の資格情報と署名鍵 (cookie) を消さない。viewer の写しは、値が変わりうるので毎回置き換える
        r = self.run_share_secrets(FAKE_EXISTING="share-credentials share-session-key share-grafana")
        self.assertEqual(r.returncode, 0, r.stderr)
        secrets = self.secrets()
        self.assertEqual(list(secrets), ["share-grafana"])
        self.assertEqual(secrets["share-grafana"][0], "replace")
        self.assertEqual([c for c in self.calls() if "share-credentials" in c and "create secret" in c], [])
        self.assertEqual([c for c in self.calls() if "--force" in c], [], "replace --force は動いている Pod の下で消す")

    def test_nothing_secret_reaches_the_command_line(self):
        self.assertEqual(self.run_share_secrets().returncode, 0)
        joined = "\n".join(self.calls())
        self.assertNotIn(VIEWER_PASSWORD, joined)
        self.assertNotIn(FAKE_RANDOM, joined, "署名鍵が kubectl の引数に出た")

    def test_refuses_without_the_viewer_password(self):
        self.viewer_file.write_text("")
        r = self.run_share_secrets()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.entries(), [], "viewer のパスワードが無いのに Secret を作った")


class EverySecretCreatingScript(FakeEnv):
    """#49: Secret を作る処理すべてで、注釈に値が残らない。"""

    @staticmethod
    def statements(text):
        """行の継続 (末尾の \\) とパイプの続き (次の行の先頭の |) をつないだ、シェルの 1 文ずつ。"""
        return re.sub(r"\\\n|\n\s*(?=\|)", " ", text).splitlines()

    def test_scripts_do_not_pipe_secrets_into_apply(self):
        # 静的な見張り: Secret の manifest を kubectl apply に渡す文が無い。新しい Secret を足すときの取りこぼしを止める (#49)
        for name in SECRET_SCRIPTS:
            statements = self.statements((JUST / name).read_text())
            secret_statements = [s for s in statements if "create secret" in s]
            self.assertTrue(secret_statements, f"{name} に Secret を作る文が無い (試験が古い)")
            for statement in secret_statements:
                self.assertNotRegex(statement, r"\bapply\b", f"{name}: {statement}")

    def test_grafana_secrets_puts_all_four_without_annotation(self):
        files = {n: self.tmp / n for n in ("admin", "viewer", "backstage")}
        for name, path in files.items():
            path.write_text(f"{name.upper()}PW-secret")
        r = self.run_script("grafana-secrets.sh", *map(str, files.values()), "observability", str(JUST), "kind-test")
        self.assertEqual(r.returncode, 0, r.stderr)
        secrets = self.secrets()
        self.assertEqual(sorted(secrets), ["backstage-grafana", "grafana-admin", "grafana-backstage", "grafana-viewer"])
        self.assertEqual(secrets["grafana-admin"][1], {"admin-user": "admin", "admin-password": "ADMINPW-secret"})
        self.assertEqual(secrets["backstage-grafana"][1]["GRAFANA_BASIC_AUTH"], base64.b64encode(b"backstage:BACKSTAGEPW-secret").decode())
        for verb, _ in secrets.values():
            self.assertIn(verb, ("create", "replace"))
        for e in self.entries():
            if e["manifest"].get("kind") == "Secret":
                self.assertNotIn("annotations", e["manifest"]["metadata"])
        self.assertEqual([c for c in self.calls() if "PW-secret" in c], [], "パスワードが kubectl の引数に出た")

    def test_viewer_rotate_puts_the_secret_without_annotation(self):
        admin = self.tmp / "admin"
        admin.write_text("admin-pw")
        self.write_stub("curl", 'case "$*" in *users/lookup*) echo \'{"id":7}\' ;; esac')
        r = self.run_script("grafana-viewer-rotate.sh", str(admin), str(self.viewer_file), "observability")
        self.assertEqual(r.returncode, 0, r.stderr)
        ((name, (verb, data)),) = self.secrets().items()
        self.assertEqual((name, verb), ("grafana-viewer", "create"))
        self.assertEqual(data["password"], self.viewer_file.read_text())
        self.assertNotIn(self.viewer_file.read_text(), "\n".join(self.calls()))


class ShareUrls(FakeEnv):
    def urls(self, **logs):
        for name, text in logs.items():
            (self.logs / f"cloudflared-{name}").write_text(text)
        r = subprocess.run(["bash", str(JUST / "share-urls.sh"), "kind-test"], capture_output=True, text=True,
                           env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_KUBECTL_LOG": str(self.log),
                                "FAKE_KUBECTL_STDIN": str(self.stdin_log), "FAKE_LOGS": str(self.logs)})
        return r

    BOX = ("2026-10-03T00:00:00Z INF Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):\n"
           "2026-10-03T00:00:00Z INF |  https://{host}.trycloudflare.com  |\n"
           "2026-10-03T00:00:01Z INF Registered tunnel connection connIndex=0\n")

    def test_prints_one_url_per_target_in_order(self):
        r = self.urls(grafana=self.BOX.format(host="aaa-bbb-ccc"), headroom=self.BOX.format(host="ddd-eee-fff"),
                      backstage=self.BOX.format(host="ggg-hhh-iii"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), [
            "grafana https://aaa-bbb-ccc.trycloudflare.com",
            "headroom https://ddd-eee-fff.trycloudflare.com",
            "backstage https://ggg-hhh-iii.trycloudflare.com"])
        for call in self.calls():
            self.assertIn("logs deploy/share -c cloudflared-", call)
            self.assertIn("-n share", call)
            self.assertIn("--context kind-test", call)

    def test_api_host_in_an_error_is_not_a_url(self):
        failed = 'ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": dial tcp: lookup failed\n'
        r = self.urls(grafana=failed, headroom=failed + self.BOX.format(host="ddd-eee-fff"), backstage=failed)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.splitlines(), ["grafana -", "headroom https://ddd-eee-fff.trycloudflare.com", "backstage -"])

    def test_missing_logs_give_a_dash_and_a_failure(self):
        r = self.urls()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.splitlines(), ["grafana -", "headroom -", "backstage -"])

    def test_first_url_wins(self):
        r = self.urls(grafana=self.BOX.format(host="old-one-aaa") + self.BOX.format(host="new-one-bbb"))
        self.assertEqual(r.stdout.splitlines()[0], "grafana https://old-one-aaa.trycloudflare.com")

    def test_usage(self):
        r = subprocess.run(["bash", str(JUST / "share-urls.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)


if __name__ == "__main__":
    unittest.main()
