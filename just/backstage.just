# Backstage のイメージの build と kind load。公開レシピの `just up` (justfile) が呼ぶ内部用。
# Backstage 自体は ArgoCD が同期する (clusters/kind/argocd/apps/backstage.yaml)。構成は docs/cluster/backstage.md。

# repo の backstage/ から作るイメージ。レジストリに置かず kind load するので、タグは固定して
# clusters/kind/backstage/values.yaml の image と揃える。アプリを変えたらタグを上げる (同じタグのままだと Pod は作り直されない)
backstage_image := "home-k8s-backstage:0.9.0"

# Backstage のイメージを build し、kind のクラスタに入れる。
# build は docker の層のキャッシュが効くので、変更が無ければすぐ終わる。
# kind load は毎回入れ直す (数秒)。タグが同じなら Deployment は変わらないので、Pod は作り直されない
_backstage-image:
    docker build -t {{backstage_image}} backstage
    kind load docker-image {{backstage_image}} --name {{kind_cluster}}

# Azure のタブの資格情報 (読み取り用のサービスプリンシパル) のファイル。Git の外 (WSL2 ホスト側) に置く。
# 無くても `just up` は失敗しない (Azure のタブは「資格情報が無い」を出す)。置き方は docs/cluster/backstage-azure.md
backstage_azure_file := env_var("HOME") + "/.local/share/home-k8s/backstage/azure.env"

# 上のファイルから Secret backstage-azure (namespace backstage) を作る。ファイルが無ければ作らず (あれば消し)、失敗しない。
# `just up` が観測スタックの Secret の後に打つ。Backstage の chart が環境変数 AZURE_* にする (optional)
_backstage-azure-secret:
    bash just/backstage-azure-secret.sh "{{backstage_azure_file}}" {{kube_context}}
