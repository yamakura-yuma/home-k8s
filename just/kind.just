# kind (kubeadmベースのマルチノードクラスタ) の操作。
# kind自体は試験で問われないため自動化するが、kubectlは試験で問われるため
# justではラップせず、素手で `kubectl --context kind-study-kind ...` を叩くこと。
# 公開レシピの `just up` (justfile) が呼ぶ内部用。クラスタの削除は `just down`。

# クラスタを起動する (control-plane x1 + worker x2)
_kind-up:
    # API server の OIDC の設定 (clusters/kind/kube-apiserver) を、control-plane の extraMounts の元に置く。クラスタを作る前に要る
    # (無いと API server が起動しない)。毎回置き直し、このディレクトリを見せているクラスタの API server はこの変更を読み直す
    # (extraMounts を足す前に作ったクラスタはノードにコピーを置いてある。docs/cluster/kube-oidc.md)
    mkdir -p "$HOME/.local/share/home-k8s/kube-apiserver" && cp -r clusters/kind/kube-apiserver/. "$HOME/.local/share/home-k8s/kube-apiserver/"
    # kind は設定ファイル中の環境変数を展開しないので、extraMounts の ${HOME} をここで埋める。
    # 作成済みなら作らない (`just up` を打ち直せるように)。
    if kind get clusters | grep -qx {{kind_cluster}}; then echo "kind のクラスタ {{kind_cluster}} は作成済み"; else sed 's|${HOME}|'"$HOME"'|g' clusters/kind/kind-config.yaml | kind create cluster --name {{kind_cluster}} --config -; fi
    # local の PV (clusters/kind/storage) はディレクトリが先にあることを前提にする (無いと Pod が起動しない)。
    # extraMounts の下に作れば、作り直さずにノードからも見える (docs/cluster/persistence.md)
    mkdir -p "$HOME"/.local/share/home-k8s/observability/{tempo,prometheus,loki,grafana,keycloak-postgres,keycloak-postgres-dump}
    # 開発用コンテナはrootで動いており kubeconfig も root:root/600 で書かれるため、
    # ホスト側ユーザー(や他リポジトリ)からも読めるように緩める。
    chmod 644 "$HOME/.kube/config"
