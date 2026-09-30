#!/usr/bin/env bash
# just observe-up の前段。引数: <パスワードファイル> <namespace> <リポジトリの所有者を調べるディレクトリ>
set -euo pipefail
file="$1"
ns="$2"
owner_ref="$3"
dir="$(dirname "$file")"
# 開発用コンテナでは ~/.local/share/home-k8s をホストと共有していないと、
# パスワードがコンテナの中にだけ残る。古いコンテナなら作り直してもらう
if [ -f /.dockerenv ] && ! mountpoint -q "$HOME/.local/share/home-k8s"; then
    echo "~/.local/share/home-k8s がホストと共有されていない。just devcontainer down && just devcontainer up で作り直す" >&2
    exit 1
fi
if [ ! -s "$file" ]; then
    mkdir -p "$dir"
    (umask 077; printf '%s' "$(openssl rand -base64 24)" > "$file")
    # コンテナは root で動くので、ホストのユーザー (リポジトリの所有者) からも読めるようにする
    chown "$(stat -c %u:%g "$owner_ref")" "$file"
fi
kubectl --context kind-study-kind create namespace "$ns" --dry-run=client -o yaml \
    | kubectl --context kind-study-kind apply -f - >/dev/null
kubectl --context kind-study-kind -n "$ns" create secret generic grafana-admin \
    --from-literal=admin-user=admin --from-file=admin-password="$file" \
    --dry-run=client -o yaml | kubectl --context kind-study-kind apply -f -
