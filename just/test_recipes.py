"""just の公開レシピが 6 個だけで、引数が正しく展開されるかのテスト。

標準ライブラリだけ: python3 -B -m unittest discover -s just -p test_recipes.py
レシピは実行せず `just --dry-run` / `just --list` の出力だけを見る (クラスタにもコンテナにも触れない)。
`just` が無い環境では飛ばす。
"""
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ["devcontainer", "down", "orca-exporter", "share", "show", "up"]


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
        steps = ["kind create cluster", "helm upgrade --install argocd", "grafana-secrets.sh",
                 "argocd/root.yaml", "argocd-wait.sh", "headlamp-token.sh"]
        pos = [out.find(step) for step in steps]
        self.assertNotIn(-1, pos, out)
        self.assertEqual(pos, sorted(pos))

    def test_orca_exporter_passes_action(self):
        for action in ["install", "uninstall", "status"]:
            self.assertRegex(just("--dry-run", "orca-exporter", action).stderr, rf"orca-exporter\.sh\" {action}\n")

    def test_bad_arguments_stop_with_usage(self):
        for args in [("show", "nope"), ("orca-exporter", "nope"), ("orca-exporter",), ("devcontainer", "nope")]:
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
