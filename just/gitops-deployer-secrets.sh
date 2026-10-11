#!/usr/bin/env bash
# just up の一部。gitops-deployer (docs/cluster/gitops-deployer.md) の資格情報を Secret にする。
#   gitops-deployer/github-app     app-id・installation-id・private-key.pem
#                                                 git-activity が home-k8s に PR を作る GitHub App (home-k8s だけに入れ、Contents・Pull requests の読み書き)。
#                                                 元は人が置く 2 つのファイル: ID (GITHUB_APP_ID=・GITHUB_APP_INSTALLATION_ID= の行) と秘密鍵 (PEM)。
#                                                 どちらかが無ければ作らずに警告だけ出す (just up は止めない。git-activity は起動できず、
#                                                 デプロイの依頼は PR を作れないまま待つ)
#   testkube/gitops-deployer-e2e   client_id・client_secret  testkube の E2E が Keycloak の client gitops-deployer で client credentials のトークンを取る。
#                                                 元は keycloak-secrets.sh が作る <client のディレクトリ>/gitops-deployer (realm の import と同じ値)。
#                                                 ここでは作らない (realm の側とずれる)。無ければ止まる。_keycloak-secrets の後に打つ
# ファイルは source せず行を読むだけ (中のコマンドを実行しない)。値は引数に出さず --from-file で渡す (#56)、apply しない (#49)。
# 引数: <GitHub App の ID のファイル> <GitHub App の秘密鍵のファイル> <client の secret のディレクトリ> <kube context>
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
. "$script_dir/secret-lib.sh"
app_file="$1"
key_file="$2"
client_dir="$3"
ctx="$4"
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

app_id=""
installation_id=""
if [ -s "$app_file" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        case "$line" in
            GITHUB_APP_ID=*) app_id="${line#GITHUB_APP_ID=}" ;;
            GITHUB_APP_INSTALLATION_ID=*) installation_id="${line#GITHUB_APP_INSTALLATION_ID=}" ;;
        esac
    done <"$app_file"
fi
# ID は数だけ (ファイルの書き損じを Secret に入れない)
case "$app_id$installation_id" in *[!0-9]*) app_id="" ;; esac
if [ -n "$app_id" ] && [ -n "$installation_id" ] && [ -s "$key_file" ] && grep -q -- '-----BEGIN .*PRIVATE KEY-----' "$key_file"; then
    printf %s "$app_id" >"$tmp/app-id"
    printf %s "$installation_id" >"$tmp/installation-id"
    kubectl --context "$ctx" -n gitops-deployer create secret generic github-app \
        --from-file="app-id=$tmp/app-id" --from-file="installation-id=$tmp/installation-id" \
        --from-file="private-key.pem=$key_file" \
        --dry-run=client -o yaml | put_secret "$ctx" gitops-deployer github-app
else
    echo "警告: GitHub App の ID ($app_file の GITHUB_APP_ID=・GITHUB_APP_INSTALLATION_ID=) か秘密鍵 ($key_file) が無い。git-activity は起動しない。作り方は docs/cluster/gitops-deployer.md" >&2
fi

printf %s "$client" >"$tmp/client_id"
kubectl --context "$ctx" -n testkube create secret generic gitops-deployer-e2e \
    --from-file="client_id=$tmp/client_id" --from-file="client_secret=$client_dir/$client" \
    --dry-run=client -o yaml | put_secret "$ctx" testkube gitops-deployer-e2e
