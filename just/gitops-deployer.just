# gitops-deployer (services/gitops-deployer) のイメージの build と kind load、資格情報の Secret。公開レシピの `just up` (justfile) が呼ぶ内部用。
# Pod は ArgoCD が同期する (clusters/kind/argocd/apps/gitops-deployer-services.yaml)。構成は docs/cluster/gitops-deployer.md。

# 6 つの Pod と testkube の E2E が使う 1 つのイメージ。レジストリに置かず kind load する。名前の kind.local/ は、Knative が
# タグを digest に解決しにいかない印 (config-deployment の registries-skipping-tag-resolving の既定)。
# タグは services/gitops-deployer/manifests の image と揃える。アプリを変えたらタグを上げる (同じタグのままだと Pod は作り直されない)
gitops_deployer_image := "kind.local/home-k8s-gitops-deployer:0.1.0"
# git-activity の GitHub の fine-grained PAT (GITHUB_TOKEN=<PAT> の 1 行)。人が作って置く。Git の外 (WSL2 ホスト側、本人だけが読める)
gitops_deployer_github_file := env_var("HOME") + "/.config/home-k8s/gitops-deployer-github.env"

# イメージを build し、kind のクラスタに入れる (docker の層のキャッシュが効くので、変更が無ければすぐ終わる)
_gitops-deployer-image:
    docker build -t {{gitops_deployer_image}} services/gitops-deployer
    kind load docker-image {{gitops_deployer_image}} --name {{kind_cluster}}

# GitHub のトークン (無ければ警告だけ) と、testkube の E2E の Keycloak の client の Secret を作る。_keycloak-secrets の後に打つ
_gitops-deployer-secrets:
    bash just/gitops-deployer-secrets.sh "{{gitops_deployer_github_file}}" "{{keycloak_secret_dir}}/clients" {{kube_context}}
