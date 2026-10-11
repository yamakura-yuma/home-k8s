# gitops-deployer の基盤 (Istio ambient・Knative・Kafka・Redis・Dapr・APISIX・testkube)

gitops-deployer (GitOps で k8s のリソースを自動でデプロイする小さな Go のマイクロサービス、学習用) を載せる基盤。
ここに書くのは基盤だけで、アプリ本体 (`services/gitops-deployer/`) と Backstage の画面は後続の PR が足す。
どれも ArgoCD の Application で入り、`main` に入れれば `just up` 済みのクラスタに同期される (`argocd.md`)。

前提: ホストの `fs.inotify.max_user_instances` が 512 以上 (`/etc/sysctl.d/99-kind.conf`)。既定の 128 では istio-cni と ztunnel が
`too many open files` で起動しない (kind の 3 ノードと既存のアプリで上限が埋まっている)。

```text
                          ┌──────────────── istio-system ────────────────┐
                          │ istiod  istio-cni (DaemonSet)  ztunnel (DaemonSet) │  ambient: サイドカー無し。L4 の mTLS は各ノードの ztunnel
                          └──────────────────────────────────────────────┘
クラスタ内のクライアント
  └─▶ istio-ingress/gateway (Gateway API の Gateway、Service gateway-istio、ClusterIP)
        └─ HTTPRoute apisix ─▶ apisix/apisix-gateway (standalone。経路は ConfigMap apisix-routes)
                                  └─▶ gitops-deployer/bff など (後続の PR)
Knative Serving: Knative Service ─ net-gateway-api が HTTPRoute を作る ─▶ istio-ingress/gateway (外向き)
                                                                        istio-ingress/knative-local-gateway (クラスタ内)
Knative Eventing: ApiServerSource など ─▶ Broker (class Kafka) ─▶ Trigger ─▶ 受け口
                                            └─ kafka/kafka-kafka-bootstrap:9092 (Strimzi、KRaft、1 ノード)
Dapr (dapr-system、mTLS 無し): サイドカー ─ state store statestore ─▶ redis/redis:6379
                                          └ pub/sub pubsub ─────────▶ kafka/kafka-kafka-bootstrap:9092
testkube: TestWorkflow platform-smoke ─▶ gateway-istio ─▶ APISIX /_platform/ping (pong)
```

## 立てるもの

| Application | 置き場所 | 中身 |
|---|---|---|
| `gateway-api` (wave -1) | `clusters/kind/gateway-api` | Gateway API の CRD (standard、v1.6.3)。上流の release を kustomize で取り込む |
| `istio-base` (wave -1)・`istiod`・`istio-cni`・`ztunnel` | `clusters/kind/istio/*-values.yaml` | Istio 1.30.5 の 4 つの chart。profile ambient。istiod は 1 台・HPA 無し |
| `istio-ingress` | `clusters/kind/istio/gateway` | namespace `istio-ingress` と Gateway 2 つ (`gateway`・`knative-local-gateway`、どちらも ClusterIP) |
| `apisix` | `clusters/kind/apisix` | APISIX (chart 2.18.0、APISIX 3.19.0) の standalone モード。経路の ConfigMap・Gateway からの HTTPRoute・namespace |
| `knative-serving` | `clusters/kind/knative/serving` | Knative Serving 1.23.0 と net-gateway-api 1.23.0 |
| `knative-eventing` | `clusters/kind/knative/eventing` | Knative Eventing 1.23.1 と eventing-kafka-broker 1.23.2 (controller と data plane)。Broker の既定の class は Kafka |
| `strimzi` (wave -1) | `clusters/kind/strimzi/values.yaml` | Strimzi の operator (chart 1.2.0)。namespace `kafka` だけを見る |
| `kafka` | `clusters/kind/kafka` | Kafka 4.3.1 (KRaft、controller と broker を兼ねる 1 ノード、PVC 5Gi)。内部の listener `plain` (9092、平文) |
| `redis` | `clusters/kind/redis` | Redis 8.10.2 の Deployment 1 つ (公式イメージ、AOF を PVC 1Gi に)。AuthorizationPolicy |
| `dapr` (wave -1) | `clusters/kind/dapr/values.yaml` | Dapr 1.18.7 の control plane。mTLS 無し・HA 無し |
| `testkube` | `clusters/kind/testkube` | testkube (chart 2.14.1) の standalone の agent と、TestWorkflow `platform-smoke` |
| `gitops-deployer` | `clusters/kind/gitops-deployer` | namespace `gitops-deployer` (ambient)・`gitops-deployer-apps` と、Dapr の Component `statestore`・`pubsub` |

wave -1 は CRD を持つ Application で、それを使う Application (wave 0) より先に入る。CRD を別の Application から借りる
`istio-ingress`・`apisix`・`kafka`・`redis`・`gitops-deployer` は、CRD がまだ無いときの失敗を `retry` で再試行する。

## Istio ambient

ambient に入れるのは namespace にラベル `istio.io/dataplane-mode: ambient` を付けたものだけで、今は `gitops-deployer`・`redis`・`apisix` の 3 つ。
既存の namespace (観測スタック・Temporal・Keycloak・Backstage など) には付けない (合意書)。どこに付いているかは
`clusters/kind/gitops-deployer/test_platform.py` が `just ci` で確かめる。

- ambient の Pod 同士の通信は、送り元と宛先のノードの ztunnel が HBONE (15008 番、mTLS) で包む。Pod の中にサイドカーは無い
- ambient の外から ambient の Pod への平文は既定で通る (PeerAuthentication の STRICT は置いていない)。Knative の activator や
  Istio の Gateway の Envoy は ambient の外にいるので、この既定に頼る
- `kafka` は ambient に入れない。Strimzi は Kafka の Pod に NetworkPolicy を作り、listener のポート (9092 など) だけを開ける。
  ambient の宛先には ztunnel が 15008 番で届けるので、この NetworkPolicy に落とされる
- Redis は AuthorizationPolicy で、送り元の namespace が `gitops-deployer` の ambient の Pod だけを通す (ztunnel が L4 で、証明書の送り元を見る)。
  ambient の外からの平文は送り元が分からないので拒まれる

Pod の通信が ztunnel を通っているかは、ztunnel のログ (`kubectl -n istio-system logs ds/ztunnel`) の `src.identity`・`dst.identity` で分かる
(SPIFFE の ID が出れば mTLS)。

## Gateway と APISIX

Gateway は Gateway API の `Gateway` (gatewayClassName `istio`) で書き、istiod が同じ namespace に Envoy の Deployment と
Service `<Gateway 名>-istio` を作る。kind に LoadBalancer は無いので、注釈 `networking.istio.io/service-type: ClusterIP` で
Service を ClusterIP にする (LoadBalancer のままだと住所が付かず、Gateway が Programmed にならない)。

ホストからは開けていない。開けるには kind-config の extraPortMappings にポートを足し、Service を NodePort にする必要があり、
extraPortMappings はクラスタを作るときにしか効かない (`just down && just up` で作り直す。人の判断が要る)。それまでは
`kubectl -n istio-ingress port-forward svc/gateway-istio 8000:80` で開ける。

外向きの `gateway` に HTTPRoute を付けられるのは、ラベル `home-k8s/gateway-access: "true"` を持つ namespace だけ (`apisix` と `gitops-deployer`)。
`knative-local-gateway` はクラスタ内向けなので、どの namespace からも付けられる。

APISIX は standalone モードで動かす。経路は ConfigMap `apisix/apisix-routes` の `apisix.yaml` にあり、Git で書いて ArgoCD で同期する。
etcd と Admin API は無い (chart の etcd は Bitnami のイメージで、無料の配布が止まっている)。APISIX は `apisix.yaml` の変更を
読み直すので、経路を足しても再起動は要らない。ファイルの末尾の `#END` が無いと読み込まれない。

経路は 2 つ。`/_platform/ping` は疎通確認用で、`mocking` プラグインが `pong` を返す。`/gitops-deployer/api/*` は gitops-deployer の
bff (`bff.gitops-deployer.svc.cluster.local:80`) への経路で、`openid-connect` が Keycloak (realm `home-k8s`) の JWT を JWKS の鍵で確かめ
(`bearer_only`。トークンが無い・不正なら 401)、`limit-count` が 60 秒に 120 回で絞り (超えたら 429)、`proxy-rewrite` が先頭の
`/gitops-deployer` を外す。bff も同じトークンの aud とロールをもう一度確かめる。どのプラグインも APISIX の既定のプラグインに入っていて、
`config.yaml` の `plugins` を書き換えずに使える。bff は後続の PR (gitops-deployer のアプリ) が足すので、それまでは正しいトークンでも 502 になる。

## Knative

ネットワーク層は **net-gateway-api** にした (net-istio にしない)。

- 合意書の公開の方法は Gateway API (HTTPRoute) で、Istio はその実装として使う。net-gateway-api なら Knative が作る経路も
  HTTPRoute になり、アプリの経路と同じ種類のリソースで見られる
- net-istio は Istio の `VirtualService` と、`istio-ingressgateway` (Gateway API でない古い入口) を前提にする。ambient では
  サイドカーを入れないので、net-istio の mesh 向けの設定 (サイドカー経由の経路) も使わない

`config-gateway` は、外向きを `istio-ingress/gateway`、クラスタ内向けを `istio-ingress/knative-local-gateway` に向ける。
Knative Service の `<名前>.<namespace>.svc.cluster.local` は `knative-local-gateway-istio` を指す ExternalName になる。

Eventing の Broker は Kafka Broker にし、`config-br-defaults` で既定の class にする (class を書かない Broker も Kafka Broker)。
Broker ごとのトピックはパーティション 1・複製 1 (ブローカーが 1 台なので)。Kafka の data plane (receiver・dispatcher) は JVM で、
上流の requests (dispatcher は 1 CPU・700Mi) を kind 向けに下げて limits を付けた。

## Kafka・Redis・Dapr

- Kafka は Strimzi の `Kafka` と `KafkaNodePool` (v1 API) で 1 ノード。KafkaTopic は Topic Operator が見るので、トピックを宣言で置ける
- Redis はパスワードを置かない。入口は上の AuthorizationPolicy で絞り、通信は ambient の mTLS で包む
- Dapr は `global.mtls.enabled: false`。暗号化は ambient に任せる (合意書)。Component は `gitops-deployer` に
  `statestore` (state.redis) と `pubsub` (pubsub.kafka、認証無し) を置く。アプリは Dapr の API だけを使う

## testkube

Testkube の Cloud に繋がない standalone の agent で、API・MongoDB・NATS・MinIO が namespace `testkube` に立つ。利用状況の送信は切る。
試験は TestWorkflow で宣言し、`clusters/kind/testkube/manifests` に置く。CLI (`kubectl testkube`) は開発用コンテナに入っていないので、
走らせるときは TestWorkflowExecution を作る:

```sh
kubectl --context kind-study-kind -n testkube create -f - <<'EOF'
apiVersion: testworkflows.testkube.io/v1
kind: TestWorkflowExecution
metadata:
  generateName: platform-smoke-
spec:
  testWorkflow:
    name: platform-smoke
EOF
```

結果は TestWorkflowExecution の status には書かれない (testkube 2.14.1)。api-server のログの `Saving execution` に出る
`executionId` で API から読む (`.result.status` が passed / failed)。ホストの shell で打つ (curl・jq を使う):

```sh
kubectl --context kind-study-kind -n testkube port-forward svc/testkube-api-server 8088 &
curl -s http://localhost:8088/v1/test-workflow-executions/<executionId> | jq '{name, status: .result.status}'
```

## 資源

kind の 3 ノード (ホストのメモリ 31GB を共有) に載せるので、上流の既定の requests が大きいもの (istiod 500m・2Gi、ztunnel 200m・512Mi、
Kafka の data plane、Dapr の scheduler の PVC 16Gi) を下げ、自分で書いた Pod と values で変えられるものには requests と limits を付けた。
上流の manifest のままのもの (Knative の control plane など) は上流の requests・limits に従う。
Dapr の scheduler は chart 1.18.7 が台数を 3 に固定していて values で変えられないので、3 台のまま (各 requests 50m・128Mi)。
APISIX の nginx の worker は 2 に固定する (`apisix.nginx.workerProcesses`)。既定の auto は CPU の数で、WSL2 のホストでは 22 プロセスになり、
起動直後で約 430MiB、要求が無くても 30 分に約 30MiB ずつ増えて limit の 512Mi で OOMKilled を繰り返した。limit は 1Gi、requests は 256Mi。

## kind に合わせて上流から変えたところ

- Knative の HPA (activator・webhook・eventing-webhook) は外す。kind に metrics-server が無く、HPA が CPU を読めずに
  Degraded のままになる。台数は Deployment の 1
- ArgoCD の比較から外す欄 (どれも、書いた値でなく Kubernetes かコントローラが書き足す欄):
  Knative の webhook の caBundle・rules・namespaceSelector、集約の ClusterRole の rules、Strimzi の CRD `kafkas` の空の
  `properties: {}`、testkube の NATS の `volumeClaimTemplates` の apiVersion・kind・status

## 確かめた結果

2026-10-11、本番 `kind-study-kind` に、この基盤の Application だけを作業ブランチに向けて入れて確かめた (既存の Application と
`argocd/root` は main のまま)。確かめるための Pod などは確かめた後に消した。

- 追加した 15 の Application がすべて Synced/Healthy。既存の Application もすべて Synced/Healthy のままで、既存の Pod の
  再起動回数は入れる前と同じ (OOMKilled なし)
- ambient: `gitops-deployer` の Pod 同士の通信を ztunnel が 15008 番 (HBONE) で運び、ログに双方の SPIFFE の ID が出た。
  ambient の外の `gitops-deployer-apps` から Redis へは `policy rejection: allow policies exist, but none allowed` で切られた
- Gateway: `gateway-istio` の `/verify` に付けた HTTPRoute から `gitops-deployer` の Pod に届いた (200)
- Knative: Kafka Broker (class Kafka) が Ready。ConfigMap を作ると ApiServerSource→Broker→Trigger→Knative Service に
  `dev.knative.apiserver.resource.add` が届いた (拡張属性 `knativekafkaoffset` が付き、Kafka のトピックを通ったと分かる)
- Dapr: サイドカー (daprd) から state store に書いて読め (Redis にキー `client||verify`)、pub/sub に publish した
  CloudEvent を Kafka のトピックから読めた。`daprsystem` の mTLS は `enabled: false`
- testkube: TestWorkflow `platform-smoke` が passed (`pong`)
