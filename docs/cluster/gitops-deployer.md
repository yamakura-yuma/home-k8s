# gitops-deployer (GitOps でデプロイする学習用のマイクロサービス)

依頼 (アプリの名前・イメージ・ポート・台数) を受けると、home-k8s に `deploys/<名前>/` を足す PR を作って auto-merge させ、
Argo CD の ApplicationSet が作った Application が Synced/Healthy になったことを Knative Eventing から Signal で受けて完了にする。
削除も同じ流れ (ディレクトリを消す PR → Application の消滅)。Go の 1 モジュール・1 イメージで 6 つの Pod を起動し分け、
進行は Temporal の Workflow (Workflow Engine Pod) が一元管理する。載せている基盤は [gitops-deployer-platform.md](gitops-deployer-platform.md)。

- 処理のアニメーション (デプロイと削除を 1 段ずつ): [gitops-deployer-flow.html](gitops-deployer-flow.html)
- 仕様書 (経路と認証・Workflow の状態遷移・GitOps・Knative Eventing・Dapr・OTel の図): [gitops-deployer-spec.html](gitops-deployer-spec.html)
- API 契約: `services/gitops-deployer/internal/bff/openapi.yaml` (bff の `GET /api/v1/openapi.yaml` でも返す)

```text
クライアント ─▶ istio-ingress/gateway ─ HTTPRoute apisix ─▶ APISIX /gitops-deployer/api/* (Keycloak の JWT・limit-count)
  ─▶ bff (gin、JWT をもう一度確かめる・ロール) ─ Dapr service invocation ─▶ api (gin) ─▶ Temporal (dev) に Workflow を開始
workflow-engine (Workflow だけ):
  1. git-activity: deploys/<名前>/app.yaml を足す (消す) PR を作り auto-merge を有効にする ─▶ GitHub (ci / just ci が通れば merge)
  2. ApplicationSet gitops-deployer-apps が Application deploy-<名前> を作り、namespace gitops-deployer-apps に同期する
  3. Signal argo-event を待つ (ポーリングしない、上限 20 分):
       ApiServerSource (argocd の Application) ─▶ Kafka Broker default ─▶ Trigger ─▶ event-receiver (Knative Service 0↔1) ─▶ Signal
     デプロイは Synced/Healthy で、その revision に PR の merge が入っていること (git-activity が GitHub の compare で確かめる)。
     削除は Application の消滅。待ちきれなければ補償 (merge 前なら PR を閉じる、merge 後なら戻す PR)
  4. store-activity: 履歴を Dapr state (history、Redis) に残し、完了イベントを Dapr pub/sub (Kafka のトピック gitops-deployer-events) に流す
```

## Pod

namespace `gitops-deployer` (Istio ambient)。イメージは `kind.local/home-k8s-gitops-deployer:<版>` 1 つで、command の `/usr/local/bin/<名前>` (`cmd/<名前>`) で起動し分ける。
`just up` が build して kind load する (`just/gitops-deployer.just`。レジストリには push しない)。

| Pod | 種類 | Task Queue | Dapr | 資格情報 | コード |
|---|---|---|---|---|---|
| bff | Deployment | なし | あり | なし | `internal/bff`・`internal/auth` |
| api | Deployment | なし (client) | あり | なし | `internal/api` |
| workflow-engine | Deployment | `workflow-engine` | なし | なし | `internal/workflow` |
| git-activity | Deployment | `git-activity` | なし | GitHub App の秘密鍵 | `internal/gitops` |
| store-activity | Deployment | `store-activity` | あり | なし | `internal/history` |
| event-receiver | Knative Service (min 0・max 1) | なし (client) | なし | なし | `internal/receiver` |

manifest は `services/gitops-deployer/manifests` (Application `gitops-deployer-services`)。デプロイされるアプリの ApplicationSet は
`clusters/kind/argocd/apps/gitops-deployer-apps.yaml`。

## 決めたこと

| 項目 | 決めたこと | 理由 |
|---|---|---|
| Temporal への接続 | dev の `temporal-internal-frontend.dev:7236` (認可を通らない内部用の frontend、ClusterIP)。Temporal の namespace は `gitops-deployer` (保存 3 日)。PreSync の Job が作る | 7233 の frontend は Keycloak の JWT (permissions クレーム) を要求する。合意書の Pod の構成で Temporal を使う Pod (api・workflow-engine・event-receiver・Worker) は資格情報を持たない |
| Workflow ID | `app-<名前>`。ConflictPolicy FAIL (走っている間の同じアプリの依頼は 409)、ReusePolicy ALLOW_DUPLICATE | アプリ 1 つに処理は同時に 1 つ (同時実行制御)。Signal の宛先がアプリの名前だけで決まる |
| 完了の判定 | デプロイ: Application が Synced かつ Healthy で、`status.sync.revision` に PR の merge commit が入っている。削除: Application の delete イベント (finalizer が中身を消した後) | 既存のアプリの更新では、merge の前から Synced/Healthy のイベントが来る。revision を見ないと古い状態で完了してしまう |
| イベントと Workflow の対応 | ApplicationSet が Application にラベル `gitops-deployer.home-k8s/app: <名前>` を付け、event-receiver がそれを Workflow ID にする | Application の名前の規約 (deploy-) に頼らない。ApiServerSource もこのラベルで絞る |
| ApiServerSource の権限 | namespace argocd の Application の get・list・watch (Role) と namespace の一覧 (ClusterRole。namespaceSelector が使う) | Application は変えない。読むのは argocd の中だけ |
| Knative のネットワーク | 基盤の net-gateway-api のまま。event-receiver は cluster-local | Trigger からクラスタの中で呼ぶだけ |
| Dead Letter | Trigger は指数バックオフで 5 回打ち直し、尽きたら Kafka Broker `dead-letter` (Trigger を持たない) へ | 基盤は KafkaSink の data plane を入れていない。Broker なら同じ data plane でイベントが Kafka のトピックに残る |
| 履歴の state store | 本 PR の Component `history` (同じ Redis、`keyPrefix: name`、scopes は api と store-activity) | 基盤の `statestore` は鍵の頭が app-id で、store-activity が書いた履歴を api が読めない。基盤の Component は変えない |
| GitHub の書き込み | GitHub App (home-k8s だけに入れ、Contents・Pull requests の Read and write)。git-activity が App の JWT で installation token (1 時間) を取り、切れる 5 分前に取り直す。取るときに home-k8s と contents・pull_requests に絞る。下の「人の作業」 | 人の持ち物 (個人のトークン) に縛られず、権限と入れ先のリポジトリが App の設定で見える。トークンは 1 時間で切れ、漏れても長く使えない。App が作った PR でも GitHub Actions の CI が走る |
| API の認証 | Keycloak の client `gitops-deployer` (confidential、service account あり、ブラウザのログインは無し)。アクセストークンの aud に `gitops-deployer`、client role `view`・`deploy`。グループ admins に両方、viewers に view | APISIX と bff が同じ issuer・aud を確かめる。後続の Backstage と testkube の E2E は client credentials で取る |

## 人の作業

### GitHub App

1. GitHub → Settings → Developer settings → GitHub Apps → New GitHub App。Homepage URL は home-k8s のリポジトリ、Webhook の Active は外す
2. Repository permissions: Contents = Read and write、Pull requests = Read and write (Metadata = Read は自動で付く)。ほかは No access。
   Where can this GitHub App be installed? は Only on this account
3. 作った App の画面で Generate a private key (PEM がダウンロードされる)。App ID を控える
4. Install App → `yamakura-yuma` → Only select repositories → `home-k8s`。入れた後の URL の末尾の数が installation ID
5. WSL2 のホストに 2 つのファイルを置き、どちらも `chmod 600`
   - `~/.config/home-k8s/gitops-deployer-github-app.env`: `GITHUB_APP_ID=<App ID>` と `GITHUB_APP_INSTALLATION_ID=<installation ID>` の 2 行
   - `~/.config/home-k8s/gitops-deployer-github-app.pem`: 3 の秘密鍵

`just up` の `_gitops-deployer-secrets` (`just/gitops-deployer-secrets.sh`) が Secret `gitops-deployer/github-app` (`app-id`・`installation-id`・`private-key.pem`) にする。
どちらかのファイルが無ければ警告だけ出し、git-activity は起動しない (デプロイの依頼は PR を作れないまま待ち、20 分で失敗する)。
秘密鍵は環境変数に出さず、git-activity にファイル (`/var/run/secrets/github-app/private-key.pem`) として渡す。
鍵を取り替えたら `just up` を打ち直す (次の installation token の取り直しから効く。すぐ効かせるなら `kubectl -n gitops-deployer rollout restart deploy/git-activity`)。

### Keycloak の client (動いている realm に足す)

realm の import は realm があると何もしない ([keycloak.md](keycloak.md) の「realm を後から変える」)。client `gitops-deployer` は
`realm-home-k8s.yaml` に書いたうえで、動いている Keycloak に kcadm で足した (secret はホストの `~/.local/share/home-k8s/keycloak/clients/gitops-deployer`、
`just up` の `_keycloak-secrets` が作るものと同じ)。作り直したクラスタでは import が入れる。

## 使う

トークンは client credentials で取る (secret は上のファイル):

```sh
token=$(curl -s https://keycloak.taild2b611.ts.net/realms/home-k8s/protocol/openid-connect/token \
  -d grant_type=client_credentials -d client_id=gitops-deployer \
  --data-urlencode "client_secret@$HOME/.local/share/home-k8s/keycloak/clients/gitops-deployer" | jq -r .access_token)
kubectl --context kind-study-kind -n istio-ingress port-forward svc/gateway-istio 8000:80 &
curl -s -H "Authorization: Bearer $token" -H 'Content-Type: application/json' -H "Idempotency-Key: $(uuidgen)" \
  -d '{"name":"hello","image":"docker.io/nginxinc/nginx-unprivileged:1.29-alpine","port":8080}' \
  http://localhost:8000/gitops-deployer/api/v1/deployments
curl -s -H "Authorization: Bearer $token" http://localhost:8000/gitops-deployer/api/v1/deployments/hello | jq
curl -s -X DELETE -H "Authorization: Bearer $token" http://localhost:8000/gitops-deployer/api/v1/deployments/hello
```

Workflow の履歴は Temporal UI (dev、`https://temporal-dev.taild2b611.ts.net`) の namespace `gitops-deployer` で見る。
trace は Grafana (観測スタック) の Tempo で、bff の応答の後に `service.name` が gitops-deployer の 6 つの Pod を引く。

## E2E (testkube)

TestWorkflow `gitops-deployer-e2e` (`services/gitops-deployer/manifests/e2e.yaml`、本体は `services/gitops-deployer/e2e/main.go`)。
本物の home-k8s に PR を 2 つ (足す・消す) 作って merge するので、走らせるのは人が決めたときだけにする (TestWorkflowExecution を作る):

```sh
kubectl --context kind-study-kind -n testkube create -f - <<'EOF'
apiVersion: testworkflows.testkube.io/v1
kind: TestWorkflowExecution
metadata:
  generateName: gitops-deployer-e2e-
spec:
  testWorkflow:
    name: gitops-deployer-e2e
EOF
```

結果の読み方は [gitops-deployer-platform.md](gitops-deployer-platform.md) の「testkube」と同じ (api-server のログの `executionId` で API を引く)。
確かめること: トークン無し・不正なトークンの 401、入力の誤りの 422、デプロイの 202・同じ Idempotency-Key の再送・走っている間の 409、
Workflow の完了と Application `deploy-<名前>` の Synced/Healthy、一覧 (Dapr state)、Tempo の trace に 6 つの Pod、削除の完了と Application の消滅、
Kafka のトピック `gitops-deployer-events` の完了イベント 2 つ。
