"""Tailscale の operator の OAuth client の Secret を作るスクリプト (tailscale-secrets.sh) の試験。

標準ライブラリだけ: python3 -B -m unittest discover -s just -p test_tailscale_secrets.py
PATH の先頭に置いた偽の kubectl (fake_kubectl.py) で、引数と標準入力だけを見る。稼働中のクラスタには触れない。
"""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_share_secrets import FakeEnv  # noqa: E402

FULL = {"client_id": "kXyZ123CNTRL", "client_secret": "tskey-client-kXyZ123CNTRL-s3cr3t=x"}
SECRET = dict(FULL)


class TailscaleSecrets(FakeEnv):
    def setUp(self):
        super().setUp()
        self.env_file = self.tmp / "tailscale-operator.env"

    def run_tailscale(self, **env):
        return self.run_script("tailscale-secrets.sh", str(self.env_file), "kind-test", **env)

    def write_env(self, values, extra=""):
        self.env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()) + extra)

    def test_no_file_fails_before_touching_the_cluster(self):
        # operator は Secret が無いと起動できず、just up の最後で 20 分待って落ちる。その前に止める
        r = self.run_tailscale()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("docs/cluster/tailscale.md", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_empty_file_is_the_same_as_no_file(self):
        self.env_file.write_text("")
        r = self.run_tailscale()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.secrets(), {})

    def test_creates_operator_oauth_with_the_chart_keys(self):
        self.write_env(FULL)
        r = self.run_tailscale()
        self.assertEqual(r.returncode, 0, r.stderr)
        verb, data = self.secrets()["operator-oauth"]
        self.assertEqual(verb, "create")
        self.assertEqual(data, SECRET)
        self.assertNotIn("last-applied", self.stdin_log.read_text())

    def test_values_are_not_on_the_command_line(self):
        self.write_env(FULL)
        r = self.run_tailscale()
        self.assertEqual(r.returncode, 0, r.stderr)
        for call in self.calls():
            self.assertNotIn(FULL["client_secret"], call)
            self.assertNotIn(FULL["client_id"], call)

    def test_replaces_an_existing_secret(self):
        self.write_env(FULL)
        r = self.run_tailscale(FAKE_EXISTING="operator-oauth")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.secrets()["operator-oauth"][0], "replace")

    def test_comments_blank_lines_crlf_and_unknown_keys(self):
        marker = self.tmp / "executed"
        # tailnet= の行 (人が控えた tailnet 名) は Secret に入れない
        lines = ["# Trust credentials", "", "tailnet=tail1234.ts.net", "OTHER=ignored", f"TS_EVIL=$(touch {marker})"]
        lines += [f"{k}={v}" for k, v in FULL.items()]
        self.env_file.write_bytes(("\r\n".join(lines) + "\r\n").encode())
        r = self.run_tailscale()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.secrets()["operator-oauth"][1], SECRET)
        self.assertFalse(marker.exists())

    def test_missing_key_fails_naming_it_and_makes_no_secret(self):
        self.write_env({"client_id": FULL["client_id"]})
        r = self.run_tailscale()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("client_secret", r.stderr)
        self.assertEqual(self.secrets(), {})


if __name__ == "__main__":
    unittest.main()
