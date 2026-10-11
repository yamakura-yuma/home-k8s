#!/usr/bin/env python3
"""試験用の偽の kcadm.sh (標準ライブラリだけ)。偽の kubectl (fake_kubectl.py) が exec keycloak-0 のスクリプトの中の kcadm.sh をこれに置き換える。

呼ばれた引数を FAKE_KCADM_LOG に 1 行ずつ (JSON の配列で) 書き、`-f -` で受けた標準入力を FAKE_KCADM_STDIN に {"args": [...], "stdin": "..."} で書く。
realm の状態は環境変数で渡す:
  FAKE_KC_USERS      既にいるユーザー。空白区切りの <ユーザー名>=<id>:<グループ>|<グループ> (create users は id-new を返す)
  FAKE_KC_EXPIRES    ユーザーの属性 guest-expires-at。空白区切りの <id>=<値>
  FAKE_KC_POLICY     user profile の unmanagedAttributePolicy (空なら無い = Keycloak の既定の Disabled)
  FAKE_KC_DROP_ATTRIBUTES  1 なら create users の属性を保存しない (unmanaged の属性を捨てる Keycloak の既定の動き)
get users/id-new は、FAKE_KCADM_LOG に残った create users の -s attributes.guest-expires-at=... を返す
"""
import json
import os
import sys

argv = sys.argv[1:]
with open(os.environ["FAKE_KCADM_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(argv) + "\n")
if "-f" in argv and argv[argv.index("-f") + 1] == "-":
    with open(os.environ["FAKE_KCADM_STDIN"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"args": argv, "stdin": sys.stdin.read()}) + "\n")

users = {}
for entry in os.environ.get("FAKE_KC_USERS", "").split():
    name, _, rest = entry.partition("=")
    uid, _, groups = rest.partition(":")
    users[name] = (uid, groups.split("|") if groups else [])
by_id = {uid: (name, groups) for name, (uid, groups) in users.items()}
expires = dict(e.split("=", 1) for e in os.environ.get("FAKE_KC_EXPIRES", "").split())


def created_attribute():
    """これまでの create users の引数から、属性 guest-expires-at の値を取る。"""
    if os.environ.get("FAKE_KC_DROP_ATTRIBUTES") == "1":
        return None
    with open(os.environ["FAKE_KCADM_LOG"], encoding="utf-8") as f:
        for line in f:
            args = json.loads(line)
            if args[:2] == ["create", "users"]:
                for a in args:
                    if a.startswith("attributes.guest-expires-at="):
                        return json.loads(a.split("=", 1)[1])[0]
    return None


def user_json(uid, at):
    attrs = f'{{\n    "guest-expires-at" : [ "{at}" ]\n  }}' if at else "{ }"
    return f'{{\n  "id" : "{uid}",\n  "attributes" : {attrs},\n  "enabled" : true\n}}'


def query(key):
    for i, a in enumerate(argv):
        if a == "-q" and argv[i + 1].startswith(key + "="):
            return argv[i + 1].split("=", 1)[1]
    return None


verb, path = argv[0], argv[1]
if verb == "config":
    pass
elif (verb, path) == ("get", "users/profile"):
    policy = os.environ.get("FAKE_KC_POLICY", "")
    print('{\n  "attributes" : [ ]' + (f',\n  "unmanagedAttributePolicy" : "{policy}"' if policy else "") + "\n}")
elif (verb, path) == ("get", "users"):
    print(users.get(query("username"), ("",))[0])
elif (verb, path) == ("create", "users"):
    print("id-new")
elif (verb, path) == ("get", "groups"):
    print("gid-guests")
elif verb == "get" and path == "groups/gid-guests/members":
    for name, (uid, groups) in users.items():
        if "guests" in groups:
            print(f"{uid},{name},true")
elif verb == "get" and path.startswith("users/") and path.endswith("/groups"):
    for g in by_id.get(path.split("/")[1], ("", []))[1]:
        print(g)
elif verb == "get" and path == "users/id-new":
    print(user_json("id-new", created_attribute()))
elif verb == "get" and path.startswith("users/") and path.count("/") == 1:
    print(user_json(path.split("/")[1], expires.get(path.split("/")[1])))
