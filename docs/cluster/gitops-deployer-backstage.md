# gitops-deployer を Backstage から使う

[gitops-deployer](gitops-deployer.md) へのデプロイ・削除の依頼と、デプロイ済みの一覧を Backstage ([backstage.md](backstage.md)) から扱う。

- 依頼: Create の画面のテンプレート「gitops-deployer: アプリをデプロイする」「gitops-deployer: アプリを削除する」。フォームの値を
  Backstage のバックエンドが bff に送り、Workflow が終わるまでタスクの画面に段 (opening-pr・waiting-for-argocd …) を出す
- 一覧: カタログのエンティティ `component:default/gitops-deployer` のタブ **Deployments**。bff の一覧 API の中身 (名前・状態・イメージ・最後の操作・Temporal・PR・Argo CD へのリンク)
- 図: 処理のアニメーション [gitops-deployer-flow.html](gitops-deployer-flow.html) の「Backstage から」、仕様書 [gitops-deployer-spec.html](gitops-deployer-spec.html) の経路と認証

## 経路

```text
ブラウザ ─▶ Backstage (Create のフォーム / Deployments のタブ)
  Backstage のバックエンド (packages/backend/src/gitopsDeployer.ts)
    ├─ Keycloak (realm home-k8s) に client credentials (client backstage-gitops-deployer) でトークンを取る。期限の 1 分前まで使い回す
    ├─ Scaffolder のアクション gitops-deployer:deploy ─ POST   /api/v1/deployments        (Idempotency-Key: backstage-<タスクの id>)
    │                          gitops-deployer:delete ─ DELETE /api/v1/deployments/<名前>
    │    └─ 10 秒ごとに GET /api/v1/deployments/<名前> で Workflow (runId) が終わるまで待つ (上限 30 分)
    └─ /api/gitops-deployer/deployments (タブが読む) ─ GET /api/v1/deployments を全ページ
         ─▶ istio-ingress/gateway (HTTPRoute) ─▶ APISIX /gitops-deployer (JWT・レート制限) ─▶ bff ─▶ api ─▶ Temporal …
```

外の利用者や testkube の E2E と同じ入口 (Gateway → APISIX → bff) を通す。トークンはバックエンドの中だけで使い、ブラウザには渡さない。
トークン無しで `/gitops-deployer/api/v1/*` を叩くと、これまでどおり APISIX が 401 を返す。

| 部品 | 置き場所 | 中身 |
|---|---|---|
| テンプレート | `services/gitops-deployer/backstage/deploy.yaml`・`delete.yaml` | kind Template。フォームの項目は bff の App (名前・イメージ・ポート・レプリカ数)。pattern と範囲は bff (`internal/spec/spec.go`) の写しで、正本は bff の検証 |
| エンティティ | `services/gitops-deployer/catalog-info.yaml` | Component `gitops-deployer` (注釈 `home-k8s/gitops-deployer: "true"` でタブを出す)・API `gitops-deployer` (bff の OpenAPI)・テンプレートの Location。repo の直下の `catalog-info.yaml` から読む |
| アクションと中継 | `backstage/packages/backend/src/gitopsDeployer.ts` | 上の経路。Scaffolder のバックエンドのモジュールと、一覧を中継するバックエンドのプラグイン `gitops-deployer` |
| タブ | `backstage/packages/app/src/modules/environments/GitopsDeployerView.tsx` | 既存のアプリ内のモジュール (environments) の拡張 `entity-content:environments/gitops-deployer`。core-components の Table で一覧を出す。新しいプラグインのパッケージは作らない |
| trace | `backstage/packages/backend/src/instrumentation.js` | アクションの span と bff への呼び出し (undici) を OTLP/HTTP で観測スタックの Collector へ。下の「trace」 |
| 設定 | `backstage/app-config.yaml` の `gitopsDeployer` | 入口の URL・トークンの URL・client の id。secret は環境変数 `GITOPS_DEPLOYER_CLIENT_SECRET` |

## 認証と権限

| 何 | 決めたこと | 理由 |
|---|---|---|
| Backstage → bff | Keycloak の client `backstage-gitops-deployer` (confidential、service account のみ) の client credentials。アクセストークンの aud に `gitops-deployer`、client role `deploy`・`view` | APISIX と bff がそのまま受け入れる形 (E2E の client `gitops-deployer` と同じ)。client を E2E と分けたので、bff の監査ログと履歴の requestedBy が `service-account-backstage-gitops-deployer` になり、Backstage からの依頼を見分けられる |
| 資格情報 | `just up` の `_keycloak-secrets` が `~/.local/share/home-k8s/keycloak/clients/backstage-gitops-deployer` を作り (realm の import の placeholder と同じ値)、`_gitops-deployer-secrets` が Secret `backstage/backstage-gitops-deployer` にする。chart が環境変数にする (optional) | リポジトリに値を置かない。Secret が無くても Backstage は起動し、アクションとタブが「資格情報が無い」を出す |
| 依頼できる人 | Backstage の admins だけ。テンプレートのフォームとタスクの経過は誰でも読める | Scaffolder のタスクを始める permission (`scaffolder.task.create`) は action が create なので、既存のポリシー (`permissionPolicy.ts`) が admins 以外を拒む。ゲスト・viewers は Create の画面を開けても実行で 403 |
| 一覧を読む人 | サインインした人 (ゲストを含む) | 一覧は読むだけ。bff の view の権限は Backstage の client が持つ |
| 利用者ごとの主体 | bff には渡さない (主体は Backstage の client)。誰が依頼したかは Backstage のタスク (createdBy) と、アクションの span の属性 `backstage.user` に残る | 利用者のトークンを bff まで運ぶには、Backstage のフロントエンドから Keycloak のトークンを取って Scaffolder の secrets に入れる自作の部品が要る (合意書の除外) |

### Idempotency-Key は依頼者ごと

api は Idempotency-Key を Dapr state の鍵 `idem|<依頼者>|<鍵>` に残す (依頼者は bff が JWT から作る `X-Requested-By`、`url.PathEscape` して `|` を含まないようにする)。
別の依頼者 (Backstage と E2E など別の client) が同じ鍵を使っても、相手の応答は返らず、それぞれの依頼として扱う
(`services/gitops-deployer/internal/api/api.go` の `idempotencyStateKey`、試験 `TestIdempotencyKeyIsScopedToTheRequester`)。
Backstage のアクションは鍵にタスクの id を使うので、同じタスクのやり直しは同じ応答になる。

## trace

Backstage のバックエンドは `instrumentation.js` を `node --require` で先に読み (values の `NODE_OPTIONS`)、次だけを trace にする。

- アクションの span `gitops-deployer template:default/gitops-deployer-deploy` など (service.name `backstage`。属性 `gitops_deployer.app`・`backstage.task_id`・`backstage.user`・`temporal.workflow_id`)
- その中の fetch (undici の計装、親の span があるときだけ)。Keycloak のトークンと bff への POST・GET。traceparent が Gateway → APISIX → bff に届き、bff → api → workflow-engine → … の trace に繋がる

カタログの GitHub の読み込みなど、ほかの外向きの呼び出しと、受けた HTTP の要求は trace にしない (Tempo を埋めない)。
Grafana (観測スタック) の Tempo で `service.name = backstage` を引くと、子に bff 以下の span が並ぶ。

## 使う

1. <https://backstage.taild2b611.ts.net/create> (admins でサインイン) で「gitops-deployer: アプリをデプロイする」を選ぶ
2. 名前・イメージ (`docker.io/nginxinc/nginx-unprivileged:1.29-alpine` など)・ポート・レプリカ数を入れて Create。タスクの画面に PR の URL と段が流れ、
   Application `deploy-<名前>` が Synced/Healthy になると終わる。出力に Workflow (Temporal)・PR・Application (Argo CD) のリンクが出る
3. <https://backstage.taild2b611.ts.net/catalog/default/component/gitops-deployer/deployments> で一覧と状態を見る (右上の ↻ で読み直す)
4. 消すときは「gitops-deployer: アプリを削除する」に名前を入れる

走っている間に同じアプリを依頼すると、bff の 409 がそのままタスクの誤りになる。

## 作り直したクラスタ・初めて入れるとき

`just up` が client の secret のファイル・Secret `keycloak-clients` のキー・Secret `backstage/backstage-gitops-deployer` を作り、realm の import が client を入れる。
動いている Keycloak (realm がすでにある) には import が効かないので、[keycloak.md](keycloak.md) の「realm を後から変える」のとおり kcadm で足す
(この PR では、`clusters/kind/auth/keycloak/realm-home-k8s.yaml` の client `backstage-gitops-deployer` と同じ定義を kcadm で入れ、
service account に `gitops-deployer` の client role `deploy`・`view` を付けた)。
