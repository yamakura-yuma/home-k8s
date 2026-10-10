# Backstage で Grafana のダッシュボードと文書を見る

Backstage (tailnet の <https://backstage.taild2b611.ts.net>、Keycloak でサインイン。ホストからはゲストで <http://localhost:7007>) のカタログに home-k8s を 1 つのエンティティとして載せ、そのページに
Grafana のダッシュボードの一覧を出す。一覧の各行は Grafana (localhost:3000) のダッシュボードへのリンクになる。
あわせて、home-k8s・knowledge-base・dotfiles の markdown の文書を TechDocs で読めるようにする (下の「TechDocs」)。
Backstage は ArgoCD が GitHub の `main` から同期する ([argocd.md](argocd.md))。

## 構成

```
just up
  ├─ docker build -t home-k8s-backstage:0.11.0 backstage   (repo の backstage/。ホストに node は要らない)
  ├─ kind load docker-image home-k8s-backstage:0.11.0       (3 つのノードに入れる)
  ├─ Secret observability/grafana-backstage                 (Grafana の閲覧用ユーザー backstage のパスワード)
  ├─ Secret backstage/backstage-grafana                     (同じ資格情報を Backstage のプロキシ用に)
  ├─ Secret dev・prod/grafana-admin・grafana-backstage      (環境ごとの Grafana の admin と閲覧用ユーザーのパスワード。backstage-grafana.md)
  ├─ Secret backstage/backstage-grafana-env                 (環境ごとの Grafana への Basic 認証。GRAFANA_DEV_BASIC_AUTH・GRAFANA_PROD_BASIC_AUTH)
  ├─ Secret backstage/backstage-azure                      (Azure の資格情報のファイルがあるときだけ。backstage-azure.md)
  ├─ Secret backstage/backstage-argocd                      (ArgoCD の読み取り専用アカウント backstage の API トークン。backstage-argocd.md)
  ├─ Secret backstage/backstage-oidc                        (Keycloak の client backstage の secret。AUTH_OIDC_CLIENT_SECRET。_oidc-secrets)
  └─ Secret backstage/backstage-session                     (session の cookie の署名鍵。AUTH_SESSION_SECRET。無いときだけ乱数で作る)

tailnet の端末 ── Ingress backstage ── Deployment backstage-tailnet (Caddy。/api/auth/guest を 403) ──┐
ホスト ── localhost:7007 (NodePort 30707) ─────────────────────────────────────────────────────┼─▶ Service backstage:7007
share Pod (Cloudflare Quick Tunnel、人ごとの Basic) ──────────────────────────────────────────┘

Application backstage (backstage chart 2.10.2、clusters/kind/backstage/values.yaml)
  Pod backstage ── イメージは kind load したもの (pullPolicy: Never)
    ├─ カタログ: GitHub の main の catalog-info.yaml を読む (home-k8s・knowledge-base・dotfiles)
    ├─ TechDocs: 文書を開いたときに GitHub から取り、Pod の中の mkdocs で HTML にして配る
    └─ /api/proxy/grafana/api ──Basic 認証──▶ grafana.observability.svc (ユーザー backstage、Viewer)
    └─ /api/proxy/grafana-{dev,prod}/api ──Basic 認証──▶ grafana.{dev,prod}.svc (環境ごとの Grafana。backstage-grafana.md)
  └─ /api/proxy/argocd/api  ──Bearer──────▶ argocd-server.argocd.svc (アカウント backstage、Application の get だけ)
```

| 部品 | 置き場所 | 中身 |
|---|---|---|
| アプリ | `backstage/` | `@backstage/create-app@0.9.2` (Backstage 1.55.0、新しいフロントエンドシステム) の雛形を、カタログ・TechDocs・検索だけに絞り、Grafana・ArgoCD・Azure・api-docs のプラグインを足したもの |
| イメージ | `backstage/Dockerfile` | 上流の multi-stage build。yarn install と build もイメージの中で行う |
| Grafana プラグイン | `backstage/packages/app` | `@backstage-community/plugin-grafana` 1.1.0。拡張 `entity-card:grafana/dashboards` を有効にする |
| Grafana のタブ・環境ごとの観測スタック | `backstage/packages/app`・`clusters/kind/env-*` | 既存の Grafana プラグインのカードを環境のタブで出す (`grafana.hosts` に `default`・`dev`・`prod`)。環境ごとに Prometheus・Loki・Tempo・OTel Collector・Grafana を ApplicationSet で立てる。設定は [backstage-grafana.md](backstage-grafana.md) |
| ArgoCD プラグイン | `backstage/packages/app` | `@roadiehq/backstage-plugin-argo-cd` 2.13.1。環境ごとの Application の同期状態と健全性を出す。設定は [backstage-argocd.md](backstage-argocd.md) |
| 環境ごとのタブ | `backstage/packages/app/src/modules/environments` | dev・prod を切り替えて中身を出すエンティティのタブ。注釈の規約と作り方は [environments.md](environments.md) |
| Swagger のタブ・API の定義 | `backstage/packages/app` | `@backstage/plugin-api-docs` 0.14.5 (`/alpha`)。Component sample-api のタブで OpenAPI を Swagger UI で出し、環境ごとのプロキシを向き先にする。[swagger-tab.md](swagger-tab.md) |
| Azure のタブ | `backstage/packages/app/src/modules/azure`・`backstage/packages/backend/src/azureSites.ts` | `@backstage-community/plugin-azure-sites` と `-backend`。資格情報はファイルから `just up` が Secret にする。無くても起動する ([backstage-azure.md](backstage-azure.md)) |
| TechDocs | `backstage/packages/app`・`backstage/packages/backend` | `@backstage/plugin-techdocs` 1.18.2 と `@backstage/plugin-techdocs-backend` 2.3。文書の画面が検索の API を要るので、検索 (`@backstage/plugin-search` と search-backend、カタログと TechDocs の索引) も載せる |
| 設定 | `backstage/app-config.yaml` | ポート 7007、`baseUrl` (tailnet の URL)、インメモリの SQLite、ログイン (Keycloak の OIDC とゲスト)、permission、Grafana・ArgoCD へのプロキシ (`/grafana/api`・`/argocd/api`)、`argocd.baseUrl`・`argocd.revisionsToLoad`、TechDocs |
| ログインと権限 | `backstage/packages/backend/src/keycloakAuth.ts`・`permissionPolicy.ts`、`backstage/packages/app/src/modules/signin`、`clusters/kind/backstage/tailnet-proxy` | 下の「ログインと権限」 |
| chart の values | `clusters/kind/backstage/values.yaml` | イメージ、NodePort 30707、Secret の参照、読む `catalog-info.yaml` |
| エンティティ | `catalog-info.yaml` (repo の直下) | Component `home-k8s` と、ダッシュボードを選ぶ注釈、TechDocs の注釈。User `yamakura-yuma`、Group `admins`・`viewers` (表示用)。サービスの `catalog-info.yaml` (`services/*/`) を読む Location |
| 文書の設定 | `mkdocs.yml` (repo の直下) | `docs/` を HTML にする MkDocs の設定 |

DB はインメモリの SQLite で、Pod を作り直すとカタログは消えるが、起動のたびに GitHub から読み直す。
ログインは入口で分ける (下の「ログインと権限」)。tailnet からは Keycloak の OIDC だけ、ホストの 127.0.0.1 と
常駐の `share` Pod 経由 (人ごとの資格情報と、通す経路の絞り込みを前に置く。下の「別の PC から見る」) はゲストで、読むだけ。

## ログインと権限

決めた形:

| 入口 | サインイン | 権限 |
|---|---|---|
| tailnet `https://backstage.taild2b611.ts.net` | Keycloak (realm `home-k8s`、client `backstage`) の OIDC だけ。ゲストの経路は入口の proxy が 403 で拒む | Keycloak のグループで決まる。admins は何でもでき、viewers は読むだけ。どちらでもない人はサインインできない |
| ホスト `http://localhost:7007` (127.0.0.1 の NodePort) | ゲストだけ (`user:development/guest`) | 読むだけ |
| `share` Pod (Cloudflare Quick Tunnel、人ごとの Basic) | ゲスト (Basic の後) | 読むだけ。share の caddy もカタログの登録の POST を止めている |

ゲストを外さず、127.0.0.1 と share に残したのは次の理由による。

- **share が使っている**。share Pod は Backstage にゲストでサインインさせる (share の caddy は `/api/auth/*` の POST を通す、[share.md](share.md))。
  share は合意の範囲の外 (触らない) で、ゲストを外すと共有先の Backstage が開けなくなる
- **OIDC の callback は tailnet の URL にしか戻らない**。Keycloak の client `backstage` の redirect URI は `https://backstage.taild2b611.ts.net/api/auth/*`
  で、localhost:7007 で開いた画面からは OIDC でサインインできない。ホストで開く手段として、読むだけのゲストを残す
- どの入口のゲストも permission のポリシーで読むだけなので、ゲストで書き込みはできない

tailnet からゲストを使えなくする方法は、Ingress `backstage` の先に Caddy の proxy `backstage-tailnet` (`clusters/kind/backstage/tailnet-proxy`) を
挟み、`/api/auth/guest` の下を 403 で返すこと。Backstage (express) は経路の大文字小文字を区別せず、provider の名前の %エンコードを解くので、
caddy の `path` の matcher (大文字小文字を区別せず、%エンコードを解き、スラッシュの重なりとドットセグメントを畳んだ path に当たる) で拒む。
表記揺れ (`/API/AUTH/GUEST`・`/api/auth/%67uest`・`//api/auth/guest`・`/api/auth/x/../guest`) も届かないことを
`clusters/kind/backstage/test_tailnet_proxy.py` (`just ci`) が caddy を起動して確かめる。サインインの画面も、tailnet の URL (`*.ts.net`) では
Keycloak だけ、それ以外ではゲストだけを出す (`packages/app/src/modules/signin`)。

部品:

| 部品 | 中身 |
|---|---|
| provider `oidc` (`packages/backend/src/keycloakAuth.ts`) | `@backstage/plugin-auth-backend-module-oidc-provider` の `oidcAuthenticator` に、自前の sign-in resolver を付けて provider id `oidc` で登録する。設定は `app-config.yaml` の `auth.providers.oidc.production` (`metadataUrl` = issuer の discovery、`clientId: backstage`、`clientSecret: ${AUTH_OIDC_CLIENT_SECRET}`、`prompt: auto`)。`auth.environment: production` |
| sign-in resolver | userinfo の `preferred_username` をユーザー名にし (`user:default/<名前>`)、`groups` のうち `admins`・`viewers` を `group:default/<グループ>` として Backstage のトークンの ownership (`ent`) に入れる。カタログは引かない (カタログに無い人もサインインできる)。どちらのグループにも入っていなければ「サインインできない」で断る (Grafana の `role_attribute_strict` と同じ扱い) |
| session (`auth.session.secret`) | OIDC の provider (openid-client の Strategy) がログインの途中の state・nonce を session に置くので要る。鍵は Secret `backstage-session` (`AUTH_SESSION_SECRET`、`_oidc-secrets` が無いときだけ乱数で作る)。session はインメモリの DB にあり、Pod を作り直すと消える (サインインし直すだけ) |
| `baseUrl` | `app.baseUrl`・`backend.baseUrl` を tailnet の URL にした。OIDC の callback (`backend.baseUrl/api/auth/oidc/handler/frame`) と、ログインの窓がトークンを渡す先のオリジン (`app.baseUrl`) がこの値になるため。localhost:7007 で開いても、フロントエンドは baseUrl を開いたオリジンに置き換えるので画面は動く |
| permission (`packages/backend/src/permissionPolicy.ts`) | `@backstage/plugin-permission-backend` と自前のポリシー。`permission.enabled: true`。ownership に `group:default/admins` があれば許す。それ以外は、`action` が `read` の permission と、何も書かない検査 2 つ (`catalog.entity.validate`・`catalog.location.analyze`) だけを許し、ほか (カタログの登録・削除・再読み込み、Azure の起動・停止、`action` の無い・知らない permission) は拒む。ownership はポリシーが userInfo サービスで credentials から引く (`PolicyQueryUser.info` は非推奨) |
| サインインの画面 (`packages/app/src/modules/signin`) | API `auth.keycloak` (`OAuth2.create`、provider `oidc`) と、`SignInPageBlueprint` で既定 (ゲストだけ) を置き換えた画面 |
| カタログのユーザーとグループ (`catalog-info.yaml`) | **静的な宣言**。User `yamakura-yuma` と Group `admins`・`viewers` を置く。メンバー (memberOf・members) は書かない |

カタログのユーザーとグループを Keycloak から取り込まず (Keycloak のプラグイン `@backstage-community/plugin-catalog-backend-module-keycloak` を使わず)
静的に置いたのは、権限がカタログではなくサインインのときの `groups` クレームで決まり、取り込みが権限に要らないため。取り込むには、client `backstage` に
realm のユーザーを全部読めるサービスアカウントの権限 (realm-management の view-users など) を足す必要があり、1 人の学習用のクラスタには割に合わない。
Keycloak のグループを変えたら、カタログは変えなくてよい (次のサインインから効く)。人が増えて、カタログでメンバーを見たくなったら取り込みを入れる。

プラグインの版は、Backstage 1.55.0 (`backstage.json`) の版の一覧 <https://versions.backstage.io/v1/releases/1.55.0/manifest.json> (2026-10-11 に引いた) に合わせた。

| package | 版 |
|---|---|
| `@backstage/plugin-auth-backend-module-oidc-provider` | 0.4.21 |
| `@backstage/plugin-permission-backend` | 0.7.16 |
| `@backstage/plugin-permission-node` | 0.11.4 |
| `@backstage/plugin-permission-common` | 0.9.11 |
| `@backstage/plugin-catalog-common` | 1.2.0 |
| `@backstage/core-app-api` (app。`OAuth2`) | 1.20.5 |

制約:

- **localhost:7007 では TechDocs の CSS と画像が出ない**。TechDocs の静的なファイルは cookie (`backstage-auth`) で認証し、Backstage はその cookie の
  `Domain` を `backend.baseUrl` のホスト名 (`backstage.taild2b611.ts.net`) にするので、localhost ではブラウザが捨てる。文書の本文は出る。
  tailnet の URL と share (caddy が `Domain` を外す) では出る
- **Backstage の中のリンクは localhost のまま** (ArgoCD・Grafana・Headlamp のリンク、`argocd.baseUrl`・`grafana.hosts[].domain`)。tailnet の端末からは
  開けない ([tailscale.md](tailscale.md))。Temporal のタブは tailnet の URL にした ([temporal.md](temporal.md))

確かめ方 (使い捨てのテスト用ユーザー `test-admin` (admins)・`test-viewer` (viewers)・`test-nogroup` で、ブラウザなしに authorization code flow を流した。
`/api/auth/oidc/start` → Keycloak のログインフォームに POST → `/api/auth/oidc/handler/frame` → `/api/auth/oidc/refresh` で Backstage のトークン):

| 要求 | test-admin | test-viewer | test-nogroup | ゲスト (localhost) |
|---|---|---|---|---|
| サインイン | できる。ownership `user:default/test-admin`・`group:default/admins` | できる。`group:default/viewers` | 断られる (「admins・viewers のどちらにも入っていない」) | できる (tailnet からは 403) |
| `/api/permission/authorize` の catalog.entity.read | ALLOW | ALLOW | — | ALLOW |
| 同 catalog.entity.refresh・delete、catalog.location.create・delete、azure.sites.update | ALLOW | DENY | — | DENY |
| `POST /api/catalog/refresh` (home-k8s) | 200 | 403 | — | 403 |
| `POST /api/catalog/locations?dryRun=true` | 201 | 403 | — | 403 |

人 (自分のユーザー `yamakura-yuma`、admins) が戻ってから、tailnet の端末のブラウザで確かめること:

1. <https://backstage.taild2b611.ts.net> を開く → サインインの画面に Keycloak だけが出る (Guest は出ない) → Keycloak でサインイン
   (パスワード + TOTP、または passkey。窓が開いて閉じる)
2. 左下の Settings で、ユーザーが `yamakura-yuma`、Ownership に `admins` が出る
3. カタログの home-k8s で、右上のメニューの **Unregister entity** が押せる (admins なので。押さなくてよい)。sample-api の Temporal・ArgoCD・Grafana・Azure のタブが出る
4. ホストのブラウザで <http://localhost:7007> を開くとゲストだけが出て、入ると読むだけ (Unregister entity が出ない・押せない)

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
viewer と `backstage` は別のユーザーなので、viewer のパスワードを変えても Backstage は読み続けられる。

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
  (最初に文書を開いてから最大 10 分後) まで無い。その間の検索は 0 件を返す (上流の Lunr は索引の無い種類の
  検索で `Missing index` の 500 を返すので、`backstage/packages/backend/src/searchEngine.ts` で 0 件に変える)
- 文書の画面は Google Fonts (fonts.googleapis.com) を読まない。mkdocs-material は既定で読むので、
  `techdocs.generator.mkdocs.disableExternalFonts` で build の前に `theme.font: false` を足す。
  文字は端末にあるフォント (mkdocs-material のシステムフォントの指定) になる。リポジトリの `mkdocs.yml` が `theme.font` を書けば、そちらが残る

手元で文書の見た目を確かめるなら、リポジトリの直下で `mkdocs serve` (要 `pip install mkdocs-techdocs-core`)。

## イメージ

レジストリ (GHCR など) には置かず、`just up` が手元で build して `kind load` する。chart の values は
タグを `0.11.0` に固定し、`pullPolicy: Never` で pull しない。クラスタを作り直しても `just up` が入れ直す。

- `docker build` は層のキャッシュが効くので、`backstage/` を変えていなければすぐ終わる。初回は 5 分ほど
- TechDocs の mkdocs は、実行用のイメージに Python の venv (`/opt/venv`) を作って pip で入れる (上流の
  Dockerfile のコメントの手順)。`mkdocs-techdocs-core` の版を固定し、mkdocs などはそれが固定する版に任せる
- 開発用コンテナの docker には buildx が無いので、Dockerfile は BuildKit の機能 (`RUN --mount=type=cache`) を使わない
- アプリを変えたら、`just/backstage.just` の `backstage_image` と `clusters/kind/backstage/values.yaml` の
  `tag` を一緒に上げる (`just/test_recipes.py` が揃っているか確かめる)。同じタグのまま build し直しても、
  Deployment が変わらないので Pod は作り直されない
- `@yarnpkg/core` は 4.9.1 に固定している (`backstage/package.json` の `resolutions`)。4.9.2 は依存の
  `got` に自分の repo の中のパッチを指していて、外から入れると yarn install が失敗する

## 別の PC から見る (just share)

```sh
just share add alice   # 名前・パスワードと、Grafana・headroom・Backstage の 3 つの URL を表示する (既定 8h で失効)
just share get alice   # 3 つの URL を引き直す (パスワードは出ない)
```

Backstage は常駐の `share` Pod (`clusters/kind/share/`、構成は [share.md](share.md)) の caddy `:8083` と、
Cloudflare Quick Tunnel (`*.trycloudflare.com`) を前に置いて公開している。入るには `just share add` が発行した
名前・パスワード (Grafana・headroom と共通の 1 つ) を、Backstage の URL で打つ。使い終わったら `just share delete <名前>`。
URL は `share` Pod を作り直すと変わる。

Backstage は誰でもゲストで入れるので、パスワードは認証サービスで掛ける。ただし Backstage の画面は
API を呼ぶときに `Authorization: Bearer` (Backstage のトークン) を付けるため、すべての要求に Basic 認証を
掛けると画面が動かない。そこで最初のページの読み込みで Basic を通ったブラウザに、認証サービスが署名した cookie
(`share_session`) を渡し、以後はその cookie を持つ要求だけを通す。cookie でも、名前が Secret にあり期限内かを
毎回確かめるので、`delete`・期限切れはすぐ効く。

TechDocs の文書の CSS や画像は、Backstage が発行する cookie (`backstage-auth`) で認証する。Backstage は
この cookie に `backend.baseUrl` のホスト名 (`Domain=backstage.taild2b611.ts.net`。以前は `localhost`) を付けるので、そのままでは trycloudflare の
ホストで捨てられ、文書の画面が読み込み中のまま止まる。caddy はこの `Domain` を外して返す。

7007 をそのまま出すと、Backstage が Grafana の資格情報で読むプロキシ (`/api/proxy/grafana/api`) や、
カタログに任意の URL を読ませる API まで外から使える。caddy (`clusters/kind/share/Caddyfile`) は、
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
共有先でも Grafana のダッシュボードは、同じ資格情報で Grafana の URL から見られる。

## 本番のクラスタに反映する手順 (この PR のマージ後)

kind-config は変えていない (7007 は ArgoCD を入れたときに開けてある) ので、クラスタの作り直しは要らない。

```sh
just up        # Backstage のイメージを build・kind load し、Secret を作り、root を同期し、全部の同期を待つ
kubectl --context kind-study-kind -n argocd get applications   # backstage を含め全部 Synced / Healthy
just show backstage   # https://backstage.taild2b611.ts.net (Keycloak) と http://localhost:7007 (ゲスト)
```

ブラウザで <http://localhost:7007> を開き、ゲストで入って `home-k8s` を開くと、Overview に
「Grafana Dashboards」のカードが出る。

TechDocs は、`home-k8s`・`knowledge-base`・`dotfiles` のページの「TechDocs」タブで本文が出ることを確かめる
(初回は build で数秒待つ)。画面を開かずに確かめるなら、ゲストのトークンで API を引く。

```sh
tok=$(curl -s -X POST http://localhost:7007/api/auth/guest/refresh | jq -r .backstageIdentity.token)
curl -s -H "Authorization: Bearer $tok" 'http://localhost:7007/api/catalog/entities?filter=kind=component' | jq -r '.[].metadata.name'
for e in home-k8s knowledge-base dotfiles; do   # build させる (最後に event: finish が出る)
  curl -s -H "Authorization: Bearer $tok" "http://localhost:7007/api/techdocs/sync/default/component/$e" | tail -n 2
done
curl -s -H "Authorization: Bearer $tok" http://localhost:7007/api/techdocs/static/docs/default/component/home-k8s/cluster/backstage/ | grep -o '<h1[^>]*>[^<]*'
```

マージから ArgoCD が `main` を読むまでに最大 3 分かかる。それより先に `just up` を打つと、`backstage` の
Application がまだ無いので待ちが先に終わることがある。そのときはもう一度 `just up` を打つ。
