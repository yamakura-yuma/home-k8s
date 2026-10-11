#!/usr/bin/env bash
# just up の一部。gitops-deployer (docs/cluster/gitops-deployer.md) の資格情報を Secret にする。
#   gitops-deployer/github-token   token          git-activity が home-k8s に PR を作る fine-grained PAT (contents・pull requests の読み書きだけ)。
#                                                 元は人が置くファイル (GITHUB_TOKEN=<PAT> の 1 行)。無ければ作らずに警告だけ出す (just up は止めない。
#                                                 git-activity は起動できず、デプロイの依頼は PR を作れないまま待つ)
#   testkube/gitops-deployer-e2e   client_id・client_secret  testkube の E2E が Keycloak の client gitops-deployer で client credentials のトークンを取る。
#                                                 元は keycloak-secrets.sh が作る <client のディレクトリ>/gitops-deployer (realm の import と同じ値)。
#                                                 ここでは作らない (realm の側とずれる)。無ければ止まる。_keycloak-secrets の後に打つ
# ファイルは source せず行を読むだけ (中のコマンドを実行しない)。値は引数に出さず --from-file で渡す (#56)、apply しない (#49)。
# 引数: <GitHub のトークンのファイル> <client の secret のディレクトリ> <kube context>
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
. "$script_dir/secret-lib.sh"
github_file="$1"
client_dir="$2"
ctx="$3"
client=gitops-deployer
if [ -f /.dockerenv ] && ! mountpoint -q "$HOME/.config/home-k8s"; then
    echo "~/.config/home-k8s がホストと共有されていない。just devcontainer down && just devcontainer up で作り直す" >&2
    exit 1
fi
if [ ! -s "$client_dir/$client" ]; then
    echo "client $client の secret のファイル $client_dir/$client が無い。先に _keycloak-secrets (just/keycloak-secrets.sh) を打つ" >&2
    exit 1
fi
tmp="$(umask 077; mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
for ns in gitops-deployer testkube; do
    kubectl --context "$ctx" create namespace "$ns" --dry-run=client -o yaml \
        | kubectl --context "$ctx" apply -f - >/dev/null
done

token=""
if [ -s "$github_file" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        case "$line" in GITHUB_TOKEN=*) token="${line#GITHUB_TOKEN=}" ;; esac
    done <"$github_file"
fi
if [ -n "$token" ]; then
    printf %s "$token" >"$tmp/token"
    kubectl --context "$ctx" -n gitops-deployer create secret generic github-token --from-file="token=$tmp/token" \
        --dry-run=client -o yaml | put_secret "$ctx" gitops-deployer github-token
else
    echo "警告: GitHub のトークンのファイル ($github_file) が無いか GITHUB_TOKEN= の行が無い。git-activity は起動しない。作り方は docs/cluster/gitops-deployer.md" >&2
fi

printf %s "$client" >"$tmp/client_id"
kubectl --context "$ctx" -n testkube create secret generic gitops-deployer-e2e \
    --from-file="client_id=$tmp/client_id" --from-file="client_secret=$client_dir/$client" \
    --dry-run=client -o yaml | put_secret "$ctx" testkube gitops-deployer-e2e
