"""orca_exporter.py の、Orca に触らずに確かめられる部分のテスト。標準ライブラリだけ:
    python3 -B -m unittest discover -s tools/orca-exporter
"""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない
sys.path.insert(0, str(Path(__file__).resolve().parent))
import orca_exporter as ex

MAIN = "term_main"
TOPIC = "term_topic"
UUID = "69fd0cf9-b963-44ad-92ca-e2335c654ea7"


def term(handle, path):
    return {"handle": handle, "worktreeId": f"{UUID}::{path}"}


TERMINALS = [
    term(MAIN, "/home/yyamakura/coordinator"),
    term(TOPIC, "/home/yyamakura/coordinator-chat-token-usage"),
    # worktree に属さない端末
    {"handle": "term_none"},
]


class CoordinatorWorktrees(unittest.TestCase):
    def test_resolves_handle_to_worktree_basename(self):
        runs = [{"id": "run_a", "coordinator_handle": MAIN}, {"id": "run_b", "coordinator_handle": TOPIC}]
        got = ex.coordinator_worktrees(runs, TERMINALS, {})
        self.assertEqual(got, {"run_a": "coordinator", "run_b": "coordinator-chat-token-usage"})

    def test_keeps_last_seen_when_handle_is_gone(self):
        # Run を閉じると coordinator_handle が空になる。残した値は消さない
        known = {"run_a": "coordinator-chat-token-usage"}
        got = ex.coordinator_worktrees([{"id": "run_a", "coordinator_handle": None}], TERMINALS, known)
        self.assertEqual(got, {"run_a": "coordinator-chat-token-usage"})
        # 端末が消えて handle を引けないときも同じ
        got = ex.coordinator_worktrees([{"id": "run_a", "coordinator_handle": "term_gone"}], [], known)
        self.assertEqual(got, {"run_a": "coordinator-chat-token-usage"})

    def test_run_use_handover_takes_the_latest_not_the_first(self):
        # main chat が作った Run を run-use で話題チャットに渡すと handle が変わる。最後の値にする
        known = ex.coordinator_worktrees([{"id": "run_a", "coordinator_handle": MAIN}], TERMINALS, {})
        self.assertEqual(known["run_a"], "coordinator")
        known = ex.coordinator_worktrees([{"id": "run_a", "coordinator_handle": TOPIC}], TERMINALS, known)
        self.assertEqual(known["run_a"], "coordinator-chat-token-usage")

    def test_unresolvable_run_has_no_entry(self):
        runs = [{"id": "run_b", "coordinator_handle": "term_none"}, {"id": "run_c"}]
        self.assertEqual(ex.coordinator_worktrees(runs, TERMINALS, {}), {})


class RunInfoMetric(unittest.TestCase):
    def run_info_attrs(self, coordinators):
        runs = [{"id": "run_a", "objective": "x"}]
        run_info = {"run_a": {"start": 1.0, "last": 2.0, "closed": False, "tasks": []}}
        ms = ex.metrics(runs, {}, [], run_info, 3.0, coordinators)
        info = next(m for m in ms if m["name"] == "orca.run.info")
        return {a["key"]: a["value"]["stringValue"] for a in info["gauge"]["dataPoints"][0]["attributes"]}

    def test_label_is_on_orca_run_info(self):
        a = self.run_info_attrs({"run_a": "coordinator-chat-token-usage"})
        self.assertEqual(a["orca.run.coordinator_worktree"], "coordinator-chat-token-usage")
        self.assertEqual(a["orca.run.id"], "run_a")

    def test_label_is_absent_when_unknown(self):
        self.assertNotIn("orca.run.coordinator_worktree", self.run_info_attrs({}))


if __name__ == "__main__":
    unittest.main()
