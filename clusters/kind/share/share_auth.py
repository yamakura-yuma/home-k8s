"""share の認証サービス。caddy の forward_auth から要求ごとに呼ばれ、200 か 401 だけを返す。

標準ライブラリだけ (ConfigMap に置いて python:3.13-alpine で動かす)。判定は純関数 decide()。
資格情報は Secret share-credentials (キー=名前、値=JSON
{"hash": sha256(パスワード) の hex, "expires_at": epoch 秒|null, "privileged": bool, "created_at": epoch 秒})、
cookie の署名鍵は Secret share-session-key (キー key)。設計は docs/cluster/share.md。

閉じる側に倒す: Secret が無い・K8s API に届かない・JSON が壊れている・名前が無い・期限切れは、すべて 401。

環境変数:
  AUTH_SOURCE      既定は K8s API から Secret を読む。file:<path> なら JSON ファイルを読む (試験用)
  AUTH_CACHE_TTL   Secret を読み直す間隔 (秒、既定 2。0 なら毎回読む)。delete・rotate の反映の遅れの上限
  AUTH_LISTEN      待ち受け (既定 127.0.0.1:9000)
  AUTH_NAMESPACE   Secret の namespace (既定は ServiceAccount の namespace)
"""
import base64
import hashlib
import hmac
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CREDENTIALS_SECRET = "share-credentials"
SESSION_KEY_SECRET = "share-session-key"
COOKIE = "share_session"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
# 名前が無いときも同じ量の比較をする (名前の有無を応答の時間から探られないように)
DUMMY_HASH = "0" * 64


def hash_password(password):
    # パスワードは openssl rand -hex 16 (128 bit) で推測できないので、遅い KDF は要らない
    return hashlib.sha256(password.encode()).hexdigest()


def parse_entry(raw):
    """Secret の 1 項目 (JSON の文字列か dict) を検証する。1 つでも欠ける・型が違うなら None。"""
    try:
        entry = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        expires_at = entry["expires_at"]
        if not isinstance(entry["hash"], str) or not HASH_RE.match(entry["hash"]):
            return None
        if not isinstance(entry["privileged"], bool) or isinstance(entry["created_at"], bool):
            return None
        if not isinstance(entry["created_at"], (int, float)):
            return None
        # bool は int の子なので、expires_at: true を期限として通さない
        if expires_at is not None and (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))):
            return None
        return entry
    except (ValueError, KeyError, TypeError):
        return None


def active_entry(credentials, name, now):
    """名前が Secret にあり、壊れておらず、期限内の項目。それ以外は None (期限ちょうども拒否)。"""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return None
    entry = parse_entry(credentials.get(name))
    if entry is None:
        return None
    if entry["expires_at"] is not None and not now < entry["expires_at"]:
        return None
    return entry


def sign(session_key, name, entry):
    # 署名に hash を混ぜる。rotate・delete→add で hash が変わるので、古い cookie も効かなくなる
    message = f"{name}:{entry['hash']}".encode()
    return hmac.new(session_key, message, hashlib.sha256).hexdigest()


def decide(credentials, request, now, session_key=b""):
    """(status, headers) を返す。I/O はしない。

    credentials: 名前 -> 項目 (JSON 文字列か dict)。request: {"authorization": ..., "cookie": ...} (無ければ省く)。
    now: epoch 秒。session_key: cookie の署名鍵 (空なら cookie は受けない)。
    Basic が付いていれば Basic だけで決める。それ以外 (Backstage の Bearer など) は cookie で決める。
    200 のときは、Basic で通った場合に限り署名つき cookie の Set-Cookie を返す。
    """
    try:
        authorization = request.get("authorization") or ""
        if authorization[:6].lower() == "basic ":
            return _basic(credentials, authorization[6:].strip(), now, session_key)
        return _cookie(credentials, request.get("cookie") or "", now, session_key)
    except Exception:  # 何が起きても通さない
        return 401, {}


def _basic(credentials, token, now, session_key):
    name, _, password = base64.b64decode(token, validate=True).decode().partition(":")
    entry = active_entry(credentials, name, now)
    expected = entry["hash"] if entry else DUMMY_HASH
    if not hmac.compare_digest(hash_password(password), expected) or entry is None:
        return 401, {}
    headers = {}
    if session_key:
        value = f"{name}.{sign(session_key, name, entry)}"
        headers["Set-Cookie"] = f"{COOKIE}={value}; Path=/; HttpOnly; Secure; SameSite=Lax"
    return 200, headers


def _cookie(credentials, header, now, session_key):
    if not session_key:
        return 401, {}
    try:
        morsel = SimpleCookie(header).get(COOKIE)
    except CookieError:
        return 401, {}
    if morsel is None:
        return 401, {}
    name, _, signature = morsel.value.rpartition(".")
    # cookie が正しくても、名前が今も Secret にあり期限内かを毎回確かめる (delete・期限切れが cookie にも効く)
    entry = active_entry(credentials, name, now)
    expected = sign(session_key, name, entry) if entry else DUMMY_HASH
    if not hmac.compare_digest(signature, expected) or entry is None:
        return 401, {}
    return 200, {}


def decode_secret_data(data):
    """Secret の .data (値は base64) を、値が文字列の dict にする。"""
    return {key: base64.b64decode(value, validate=True).decode() for key, value in (data or {}).items()}


class FileSource:
    """AUTH_SOURCE=file:<path>。{"credentials": {名前: 項目}, "session_key": "..."} の JSON。試験用。"""

    def __init__(self, path):
        self.path = path

    def load(self):
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        return data["credentials"], data["session_key"].encode()


class KubernetesSource:
    """K8s API から Secret を読む。ServiceAccount のトークンで認証する (RBAC は resourceNames で 2 つの get だけ)。"""

    def __init__(self, base_url, token_path, ca_path, namespace):
        self.base_url = base_url
        self.token_path = token_path
        self.ca_path = ca_path
        self.namespace = namespace

    def _get(self, secret):
        with open(self.token_path, encoding="utf-8") as f:
            token = f.read().strip()
        url = f"{self.base_url}/api/v1/namespaces/{self.namespace}/secrets/{secret}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        context = ssl.create_default_context(cafile=self.ca_path) if url.startswith("https:") else None
        try:
            with urllib.request.urlopen(req, timeout=3, context=context) as resp:
                return decode_secret_data(json.load(resp).get("data"))
        except urllib.error.HTTPError as e:
            e.close()
            raise

    def load(self):
        return self._get(CREDENTIALS_SECRET), self._get(SESSION_KEY_SECRET)["key"].encode()


def source_from_env(env):
    spec = env.get("AUTH_SOURCE", "")
    if spec.startswith("file:"):
        return FileSource(spec[len("file:"):])
    with open(f"{SA_DIR}/namespace", encoding="utf-8") as f:
        default_ns = f.read().strip()
    return KubernetesSource(
        f"https://{env['KUBERNETES_SERVICE_HOST']}:{env.get('KUBERNETES_SERVICE_PORT', '443')}",
        f"{SA_DIR}/token",
        f"{SA_DIR}/ca.crt",
        env.get("AUTH_NAMESPACE", default_ns),
    )


class CachedSource:
    """source.load() を ttl 秒だけ覚える。読めなかったら空 (= 全部 401)。古い値は使い回さない。"""

    def __init__(self, source, ttl, clock=time.monotonic):
        self.source = source
        self.ttl = ttl
        self.clock = clock
        self.fetched_at = None
        self.value = ({}, b"")

    def load(self):
        now = self.clock()
        if self.fetched_at is None or not now - self.fetched_at < self.ttl:
            try:
                self.value = self.source.load()
            except Exception as e:
                print(f"資格情報を読めない (全部 401 にする): {type(e).__name__}", file=sys.stderr, flush=True)
                self.value = ({}, b"")
            self.fetched_at = now
        return self.value


def make_handler(cache):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            credentials, session_key = cache.load()
            request = {"authorization": self.headers.get("Authorization"), "cookie": self.headers.get("Cookie")}
            status, headers = decide(credentials, request, time.time(), session_key)
            self.send_response(status)
            if status == 401:
                headers = {**headers, "WWW-Authenticate": 'Basic realm="share", charset="UTF-8"'}
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET

        def log_message(self, format, *args):
            pass  # 要求行にも資格情報は無いが、判定のたびに書くほどでもない

    return Handler


def serve(env=os.environ):
    cache = CachedSource(source_from_env(env), float(env.get("AUTH_CACHE_TTL", "2")))
    host, _, port = env.get("AUTH_LISTEN", "127.0.0.1:9000").rpartition(":")
    ThreadingHTTPServer((host, int(port)), make_handler(cache)).serve_forever()


if __name__ == "__main__":
    serve()
