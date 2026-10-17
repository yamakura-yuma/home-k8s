"""ゲストのアカウントのスクリプト (just/guest.sh、just guest add・list・delete) の試験。

標準ライブラリだけ: python3 -B -m unittest discover -s just -p test_guest.py
PATH の先頭に置いた偽の kubectl (fake_kubectl.py) が、Keycloak の Pod の中のスクリプトを手元の bash で動かし、
kcadm.sh を偽物 (fake_kcadm.py) に置き換える。稼働中のクラスタには触れない。
見ること: 一時パスワードが kubectl・kcadm の引数に残らず、画面に 1 回だけ出ること、名前の検査、期限 (発行した日の次の 3:00 JST) を
属性に書くこと (人ごとの期限 --hours は持たない)、guests 以外の人を消さないこと。期限を過ぎた人を消す側は
clusters/kind/auth/guest-expiry/test_guest_expiry.py。
"""
import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_secrets import FakeEnv  # noqa: E402

JUST = Path(__file__).resolve().parent
# 2026-10-12 10:00 JST (01:00 UTC)
NOON_ISH = 1791766800


class Guest(FakeEnv):
    def setUp(self):
        super().setUp()
        self.kcadm_log = self.tmp / "kcadm.log"
        self.kcadm_stdin = self.tmp / "kcadm-stdin.log"
        self.write_stub("kcadm", f'exec python3 "{JUST / "fake_kcadm.py"}" "$@"')

    def guest(self, *args, **env):
        return self.run_script("guest.sh", "kind-test", *args, FAKE_KCADM=str(self.bin / "kcadm"),
                               FAKE_KCADM_LOG=str(self.kcadm_log), FAKE_KCADM_STDIN=str(self.kcadm_stdin), **env)

    def kcadm(self):
        return [json.loads(line) for line in self.kcadm_log.read_text().splitlines()] if self.kcadm_log.exists() else []

    def kcadm_stdin_of(self, path):
        entries = [json.loads(line) for line in self.kcadm_stdin.read_text().splitlines()] if self.kcadm_stdin.exists() else []
        return [e["stdin"] for e in entries if path in e["args"]]

    def password_of(self, r):
        # 表示の形: 空行のあとに 2 字下げで 1 行
        m = re.search(r"\n\n  (\S+)\n\n", r.stdout)
        self.assertIsNotNone(m, r.stdout)
        return m.group(1)

    def test_add_makes_a_guest_with_a_temporary_password_shown_once(self):
        r = self.guest("add", "alice")
        self.assertEqual(r.returncode, 0, r.stderr)
        password = self.password_of(r)
        self.assertGreaterEqual(len(password), 20)
        self.assertEqual(r.stdout.count(password), 1)
        self.assertNotIn(password, r.stderr)
        # ゲストに渡す情報: ログインのユーザー名 (引数の名前ではなく guest-<名前>)、入口の URL、ログインの順
        self.assertIn("ユーザー名: guest-alice\n", r.stdout)
        self.assertIn("一時パスワード:\n\n  " + password + "\n", r.stdout)
        self.assertIn("https://grafana.taild2b611.ts.net", r.stdout)
        self.assertIn("https://backstage.taild2b611.ts.net", r.stdout)
        self.assertIn("tailnet の外からも開ける", r.stdout)
        steps = ["このユーザー名と一時パスワード", "新しいパスワード", "TOTP を登録"]
        self.assertEqual([r.stdout.index(s) for s in steps], sorted(r.stdout.index(s) for s in steps), r.stdout)
        create = next(a for a in self.kcadm() if a[:2] == ["create", "users"])
        self.assertIn("username=guest-alice", create)
        self.assertIn('groups=["/guests"]', create)
        # 最初のログインでパスワードを変えさせ、TOTP を設定させる
        self.assertIn('requiredActions=["UPDATE_PASSWORD","CONFIGURE_TOTP"]', create)
        [body] = self.kcadm_stdin_of("users/id-new/reset-password")
        self.assertEqual(json.loads(body), {"type": "password", "temporary": True, "value": password})

    def test_password_is_not_on_any_command_line(self):
        r = self.guest("add", "alice")
        self.assertEqual(r.returncode, 0, r.stderr)
        password = self.password_of(r)
        self.assertTrue(self.calls())
        for call in self.calls():
            self.assertNotIn(password, call)
        for args in self.kcadm():
            self.assertNotIn(password, " ".join(args))
        # 作業ディレクトリ (HOME) にもファイルを残さない
        for f in self.tmp.rglob("*"):
            if f.is_file() and f not in (self.kcadm_stdin,):
                self.assertNotIn(password, f.read_text(errors="replace"), f)

    def test_each_add_makes_a_new_password(self):
        first = self.password_of(self.guest("add", "alice"))
        second = self.password_of(self.guest("add", "bob"))
        self.assertNotEqual(first, second)

    def test_expiry_is_the_next_3am_jst_written_as_an_attribute(self):
        # 発行した日の次の 3:00 JST (= 18:00 UTC)。0:00〜3:00 JST の発行はその日の 3:00
        cases = [
            (NOON_ISH, "2026-10-12T18:00:00Z", "2026-10-13 03:00 JST"),             # 10:00 JST
            (NOON_ISH + 8 * 3600, "2026-10-12T18:00:00Z", "2026-10-13 03:00 JST"),  # 18:00 JST
            (NOON_ISH + 15 * 3600, "2026-10-12T18:00:00Z", "2026-10-13 03:00 JST"),  # 翌 01:00 JST
            (NOON_ISH + 17 * 3600, "2026-10-13T18:00:00Z", "2026-10-14 03:00 JST"),  # 翌 03:00 JST ちょうど
            (NOON_ISH + 17 * 3600 - 1, "2026-10-12T18:00:00Z", "2026-10-13 03:00 JST"),  # 翌 02:59:59 JST
        ]
        for now, want, shown in cases:
            self.kcadm_log.unlink(missing_ok=True)
            r = self.guest("add", "alice", GUEST_NOW_EPOCH=str(now))
            self.assertEqual(r.returncode, 0, (now, r.stderr))
            create = next(a for a in self.kcadm() if a[:2] == ["create", "users"])
            self.assertIn(f'attributes.guest-expires-at=["{want}"]', create, now)
            self.assertIn(f"{shown} に消える", r.stdout, now)

    def test_there_is_no_per_guest_expiry(self):
        for args in [("add", "alice", "--hours", "8"), ("add", "alice", "--days", "1"), ("add", "alice", "8")]:
            r = self.guest(*args)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("usage", r.stderr, args)
        self.assertEqual(self.calls(), [])

    def test_unmanaged_attributes_are_turned_on_only_when_off(self):
        # Keycloak 26 の既定では、user profile に無い属性は保存されない。管理者だけが読み書きできる ADMIN_EDIT にする
        self.assertEqual(self.guest("add", "alice").returncode, 0)
        self.assertIn(["update", "users/profile", "-r", "home-k8s", "-s", "unmanagedAttributePolicy=ADMIN_EDIT", "--config"],
                      [a[:7] for a in self.kcadm()])
        self.kcadm_log.unlink()
        self.assertEqual(self.guest("add", "bob", FAKE_KC_POLICY="ADMIN_EDIT").returncode, 0)
        self.assertFalse([a for a in self.kcadm() if a[:2] == ["update", "users/profile"]])

    def test_guest_is_removed_again_when_the_expiry_did_not_stick(self):
        r = self.guest("add", "alice", FAKE_KC_DROP_ATTRIBUTES="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("guest-expires-at", r.stderr)
        self.assertIn(["delete", "users/id-new"], [a[:2] for a in self.kcadm()])
        self.assertFalse([a for a in self.kcadm() if a[1:2] == ["users/id-new/reset-password"]])
        self.assertNotIn("一時パスワード", r.stdout)

    def test_bad_names_are_refused_before_touching_the_cluster(self):
        marker = self.tmp / "executed"
        bad = ["", "Alice", "a b", "-a", "a-", "a_b", "a.b", "a/b", "x" * 33, f"a;touch {marker}", f"$(touch {marker})", "日本"]
        for name in bad:
            for action in ("add", "delete"):
                r = self.guest(action, name)
                self.assertNotEqual(r.returncode, 0, (action, name))
        self.assertEqual(self.calls(), [])
        self.assertFalse(marker.exists())
        for name in ["a", "alice", "bob-2", "x" * 32]:
            self.assertEqual(self.guest("add", name).returncode, 0, name)

    def test_existing_user_is_not_overwritten(self):
        r = self.guest("add", "alice", FAKE_KC_USERS="guest-alice=id-1:guests")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("もういる", r.stderr)
        self.assertFalse([a for a in self.kcadm() if a[:2] == ["create", "users"]])
        self.assertNotIn("一時パスワード", r.stdout)

    def test_admin_password_is_read_inside_the_pod(self):
        self.guest("add", "alice")
        for call in self.calls():
            self.assertNotIn("FAKE-admin-password", call)

    def test_delete_logs_out_and_deletes_only_guests(self):
        users = "guest-alice=id-1:guests guest-bob=id-2:admins"
        r = self.guest("delete", "alice", FAKE_KC_USERS=users)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([a[:2] for a in self.kcadm() if a[0] in ("create", "delete")],
                         [["create", "users/id-1/logout"], ["delete", "users/id-1"]])
        self.kcadm_log.unlink()
        r = self.guest("delete", "bob", FAKE_KC_USERS=users)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("guests に入っていない", r.stderr)
        self.assertFalse([a for a in self.kcadm() if a[0] == "delete"])
        r = self.guest("delete", "carol", FAKE_KC_USERS=users)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("いない", r.stderr)

    def test_list_shows_each_guest_and_its_expiry_in_jst(self):
        r = self.guest("list", FAKE_KC_USERS="guest-alice=id-1:guests guest-bob=id-2:guests guest-carol=id-4:guests yamakura-yuma=id-3:admins",
                       FAKE_KC_EXPIRES="id-1=2026-10-12T18:00:00Z id-4=garbage")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 4, r.stdout)
        self.assertRegex(lines[1], r"^guest-alice +true +2026-10-13 03:00 JST$")
        self.assertRegex(lines[2], r"^guest-bob +true +無し \(次の実行で消える\)$")
        self.assertRegex(lines[3], r"^guest-carol +true +読めない: garbage")
        self.assertNotIn("yamakura-yuma", r.stdout)

    def test_usage(self):
        for args in [(), ("nope",), ("add",), ("delete",), ("delete", "a", "b"), ("list", "x"), ("add", "a", "extra")]:
            r = self.guest(*args)
            self.assertNotEqual(r.returncode, 0, args)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
