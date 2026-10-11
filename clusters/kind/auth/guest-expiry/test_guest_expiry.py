"""ゲストの期限切れを消す処理 (guest_expiry.py) と CronJob (cronjob.yaml) の試験。

標準ライブラリだけ: python3 -B -m unittest discover -s clusters/kind/auth/guest-expiry -v
手元に偽の Keycloak (Admin REST API の、この処理が使う所だけ) を http.server で立てて流す。クラスタにもネットワークにも出ない。
"""
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import guest_expiry  # noqa: E402

NOW = datetime.datetime(2026, 10, 12, 18, 0, tzinfo=datetime.timezone.utc)  # 2026-10-13 03:00 JST


class FakeKeycloak:
    """users: {id: {"username": ..., "attributes": {...}, "groups": [...]}}。呼ばれた (メソッド, パス) を calls に残す。"""

    def __init__(self, users, page=100):
        self.users = users
        self.page = page
        self.calls = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body=None):
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_any(self):
                url = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(url.query))
                fake.calls.append((self.command, url.path))
                if url.path == "/realms/master/protocol/openid-connect/token":
                    n = int(self.headers["Content-Length"])
                    form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode()))
                    ok = form == {"grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": "pw"}
                    return self.reply(200, {"access_token": "tok"}) if ok else self.reply(401, {})
                if self.headers.get("Authorization") != "Bearer tok":
                    return self.reply(401, {})
                path = url.path.removeprefix("/admin/realms/home-k8s/")
                if path == "groups":
                    return self.reply(200, [{"id": "g-guests", "name": "guests"}] if q.get("search") == "guests" else [])
                if path == "groups/g-guests/members":
                    assert q["briefRepresentation"] == "false"
                    members = [{"id": i, **{k: v for k, v in u.items() if k != "groups"}}
                               for i, u in fake.users.items() if "guests" in u["groups"]]
                    first, size = int(q["first"]), min(int(q["max"]), fake.page)
                    return self.reply(200, members[first:first + size])
                m = re.fullmatch(r"users/([^/]+)(/logout|/groups/g-guests)?", path)
                if m and m.group(1) in fake.users:
                    uid, sub = m.groups()
                    if self.command == "POST" and sub == "/logout":
                        return self.reply(204)
                    if self.command == "DELETE" and sub is None:
                        del fake.users[uid]
                        return self.reply(204)
                    if self.command == "DELETE" and sub == "/groups/g-guests":
                        fake.users[uid]["groups"].remove("guests")
                        return self.reply(204)
                return self.reply(404, {})

            do_GET = do_POST = do_DELETE = handle_any

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def guest(name, expires=None, groups=("guests",)):
    attrs = {"guest-expires-at": [expires]} if expires is not None else {}
    return {"username": name, "attributes": attrs, "groups": list(groups)}


class Sweep(unittest.TestCase):
    def run_sweep(self, users, page=100, now=NOW):
        fake = FakeKeycloak(users, page)
        self.addCleanup(fake.close)
        kc = guest_expiry.Keycloak(fake.url, "admin", "pw")
        kc.login()
        return guest_expiry.sweep(kc, now), fake

    def test_expired_guests_are_logged_out_and_deleted_and_others_stay(self):
        users = {
            "1": guest("guest-old", "2026-10-12T18:00:00Z"),       # ちょうど期限 (3:00 JST) は消す
            "2": guest("guest-new", "2026-10-13T18:00:00Z"),       # 次の日の 3:00 まで残す
            "3": guest("guest-past", "2026-01-01T00:00:00Z"),
            "4": {"username": "yamakura-yuma", "attributes": {}, "groups": ["admins"]},
        }
        (deleted, removed, kept), fake = self.run_sweep(users)
        self.assertEqual(sorted(deleted), ["guest-old", "guest-past"])
        self.assertEqual((removed, kept), ([], 1))
        self.assertEqual(sorted(u["username"] for u in fake.users.values()), ["guest-new", "yamakura-yuma"])
        # セッションを切ってから消す
        for uid in ("1", "3"):
            i = fake.calls.index(("POST", f"/admin/realms/home-k8s/users/{uid}/logout"))
            self.assertLess(i, fake.calls.index(("DELETE", f"/admin/realms/home-k8s/users/{uid}")))

    def test_missing_or_broken_expiry_counts_as_expired(self):
        users = {"1": guest("guest-none"), "2": guest("guest-bad", "tomorrow"), "3": guest("guest-naive", "2099-01-01T00:00:00"),
                 "4": {"username": "guest-empty", "attributes": {"guest-expires-at": []}, "groups": ["guests"]},
                 "5": {"username": "guest-null", "attributes": None, "groups": ["guests"]}}
        (deleted, _, kept), fake = self.run_sweep(users)
        self.assertEqual(sorted(deleted), ["guest-bad", "guest-empty", "guest-naive", "guest-none", "guest-null"])
        self.assertEqual((kept, fake.users), (0, {}))

    def test_people_without_the_guest_prefix_are_only_removed_from_guests(self):
        # 手で guests に入れた人 (人のユーザーを取り違えたときも) は消さない
        users = {"1": guest("yamakura-yuma", None, ("admins", "guests")), "2": guest("someone", "2099-01-01T00:00:00Z")}
        (deleted, removed, kept), fake = self.run_sweep(users)
        self.assertEqual((deleted, removed, kept), ([], ["yamakura-yuma"], 1))
        self.assertEqual(fake.users["1"]["groups"], ["admins"])
        self.assertNotIn(("DELETE", "/admin/realms/home-k8s/users/1"), fake.calls)
        self.assertNotIn(("POST", "/admin/realms/home-k8s/users/1/logout"), fake.calls)

    def test_running_again_changes_nothing(self):
        users = {"1": guest("guest-old", "2026-10-12T00:00:00Z"), "2": guest("guest-new", "2026-10-13T18:00:00Z")}
        fake = FakeKeycloak(users)
        self.addCleanup(fake.close)
        kc = guest_expiry.Keycloak(fake.url, "admin", "pw")
        kc.login()
        self.assertEqual(guest_expiry.sweep(kc, NOW), (["guest-old"], [], 1))
        self.assertEqual(guest_expiry.sweep(kc, NOW), ([], [], 1))
        # 3:00 を逃して後で流しても、その時点で過ぎていれば消える
        self.assertEqual(guest_expiry.sweep(kc, NOW + datetime.timedelta(days=2)), (["guest-new"], [], 0))

    def test_reads_every_page_of_members(self):
        users = {str(i): guest(f"guest-{i}", "2026-01-01T00:00:00Z") for i in range(7)}
        (deleted, _, _), fake = self.run_sweep(users, page=3)
        self.assertEqual(len(deleted), 7)
        self.assertEqual(fake.users, {})

    def test_missing_group_fails(self):
        fake = FakeKeycloak({})
        self.addCleanup(fake.close)
        kc = guest_expiry.Keycloak(fake.url, "admin", "pw")
        kc.login()
        kc.call = lambda method, path, query=None: [] if path == "groups" else self.fail(path)
        with self.assertRaises(SystemExit):
            guest_expiry.sweep(kc, NOW)

    def test_main_prints_names_and_counts_but_no_secrets(self):
        fake = FakeKeycloak({"1": guest("guest-old", "2026-01-01T00:00:00Z")})
        self.addCleanup(fake.close)
        env = {**os.environ, "KEYCLOAK_URL": fake.url, "KEYCLOAK_ADMIN_USERNAME": "admin", "KEYCLOAK_ADMIN_PASSWORD": "pw"}
        r = subprocess.run([sys.executable, "-B", "-I", str(HERE / "guest_expiry.py")], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("消した (期限切れ): guest-old", r.stdout)
        self.assertIn("消した 1、外した 0、残した 0", r.stdout)
        self.assertNotIn("tok", r.stdout + r.stderr)
        self.assertNotIn("pw", r.stdout + r.stderr)

    def test_main_gives_up_when_keycloak_does_not_answer(self):
        env = {**os.environ, "KEYCLOAK_URL": "http://127.0.0.1:9", "KEYCLOAK_ADMIN_USERNAME": "admin",
               "KEYCLOAK_ADMIN_PASSWORD": "pw", "KEYCLOAK_WAIT_SECONDS": "0"}
        r = subprocess.run([sys.executable, "-B", "-I", str(HERE / "guest_expiry.py")], env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("つながらない", r.stderr)


class CronJob(unittest.TestCase):
    def setUp(self):
        self.cron = (HERE / "cronjob.yaml").read_text()

    def test_runs_often_and_does_not_count_missed_runs(self):
        self.assertIn('  schedule: "*/5 * * * *"\n', self.cron)
        # 取りこぼしを数えない (周期の 300 秒より短い)。Forbid で重ねない
        deadline = int(re.search(r"startingDeadlineSeconds: (\d+)", self.cron).group(1))
        self.assertLess(deadline, 300)
        self.assertIn("concurrencyPolicy: Forbid", self.cron)
        # Keycloak を待つ時間は Job の上限より短い
        active = int(re.search(r"activeDeadlineSeconds: (\d+)", self.cron).group(1))
        wait = int(re.search(r'name: KEYCLOAK_WAIT_SECONDS\n\s+value: "(\d+)"', self.cron).group(1))
        self.assertLess(wait, active)
        self.assertLessEqual(active, 300)

    def test_reads_the_admin_from_the_secret_and_the_script_from_the_configmap(self):
        self.assertIn("name: keycloak-bootstrap-admin", self.cron)
        self.assertNotIn("password:", self.cron.replace("key: password", ""))
        kust = (HERE / "kustomization.yaml").read_text()
        self.assertIn("      - guest_expiry.py\n", kust)
        self.assertIn("/script/guest_expiry.py", self.cron)

    def test_application_points_here(self):
        app = (HERE.parents[1] / "argocd/apps/guest-expiry.yaml").read_text()
        self.assertIn("    path: clusters/kind/auth/guest-expiry\n", app)
        self.assertIn("    namespace: auth\n", app)


if __name__ == "__main__":
    unittest.main()
