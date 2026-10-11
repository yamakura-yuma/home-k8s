"""ゲスト (realm home-k8s のグループ guests) のうち、期限を過ぎた人を消す。CronJob guest-expiry が 5 分ごとに流す (cronjob.yaml)。

期限はユーザーの属性 guest-expires-at (UTC の ISO 8601、例 2026-10-12T18:00:00Z)。just guest add が「発行した日の次の 3:00 JST」を書く
(just/guest.sh)。この処理は「今の時刻で期限を過ぎた guests を消す」だけで、何度流しても結果は同じ。3:00 に PC が止まっていても、
クラスタが戻った後の最初の実行で消える (docs/cluster/guest-access.md の「期限」)。

- 期限を過ぎた人、属性が無いか読めない人は期限切れとみなす (期限の無いゲストを残さない)
- ユーザー名が guest- で始まる人は、Keycloak のセッションを切ってからユーザーごと消す
- guest- で始まらない人 (Admin Console で手で guests に入れた人など) は消さず、guests から外すだけにする (取り違えて人のユーザーを消さない)
- 標準ライブラリだけ。Keycloak の Admin REST API を、realm master の管理者 (Secret keycloak-bootstrap-admin) で呼ぶ
- 出力はユーザー名と件数だけ (トークン・パスワードは出さない)
"""
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REALM = "home-k8s"
GROUP = "guests"
ATTRIBUTE = "guest-expires-at"
PREFIX = "guest-"


class Keycloak:
    def __init__(self, base, username, password):
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.token = None

    def login(self):
        body = urllib.parse.urlencode({"grant_type": "password", "client_id": "admin-cli",
                                       "username": self.username, "password": self.password}).encode()
        with urllib.request.urlopen(f"{self.base}/realms/master/protocol/openid-connect/token", data=body, timeout=30) as r:
            self.token = json.load(r)["access_token"]

    def call(self, method, path, query=None):
        url = f"{self.base}/admin/realms/{REALM}/{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
        req = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        return json.loads(data) if data else None


def expires_at(user):
    """属性の期限 (aware な datetime)。無い・読めないときは None。"""
    values = (user.get("attributes") or {}).get(ATTRIBUTE) or []
    try:
        at = datetime.datetime.fromisoformat(values[0].replace("Z", "+00:00"))
    except (IndexError, AttributeError, ValueError):
        return None
    return at if at.tzinfo else None


def is_expired(user, now):
    at = expires_at(user)
    return at is None or at <= now


def members(kc, gid):
    # 空のページが返るまで読む (サーバーが max より少なく返しても取りこぼさない)
    first = 0
    while True:
        page = kc.call("GET", f"groups/{gid}/members", {"first": first, "max": 100, "briefRepresentation": "false"})
        if not page:
            return
        yield from page
        first += len(page)


def sweep(kc, now):
    """期限を過ぎた guests を消す (または外す)。(消した人, 外した人, 残した人数) を返す。"""
    groups = [g for g in kc.call("GET", "groups", {"search": GROUP, "exact": "true"}) if g["name"] == GROUP]
    if not groups:
        raise SystemExit(f"realm {REALM} にグループ {GROUP} が無い")
    gid = groups[0]["id"]
    deleted, removed, kept = [], [], 0
    for user in list(members(kc, gid)):
        if not is_expired(user, now):
            kept += 1
            continue
        if user["username"].startswith(PREFIX):
            kc.call("POST", f"users/{user['id']}/logout")
            kc.call("DELETE", f"users/{user['id']}")
            deleted.append(user["username"])
        else:
            kc.call("DELETE", f"users/{user['id']}/groups/{gid}")
            removed.append(user["username"])
    return deleted, removed, kept


def main():
    kc = Keycloak(os.environ.get("KEYCLOAK_URL", "http://keycloak-service.auth.svc:8080"),
                  os.environ["KEYCLOAK_ADMIN_USERNAME"], os.environ["KEYCLOAK_ADMIN_PASSWORD"])
    # クラスタが戻った直後は Keycloak がまだ起動していないことがある。待つ上限は Job の activeDeadlineSeconds より短くする
    wait = int(os.environ.get("KEYCLOAK_WAIT_SECONDS", "180"))
    deadline = time.monotonic() + wait
    while True:
        try:
            kc.login()
            break
        except (urllib.error.URLError, OSError) as e:
            if time.monotonic() >= deadline:
                raise SystemExit(f"Keycloak に {wait} 秒つながらない: {e}")
            time.sleep(5)
    now = datetime.datetime.now(datetime.timezone.utc)
    deleted, removed, kept = sweep(kc, now)
    for name in deleted:
        print(f"消した (期限切れ): {name}")
    for name in removed:
        print(f"guests から外した (期限切れ、guest- で始まらない): {name}")
    print(f"{now.isoformat(timespec='seconds')} 消した {len(deleted)}、外した {len(removed)}、残した {kept}")


if __name__ == "__main__":
    sys.exit(main())
