#!/usr/bin/env bash
# just up の一部。ArgoCD の読み取り専用アカウント backstage (clusters/kind/argocd/values.yaml の accounts.backstage) の
# API トークンを (無ければ・今の ArgoCD で通らなければ作って) Git の外のファイルに置き、
# Backstage に Secret backstage-argocd (namespace backstage、キー ARGOCD_AUTH_TOKEN) で渡す。
# Backstage のプロキシ (app-config.yaml の /argocd/api) が Authorization: Bearer に入れる。ブラウザには渡らない。
# トークンは admin のセッションでしか作れないので、argocd-server の Pod の中の argocd CLI で作る
# (admin のパスワードは argocd-initial-admin-secret から。kubectl exec の標準入力で渡し、引数や ps に出さない)。
# 引数: <トークンのファイル> <リポジトリの所有者を調べるディレクトリ> <kube context>
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
. "$script_dir/secret-lib.sh"
token_file="$1"
owner_ref="$2"
ctx="$3"
argocd_ns="${ARGOCD_NAMESPACE:-argocd}"
# 開発用コンテナでは ~/.local/share/home-k8s をホストと共有していないと、
# トークンがコンテナの中にだけ残る。古いコンテナなら作り直してもらう
if [ -f /.dockerenv ] && ! mountpoint -q "$HOME/.local/share/home-k8s"; then
    echo "~/.local/share/home-k8s がホストと共有されていない。just devcontainer down && just devcontainer up で作り直す" >&2
    exit 1
fi

admin_password="$(kubectl --context "$ctx" -n "$argocd_ns" get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d)"
if [ -z "$admin_password" ]; then
    echo "ArgoCD の admin のパスワード (Secret argocd-initial-admin-secret) が読めない。パスワードを変えて Secret を消したなら、トークンは手で作る (docs/cluster/backstage-argocd.md)" >&2
    exit 1
fi
old_token=""
[ -s "$token_file" ] && old_token="$(cat "$token_file")"

# argocd-server の Pod の中で実行する。標準入力は 1 行目が admin のパスワード、2 行目が以前のトークン (無ければ空)。
# 以前のトークンが今の ArgoCD で通れば、それをそのまま返す。クラスタを作り直すと署名鍵 (server.secretkey) が変わるので古いトークンは通らず、
# 通らなければ作る。打ち直すたびに作ると、ArgoCD のアカウントにトークンが溜まる。
# server.insecure: true (clusters/kind/argocd/values.yaml) なので、Pod の中の 8080 は TLS なしの HTTP
remote='set -eu
server=localhost:8080
config=$(mktemp)
trap "rm -f $config" EXIT
read -r password
read -r old || old=""
argocd login "$server" --plaintext --username admin --password "$password" --config "$config" >/dev/null
if [ -n "$old" ] && argocd account get-user-info --server "$server" --plaintext --auth-token "$old" --config "$config" 2>/dev/null | grep -q "Logged In: true"; then
    printf %s "$old"
else
    argocd account generate-token --account backstage --config "$config"
fi'

# accounts.backstage を入れた argocd-cm を、ArgoCD が読み込むまで少し待つ (打った直後は "account 'backstage' does not exist" になりうる)
token=""
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if token="$(printf '%s\n%s\n' "$admin_password" "$old_token" \
            | kubectl --context "$ctx" -n "$argocd_ns" exec -i deploy/argocd-server -- sh -c "$remote")"; then
        break
    fi
    token=""
    echo "ArgoCD のアカウント backstage のトークンを作れない (${attempt}/10)。3 秒待って打ち直す" >&2
    sleep 3
done
token="$(printf %s "$token" | tr -d '[:space:]')"
# JWT (3 つの部分を . でつないだ文字列) であること。値は表示しない
if ! printf %s "$token" | grep -Eq '^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$'; then
    echo "ArgoCD のアカウント backstage のトークンを作れなかった。clusters/kind/argocd/values.yaml の accounts.backstage と rbac が ArgoCD に入っているか確かめる" >&2
    exit 1
fi

if [ "$token" != "$old_token" ]; then
    mkdir -p "$(dirname "$token_file")"
    (umask 077; printf %s "$token" >"$token_file")
    # コンテナは root で動くので、ホストのユーザー (リポジトリの所有者) からも読めるようにする
    chown "$(stat -c %u:%g "$owner_ref")" "$(dirname "$token_file")" "$token_file" 2>/dev/null || true
fi

# Secret は apply ではなく put_secret (replace・create) で入れる。apply は値を last-applied-configuration の注釈に残す (#49)。
# トークンは引数に出さず --from-file で渡す (ps に残る。#56)。ファイルは Git の外に置いたもの
kubectl --context "$ctx" create namespace backstage --dry-run=client -o yaml \
    | kubectl --context "$ctx" apply -f - >/dev/null
kubectl --context "$ctx" -n backstage create secret generic backstage-argocd \
    --from-file=ARGOCD_AUTH_TOKEN="$token_file" \
    --dry-run=client -o yaml | put_secret "$ctx" backstage backstage-argocd
