#!/usr/bin/env bash
# just up の一部。ArgoCD と Grafana ×3 が Keycloak (realm home-k8s) の OIDC で使う client の secret を、各 UI の namespace の Secret にする。
#   argocd/argocd-oidc-keycloak   clientSecret  (argocd-cm の oidc.config が $argocd-oidc-keycloak:clientSecret で読む。
#                                                ArgoCD はラベル app.kubernetes.io/part-of: argocd の付いた Secret しか読まない)
#   observability/grafana-oidc    client-secret (Grafana の環境変数 GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET)
#   dev/grafana-oidc・prod/grafana-oidc  同上 (client grafana-dev・grafana-prod)
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
