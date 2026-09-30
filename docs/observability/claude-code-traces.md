# Claude Code のトレースを見る観測スタック

WSL2 上の Claude Code が OpenTelemetry (OTLP) で出すトレースを kind クラスタで受け、
Grafana でスパンの親子関係 (サブエージェントを含む) として見るための構成をまとめる。
オールインワンイメージ (`grafana/otel-lgtm`) は使わず、OTel Collector・Tempo・Grafana
を別々の Helm chart で入れている。本番の構成に近い形で、部品ごとに差し替えて学べる。

## 構成

```
WSL2 ホスト
  claude (OTLP/HTTP)           ブラウザ
     │ localhost:4318              │ localhost:3000
     │ (gRPC は localhost:4317)     │
─────┼─────────────────────────────┼──────────────── kind: extraPortMappings
     ▼                             ▼                  (control-plane ノード)
  NodePort 30318 / 30317       NodePort 30300
     │                             │
  namespace observability          │
     ▼                             ▼
  OTel Collector ──OTLP/gRPC──▶ Tempo ◀──HTTP:3200── Grafana
  (Deployment)                 (StatefulSet,         (データソースは
                                study-kind-worker     provisioning で登録)
                                に固定)
                                  │ /var/tempo (hostPath)
─────────────────────────────────┼──────────────── kind: extraMounts
                                  ▼                  (study-kind-worker ノード)
                     ~/.local/share/home-k8s/observability/tempo
                                  (WSL2 ホストのディレクトリ)
```

| コンポーネント | chart | version | values |
|---|---|---|---|
| OTel Collector | `open-telemetry/opentelemetry-collector` | 0.174.0 | `clusters/kind/observability/otel-collector-values.yaml` |
| Tempo (単一バイナリ) | `grafana-community/tempo` | 3.0.0 | `clusters/kind/observability/tempo-values.yaml` |
| Grafana | `grafana-community/grafana` | 13.2.7 | `clusters/kind/observability/grafana-values.yaml` |

Tempo と Grafana の chart は `grafana/helm-charts` から `grafana-community/helm-charts`
に移っている。旧リポジトリの `grafana/tempo` は Tempo 2.9 で止まっているため使わない。

Collector はトレースだけを Tempo に中継する。メトリクスとログも受け取るが、保存先が
無いので `debug` exporter に流して捨てている。

## 手順

開発用コンテナ (`just devcontainer shell`) の中で実行する。

```sh
just kind-up        # クラスタ作成 (受け口のポートと保存先のマウントも作られる)
just observe-up     # Tempo → OTel Collector → Grafana の順に helm upgrade --install
kubectl --context kind-study-kind -n observability get pods -o wide
just observe-down   # 観測スタックを消す (トレースはホストに残る)
```

`observe-up` は `helm upgrade --install` なので、values を変えたあとに打ち直せば反映される。
chart のリポジトリは `--repo` で直接指定しており、`helm repo add` は不要。

Grafana は <http://localhost:3000> で開く。Explore でデータソース `Tempo` を選び、
TraceQL で `{resource.service.name="claude-code"}` や `{name="claude_code.interaction"}`
と打つと一覧が出る。

### 動作確認用のスパンを送る

WSL2 ホストから curl で OTLP/JSON を送る例。`traceId` は 16 バイト、`spanId` は 8 バイトの
16 進文字列にする。

```sh
tid=$(openssl rand -hex 16); pid=$(openssl rand -hex 8); now=$(date +%s%N)
curl -sS -H 'Content-Type: application/json' http://localhost:4318/v1/traces -d '{
  "resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"smoke"}}]},
  "scopeSpans":[{"spans":[{"traceId":"'$tid'","spanId":"'$pid'","name":"parent","kind":1,
  "startTimeUnixNano":"'$((now-1000000000))'","endTimeUnixNano":"'$now'"}]}]}]}'
curl -sS http://localhost:3000/api/datasources/proxy/uid/tempo/api/v2/traces/$tid
```

gRPC 側は telemetrygen で確かめられる。

```sh
docker run --rm --network host ghcr.io/open-telemetry/opentelemetry-collector-contrib/telemetrygen:latest \
  traces --otlp-insecure --otlp-endpoint localhost:4317 --traces 1
```

ID を指定した取得はすぐに成功するが、検索 (TraceQL) に出るまでは 10〜20 秒ほどかかる。

## Claude Code 側の環境変数

```sh
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1   # トレース (スパン) の出力を有効にする
export OTEL_TRACES_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

この状態で `claude -p "hello"` を実行すると、`claude_code.interaction` をルートに
`claude_code.llm_request` が子スパンとしてぶら下がったトレースが Tempo に入る。

`claude_code.hook` スパンは上の設定だけでは出ない。Claude Code 2.1.285 では hook の
スパンを詳細トレース側でしか作っておらず、次の 2 つも要る。

```sh
export ENABLE_BETA_TRACING_DETAILED=1
export BETA_TRACING_ENDPOINT=http://localhost:4318
```

詳細トレースでは llm_request に `system_prompt_hash` や `tools` (ツール名とハッシュ) が
増える。プロンプト本文は伏せ字のままだった (2.1.285 で確認)。

## 永続化の仕組み

トレースはオブジェクトストレージではなく、Tempo の `local` backend でファイルとして
保存する。保存先は次の 3 段でつながっている。

1. `clusters/kind/kind-config.yaml` の `extraMounts` で、WSL2 ホストの
   `~/.local/share/home-k8s/observability` を `study-kind-worker` ノード (Docker コンテナ)
   の `/var/local/home-k8s/observability` にマウントする。kind は設定ファイル中の環境変数を
   展開しないため、`${HOME}` は `just kind-up` が sed で埋めてから `kind create cluster --config -`
   に渡している。開発用コンテナは docker.sock を共有しており、ノードは WSL2 の Docker 上で
   動くので、パスは WSL2 ホスト側のものになる。
2. 同じノードに `home-k8s/observability-storage: "true"` のラベルを付け、Tempo の
   `nodeSelector` でそのノードに固定する。
3. Tempo の Pod は hostPath `/var/local/home-k8s/observability/tempo` を `/var/tempo` に
   マウントし、`traces/` (ブロック)、`wal/`、`live-store/` をそこに書く。

クラスタを消しても 1 のホスト側ディレクトリは残るため、`just kind-down` → `just kind-up`
→ `just observe-up` のあとも同じトレースを引ける。保持期間は 14 日
(`tempo.retention: 336h`) で、それより古いブロックは Tempo が消す。

hostPath は root 所有で作られるため、initContainer が Tempo の実行ユーザー (UID 10001)
に chown してから Tempo を起動する。その結果、ホスト側の `tempo/` 以下も UID 10001 の
所有になる。トレースを全部消したいときは `sudo rm -rf ~/.local/share/home-k8s/observability/tempo`
とする。

後で MinIO を立てて Tempo の backend を `s3` に切り替えると、オブジェクトストレージに
保存する本番に近い構成を練習できる。

## Grafana の認証

ローカル専用で、ポートも `127.0.0.1` にしか bind していないため、匿名アクセスを
Admin 権限で有効にし、ログイン画面を出さない設定にしている
(`grafana.ini` の `auth.anonymous` と `auth.disable_login_form`)。chart が作る admin
ユーザーは使わないが、パスワードを固定 (`admin`) しておかないと `observe-up` のたびに
乱数で作り直されて Pod が再起動するため、values で指定している。

データソース `Tempo` (uid `tempo`) は `datasources` の provisioning で登録しており、
UI からは編集できない。変えるときは `grafana-values.yaml` を直して `just observe-up`
を打ち直す。Grafana 自体は永続化していないので、UI で作ったダッシュボードは Pod の
再起動で消える。

## ダッシュボード

`just observe-up` で Grafana にダッシュボード「Claude Code traces」
(<http://localhost:3000/d/claude-code-traces>) が入る。定義は
`clusters/kind/observability/dashboards/claude-code-traces.json` で、UI で直しても Pod の
再起動で消えるので、変えるときは JSON を編集して `just observe-up` を打ち直す。

### 読み込みの仕組み

chart の `dashboardProviders` で `/var/lib/grafana/dashboards/default` を読むプロバイダを
作り、`observe-up` が `--set-file dashboards.default.claude-code-traces.json=<JSON>` で
JSON を values に流し込む。chart はそれを ConfigMap にしてそのディレクトリにマウントする。
sidecar (ラベル付き ConfigMap を拾うコンテナ) は、ダッシュボードが 1 枚で ConfigMap を
自分で書く必要も無いので使っていない。values に JSON を直書きしないのは、ファイルのままなら
Grafana の Export / Import とそのまま行き来できるため。

### 集計の方式

パネルはすべて Tempo に TraceQL metrics (`count_over_time()`、`quantile_over_time()`、
`sum_over_time()`) を投げて、保存済みのトレースからその場で集計する。metrics-generator
(スパンから Prometheus 形式のメトリクスを作って remote write する機能) は使わない。

- metrics-generator の出力先となる Prometheus がこのスタックに無い。
- Tempo 3.0 の単一バイナリ構成は、追加の設定なしで TraceQL metrics に答えられる
  (local-blocks プロセッサの有効化は不要だった)。
- 件数が少ない (1 日に数十トレース) ので、クエリのたびに全スパンを読んでも速い。

変えた設定は 1 つで、Tempo の `query_frontend.metrics.max_duration` を既定の 24h から
168h に広げた (`tempo-values.yaml`)。既定のままだと時間範囲を 24h より長くすると
`metrics query time range exceeds the maximum allowed duration` で失敗する。

### 属性名の注意

Claude Code はスパンの種類を `span.type` という名前の属性で出している。TraceQL の
`span.type` は「スパンスコープの属性 `type`」と解釈されるため一致しない。この属性を
使うなら `span."span.type"` と書く必要がある。ダッシュボードでは代わりにスパン名
(`name`) で絞っている。

実データで伏せ字でなく使えた属性は次のとおり。

| スパン | 使える属性 |
|---|---|
| interaction | `user_prompt_length`、`interaction.sequence` (`user_prompt` は `<REDACTED>`) |
| llm_request | `model`、`input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_creation_tokens`、`success`、`attempt`、`ttft_ms`、`stop_reason`、`query_source_safe`、`agent_id` (サブエージェント内のみ) |
| tool | `tool_name`、`tool_name_safe` (MCP ツールは `mcp_other`)、`bash_command_class`、`bash_argv0` |
| tool.blocked_on_user | `decision` (accept / reject / unknown)、`source` |
| tool.execution | `success` |
| hook | `hook_event`、`hook_name` (`PreToolUse:Bash` のようにマッチャー付き)、`num_hooks`、`num_success`、`num_blocking` |

### パネルと TraceQL

時系列パネルは range クエリ、棒グラフと数値のパネルは instant クエリ (期間全体を 1 つの値に
まとめる) にしている。

| # | パネル | TraceQL |
|---|---|---|
| 1 | 依頼の件数 | `{name="claude_code.interaction"} \| count_over_time()` |
| 2 | 依頼と LLM リクエストの所要時間 | `{name=~"claude_code.(interaction\|llm_request)"} \| quantile_over_time(duration, .5, .95) by (name)` |
| 3 | モデル別のトークン量 | `{name="claude_code.llm_request"} \| sum_over_time(span.input_tokens) by (span.model)` (output / cache_read / cache_creation も同形) |
| 3 | キャッシュ読み出し割合 | 上の input / cache_read / cache_creation を instant で取り、Grafana の式で `cache_read / (input + cache_read + cache_creation)` |
| 4 | ツール別の呼び出し回数 | `{name="claude_code.tool"} \| count_over_time() by (span.tool_name)` |
| 4 | ツール別の所要時間 | `{name="claude_code.tool"} \| quantile_over_time(duration, .95) by (span.tool_name)` |
| 4 | 許可待ちと実行 | `{name="claude_code.tool" && span.tool_name=~"$tool"} > {name=~"claude_code.tool.(blocked_on_user\|execution)"} \| quantile_over_time(duration, .95) by (name)` |
| 5 | hook 別の所要時間 | `{name="claude_code.hook"} \| quantile_over_time(duration, .95) by (span.hook_name)` |
| 6 | サブエージェントの起動数・所要時間 | `{name="claude_code.tool" && span.tool_name=~"Agent\|Task"} \| count_over_time()` (所要時間は `quantile_over_time(duration, .5, .95)`) |
| 6 | 種別ごとの LLM リクエスト数 | `{name="claude_code.llm_request" && span.agent_id != nil} \| count_over_time() by (span.query_source_safe)` |
| 7 | 最近の依頼 | `{name="claude_code.interaction"}` (Table 表示の検索結果) |
| 7 | サブエージェントを含む依頼 | `{name="claude_code.tool" && span.tool_name=~"Agent\|Task"}` |

見方の補足。

- 4 の許可待ちと実行: 子スパン (`blocked_on_user`、`execution`) は `tool_name` を持たない。
  親子演算子 `>` で親の `tool` スパンを条件にし、上部の変数「ツール」で絞る。許可が設定
  (`source=config`) で自動に通る場合、許可待ちは数ミリ秒になる。
- 6 のサブエージェント: Agent ツールのスパンの下 (`tool` → `tool.execution`) にサブエージェント
  の `llm_request` と `tool` がぶら下がる。サブエージェント内の `llm_request` と `tool` には
  `agent_id` が付き、`query_source_safe` が `agent.builtin.Explore` のように種別を表す。
- 7 の表: Trace ID をクリックすると、同じ Tempo データソースでトレースのウォーターフォールが開く。
- 3 のトークン量: 種類ごとに 1 パネルに分けている。Tempo データソースは返すフレームの
  refId を系列名 (モデル名) で上書きするため、1 パネルに複数クエリを置くと Grafana の
  「クエリごとに名前を付ける」上書き (`byFrameRefID`) が効かず、どの系列がどのトークンか
  区別できなくなる。
- `quantile_over_time()` の値は Tempo が 2 のべき乗のバケットで近似するため、概算になる
  (たとえば 0.537 s や 68.7 s のような値が出る)。
