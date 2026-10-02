# Backstage で Grafana のダッシュボードと文書を見る

Backstage (<http://localhost:7007>) のカタログに home-k8s を 1 つのエンティティとして載せ、そのページに
Grafana のダッシュボードの一覧を出す。一覧の各行は Grafana (localhost:3000) のダッシュボードへのリンクになる。
あわせて、home-k8s・knowledge-base・dotfiles の markdown の文書を TechDocs で読めるようにする (下の「TechDocs」)。
Backstage は ArgoCD が GitHub の `main` から同期する ([argocd.md](argocd.md))。

## 構成

```
just up
  ├─ docker build -t home-k8s-backstage:0.2.0 backstage   (repo の backstage/。ホストに node は要らない)
  ├─ kind load docker-image home-k8s-backstage:0.2.0       (3 つのノードに入れる)
  ├─ Secret observability/grafana-backstage                 (Grafana の閲覧用ユーザー backstage のパスワード)
  └─ Secret backstage/backstage-grafana                     (同じ資格情報を Backstage のプロキシ用に)

Application backstage (backstage chart 2.10.2、clusters/kind/backstage/values.yaml)
  Pod backstage ── イメージは kind load したもの (pullPolicy: Never)
    ├─ カタログ: GitHub の main の catalog-info.yaml を読む (home-k8s・knowledge-base・dotfiles)
    ├─ TechDocs: 文書を開いたときに GitHub から取り、Pod の中の mkdocs で HTML にして配る
    └─ /api/proxy/grafana/api ──Basic 認証──▶ grafana.observability.svc (ユーザー backstage、Viewer)
```

| 部品 | 置き場所 | 中身 |
|---|---|---|
| アプリ | `backstage/` | `@backstage/create-app@0.9.2` (Backstage 1.55.0、新しいフロントエンドシステム) の雛形から、カタログと Grafana プラグイン以外を外したもの |
| イメージ | `backstage/Dockerfile` | 上流の multi-stage build。yarn install と build もイメージの中で行う |
| Grafana プラグイン | `backstage/packages/app` | `@backstage-community/plugin-grafana` 1.1.0。拡張 `entity-card:grafana/dashboards` を有効にする |
| TechDocs | `backstage/packages/app`・`backstage/packages/backend` | `@backstage/plugin-techdocs` 1.18 と `@backstage/plugin-techdocs-backend` 2.3。文書の画面が検索の API を要るので、検索 (`@backstage/plugin-search` と search-backend、カタログと TechDocs の索引) も載せる |
| 設定 | `backstage/app-config.yaml` | ポート 7007、インメモリの SQLite、ゲストのログイン、Grafana へのプロキシ、TechDocs |
| chart の values | `clusters/kind/backstage/values.yaml` | イメージ、NodePort 30707、Secret の参照、読む `catalog-info.yaml` |
| エンティティ | `catalog-info.yaml` (repo の直下) | Component `home-k8s` と、ダッシュボードを選ぶ注釈、TechDocs の注釈 |
| 文書の設定 | `mkdocs.yml` (repo の直下) | `docs/` を HTML にする MkDocs の設定 |

DB はインメモリの SQLite で、Pod を作り直すとカタログは消えるが、起動のたびに GitHub から読み直す。
ログインはゲストだけ (`auth.providers.guest.dangerouslyAllowOutsideDevelopment: true`)。127.0.0.1 にしか
出さないので認証を付けていない。外に出すのは `just share backstage` のときだけで、そのときは caddy の
パスワードと、通す経路の絞り込みを前に置く (下の「別の PC から見る」)。

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

Grafana は永続化していないので、API で作るサービスアカウントのトークンは Pod の作り直しで消える。
トークンは値を指定して作れないので、ホストのファイルから入れることもできない。そこで Viewer の専用
ユーザー `backstage` を作り、プロキシは Basic 認証で読む。

1. `just up` (`just/grafana-secrets.sh`) が `~/.local/share/home-k8s/observability/grafana-backstage-password`
   を (無ければ作って) 読み、Secret `observability/grafana-backstage` と、Basic 認証の値
   (`backstage:<パスワード>` の base64) を入れた Secret `backstage/backstage-grafana` を作る。
2. Grafana の Pod のサイドカー `viewer-user` が、ユーザー `backstage` が無ければ作り、居てパスワードが
   Secret と違えば合わせる ([claude-code-traces.md](../observability/claude-code-traces.md) の「Grafana の認証」)。
3. Backstage の chart が Secret `backstage-grafana` を環境変数 `GRAFANA_BASIC_AUTH` にし、`app-config.yaml`
   の `proxy.endpoints./grafana/api.headers.Authorization` が `Basic ${GRAFANA_BASIC_AUTH}` として使う。

ArgoCD の管理対象は Secret を名前で参照するだけで、公開 repo にも ArgoCD にも中身は入らない。
`just share` が作り直すのは viewer のパスワードだけなので、共有しても Backstage は読み続けられる。

## TechDocs

カタログのエンティティのうち、`catalog-info.yaml` に注釈 `backstage.io/techdocs-ref: dir:.` を持つものは、
ページに「TechDocs」のタブが出て、そのリポジトリの markdown を読める。左の「Docs」(`/docs`) は文書の一覧。

| エンティティ | リポジトリ | 文書の置き場所 (`mkdocs.yml` の `docs_dir`) |
|---|---|---|
| `home-k8s` | yamakura-yuma/home-k8s | `docs/` |
| `knowledge-base` | yamakura-yuma/knowledge-base | `docs/src` (`data/` は出さない) |
| `dotfiles` | yamakura-yuma/dotfiles | `docs/` |

```text
ブラウザがエンティティの TechDocs を開く
  └─ techdocs-backend: Pod の中に build 済みの HTML が無い、または古ければ
       ├─ GitHub からリポジトリを取る (mkdocs.yml と docs_dir)
       ├─ mkdocs build (イメージの /opt/venv に入れた mkdocs-techdocs-core)
       └─ Pod の中に置いて配る (publisher: local)
```

- build は文書を開いたときに Pod の中で行う (`techdocs.builder: local`、`generator.runIn: local`)。
  CI も、build 済みの HTML の置き場 (S3 など) も持たない。初回は 1 つあたり数秒待つ
- build した HTML は Pod の中にあるだけなので、Pod を作り直すと消え、次に開いたときに build し直す
- 1 度 build した文書は、開くたびに GitHub の main と比べ、変わっていれば build し直す
  (同じ文書の確認は 1 分に 1 回まで)。main に push した文書は、次に開いたときに反映される
- 3 つとも公開リポジトリなので、GitHub のトークンは渡していない (`integrations.github` を書かない)。
  トークンが無いと、カタログの `catalog-info.yaml` は raw.githubusercontent.com から読み、API の回数を使わない。
  GitHub API (トークン無しは IP ごとに 1 時間 60 回まで) を使うのは TechDocs の build と更新の確認だけで、
  検証では 3 つを build して 4 回、3 つを開き直して 3 回だった。何もしていない間は使わない。
  足りなくなったら (ログに `API rate limit exceeded`)、トークンを Secret にして `integrations.github` に渡す
- 新しいリポジトリの文書を足すときは、そのリポジトリに `catalog-info.yaml` (注釈 `backstage.io/techdocs-ref: dir:.`)
  と `mkdocs.yml` (`plugins: [techdocs-core]`) を置き、`clusters/kind/backstage/values.yaml` の
  `catalog.locations` に `catalog-info.yaml` の URL を足す。非公開のリポジトリはトークンが要る
- home-k8s の `docs/certification/` の HTML (認定試験のまとめ) は、mkdocs が手を加えずにそのまま
  コピーするので、`roadmap.md` からのリンク (`index.html`) で開ける。TechDocs の見た目にはならない
- 文書の画面 (TechDocs のタブと `/docs`) は検索欄を出し、検索の API が無いと開けない。そのため検索の
  プラグインも載せている。索引はメモリに持ち、起動後と 10 分ごとに作り直す。TechDocs の索引に
  入るのは、build 済みの文書だけ。起動直後はどの文書も build していないので、TechDocs の索引は次の作り直し
  (最初に文書を開いてから最大 10 分後) まで無く、それまで検索欄は何も返さない (ログに `Missing index for techdocs`)

手元で文書の見た目を確かめるなら、リポジトリの直下で `mkdocs serve` (要 `pip install mkdocs-techdocs-core`)。

## イメージ

レジストリ (GHCR など) には置かず、`just up` が手元で build して `kind load` する。chart の values は
タグを `0.2.0` に固定し、`pullPolicy: Never` で pull しない。クラスタを作り直しても `just up` が入れ直す。

- `docker build` は層のキャッシュが効くので、`backstage/` を変えていなければすぐ終わる。初回は 5 分ほど
- TechDocs の mkdocs は、実行用のイメージに Python の venv (`/opt/venv`) を作って pip で入れる (上流の
  Dockerfile のコメントの手順)。`mkdocs-techdocs-core` の版を固定し、mkdocs などはそれが固定する版に任せる
- 開発用コンテナの docker には buildx が無いので、Dockerfile は BuildKit の機能 (`RUN --mount=type=cache`) を使わない
- アプリを変えたら、`just/backstage.just` の `backstage_image` と `clusters/kind/backstage/values.yaml` の
  `tag` を一緒に上げる (`just/test_recipes.py` が揃っているか確かめる)。同じタグのまま build し直しても、
  Deployment が変わらないので Pod は作り直されない
- `@yarnpkg/core` は 4.9.1 に固定している (`backstage/package.json` の `resolutions`)。4.9.2 は依存の
  `got` に自分の repo の中のパッチを指していて、外から入れると yarn install が失敗する

## 別の PC から見る (just share backstage)

```sh
just share backstage   # URL・ユーザー (viewer)・パスワードを表示する。Ctrl-C で止めると URL は無効になる
```

Grafana の `just share` ([claude-code-traces.md](../observability/claude-code-traces.md) の「別の PC から見る」)
と同じく、Cloudflare Quick Tunnel (`*.trycloudflare.com`) と caddy (127.0.0.1:3003) を前に置き、
localhost:7007 を一時的に公開する。パスワードは共有のたびに作る使い捨てで、表示するだけでファイルには残さない。
使い終わったら必ず止める。

Backstage は誰でもゲストで入れるので、パスワードは caddy の basic_auth で掛ける。ただし Backstage の画面は
API を呼ぶときに `Authorization: Bearer` (Backstage のトークン) を付けるため、すべての要求に basic_auth を
掛けると画面が動かない。そこで最初のページの読み込みで basic_auth を通ったブラウザに cookie
(`share_session`、値は共有のたびに作る) を渡し、以後はその cookie を持つ要求だけを通す。

TechDocs の文書の CSS や画像は、Backstage が発行する cookie (`backstage-auth`) で認証する。Backstage は
この cookie に `backend.baseUrl` のホスト名 (`Domain=localhost`) を付けるので、そのままでは trycloudflare の
ホストで捨てられ、文書の画面が読み込み中のまま止まる。caddy はこの `Domain` を外して返す。

7007 をそのまま出すと、Backstage が Grafana の資格情報で読むプロキシ (`/api/proxy/grafana/api`) や、
カタログに任意の URL を読ませる API まで外から使える。caddy (`just/share-backstage.Caddyfile`) は、
画面と TechDocs を読むのに要る経路だけを通し、残りは 404 にする。

| 経路 | メソッド | 外から | 理由 |
|---|---|---|---|
| 画面 (`/`、`/catalog/*`、`/docs/*`、`/search` など) と `/static/*` | GET | 開ける | 画面を出す |
| `/api/catalog/*` | GET | 開ける | エンティティの読み取り |
| `/api/catalog/entities/by-refs` | POST | 開ける | 画面がエンティティをまとめて読む (読み取り) |
| `/api/techdocs/*` | GET | 開ける | 文書の読み取りと build (sync) |
| `/api/search/query` | GET | 開ける | 文書の画面の検索欄 |
| `/api/auth/*` | GET・POST | 開ける | ゲストのログイン |
| `/api/proxy/*` (`/api/proxy/grafana/api` など) | すべて | 404 | Grafana の API を Backstage の資格情報で読める |
| `/api/catalog/locations` などカタログへの書き込み | POST・DELETE など | 404 | カタログに任意の URL を足す・消す |
| そのほかの POST・PUT・PATCH・DELETE | すべて | 404 | 読むだけの共有で要らない |

Grafana のダッシュボードの一覧 (Overview のカード) は `/api/proxy` を通すので、共有先では読めずエラーになる。
共有先で Grafana を見せたいときは `just share grafana` を別に立てる。

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
