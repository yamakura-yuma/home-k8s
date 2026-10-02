# grafana.com の公開ダッシュボード (Kubernetes・OpenTelemetry・Claude Code)

自作のダッシュボード (フォルダ無しの `Claude Code usage` など、フォルダ「Claude Code 設定項目別」) とは
別に、grafana.com で配られているダッシュボードを 8 枚入れている。自作のものには手を入れず、
フォルダも provider も分けている。どれも JSON を手で直さず、上流の版 (rev) を固定して取り込む。

| フォルダ | ID | rev | 名前 | 読むメトリクス |
|---|---|---|---|---|
| grafana.com / Kubernetes | [15757](https://grafana.com/grafana/dashboards/15757) | 43 | Kubernetes / Views / Global | `kube_*`・`node_*`・`container_*`・`machine_*` |
| | [15758](https://grafana.com/grafana/dashboards/15758) | 46 | Kubernetes / Views / Namespaces | `kube_*`・`container_*` |
| | [15759](https://grafana.com/grafana/dashboards/15759) | 40 | Kubernetes / Views / Nodes | `kube_node_info`・`node_*`・`container_*` |
| | [15760](https://grafana.com/grafana/dashboards/15760) | 41 | Kubernetes / Views / Pods | `kube_pod_*`・`container_*` |
| | [15761](https://grafana.com/grafana/dashboards/15761) | 21 | Kubernetes / System / API Server | `apiserver_*` |
| | [15762](https://grafana.com/grafana/dashboards/15762) | 22 | Kubernetes / System / CoreDNS | `coredns_*` |
| grafana.com / OpenTelemetry | [15983](https://grafana.com/grafana/dashboards/15983) | 31 | OpenTelemetry Collector | `otelcol_*` |
| grafana.com / Claude Code | [25255](https://grafana.com/grafana/dashboards/25255) | 10 | Claude Code Metrics (Prometheus) | `claude_code_*` |

Kubernetes の 6 枚は dotdc/grafana-dashboards-kubernetes。どのダッシュボードも概要 (description) の
末尾に `Source: <元 URL> (rev <版>)` がある。

## メトリクスの出どころ

ダッシュボードを入れるだけでは No data になる。Prometheus は以前 Claude Code の OTLP しか
受けておらず、クラスタのスクレイプは 0 件、Collector 自身のメトリクスも誰も取っていなかった。
いまは次のように入れている (`clusters/kind/observability/prometheus-values.yaml`、
`otel-collector-values.yaml`)。

| メトリクス | 出す部品 | 取り方 |
|---|---|---|
| `kube_*` | kube-state-metrics (prometheus chart のサブチャート) | スクレイプ `kubernetes-service-endpoints` |
| `node_*` | prometheus-node-exporter (同上、DaemonSet) | 同上 |
| `coredns_*` | kind の CoreDNS (Service `kube-dns` に `prometheus.io/scrape` 注釈が最初からある) | 同上 |
| `container_*`・`machine_*` | kubelet の cAdvisor | スクレイプ `kubernetes-nodes-cadvisor` |
| `apiserver_*` | API サーバー | スクレイプ `kubernetes-api-servers` |
| `otelcol_*` | OTel Collector 自身 | Collector が OTLP で Prometheus の OTLP 受信に送る (10 秒ごと) |

ダッシュボードに合わせて足した設定が 3 つある。

- cAdvisor の系列に `node` ラベルを足す (`post_relabel_configs`)。dotdc は `container_*` と
  `machine_*` をノード名で絞るが、kubelet も chart の既定の relabel もこのラベルを付けない。
- kube-state-metrics の `metricLabelsAllowlist` に Deployment・StatefulSet・DaemonSet・
  NetworkPolicy を挙げる。`kube_<リソース>_labels` は挙げたリソースにしか出ず、15758 はこれで数を数える。
- Collector の送信間隔を 10 秒にする。15983 のパネルは最小間隔 10 秒で `$__rate_interval` が
  40 秒になり、既定の 60 秒ごとでは窓に点が 1 つしか入らず `rate()` が空になる。

dotdc は全クエリに `cluster="$cluster"` を付けるが、この Prometheus の系列には `cluster` ラベルが無い。
変数 `cluster` は候補 0 件で空になり、`cluster=""` は「ラベルが無い系列」に一致するので、そのまま埋まる。

Collector の名前は Prometheus の OTLP 変換 (`UnderscoreEscapingWithSuffixes`) で `_total` や
`_seconds` が付く (`otelcol_process_uptime_seconds_total` など)。15983 は接尾辞を変数で判定するので
そのまま読める。`job` は Collector の `service.name` (`otelcol-k8s`)、`instance` は
`service.instance.id` になる。

## ファイルと Grafana への入り方

```
clusters/kind/observability/dashboards/grafana-com/
├── manifest.json     群 → フォルダ名と、名前 → ID・rev・元 URL (必要なら uid)
├── fetch.py          manifest の rev を取ってきて正規化し、群のディレクトリに書く
├── test_fetch.py     正規化のテスト
├── kubernetes/*.json     (6 枚)
├── opentelemetry/*.json  (1 枚)
└── claude-code/*.json    (1 枚)
```

`dashboards/kustomization.yaml` が群のディレクトリの JSON を ConfigMap `grafana-dashboards-grafana-com-<群>` にし
(ArgoCD の Application grafana が同期する)、chart の `dashboardsConfigMaps` が provider のディレクトリにマウントする。
`grafana-values.yaml` の provider `grafana-com-<群>` がフォルダ「grafana.com / <群>」に置く。
群ごとに分けているのは、ConfigMap が 1 MiB までだから (Kubernetes の 6 枚で約 480 KB)。

fetch.py の正規化は次のとおり。grafana.com の JSON は UI のインポート画面向けの書き出し形式で、
file provisioning はそのままでは読めない部分がある。

- `__inputs`・`__requires`・`__elements` を消す。`${DS_PROMETHEUS}` のような `__inputs` の参照は
  インポート画面が置き換えるもので、file provisioning は解決しない。リテラルの UID (`prometheus`) に置き換える。
- `id` を null にする。`uid` は上流のまま。25255 は上流の `uid` が空なので、manifest の
  `uid` (`gcom-claude-code-metrics`) を使う。空のままだと Grafana が uid を振り、`grafana.db` を
  作り直すたびに変わって URL が切れる。
- `datasource` 型の変数 (dotdc・15983 が使う) の選択を Prometheus にそろえる。
- description の末尾に元リンクと rev を足す。

## 更新手順

1. grafana.com の各ページ (Revisions) で新しい rev を確かめ、`manifest.json` の `rev` を上げる。
   足す・外すときも manifest を直す (外したものの JSON は fetch.py が消す)。
2. 取ってきて正規化する。同じ rev なら何度打っても同じ JSON になる。
   ```sh
   python3 clusters/kind/observability/dashboards/grafana-com/fetch.py
   python3 -B -m unittest discover -s clusters/kind/observability/dashboards/grafana-com
   ```
3. `git diff` で上流の変更を読む (クエリのメトリクス名・ラベル・変数が変わっていないか)。
4. JSON を足したり消したりしたら `dashboards/kustomization.yaml` の `files` も直し、
   `python3 -B -m unittest discover -s clusters/kind/observability/dashboards -p test_kustomization.py` で確かめる。
5. `main` に入れると ArgoCD が反映する。Grafana で開いて確かめる。

群を足すときは、manifest に群を足し、`grafana-values.yaml` に provider と `dashboardsConfigMaps` を、
`dashboards/kustomization.yaml` に ConfigMap を、`test_kustomization.py` の `GROUPS` に群を足す。

## 空になるパネル

2026-10-01 に確かめた結果。「事象待ち」は、その事象がいま起きていないだけで、起きれば出る。

| ダッシュボード | パネル | 理由 |
|---|---|---|
| 15757・15758・15760 | OOM Events・Container Restarts・Pods Status Reason・Pods unexpected status・Pods with Container Issues・Unscheduled Pods | 事象待ち |
| 15757・15759 | CPU Core Throttled | 事象待ち (`node_cpu_core_throttles_total` が 0 のまま) |
| 15758・15759 | Persistent Volumes の 3 枚 | `kubelet_volume_stats_*` は kubelet 自身のメトリクスで、スクレイプ `kubernetes-nodes` が要る。スクレイプ `kubernetes-nodes` は切っているので入れていない (PVC は [persistence.md](../cluster/persistence.md) で使うようになった) |
| 15760 | Information 行 (Created by・Running on など) | 変数 `pod` が All のときは空。Pod を 1 つ選ぶと出る (上流の作り) |
| 15762 | DNS Errors | 事象待ち (SERVFAIL・REFUSED の転送が無い) |
| 15983 | Service Instance Details、Processors 1 段目 (incoming/outgoing items の Spans・Metric Points・Log Records) | 変数 `divider` が `service_instance_id` ラベルを前提にしている。OTLP で入れると `instance` になり、このラベルが無い |
| 15983 | (空にはならない) Processors 2 段目の accepted/refused/dropped | Collector 0.161 は `otelcol_processor_accepted_*` などを出さないが、同じパネルの正規表現のクエリが `otelcol_processor_memory_limiter_accepted_*` を拾うので埋まる |
| 15983 | enqueue_failed・send_failed・batch_size_trigger_send、Filter processors・Kubernetes 行 | 事象待ち、またはこの構成に部品 (filter・k8sattributes processor) が無い |
| 25255 | Sessions・Top Sessions by Cost・Sessions by Terminal (2026-10-15 ごろまで 1 つ多い) | 空にはならない。2026-10-01 に `OTEL_METRICS_INCLUDE_SESSION_ID` を `true` にしてから、`session_id` ごとに正しく数える。ただし切り替え前の系列には `session_id` が無く、期間が切り替えの時刻をまたぐと、それらがまとめて空の `session_id` の「1 セッション」として加わる (Top Sessions by Cost では大きな 1 行)。切り替えの前から動いていたセッションには、`claude_code_session_count_total` は `session_id` 無しで、コスト・トークンは `session_id` 付きで出ているものがある (2026-10-01 に 3 件。理由は未確認)。そうしたセッションは Sessions では空の 1 つに入り、Top Sessions by Cost では `session_id` 付きの行にも出る。保持期間が 14 日なので 2026-10-15 ごろに消える。Prometheus の系列は消していない |
| 25255 | Top Users | 利用者が 1 人なので 1 本だけ |
