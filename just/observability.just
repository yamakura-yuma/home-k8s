# 観測スタック (OTel Collector + Tempo + Prometheus + Loki + Grafana) の導入/削除。
# 構成と使い方は docs/observability/claude-code-traces.md。
# helm の操作は試験範囲ではないため自動化するが、状態の確認は kubectl を素手で叩くこと。

observe_ns := "observability"
observe_values := "clusters/kind/observability"
# Grafana の admin パスワード。リポジトリの外 (WSL2 ホスト側) に置き、2 回目以降は再利用する
grafana_password_file := env_var("HOME") + "/.local/share/home-k8s/observability/grafana-admin-password"
# observe-share の cloudflared と caddy のログ (起動のたびに上書きする)
share_log_file := env_var("HOME") + "/.local/share/home-k8s/observability/observe-share.log"
# observe-share が動いている間だけ置く、公開 URL と cloudflared の pid (observe-show-connection が読む)
share_state_file := env_var("HOME") + "/.local/share/home-k8s/observability/observe-share.state"

# 観測スタックを入れる (既にあれば values を反映し直す)
observe-up: _grafana-admin-secret
    helm upgrade --install tempo tempo \
        --repo https://grafana-community.github.io/helm-charts --version 3.0.0 \
        --kube-context kind-study-kind --namespace {{observe_ns}} --create-namespace \
        --values {{observe_values}}/tempo-values.yaml --wait
    helm upgrade --install prometheus prometheus \
        --repo https://prometheus-community.github.io/helm-charts --version 29.35.0 \
        --kube-context kind-study-kind --namespace {{observe_ns}} \
        --values {{observe_values}}/prometheus-values.yaml --wait
    helm upgrade --install loki loki \
        --repo https://grafana-community.github.io/helm-charts --version 18.13.7 \
        --kube-context kind-study-kind --namespace {{observe_ns}} \
        --values {{observe_values}}/loki-values.yaml --wait
    helm upgrade --install otel-collector opentelemetry-collector \
        --repo https://open-telemetry.github.io/opentelemetry-helm-charts --version 0.174.0 \
        --kube-context kind-study-kind --namespace {{observe_ns}} \
        --values {{observe_values}}/otel-collector-values.yaml --wait
    helm upgrade --install grafana grafana \
        --repo https://grafana-community.github.io/helm-charts --version 13.2.7 \
        --kube-context kind-study-kind --namespace {{observe_ns}} \
        --values {{observe_values}}/grafana-values.yaml \
        --set-file 'dashboards.default.claude-code-traces.json={{observe_values}}/dashboards/claude-code-traces.json' \
        --set-file 'dashboards.default.claude-code-usage.json={{observe_values}}/dashboards/claude-code-usage.json' \
        --set-file 'dashboards.default.claude-code-improve.json={{observe_values}}/dashboards/claude-code-improve.json' --wait

# 観測スタックを消す (保存済みのトレース・メトリクス・ログはホスト側に残る)
observe-down:
    -helm uninstall grafana otel-collector loki prometheus tempo --kube-context kind-study-kind --namespace {{observe_ns}} --wait

# いまの Grafana の接続先 (URL・ユーザー・パスワード) を表示する。
# observe-share が動いていれば公開 URL、止まっていれば localhost:3000
observe-show-connection:
    @bash just/observe-show-connection.sh "{{grafana_password_file}}" "{{share_state_file}}"

# Grafana (localhost:3000) だけを Cloudflare Quick Tunnel で一時的に公開する。
# URL が開けるようになってから URL とパスワードを表示する。Ctrl-C で止めれば URL は無効になる
observe-share:
    @bash just/observe-share.sh "{{grafana_password_file}}" "{{share_log_file}}" "{{share_state_file}}"

# admin パスワードを (無ければ生成して) Secret grafana-admin に入れる。
# 中身が同じなら apply は何も変えないので、Grafana の Pod は再起動しない
_grafana-admin-secret:
    bash just/grafana-admin-secret.sh "{{grafana_password_file}}" {{observe_ns}} "{{justfile_directory()}}"
