#!/usr/bin/env bash
# just up の一部。環境 (dev・prod) ごとの Grafana (clusters/kind/env-grafana、docs/cluster/backstage-grafana.md) の Secret を、
# (無ければパスワードを生成して) Git の外のファイルから作る。grafana-secrets.sh (observability の Grafana) と同じやり方。
#   各環境の namespace に  Secret grafana-admin (admin-user・admin-password)  と  grafana-backstage (password。Backstage 用の閲覧用ユーザー)
#   namespace backstage に  Secret backstage-grafana-env (GRAFANA_<ENV>_BASIC_AUTH。環境ごとの「backstage:パスワード」の base64)
# Grafana と Backstage (どちらも ArgoCD が同期する) は名前で参照する。Backstage のプロキシ (app-config.yaml の /grafana-<環境>/api) が
# Authorization: Basic に入れる。ブラウザには渡らない。
# 引数: <パスワードのファイルを置くディレクトリ> <リポジトリの所有者を調べるディレクトリ> <kube context> <環境>...
# ファイルは <ディレクトリ>/<環境>-admin-password と <環境>-backstage-password
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
. "$script_dir/secret-lib.sh"
dir="$1"
owner_ref="$2"
ctx="$3"
shift 3
[ "$#" -gt 0 ] || { echo "環境が 1 つも指定されていない" >&2; exit 1; }
# 開発用コンテナでは ~/.local/share/home-k8s をホストと共有していないと、
# パスワードがコンテナの中にだけ残る。古いコンテナなら作り直してもらう
if [ -f /.dockerenv ] && ! mountpoint -q "$HOME/.local/share/home-k8s"; then
    echo "~/.local/share/home-k8s がホストと共有されていない。just devcontainer down && just devcontainer up で作り直す" >&2
    exit 1
fi
# base64 にしても値なので、--from-literal ではなく --from-file で渡す (引数に出ると ps に残る。#56)。一時ファイルは本人だけが読めるようにして、終わったら消す
tmp="$(umask 077; mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
basic_args=()
for env in "$@"; do
    case "$env" in
        *[!a-z0-9-]* | "") echo "環境の名前は小文字・数字・- だけ: $env" >&2; exit 1 ;;
    esac
    admin_file="$dir/$env-admin-password"
    backstage_file="$dir/$env-backstage-password"
    for file in "$admin_file" "$backstage_file"; do
        if [ ! -s "$file" ]; then
            mkdir -p "$dir"
            (umask 077; printf '%s' "$(openssl rand -base64 24)" > "$file")
            # コンテナは root で動くので、ホストのユーザー (リポジトリの所有者) からも読めるようにする
            chown "$(stat -c %u:%g "$owner_ref")" "$file"
        fi
    done
    # Secret は apply ではなく put_secret (replace・create) で入れる。apply は値を last-applied-configuration の注釈に残す (#49)
    kubectl --context "$ctx" create namespace "$env" --dry-run=client -o yaml \
        | kubectl --context "$ctx" apply -f - >/dev/null
    kubectl --context "$ctx" -n "$env" create secret generic grafana-admin \
        --from-literal=admin-user=admin --from-file=admin-password="$admin_file" \
        --dry-run=client -o yaml | put_secret "$ctx" "$env" grafana-admin
    kubectl --context "$ctx" -n "$env" create secret generic grafana-backstage \
        --from-file=password="$backstage_file" \
        --dry-run=client -o yaml | put_secret "$ctx" "$env" grafana-backstage
    key="GRAFANA_$(printf '%s' "$env" | tr 'a-z-' 'A-Z_')_BASIC_AUTH"
    printf 'backstage:%s' "$(cat "$backstage_file")" | base64 -w0 >"$tmp/$env-basic"
    basic_args+=("--from-file=$key=$tmp/$env-basic")
done
# put_secret は Secret を丸ごと置き換えるので、全環境のキーを 1 度で入れる
kubectl --context "$ctx" create namespace backstage --dry-run=client -o yaml \
    | kubectl --context "$ctx" apply -f - >/dev/null
kubectl --context "$ctx" -n backstage create secret generic backstage-grafana-env \
    "${basic_args[@]}" --dry-run=client -o yaml | put_secret "$ctx" backstage backstage-grafana-env
