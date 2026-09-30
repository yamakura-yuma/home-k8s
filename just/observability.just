# 観測スタック (OTel Collector + Tempo + Grafana) の導入/削除。
# 構成と使い方は docs/observability/claude-code-traces.md。
# helm の操作は試験範囲ではないため自動化するが、状態の確認は kubectl を素手で叩くこと。

observe_ns := "observability"
observe_values := "clusters/kind/observability"

# 観測スタックを入れる (既にあれば values を反映し直す)
observe-up:
    helm upgrade --install tempo tempo \
        --repo https://grafana-community.github.io/helm-charts --version 3.0.0 \
        --kube-context kind-study-kind --namespace {{observe_ns}} --create-namespace \
        --values {{observe_values}}/tempo-values.yaml --wait
    helm upgrade --install otel-collector opentelemetry-collector \
        --repo https://open-telemetry.github.io/opentelemetry-helm-charts --version 0.174.0 \
        --kube-context kind-study-kind --namespace {{observe_ns}} \
        --values {{observe_values}}/otel-collector-values.yaml --wait
    helm upgrade --install grafana grafana \
        --repo https://grafana-community.github.io/helm-charts --version 13.2.7 \
        --kube-context kind-study-kind --namespace {{observe_ns}} \
        --values {{observe_values}}/grafana-values.yaml \
        --set-file 'dashboards.default.claude-code-traces.json={{observe_values}}/dashboards/claude-code-traces.json' --wait

# 観測スタックを消す (保存済みのトレースはホスト側に残る)
observe-down:
    -helm uninstall grafana otel-collector tempo --kube-context kind-study-kind --namespace {{observe_ns}} --wait
