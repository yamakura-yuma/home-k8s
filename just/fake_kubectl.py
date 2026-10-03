#!/usr/bin/env python3
"""試験用の偽の kubectl (標準ライブラリだけ)。PATH の先頭に `kubectl` として置き、実物のクラスタには触れない。

呼ばれた引数を FAKE_KUBECTL_LOG に 1 行ずつ書き、標準入力で受けた manifest を FAKE_KUBECTL_STDIN に JSON の 1 行で書く
({"verb": "replace"|"create"|"apply", "manifest": {...}})。実物と同じ形で返すのは、試験が使う次の呼び方だけ:
  get secret <名前>                                   FAKE_EXISTING (空白区切りの名前) にあれば 0、無ければ 1
  create secret generic <名前> --from-literal/--from-file ... --dry-run=client -o yaml
                                                       data を base64 にした Secret の manifest を JSON で出す
  create secret generic <名前> ... (--dry-run 無し)     作った Secret を FAKE_KUBECTL_STDIN に {"verb": "create-secret", ...} で書く
  create namespace <名前> --dry-run=client -o yaml     Namespace の manifest を出す
  replace -f - / create -f - / apply -f -              標準入力を FAKE_KUBECTL_STDIN に書く
  logs deploy/share -c <コンテナ>                       FAKE_LOGS/<コンテナ> の中身を出す (無ければ 1)
"""
import base64
import json
import os
import sys

argv = sys.argv[1:]
with open(os.environ["FAKE_KUBECTL_LOG"], "a", encoding="utf-8") as f:
    f.write("kubectl " + " ".join(argv) + "\n")


def record(entry):
    with open(os.environ["FAKE_KUBECTL_STDIN"], "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# --context X と -n X は位置に関わらず取り除いて、動詞だけを見る
args, namespace, i = [], None, 0
while i < len(argv):
    if argv[i] in ("--context", "-n"):
        if argv[i] == "-n":
            namespace = argv[i + 1]
        i += 2
        continue
    args.append(argv[i])
    i += 1


def secret_manifest(name):
    data = {}
    for arg in args:
        if arg.startswith("--from-literal="):
            key, _, value = arg[len("--from-literal="):].partition("=")
            data[key] = base64.b64encode(value.encode()).decode()
        elif arg.startswith("--from-file="):
            key, _, path = arg[len("--from-file="):].partition("=")
            with open(path, "rb") as f:
                data[key] = base64.b64encode(f.read()).decode()
    manifest = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name}, "data": data}
    if namespace:
        manifest["metadata"]["namespace"] = namespace
    return manifest


dry_run = any(a.startswith("--dry-run") for a in args)
if args[:2] == ["get", "secret"]:
    sys.exit(0 if args[2] in os.environ.get("FAKE_EXISTING", "").split() else 1)
elif args[:3] == ["create", "secret", "generic"]:
    manifest = secret_manifest(args[3])
    if dry_run:
        print(json.dumps(manifest))
    else:
        record({"verb": "create-secret", "manifest": manifest})
elif args[:2] == ["create", "namespace"]:
    print(json.dumps({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": args[2]}}))
elif args[:1] in (["replace"], ["create"], ["apply"]) and "-f" in args:
    record({"verb": args[0], "manifest": json.loads(sys.stdin.read())})
elif args[:1] == ["logs"]:
    path = os.path.join(os.environ.get("FAKE_LOGS", ""), args[args.index("-c") + 1])
    if not os.path.isfile(path):
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        sys.stdout.write(f.read())
