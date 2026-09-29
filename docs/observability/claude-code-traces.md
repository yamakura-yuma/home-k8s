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
