# 観測スタック (OTel Collector + Tempo + Prometheus + Loki + Grafana) の Secret と、Grafana の接続先の表示・共有の変数。
# 表示と共有は公開レシピの `just show`・`just share` (justfile)、Secret は `just up` が呼ぶ内部用。
# 観測スタック自体は ArgoCD が同期する (`just up`、docs/cluster/argocd.md)。
# 構成と使い方は docs/observability/claude-code-traces.md。状態の確認は kubectl を素手で叩くこと。

observe_ns := "observability"
# Grafana の admin パスワード。リポジトリの外 (WSL2 ホスト側) に置き、2 回目以降は再利用する
grafana_password_file := env_var("HOME") + "/.local/share/home-k8s/observability/grafana-admin-password"
# 共有相手に渡す閲覧用ユーザー viewer のパスワード。`just share` が起動のたびに作り直す
grafana_viewer_password_file := env_var("HOME") + "/.local/share/home-k8s/observability/grafana-viewer-password"
# `just share` の cloudflared と caddy のログ (起動のたびに上書きする)
share_log_file := env_var("HOME") + "/.local/share/home-k8s/observability/observe-share.log"
# `just share` が動いている間だけ置く、公開 URL と cloudflared の pid (`just show grafana` が読む)
share_state_file := env_var("HOME") + "/.local/share/home-k8s/observability/observe-share.state"

# admin と viewer のパスワードを (無ければ生成して) Secret grafana-admin と grafana-viewer に入れる。
# `just up` が先に打つ。中身が同じなら apply は何も変えないので、Grafana の Pod は再起動しない
_grafana-secrets:
    bash just/grafana-secrets.sh "{{grafana_password_file}}" "{{grafana_viewer_password_file}}" {{observe_ns}} "{{justfile_directory()}}" {{kube_context}}
