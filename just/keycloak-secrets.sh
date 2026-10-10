#!/usr/bin/env bash
# just up の一部。Keycloak (namespace auth) の資格情報を、無ければ生成してホストのファイルに置き、Secret にする。
#   keycloak-postgres         DB のユーザー (keycloak) とパスワード。PostgreSQL・Keycloak・pg_dump の CronJob が読む
#   keycloak-bootstrap-admin  realm master の初期の管理者 (admin)。非常用。Keycloak の CR の bootstrapAdmin が読む
#   keycloak-clients          realm home-k8s の各 client の secret (キーは clientId)。KeycloakRealmImport の placeholders が読む
# ファイルは一度作ったら再利用する。DB と realm はホストのディレクトリに残るので (docs/cluster/persistence.md)、作り直すと DB の側とずれる。
# 各 UI (単位 3〜6) は同じファイルから自分の namespace の Secret を作る。構成は docs/cluster/keycloak.md。
# 引数: <資格情報のディレクトリ> <リポジトリの所有者を調べるディレクトリ> <kube context>
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
. "$script_dir/secret-lib.sh"
dir="$1"
owner_ref="$2"
ctx="$3"
ns=auth
# 秘密を持つ client (public の argocd-cli・kubernetes は持たない)。clusters/kind/auth/keycloak/realm-home-k8s.yaml の placeholders と同じ並び
clients=(argocd grafana grafana-dev grafana-prod temporal-dev temporal-prod backstage headlamp oauth2-proxy)
# 開発用コンテナでは ~/.local/share/home-k8s をホストと共有していないと、パスワードがコンテナの中にだけ残る
if [ -f /.dockerenv ] && ! mountpoint -q "$HOME/.local/share/home-k8s"; then
    echo "~/.local/share/home-k8s がホストと共有されていない。just devcontainer down && just devcontainer up で作り直す" >&2
    exit 1
fi
# 無ければ作る。値は英数字だけにする (realm の import の placeholder と、各 UI の設定ファイルにそのまま入れられるように)
ensure() {
    local file="$1"
    [ -s "$file" ] && return 0
    mkdir -p "$(dirname "$file")"
    (umask 077; printf '%s' "$(openssl rand -hex 24)" >"$file")
    # コンテナは root で動くので、ホストのユーザー (リポジトリの所有者) からも読めるようにする
    chown "$(stat -c %u:%g "$owner_ref")" "$file" "$(dirname "$file")"
}
ensure "$dir/postgres-password"
ensure "$dir/admin-password"
client_args=()
for c in "${clients[@]}"; do
    ensure "$dir/clients/$c"
    client_args+=("--from-file=$c=$dir/clients/$c")
done
kubectl --context "$ctx" create namespace "$ns" --dry-run=client -o yaml \
    | kubectl --context "$ctx" apply -f - >/dev/null
# Secret は apply ではなく put_secret (replace・create) で入れる。apply は値を last-applied-configuration の注釈に残す (#49)。値は --from-file で渡す (#56)
kubectl --context "$ctx" -n "$ns" create secret generic keycloak-postgres \
    --from-literal=username=keycloak --from-file=password="$dir/postgres-password" \
    --dry-run=client -o yaml | put_secret "$ctx" "$ns" keycloak-postgres
kubectl --context "$ctx" -n "$ns" create secret generic keycloak-bootstrap-admin \
    --from-literal=username=admin --from-file=password="$dir/admin-password" \
    --dry-run=client -o yaml | put_secret "$ctx" "$ns" keycloak-bootstrap-admin
kubectl --context "$ctx" -n "$ns" create secret generic keycloak-clients "${client_args[@]}" \
    --dry-run=client -o yaml | put_secret "$ctx" "$ns" keycloak-clients
