"""Azure のタブの資格情報の Secret を作るスクリプト (backstage-azure-secret.sh) の試験。

標準ライブラリだけ: python3 -B -m unittest discover -s just -p test_backstage_azure_secret.py
PATH の先頭に置いた偽の kubectl (fake_kubectl.py) で、引数と標準入力だけを見る。稼働中のクラスタには触れない。
"""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_share_secrets import FakeEnv  # noqa: E402

FULL = {
    "AZURE_DOMAIN": "example.onmicrosoft.com",
    "AZURE_TENANT_ID": "00000000-0000-0000-0000-000000000000",
    "AZURE_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
    "AZURE_CLIENT_SECRET": "s3cr3t=with=equals",
    "AZURE_SUBSCRIPTION_ID": "22222222-2222-2222-2222-222222222222",
}


class BackstageAzureSecret(FakeEnv):
    def setUp(self):
        super().setUp()
        self.env_file = self.tmp / "azure.env"

    def run_azure(self, **env):
        return self.run_script("backstage-azure-secret.sh", str(self.env_file), "kind-test", **env)

    def write_env(self, values, extra=""):
        self.env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()) + extra)

    def test_no_file_does_not_fail_and_makes_no_secret(self):
        r = self.run_azure()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.secrets(), {})
        self.assertIn("資格情報が無い", r.stdout)
        # 前に作った Secret が残らないよう、あれば消す
        self.assertTrue(any(c.startswith("kubectl --context kind-test -n backstage delete secret backstage-azure --ignore-not-found") for c in self.calls()), self.calls())

    def test_empty_file_is_the_same_as_no_file(self):
        self.env_file.write_text("")
        r = self.run_azure()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.secrets(), {})

    def test_creates_the_secret_from_the_file_without_annotation(self):
        self.write_env(FULL)
        r = self.run_azure()
        self.assertEqual(r.returncode, 0, r.stderr)
        verb, data = self.secrets()["backstage-azure"]
        self.assertEqual(verb, "create")
        self.assertEqual(data, FULL)
        self.assertNotIn("last-applied", self.stdin_log.read_text())

    def test_replaces_an_existing_secret(self):
        self.write_env(FULL)
        r = self.run_azure(FAKE_EXISTING="backstage-azure")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.secrets()["backstage-azure"][0], "replace")

    def test_comments_blank_lines_crlf_and_unknown_keys(self):
        # 注釈・空行・CRLF は読み飛ばし、要る項目以外は Secret に入れない。行は source せず読むだけ (コマンドを実行しない)
        marker = self.tmp / "executed"
        lines = ["# サービスプリンシパル", "", "OTHER=ignored", f"AZURE_EVIL=$(touch {marker})"]
        lines += [f"{k}={v}" for k, v in FULL.items()]
        self.env_file.write_bytes(("\r\n".join(lines) + "\r\n").encode())
        r = self.run_azure()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.secrets()["backstage-azure"][1], FULL)
        self.assertFalse(marker.exists())

    def test_missing_key_fails_naming_it_and_makes_no_secret(self):
        self.write_env({k: v for k, v in FULL.items() if k != "AZURE_CLIENT_SECRET"})
        r = self.run_azure()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("AZURE_CLIENT_SECRET", r.stderr)
        self.assertEqual(self.secrets(), {})

    def test_secret_values_never_reach_the_command_line(self):
        self.write_env(FULL)
        self.run_azure()
        self.assertNotIn("s3cr3t", "\n".join(self.calls()))


if __name__ == "__main__":
    unittest.main()
