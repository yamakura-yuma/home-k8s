#!/usr/bin/env bash
# just headlamp-token の本体。Headlamp にログインするトークンを作ってファイルに書く。
# ServiceAccount headlamp (Application headlamp が作る) に結び付いた Secret を作り、
# kube-controller-manager が入れたトークンを取り出す。Secret の中身は Git に置かない。
# 引数: <kube context> <トークンのファイル> <リポジトリの所有者を調べるディレクトリ>
set -euo pipefail
ctx="$1"
token_file="$2"
owner_ref="$3"
for _ in $(seq 60); do
    kubectl --context "$ctx" -n headlamp get serviceaccount headlamp >/dev/null 2>&1 && break
    sleep 5
done
kubectl --context "$ctx" -n headlamp apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Secret
metadata:
  name: headlamp-token
  annotations:
    kubernetes.io/service-account.name: headlamp
type: kubernetes.io/service-account-token
YAML
token=
for _ in $(seq 30); do
    token=$(kubectl --context "$ctx" -n headlamp get secret headlamp-token -o jsonpath='{.data.token}' | base64 -d)
    [ -n "$token" ] && break
    sleep 1
done
if [ -z "$token" ]; then
    echo "Secret headlamp-token にトークンが入らなかった。ServiceAccount headlamp があるか確かめる" >&2
    exit 1
fi
mkdir -p "$(dirname "$token_file")"
(umask 077; printf '%s' "$token" > "$token_file")
# コンテナは root で動くので、ホストのユーザー (リポジトリの所有者) からも読めるようにする
chown "$(stat -c %u:%g "$owner_ref")" "$token_file"
