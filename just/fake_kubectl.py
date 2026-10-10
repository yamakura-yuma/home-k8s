#!/usr/bin/env python3
"""試験用の偽の kubectl (標準ライブラリだけ)。PATH の先頭に `kubectl` として置き、実物のクラスタには触れない。

呼ばれた引数を FAKE_KUBECTL_LOG に 1 行ずつ書き、標準入力で受けた manifest を FAKE_KUBECTL_STDIN に JSON の 1 行で書く
({"verb": "replace"|"create"|"apply", "manifest": {...}})。実物と同じ形で返すのは、試験が使う次の呼び方だけ:
  get secret <名前>                                   FAKE_EXISTING (空白区切りの名前) にあれば 0、無ければ 1
  create secret generic <名前> --from-literal/--from-file/--from-env-file ... --dry-run=client -o yaml
                                                       data を base64 にした Secret の manifest を JSON で出す
  create secret generic <名前> ... (--dry-run 無し)     作った Secret を FAKE_KUBECTL_STDIN に {"verb": "create-secret", ...} で書く
  create namespace <名前> --dry-run=client -o yaml     Namespace の manifest を出す
  replace -f - / create -f - / apply -f -              標準入力を FAKE_KUBECTL_STDIN に書く
  logs deploy/share -c <コンテナ>                       FAKE_LOGS/<コンテナ> の中身を出す (無ければ 1)
  delete pod -l app=share --wait=false                 何もせず 0 で終わる (呼ばれたことは FAKE_KUBECTL_LOG に残る。#64 の Pod の作り直し)
  get secret share-credentials -o go-template=...      FAKE_CREDENTIALS (JSON のファイル {名前: 項目の JSON 文字列}) を "名前<TAB>項目" の行で出す。ファイルが無ければ 1 (Secret が無い)
  patch secret share-credentials --type merge --patch-file F
                                                       {"data": {名前: base64 | null}} を FAKE_CREDENTIALS に反映し、FAKE_KUBECTL_STDIN に {"verb": "patch", "manifest": <patch>} で書く
  get secret argocd-initial-admin-secret -o jsonpath=...  FAKE_ARGOCD_ADMIN_PASSWORD を base64 にして出す (argocd-secrets.sh)
  exec -i deploy/argocd-server -- sh -c <スクリプト>       FAKE_ARGOCD_EXECS (回数のファイル) が FAKE_ARGOCD_FAILURES より小さい間は 1 で落ち、それ以降は手元の sh で同じスクリプトを動かす
                                                       (PATH の先頭の偽の argocd を呼ぶ。標準入力はそのまま渡る)
  exec -i deploy/share -c auth -- python -B -c <コード>  実物の share_auth を FAKE_AUTH_DIR から読んで、手元の python3 で同じコードを動かす (標準入力はそのまま渡る)
  label --local -f - <キー>=<値> -o yaml              標準入力の manifest (JSON) にラベルを足して出す (oidc-secrets.sh)
  get configmap coredns -o jsonpath={.data.Corefile}  FAKE_COREFILE の中身を末尾の改行を除いて出す (実物の jsonpath と同じ)
  patch configmap coredns --type merge --patch-file F  F の中身 (YAML) をそのまま FAKE_KUBECTL_STDIN に {"verb": "patch-configmap", "patch": <文字列>} で書く
"""
import base64
import json
import os
import subprocess
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


def build_manifest(name):
    data = {}
    for arg in args:
        if arg.startswith("--from-literal="):
            key, _, value = arg[len("--from-literal="):].partition("=")
            data[key] = base64.b64encode(value.encode()).decode()
        elif arg.startswith("--from-env-file="):
            with open(arg[len("--from-env-file="):], encoding="utf-8") as f:
                for env_line in f:
                    key, _, value = env_line.rstrip("\n").partition("=")
                    data[key] = base64.b64encode(value.encode()).decode()
        elif arg.startswith("--from-file="):
            key, _, path = arg[len("--from-file="):].partition("=")
            with open(path, "rb") as f:
                data[key] = base64.b64encode(f.read()).decode()
    manifest = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name}, "data": data}
    if namespace:
        manifest["metadata"]["namespace"] = namespace
    return manifest


def credentials_path():
    return os.environ.get("FAKE_CREDENTIALS", "")


dry_run = any(a.startswith("--dry-run") for a in args)
if args[:3] == ["get", "secret", "share-credentials"] and "-o" in args:
    path = credentials_path()
    if not path or not os.path.isfile(path):
        print('Error from server (NotFound): secrets "share-credentials" not found', file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        for name, entry in sorted(json.load(f).items()):
            print(f"{name}\t{entry}")
elif args[:3] == ["patch", "secret", "share-credentials"]:
    with open(args[args.index("--patch-file") + 1], encoding="utf-8") as f:
        patch = json.load(f)
    record({"verb": "patch", "manifest": patch})
    path = credentials_path()
    state = {}
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    for name, value in patch["data"].items():
        if value is None:
            state.pop(name, None)
        else:
            state[name] = base64.b64decode(value).decode()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)
elif args[:3] == ["get", "secret", "argocd-initial-admin-secret"] and "-o" in args:
    sys.stdout.write(base64.b64encode(os.environ["FAKE_ARGOCD_ADMIN_PASSWORD"].encode()).decode())
elif args[:1] == ["exec"] and "deploy/argocd-server" in args:
    counter = os.environ["FAKE_ARGOCD_EXECS"]
    done = int(open(counter).read()) if os.path.isfile(counter) else 0
    with open(counter, "w", encoding="utf-8") as f:
        f.write(str(done + 1))
    if done < int(os.environ.get("FAKE_ARGOCD_FAILURES", "0")):
        print("error: account 'backstage' does not exist", file=sys.stderr)
        sys.exit(1)
    sys.exit(subprocess.run(["sh", "-c", args[args.index("-c") + 1]]).returncode)
elif args[:1] == ["exec"] and "--" in args:
    code = args[args.index("-c", args.index("--")) + 1]
    sys.exit(subprocess.run([sys.executable, "-B", "-c", code], env={**os.environ, "PYTHONPATH": os.environ["FAKE_AUTH_DIR"]}).returncode)
elif args[:2] == ["label", "--local"]:
    manifest = json.loads(sys.stdin.read())
    for arg in args:
        if "=" in arg and not arg.startswith("-"):
            key, _, value = arg.partition("=")
            manifest["metadata"].setdefault("labels", {})[key] = value
    print(json.dumps(manifest))
elif args[:3] == ["get", "configmap", "coredns"]:
    with open(os.environ["FAKE_COREFILE"], encoding="utf-8") as f:
        sys.stdout.write(f.read().rstrip("\n"))
elif args[:3] == ["patch", "configmap", "coredns"]:
    with open(args[args.index("--patch-file") + 1], encoding="utf-8") as f:
        record({"verb": "patch-configmap", "patch": f.read()})
elif args[:2] == ["get", "secret"]:
    sys.exit(0 if args[2] in os.environ.get("FAKE_EXISTING", "").split() else 1)
elif args[:3] == ["create", "secret", "generic"]:
    manifest = build_manifest(args[3])
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
