# Claude Code のトレースを見る観測スタック

WSL2 上の Claude Code が OpenTelemetry (OTLP) で出すトレースを kind クラスタで受け、
Grafana でスパンの親子関係 (サブエージェントを含む) として見るための構成をまとめる。
同じ経路でメトリクスを Prometheus に、ログ (イベント) を Loki に入れている。
オールインワンイメージ (`grafana/otel-lgtm`) は使わず、OTel Collector・Tempo・Prometheus・
Loki・Grafana を別々の Helm chart で入れている。本番の構成に近い形で、部品ごとに差し替えて学べる。

## 構成

トレース・メトリクス・ログの 3 種類をどう見るかは
[claude-code-usage.md](claude-code-usage.md) にまとめた。ここは部品と入れ方の話。

```
WSL2 ホスト
  claude (OTLP/HTTP)                               ブラウザ
     │ localhost:4318 (gRPC は 4317)                    │ localhost:3000
─────┼──────────────────────────────────────────────────┼──── kind: extraPortMappings
     ▼                                                  ▼
  NodePort 30318 / 30317                            NodePort 30300
     │   namespace observability                        │
     ▼                                                  ▼
  OTel Collector ─ traces  ─OTLP/gRPC──▶ Tempo       ◀─┐
  (Deployment)   ─ metrics ─OTLP/HTTP──▶ Prometheus  ◀─┼── Grafana
                 ─ logs    ─OTLP/HTTP──▶ Loki        ◀─┘  (データソースと
                                          │                ダッシュボードは
                                          │                provisioning)
                     3 つとも study-kind-worker に固定し hostPath に保存
─────────────────────────────────────────┼─────────────────── kind: extraMounts
                                          ▼
                     ~/.local/share/home-k8s/observability/{tempo,prometheus,loki}
                                   (WSL2 ホストのディレクトリ)
```

| コンポーネント | chart | version | values |
|---|---|---|---|
| OTel Collector | `open-telemetry/opentelemetry-collector` | 0.174.0 | `clusters/kind/observability/otel-collector-values.yaml` |
| Tempo (単一バイナリ) | `grafana-community/tempo` | 3.0.0 | `clusters/kind/observability/tempo-values.yaml` |
| Prometheus (server のみ) | `prometheus-community/prometheus` | 29.35.0 | `clusters/kind/observability/prometheus-values.yaml` |
| Loki (単一バイナリ) | `grafana-community/loki` | 18.13.7 | `clusters/kind/observability/loki-values.yaml` |
| Grafana | `grafana-community/grafana` | 13.2.7 | `clusters/kind/observability/grafana-values.yaml` |

Tempo・Loki・Grafana の chart は `grafana/helm-charts` から `grafana-community/helm-charts`
に移っている。旧リポジトリの `grafana/tempo` は Tempo 2.9、`grafana/loki` は Loki 3.6 で
止まっているため使わない。

### メトリクスとログの受け方

Collector は 3 種類とも OTLP のまま送り先に渡す。変換はしない。

- メトリクス: Prometheus の OTLP 受信 (`--web.enable-otlp-receiver`、
  `/api/v1/otlp/v1/metrics`) に `otlphttp` exporter で送る。Collector の
  `prometheusremotewrite` exporter は使わない。使っている Collector のイメージ
  (`otel/opentelemetry-collector-k8s`) に入っておらず、contrib イメージへの差し替えが要るため。
  OTLP 受信なら Prometheus 側でリソース属性をラベルに昇格させられる
  (`otlp.promote_resource_attributes`)。`orca.worktree.name` と `orca.worktree.id` を
  昇格させ、`orca_worktree_name` / `orca_worktree_id` ラベルとして全系列に付けている。
- ログ: Loki 3 の OTLP 受信 (`/otlp`) に `otlphttp` exporter で送る。contrib の `loki`
  exporter は廃止済み。リソース属性のうち `service.name` はインデックスラベル
  (`service_name`) に、残りとログの属性 (`event.name`、`trace_id` など) は structured
  metadata になり、`| event_name="tool_result"` のように絞れる。

Prometheus には 2 つの feature flag を付けている。

- `created-timestamp-zero-ingestion`: OTLP の開始時刻に 0 の点を入れる。`claude -p` の
  ような短いセッションは点が 1〜2 個しか無く、これが無いと `increase()` が最初の送信分を
  数えない。
- `promql-extended-range-selectors`: ダッシュボードの `increase(...[$__range] anchored)` に
  使う。`anchored` は範囲の外挿をせず、範囲の端の直前の値からの差をそのまま返すので、
  コストや行数の合計が実際の値からずれない。

## 手順

ホストでもコンテナ (`just devcontainer shell`) の中でも打てる。ホストで打つと自動で開発用コンテナの中で実行される (README の「ホストで打つか、コンテナで打つか」)。

```sh
just kind-up        # クラスタ作成 (受け口のポートと保存先のマウントも作られる)
just observe-up     # Tempo → Prometheus → Loki → OTel Collector → Grafana の順に helm upgrade --install
kubectl --context kind-study-kind -n observability get pods -o wide
just observe-down   # 観測スタックを消す (トレース・メトリクス・ログはホストに残る)
```

`observe-up` は `helm upgrade --install` なので、values を変えたあとに打ち直せば反映される。
chart のリポジトリは `--repo` で直接指定しており、`helm repo add` は不要。

Grafana は <http://localhost:3000> で開き、`admin` でログインする (パスワードは下の「Grafana の認証」)。Explore でデータソース `Tempo` を選び、
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
curl -sS -u "admin:$(cat ~/.local/share/home-k8s/observability/grafana-admin-password)" \
  http://localhost:3000/api/datasources/proxy/uid/tempo/api/v2/traces/$tid
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

Prometheus と Loki も同じ仕組みで、同じノードに固定し、hostPath
`/var/local/home-k8s/observability/prometheus` と `.../loki` に書く。保持期間はどちらも
14 日 (Prometheus は `server.retention: 14d`、Loki は `limits_config.retention_period: 336h`
と compactor の `retention_enabled`)。所有者は Prometheus が UID 65534 (nobody)、Loki が
UID 10001 になる。PVC は使わない (kind の local-path は PVC を消すとデータも消えるため)。

後で MinIO を立てて Tempo の backend を `s3` に切り替えると、オブジェクトストレージに
保存する本番に近い構成を練習できる。

## Grafana の認証

Grafana はログイン必須で、匿名アクセスは付けていない (閲覧のみの匿名も無い)。
下の「別の PC から見る」で外に出すことがあるため。ユーザー名は `admin`、パスワードは
次のように決まる。

1. `just observe-up` が最初に `~/.local/share/home-k8s/observability/grafana-admin-password`
   (WSL2 ホスト側、パーミッション 600) を見る。無ければ `openssl rand` で 32 文字の値を作って
   保存し、あればそれを使う。リポジトリには置かない。
2. その値を Secret `observability/grafana-admin` (`admin-user` / `admin-password`) に
   `kubectl apply` で入れる。
3. chart の `admin.existingSecret: grafana-admin` で、Grafana はこの Secret を環境変数として読む。

chart に Secret を作らせると `observe-up` のたびに乱数で作り直されて Pod が再起動するが、
この形なら Secret の中身が変わらないので `observe-up` を打ち直しても Pod はそのまま残る。

パスワードは `just observe-show-connection` と打つと、接続先の URL と一緒に表示される。ファイルの
所有者はホストのユーザーにしてあるので、WSL2 のシェルから `cat` しても読める。
変えたいときはファイルを消して `just observe-up` を打ち、Secret が変わったあとで
`kubectl --context kind-study-kind -n observability rollout restart deploy/grafana` とする
(Grafana は永続化していないので、起動のたびに Secret の値で admin を作り直す)。

開発用コンテナは `~/.local/share/home-k8s` をホストと同じパスでマウントしている。この
マウントが無い古いコンテナで `observe-up` を打つと、パスワードがコンテナの中にだけ残らない
よう止まるので、`just devcontainer down && just devcontainer up` で作り直す。

API を curl で叩くときは Basic 認証を付ける。

```
curl -u "admin:$(cat ~/.local/share/home-k8s/observability/grafana-admin-password)" \
  http://localhost:3000/api/dashboards/uid/claude-code-traces
```

データソース `Tempo` (uid `tempo`)・`Prometheus` (uid `prometheus`)・`Loki` (uid `loki`) は
`datasources` の provisioning で登録しており、
UI からは編集できない。変えるときは `grafana-values.yaml` を直して `just observe-up`
を打ち直す。Grafana 自体は永続化していないので、UI で作ったダッシュボードは Pod の
再起動で消える。

## 別の PC から見る

Cloudflare Quick Tunnel で Grafana だけを一時的に公開する。Cloudflare のアカウントは要らず、
`https://<ランダム>.trycloudflare.com` の URL が発行される。次を打つ (ホストから打ってよい)。

```
$ just observe-share
Grafana を公開しました（Ctrl-C で停止）
  URL:        https://xxxx.trycloudflare.com
  ユーザー:   admin
  パスワード: <値>
  ログ:       /home/<you>/.local/share/home-k8s/observability/observe-share.log
```

表示された URL・ユーザー・パスワードの 3 行を別の PC に渡し、ブラウザで開いてログインする。
Ctrl-C で止めると URL は無効になり、次に打つと別の URL になる。

あとから接続先を確かめるときは、別のターミナルで `just observe-show-connection` を打つ。
share が動いていれば同じ URL を、止まっていれば `http://localhost:3000` と「share は停止中」を、
ユーザー・パスワードと一緒に表示する。

```
$ just observe-show-connection
Grafana は公開中 (just observe-share)
  URL:        https://xxxx.trycloudflare.com
  ユーザー:   admin
  パスワード: <値>
```

observe-share は URL が応答した時点で、URL と cloudflared の pid を
`~/.local/share/home-k8s/observability/observe-share.state` に書き、止めるときに消す。
kill -9 などで消されずに残っても、show-connection はその pid の cloudflared が生きているかを
確かめるので、無効になった古い URL は出さない。

- 公開するのは Grafana (3000) だけで、OTLP の受け口 (4318 / 4317) は出さない。
  cloudflared は外から localhost:3000 に向かう接続を 1 本張るだけで、ホストのポートを開ける
  わけではない。開発用コンテナは `--network=host` なので、コンテナ内の cloudflared から
  localhost:3000 に届く。
- URL を知っていれば誰でもログイン画面まで来られる。URL とパスワードは自分以外に渡さない。
- 使い終わったら必ず止める。開きっぱなしにしない。
- Quick Tunnel は試用向けで、稼働の保証は無い (同時リクエスト数の上限もある)。
  長く使う場合は Cloudflare のアカウントで名前付きトンネルと Access を組む。

### 仕組み (`just/observe-share.sh`)

```
別の PC のブラウザ ──https──▶ *.trycloudflare.com ──▶ cloudflared ──▶ caddy (127.0.0.1:3001) ──▶ Grafana (localhost:3000)
                                                  └──────── 開発用コンテナ ────────┘
```

1. caddy と cloudflared を裏で起動し、2 つのログは `observe-share.log` (リポジトリの外、
   起動のたびに上書き) に流す。画面には出さない。
2. ログから URL を拾い、その URL の `/api/health` が外から応答するまで待つ。
3. 応答したら URL・ユーザー・パスワードを表示し、どちらかのプロセスが止まるか Ctrl-C を
   受けるまで待つ。抜けるときは両方を止める (`--grace-period 1s` で、開いたままのブラウザの
   接続を 30 秒待たない)。

#### URL が応答するまで待つ理由

cloudflared が URL を出した時点では、その名前はまだ DNS に無い (実測で URL の表示から 3〜5 秒
後に引けるようになった)。出てすぐ開くと NXDOMAIN になり、それがブラウザや DNS にキャッシュ
される (`trycloudflare.com` の SOA の否定キャッシュは 60 秒)。そのあいだは正しい URL でも
開けない。待つときの名前解決は `curl --doh-url https://1.1.1.1/dns-query` で行い、手元の
リゾルバには NXDOMAIN を覚えさせない。

#### caddy を挟む理由 (root_url を変えない)

Grafana の `server.root_url` は既定の `http://localhost:3000/` のままにしている。Grafana は
ログイン画面への転送などは相対パスで返すが、共有の「Copy link」が作る短縮リンク
(`/goto/<uid>`) の転送先は `root_url` から作る (`Location: http://localhost:3000/d/...`)。
別の PC ではその localhost は自分自身なので、`ERR_CONNECTION_REFUSED` で開けない。

caddy は `Location` の `http://localhost:3000/` を `/` に書き換えるだけで、Host ヘッダは
そのまま Grafana に渡す (Grafana はログインの POST の Origin を Host と比べる)。
Grafana Live の WebSocket もそのまま通る。設定は `just/observe-share.Caddyfile`。

ほかの方法は次の理由で採らなかった。

- `root_url` を公開 URL にする: URL は起動ごとに変わるので、share のたびに Grafana を
  再起動し、終わったら戻すためにもう一度再起動することになる。`observe-up` で不要に再起動
  しない性質を崩し、share を強制終了すると公開 URL の設定が残る。
- `root_url = /` (相対) にする: 短縮リンクが `invalid app URL configuration` で開けなくなる
  (Grafana 13.2.3 で確認)。

公開中も localhost:3000 は従来どおり Grafana に直接つながり、caddy を通らない。
`observe-share` でブラウザから確かめた項目 (ログイン、2 つのダッシュボード、ダッシュボード
リンク、ログ→トレース、短縮リンク) は、`--network host` を付けないコンテナのヘッドレス
Chrome (= localhost:3000 に届かない別の PC と同じ条件) で通した。

## ダッシュボード

`just observe-up` で Grafana にダッシュボード「Claude Code traces」
(<http://localhost:3000/d/claude-code-traces>) と「Claude Code usage」
(<http://localhost:3000/d/claude-code-usage>、[claude-code-usage.md](claude-code-usage.md))
が入る。ほかに「Claude Code improve」([claude-code-improve.md](claude-code-improve.md))、
テレメトリを加工せずに一覧する「Claude Code raw」(<http://localhost:3000/d/claude-code-raw>)、
入口の「Claude Code はじめに」(<http://localhost:3000/d/claude-code-start>、ログイン直後のホーム。
内容は [playbook.md](playbook.md) の短縮版) が入る。
上部の「Claude Code」リンク (タグ `claude-code`) で互いに行き来できる。定義は
`clusters/kind/observability/dashboards/claude-code-traces.json` で、UI で直しても Pod の
再起動で消えるので、変えるときは JSON を編集して `just observe-up` を打ち直す。

### 読み込みの仕組み

chart の `dashboardProviders` で `/var/lib/grafana/dashboards/default` を読むプロバイダを
作り、`observe-up` が `--set-file dashboards.default.<名前>.json=<JSON>` で
JSON を values に流し込む。chart はそれを ConfigMap にしてそのディレクトリにマウントする。
sidecar (ラベル付き ConfigMap を拾うコンテナ) は、ダッシュボードが数枚で ConfigMap を
自分で書く必要も無いので使っていない。values に JSON を直書きしないのは、ファイルのままなら
Grafana の Export / Import とそのまま行き来できるため。

### 集計の方式

パネルはすべて Tempo に TraceQL metrics (`count_over_time()`、`quantile_over_time()`、
`sum_over_time()`) を投げて、保存済みのトレースからその場で集計する。metrics-generator
(スパンから Prometheus 形式のメトリクスを作って remote write する機能) は使わない。

- Claude Code 自身がコストやトークンをメトリクスとして出しており、Prometheus にはそれが
  入っている。スパンからメトリクスを作り直す必要が無い。
- Tempo 3.0 の単一バイナリ構成は、追加の設定なしで TraceQL metrics に答えられる
  (local-blocks プロセッサの有効化は不要だった)。
- 件数が少ない (1 日に数十トレース) ので、クエリのたびに全スパンを読んでも速い。

変えた設定は、Tempo の `query_frontend.metrics.max_duration` と `query_frontend.search.max_duration`
を保存期間と同じ 336h に広げたこと (`tempo-values.yaml`)。既定 (24h / 168h) のままだと、
Grafana の「Last 7 days」でも端数の分だけ 168h を超え、
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
  変数「ツール」の定義はスコープを付けない `tool_name` にする。Grafana の Tempo プラグインは
  タグ一覧からスコープを探してから `span.` を付けるので、`span.tool_name` と書くと
  `Scope for tag span.tool_name not found` になる。タグ一覧は Tempo データソースの
  `timeRangeForTags` (14 日) の範囲で引く。未設定だと時間範囲なしで引き、Tempo はごく最近の
  ブロックのタグしか返さないため、同じエラーになることがある。
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
