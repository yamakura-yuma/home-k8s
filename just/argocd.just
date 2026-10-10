# ArgoCD の初回導入 (bootstrap) と、ArgoCD の外に置く Secret の作成。公開レシピの `just up` (justfile) が呼ぶ内部用。
# 観測スタックと Headlamp は ArgoCD が GitHub の main から同期する (clusters/kind/argocd/apps)。
# 入れ方と構成は docs/cluster/argocd.md。状態の確認は kubectl を素手で叩くこと。

argocd_ns := "argocd"
argocd_chart_version := "10.9.6" # clusters/kind/argocd/apps/argocd.yaml の targetRevision と揃える
# Headlamp にログインするトークンの置き場所。Git の外に置き、`just show headlamp` が表示する
headlamp_token_file := "/tmp/claude-1000/-home-yyamakura-k8s-workspace-home-k8s-gitops-deployer-platform/0672a78f-2795-41c8-9f24-1ef269a3e538/scratchpad/verify-share/headlamp/token"
# Backstage が ArgoCD の API を読むトークン (読み取り専用のアカウント backstage の API トークン)。Git の外に置き、無ければ作る
argocd_backstage_token_file := "/tmp/claude-1000/-home-yyamakura-k8s-workspace-home-k8s-gitops-deployer-platform/0672a78f-2795-41c8-9f24-1ef269a3e538/scratchpad/verify-share/argocd/backstage-token"

# トークンは ServiceAccount headlamp (Application headlamp が作る) に結び付いた Secret から取る
# Headlamp (localhost:4466) にログインするトークンを作り、ファイルに書いて表示する
_headlamp-token:
    bash just/headlamp-token.sh {{kube_context}} "{{headlamp_token_file}}" "{{justfile_directory()}}"
    @echo "  URL:      http://localhost:4466"
    @echo "  トークン: $(cat "{{headlamp_token_file}}")"

# ArgoCD 自身は以後 Application argocd が同じ chart・版・values で同期する。ここは初回の導入のため
_argocd-install:
    helm upgrade --install argocd argo-cd \
        --repo https://argoproj.github.io/argo-helm --version {{argocd_chart_version}} \
        --kube-context {{kube_context}} --namespace {{argocd_ns}} --create-namespace \
        --values clusters/kind/argocd/values.yaml --wait

# 読み取り専用のアカウント backstage (clusters/kind/argocd/values.yaml) の API トークンを作り、Secret backstage-argocd (Backstage 側) に入れる。
# `just up` が ArgoCD を入れたあとに打つ。トークンのファイルが今の ArgoCD で通るなら作り直さない
_argocd-secrets:
    bash just/argocd-secrets.sh "{{argocd_backstage_token_file}}" "{{justfile_directory()}}" {{kube_context}}
