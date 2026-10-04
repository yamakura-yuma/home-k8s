"""share の認証サービス (share_auth.py) のテスト。拒否を確かめるのが主で、既定は 401。

標準ライブラリだけ: python3 -B -m unittest discover -s clusters/kind/share
時刻は decide() の引数なので、期限の前後を実時間を待たずに確かめられる。
"""
import base64
import contextlib
import functools
import hashlib
import io
import json
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.dont_write_bytecode = True  # __pycache__ をリポジトリに作らない
sys.path.insert(0, str(Path(__file__).resolve().parent))

import share_auth as sa  # noqa: E402

KEY = b"session-key"
T0 = 1_000_000  # 発行時刻
NOT_GIVEN = object()


@functools.lru_cache(maxsize=None)
def hashed(password):
    """試験の速さのため、同じパスワードには同じ hash を返す (PBKDF2 は 1 回約 70ms)。ソルトの試験は hash_password を直接呼ぶ。"""
    return sa.hash_password(password)


def entry(password, expires_at=None, privileged=False):
    return {"hash": hashed(password), "expires_at": expires_at, "privileged": privileged, "created_at": T0}


def basic(name, password):
    return "Basic " + base64.b64encode(f"{name}:{password}".encode()).decode()


def with_basic(name, password):
    return {"authorization": basic(name, password)}


def cookie_from(headers):
    """Set-Cookie の `share_session=<値>; ...` から、ブラウザが送り返す Cookie ヘッダを作る。"""
    return headers["Set-Cookie"].split(";")[0]


class Decide(unittest.TestCase):
    def setUp(self):
        self.creds = {"alice": entry("pw-alice", expires_at=T0 + 8 * 3600), "root": entry("pw-root", privileged=True)}

    def status(self, request, now=T0, creds=NOT_GIVEN, key=KEY):
        return sa.decide(self.creds if creds is NOT_GIVEN else creds, request, now, key)[0]

    # --- Basic ---
    def test_valid_basic_passes(self):
        self.assertEqual(self.status(with_basic("alice", "pw-alice")), 200)

    def test_no_credentials_rejected(self):
        self.assertEqual(self.status({}), 401)

    def test_wrong_password_rejected(self):
        self.assertEqual(self.status(with_basic("alice", "pw-bob")), 401)

    def test_unknown_name_rejected(self):
        self.assertEqual(self.status(with_basic("bob", "pw-alice")), 401)

    def test_empty_name_and_password_rejected(self):
        self.assertEqual(self.status(with_basic("", "")), 401)

    def test_bad_name_rejected(self):
        creds = {"Alice": entry("pw"), "../x": entry("pw"), "alice\n": entry("pw")}
        self.assertEqual(self.status(with_basic("alice\n", "pw"), creds=creds), 401)
        self.assertEqual(self.status(with_basic("Alice", "pw"), creds=creds), 401)
        self.assertEqual(self.status(with_basic("../x", "pw"), creds=creds), 401)

    def test_malformed_authorization_rejected(self):
        for value in ["Basic", "Basic !!!not-base64", "Basic " + base64.b64encode(b"\xff\xfe:x").decode(), "Bearer x"]:
            self.assertEqual(self.status({"authorization": value}), 401, value)

    def test_password_may_contain_colon(self):
        creds = {"alice": entry("a:b")}
        self.assertEqual(self.status(with_basic("alice", "a:b"), creds=creds), 200)

    # --- 期限 ---
    def test_expiry_boundary(self):
        exp = T0 + 8 * 3600
        request = with_basic("alice", "pw-alice")
        self.assertEqual(self.status(request, now=exp - 1), 200)
        self.assertEqual(self.status(request, now=exp), 401)
        self.assertEqual(self.status(request, now=exp + 1), 401)

    def test_permanent_never_expires(self):
        far = T0 + 10 * 365 * 24 * 3600
        self.assertEqual(self.status(with_basic("root", "pw-root"), now=far), 200)

    # --- 削除・ローテーション ---
    def test_deleted_entry_rejected(self):
        request = with_basic("alice", "pw-alice")
        self.assertEqual(self.status(request), 200)
        del self.creds["alice"]
        self.assertEqual(self.status(request), 401)

    def test_old_password_rejected_after_rotate(self):
        self.creds["root"] = entry("pw-root-2", privileged=True)
        self.assertEqual(self.status(with_basic("root", "pw-root")), 401)
        self.assertEqual(self.status(with_basic("root", "pw-root-2")), 200)

    # --- cookie ---
    def issue_cookie(self, name="alice", password="pw-alice"):
        status, headers = sa.decide(self.creds, with_basic(name, password), T0, KEY)
        self.assertEqual(status, 200)
        return cookie_from(headers)

    def test_basic_sets_signed_cookie(self):
        _, headers = sa.decide(self.creds, with_basic("alice", "pw-alice"), T0, KEY)
        cookie = headers["Set-Cookie"]
        self.assertTrue(cookie.startswith("share_session=alice."), cookie)
        for attr in ["Path=/", "HttpOnly", "Secure", "SameSite=Lax"]:
            self.assertIn(attr, cookie)

    def test_rejected_basic_sets_no_cookie(self):
        self.assertEqual(sa.decide(self.creds, with_basic("alice", "bad"), T0, KEY), (401, {}))

    def test_cookie_passes_with_bearer_authorization(self):
        # Backstage の API 呼び出しは Bearer を付ける。名前の確認は cookie で行う
        request = {"authorization": "Bearer backstage-token", "cookie": self.issue_cookie()}
        self.assertEqual(self.status(request), 200)

    def test_cookie_among_other_cookies(self):
        request = {"cookie": "a=1; " + self.issue_cookie() + "; b=2"}
        self.assertEqual(self.status(request), 200)

    def test_tampered_cookie_rejected(self):
        value = self.issue_cookie().split("=", 1)[1]
        name, sig = value.rsplit(".", 1)
        forged = [
            f"{name}.{'0' * 64}",
            f"{name}.{sig[:-1]}{'0' if sig[-1] != '0' else '1'}",
            f"root.{sig}",  # 別の名前に付け替える
            name,  # 署名なし
            f".{sig}",
            "",
        ]
        for v in forged:
            self.assertEqual(self.status({"cookie": f"share_session={v}"}), 401, v)

    def test_cookie_signed_with_other_key_rejected(self):
        request = {"cookie": self.issue_cookie()}
        self.assertEqual(self.status(request, key=b"another-key"), 401)

    def test_cookie_rejected_without_session_key(self):
        request = {"cookie": self.issue_cookie()}
        self.assertEqual(self.status(request, key=b""), 401)

    def test_unsigned_cookie_forged_for_empty_key_rejected(self):
        # 鍵が読めない (空) ときに、空の鍵で作った署名を通さない
        forged = sa.sign(b"", "alice", self.creds["alice"])
        self.assertEqual(self.status({"cookie": f"share_session=alice.{forged}"}, key=b""), 401)

    def test_cookie_rejected_after_delete(self):
        request = {"cookie": self.issue_cookie()}
        del self.creds["alice"]
        self.assertEqual(self.status(request), 401)

    def test_cookie_rejected_after_expiry(self):
        request = {"cookie": self.issue_cookie()}
        self.assertEqual(self.status(request, now=T0 + 8 * 3600 - 1), 200)
        self.assertEqual(self.status(request, now=T0 + 8 * 3600 + 1), 401)

    def test_cookie_rejected_after_rotate(self):
        request = {"cookie": self.issue_cookie("root", "pw-root")}
        self.assertEqual(self.status(request), 200)
        self.creds["root"] = entry("pw-root-2", privileged=True)
        self.assertEqual(self.status(request), 401)

    def test_garbage_cookie_header_rejected(self):
        for header in ["share_session", "share_session=", ";;;", 'share_session="a.b', "\x00"]:
            self.assertEqual(self.status({"cookie": header}), 401, header)

    # --- Secret の状態 ---
    def test_empty_secret_rejected(self):
        self.assertEqual(self.status(with_basic("alice", "pw-alice"), creds={}), 401)
        self.assertEqual(self.status({"cookie": self.issue_cookie()}, creds={}), 401)

    def test_broken_entry_rejected(self):
        good = entry("pw")
        broken = {
            "not json": "{nope",
            "empty": "",
            "null": "null",
            "list": "[]",
            "number": "5",
            "no-hash": json.dumps({k: v for k, v in good.items() if k != "hash"}),
            "no-expiry": json.dumps({k: v for k, v in good.items() if k != "expires_at"}),
            "no-privileged": json.dumps({k: v for k, v in good.items() if k != "privileged"}),
            "no-created": json.dumps({k: v for k, v in good.items() if k != "created_at"}),
            "short-hash": json.dumps({**good, "hash": "abc"}),
            "upper-hash": json.dumps({**good, "hash": good["hash"].upper()}),
            "hash-type": json.dumps({**good, "hash": 1}),
            "legacy-sha256": json.dumps({**good, "hash": hashlib.sha256(b"pw").hexdigest()}),
            "other-scheme": json.dumps({**good, "hash": good["hash"].replace("pbkdf2_sha256", "pbkdf2_sha1", 1)}),
            "weak-iterations": json.dumps({**good, "hash": sa.hash_password("pw", iterations=1000)}),
            "trailing-newline": json.dumps({**good, "hash": good["hash"] + "\n"}),
            "expiry-string": json.dumps({**good, "expires_at": "never"}),
            "expiry-true": json.dumps({**good, "expires_at": True}),
            "privileged-str": json.dumps({**good, "privileged": "yes"}),
            "created-true": json.dumps({**good, "created_at": True}),
        }
        for name, raw in broken.items():
            self.assertEqual(self.status(with_basic(name, "pw"), creds={name: raw}), 401, name)

    def test_broken_entry_does_not_affect_others(self):
        self.creds["broken"] = "{nope"
        self.assertEqual(self.status(with_basic("alice", "pw-alice")), 200)
        self.assertEqual(self.status(with_basic("broken", "pw")), 401)

    def test_entry_as_json_string_passes(self):
        creds = {"alice": json.dumps(entry("pw"))}
        self.assertEqual(self.status(with_basic("alice", "pw"), creds=creds), 200)

    def test_creds_not_a_mapping_rejected(self):
        for creds in [None, [], "x"]:
            self.assertEqual(self.status(with_basic("alice", "pw-alice"), creds=creds), 401)


class Password(unittest.TestCase):
    def test_format_and_documented_iterations(self):
        scheme, iterations, salt, key = sa.hash_password("pw").split("$")
        self.assertEqual((scheme, int(iterations)), ("pbkdf2_sha256", 600_000))  # docs/cluster/share.md §2 の値
        self.assertEqual((len(salt), len(key)), (32, 64))

    def test_same_password_gets_different_salt(self):
        self.assertNotEqual(sa.hash_password("pw", iterations=sa.MIN_ITERATIONS), sa.hash_password("pw", iterations=sa.MIN_ITERATIONS))

    def test_verify(self):
        stored = sa.hash_password("pw", iterations=sa.MIN_ITERATIONS)
        self.assertTrue(sa.verify_password("pw", stored))
        self.assertFalse(sa.verify_password("pw2", stored))
        self.assertFalse(sa.verify_password("", stored))

    def test_old_iteration_count_still_verifies(self):
        # 反復回数は項目ごとに持つので、将来 ITERATIONS を上げても古い項目は読める
        self.assertTrue(sa.verify_password("pw", sa.hash_password("pw", iterations=sa.MIN_ITERATIONS + 50_000)))

    def test_malformed_or_weak_stored_value_never_verifies(self):
        weak = sa.hash_password("pw", iterations=1000)
        for stored in ["", "x", sa.DUMMY_HASH + "0", hashlib.sha256(b"pw").hexdigest(), weak]:
            self.assertFalse(sa.verify_password("pw", stored), stored)

    def test_unknown_name_still_computes_pbkdf2(self):
        # 名前の有無を応答の時間から探られない。ダミーでも、保存された hash と同じ回数の反復をする
        with unittest.mock.patch.object(hashlib, "pbkdf2_hmac", wraps=hashlib.pbkdf2_hmac) as kdf:
            self.assertEqual(sa.decide({}, with_basic("nobody", "pw"), T0, KEY)[0], 401)
        self.assertEqual([c.args[3] for c in kdf.call_args_list], [sa.ITERATIONS])


class VerifyCache(unittest.TestCase):
    """照合結果の覚え。覚えていても、期限・delete・ローテーションは毎回効く。"""

    def setUp(self):
        self.clock = [0.0]
        self.kdf = []
        real = sa.verify_password

        def counting(password, stored):
            self.kdf.append(1)
            return real(password, stored)

        patch = unittest.mock.patch.object(sa, "verify_password", counting)
        patch.start()
        self.addCleanup(patch.stop)
        self.verifier = sa.VerifyCache(ttl=30, clock=lambda: self.clock[0])
        self.creds = {"alice": entry("pw", expires_at=T0 + 3600)}

    def status(self, password="pw", now=T0, creds=None):
        request = with_basic("alice", password)
        return sa.decide(self.creds if creds is None else creds, request, now, KEY, self.verifier)[0]

    def test_second_request_skips_pbkdf2(self):
        self.assertEqual(self.status(), 200)
        self.assertEqual(self.status(), 200)
        self.assertEqual(len(self.kdf), 1)

    def test_wrong_password_is_never_remembered(self):
        self.assertEqual(self.status("bad"), 401)
        self.assertEqual(self.status("bad"), 401)
        self.assertEqual(len(self.kdf), 2)

    def test_wrong_password_after_success_still_rejected(self):
        self.assertEqual(self.status(), 200)
        self.assertEqual(self.status("bad"), 401)

    def test_remembered_result_expires(self):
        self.assertEqual(self.status(), 200)
        self.clock[0] = 29.9
        self.assertEqual(self.status(), 200)
        self.assertEqual(len(self.kdf), 1)
        self.clock[0] = 30.0
        self.assertEqual(self.status(), 200)
        self.assertEqual(len(self.kdf), 2)

    def test_ttl_zero_remembers_nothing(self):
        self.verifier = sa.VerifyCache(ttl=0, clock=lambda: self.clock[0])
        self.status()
        self.status()
        self.assertEqual(len(self.kdf), 2)

    def test_delete_wins_over_remembered_result(self):
        self.assertEqual(self.status(), 200)
        self.assertEqual(self.status(creds={}), 401)

    def test_expiry_wins_over_remembered_result(self):
        self.assertEqual(self.status(), 200)
        self.assertEqual(self.status(now=T0 + 3600), 401)

    def test_rotate_wins_over_remembered_result(self):
        self.assertEqual(self.status(), 200)
        self.creds["alice"] = entry("pw-2", expires_at=T0 + 3600)
        self.assertEqual(self.status("pw"), 401)
        self.assertEqual(self.status("pw-2"), 200)

    def test_cookie_binds_salt_not_password_hash(self):
        # 同じパスワードでも作り直せば salt が変わり、古い cookie は効かない
        request = {"cookie": cookie_from(sa.decide(self.creds, with_basic("alice", "pw"), T0, KEY, self.verifier)[1])}
        self.assertEqual(sa.decide(self.creds, request, T0, KEY)[0], 200)
        self.creds["alice"] = {**self.creds["alice"], "hash": sa.hash_password("pw")}
        self.assertEqual(sa.decide(self.creds, request, T0, KEY)[0], 401)

    def test_delete_then_add_with_same_password_makes_new_hash(self):
        # ソルトが違うので hash が変わり、覚えた結果は使われない (cookie の署名も別になる)
        self.assertEqual(self.status(), 200)
        self.creds["alice"] = {**self.creds["alice"], "hash": sa.hash_password("pw")}
        before = len(self.kdf)
        self.assertEqual(self.status(), 200)
        self.assertEqual(len(self.kdf), before + 1)

    def test_non_ascii_password_is_remembered_and_checked(self):
        creds = {"alice": entry("パスワード", expires_at=T0 + 3600)}
        self.assertEqual(self.status("パスワード", creds=creds), 200)
        self.assertEqual(self.status("パスワード", creds=creds), 200)
        self.assertEqual(len(self.kdf), 1)
        self.assertEqual(self.status("パスワート", creds=creds), 401)

    def test_one_slot_per_name(self):
        for _ in range(3):
            self.verifier("alice", "pw", self.creds["alice"]["hash"])
        self.assertEqual(len(self.verifier.entries), 1)


class Sources(unittest.TestCase):
    def setUp(self):
        # 読めないときの警告は期待どおりなので、CI のログに混ぜない
        stderr = contextlib.redirect_stderr(io.StringIO())
        stderr.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)

    def write(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        self.addCleanup(Path(f.name).unlink)
        f.write(data if isinstance(data, str) else json.dumps(data))
        f.close()
        return f.name

    def test_file_source_reads_credentials_and_key(self):
        path = self.write({"credentials": {"alice": entry("pw")}, "session_key": "k"})
        creds, key = sa.source_from_env({"AUTH_SOURCE": f"file:{path}"}).load()
        self.assertEqual(sa.decide(creds, with_basic("alice", "pw"), T0, key)[0], 200)

    def test_cache_returns_empty_when_unreadable(self):
        for body in ["{broken", "[]", '{"credentials": {}}', ""]:
            cache = sa.CachedSource(sa.FileSource(self.write(body)), ttl=0)
            self.assertEqual(cache.load(), ({}, b""), body)
        cache = sa.CachedSource(sa.FileSource("/nonexistent/share.json"), ttl=0)
        self.assertEqual(cache.load(), ({}, b""))

    def test_cache_ttl(self):
        clock = [0.0]
        loads = []

        class Counting:
            def load(self):
                loads.append(1)
                return {"n": len(loads)}, b"k"

        cache = sa.CachedSource(Counting(), ttl=2, clock=lambda: clock[0])
        cache.load()
        clock[0] = 1.9
        cache.load()
        self.assertEqual(len(loads), 1)
        clock[0] = 2.0
        self.assertEqual(cache.load()[0], {"n": 2})

    def test_cache_does_not_keep_stale_value_after_failure(self):
        class Flaky:
            ok = True

            def load(self):
                if not self.ok:
                    raise OSError("api down")
                return {"alice": entry("pw")}, b"k"

        source = Flaky()
        cache = sa.CachedSource(source, ttl=0)
        self.assertEqual(cache.load()[1], b"k")
        source.ok = False
        self.assertEqual(cache.load(), ({}, b""))


class FakeKubernetesAPI(BaseHTTPRequestHandler):
    """K8s API の代わり。Secret を返す。トークンが違えば 401、無い Secret は 404。"""

    secrets = {}

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer sa-token":
            return self.reply(401, {})
        name = self.path.rsplit("/", 1)[-1]
        if self.path != f"/api/v1/namespaces/share/secrets/{name}" or name not in self.secrets:
            return self.reply(404, {})
        data = {k: base64.b64encode(v.encode()).decode() for k, v in self.secrets[name].items()}
        self.reply(200, {"kind": "Secret", "data": data})

    def reply(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class KubernetesSource(unittest.TestCase):
    def setUp(self):
        stderr = contextlib.redirect_stderr(io.StringIO())
        stderr.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)
        self.api = ThreadingHTTPServer(("127.0.0.1", 0), FakeKubernetesAPI)
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.addCleanup(self.api.server_close)
        self.addCleanup(self.api.shutdown)
        self.token = tempfile.NamedTemporaryFile("w", delete=False)
        self.token.write("sa-token\n")
        self.token.close()
        self.addCleanup(Path(self.token.name).unlink)
        FakeKubernetesAPI.secrets = {
            sa.CREDENTIALS_SECRET: {"alice": json.dumps(entry("pw"))},
            sa.SESSION_KEY_SECRET: {"key": "k"},
        }

    def source(self, token_path=None):
        url = f"http://127.0.0.1:{self.api.server_port}"
        return sa.KubernetesSource(url, token_path or self.token.name, "/unused", "share")

    def test_reads_and_decodes_secrets(self):
        creds, key = self.source().load()
        self.assertEqual(key, b"k")
        self.assertEqual(sa.decide(creds, with_basic("alice", "pw"), T0, key)[0], 200)

    def test_reads_the_viewer_value_from_share_grafana(self):
        # #58: Grafana に渡す viewer のパスワードは Secret share-grafana (キー viewer-password) を API で読む
        FakeKubernetesAPI.secrets[sa.GRAFANA_SECRET] = {sa.VIEWER_ENTRY: "viewer pw"}
        self.assertEqual(self.source().load_viewer_value(), "viewer pw")

    def test_missing_share_grafana_leaves_the_credentials_readable(self):
        # share-grafana が無い・キーが違う・空: viewer のパスワードは空 (Grafana の経路だけ拒否)。資格情報は読める (headroom・backstage は動く)
        viewer = sa.ViewerCache(self.source(), ttl=0)
        self.assertEqual(viewer.load(), "")
        FakeKubernetesAPI.secrets[sa.GRAFANA_SECRET] = {"other-key": "x"}
        self.assertEqual(viewer.load(), "")
        FakeKubernetesAPI.secrets[sa.GRAFANA_SECRET] = {sa.VIEWER_ENTRY: ""}
        self.assertEqual(viewer.load(), "")
        credentials, key = sa.CachedSource(self.source(), ttl=0).load()
        self.assertEqual(sa.decide(credentials, with_basic("alice", "pw"), T0, key)[0], 200)

    def test_viewer_value_change_is_picked_up_without_restart(self):
        # #58: 読み直しは AUTH_CACHE_TTL ごと。変えたら ttl の後の次の要求から新しい値で、プロセスは作り直さない。消したら古い値を使い回さない
        now = [0.0]
        FakeKubernetesAPI.secrets[sa.GRAFANA_SECRET] = {sa.VIEWER_ENTRY: "old"}
        viewer = sa.ViewerCache(self.source(), ttl=2, clock=lambda: now[0])
        self.assertEqual(viewer.load(), "old")
        FakeKubernetesAPI.secrets[sa.GRAFANA_SECRET] = {sa.VIEWER_ENTRY: "new"}
        now[0] = 1.9
        self.assertEqual(viewer.load(), "old", "ttl の間は読み直さない")
        now[0] = 2.0
        self.assertEqual(viewer.load(), "new")
        del FakeKubernetesAPI.secrets[sa.GRAFANA_SECRET]
        now[0] = 4.0
        self.assertEqual(viewer.load(), "", "消した Secret の古い値を使い回した")

    def test_unreadable_viewer_value_is_empty_and_the_warning_has_no_value(self):
        bad = tempfile.NamedTemporaryFile("w", delete=False)
        bad.write("wrong")
        bad.close()
        self.addCleanup(Path(bad.name).unlink)
        FakeKubernetesAPI.secrets[sa.GRAFANA_SECRET] = {sa.VIEWER_ENTRY: "viewer-secret-value"}
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            self.assertEqual(sa.ViewerCache(self.source(bad.name), ttl=0).load(), "")
        self.assertIn("HTTPError", log.getvalue())
        self.assertNotIn("viewer-secret-value", log.getvalue())

    def test_missing_secret_is_closed(self):
        del FakeKubernetesAPI.secrets[sa.SESSION_KEY_SECRET]
        self.assertEqual(sa.CachedSource(self.source(), ttl=0).load(), ({}, b""))

    def test_api_error_is_closed(self):
        bad = tempfile.NamedTemporaryFile("w", delete=False)
        bad.write("wrong")
        bad.close()
        self.addCleanup(Path(bad.name).unlink)
        self.assertEqual(sa.CachedSource(self.source(bad.name), ttl=0).load(), ({}, b""))

    def test_unreachable_api_is_closed(self):
        self.api.shutdown()
        self.api.server_close()
        self.assertEqual(sa.CachedSource(self.source(), ttl=0).load(), ({}, b""))

    def test_secrets_created_after_start_are_picked_up_without_restart(self):
        # #64: Pod は Secret が無くても起動する (参照は optional)。auth は Secret を実行時に API から読み直すので、Secret ができれば再起動なしで効く。
        # できるまでは全部 401 (閉じる側)。反映の遅れの上限はキャッシュの ttl
        secrets = FakeKubernetesAPI.secrets
        FakeKubernetesAPI.secrets = {}
        now = [0.0]
        cache = sa.CachedSource(self.source(), ttl=2, clock=lambda: now[0])
        credentials, key = cache.load()
        self.assertEqual(sa.decide(credentials, with_basic("alice", "pw"), T0, key)[0], 401)
        FakeKubernetesAPI.secrets = secrets
        self.assertEqual(cache.load(), ({}, b""), "ttl の間は読み直さない")
        now[0] = 2.0
        credentials, key = cache.load()
        self.assertEqual(sa.decide(credentials, with_basic("alice", "pw"), T0, key)[0], 200)

    def test_only_one_of_the_two_secrets_is_still_closed(self):
        # 署名鍵だけ・資格情報だけが先にできても、1 つでも無ければ全部 401 (cookie も Basic も通さない)
        for missing in (sa.CREDENTIALS_SECRET, sa.SESSION_KEY_SECRET):
            del FakeKubernetesAPI.secrets[missing]
            self.assertEqual(sa.CachedSource(self.source(), ttl=0).load(), ({}, b""), missing)
            FakeKubernetesAPI.secrets[missing] = {"alice": json.dumps(entry("pw"))} if missing == sa.CREDENTIALS_SECRET else {"key": "k"}


class Server(unittest.TestCase):
    """forward_auth が見る応答 (状態コードと Set-Cookie・WWW-Authenticate) を、実際の HTTP で確かめる。"""

    def setUp(self):
        stderr = contextlib.redirect_stderr(io.StringIO())
        stderr.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)
        self.state = {"credentials": {"alice": entry("pw", expires_at=4_000_000_000)}, "session_key": "k"}
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        self.addCleanup(Path(f.name).unlink)
        self.path = f.name
        f.close()
        self.write()
        cache = sa.CachedSource(sa.FileSource(self.path), ttl=0)
        self.verifier = sa.VerifyCache(ttl=30)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), sa.make_handler(cache, self.verifier))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def write(self):
        Path(self.path).write_text(json.dumps(self.state), encoding="utf-8")

    def get(self, headers=None, path="/", method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.httpd.server_port}{path}", headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.headers
        except urllib.error.HTTPError as e:
            e.close()
            return e.code, e.headers

    def test_401_asks_for_basic(self):
        status, headers = self.get()
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])

    def test_200_hands_back_cookie_that_works(self):
        status, headers = self.get({"Authorization": basic("alice", "pw")})
        self.assertEqual(status, 200)
        status, _ = self.get({"Cookie": cookie_from({"Set-Cookie": headers["Set-Cookie"]})}, path="/api/anything")
        self.assertEqual(status, 200)

    def test_every_method_and_path_is_decided_the_same(self):
        for method in ["GET", "HEAD", "POST", "PUT", "DELETE"]:
            data = b"" if method in ("POST", "PUT") else None
            req = urllib.request.Request(f"http://127.0.0.1:{self.httpd.server_port}/health", method=method, data=data)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            ctx.exception.close()
            self.assertEqual(ctx.exception.code, 401, method)

    def test_delete_takes_effect_on_the_next_request(self):
        request = {"Authorization": basic("alice", "pw")}
        self.assertEqual(self.get(request)[0], 200)
        self.state["credentials"] = {}
        self.write()
        self.assertEqual(self.get(request)[0], 401)

    def test_delete_and_rotate_win_over_remembered_result(self):
        request = {"Authorization": basic("alice", "pw")}
        self.assertEqual(self.get(request)[0], 200)
        self.assertEqual(self.get(request)[0], 200)  # 2 回目は覚えた結果
        self.state["credentials"]["alice"] = entry("pw-2", expires_at=4_000_000_000)
        self.write()
        self.assertEqual(self.get(request)[0], 401)
        self.assertEqual(self.get({"Authorization": basic("alice", "pw-2")})[0], 200)
        self.state["credentials"] = {}
        self.write()
        self.assertEqual(self.get({"Authorization": basic("alice", "pw-2")})[0], 401)

    def test_source_unreadable_is_401(self):
        request = {"Authorization": basic("alice", "pw")}
        self.assertEqual(self.get(request)[0], 200)
        Path(self.path).write_text("{broken", encoding="utf-8")
        self.assertEqual(self.get(request)[0], 401)

    def test_secret_created_later_is_picked_up_by_the_running_server(self):
        # #64: Secret が無いまま起動した認証サービス (全部 401) が、Secret ができたら、再起動なしで次の要求から通す
        request = {"Authorization": basic("alice", "pw")}
        Path(self.path).unlink()
        self.assertEqual(self.get(request)[0], 401)
        self.assertEqual(self.get()[0], 401)
        self.write()
        self.assertEqual(self.get(request)[0], 200)


class GrafanaKey(unittest.TestCase):
    """#58: Grafana に渡す Basic の値の組み立てと、200 の判定への足し方 (I/O なし)。"""

    def test_basic_value_is_viewer_and_the_password(self):
        self.assertEqual(sa.grafana_basic("pw"), "Basic " + base64.b64encode(b"viewer:pw").decode())
        for password in ("pw 'with' %s $x", "a:b:c", "日本語"):
            value = sa.grafana_basic(password)
            self.assertEqual(base64.b64decode(value[len("Basic "):]).decode(), f"viewer:{password}")
            self.assertNotIn("\n", value)

    def test_no_password_gives_no_value(self):
        # base64("viewer:") は空でない。パスワードが無いのに Basic を作らない
        for password in ("", None, 0, b"pw", ["pw"]):
            self.assertEqual(sa.grafana_basic(password), "", password)

    def test_a_granted_200_carries_the_key_and_keeps_the_cookie(self):
        status, headers = sa.grant_grafana(200, {"Set-Cookie": "c=1"}, "pw")
        self.assertEqual(status, 200)
        self.assertEqual(headers, {"Set-Cookie": "c=1", sa.GRAFANA_HEADER: sa.grafana_basic("pw")})

    def test_a_200_without_a_password_becomes_503_without_cookie_or_key(self):
        for password in ("", None):
            self.assertEqual(sa.grant_grafana(200, {"Set-Cookie": "c=1"}, password), (503, {}))

    def test_401_is_left_alone(self):
        # 認証が通らない要求は、パスワードの有無によらず同じ (Secret の有無を探らせない)
        for password in ("", "pw"):
            self.assertEqual(sa.grant_grafana(401, {}, password), (401, {}))


class GrafanaServer(unittest.TestCase):
    """#58: caddy の Grafana の経路が付ける X-Share-Target: grafana に、認証サービスが viewer の Basic で答える (HTTP)。"""

    def setUp(self):
        self.stderr = io.StringIO()
        redirect = contextlib.redirect_stderr(self.stderr)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        f.close()
        self.addCleanup(Path(f.name).unlink)
        self.path = f.name
        self.state = {"credentials": {"alice": entry("pw")}, "session_key": KEY.decode(), "viewer_value": "viewer-pw"}
        self.write()
        source = sa.FileSource(self.path)
        handler = sa.make_handler(sa.CachedSource(source, ttl=0), sa.VerifyCache(ttl=30), sa.ViewerCache(source, ttl=0))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def write(self):
        Path(self.path).write_text(json.dumps(self.state), encoding="utf-8")

    def get(self, headers=None, target=sa.TARGET_GRAFANA):
        headers = {"Authorization": basic("alice", "pw")} if headers is None else dict(headers)
        if target is not None:
            headers[sa.TARGET_HEADER] = target
        req = urllib.request.Request(f"http://127.0.0.1:{self.httpd.server_port}/", headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.headers
        except urllib.error.HTTPError as e:
            e.close()
            return e.code, e.headers

    def test_grafana_target_gets_the_viewer_basic(self):
        status, headers = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(headers[sa.GRAFANA_HEADER], sa.grafana_basic("viewer-pw"))
        self.assertIn("share_session=", headers["Set-Cookie"], "Basic で通ったときの cookie はそのまま付く")

    def test_cookie_authentication_gets_it_too(self):
        _, headers = self.get()
        status, headers = self.get({"Cookie": cookie_from({"Set-Cookie": headers["Set-Cookie"]})})
        self.assertEqual(status, 200)
        self.assertEqual(headers[sa.GRAFANA_HEADER], sa.grafana_basic("viewer-pw"))

    def test_other_targets_never_get_the_key(self):
        # headroom・backstage の経路 (X-Share-Target が違う・無い) には、viewer の Basic を返さない
        for target in ("headroom", "backstage", "GRAFANA", "", None):
            status, headers = self.get(target=target)
            self.assertEqual(status, 200, target)
            self.assertIsNone(headers.get(sa.GRAFANA_HEADER), target)

    def test_a_changed_password_is_returned_from_the_next_request(self):
        self.assertEqual(self.get()[1][sa.GRAFANA_HEADER], sa.grafana_basic("viewer-pw"))
        self.state["viewer_value"] = "viewer-pw-2"
        self.write()
        self.assertEqual(self.get()[1][sa.GRAFANA_HEADER], sa.grafana_basic("viewer-pw-2"))

    def test_no_password_is_503_for_grafana_only(self):
        for how in ("missing", "empty"):
            if how == "missing":
                del self.state["viewer_value"]
            else:
                self.state["viewer_value"] = ""
            self.write()
            status, headers = self.get()
            self.assertEqual(status, 503, how)
            self.assertIsNone(headers.get(sa.GRAFANA_HEADER), how)
            self.assertIsNone(headers.get("Set-Cookie"), how)
            self.assertEqual(self.get(target="headroom")[0], 200, how)
            # 認証が通らない要求は 401 のまま (Secret の有無を探らせない)
            self.assertEqual(self.get({"Authorization": basic("alice", "wrong")})[0], 401, how)
            self.assertEqual(self.get({})[0], 401, how)
        self.state["viewer_value"] = "back"
        self.write()
        self.assertEqual(self.get()[0], 200)

    def test_an_unreadable_file_is_closed_and_the_log_has_no_secret(self):
        self.assertEqual(self.get()[0], 200)
        Path(self.path).write_text("{broken", encoding="utf-8")
        self.assertEqual(self.get()[0], 401)
        self.state["viewer_value"] = "viewer-pw"
        self.write()
        self.assertEqual(self.get()[0], 200)
        log = self.stderr.getvalue()
        self.assertIn("読めない", log)
        for secret in ("viewer-pw", sa.grafana_basic("viewer-pw"), sa.grafana_basic("viewer-pw")[len("Basic "):]):
            self.assertNotIn(secret, log)

    def test_a_handler_without_a_viewer_source_denies_grafana(self):
        source = sa.FileSource(self.path)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), sa.make_handler(sa.CachedSource(source, ttl=0)))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}/",
                                     headers={"Authorization": basic("alice", "pw"), sa.TARGET_HEADER: sa.TARGET_GRAFANA})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        ctx.exception.close()
        self.assertEqual(ctx.exception.code, 503)


if __name__ == "__main__":
    unittest.main()
