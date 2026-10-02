# kind (kubeadmベースのマルチノードクラスタ) の操作。
# kind自体は試験で問われないため自動化するが、kubectlは試験で問われるため
# justではラップせず、素手で `kubectl --context kind-study-kind ...` を叩くこと。
# 公開レシピの `just up` (justfile) が呼ぶ内部用。クラスタの削除は `just down`。

# クラスタを起動する (control-plane x1 + worker x2)
_kind-up:
    # kind は設定ファイル中の環境変数を展開しないので、extraMounts の ${HOME} をここで埋める。
    # 作成済みなら作らない (`just up` を打ち直せるように)。
    if kind get clusters | grep -qx {{kind_cluster}}; then echo "kind のクラスタ {{kind_cluster}} は作成済み"; else sed 's|${HOME}|'"$HOME"'|g' clusters/kind/kind-config.yaml | kind create cluster --name {{kind_cluster}} --config -; fi
    # 開発用コンテナはrootで動いており kubeconfig も root:root/600 で書かれるため、
    # ホスト側ユーザー(や他リポジトリ)からも読めるように緩める。
    chmod 644 "$HOME/.kube/config"
