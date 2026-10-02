#!/usr/bin/env bash
# just grafana-secrets の本体。Grafana の admin と viewer (共有相手に渡す閲覧用) のパスワードを
# (無ければ生成して) Secret grafana-admin と grafana-viewer に入れる。Grafana (ArgoCD が同期する) は名前で参照する。
# 引数: <admin のパスワードファイル> <viewer のパスワードファイル> <namespace> <リポジトリの所有者を調べるディレクトリ> <kube context>
set -euo pipefail
admin_file="$1"
viewer_file="$2"
ns="$3"
owner_ref="$4"
ctx="$5"
# 開発用コンテナでは ~/.local/share/home-k8s をホストと共有していないと、
# パスワードがコンテナの中にだけ残る。古いコンテナなら作り直してもらう
if [ -f /.dockerenv ] && ! mountpoint -q "$HOME/.local/share/home-k8s"; then
    echo "~/.local/share/home-k8s がホストと共有されていない。just devcontainer down && just devcontainer up で作り直す" >&2
    exit 1
fi
for file in "$admin_file" "$viewer_file"; do
    if [ ! -s "$file" ]; then
        mkdir -p "$(dirname "$file")"
        (umask 077; printf '%s' "$(openssl rand -base64 24)" > "$file")
        # コンテナは root で動くので、ホストのユーザー (リポジトリの所有者) からも読めるようにする
        chown "$(stat -c %u:%g "$owner_ref")" "$file"
    fi
done
kubectl --context "$ctx" create namespace "$ns" --dry-run=client -o yaml \
    | kubectl --context "$ctx" apply -f - >/dev/null
kubectl --context "$ctx" -n "$ns" create secret generic grafana-admin \
    --from-literal=admin-user=admin --from-file=admin-password="$admin_file" \
    --dry-run=client -o yaml | kubectl --context "$ctx" apply -f -
kubectl --context "$ctx" -n "$ns" create secret generic grafana-viewer \
    --from-file=password="$viewer_file" \
    --dry-run=client -o yaml | kubectl --context "$ctx" apply -f -
