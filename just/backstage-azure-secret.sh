#!/usr/bin/env bash
# just up の一部。Azure のタブの資格情報 (読み取り用のサービスプリンシパル) を、Git の外のファイルから
# Secret backstage-azure (namespace backstage) にする。Backstage の chart が環境変数 AZURE_* にする
# (clusters/kind/backstage/values.yaml の extraEnvVars は optional)。
# ファイルが無い・空なら Secret を作らず (あれば消し)、失敗しない: Backstage は起動し、Azure のタブは「資格情報が無い」を出す。
# ファイルの置き方・サービスプリンシパルの作り方は docs/cluster/backstage-azure.md。
# ファイルは KEY=VALUE の行 (# の行と空行は無視)。要る項目は下の keys の 5 つ。source はせず、行を読むだけ (中のコマンドを実行しない)。
# 引数: <資格情報のファイル> <kube context>
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
. "$script_dir/secret-lib.sh"
file="$1"
ctx="$2"
ns=backstage
name=backstage-azure
keys=(AZURE_DOMAIN AZURE_TENANT_ID AZURE_CLIENT_ID AZURE_CLIENT_SECRET AZURE_SUBSCRIPTION_ID)
# 開発用コンテナでは ~/.local/share/home-k8s をホストと共有していないと、ホストに置いたファイルが見えず「無い」になる
if [ -f /.dockerenv ] && ! mountpoint -q "$HOME/.local/share/home-k8s"; then
    echo "~/.local/share/home-k8s がホストと共有されていない。just devcontainer down && just devcontainer up で作り直す" >&2
    exit 1
fi
kubectl --context "$ctx" create namespace "$ns" --dry-run=client -o yaml \
    | kubectl --context "$ctx" apply -f - >/dev/null
if [ ! -s "$file" ]; then
    echo "Azure の資格情報のファイルが無い ($file)。Secret $name は作らない: Azure のタブは「資格情報が無い」を出す"
    kubectl --context "$ctx" -n "$ns" delete secret "$name" --ignore-not-found >/dev/null
    exit 0
fi
# 要る項目だけを一時ファイルに写す (本人だけが読める。終わったら消す)。ほかの行は Secret に入れない
tmp="$(umask 077; mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
declare -A values=()
while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"
    [ "$key" != "$line" ] || continue
    values["$key"]="${line#*=}"
done <"$file"
missing=()
for key in "${keys[@]}"; do
    [ -n "${values[$key]:-}" ] || missing+=("$key")
    printf '%s=%s\n' "$key" "${values[$key]:-}" >>"$tmp/env"
done
if [ "${#missing[@]}" -gt 0 ]; then
    echo "Azure の資格情報のファイル ($file) に、次の項目が無い・空: ${missing[*]}" >&2
    exit 1
fi
# Secret は apply ではなく put_secret で入れる (apply は値を last-applied-configuration の注釈に残す。#49)
kubectl --context "$ctx" -n "$ns" create secret generic "$name" --from-env-file="$tmp/env" \
    --dry-run=client -o yaml | put_secret "$ctx" "$ns" "$name"
echo "Azure の資格情報から Secret $name を作った。Backstage の Pod は起動時にしか読まないので、変えたあとは Pod を作り直す (docs/cluster/backstage-azure.md)"
