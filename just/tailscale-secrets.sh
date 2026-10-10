#!/usr/bin/env bash
# just up の一部。Tailscale の Kubernetes operator の OAuth client (管理画面の Trust credentials) を、Git の外のファイルから
# Secret operator-oauth (namespace tailscale、キー client_id・client_secret) にする。chart は oauth を空にするとこの Secret を読む
# (clusters/kind/tailscale/values.yaml)。client の作り方・ファイルの置き方は docs/cluster/tailscale.md。
# ファイルが無い・空なら失敗する: operator が起動できず、just up の最後 (argocd-wait.sh) が 20 分待って落ちるより先に止める。
# ファイルは KEY=VALUE の行 (# の行と空行は無視)。要る項目は下の keys の 2 つ (Secret のキーと同じ名前)。tailnet= の行もあってよい (Secret には入れない)。
# source はせず、行を読むだけ (中のコマンドを実行しない)。
# 引数: <資格情報のファイル> <kube context>
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
. "$script_dir/secret-lib.sh"
file="$1"
ctx="$2"
ns=tailscale
name=operator-oauth
keys=(client_id client_secret)
# 開発用コンテナでは ~/.config/home-k8s をホストと共有していないと、ホストに置いたファイルが見えず「無い」になる
if [ -f /.dockerenv ] && ! mountpoint -q "$HOME/.config/home-k8s"; then
    echo "~/.config/home-k8s がホストと共有されていない。just devcontainer down && just devcontainer up で作り直す" >&2
    exit 1
fi
if [ ! -s "$file" ]; then
    echo "Tailscale の operator の OAuth client のファイルが無い ($file)。作り方は docs/cluster/tailscale.md" >&2
    exit 1
fi
# 要る項目だけを一時ディレクトリに 1 項目 1 ファイルで写す (本人だけが読める。終わったら消す)。値は引数に出さず --from-file で渡す (#56)
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
from_files=()
for key in "${keys[@]}"; do
    [ -n "${values[$key]:-}" ] || missing+=("$key")
    printf %s "${values[$key]:-}" >"$tmp/$key"
    from_files+=("--from-file=$key=$tmp/$key")
done
if [ "${#missing[@]}" -gt 0 ]; then
    echo "Tailscale の OAuth client のファイル ($file) に、次の項目が無い・空: ${missing[*]}" >&2
    exit 1
fi
kubectl --context "$ctx" create namespace "$ns" --dry-run=client -o yaml \
    | kubectl --context "$ctx" apply -f - >/dev/null
# Secret は apply ではなく put_secret で入れる (apply は値を last-applied-configuration の注釈に残す。#49)
kubectl --context "$ctx" -n "$ns" create secret generic "$name" "${from_files[@]}" \
    --dry-run=client -o yaml | put_secret "$ctx" "$ns" "$name"
echo "Tailscale の OAuth client から Secret $name を作った。operator は起動時にしか読まないので、変えたあとは Pod を作り直す (docs/cluster/tailscale.md)"
