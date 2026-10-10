#!/usr/bin/env bash
# just up の一部。ArgoCD・Grafana ×3・Temporal UI ×2・Backstage・Headlamp が Keycloak (realm home-k8s) の OIDC で使う client の secret を、各 UI の namespace の Secret にする。
#   argocd/argocd-oidc-keycloak   clientSecret  (argocd-cm の oidc.config が $argocd-oidc-keycloak:clientSecret で読む。
#                                                ArgoCD はラベル app.kubernetes.io/part-of: argocd の付いた Secret しか読まない)
#   observability/grafana-oidc    client-secret (Grafana の環境変数 GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET)
#   dev/grafana-oidc・prod/grafana-oidc  同上 (client grafana-dev・grafana-prod)
#   dev/temporal-oidc・prod/temporal-oidc client-secret (Temporal Web UI の環境変数 TEMPORAL_AUTH_CLIENT_SECRET。client temporal-dev・temporal-prod)
#   backstage/backstage-oidc      AUTH_OIDC_CLIENT_SECRET (chart の extraEnvVarsSecrets がキーの名前のまま環境変数にする。app-config.yaml の auth.providers.oidc)
#   headlamp/headlamp-oidc        OIDC_CLIENT_SECRET (chart の config.oidc.externalSecret が envFrom で環境変数にする。clusters/kind/headlamp/values.yaml)
# あわせて backstage/backstage-session (AUTH_SESSION_SECRET。app-config.yaml の auth.session.secret) を、無いときだけ乱数で作る。
# OIDC のログインの途中 (state・nonce) を入れる session の cookie の署名鍵で、session はインメモリの DB にあるので、ファイルには残さない
# 元は keycloak-secrets.sh が作るホストのファイル <ディレクトリ>/<clientId> (realm の import と同じ値)。ここでは作らない:
# 作ると realm の側の secret とずれるので、無ければ止まる。_keycloak-secrets の後に打つ。
# 引数: <client の secret のディレクトリ> <kube context>
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
. "$script_dir/secret-lib.sh"
dir="$1"
ctx="$2"
# <namespace> <Secret> <キー> <clientId>
targets=(
    "argocd argocd-oidc-keycloak clientSecret argocd"
    "observability grafana-oidc client-secret grafana"
    "dev grafana-oidc client-secret grafana-dev"
    "prod grafana-oidc client-secret grafana-prod"
    "dev temporal-oidc client-secret temporal-dev"
    "prod temporal-oidc client-secret temporal-prod"
    "backstage backstage-oidc AUTH_OIDC_CLIENT_SECRET backstage"
    "headlamp headlamp-oidc OIDC_CLIENT_SECRET headlamp"
)
for t in "${targets[@]}"; do
    read -r _ _ _ client <<<"$t"
    if [ ! -s "$dir/$client" ]; then
        echo "client $client の secret のファイル $dir/$client が無い。先に _keycloak-secrets (just/keycloak-secrets.sh) を打つ" >&2
        exit 1
    fi
done
for t in "${targets[@]}"; do
    read -r ns name key client <<<"$t"
    kubectl --context "$ctx" create namespace "$ns" --dry-run=client -o yaml \
        | kubectl --context "$ctx" apply -f - >/dev/null
    # Secret は apply ではなく put_secret (replace・create) で入れる。apply は値を last-applied-configuration の注釈に残す (#49)。値は --from-file で渡す (#56)。
    # ラベルは ArgoCD が Secret を読むためのもの。Grafana の Secret にも付けて害は無いが、要るものにだけ付ける
    manifest="$(kubectl --context "$ctx" -n "$ns" create secret generic "$name" --from-file="$key=$dir/$client" --dry-run=client -o yaml)"
    if [ "$ns" = argocd ]; then
        manifest="$(kubectl --context "$ctx" label --local -f - app.kubernetes.io/part-of=argocd -o yaml <<<"$manifest")"
    fi
    put_secret "$ctx" "$ns" "$name" <<<"$manifest"
done

# Backstage の session の署名鍵。値はコマンドラインに出さず、一時ファイルから --from-file で渡す (#56)
session_file="$(mktemp)"
trap 'rm -f "$session_file"' EXIT
(umask 077; printf '%s' "$(openssl rand -hex 32)" >"$session_file")
create_secret_if_missing "$ctx" backstage backstage-session --from-file="AUTH_SESSION_SECRET=$session_file" >/dev/null
