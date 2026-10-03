"""just share の CLI (share.sh) の試験 (#42)。

標準ライブラリだけ: python3 -B -m unittest discover -s just -p test_share_cli.py
PATH の先頭に置いた偽の kubectl (fake_kubectl.py)・curl・sleep で、Secret share-credentials の読み書きだけを見る。
`kubectl exec` は実物の share_auth (clusters/kind/share) を手元の python3 で動かすので、CLI が作った項目を
実物の decide() に通して、通る・通らないまで確かめる。稼働中のクラスタにも、ホストの docker にも、ネットワークにも触れない。
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない

JUST = Path(__file__).resolve().parent
AUTH_DIR = JUST.parent / "clusters" / "kind" / "share"
sys.path.insert(0, str(AUTH_DIR))
import share_auth  # noqa: E402

FAKE_KUBECTL = JUST / "fake_kubectl.py"
HOSTS = {"grafana": "aaa-grafana", "headroom": "bbb-headroom", "backstage": "ccc-backstage"}
BOX = "INF |  https://{host}.trycloudflare.com  |\n"
PASSWORD_RE = re.compile(r"パスワード: ([0-9a-f]{32}) ")


def entry(expires_at, privileged=False, created_at=None):
    """Secret の 1 項目 (share.sh が書くのと同じ形)。hash は本物の PBKDF2。"""
    return json.dumps({"hash": share_auth.hash_password("old-password"), "expires_at": expires_at, "privileged": privileged,
                       "created_at": created_at or int(time.time()) - 60}, separators=(",", ":"))


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        self.stdin_log = self.tmp / "stdin.log"
        self.creds = self.tmp / "credentials.json"
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        self.curl_log = self.tmp / "curl.log"
        self.write_stub("kubectl", f'exec python3 "{FAKE_KUBECTL}" "$@"')
        self.write_stub("curl", f'echo "$*" >> "{self.curl_log}"\nexit "${{FAKE_CURL_EXIT:-0}}"')
        self.write_stub("sleep", "exit 0")
        for target, host in HOSTS.items():
            (self.logs / f"cloudflared-{target}").write_text(BOX.format(host=host))

    def write_stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\n" + body + "\n")
        path.chmod(0o755)

    def seed(self, **entries):
        """Secret share-credentials がある状態にする (項目は {名前: 項目の JSON 文字列})。"""
        self.creds.write_text(json.dumps(entries))

    def run_cli(self, *args, **env):
        return subprocess.run(
            ["bash", str(JUST / "share.sh"), "kind-test", *args], capture_output=True, text=True, cwd=self.tmp,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_KUBECTL_LOG": str(self.log),
                 "FAKE_KUBECTL_STDIN": str(self.stdin_log), "FAKE_LOGS": str(self.logs), "FAKE_CREDENTIALS": str(self.creds),
                 "FAKE_AUTH_DIR": str(AUTH_DIR), "SHARE_WAIT_SECONDS": "0", "HOME": str(self.tmp), **env})

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def patches(self):
        if not self.stdin_log.exists():
            return []
        return [json.loads(line)["manifest"]["data"] for line in self.stdin_log.read_text().splitlines()
                if json.loads(line)["verb"] == "patch"]

    def stored(self):
        """Secret の今の中身 {名前: 項目 (dict)}。"""
        return {name: json.loads(raw) for name, raw in json.loads(self.creds.read_text()).items()} if self.creds.exists() else {}

    def decide(self, name, password, now=None, creds=None):
        raw = creds if creds is not None else {k: json.dumps(v) for k, v in self.stored().items()}
        header = "Basic " + base64.b64encode(f"{name}:{password}".encode()).decode()
        return share_auth.decide(raw, {"authorization": header}, now or time.time(), b"k")[0]

    def password_of(self, result):
        return PASSWORD_RE.search(result.stdout).group(1)


class RejectedBeforeTouchingTheCluster(CliCase):
    """引数の誤りは、kubectl を 1 回も呼ばずに断る。"""

    def assert_rejected(self, *args, code=2):
        r = self.run_cli(*args)
        self.assertEqual(r.returncode, code, (args, r.stdout, r.stderr))
        self.assertEqual(self.calls(), [], f"{args}: クラスタに触れた")
        return r

    def test_ttl_over_the_limit(self):
        self.assert_rejected("add", "alice", "--ttl", "25h")
        self.assert_rejected("add", "alice", "--ttl", "1441m")
        self.assert_rejected("add", "alice", "--ttl", "99999h")

    def test_ttl_at_the_limit_is_not_rejected_by_the_check(self):
        self.seed()
        for ttl in ("24h", "1440m"):
            self.assertEqual(self.run_cli("add", f"a{ttl[:2]}", "--ttl", ttl).returncode, 0, ttl)

    def test_ttl_zero(self):
        for ttl in ("0", "0m", "0h", "00h"):
            self.assert_rejected("add", "alice", "--ttl", ttl)

    def test_ttl_in_a_bad_form(self):
        for ttl in ("8", "h", "-1h", "1.5h", "8hours", "1d", "", "8h ", " 8h"):
            self.assert_rejected("add", "alice", "--ttl", ttl)
        self.assert_rejected("add", "alice", "--ttl")

    def test_bad_names(self):
        for name in ("Alice", "-alice", "a_b", "a.b", "a b", "a" * 33, "あ", "a;b", 'a"b', "a/b"):
            self.assert_rejected("add", name)
        for name in ("Alice", "a" * 33, "a;b"):
            self.assert_rejected("delete", name)
            self.assert_rejected("get", name)
            self.assert_rejected("rotate", name)

    def test_permanent_with_ttl(self):
        self.assert_rejected("add", "alice", "--permanent", "--ttl", "1h")
        self.assert_rejected("add", "alice", "--ttl", "1h", "--permanent")

    def test_usage_errors(self):
        self.assert_rejected()
        self.assert_rejected("nope")
        self.assert_rejected("add")
        self.assert_rejected("add", "alice", "bob")
        self.assert_rejected("add", "alice", "--unknown")
        self.assert_rejected("delete")
        self.assert_rejected("list", "extra")
        self.assert_rejected("prune", "extra")
        self.assert_rejected("smoke")
        self.assert_rejected("test-smoke", "extra")
        self.assertIn("usage: just share", self.run_cli("nope").stderr)


class Add(CliCase):
    def test_adds_a_timed_entry_and_shows_everything(self):
        self.seed()
        r = self.run_cli("add", "alice", "--ttl", "2h")
        self.assertEqual(r.returncode, 0, r.stderr)
        password = self.password_of(r)
        ((name, item),) = self.stored().items()
        self.assertEqual(name, "alice")
        self.assertEqual((item["privileged"], item["expires_at"] - item["created_at"]), (False, 2 * 3600))
        self.assertIn("名前:       alice", r.stdout)
        self.assertIn("期限:", r.stdout)
        for target, host in HOSTS.items():
            self.assertIn(f"{target} https://{host}.trycloudflare.com", r.stdout)
        # 保存はハッシュだけ。パスワードは Secret にも、kubectl の引数にも、curl の引数にも出ない
        self.assertNotIn(password, self.creds.read_text())
        self.assertNotIn(password, "\n".join(self.calls()))
        self.assertNotIn(password, self.curl_log.read_text() if self.curl_log.exists() else "")
        for patch in self.patches():
            self.assertNotIn(password, json.dumps(patch))
            self.assertNotIn(base64.b64encode(password.encode()).decode(), json.dumps(patch))

    def test_default_ttl_is_8_hours(self):
        self.seed()
        self.assertEqual(self.run_cli("add", "alice").returncode, 0)
        item = self.stored()["alice"]
        self.assertEqual(item["expires_at"] - item["created_at"], 8 * 3600)

    def test_the_new_credential_works_and_a_wrong_password_does_not(self):
        # CLI が作った項目を、実物の認証サービス (decide) に通す
        self.seed()
        password = self.password_of(self.run_cli("add", "alice", "--ttl", "30m"))
        self.assertEqual(self.decide("alice", password), 200)
        self.assertEqual(self.decide("alice", "0" * 32), 401)
        self.assertEqual(self.decide("bob", password), 401)
        self.assertEqual(self.decide("alice", password, now=time.time() + 31 * 60), 401, "期限後は拒否")

    def test_writes_only_its_own_entry_with_a_merge_patch(self):
        self.seed(bob=entry(int(time.time()) + 3600))
        self.assertEqual(self.run_cli("add", "alice").returncode, 0)
        self.assertEqual([list(p) for p in self.patches()], [["alice"]], "ほかの人の項目に触れない")
        self.assertIn("bob", self.stored())
        patch_calls = [c for c in self.calls() if " patch " in c]
        self.assertEqual(len(patch_calls), 1)
        self.assertIn("--type merge", patch_calls[0])
        self.assertIn("--patch-file", patch_calls[0], "hash をコマンドラインに出さない")

    def test_existing_name_is_refused(self):
        self.seed(alice=entry(int(time.time()) + 3600))
        r = self.run_cli("add", "alice")
        self.assertEqual(r.returncode, 1)
        self.assertIn("既にある", r.stderr)
        self.assertEqual(self.patches(), [])

    def test_a_second_permanent_is_refused(self):
        self.seed(boss=entry(None, privileged=True))
        r = self.run_cli("add", "boss2", "--permanent")
        self.assertEqual(r.returncode, 1)
        self.assertIn("特権 viewer は全体で 1 つだけ", r.stderr)
        self.assertEqual(self.patches(), [], "書き込んでいない")
        self.assertEqual(list(self.stored()), ["boss"])

    def test_the_first_permanent_has_no_expiry(self):
        self.seed(bob=entry(int(time.time()) + 3600))
        r = self.run_cli("add", "boss", "--permanent")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.stored()["boss"]["privileged"], self.stored()["boss"]["expires_at"]), (True, None))
        self.assertIn("特権 viewer", r.stdout)
        self.assertEqual(self.decide("boss", self.password_of(r), now=time.time() + 10 * 365 * 86400), 200, "期限がない")

    def test_a_timed_add_is_allowed_next_to_a_permanent(self):
        self.seed(boss=entry(None, privileged=True))
        self.assertEqual(self.run_cli("add", "alice").returncode, 0)

    def test_add_sweeps_expired_entries_first(self):
        self.seed(old=entry(int(time.time()) - 5), keep=entry(int(time.time()) + 3600))
        r = self.run_cli("add", "old")  # 期限切れの同名は、掃除されてから作り直せる
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(self.stored()), ["keep", "old"])
        self.assertEqual(self.patches()[0], {"old": None})
        self.assertGreater(self.stored()["old"]["expires_at"], time.time())

    def test_the_secret_is_missing(self):
        r = self.run_cli("add", "alice")  # seed していない
        self.assertEqual(r.returncode, 1)
        self.assertIn("just up", r.stderr)
        self.assertEqual(self.patches(), [])

    def test_nothing_is_written_when_the_pod_cannot_hash(self):
        self.seed()
        self.write_stub("kubectl", f'case "$*" in *" exec "*) echo "pod not found" >&2; exit 1;; esac\nexec python3 "{FAKE_KUBECTL}" "$@"')
        r = self.run_cli("add", "alice")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.patches(), [])
        self.assertEqual(self.stored(), {})

    def test_credentials_are_shown_even_if_the_urls_do_not_come_up(self):
        # パスワードは 1 回しか出せない。URL が引けなくても失わない
        self.seed()
        r = self.run_cli("add", "alice", FAKE_CURL_EXIT="6")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, PASSWORD_RE)
        self.assertIn("確かめられなかった", r.stderr)

    def test_urls_are_checked_over_doh(self):
        self.seed()
        self.assertEqual(self.run_cli("add", "alice").returncode, 0)
        curl_calls = self.curl_log.read_text().splitlines()
        self.assertEqual(len(curl_calls), 3)
        for line, host in zip(curl_calls, HOSTS.values()):
            self.assertIn("--doh-url https://1.1.1.1/dns-query", line)
            self.assertIn(f"https://{host}.trycloudflare.com/", line)


class Delete(CliCase):
    def test_deletes_the_entry_and_the_old_password_stops_working(self):
        self.seed()
        password = self.password_of(self.run_cli("add", "alice"))
        self.assertEqual(self.decide("alice", password), 200)
        r = self.run_cli("delete", "alice")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.stored(), {})
        self.assertEqual(self.decide("alice", password), 401)
        self.assertEqual(self.patches()[-1], {"alice": None})

    def test_deletes_a_permanent_one_too(self):
        self.seed(boss=entry(None, privileged=True), bob=entry(int(time.time()) + 3600))
        self.assertEqual(self.run_cli("delete", "boss").returncode, 0)
        self.assertEqual(list(self.stored()), ["bob"])

    def test_unknown_name(self):
        self.seed()
        r = self.run_cli("delete", "nobody")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.patches(), [])


class ListAndGet(CliCase):
    def setUp(self):
        super().setUp()
        t = int(time.time())
        self.seed(alice=entry(t + 2 * 3600 + 30 * 60), boss=entry(None, privileged=True), gone=entry(t - 10), broken="not json")

    def test_list_shows_kind_remaining_state_and_urls(self):
        r = self.run_cli("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = {line.split()[0]: line for line in r.stdout.splitlines() if line.split() and line.split()[0] in ("alice", "boss", "gone", "broken")}
        self.assertRegex(rows["alice"], r"期限付き\s+2時間(29|30)分\s+有効")
        self.assertRegex(rows["boss"], r"特権\s+期限なし\s+有効")
        self.assertRegex(rows["gone"], r"期限付き\s+期限切れ\s+期限切れ")
        self.assertRegex(rows["broken"], r"不正")
        for target, host in HOSTS.items():
            self.assertIn(f"{target} https://{host}.trycloudflare.com", r.stdout)
        self.assertEqual(self.patches(), [], "list は書かない")

    def test_list_when_empty(self):
        self.seed()
        r = self.run_cli("list")
        self.assertEqual(r.returncode, 0)
        self.assertIn("資格情報は無い", r.stdout)

    def test_get_shows_info_and_urls_but_never_the_password(self):
        r = self.run_cli("get", "alice")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("名前: alice", r.stdout)
        self.assertIn("種別: 期限付き", r.stdout)
        self.assertNotRegex(r.stdout, r"[0-9a-f]{32}")
        self.assertNotIn("pbkdf2", r.stdout, "hash も出さない")
        self.assertIn("backstage https://ccc-backstage.trycloudflare.com", r.stdout)
        self.assertEqual(self.patches(), [])

    def test_get_a_permanent_one(self):
        r = self.run_cli("get", "boss")
        self.assertIn("種別: 特権", r.stdout)
        self.assertIn("期限: なし", r.stdout)

    def test_get_an_expired_one_says_so(self):
        self.assertIn("期限切れ", self.run_cli("get", "gone").stdout)

    def test_get_unknown(self):
        self.assertEqual(self.run_cli("get", "nobody").returncode, 1)

    def test_urls_that_cannot_be_read_are_dashes(self):
        for f in self.logs.iterdir():
            f.unlink()
        r = self.run_cli("list")
        self.assertEqual(r.returncode, 0)
        self.assertIn("grafana -", r.stdout)


class Rotate(CliCase):
    def test_rotates_the_permanent_one(self):
        self.seed()
        old = self.password_of(self.run_cli("add", "boss", "--permanent"))
        before = self.stored()["boss"]
        r = self.run_cli("rotate", "boss")
        self.assertEqual(r.returncode, 0, r.stderr)
        new = self.password_of(r)
        self.assertNotEqual(old, new)
        after = self.stored()["boss"]
        self.assertNotEqual(after["hash"], before["hash"])
        self.assertEqual((after["privileged"], after["expires_at"]), (True, None))
        self.assertEqual(self.decide("boss", new), 200)
        self.assertEqual(self.decide("boss", old), 401, "古いパスワードは効かない")
        self.assertNotIn(new, "\n".join(self.calls()))

    def test_refused_for_a_timed_entry(self):
        self.seed(alice=entry(int(time.time()) + 3600))
        r = self.run_cli("rotate", "alice")
        self.assertEqual(r.returncode, 2)
        self.assertIn("delete → add", r.stderr)
        self.assertEqual(self.patches(), [], "書いていない")
        self.assertEqual([c for c in self.calls() if " exec " in c], [], "ハッシュも作っていない")

    def test_unknown_name(self):
        self.seed()
        self.assertEqual(self.run_cli("rotate", "nobody").returncode, 1)


class Prune(CliCase):
    def test_removes_only_expired_entries(self):
        t = int(time.time())
        self.seed(gone=entry(t - 1), gone2=entry(t - 3600), alice=entry(t + 3600), boss=entry(None, privileged=True))
        r = self.run_cli("prune")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(self.stored()), ["alice", "boss"])
        self.assertEqual(len(self.patches()), 1, "1 回の patch でまとめて消す")
        self.assertIn("gone", r.stdout)

    def test_nothing_to_prune(self):
        self.seed(alice=entry(int(time.time()) + 3600))
        r = self.run_cli("prune")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.patches(), [])
        self.assertIn("無い", r.stdout)


class EntryFormat(CliCase):
    def test_the_entry_passes_the_services_own_validation(self):
        self.seed()
        self.run_cli("add", "alice")
        raw = json.loads(self.creds.read_text())["alice"]
        parsed = share_auth.parse_entry(raw)
        self.assertIsNotNone(parsed)
        self.assertGreaterEqual(int(share_auth.HASH_RE.fullmatch(parsed["hash"])[1]), share_auth.MIN_ITERATIONS)


if __name__ == "__main__":
    unittest.main()
