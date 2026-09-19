# kind (kubeadmベースのマルチノードクラスタ) の操作。
# kind自体は試験で問われないため自動化するが、kubectlは試験で問われるため
# justではラップせず、素手で `kubectl --context kind-study-kind ...` を叩くこと。

# クラスタを起動する (control-plane x1 + worker x2)
kind-up:
    kind create cluster --name study-kind --config clusters/kind/kind-config.yaml

# クラスタを削除する
kind-down:
    kind delete cluster --name study-kind
