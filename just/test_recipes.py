"""just の公開レシピが 7 個だけで、引数が正しく展開されるかのテスト。

標準ライブラリだけ: python3 -B -m unittest discover -s just -p test_recipes.py
レシピは実行せず `just --dry-run` / `just --list` の出力だけを見る (クラスタにもコンテナにも触れない)。
`just` が無い環境では飛ばす。
"""
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ["ci", "devcontainer", "down", "orca-exporter", "share", "show", "up"]


def just(*args):
    return subprocess.run(["just", *args], cwd=ROOT, capture_output=True, text=True)


@unittest.skipUnless(shutil.which("just"), "just が無い")
class Recipes(unittest.TestCase):
    def test_public_recipes(self):
        names = [line.split()[0] for line in just("--list").stdout.splitlines()[1:]]
        self.assertEqual(names, PUBLIC)

    def test_show_expands_per_target(self):
        expect = {
            "grafana": "observe-show-connection.sh",
            "grafana-admin": "grafana-admin-password",
            "argocd": "argocd-initial-admin-secret",
            "headlamp": "headlamp/token",
            "backstage": "localhost:7007",
        }
        for target, marker in expect.items():
            out = just("--dry-run", "show", target).stderr
            self.assertIn(marker, out, target)
            for other, other_marker in expect.items():
                if other != target:
                    self.assertNotIn(other_marker, out, f"{target} に {other} が混ざった")
        everything = just("--dry-run", "show").stderr
        for marker in expect.values():
            self.assertIn(marker, everything)

    def test_up_runs_in_order(self):
        out = just("--dry-run", "up").stderr
        steps = ["kind create cluster", "observability/{tempo,prometheus,loki,grafana}",
                 "docker build -t home-k8s-backstage:", "kind load docker-image home-k8s-backstage:",
                 "helm upgrade --install argocd", "grafana-secrets.sh",
                 "share-relay.sh up", "share-secrets.sh", "argocd/root.yaml", "argocd-wait.sh", "headlamp-token.sh"]
        pos = [out.find(step) for step in steps]
        self.assertNotIn(-1, pos, out)
        self.assertEqual(pos, sorted(pos))

    def test_share_relay_follows_the_cluster(self):
        # 中継は kind の bridge ができた後 (up)・クラスタを消す前 (down) に動く。ポートは Secret share-host の宛先と同じ値を渡す
        up = just("--dry-run", "up").stderr
        self.assertRegex(up, r'share-relay\.sh up ".*/share/relay-token" ".+" kind-study-kind 8788\n')
        down = just("--dry-run", "down").stderr
        self.assertLess(down.find("share-relay.sh down"), down.find("kind delete cluster"))
        self.assertNotIn("share-relay", " ".join(just("--list").stdout.split()), "内部用は公開しない")

    def test_share_secrets_exist_before_argocd_syncs_the_pod(self):
        # share Pod は Secret が無いと起動できない (optional: false)。root.yaml (ArgoCD の同期) より前に作り、viewer のパスワードのファイルが要るので _grafana-secrets の後
        up = just("--dry-run", "up").stderr
        self.assertRegex(up, r'share-secrets\.sh ".*/grafana-viewer-password" kind-study-kind\n')
        self.assertLess(up.find("grafana-secrets.sh"), up.find("share-secrets.sh"))
        self.assertLess(up.find("share-secrets.sh"), up.find("argocd/root.yaml"))
        self.assertNotIn("share-secrets", " ".join(just("--list").stdout.split()), "内部用は公開しない")

    def test_backstage_image_tag_matches_values(self):
        # kind load するイメージのタグと、chart が使うタグ (pull しない) がずれると Pod が起動しない
        image = re.search(r"docker build -t (\S+) backstage", just("--dry-run", "up").stderr).group(1)
        values = (ROOT / "clusters/kind/backstage/values.yaml").read_text()
        repo = re.search(r"repository: (\S+)", values).group(1)
        tag = re.search(r'tag: "([^"]+)"', values).group(1)
        self.assertEqual(image, f"{repo}:{tag}")
        self.assertIn("pullPolicy: Never", values)

    def test_orca_exporter_passes_action(self):
        for action in ["install", "uninstall", "status"]:
            self.assertRegex(just("--dry-run", "orca-exporter", action).stderr, rf"orca-exporter\.sh\" {action}\n")

    def test_share_passes_target(self):
        for target in ["grafana", "headroom", "backstage"]:
            self.assertRegex(just("--dry-run", "share", target).stderr, rf"observe-share\.sh\b.* {target}\n")

    def test_bad_arguments_stop_with_usage(self):
        for args in [("show", "nope"), ("share", "nope"), ("orca-exporter", "nope"), ("orca-exporter",), ("devcontainer", "nope")]:
            r = just("--dry-run", *args)
            self.assertNotEqual(r.returncode, 0, args)
            if len(args) > 1:
                self.assertIn("usage: just " + args[0], r.stderr, args)

    def test_arguments_are_required_where_needed(self):
        for args in [("orca-exporter",), ("devcontainer",)]:
            self.assertNotEqual(just("--dry-run", *args).returncode, 0, args)

    def test_devcontainer_accepts_each_action(self):
        for action in ["up", "shell", "down"]:
            self.assertEqual(just("--dry-run", "devcontainer", action).returncode, 0, action)


if __name__ == "__main__":
    unittest.main()
