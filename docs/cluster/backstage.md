# Backstage で Grafana のダッシュボードを見る

Backstage (<http://localhost:7007>) のカタログに home-k8s を 1 つのエンティティとして載せ、そのページに
Grafana のダッシュボードの一覧を出す。一覧の各行は Grafana (localhost:3000) のダッシュボードへのリンクになる。
Backstage は ArgoCD が GitHub の `main` から同期する ([argocd.md](argocd.md))。

## 構成

```
just up
  ├─ docker build -t home-k8s-backstage:0.1.0 backstage   (repo の backstage/。ホストに node は要らない)
  ├─ kind load docker-image home-k8s-backstage:0.1.0       (3 つのノードに入れる)
  ├─ Secret observability/grafana-backstage                 (Grafana の閲覧用ユーザー backstage のパスワード)
  └─ Secret backstage/backstage-grafana                     (同じ資格情報を Backstage のプロキシ用に)

Application backstage (backstage chart 2.10.2、clusters/kind/backstage/values.yaml)
  Pod backstage ── イメージは kind load したもの (pullPolicy: Never)
    ├─ カタログ: GitHub の main の catalog-info.yaml を読む
    └─ /api/proxy/grafana/api ──Basic 認証──▶ grafana.observability.svc (ユーザー backstage、Viewer)
```

| 部品 | 置き場所 | 中身 |
|---|---|---|
| アプリ | `backstage/` | `@backstage/create-app@0.9.2` (Backstage 1.55.0、新しいフロントエンドシステム) の雛形から、カタログと Grafana プラグイン以外を外したもの |
| イメージ | `backstage/Dockerfile` | 上流の multi-stage build。yarn install と build もイメージの中で行う |
| Grafana プラグイン | `backstage/packages/app` | `@backstage-community/plugin-grafana` 1.1.0。拡張 `entity-card:grafana/dashboards` を有効にする |
| 設定 | `backstage/app-config.yaml` | ポート 7007、インメモリの SQLite、ゲストのログイン、Grafana へのプロキシ |
| chart の values | `clusters/kind/backstage/values.yaml` | イメージ、NodePort 30707、Secret の参照、読む `catalog-info.yaml` |
| エンティティ | `catalog-info.yaml` (repo の直下) | Component `home-k8s` と、ダッシュボードを選ぶ注釈 |

DB はインメモリの SQLite で、Pod を作り直すとカタログは消えるが、起動のたびに GitHub から読み直す。
ログインはゲストだけ (`auth.providers.guest.dangerouslyAllowOutsideDevelopment: true`)。127.0.0.1 にしか
出さないので認証を付けていない。

## ダッシュボードの選び方

エンティティのページに出るダッシュボードは、`catalog-info.yaml` の注釈 `grafana/dashboard-selector` で選ぶ。
Grafana の `/api/search` が返すダッシュボードを、このセレクタで絞る。使える変数は `title`・`tags`・`url`・
`folderTitle`・`folderUrl`、演算子は `||`・`&&`・`==`・`!=`・`@>` (配列が含む)・`!`
([プラグインの説明](https://github.com/backstage/community-plugins/blob/main/workspaces/grafana/plugins/grafana/docs/dashboards-on-component-page.md))。

```yaml
annotations:
  grafana/dashboard-selector: "tags @> 'claude-code' || tags @> 'claude-code-setting' || tags @> 'Kubernetes' || tags @> 'opentelemetry'"
```

いま repo が Grafana に入れるダッシュボード (27 個) は、どれもこの 4 つのタグのどれかを持つ。
新しいダッシュボードを足すときは、JSON の `tags` にこのどれかを入れるか、セレクタを足す。
フォルダで選ぶなら `folderTitle == 'Claude Code 設定項目別'` のようにも書ける。

## Grafana の読み方

Grafana のプラグインは、Backstage のバックエンドのプロキシ (`/api/proxy/grafana/api`) を通して Grafana の
API を読む。資格情報はプロキシが付けるので、ブラウザには渡らない。

API で作るサービスアカウントのトークンは値を指定して作れないので、ホストのファイルから入れられない
(`grafana.db` を作り直すと消え、作り直したトークンを手で渡し直すことになる)。そこで Viewer の専用
ユーザー `backstage` を作り、プロキシは Basic 認証で読む。

1. `just up` (`just/grafana-secrets.sh`) が `~/.local/share/home-k8s/observability/grafana-backstage-password`
   を (無ければ作って) 読み、Secret `observability/grafana-backstage` と、Basic 認証の値
   (`backstage:<パスワード>` の base64) を入れた Secret `backstage/backstage-grafana` を作る。
2. Grafana の Pod のサイドカー `viewer-user` が、Pod の起動ごとに一度だけ、ユーザー `backstage` が無ければ作り、
   居ればパスワードを Secret の値に合わせる ([claude-code-traces.md](../observability/claude-code-traces.md) の「Grafana の認証」)。
3. Backstage の chart が Secret `backstage-grafana` を環境変数 `GRAFANA_BASIC_AUTH` にし、`app-config.yaml`
   の `proxy.endpoints./grafana/api.headers.Authorization` が `Basic ${GRAFANA_BASIC_AUTH}` として使う。

ArgoCD の管理対象は Secret を名前で参照するだけで、公開 repo にも ArgoCD にも中身は入らない。
`just share` が作り直すのは viewer のパスワードだけなので、共有しても Backstage は読み続けられる。

## イメージ

レジストリ (GHCR など) には置かず、`just up` が手元で build して `kind load` する。chart の values は
タグを `0.1.0` に固定し、`pullPolicy: Never` で pull しない。クラスタを作り直しても `just up` が入れ直す。

- `docker build` は層のキャッシュが効くので、`backstage/` を変えていなければすぐ終わる。初回は 3 分ほど
- 開発用コンテナの docker には buildx が無いので、Dockerfile は BuildKit の機能 (`RUN --mount=type=cache`) を使わない
- アプリを変えたら、`just/backstage.just` の `backstage_image` と `clusters/kind/backstage/values.yaml` の
  `tag` を一緒に上げる (`just/test_recipes.py` が揃っているか確かめる)。同じタグのまま build し直しても、
  Deployment が変わらないので Pod は作り直されない
- `@yarnpkg/core` は 4.9.1 に固定している (`backstage/package.json` の `resolutions`)。4.9.2 は依存の
  `got` に自分の repo の中のパッチを指していて、外から入れると yarn install が失敗する

## 本番のクラスタに反映する手順 (この PR のマージ後)

kind-config は変えていない (7007 は ArgoCD を入れたときに開けてある) ので、クラスタの作り直しは要らない。

```sh
just up        # Backstage のイメージを build・kind load し、Secret を作り、root を同期し、全部の同期を待つ
kubectl --context kind-study-kind -n argocd get applications   # backstage を含め全部 Synced / Healthy
just show backstage   # http://localhost:7007 (ゲストで入る)
```

ブラウザで <http://localhost:7007> を開き、ゲストで入って `home-k8s` を開くと、Overview に
「Grafana Dashboards」のカードが出る。

マージから ArgoCD が `main` を読むまでに最大 3 分かかる。それより先に `just up` を打つと、`backstage` の
Application がまだ無いので待ちが先に終わることがある。そのときはもう一度 `just up` を打つ。
