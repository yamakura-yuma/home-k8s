# ArgoCD の初回導入 (bootstrap) と、ArgoCD の外に置く Secret の作成。
# 観測スタックと Headlamp は ArgoCD が GitHub の main から同期する (clusters/kind/argocd/apps)。
# 入れ方と構成は docs/cluster/argocd.md。状態の確認は kubectl を素手で叩くこと。

argocd_ns := "argocd"
argocd_chart_version := "10.9.6" # clusters/kind/argocd/apps/argocd.yaml の targetRevision と揃える
# Headlamp にログインするトークンの置き場所。Git の外に置き、`just headlamp-show-token` が表示する
headlamp_token_file := env_var("HOME") + "/.local/share/home-k8s/headlamp/token"

# 以後は ArgoCD が自分自身と観測スタック・Headlamp を同期する。打ち直しても同じ状態に戻るだけ
# ArgoCD を helm で入れ、Secret を作ってから root の Application を apply する (bootstrap)
argocd-up: _argocd-install grafana-secrets && headlamp-token
    kubectl --context {{kube_context}} apply -f clusters/kind/argocd/root.yaml
    @echo "ArgoCD が子の Application を同期するのを待つ (初回は image の pull で数分かかる)"
    bash just/argocd-wait.sh {{kube_context}} {{argocd_ns}}

# ArgoCD の UI (localhost:8080) の admin のパスワードを表示する
argocd-show-admin:
    @echo "  URL:        http://localhost:8080"
    @echo "  ユーザー:   admin"
    @echo "  パスワード: $(kubectl --context {{kube_context}} -n {{argocd_ns}} get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d)"

# トークンは ServiceAccount headlamp (Application headlamp が作る) に結び付いた Secret から取る
# Headlamp (localhost:4466) にログインするトークンを作り、ファイルに書いて表示する
headlamp-token:
    bash just/headlamp-token.sh {{kube_context}} "{{headlamp_token_file}}" "{{justfile_directory()}}"
    @echo "  URL:      http://localhost:4466"
    @echo "  トークン: $(cat "{{headlamp_token_file}}")"

# Headlamp にログインするトークンを表示する
headlamp-show-token:
    @echo "  URL:      http://localhost:4466"
    @echo "  トークン: $(cat "{{headlamp_token_file}}")"

# ArgoCD 自身は以後 Application argocd が同じ chart・版・values で同期する。ここは初回の導入のため
_argocd-install:
    helm upgrade --install argocd argo-cd \
        --repo https://argoproj.github.io/argo-helm --version {{argocd_chart_version}} \
        --kube-context {{kube_context}} --namespace {{argocd_ns}} --create-namespace \
        --values clusters/kind/argocd/values.yaml --wait
