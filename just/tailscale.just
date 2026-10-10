# Tailscale の Kubernetes operator の Secret の作成。公開レシピの `just up` (justfile) が呼ぶ内部用。
# operator と UI の Ingress は ArgoCD が同期する (clusters/kind/argocd/apps/tailscale-*.yaml)。構成と人の画面操作は docs/cluster/tailscale.md。

# operator の OAuth client (client_id=・client_secret= の行。tailnet= の行もあってよい)。Git の外 (WSL2 ホスト側、本人だけが読める) に置く。
# 無ければ `just up` は止まる。~/.local/share/home-k8s は just up (コンテナの root) が作るので人が書けないことがあり、人が置くファイルは ~/.config/home-k8s に置く
tailscale_oauth_file := env_var("HOME") + "/.config/home-k8s/tailscale-operator.env"

# 上のファイルから Secret operator-oauth (namespace tailscale) を作る。ArgoCD が operator を同期する前に打つ
_tailscale-secrets:
    bash just/tailscale-secrets.sh "{{tailscale_oauth_file}}" {{kube_context}}
