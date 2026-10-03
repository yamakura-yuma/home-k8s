# 観測スタック (OTel Collector + Tempo + Prometheus + Loki + Grafana) の Secret と、Grafana の接続先の表示・共有の変数。
# 表示は公開レシピの `just show` (justfile)、Secret は `just up` が呼ぶ内部用。共有は常駐する share Pod (docs/cluster/share.md)。
# 観測スタック自体は ArgoCD が同期する (`just up`、docs/cluster/argocd.md)。
# 構成と使い方は docs/observability/claude-code-traces.md。状態の確認は kubectl を素手で叩くこと。

observe_ns := "observability"
# Grafana の admin パスワード。リポジトリの外 (WSL2 ホスト側) に置き、2 回目以降は再利用する
grafana_password_file := env_var("HOME") + "/.local/share/home-k8s/observability/grafana-admin-password"
# 閲覧用ユーザー viewer のパスワード。無ければ `just up` が作り、以後は再利用する。share Pod が写しを持つ (Secret share-grafana)。共有相手には渡さない
grafana_viewer_password_file := env_var("HOME") + "/.local/share/home-k8s/observability/grafana-viewer-password"
# Backstage が Grafana を読む閲覧用ユーザー backstage のパスワード。無ければ `just up` が作り、以後は再利用する
grafana_backstage_password_file := env_var("HOME") + "/.local/share/home-k8s/observability/grafana-backstage-password"

# admin・viewer・backstage のパスワードを (無ければ生成して) Secret grafana-admin・grafana-viewer・grafana-backstage と
# backstage-grafana (Backstage 側) に入れる。
# `just up` が先に打つ。中身が同じなら apply は何も変えないので、Grafana の Pod は再起動しない
_grafana-secrets:
    bash just/grafana-secrets.sh "{{grafana_password_file}}" "{{grafana_viewer_password_file}}" "{{grafana_backstage_password_file}}" {{observe_ns}} "{{justfile_directory()}}" {{kube_context}}
