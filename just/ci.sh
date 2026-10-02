#!/usr/bin/env bash
# `just ci` の本体。PR のゲート (dotfiles の docs/gates.md) で、ローカルと Actions が同じに走らせる 1 本。
# 静的チェックだけ (クラスタは立てない・触らない)。ツールは flake の devShells.ci から入る
# (nix が無ければ落ちる)。作業ツリーは変えず、描画した manifest は一時ディレクトリに出す。
#
#   1. yamllint        リポジトリの YAML (.yamllint.yaml)
#   2. helm template   clusters/kind/argocd/apps の各 Application が指す chart を、ArgoCD と同じ版・values で描画
#   3. kustomize build / 素の manifest  同じ Application の path (dashboards、storage、headlamp/manifests)
#   4. kubeconform     3 までの出力を、kind の Kubernetes の版のスキーマに照らす
#   5. kube-linter     3 までの出力 (.kube-linter.yaml)
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -z "${HOME_K8S_CI:-}" ]; then
    command -v nix >/dev/null || { echo "nix が無い。just ci は nix develop .#ci でツールを入れる" >&2; exit 1; }
    exec nix develop .#ci -c env HOME_K8S_CI=1 bash "$0" "$@"
fi

# kind が既定で立てる Kubernetes の版 (kubeconform のスキーマの版)。kind を上げたらここも上げる
k8s_version=1.35.0
apps=clusters/kind/argocd/apps

out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT

echo "== yamllint =="
git ls-files -z '*.yaml' '*.yml' | xargs -0 yamllint --strict

echo "== 描画 (helm template / kustomize build) =="
for app in "$apps"/*.yaml; do
    name=$(yq '.metadata.name' "$app")
    ns=$(yq '.spec.destination.namespace // "default"' "$app")
    # chart を持つ source は helm template。values は ArgoCD の $values/ (この repo) を実ファイルに戻す
    while IFS=$'\t' read -r chart repo version release values; do
        [ -n "$chart" ] || continue
        args=()
        for f in ${values//\$values\//}; do args+=(-f "$f"); done
        echo "helm template $name ($chart $version)"
        helm template "$release" "$chart" --repo "$repo" --version "$version" --namespace "$ns" \
            "${args[@]}" >"$out/helm-$name.yaml"
    done < <(yq '.spec | (.sources // [.source]) | .[] | select(has("chart"))
                 | [.chart, .repoURL, .targetRevision, .helm.releaseName, (.helm.valueFiles // [] | join(" "))] | @tsv' "$app")
    # path を持つ source は、この repo の manifest。kustomization.yaml があれば kustomize、無ければそのまま
    while IFS= read -r dir; do
        [ -n "$dir" ] && [ "$dir" != "$apps" ] || continue
        out_file="$out/dir-$(echo "$dir" | tr / -).yaml"
        [ -e "$out_file" ] && continue
        echo "manifest $name ($dir)"
        if [ -f "$dir/kustomization.yaml" ]; then
            kustomize build "$dir" >"$out_file"
        else
            yq eval-all '.' "$dir"/*.yaml >"$out_file"
        fi
    done < <(yq '.spec | (.sources // [.source]) | .[] | select(has("path")) | .path' "$app")
done

echo "== kubeconform (Kubernetes $k8s_version) =="
# CRD (ArgoCD・ServiceMonitor など) のスキーマは標準のカタログに無いので飛ばす
kubeconform -strict -summary -ignore-missing-schemas -kubernetes-version "$k8s_version" "$out"

echo "== kube-linter =="
kube-linter lint --config .kube-linter.yaml "$out"
