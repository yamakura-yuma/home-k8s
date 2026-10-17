# gitops-deployer のアプリの登録状況の DB (設計)

gitops-deployer の kro 版で、デプロイされたアプリの登録状況を外部の API と Backstage が引くための DB の設計。
**設計だけで、まだ実装していない。** 実装はこの設計を承認した後の PR にする。

- 正本は manifest (git の `deploys/` にあるアプリ CR と、クラスタの中のアプリ CR とその status)。DB はそれを写した読み取り用のもの
- 向きは manifest → DB の同期だけ。kro は DB を読まない。Temporal は DB を読まない
- DB を置く理由: 外部の API から登録状況を取るとき、manifest (git やクラスタ) を探すより DB を引くほうが合理的だから (env・状態で絞る、一覧をページで返す)

アプリ CR は kro の ResourceGraphDefinition (RGD) が生やす CRD のインスタンス。kind・group・spec の細部は並行する実装の PR
(gitops-deployer-kro-impl) が決める。この設計は kro のインスタンスの一般的な形 (下の「アプリ CR の形の前提」) だけを前提にし、
細部は実装の PR に合わせる。

## 結論

**専用の小さなコントローラ `registry-sync` がアプリ CR を watch し、PostgreSQL の表 `apps` に写す (案 B)。**
Temporal は DB に何も書かない。Temporal から DB に渡したい値 (Workflow ID・依頼者) は、git-activity がアプリ CR の annotation に書き、
manifest を通って DB に入る。

```text
コマンド側 (書き込み。正本は git と K8s の宣言的な状態)
  クライアント ─▶ bff ─▶ api ─▶ Temporal: Workflow app-<名前>
                                   └─ git-activity: deploys/<名前>/<env>.yaml (アプリ CR) を足す・消す PR ─▶ GitHub (merge)
  Argo CD (ApplicationSet gitops-deployer-apps) ─▶ アプリ CR を apply・prune
  kro ─▶ namespace <名前>-<env>・Deployment・Service・HTTPRoute・ResourceQuota・LimitRange を作り、アプリ CR の status を書く
                                   │
                                   │ watch (informer: 起動時の list + 変化のイベント)
                                   ▼
クエリ側 (読み取り。投影 = read model)
  registry-sync (Deployment 1 つ、cmd/registry-sync) ─ upsert / delete ─▶ PostgreSQL gitops-deployer-registry: 表 apps
                                                                               ▲
  外部の API・Backstage ─▶ APISIX ─▶ bff ─▶ api ─ SELECT ──────────────────────┘
```

## CQRS の発展形として見る

書き込み (コマンド) と読み取り (クエリ) を分ける点は CQRS と同じ。読み取り側の DB は書き込み側の投影 (read model) で、捨てても作り直せる。
普通の CQRS と違う点は 3 つある。

| 点 | 普通の CQRS | この設計 |
|---|---|---|
| コマンドの正本 | アプリが持つ書き込み用の DB (またはイベントストア) | git の `deploys/` と K8s の宣言的な状態 (アプリ CR)。Temporal は git に PR を書くだけで、自分では状態を持たない |
| 投影の元 | コマンドが出すドメインイベント | アプリ CR の spec と status の変化 (K8s の watch のイベント)。status は kro が実体を見て書くので、「依頼した」ではなく「クラスタで今どうなっているか」が写る |
| 結果整合の遅れ | コマンドの処理 → イベント → 投影 | PR の merge → Argo CD の同期 → kro の reconcile → watch → upsert。merge から DB まで数十秒〜数分。DB には「まだ写っていない」がありうる。依頼の進み具合は今までどおり Workflow (`GET /api/v1/deployments/<名前>`) で見る |

投影の元を watch にしたので、イベントを取りこぼしても次の list で正しい状態に戻る (level-triggered)。これがイベントで投影を作る普通の CQRS との一番の違いで、
案の比較もこの点で決まった。

## アプリ CR の形の前提

kro v0.9.4 の文書 (出典 1・2) から、インスタンスについて次を前提にする。

- apiVersion は RGD の `spec.schema.group` (省略すれば `kro.run`) と `spec.schema.apiVersion` (例 `v1alpha1`)
- `spec` は RGD の schema が決める (この話題では `env`・イメージ・ポート・レプリカ数・LimitRange)
- `status.state` (`ACTIVE`・`IN_PROGRESS`・`FAILED`・`DELETING`・`ERROR`) と `status.conditions` (`Ready` を見る。下位の condition は変わりうる)。
  RGD の `status` に書いた値 (例 `availableReplicas`) もここに入る
- kro が付けるラベル `kro.run/owned`・`kro.run/resource-graph-definition-name` など

DB に写すのは `spec` の全体 (jsonb)、`status.state`、`Ready` の condition、RGD の status の値。列の名前と spec の項目は実装の PR に合わせる。

kro と DB の関係は合意どおり「kro は DB を読まない」で、kro にもその手段が無い。kro が他のリソースを読む `externalRef` は
K8s のオブジェクトを GVK ごとの informer で watch するもので (出典 3)、SQL の DB を直接読む機能は文書に無い。

## 比べた案

写す元・書き手・DB の種類の組み合わせから、筋の通る 4 つを比べた。

| 案 | 写す元 | 書き手 | DB | 取りこぼし・作り直し | 既存の部品に乗るか | 増えるもの | Temporal が書くもの |
|---|---|---|---|---|---|---|---|
| A. Knative の経路に乗せる | アプリ CR (spec・status) | ApiServerSource → Kafka Broker → 新しい Trigger → Knative Service `registry-writer` | PostgreSQL | **×** ApiServerSource は起動時・watch の張り直し時の list を送らない (出典 4)。止まっている間の削除は DB に残り続け、DB を作り直しても埋まらない。別に全件の突き合わせ (CronJob) が要る | ◎ 既存の Broker・Dead Letter・ordered の Trigger | Trigger 1・Knative Service 1 (0↔1)・突き合わせの CronJob・ApiServerSource の権限 | なし |
| **B. 専用の小さなコントローラ (推し)** | アプリ CR (spec・status) | Deployment `registry-sync` (同じイメージ、client-go の informer) | PostgreSQL | ◎ 起動時の list で全件を突き合わせ、DB に有って CR に無い行を消す。DB を捨てても起動で埋まる | ○ イメージ・OTel・Postgres の運用 (Keycloak の DB の形) に乗る。Knative の経路には乗らない | Deployment 1 (常駐、1 台)・Postgres 1・ClusterRole (アプリ CR の get・list・watch) | なし |
| C. store-activity を広げる | Workflow の結果 (依頼の値) | store-activity (Temporal の Activity) | Redis (Dapr state `history`) | × Workflow の外の変化 (手で消した、kro が FAILED にした、Argo の差分の巻き戻し) が写らない。manifest → DB の同期にならない | ◎ そのまま | ほぼなし | 登録・変更・削除のすべて |
| D. Argo の Application を写す | Application (sync・health・revision) | event-receiver に DB 書き込みを足す | PostgreSQL | △ A と同じ取りこぼし。Application はアプリと env の単位でなく、spec の値も持たない | ◎ 今の ApiServerSource をそのまま使う | event-receiver の責務が増える (Signal と DB の失敗が同じ 5xx の打ち直しに混ざる) | なし |

判断:

- **A** は既存の経路に一番よく乗るが、Knative の ApiServerSource の adapter は reflector の初回の list (と watch を張り直したときの list) を
  `Replace` で受けて何も送らない (出典 4)。今の event-receiver は「走っている Workflow へ Signal する」用途なので、これで困らない
  (Workflow が待っている間の変化だけが要る)。投影は全件が要るので、取りこぼしを埋める仕組みを別に持つことになり、書き手が 2 つになる。
- **B** は K8s のコントローラの基本の形 (list → watch、level-triggered) で、上の取りこぼしが起きない。常駐の Pod が 1 つ増えるが、
  アプリ CR の数は少なく、informer のキャッシュも小さい。
- **C** は「Temporal は登録・変更の最小限」に反し、クラスタの実体 (kro の status) も写らない。
- **D** は粒度が合わない (Application は ApplicationSet の生成物で、アプリ CR の spec を持たない)。

DB の種類:

| 観点 | Redis (Dapr state `history` と同じ置き場) | PostgreSQL |
|---|---|---|
| 外部の API の問い合わせ (env・状態で絞る、名前の順のページング) | △ 二次索引を自分で持つ (今の `apps` の鍵と同じ手作り) | ◎ `WHERE env = $1 AND ready ORDER BY name LIMIT` |
| 既存の運用 | Redis 8.10.2 の Deployment 1 つ。AuthorizationPolicy で namespace `gitops-deployer` だけを通す | Keycloak・Temporal の DB と同じ公式イメージ `postgres:17.6-alpine` の StatefulSet ([keycloak.md](keycloak.md)) |
| データを失ったとき | 投影なので B なら起動で埋まる | 同左 |

投影は作り直せるので、残す強さ (PV の `Retain`・pg_dump) は決め手にならない。決め手は問い合わせのしやすさで、PostgreSQL にした。

## 推しの中身 (案 B)

### 置くもの (実装の PR で足す)

| もの | 場所 | 中身 |
|---|---|---|
| `registry-sync` | `services/gitops-deployer/cmd/registry-sync`・`internal/registry` | 同じイメージの 7 つ目の Pod (Deployment、1 台)。アプリ CR の dynamic informer。起動時と変化のたびに upsert・delete |
| PostgreSQL | `services/gitops-deployer/manifests/registry-postgres.yaml` | StatefulSet `registry-postgres` (namespace `gitops-deployer`、`postgres:17.6-alpine`)。PVC は kind の既定の StorageClass でよい (投影なので消えても埋まる)。pg_dump は置かない |
| 権限 | `services/gitops-deployer/manifests/rbac.yaml` | ServiceAccount `registry-sync` に、アプリ CR の get・list・watch (アプリ CR の置き場の namespace だけなら Role) |
| 表 | `internal/registry` の migration (起動時に `CREATE TABLE IF NOT EXISTS`) | 下の「表」 |
| 読み取り | `internal/api`・`internal/bff`・OpenAPI | api が Postgres を SELECT する。外部への口は bff の API (APISIX・Keycloak の JWT はそのまま) |

API・bff・Backstage・OpenAPI は今回の話題では変えない (合意)。読み取りの API の形 (今の一覧 `GET /api/v1/deployments` を DB に替えるか、別の口を足すか) は実装の PR で決める。

### 誰が何を読み、誰が書くか

| 主体 | 読む | 書く |
|---|---|---|
| Temporal (workflow-engine・git-activity) | GitHub・Argo の Application のイベント (今までどおり) | git の `deploys/` (アプリ CR)。アプリ CR に annotation `gitops-deployer.home-k8s/workflow-id`・`gitops-deployer.home-k8s/requested-by` を付ける。**DB には書かない** |
| store-activity | なし | 今の Dapr state `history` (Workflow の実行の履歴) をそのまま書く。登録状況の DB とは別物 |
| kro | アプリ CR | 子のリソースとアプリ CR の status。DB は読まない |
| registry-sync | アプリ CR (spec・status・annotation) | Postgres の表 `apps` (書くのはここだけ) |
| api (外部の API・Backstage の入口) | Postgres の表 `apps`、Dapr state `history` | なし (DB には) |

Temporal が DB に書く必要があるかは、**書かない**と決めた。DB に要る値で manifest に無いのは依頼の文脈 (Workflow ID・依頼者) だけで、
git-activity がアプリ CR を書くときに annotation にすれば manifest → DB の一本の向きで入る。書き手が registry-sync だけになるので、
同じ行を 2 者が書く順序の問題も起きない。PR の番号は CR を書く時点で決まっていないので annotation に載せない
(PR は Workflow の履歴の `history` から引く)。

### 表

```sql
CREATE TABLE apps (
  name                text        NOT NULL,          -- アプリの名前
  env                 text        NOT NULL,          -- feature / dev / stg / prod (spec.env)
  namespace           text        NOT NULL,          -- <名前>-<env>
  spec                jsonb       NOT NULL,          -- アプリ CR の spec の全体 (イメージ・ポート・レプリカ数・LimitRange)
  state               text,                          -- status.state (ACTIVE / IN_PROGRESS / FAILED / DELETING / ERROR)
  ready               boolean     NOT NULL DEFAULT false, -- status.conditions の Ready が True で、その observedGeneration が metadata.generation と同じ
  status              jsonb,                         -- status の全体 (RGD の status の値を含む)
  workflow_id         text,                          -- annotation gitops-deployer.home-k8s/workflow-id
  requested_by        text,                          -- annotation gitops-deployer.home-k8s/requested-by
  cr_uid              uuid        NOT NULL,          -- アプリ CR の metadata.uid (作り直しを見分ける)
  generation          bigint      NOT NULL,
  resource_version    text        NOT NULL,          -- 写した版 (順序の比較には使わない。調べるときの手がかり)
  synced_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (name, env)
);
CREATE INDEX apps_env_ready ON apps (env, ready, name);
```

### registry-sync の動き

1. 起動: informer がアプリ CR を list する。キャッシュが揃ったら (`HasSynced`)、全件を upsert し、表にあって CR に無い `(name, env)` を delete する
2. 変化 (add・update): その CR の行を upsert する (`INSERT … ON CONFLICT (name, env) DO UPDATE`)。行の中身は informer のキャッシュの最新の CR から作るので、
   イベントの順序が入れ替わっても最後に書くのは最新の状態
3. 削除 (delete): 行を delete する。kro が子を消している間は CR に deletionTimestamp が付き `state` が `DELETING` になるので、その間は行が残り、状態で見える
4. DB に書けなければ、その CR をワークキューに戻して指数バックオフで打ち直す (client-go の workqueue)。Postgres が戻れば追いつく
5. 定期の resync (例 10 分) で全件を upsert し直す。手で表を触った・書き込みに失敗し続けた、のどちらでも戻る

1 台で動かす (書き手を 1 つにする)。2 台にするなら leader election を入れる。

### 順序 (アプリ CR の CRD がまだ無いとき)

アプリ CR の CRD は kro が RGD を reconcile するまで無い。registry-sync は起動時に CRD が無ければ、ログを出して待ち、
discovery で現れたら informer を始める (CrashLoop にしない)。Argo CD の sync wave で RGD より後にする必要はない。

### 観測

既存の OTel の設定 (`internal/obs`) をそのまま使う。span は upsert・delete ごと、メトリクスは
「最後に写した時刻」と「CR の数と表の行の数の差」。差が続けば投影がずれている。

## 決めきれず人に聞きたいこと

1. 外部の API の形: 今の一覧 `GET /api/v1/deployments` (Dapr state の `history` から作る) を表 `apps` に替えるか、`GET /api/v1/apps?env=` のような別の口を足すか
2. `history` (Workflow の実行の履歴、Redis) も PostgreSQL に移して DB を 1 つにするか。移すなら Temporal (store-activity) が DB に書くものが「実行の記録」として残る
3. 常駐の Pod が 1 つ増えるのを受け入れるか。Knative の経路にこだわるなら案 A + 突き合わせの CronJob になる (書き手が 2 つ)

## 出典

1. kro v0.9.4 の文書「Instances」(状態・conditions・ラベル): https://github.com/kubernetes-sigs/kro/blob/v0.9.4/website/docs/docs/concepts/15-instances.md
2. kro v0.9.4 の文書「Schema」(group の既定 `kro.run`、status の値): https://github.com/kubernetes-sigs/kro/blob/v0.9.4/website/docs/docs/concepts/rgd/01-schema.md
3. kro v0.9.4 の文書「External References」(K8s のオブジェクトを GVK ごとの informer で読む): https://github.com/kubernetes-sigs/kro/blob/v0.9.4/website/docs/docs/concepts/rgd/02-resource-definitions/05-external-references.md
4. Knative Eventing の ApiServerSource の adapter。`resourceDelegate` の `Replace` (reflector の list を受ける) は何もしない: https://github.com/knative/eventing/blob/6fad7f57a5f083f54d4ae842282778e283fa67ab/pkg/adapter/apiserver/delegate.go#L122-L124
