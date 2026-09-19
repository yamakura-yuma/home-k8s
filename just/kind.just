# kind (kubeadmベースのマルチノードクラスタ) の操作。
# kind自体は試験で問われないため自動化するが、kubectlは試験で問われるため
# justではラップせず、素手で `kubectl --context kind-study-kind ...` を叩くこと。

# クラスタを起動する (control-plane x1 + worker x2)
kind-up:
    kind create cluster --name study-kind --config clusters/kind/kind-config.yaml
    # 開発用コンテナはrootで動いており kubeconfig も root:root/600 で書かれるため、
    # ホスト側ユーザー(や他リポジトリ)からも読めるように緩める。
    chmod 644 "$HOME/.kube/config"

# クラスタを削除する
kind-down:
    kind delete cluster --name study-kind
