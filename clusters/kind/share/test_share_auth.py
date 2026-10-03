"""share の認証サービス (share_auth.py) のテスト。拒否を確かめるのが主で、既定は 401。

標準ライブラリだけ: python3 -B -m unittest discover -s clusters/kind/share
時刻は decide() の引数なので、期限の前後を実時間を待たずに確かめられる。
"""
import base64
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
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


def entry(password, expires_at=None, privileged=False):
    return {"hash": sa.hash_password(password), "expires_at": expires_at, "privileged": privileged, "created_at": T0}


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
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), sa.make_handler(cache))
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

    def test_source_unreadable_is_401(self):
        request = {"Authorization": basic("alice", "pw")}
        self.assertEqual(self.get(request)[0], 200)
        Path(self.path).write_text("{broken", encoding="utf-8")
        self.assertEqual(self.get(request)[0], 401)


if __name__ == "__main__":
    unittest.main()
