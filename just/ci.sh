#!/usr/bin/env bash
# `just ci` の本体。PR のゲート (dotfiles の docs/gates.md) で、ローカルと Actions が同じに走らせる 1 本。
# 静的チェックと単体試験・型検査だけ (クラスタは立てない・触らない)。ツールは flake の devShells.ci から入る
# (nix が無ければ落ちる)。作業ツリーは変えず、描画した manifest は一時ディレクトリに出す。
#
#   1. yamllint        リポジトリの YAML (.yamllint.yaml)
#   2. helm template   clusters/kind/argocd/apps の各 Application が指す chart を、ArgoCD と同じ版・values で描画。
#                      ApplicationSet (環境ごとの展開) は list generator の要素ごとに Application へ展開してから描画する
#   3. kustomize build / 素の manifest  同じ Application の path (dashboards、storage、headlamp/manifests)
#   4. kubeconform     3 までの出力を、kind の Kubernetes の版のスキーマに照らす
#   5. kube-linter     3 までの出力 (.kube-linter.yaml)
#   5b. hostPort       3 までの出力で、同じ hostPort を 2 つ以上のワークロードが使わないこと、環境 (dev・prod) の Collector が hostPort を使わないこと
#   6. share の公開の入口  share namespace に同期する manifest に NodePort・LoadBalancer の Service と Ingress が無いこと (yq)
#   7. unittest        share の認証サービスと、share Pod の Caddyfile の経路・manifest (clusters/kind/share)。caddy と認証サービスを空きポートで起動し、
#                      偽の upstream に向けて、認証なし・許可リスト外・delete・期限切れ・認証サービスに届かないときの拒否を確かめる。クラスタにもネットワークにも出ない
#   8. share のホスト側  中継 (just/share-relay.Caddyfile) を caddy で起動して認証なし・許可リスト外の拒否を確かめ、Secret を作るスクリプトと
#                      URL を引く関数と CLI (just share add/delete/...、just/share.sh) を偽の kubectl で確かめる (注釈・引数に値が残らない作り方、#49・#56。
#                      CLI は上限超えの --ttl・不正な名前・2 つ目の特権・期限付きへの rotate の拒否と、作った項目を実物の認証サービスに通す往復)。
#                      稼働中のクラスタ・ホストには触れない。あわせて環境ごとのサンプルの API (services/sample-api) の経路と OpenAPI の突き合わせ、
#                      Temporal (clusters/kind/temporal) の URL・ポート・CSP・proxy の突き合わせ、gitops-deployer の基盤 (clusters/kind/gitops-deployer/test_platform.py) の
#                      ambient に入れる namespace・Gateway と Knative の経路・Kafka と Redis の入口・Dapr の mTLS・APISIX の経路の突き合わせ
#   8b. Azure の資格情報  just/backstage-azure-secret.sh を偽の kubectl で確かめる (ファイルが無い・空・欠けた項目・CRLF・コメント、値が引数に残らないこと)
#   8c. Tailscale の OAuth client  just/tailscale-secrets.sh を偽の kubectl で確かめる (ファイルが無ければ止まる・欠けた項目・CRLF、値が引数に残らないこと)
#   8d. Keycloak       just/keycloak-secrets.sh (ファイルを一度だけ作る・値が引数に残らない) と just/coredns-tailnet.sh (rewrite の挿入・打ち直しで変わらない) を
#                      偽の kubectl で確かめ、realm の client と Secret keycloak-clients のキーを突き合わせる
#   9. backstage       yarn install --immutable (yarn.lock のとおりに入れ、ずれていたら落とす) のあと、backend・app の jest (yarn workspace backend/app test) と
#                      型検査 (yarn tsc)。node_modules は backstage/ に入る (git の管理外・.dockerignore 済み)
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
expanded=$(mktemp -d)  # ApplicationSet を要素ごとの Application に展開したもの (2 で描画する)
trap 'rm -rf "$out" "$expanded"' EXIT
share_renders=()  # destination が namespace share の Application が描画した manifest (6 で調べる)

echo "== yamllint =="
git ls-files -z '*.yaml' '*.yml' | xargs -0 yamllint --strict

echo "== 描画 (helm template / kustomize build) =="
# ApplicationSet (環境ごとの展開、docs/cluster/environments.md) は list generator の要素ごとに template の
# {{.キー}} を値に置き換え、Application として下の描画に回す。要素の無い ApplicationSet は落とす
app_files=()
for app in "$apps"/*.yaml; do
    if [ "$(yq '.kind' "$app")" != ApplicationSet ]; then
        app_files+=("$app")
        continue
    fi
    if [ "$(yq '[.spec.generators[] | select(has("list") | not)] | length' "$app")" != 0 ]; then
        echo "ApplicationSet の generator は list だけにする (just ci が展開できない): $app" >&2
        exit 1
    fi
    n=$(yq '[.spec.generators[].list.elements[]] | length' "$app")
    [ "$n" -gt 0 ] || { echo "ApplicationSet に list generator の要素が無い: $app" >&2; exit 1; }
    for ((i = 0; i < n; i++)); do
        element=$(yq -o=json -I=0 "[.spec.generators[].list.elements[]] | .[$i]" "$app")
        rendered=$(yq '{"apiVersion": "argoproj.io/v1alpha1", "kind": "Application"} * .spec.template' "$app")
        while IFS=$'\t' read -r key value; do
            rendered=${rendered//"{{.$key}}"/$value}
        done < <(echo "$element" | yq -p=json 'to_entries | .[] | [.key, .value] | @tsv')
        if grep -q '{{' <<<"$rendered"; then
            echo "ApplicationSet の template に要素で置き換わらない {{ }} が残る: $app" >&2
            exit 1
        fi
        file="$expanded/$(basename "$app" .yaml)-$i.yaml"
        echo "$rendered" >"$file"
        echo "ApplicationSet $(yq '.metadata.name' "$app") -> Application $(yq '.metadata.name' "$file")"
        app_files+=("$file")
    done
done
for app in "${app_files[@]}"; do
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
        [ "$ns" = share ] && share_renders+=("$out_file")
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

echo "== share の公開の入口 (NodePort・LoadBalancer・Ingress が無いこと) =="
# share の入口は Cloudflare への外向き接続だけ。クラスタの外に待ち受けを開ける種類を足したら落とす。描画した manifest が 1 つも無いのも落とす (Application が無いと検査にならない)
[ "${#share_renders[@]}" -gt 0 ] || { echo "namespace share の Application が描画されていない (clusters/kind/argocd/apps/share.yaml)" >&2; exit 1; }
exposed=$(yq 'select(.kind == "Ingress" or (.kind == "Service" and (.spec.type == "NodePort" or .spec.type == "LoadBalancer")))
                       | .kind + "/" + .metadata.name' "${share_renders[@]}")
[ -z "$exposed" ] || { echo "share に公開の入口が入っている: $exposed" >&2; exit 1; }
echo "ok (${share_renders[*]##*/})"

echo "== hostPort (同じノードに置く Pod が同じ hostPort を取り合わないこと) =="
# 環境ごとの Collector (daemonset) は chart の既定で 4317・4318 を hostPort に開け、dev と prod が同じノードで取り合って Pending になった。
# 描画した全ワークロードの hostPort を集め、同じ port/protocol を 2 つ以上のワークロードが使っていたら落とす。環境の Collector は使わない
host_ports=$(yq -N 'select(.kind == "DaemonSet" or .kind == "Deployment" or .kind == "StatefulSet")
                 | (.metadata.namespace + "/" + .metadata.name) as $w
                 | .spec.template.spec.containers[] | (.ports // [])[] | select(has("hostPort"))
                 | (.hostPort | tostring) + "/" + (.protocol // "TCP") + " " + $w' "$out"/*.yaml)
dup=$(awk '{print $1}' <<<"$host_ports" | sort | uniq -d)
[ -z "$dup" ] || { echo "同じ hostPort を 2 つ以上のワークロードが使っている: $dup" >&2; echo "$host_ports" >&2; exit 1; }
env_host_ports=$(grep -E ' [^/ ]+/[^ ]*otel-collector[^ ]*$' <<<"$host_ports" | grep -E ' (dev|prod)/' || true)
[ -z "$env_host_ports" ] || { echo "環境 (dev・prod) の Collector が hostPort を使っている: $env_host_ports" >&2; exit 1; }
echo "ok (hostPort のワークロード: $(grep -c . <<<"$host_ports" || true))"

# caddy の試験は caddy が無いと飛ばされるので、CI では無いことを失敗にする
command -v caddy >/dev/null || { echo "caddy が無い (devShells.ci に入っているはず)" >&2; exit 1; }
echo "== share 認証の単体試験・Caddyfile の経路・manifest (caddy + 認証サービス + 偽の upstream) =="
python3 -B -m unittest discover -s clusters/kind/share -v
echo "== sample-api (環境ごとのサンプルの API の経路と OpenAPI の突き合わせ) =="
python3 -B -m unittest discover -s services/sample-api -v
echo "== temporal (環境ごとの UI の URL・ポート・CSP・proxy の突き合わせ) =="
python3 -B -m unittest discover -s clusters/kind/temporal -v
echo "== gitops-deployer の基盤 (ambient の namespace・Gateway と Knative・Kafka の入口・Dapr・APISIX の経路の突き合わせ) =="
python3 -B -m unittest discover -s clusters/kind/gitops-deployer -p 'test_*.py' -v

echo "== share のホスト側 (中継の caddy、Secret を作るスクリプト、URL を引く関数、CLI) =="
python3 -B -m unittest discover -s just -p 'test_share_*.py' -v
echo "== Azure のタブの資格情報の Secret (ファイルが無くても失敗しない、値が引数に残らない) =="
python3 -B -m unittest discover -s just -p 'test_backstage_azure_secret.py' -v
echo "== Tailscale の operator の OAuth client の Secret (ファイルが無ければ止まる、値が引数に残らない) =="
python3 -B -m unittest discover -s just -p 'test_tailscale_secrets.py' -v
echo "== Keycloak の Secret (ファイルを一度だけ作る、値が引数に残らない)・realm の client と Secret のキー・CoreDNS の読み替え =="
python3 -B -m unittest discover -s just -p 'test_keycloak.py' -v

# backstage の試験と型検査。node と yarn が無いと飛ばされるのではなく失敗にする
command -v node >/dev/null && command -v yarn >/dev/null || { echo "node か yarn が無い (devShells.ci に入っているはず)" >&2; exit 1; }
echo "== backstage の依存 (yarn install --immutable) =="
# 版は backstage/.yarnrc.yml の yarnPath (.yarn/releases) が決める。CI=1 は backstage-cli の jest を watch にしないため
(cd backstage && yarn install --immutable)
echo "== backstage backend の jest (yarn workspace backend test) =="
(cd backstage && CI=1 yarn workspace backend test)
echo "== backstage app の jest (yarn workspace app test) =="
(cd backstage && CI=1 yarn workspace app test)
echo "== backstage の型検査 (yarn tsc) =="
(cd backstage && yarn tsc)
