"""gitops-deployer の資格情報の Secret を作るスクリプト (gitops-deployer-secrets.sh) の試験。

標準ライブラリだけ: python3 -B -m unittest discover -s just -p test_gitops_deployer_secrets.py
PATH の先頭に置いた偽の kubectl (fake_kubectl.py) で、引数と標準入力だけを見る。稼働中のクラスタには触れない。
"""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_secrets import FakeEnv  # noqa: E402

TOKEN = "github_pat_FAKE-for-test"
CLIENT_SECRET = "FAKE-client-secret"


class GitopsDeployerSecrets(FakeEnv):
    def setUp(self):
        super().setUp()
        self.github = self.tmp / "gitops-deployer-github.env"
        self.clients = self.tmp / "clients"
        self.clients.mkdir()
        (self.clients / "gitops-deployer").write_text(CLIENT_SECRET)

    def run_secrets(self, **env):
        return self.run_script("gitops-deployer-secrets.sh", str(self.github), str(self.clients), "kind-test", **env)

    def test_makes_the_token_and_the_e2e_client_secrets(self):
        self.github.write_text(f"# PAT\r\nGITHUB_TOKEN={TOKEN}\r\n")
        r = self.run_secrets()
        self.assertEqual(r.returncode, 0, r.stderr)
        secrets = self.secrets()
        self.assertEqual(secrets["github-token"], ("create", {"token": TOKEN}))
        self.assertEqual(secrets["gitops-deployer-e2e"], ("create", {"client_id": "gitops-deployer", "client_secret": CLIENT_SECRET}))
        namespaces = {e["manifest"]["metadata"]["name"]: e["manifest"]["metadata"]["namespace"]
                      for e in self.entries() if e["manifest"].get("kind") == "Secret"}
        self.assertEqual(namespaces, {"github-token": "gitops-deployer", "gitops-deployer-e2e": "testkube"})
        self.assertNotIn("last-applied", self.stdin_log.read_text())

    def test_values_are_not_on_the_command_line(self):
        self.github.write_text(f"GITHUB_TOKEN={TOKEN}\n")
        self.assertEqual(self.run_secrets().returncode, 0)
        for call in self.calls():
            self.assertNotIn(TOKEN, call)
            self.assertNotIn(CLIENT_SECRET, call)

    def test_missing_token_file_only_warns(self):
        # トークンは人が作る。無くても just up は止めない (git-activity が起動しないだけ)
        r = self.run_secrets()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("docs/cluster/gitops-deployer.md", r.stderr)
        self.assertEqual(sorted(self.secrets()), ["gitops-deployer-e2e"])

    def test_the_file_is_read_not_executed(self):
        marker = self.tmp / "executed"
        self.github.write_text(f"GITHUB_TOKEN=$(touch {marker})x\n")
        self.assertEqual(self.run_secrets().returncode, 0)
        self.assertFalse(marker.exists())

    def test_missing_client_file_stops_before_touching_the_cluster(self):
        (self.clients / "gitops-deployer").unlink()
        r = self.run_secrets()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("_keycloak-secrets", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_replaces_existing_secrets(self):
        self.github.write_text(f"GITHUB_TOKEN={TOKEN}\n")
        r = self.run_secrets(FAKE_EXISTING="github-token gitops-deployer-e2e")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual({k: v[0] for k, v in self.secrets().items()}, {"github-token": "replace", "gitops-deployer-e2e": "replace"})


if __name__ == "__main__":
    unittest.main()
