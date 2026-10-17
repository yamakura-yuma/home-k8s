# gitops-deployer のアプリの登録状況の DB (設計)

gitops-deployer の kro 版で、デプロイされたアプリの登録状況を外部の API と Backstage が引くための DB の設計。
**設計だけで、まだ実装していない。** 実装はこの設計を承認した後の PR にする。

- 正本は manifest (git の `deploys/` にあるアプリ CR と、クラスタの中のアプリ CR とその status)。DB はそれを写した読み取り用のもの
- 向きは manifest → DB の同期だけ。kro は DB を読まない。Temporal は DB を読まない
- DB を置く理由: 外部の API から登録状況を取るとき、manifest (git やクラスタ) を探すより DB を引くほうが合理的だから (env・状態で絞る、一覧をページで返す)
- **必須の条件は「疎」**: RGD (別の種類の CR) を足しても、同期の側にコードの追加が要らない。特定の CRD の GVK や spec・status の形を知るコードを同期の側に持たない

アプリ CR は kro の ResourceGraphDefinition (RGD) が生やす CRD のインスタンス。kind・group・spec の細部は並行する実装の PR
(gitops-deployer-kro-impl) が決める。この設計は、kro がどのインスタンスにも付ける共通の形 (下の「kro のインスタンスの共通の形」) だけに頼る。

## CR が自分で DB に書くことはできるか

**できない。何かのコードが要る。** CR はただのデータで、書くのは kube-apiserver、置き場は etcd。kro にも外の SQL の DB に書く機能は無い
(kro v0.9.4 の `pkg/`・`cmd/` に `database/sql` を使うコードは無い)。CR の子に「DB の行」を表す CR を置いて書かせる OSS の operator も見つからなかった (下の案 (a))。

コードを置ける場所は 3 つある。

| 場所 | 案 | 一番小さいか |
|---|---|---|
| apiserver の保存先そのものを Postgres にする | (b) kine | × kind の etcd を差し替える工事。表は版の履歴を持つ key-value で、SQL では引きにくい |
| apiserver から出る記録 (audit) を受ける | (c) audit webhook | × 起動時の全件が無く、イベントが落ちうる |
| クラスタの中で watch する 1 つの汎用の投影 | (i) 汎用の投影 / (d) Clusterpedia | ◎ 常駐 1 つで、どの kind にも同じコードで効く |

一番小さいのは「kind を知らない汎用の投影を 1 つ、クラスタの中で動かす」こと。これを推しにする。

## 結論

**汎用の投影 `registry-sync` (案 (i)) を 1 つ置く。** kro が生やした CRD (ラベル `kro.run/owned=true`) を watch し、現れた GVK ごとに
dynamic informer を張り、消えたら外す。インスタンスは kind を問わず PostgreSQL の 1 つの表 `resources` に jsonb で写す。
kind ごとの形 (例: アプリ CR の `env`) は SQL の view で作り、同期の側のコードには持たない。

Temporal は DB に何も書かない。Temporal から DB に渡したい値 (Workflow ID・依頼者) は、git-activity がアプリ CR の annotation に書き、
manifest を通って DB に入る (annotation も表に写る)。

```text
コマンド側 (書き込み。正本は git と K8s の宣言的な状態)
  クライアント ─▶ bff ─▶ api ─▶ Temporal: Workflow app-<名前>
                                   └─ git-activity: deploys/<名前>/<env>.yaml (アプリ CR) を足す・消す PR ─▶ GitHub (merge)
  Argo CD (ApplicationSet gitops-deployer-apps) ─▶ アプリ CR を apply・prune
  kro ─▶ RGD から CRD を生やす (ラベル kro.run/owned=true)
      ─▶ インスタンスから namespace・Deployment・Service・HTTPRoute・ResourceQuota・LimitRange を作り、status を書く
                                   │
                                   │ ① CRD を watch (kro.run/owned=true)  ② GVK ごとに dynamic informer (list + watch)
                                   ▼
クエリ側 (読み取り。投影 = read model)
  registry-sync (Deployment 1 つ。kind を知らない) ─ upsert / delete ─▶ PostgreSQL: 表 resources (jsonb)
                                                                          └─ view apps (アプリ CR の形。SQL だけ)
  外部の API・Backstage ─▶ APISIX ─▶ bff ─▶ api ─ SELECT ─────────────────────┘
```

RGD を足したときに要るもの:

| 足すもの | 同期の側のコード | 要る作業 |
|---|---|---|
| RGD (新しい kind) | **要らない** (CRD が現れれば informer が張られ、表 `resources` に入る) | なし。kind の形で引きたければ view を 1 つ足す (SQL)。RGD の group が新しければ registry-sync の ClusterRole に 1 行足す (manifest) |

## CQRS の発展形として見る

書き込み (コマンド) と読み取り (クエリ) を分ける点は CQRS と同じ。読み取り側の DB は書き込み側の投影 (read model) で、捨てても作り直せる。
普通の CQRS と違う点は 3 つある。

| 点 | 普通の CQRS | この設計 |
|---|---|---|
| コマンドの正本 | アプリが持つ書き込み用の DB (またはイベントストア) | git の `deploys/` と K8s の宣言的な状態 (アプリ CR)。Temporal は git に PR を書くだけで、自分では状態を持たない |
| 投影の元 | コマンドが出すドメインイベント | CR の spec と status の変化 (K8s の watch のイベント)。status は kro が実体を見て書くので、「依頼した」ではなく「クラスタで今どうなっているか」が写る。投影はイベントの種類 (ドメインの語彙) を知らず、どの kind も同じ形 (メタデータ・spec・status) で受ける |
| 結果整合の遅れ | コマンドの処理 → イベント → 投影 | PR の merge → Argo CD の同期 → kro の reconcile → watch → upsert。merge から DB まで数十秒〜数分。DB には「まだ写っていない」がありうる。依頼の進み具合は今までどおり Workflow (`GET /api/v1/deployments/<名前>`) で見る |

投影の元を watch (起動時の list と変化のイベント) にしたので、イベントを取りこぼしても次の list で正しい状態に戻る (level-triggered)。
案の比較はこの点と「疎」で決まった。

## kro のインスタンスの共通の形

kro v0.9.4 のソース (出典 1〜4) で確かめた、どの RGD のインスタンスにも共通する形。同期の側はこれだけに頼る。

| もの | 中身 | 出典 |
|---|---|---|
| 生やした CRD のラベル | `kro.run/owned=true`・`kro.run/resource-graph-definition-name`・`kro.run/resource-graph-definition-id` (RGD の UID)・`kro.run/kro-version` | 1 |
| インスタンスのラベル | 上と同じラベルを kro が server-side apply で付ける | 2 |
| `status.state` | `IN_PROGRESS`・`FAILED`・`ACTIVE`・`DELETING`・`ERROR`。kro がどの CRD の schema にも足す | 3 |
| `status.conditions` | `Ready`・`InstanceManaged`・`GraphResolved`・`ResourcesReady` | 3 |
| CRD の名前 | `<kind の複数形の小文字>.<group>` (group の既定は `kro.run`) | 4 |

RGD の status には生やした CRD の名前や GVK を持つ欄が無い (出典 4)。だから registry-sync は RGD ではなく、ラベル `kro.run/owned=true` で CRD を watch する。

kro と DB の関係は合意どおり「kro は DB を読まない」で、kro にもその手段が無い。kro が他のリソースを読む `externalRef` は
K8s のオブジェクトを GVK ごとの informer で watch するもので (出典 5)、SQL の DB を読む機能は無い。

## 比べた案 (疎を満たすもの)

| 案 | 中身 | 疎 | 起動時の全件・取りこぼし | etcd を変える工事 | SQL での引きやすさ | 常駐物 |
|---|---|---|---|---|---|---|
| **(i) 汎用の投影 (推し)** | 自前の 1 つの Deployment。kro の CRD を watch し、GVK ごとに dynamic informer を張る・外す。表 1 つに jsonb で写し、kind の形は view | ◎ kind を知るコードが無い | ◎ GVK ごとに list → watch。起動時に全件を突き合わせ、表にあって CR に無い行を消す | なし | ◎ 表は自分で決める。共通の `state`・`ready` は列、kind ごとの値は jsonb と view | Deployment 1・Postgres 1 |
| (a) RGD の子に「DB の行」の CR を足し、SQL の operator に書かせる | 各 RGD に行を表す子のリソースを書く | × RGD ごとに子を書く。書かせる operator が OSS に無い (出典 9) ので、作るなら自前の operator | 使う operator 次第 (無い) | なし | — | operator 1 (自作)・Postgres 1 |
| (b) kine (apiserver の保存先を Postgres に) | etcd の代わりに kine を置き、Postgres に保存させる | ◎ apiserver が全部書く | ◎ 保存先そのもの | × kind のクラスタを作り直し、kube-apiserver の `--etcd-servers` を kine に向ける。全リソースの保存がこの Postgres に乗る (出典 6) | × 表 `kine` は `name`・`value bytea`・revision の key-value で、更新のたびに行が増える (圧縮まで履歴が残る)。組み込みの型は protobuf で、CR は JSON (出典 6・7)。最新の行を選び bytea を JSON に直す SQL が要る | kine 1・Postgres 1 (クラスタの心臓部) |
| (c) apiserver の audit webhook | `RequestResponse` の記録を webhook で受け、応答のオブジェクトを写す | ○ kind を問わない (受け手でフィルタ) | × audit はリクエストの記録だけで、今ある状態の一覧が無い。batch モードでは溢れると捨てる (出典 8)。突き合わせの list を別に持つことになる | △ kind の kube-apiserver に audit の policy・webhook の設定を足す (kubeadmConfigPatches、クラスタの作り直し) | ○ 受け手が表を決める | 受け手 1・Postgres 1・突き合わせ |
| (d) 既存の OSS: Clusterpedia | PediaCluster の `syncResources` に kro の group と `resources: ["*"]` を書き、Postgres に写させる | ◎ 新しい CRD を再起動なしで拾う (出典 10) | ◎ informer の list → watch。消えた CR は行も消える (出典 10) | なし | △ 表 `resources` の `object` が jsonb で引ける。ただし表は Clusterpedia の内部の保存の形で、公式の読み口は集約 API (APIService)。SQL で直接引くと版の上げで壊れうる | apiserver・clustersynchro-manager・controller-manager の 3・APIService・Postgres 1 |

(d) の他の候補も見た: Steampipe の Kubernetes プラグインは CR を動的な表にするが、引くたびに API を呼ぶ (DB に写さない。出典 11)。
CloudQuery の k8s プラグインは決まった表だけで、任意の CR のインスタンスを写さない (出典 12)。

判断:

- **(a)** は書かせる OSS が無く、RGD ごとに子を書くので疎でもない。
- **(b)** は「CR がそのまま DB に入る」に一番近いが、kind の etcd を差し替える工事が大きく、表は apiserver の保存の形 (key-value と履歴) で SQL では引きにくい。登録状況の DB のためにクラスタの心臓部を替えるのは割に合わない。
- **(c)** は起動時の全件が無く、溢れると捨てる。投影は全件が要るので、(i) と同じ list を別に持つことになり、書き手が 2 つになる。
- **(d) Clusterpedia** は自前のコードが要らず、疎も全件も満たす。落とした理由は常駐物の多さ (3 つと APIService) と、SQL で引く相手が Clusterpedia の内部の表になる点。
  同期を始める kind 全部 (`group: "*"`) は alpha の feature gate が要り (出典 10)、クラスタ自身を同期する手順は文書に無い (kubeconfig か token で `kubernetes.default.svc` を指せば足りるはずだが未確認)。
- **(i)** は常駐が 1 つで、表を自分で決められる。自前のコードは要るが、kind を知らないので一度書けば RGD を足しても触らない。GVK ごとに informer を張り・外す形は kro 自身が `pkg/watch/manager.go` でやっている形と同じ (出典 13)。

## v2 までの案 (疎でない)

v2 では、アプリ CR の GVK を知る専用のコントローラ (案 B) を推していた。動きはするが、別の RGD を写すたびにコードを足すことになる。

| 案 | 中身 | 疎でない理由 |
|---|---|---|
| A. ApiServerSource → Kafka Broker → Trigger → Knative Service | 既存の Knative の経路に乗せる | ApiServerSource の `resources` にアプリ CR の GVK を書き、受け手が spec の形を知る。さらに起動時・watch の張り直し時の list を送らない (出典 14) |
| B. アプリ CR 専用のコントローラ (v2 の推し) | アプリ CR の informer で表 `apps` に写す | informer の GVK と、表の列 (`env` など spec の項目) がコードに固定。RGD を足すたびに informer と表とコードが要る |
| C. store-activity を広げる | Temporal が Redis に書く | Workflow ごとに書く値を決めるコードが要る。クラスタの実体 (kro の status) も写らない |
| D. Argo の Application を写す | event-receiver に DB 書き込みを足す | Application はアプリ CR の spec を持たず、粒度も合わない。A と同じ取りこぼし |

## 推しの中身 (案 (i))

### 置くもの (実装の PR で足す)

| もの | 場所 | 中身 |
|---|---|---|
| `registry-sync` | `services/gitops-deployer/cmd/registry-sync`・`internal/registry` | 同じイメージの 7 つ目の Pod (Deployment、1 台)。CRD の informer (ラベル `kro.run/owned=true`) と、GVK ごとの dynamic informer |
| PostgreSQL | `services/gitops-deployer/manifests/registry-postgres.yaml` | StatefulSet `registry-postgres` (namespace `gitops-deployer`、`postgres:17.6-alpine`。Keycloak の DB と同じ形 ([keycloak.md](keycloak.md)))。PVC は kind の既定の StorageClass でよい (投影なので消えても埋まる)。pg_dump は置かない |
| 権限 | `services/gitops-deployer/manifests/rbac.yaml` | ServiceAccount `registry-sync` に、`customresourcedefinitions` の get・list・watch と、RGD が使う group の `resources: ["*"]` の list・watch |
| 表と view | `internal/registry` の migration (起動時に `CREATE TABLE IF NOT EXISTS`) | 下の「表」。view `apps` は読み取り側 (api) の migration に置く |
| 読み取り | `internal/api`・`internal/bff`・OpenAPI | api が view を SELECT する。外部への口は bff の API (APISIX・Keycloak の JWT はそのまま) |

API・bff・Backstage・OpenAPI は今回の話題では変えない (合意)。読み取りの API の形は実装の PR で決める。

RBAC は label で絞れないので、group で絞る。RGD の group を 1 つ (例: 実装の PR が決めるアプリ CR の group) に揃えておけば、RGD を足しても RBAC は変わらない。

### 誰が何を読み、誰が書くか

| 主体 | 読む | 書く |
|---|---|---|
| Temporal (workflow-engine・git-activity) | GitHub・Argo の Application のイベント (今までどおり) | git の `deploys/` (アプリ CR)。アプリ CR に annotation `gitops-deployer.home-k8s/workflow-id`・`gitops-deployer.home-k8s/requested-by` を付ける。**DB には書かない** |
| store-activity | なし | 今の Dapr state `history` (Workflow の実行の履歴) をそのまま書く。登録状況の DB とは別物 |
| kro | RGD・インスタンス | CRD・子のリソース・インスタンスの status。DB は読まない |
| registry-sync | kro の CRD・そのインスタンス (メタデータ・spec・status) | Postgres の表 `resources` (書くのはここだけ) |
| api (外部の API・Backstage の入口) | Postgres の view `apps`、Dapr state `history` | なし (DB には) |

Temporal が DB に書く必要があるかは、**書かない**と決めた。DB に要る値で manifest に無いのは依頼の文脈 (Workflow ID・依頼者) だけで、
annotation にすれば manifest → DB の一本の向きで入る。書き手が registry-sync だけになるので、同じ行を 2 者が書く順序の問題も起きない。
PR の番号は CR を書く時点で決まっていないので annotation に載せない (PR は Workflow の履歴の `history` から引く)。

### 表

列は K8s のオブジェクトの共通の形と、kro がどのインスタンスにも付ける `status.state`・`Ready` だけ。kind ごとの項目は列にしない。

```sql
CREATE TABLE resources (
  api_group        text        NOT NULL,          -- 例 kro.run
  version          text        NOT NULL,          -- 例 v1alpha1
  kind             text        NOT NULL,
  namespace        text        NOT NULL DEFAULT '', -- クラスタスコープなら ''
  name             text        NOT NULL,
  uid              uuid        NOT NULL,          -- 作り直しを見分ける
  rgd_name         text,                          -- ラベル kro.run/resource-graph-definition-name
  labels           jsonb       NOT NULL DEFAULT '{}',
  annotations      jsonb       NOT NULL DEFAULT '{}', -- workflow-id・requested-by もここ
  spec             jsonb,
  status           jsonb,
  state            text,                          -- status.state (kro の共通)
  ready            boolean     NOT NULL DEFAULT false, -- status.conditions の Ready が True で、observedGeneration が metadata.generation と同じ
  generation       bigint      NOT NULL,
  resource_version text        NOT NULL,          -- 写した版 (調べるときの手がかり。順序の比較には使わない)
  deleting         boolean     NOT NULL DEFAULT false, -- deletionTimestamp が付いている
  synced_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (api_group, kind, namespace, name)
);
CREATE INDEX resources_kind_ready ON resources (api_group, kind, ready, name);
CREATE INDEX resources_spec ON resources USING gin (spec jsonb_path_ops);
```

kind の形は view で作る (読み取り側の SQL。同期の側のコードではない)。アプリ CR の例 (group・kind・spec の項目は実装の PR に合わせる):

```sql
CREATE VIEW apps AS
SELECT name,
       spec->>'env'                     AS env,
       namespace,
       spec->>'image'                   AS image,
       state, ready,
       annotations->>'gitops-deployer.home-k8s/workflow-id'  AS workflow_id,
       annotations->>'gitops-deployer.home-k8s/requested-by' AS requested_by,
       synced_at
FROM resources
WHERE api_group = 'kro.run' AND kind = 'App';
```

view を足さなくても、新しい kind は `resources` に入った時点で `WHERE kind = … AND spec->>'…' = …` で引ける。

### registry-sync の動き

1. 起動: CRD の informer (ラベル `kro.run/owned=true`) を始める。揃ったら、CRD ごとに GVK を出し (served・storage の版)、GVK ごとの informer を始める
2. GVK の informer が揃ったら (`HasSynced`)、その GVK の全件を upsert し、表にあってその GVK の CR に無い行を delete する
3. 変化 (add・update): その CR の行を upsert する (`INSERT … ON CONFLICT … DO UPDATE`)。行の中身は informer のキャッシュの最新の CR から作るので、
   イベントの順序が入れ替わっても最後に書くのは最新の状態
4. 削除 (delete): 行を delete する。kro が子を消している間は deletionTimestamp が付き `state` が `DELETING` になるので、その間は行が残り、状態で見える
5. CRD が現れた (RGD を足した): その GVK の informer を始める (1・2 と同じ)。CRD が消えた (RGD を消した): その GVK の informer を止め、その GVK の行を消す
6. DB に書けなければ、その CR をワークキューに戻して指数バックオフで打ち直す (client-go の workqueue)。Postgres が戻れば追いつく
7. 定期の resync (例 10 分) で全件を upsert し直す。手で表を触った・書き込みに失敗し続けた、のどちらでも戻る

GVK ごとに止めるため、informer は `DynamicSharedInformerFactory` ではなく `NewFilteredDynamicInformer` で 1 つずつ作り、それぞれに context を持たせる。
factory の `Start` はすべての informer に同じ stop を渡し、1 つだけ止める手段が無いため (出典 13)。kro の `pkg/watch/manager.go` が同じ形 (出典 13)。

1 台で動かす (書き手を 1 つにする)。2 台にするなら leader election を入れる。

### 順序 (CRD がまだ無いとき)

CRD は kro が RGD を reconcile するまで無い。registry-sync は CRD を watch しているので、無ければ何もせず、現れたら informer を始める
(CrashLoop にしない)。Argo CD の sync wave で RGD より後にする必要はない。

### 観測

既存の OTel の設定 (`internal/obs`) をそのまま使う。span は upsert・delete ごと、メトリクスは GVK ごとの
「最後に写した時刻」と「キャッシュの CR の数と表の行の数の差」。差が続けば投影がずれている。

## 決めきれず人に聞きたいこと

1. 自前のコード (registry-sync、kind を知らない数百行) を持つか、自前のコードを持たず常駐が 3 つ増える Clusterpedia (案 (d)) にするか。推しは (i)
2. 外部の API の形: 今の一覧 `GET /api/v1/deployments` (Dapr state の `history` から作る) を view `apps` に替えるか、`GET /api/v1/apps?env=` のような別の口を足すか
3. `history` (Workflow の実行の履歴、Redis) も PostgreSQL に移して DB を 1 つにするか。移すなら Temporal (store-activity) が DB に書くものが「実行の記録」として残る

## 出典

1. kro v0.9.4 の生やした CRD のラベル: https://github.com/kubernetes-sigs/kro/blob/v0.9.4/pkg/controller/resourcegraphdefinition/controller_reconcile.go#L377-L385 、 https://github.com/kubernetes-sigs/kro/blob/v0.9.4/pkg/metadata/labels.go#L154-L206
2. kro v0.9.4 のインスタンスへのラベル: https://github.com/kubernetes-sigs/kro/blob/v0.9.4/pkg/controller/instance/controller.go#L401-L436
3. kro v0.9.4 の `status.state`・`status.conditions`: https://github.com/kubernetes-sigs/kro/blob/v0.9.4/pkg/graph/crd/crd.go#L97-L101 、 https://github.com/kubernetes-sigs/kro/blob/v0.9.4/api/v1alpha1/instance_state.go 、 https://github.com/kubernetes-sigs/kro/blob/v0.9.4/api/v1alpha1/conditions.go#L60-L63
4. kro v0.9.4 の RGD の schema・status と CRD の名前: https://github.com/kubernetes-sigs/kro/blob/v0.9.4/api/v1alpha1/resourcegraphdefinition_types.go#L50-L74 、 https://github.com/kubernetes-sigs/kro/blob/v0.9.4/api/v1alpha1/resourcegraphdefinition_types.go#L295-L314 、 https://github.com/kubernetes-sigs/kro/blob/v0.9.4/pkg/graph/crd/crd.go#L36-L47
5. kro v0.9.4 の文書「External References」: https://github.com/kubernetes-sigs/kro/blob/v0.9.4/website/docs/docs/concepts/rgd/02-resource-definitions/05-external-references.md
6. kine v0.17.2 の Postgres の表と、標準の K8s で使えること: https://github.com/k3s-io/kine/blob/v0.17.2/pkg/drivers/pgsql/pgsql.go#L36-L56 、 https://github.com/k3s-io/kine/blob/v0.17.2/README.md
7. K8s の文書「Custom Resources」(CRD は Protocol Buffers を使えない): https://github.com/kubernetes/website/blob/c6df00c6/content/en/docs/concepts/extend-kubernetes/api-extension/custom-resources.md#L225
8. K8s の文書「Auditing」(RequestResponse、webhook の batch モードは溢れると捨てる): https://github.com/kubernetes/website/blob/cc8fd815/content/en/docs/tasks/debug/debug-cluster/audit.md 。kind での有効化: https://kind.sigs.k8s.io/docs/user/auditing/
9. 行を書く operator を探した先: crossplane-contrib/provider-sql v0.16.2 (Database・Role・Grant・Extension・Schema・DefaultPrivileges だけ)、ariga/atlas-operator v0.8.1 (スキーマとマイグレーション)、schemahero v0.26.2 (Table の `seedData.rows` は表の定義の中の固定の行): https://github.com/schemahero/schemahero/blob/v0.26.2/config/crds/v1/schemas.schemahero.io_tables.yaml#L993-L1020
10. Clusterpedia v0.9.1: 表 `resources` https://github.com/clusterpedia-io/clusterpedia/blob/v0.9.1/pkg/storage/internalstorage/types.go#L90-L114 、全部を同期する feature gate https://github.com/clusterpedia-io/clusterpedia/blob/v0.9.1/pkg/synchromanager/clustersynchro_manager.go#L444-L454 ・ https://github.com/clusterpedia-io/clusterpedia/blob/v0.9.1/pkg/synchromanager/features/features.go#L32-L42 、新しい CRD を拾う https://github.com/clusterpedia-io/clusterpedia/blob/v0.9.1/pkg/runtime/discovery/dynamic_discovery.go#L107-L121 ・ https://github.com/clusterpedia-io/clusterpedia/blob/v0.9.1/pkg/synchromanager/clustersynchro/cluster_synchro.go#L321-L346 、list → watch と削除 https://github.com/clusterpedia-io/clusterpedia/blob/v0.9.1/pkg/synchromanager/clustersynchro/default_resource_synchro.go#L274-L306 ・ https://github.com/clusterpedia-io/clusterpedia/blob/v0.9.1/pkg/storage/internalstorage/resource_storage.go#L161-L175 、部品 https://github.com/clusterpedia-io/clusterpedia/tree/v0.9.1/deploy
11. Steampipe の Kubernetes プラグイン v1.7.0 (動的な表): https://github.com/turbot/steampipe-plugin-kubernetes/blob/v1.7.0/kubernetes/plugin.go#L42 、Postgres の FDW (引くときに API を呼ぶ): https://github.com/turbot/steampipe-postgres-fdw/blob/v2.2.7/README.md
12. CloudQuery の k8s プラグイン (決まった表): https://github.com/cloudquery/cloudquery/tree/plugins-source-k8s-v6.2.4/plugins/source/k8s/docs/tables
13. client-go v0.37.1 の dynamic informer (factory の `Start` は共通の stop、単体の `NewFilteredDynamicInformer`): https://github.com/kubernetes/client-go/blob/v0.37.1/dynamic/dynamicinformer/informer.go#L91-L147 。kro が GVR ごとに informer を張り・止める: https://github.com/kubernetes-sigs/kro/blob/v0.9.4/pkg/watch/manager.go#L134-L136 ・ https://github.com/kubernetes-sigs/kro/blob/v0.9.4/pkg/watch/manager.go#L226-L231
14. Knative Eventing の ApiServerSource の adapter。`resourceDelegate` の `Replace` (reflector の list を受ける) は何もしない: https://github.com/knative/eventing/blob/6fad7f57a5f083f54d4ae842282778e283fa67ab/pkg/adapter/apiserver/delegate.go#L122-L124
