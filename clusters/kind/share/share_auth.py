"""share の認証サービス。caddy の forward_auth から要求ごとに呼ばれ、200 か 401 を返す (Grafana の経路で viewer のパスワードを読めなければ 503)。

標準ライブラリだけ (ConfigMap に置いて python:3.13-alpine で動かす)。判定は純関数 decide()。
資格情報は Secret share-credentials (キー=名前、値=JSON
{"hash": "pbkdf2_sha256$<反復回数>$<salt の hex>$<鍵の hex>", "expires_at": epoch 秒|null, "privileged": bool,
"created_at": epoch 秒})、cookie の署名鍵は Secret share-session-key (キー key)。設計は docs/cluster/share.md。

Grafana の経路 (caddy が X-Share-Target: grafana を付けて聞く) は、認証が通ったとき、Secret share-grafana (キー viewer-password) を
実行時に読んで Basic の値 (viewer:<パスワード>) を組み、応答の X-Share-Grafana-Authorization に載せる (#58)。caddy がそれを Grafana への要求の
Authorization にする (人の Authorization は渡さない)。パスワードを変えても share を作り直さず、次に読み直した要求から新しい値になる。

閉じる側に倒す: Secret が無い・K8s API に届かない・JSON が壊れている・名前が無い・期限切れは、すべて 401。
認証が通っても、Grafana の経路で share-grafana が読めない・空なら 503 (Basic を渡せないので通さない)。headroom・backstage の経路には関わらない。
Basic の値もパスワードも、ログには出さない。

環境変数:
  AUTH_SOURCE      既定は K8s API から Secret を読む。file:<path> なら JSON ファイルを読む (試験用)
  AUTH_CACHE_TTL   Secret を読み直す間隔 (秒、既定 2。0 なら毎回読む)。delete・rotate・viewer のパスワードの変更の反映の遅れの上限
  AUTH_VERIFY_TTL  Basic の照合に成功した結果を覚える秒数 (既定 30。0 なら毎回 PBKDF2 を計算する)
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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CREDENTIALS_SECRET = "share-credentials"
SESSION_KEY_SECRET = "share-session-key"
GRAFANA_SECRET = "share-grafana"  # キー viewer-password。just up の _share-secrets が grafana-viewer の写しを入れる
VIEWER_ENTRY = "viewer-password"
VIEWER_USER = "viewer"
# caddy が認証サービスへの問い合わせに付ける (header_up は人の値を置き換える)。どの対象の経路か。grafana のときだけ Basic の値を返す
TARGET_HEADER = "X-Share-Target"
TARGET_GRAFANA = "grafana"
# 認証サービスが 200 の応答に載せる、Grafana への Authorization の値。caddy が要求に写し、Grafana に渡す
GRAFANA_HEADER = "X-Share-Grafana-Authorization"
COOKIE = "share_session"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# pbkdf2_sha256$<反復回数>$<salt (16 byte) の hex>$<鍵 (32 byte) の hex>
HASH_RE = re.compile(r"pbkdf2_sha256\$([0-9]{1,9})\$([0-9a-f]{32})\$([0-9a-f]{64})")
# OWASP Password Storage Cheat Sheet の PBKDF2-HMAC-SHA256 の値。反復回数は項目ごとに持つので、将来上げても古い項目は読める
ITERATIONS = 600_000
MIN_ITERATIONS = 100_000  # これを下回る項目は、弱く書き換えられたものとして拒否する
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
# 名前が無いときも同じ量の計算をする (名前の有無を応答の時間から探られないように)
DUMMY_HASH = f"pbkdf2_sha256${ITERATIONS}${'0' * 32}${'0' * 64}"
# 照合に成功した結果を覚える秒数。delete・期限切れ・ローテーションは覚えた結果より先に確かめるので効き続ける
VERIFY_TTL = 30.0


def hash_password(password, iterations=ITERATIONS, salt=None):
    """PBKDF2-HMAC-SHA256 (ソルト付き)。Secret に置く "pbkdf2_sha256$..." の文字列を返す。"""
    salt = os.urandom(16) if salt is None else salt
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations, 32)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${key.hex()}"


def verify_password(password, stored):
    """stored (hash_password の形) とパスワードが合うか。形が違う・反復回数が少ないなら False。"""
    match = HASH_RE.fullmatch(stored)
    if match is None or int(match[1]) < MIN_ITERATIONS:
        return False
    salt = bytes.fromhex(match[2])
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(match[1]), 32)
    return hmac.compare_digest(key.hex(), match[3])


class VerifyCache:
    """Basic の照合に成功した結果を ttl 秒だけ覚え、PBKDF2 (約 70ms) を要求のたびに計算しないようにする。

    名前 -> (hash, パスワード, 覚えた時刻)。成功だけを覚える (失敗は毎回計算する)。覚えるのは名前ごとに 1 つなので、
    大きさは名前の数で止まる (delete した名前の分は再起動まで残るが、hash が合わないので使われない)。hash が変われば (ローテーション・delete→add) 一致しないので使わない。
    パスワードはプロセスのメモリに最大 ttl 秒だけ平文で残る (ハッシュ化はしない)。
    """

    def __init__(self, ttl=VERIFY_TTL, clock=time.monotonic):
        self.ttl = ttl
        self.clock = clock
        self.lock = threading.Lock()
        self.entries = {}

    def __call__(self, name, password, stored):
        now = self.clock()
        with self.lock:
            hit = self.entries.get(name)
        if hit is not None and hit[0] == stored and now - hit[2] < self.ttl and hmac.compare_digest(hit[1].encode(), password.encode()):
            return True
        ok = verify_password(password, stored)
        if ok and self.ttl > 0:
            with self.lock:
                self.entries[name] = (stored, password, now)
        return ok


def verify_uncached(name, password, stored):
    return verify_password(password, stored)


def parse_entry(raw):
    """Secret の 1 項目 (JSON の文字列か dict) を検証する。1 つでも欠ける・型が違うなら None。"""
    try:
        entry = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        expires_at = entry["expires_at"]
        match = HASH_RE.fullmatch(entry["hash"]) if isinstance(entry["hash"], str) else None
        if match is None or int(match[1]) < MIN_ITERATIONS:
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
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return None
    entry = parse_entry(credentials.get(name))
    if entry is None:
        return None
    if entry["expires_at"] is not None and not now < entry["expires_at"]:
        return None
    return entry


def sign(session_key, name, entry):
    # cookie の署名は MAC (HMAC-SHA256) で、パスワードのハッシュ化ではない。パスワードもその派生物も入れない。
    # 束ねるのは項目の salt (秘密ではない世代の印) だけ。salt は hash_password のたびに変わるので、
    # rotate・delete→add の後は古い cookie も効かなくなる
    salt = HASH_RE.fullmatch(entry["hash"])[2]
    message = f"{name}:{salt}".encode()
    return hmac.new(session_key, message, hashlib.sha256).hexdigest()


def decide(credentials, request, now, session_key=b"", verifier=verify_uncached):
    """(status, headers) を返す。I/O はしない。

    credentials: 名前 -> 項目 (JSON 文字列か dict)。request: {"authorization": ..., "cookie": ...} (無ければ省く)。
    now: epoch 秒。session_key: cookie の署名鍵 (空なら cookie は受けない)。
    verifier(名前, パスワード, 保存された hash): パスワードの照合。既定は毎回 PBKDF2 を計算し、サーバーは VerifyCache を渡す。
    Basic が付いていれば Basic だけで決める。それ以外 (Backstage の Bearer など) は cookie で決める。
    200 のときは、Basic で通った場合に限り署名つき cookie の Set-Cookie を返す。
    """
    try:
        authorization = request.get("authorization") or ""
        if authorization[:6].lower() == "basic ":
            return _basic(credentials, authorization[6:].strip(), now, session_key, verifier)
        return _cookie(credentials, request.get("cookie") or "", now, session_key)
    except Exception:  # 何が起きても通さない
        return 401, {}


def _basic(credentials, token, now, session_key, verifier):
    name, _, password = base64.b64decode(token, validate=True).decode().partition(":")
    entry = active_entry(credentials, name, now)  # 期限・削除は、覚えた照合結果より先に毎回確かめる
    if entry is None:
        verify_password(password, DUMMY_HASH)  # 名前が無くても同じ量の計算をする
        return 401, {}
    if not verifier(name, password, entry["hash"]):
        return 401, {}
    headers = {}
    if session_key:
        # cookie には、要求の文字列ではなく Secret に保存されている名前を使う (active_entry で一致を確かめ済みなので同じ値)。
        # Authorization は「名前:パスワード」の 1 つの値なので、要求から取った name を使うと、静的解析 (CodeQL) が
        # パスワードを署名や Set-Cookie に流していると誤検知する
        stored_name = next(key for key in credentials if key == name)
        value = f"{urllib.parse.quote(stored_name, safe='')}.{sign(session_key, stored_name, entry)}"
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


def grafana_basic(viewer_value):
    """Grafana に渡す Authorization の値 (viewer の Basic)。パスワードが空なら空文字 (渡せない)。"""
    if not isinstance(viewer_value, str) or not viewer_value:
        return ""
    return "Basic " + base64.b64encode(f"{VIEWER_USER}:{viewer_value}".encode()).decode()


def grant_grafana(status, headers, viewer_value):
    """Grafana の経路で 200 になった判定に、viewer の Basic を足す。パスワードが無い・空なら 503 (Cookie も付けず、通さない)。

    200 以外 (401) はそのまま返す。認証が通らなかった要求に、Secret の有無を応答の違いで探らせない。
    """
    if status != 200:
        return status, headers
    value = grafana_basic(viewer_value)
    if not value:
        return 503, {}
    return status, {**headers, GRAFANA_HEADER: value}


def decode_secret_data(data):
    """Secret の .data (値は base64) を、値が文字列の dict にする。"""
    return {key: base64.b64decode(value, validate=True).decode() for key, value in (data or {}).items()}


class FileSource:
    """AUTH_SOURCE=file:<path>。{"credentials": {名前: 項目}, "session_key": "...", "viewer_value": "..."} の JSON。試験用。

    viewer_value (Secret share-grafana の viewer-password の写し) は省ける (Secret が無い状態)。
    """

    def __init__(self, path):
        self.path = path

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def load(self):
        data = self._read()
        return data["credentials"], data["session_key"].encode()

    def load_viewer_value(self):
        return self._read().get("viewer_value", "")


class KubernetesSource:
    """K8s API から Secret を読む。ServiceAccount のトークンで認証する (RBAC は resourceNames で 3 つの get だけ)。"""

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

    def load_viewer_value(self):
        return self._get(GRAFANA_SECRET)[VIEWER_ENTRY]


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


class ViewerCache:
    """source.load_viewer_value() を ttl 秒だけ覚える。読めなかったら空 (= Grafana の経路を拒否)。古い値は使い回さない。

    資格情報 (CachedSource) とは別に読むので、share-grafana が無くても資格情報は読め、headroom・backstage の経路は動く。
    CachedSource と同じ振る舞いだが、クラスを分ける: 同じクラスだと、静的解析 (CodeQL) が viewer のパスワードを
    資格情報の流れ (cookie の HMAC) に混ぜて見る (py/weak-sensitive-data-hashing の誤検知)。警告には例外の型だけを書く (値は出さない)。
    """

    def __init__(self, source, ttl, clock=time.monotonic):
        self.source = source
        self.ttl = ttl
        self.clock = clock
        self.fetched_at = None
        self.latest = ""

    def load(self):
        now = self.clock()
        if self.fetched_at is None or not now - self.fetched_at < self.ttl:
            try:
                self.latest = self.source.load_viewer_value()
            except Exception as e:
                print(f"viewer のパスワードを読めない (Grafana の経路を拒否する): {type(e).__name__}", file=sys.stderr, flush=True)
                self.latest = ""
            self.fetched_at = now
        return self.latest


def make_handler(cache, verifier=verify_uncached, viewer=None):
    """viewer: viewer のパスワードの ViewerCache。無ければ Grafana の経路は 503 になる。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            credentials, session_key = cache.load()
            request = {"authorization": self.headers.get("Authorization"), "cookie": self.headers.get("Cookie")}
            status, headers = decide(credentials, request, time.time(), session_key, verifier)
            if self.headers.get(TARGET_HEADER) == TARGET_GRAFANA:
                status, headers = grant_grafana(status, headers, viewer.load() if viewer else "")
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
    source = source_from_env(env)
    ttl = float(env.get("AUTH_CACHE_TTL", "2"))
    cache = CachedSource(source, ttl)
    viewer = ViewerCache(source, ttl)
    verifier = VerifyCache(float(env.get("AUTH_VERIFY_TTL", VERIFY_TTL)))
    host, _, port = env.get("AUTH_LISTEN", "127.0.0.1:9000").rpartition(":")
    ThreadingHTTPServer((host, int(port)), make_handler(cache, verifier, viewer)).serve_forever()


if __name__ == "__main__":
    serve()
