#!/usr/bin/env bash
# just up の本体。Grafana の admin と viewer (共有相手に渡す閲覧用) と backstage (Backstage が読む閲覧用) の
# パスワードを (無ければ生成して) Secret grafana-admin・grafana-viewer・grafana-backstage に入れる。
# Backstage には同じ backstage の資格情報を Secret backstage-grafana (namespace backstage) で渡す。
# Grafana と Backstage (ArgoCD が同期する) は名前で参照する。
# 引数: <admin のパスワードファイル> <viewer のパスワードファイル> <backstage のパスワードファイル> <namespace>
#       <リポジトリの所有者を調べるディレクトリ> <kube context>
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
. "$script_dir/secret-lib.sh"
admin_file="$1"
viewer_file="$2"
backstage_file="$3"
ns="$4"
owner_ref="$5"
ctx="$6"
# 開発用コンテナでは ~/.local/share/home-k8s をホストと共有していないと、
# パスワードがコンテナの中にだけ残る。古いコンテナなら作り直してもらう
if [ -f /.dockerenv ] && ! mountpoint -q "$HOME/.local/share/home-k8s"; then
    echo "~/.local/share/home-k8s がホストと共有されていない。just devcontainer down && just devcontainer up で作り直す" >&2
    exit 1
fi
for file in "$admin_file" "$viewer_file" "$backstage_file"; do
    if [ ! -s "$file" ]; then
        mkdir -p "$(dirname "$file")"
        (umask 077; printf '%s' "$(openssl rand -base64 24)" > "$file")
        # コンテナは root で動くので、ホストのユーザー (リポジトリの所有者) からも読めるようにする
        chown "$(stat -c %u:%g "$owner_ref")" "$file"
    fi
done
# Secret は apply ではなく put_secret (replace・create) で入れる。apply は値を last-applied-configuration の注釈に残す (#49)
for n in "$ns" backstage; do
    kubectl --context "$ctx" create namespace "$n" --dry-run=client -o yaml \
        | kubectl --context "$ctx" apply -f - >/dev/null
done
kubectl --context "$ctx" -n "$ns" create secret generic grafana-admin \
    --from-literal=admin-user=admin --from-file=admin-password="$admin_file" \
    --dry-run=client -o yaml | put_secret "$ctx" "$ns" grafana-admin
kubectl --context "$ctx" -n "$ns" create secret generic grafana-viewer \
    --from-file=password="$viewer_file" \
    --dry-run=client -o yaml | put_secret "$ctx" "$ns" grafana-viewer
kubectl --context "$ctx" -n "$ns" create secret generic grafana-backstage \
    --from-file=password="$backstage_file" \
    --dry-run=client -o yaml | put_secret "$ctx" "$ns" grafana-backstage
# Backstage のプロキシが Authorization: Basic に入れる値 (app-config.yaml の GRAFANA_BASIC_AUTH)
kubectl --context "$ctx" -n backstage create secret generic backstage-grafana \
    --from-literal=GRAFANA_BASIC_AUTH="$(printf 'backstage:%s' "$(cat "$backstage_file")" | base64 -w0)" \
    --dry-run=client -o yaml | put_secret "$ctx" backstage backstage-grafana
