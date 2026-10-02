# 観測スタック (OTel Collector + Tempo + Prometheus + Loki + Grafana) の Secret と、Grafana の接続先の表示・共有。
# 観測スタック自体は ArgoCD が同期する (`just argocd-up`、docs/cluster/argocd.md)。
# 構成と使い方は docs/observability/claude-code-traces.md。状態の確認は kubectl を素手で叩くこと。

observe_ns := "observability"
# Grafana の admin パスワード。リポジトリの外 (WSL2 ホスト側) に置き、2 回目以降は再利用する
grafana_password_file := env_var("HOME") + "/.local/share/home-k8s/observability/grafana-admin-password"
# 共有相手に渡す閲覧用ユーザー viewer のパスワード。observe-share が起動のたびに作り直す
grafana_viewer_password_file := env_var("HOME") + "/.local/share/home-k8s/observability/grafana-viewer-password"
# observe-share の cloudflared と caddy のログ (起動のたびに上書きする)
share_log_file := env_var("HOME") + "/.local/share/home-k8s/observability/observe-share.log"
# observe-share が動いている間だけ置く、公開 URL と cloudflared の pid (observe-show-connection が読む)
share_state_file := env_var("HOME") + "/.local/share/home-k8s/observability/observe-share.state"

# いまの Grafana の接続先 (URL と viewer のユーザー・パスワード) を表示する。共有相手に渡してよい。
# observe-share が動いていれば公開 URL、止まっていれば localhost:3000
observe-show-connection:
    @bash just/observe-show-connection.sh "{{grafana_viewer_password_file}}" "{{share_state_file}}"

# Grafana の admin のユーザー・パスワードを表示する。自分で編集するとき用で、共有相手には渡さない
observe-show-admin:
    @echo "  URL:        http://localhost:3000"
    @echo "  ユーザー:   admin"
    @echo "  パスワード: $(cat "{{grafana_password_file}}")"

# viewer のパスワードを作り直してから公開し、URL が開けるようになった時点で URL と viewer の資格情報を
# 表示する。Ctrl-C で止めれば URL は無効になる
# Grafana (localhost:3000) だけを Cloudflare Quick Tunnel で一時的に公開する (viewer の資格情報を表示)
observe-share:
    @bash just/observe-share.sh "{{grafana_password_file}}" "{{grafana_viewer_password_file}}" {{observe_ns}} "{{share_log_file}}" "{{share_state_file}}"

# admin と viewer のパスワードを (無ければ生成して) Secret grafana-admin と grafana-viewer に入れる。
# `just argocd-up` が先に打つ。中身が同じなら apply は何も変えないので、Grafana の Pod は再起動しない
grafana-secrets:
    bash just/grafana-secrets.sh "{{grafana_password_file}}" "{{grafana_viewer_password_file}}" {{observe_ns}} "{{justfile_directory()}}" {{kube_context}}
