# Claude Code のトレースを見る観測スタック

WSL2 上の Claude Code が OpenTelemetry (OTLP) で出すトレースを kind クラスタで受け、
Grafana でスパンの親子関係 (サブエージェントを含む) として見るための構成をまとめる。
同じ経路でメトリクスを Prometheus に、ログ (イベント) を Loki に入れている。
オールインワンイメージ (`grafana/otel-lgtm`) は使わず、OTel Collector・Tempo・Prometheus・
Loki・Grafana を別々の Helm chart で入れている。本番の構成に近い形で、部品ごとに差し替えて学べる。
chart は ArgoCD が GitHub の `main` から同期する ([../cluster/argocd.md](../cluster/argocd.md))。

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
| Prometheus (server・kube-state-metrics・node-exporter) | `prometheus-community/prometheus` | 29.35.0 | `clusters/kind/observability/prometheus-values.yaml` |
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
  `service.instance.id` も昇格させるが、これは Collector 自身のメトリクス (grafana.com 15983) のため。
  Claude Code はこの属性を送らないので、Claude Code の系列には付かない。
- ログ: Loki 3 の OTLP 受信 (`/otlp`) に `otlphttp` exporter で送る。contrib の `loki`
  exporter は廃止済み。リソース属性のうち `service.name` はインデックスラベル
  (`service_name`) に、残りとログの属性 (`event.name`、`trace_id` など) は structured
  metadata になり、`| event_name="tool_result"` のように絞れる。

Claude Code 以外のメトリクスも同じ Prometheus に入る。経路は 2 つで、どちらも
grafana.com の公開ダッシュボードが読む (`docs/observability/grafana-com-dashboards.md`)。

- Collector 自身のメトリクス (`otelcol_*`): Collector の `service.telemetry.metrics` に
  OTLP の reader を足し、上と同じ OTLP 受信に 10 秒ごとに送る。スクレイプはしない。
- Kubernetes 自体のメトリクス: Prometheus がスクレイプする。kube-state-metrics
  (`kube_*`) と node-exporter (`node_*`) は prometheus chart のサブチャートで入れ、
  kubelet の cAdvisor (`container_*`)・API サーバー (`apiserver_*`)・CoreDNS (`coredns_*`)
  と合わせて chart 既定のスクレイプ設定 3 つで取る。

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
just up   # クラスタ作成 (受け口のポートと保存先のマウントも作られる)、ArgoCD の導入、Secret の作成、観測スタックと Headlamp の同期
kubectl --context kind-study-kind -n argocd get applications
kubectl --context kind-study-kind -n observability get pods -o wide
```

5 つの chart は ArgoCD の Application (`clusters/kind/argocd/apps/`) が入れる。chart の版は
Application の `targetRevision`、values は上の表のファイルで、どちらも `main` に入れれば
ArgoCD が反映する (既定で 3 分ごとに見に行く)。入れ方の詳細は [../cluster/argocd.md](../cluster/argocd.md)。

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
保存する。保存先は次の 3 段でつながっている。StorageClass・PV・PVC の設計と運用は
[persistence.md](../cluster/persistence.md)。

1. `clusters/kind/kind-config.yaml` の `extraMounts` で、WSL2 ホストの
   `~/.local/share/home-k8s/observability` を `study-kind-worker` ノード (Docker コンテナ)
   の `/var/local/home-k8s/observability` にマウントする。kind は設定ファイル中の環境変数を
   展開しないため、`${HOME}` は `just up` が sed で埋めてから `kind create cluster --config -`
   に渡している。開発用コンテナは docker.sock を共有しており、ノードは WSL2 の Docker 上で
   動くので、パスは WSL2 ホスト側のものになる。
2. `clusters/kind/storage/pv-tempo.yaml` の `local` PV `tempo` が、そのノードの
   `/var/local/home-k8s/observability/tempo` を指す。PV の `nodeAffinity` が同じノードの
   ラベル `home-k8s/observability-storage: "true"` を指すので、Tempo の Pod はそのノードに置かれる。
3. Tempo の StatefulSet が作る PVC `storage-tempo-0` が、PV の `claimRef` で PV `tempo` と結ばれ、
   `/var/tempo` にマウントされる。Tempo は `traces/` (ブロック)、`wal/`、`live-store/` をそこに書く。

クラスタを消すと PV・PVC のオブジェクトは消えるが、1 のホスト側ディレクトリは残る。
`just up` のあと ArgoCD が同じ名前の PV と PVC を作り直し、同じディレクトリに結び付くので、
`just down` → `just up` のあとも同じトレースを引ける。保持期間は 14 日
(`tempo.retention: 336h`) で、それより古いブロックは Tempo が消す。

所有者は Pod の `fsGroup` (Tempo は 10001) で kubelet が付け替える (`local` の volume は
`fsGroup` が効く。hostPath は効かない)。ホスト側の `tempo/` 以下は UID 10001 の所有になる。
トレースを全部消したいときは `sudo rm -rf ~/.local/share/home-k8s/observability/tempo` とし、
`just up` を打ってディレクトリを作り直す (`local` の PV はディレクトリが先にあることが前提)。

Prometheus・Loki・Grafana も同じ仕組みで、PV `prometheus`・`loki`・`grafana` がそれぞれ
`/var/local/home-k8s/observability/prometheus`・`.../loki`・`.../grafana` を指す。保持期間は
Prometheus・Loki とも 14 日 (Prometheus は `server.retention: 14d`、Loki は
`limits_config.retention_period: 336h` と compactor の `retention_enabled`)。所有者は
Prometheus が UID 65534 (nobody)、Loki が UID 10001、Grafana が GID 472 になる。
kind 標準の StorageClass `standard` (local-path) は使わない (PVC を消すとデータも消え、
クラスタを消せばノードの中の保存先ごと消えるため)。

後で MinIO を立てて Tempo の backend を `s3` に切り替えると、オブジェクトストレージに
保存する本番に近い構成を練習できる。

## Grafana の認証

Grafana はログイン必須で、匿名アクセスは付けていない (閲覧のみの匿名も無い)。
「別の PC から見る」で外に出すことがあるため。ユーザーは 3 つある。

| ユーザー | ロール | 使う人 | パスワードのファイル | 表示するコマンド |
| --- | --- | --- | --- | --- |
| `admin` | Admin | 自分 (ダッシュボードの編集、Explore) | `grafana-admin-password` | `just show grafana-admin` |
| `viewer` | Viewer | 公開の Pod (`share`) が人の代わりに Grafana へ入る (人には渡さない。localhost:3000 では自分も使える) | `grafana-viewer-password` | `just show grafana` |
| `backstage` | Viewer | Backstage (ダッシュボードの一覧を読む。人は使わない) | `grafana-backstage-password` | なし |

ファイルはどれも `~/.local/share/home-k8s/observability/` (WSL2 ホスト側、パーミッション 600) に
置き、リポジトリには置かない。viewer はダッシュボードを見られるが、保存・編集と Explore はできない。

admin のパスワードは次のように決まる。

1. `just up` の中の Secret の作成 (`just/grafana-secrets.sh`) が最初に `grafana-admin-password` を見る。無ければ `openssl rand` で
   32 文字の値を作って保存し、あればそれを使う。
2. その値を Secret `observability/grafana-admin` (`admin-user` / `admin-password`) に
   `just/secret-lib.sh` の `put_secret` (あれば `replace`、無ければ `create`) で入れる。`kubectl apply` は値を `last-applied-configuration` の注釈に残すので使わない。
3. chart の `admin.existingSecret: grafana-admin` で、Grafana はこの Secret を環境変数として読む。

chart に Secret を作らせると同期のたびに乱数で作り直されて Pod が再起動するが、
この形なら Secret の中身が変わらないので `just up` を打ち直しても Pod はそのまま残る。
ArgoCD は Secret を名前で参照するだけで、中身は Git にも ArgoCD にも入らない。

Grafana は `grafana.db` を PV に残すので、Secret の admin のパスワードを Grafana が使うのは
DB が空の初回だけになる。そこで Grafana の Pod の initContainer `reset-admin-password` が、
起動のたびに `grafana cli admin reset-admin-password` で DB の admin のパスワードを Secret の値に
そろえる。Grafana のイメージは distroless で sh が無いので、同じイメージの `grafana` バイナリを
exec 形式で直接打っている。

viewer も同じく、`just up` が `grafana-viewer-password` を (無ければ作って) Secret
`observability/grafana-viewer` (`password`) に入れる。Grafana には viewer を最初から作る設定が
無いので、Grafana の Pod にサイドカー `viewer-user` (curl のイメージ) を足し、Pod の起動ごとに一度だけ
admin で `/api/users/lookup?loginOrEmail=viewer` を引き、居なければ `/api/admin/users` で作り、
居れば `/api/admin/users/<id>/password` でパスワードを Secret の値に変える。
ロールは `grafana.ini` の `users.auto_assign_org_role: Viewer` で決まる。
Grafana のイメージは distroless で sh が無く、postStart で API を叩けないため別コンテナにした。

backstage も同じサイドカーが同じ処理でそろえる。パスワードは `just up` が `grafana-backstage-password` から Secret
`observability/grafana-backstage` に入れる。viewer とは分けてあるので、viewer を変えても
Backstage は読み続けられる。Backstage 側の設定は [docs/cluster/backstage.md](../cluster/backstage.md)。

viewer のパスワードを変えるのは、ファイルを書き換えて `just up` を打つときだけ。`just up` は
Secret `observability/grafana-viewer` と、公開の Pod (`share`) が持つ写し (Secret `share/share-grafana`) を一緒に入れ直す。
ただし Grafana の DB へは Pod の起動時にサイドカーがそろえ、share の caddy は写しを Pod の起動時に 1 度だけ読むので、
`just up` のあとに **`deploy/grafana` と `deploy/share` の両方を `rollout restart` する**。片方だけだと Grafana の経路が
302 (ログイン画面) になる。`share` を作り直すと 3 つの公開 URL が変わる (`just share get` で引き直す)。自動で読み直す仕組みは [#58](https://github.com/yamakura-yuma/home-k8s/issues/58)。
人ごとの資格情報 (`just share add`) は Grafana のユーザーではないので、配るたびには viewer のパスワードは変わらない。

パスワードのファイルの所有者はホストのユーザーにしてあるので、WSL2 のシェルから `cat` しても読める。
admin のパスワードを変えたいときはファイルを消して (または書き換えて) `just up` を打ち、Secret が変わったあとで
`kubectl --context kind-study-kind -n observability rollout restart deploy/grafana` とする
(起動のたびに initContainer が DB の admin を Secret の値にそろえる)。viewer も同じ手順で変わる (上の `share` の再起動も要る)。backstage は `rollout restart` のあと Backstage の Pod を作り直す。

開発用コンテナは `~/.local/share/home-k8s` をホストと同じパスでマウントしている。この
マウントが無い古いコンテナで `just up` を打つと、パスワードがコンテナの中にだけ残らない
よう止まるので、`just devcontainer down && just devcontainer up` で作り直す。

API を curl で叩くときは Basic 認証を付ける。

```
curl -u "admin:$(cat ~/.local/share/home-k8s/observability/grafana-admin-password)" \
  http://localhost:3000/api/dashboards/uid/claude-code-traces
```

データソース `Tempo` (uid `tempo`)・`Prometheus` (uid `prometheus`)・`Loki` (uid `loki`) は
`datasources` の provisioning で登録しており、
UI からは編集できない。変えるときは `grafana-values.yaml` を直して `main` に入れる
(ArgoCD が同期する)。UI で作ったダッシュボードは `grafana.db` (PV `grafana`) に入り、
Pod の再起動やクラスタの作り直しのあとも残る。

## 別の PC から見る

クラスタの `share` Pod (`clusters/kind/share/`) が、Grafana・headroom のダッシュボード・Backstage を
Cloudflare Quick Tunnel (`https://<ランダム>.trycloudflare.com`、アカウント不要) で常に公開している。
`just up` で一緒に立ち、`just down` で消える。人ごとの資格情報を持たない要求は、どの経路も 401 になる。
見せたい人には、次で資格情報を発行して渡す (ホストから打ってよい)。

```
$ just share add alice          # 既定 8h で失効。--ttl 2h で変えられる (上限 24h)
$ just share get alice          # その人の情報と URL (パスワードは出ない)
$ just share delete alice       # 即失効 (最大 2 秒)
```

`add` は名前・パスワードと、Grafana・headroom・Backstage の 3 つの URL を表示する。パスワードが出るのは
このときだけ (特権 viewer の `rotate` も同じ) で、Secret には PBKDF2 のハッシュしか残らない。1 つの資格情報で
3 つの URL に入れる。ブラウザの Basic 認証は URL ごとなので、名前とパスワードを URL ごとに打つ。
URL は `share` Pod を作り直す (再起動・クラスタの作り直し) と変わる。変わったら `just share get` で引き直す。
構成と全サブコマンドは [docs/cluster/share.md](../cluster/share.md)。

- 渡せるのは閲覧用の `viewer` の権限だけで、admin の資格情報は出さない。自分で編集するときは
  `just show grafana-admin`。
- 期限のない資格情報 (`--permanent`、特権 viewer) は全体で 1 つ。期限がないこと以外は他の人と同じ。
- URL と資格情報は見せたい相手にだけ渡す。使い終わったら `delete` する。
- Quick Tunnel は試用向けで、稼働の保証は無い。長く使う場合は Cloudflare のアカウントで名前付きトンネルと Access を組む。

### 各対象で通る経路

| 対象 | 通すもの | 通さないもの |
|---|---|---|
| Grafana | 画面と読み取りの API。Grafana へは caddy が viewer の Basic に差し替えて渡す | `/profile/password`・`/api/user/password`、`/api/user` と `preferences` への書き込み (人が viewer を壊さないため) |
| headroom | ダッシュボード用の GET (`/dashboard`・`/health`・`/stats`・`/stats-history`・`/stats-lifetime`・`/transformations/feed`・`/favicon.ico`) だけ。ホストの中継 (caddy) を経由し、headroom 本体は 127.0.0.1:8787 のまま | `/v1/*` (Anthropic へ転送するプロキシ本体)・`/settings`・`/stats/reset`・`/cache/clear` ほか。HEAD も通さない |
| Backstage | 画面・カタログ・TechDocs の読み取り | `/api/proxy/*` (Grafana の API を Backstage の資格情報で読める)・カタログへの書き込み |

### Grafana の root_url を変えない理由

Grafana の `server.root_url` は既定の `http://localhost:3000/` のままにしている。共有の「Copy link」が作る
短縮リンク (`/goto/<uid>`) の転送先は `root_url` から作る (`Location: http://localhost:3000/d/...`) ので、
別の PC ではその localhost が自分自身になり開けない。share の caddy は `Location` の
`http://localhost:3000/` を `/` に書き換え、Host ヘッダはそのまま Grafana に渡す
(Grafana はログインの POST の Origin を Host と比べる)。Grafana Live の WebSocket もそのまま通る。
`root_url` を公開 URL にすると Pod を作り直すたびに URL が変わって Grafana の設定も変わり、
`root_url = /` (相対) にすると短縮リンクが `invalid app URL configuration` で開けなくなる (Grafana 13.2.3 で確認)。
公開と無関係に localhost:3000 は Grafana に直接つながる。

## ダッシュボード

Grafana にはダッシュボード「Claude Code traces」
(<http://localhost:3000/d/claude-code-traces>) と「Claude Code usage」
(<http://localhost:3000/d/claude-code-usage>、[claude-code-usage.md](claude-code-usage.md))
が入る。ほかに「Claude Code improve」([claude-code-improve.md](claude-code-improve.md))、
テレメトリを加工せずに一覧する「Claude Code raw」(<http://localhost:3000/d/claude-code-raw>)、
入口の「Claude Code はじめに」(<http://localhost:3000/d/claude-code-start>、ログイン直後のホーム。
内容は [playbook.md](playbook.md) の短縮版) が入る。
上部の「Claude Code」リンク (タグ `claude-code`) で互いに行き来できる。定義は
`clusters/kind/observability/dashboards/claude-code-traces.json` で、UI で直しても Pod の
再起動で消えるので、変えるときは JSON を編集して `main` に入れる (ArgoCD が ConfigMap を更新し、
Grafana が 1 分ほどで読み直す)。

### 読み込みの仕組み

chart の `dashboardProviders` で `/var/lib/grafana/dashboards/default` を読むプロバイダを
作る (フォルダごとに provider を分け、設定項目別と grafana.com も同じ形)。JSON は
`clusters/kind/observability/dashboards/kustomization.yaml` の `configMapGenerator` が
provider ごとの ConfigMap `grafana-dashboards-<provider>` にし、chart の `dashboardsConfigMaps` が
それを `/var/lib/grafana/dashboards/<provider>` にマウントする。ConfigMap は Application grafana が
chart と一緒に同期する。

ArgoCD の Helm のソースには `--set-file` に当たるものが無く、chart の外 (repo のファイル) を
values に読み込めない。kustomize ならファイルのまま ConfigMap にでき、provider とフォルダの
設定も前のまま使える。sidecar (ラベル付き ConfigMap を拾うコンテナ) は、フォルダの割り当てを
ConfigMap の注釈に移すことになり、変える所が増えるので使っていない。values に JSON を直書きしないのは、
ファイルのままなら Grafana の Export / Import とそのまま行き来できるため。

JSON を足したり消したりしたら `kustomization.yaml` の `files` も直す。書き忘れると Grafana に
出ないので、`test_kustomization.py` で突き合わせる。

```sh
python3 -B -m unittest discover -s clusters/kind/observability/dashboards -p test_kustomization.py
```

ConfigMap は大きいもので 400 KiB を超え、client-side apply の注釈 `last-applied-configuration`
(上限 256 KiB) に収まらない。Application grafana は `ServerSideApply=true` で同期する。

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
