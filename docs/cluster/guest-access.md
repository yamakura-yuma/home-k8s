# ゲスト (他の人) の入れ方 — 比較と推奨

状態: 調査と比較だけ (2026-10-11)。実装はしていない。人が方式を選んでから、下の「実装の段取り」の単位で進める。

`just share` (Cloudflare Quick Tunnel + 人ごとの Basic、[share.md](share.md)) は廃止する (別の単位 `share-retire`)。
この文書は、その後で他の人 (ゲスト) に home-k8s の UI (Grafana、Backstage、headroom のダッシュボードなど) を見せる方法を並べて比べ、
ゲスト像ごとに推奨を出す。ゲスト像はまだ決めていない (2026-10-11 のユーザーの回答) ので、1 つに絞らず像ごとに書く。

前提にする今の構成は次のとおり ([tailscale.md](tailscale.md)、[keycloak.md](keycloak.md)、[kube-oidc.md](kube-oidc.md))。

- UI と Keycloak は Tailscale の Kubernetes operator の Ingress で `https://<名前>.taild2b611.ts.net` に出ている。tailnet の外からは名前が引けない
- 各 UI は Keycloak (realm `home-k8s`、issuer `https://keycloak.taild2b611.ts.net/realms/home-k8s`) の OIDC でログインし、グループ `admins`・`viewers` で権限を分ける
- headroom のダッシュボードは、ホストのプロセス (`127.0.0.1:8787`) で OIDC を持たない。外へは share の中継だけが出していた (share と一緒に消える)
- 選ぶ基準は [access-control.md](access-control.md) と同じ: 無料で済むこと、軽くて運用が楽なこと、業務でも通用する構成であること

表の記号は [idp-options.md](idp-options.md) と同じ。

- **[公式]**: ベンダーの公式ドキュメントで今回確かめた仕様。出典は末尾にある
- **[観測]**: この repo とその docs から読み取った現状。クラスタは今回触っていない
- **[未確認]**: 一次資料で確かめられなかったもの、または推測

## 結論

- **Tailscale を入れられるゲスト → 届き方は Tailscale の端末共有 (A1)、アカウントは Keycloak のグループ `guests` (C)**。
  インターネットに何も出さずに済み、費用は 0 で、Tailscale の Personal の 6 ユーザーの枠も使わない見込み (下の比較)。
  ただし、operator の proxy (タグ `tag:k8s` の付いた端末) を外に共有できるかは公式の文書に書いておらず [未確認]、実装の最初に 1 台で試す。
  できなかったときは tailnet への招待 (A2。6 ユーザーの枠を 1 人 1 つ使う) に替える
- **Tailscale を入れられないゲスト → Tailscale Funnel で、ゲストに見せる UI と Keycloak だけをインターネットに出す (B1)。アカウントは C**。
  Funnel は無料で、URL は今の `*.ts.net` のまま変わらない。代わりに Keycloak のログイン画面がインターネットに出るので、
  Keycloak の前に経路を絞る proxy を置き (`/admin/` と realm `master` を出さない)、ブルートフォースの検知を入れる (下の「公開するときの Keycloak の守り」)
- **期限は Keycloak の Workflows で付ける**。Keycloak のユーザーには期限の項目が無い [公式]。26.6 で正式になった Workflows に
  「グループ `guests` に入ったら N 日後に `disable-user`」と書ける [公式]。CronJob で kcadm.sh を回す形は、Workflows が使えないときの控え
- **外部 IdP の仲介 (D: Google・GitHub) は今は入れない**。ゲストのパスワードと TOTP の登録を省けるが、
  知らない人のアカウントが自動で作られないよう first broker login の設定を変える手間と、相手が Google か GitHub のアカウントを持つ前提が増える。
  ゲストが増えて一時パスワードの受け渡しが負担になったら足す
- **headroom のダッシュボードは、最初は見せない**。見せるなら oauth2-proxy (`--provider=keycloak-oidc`) を前に置くが、headroom はホストの loopback にいるので、
  share の中継と同じ「GET の許可リストだけを通すホスト側の中継」を作り直すことになる (下の「OIDC を持たない UI」)
- **ゲストに見せるのは Grafana (観測スタック) と Backstage まで**。ArgoCD・Temporal UI・Headlamp・Grafana (dev・prod) は、`guests` に権限を付けず、
  A1 なら共有もしない、B1 なら Funnel にも出さない
- **使わないもの**: Cloudflare の名前付き Tunnel と Access は、Cloudflare 上の自分のドメイン (登録料がかかる) が要るので外す。
  Cloudflare Quick Tunnel は URL が変わり issuer を置けないので、OIDC の UI を見せられない (share の形に戻る)

## ゲスト像

| ゲスト像 | 例 | ゲストが用意するもの | 推奨 |
|---|---|---|---|
| **G1: Tailscale を入れられる人** | 技術者の友人、同じ勉強会の人。何度か見に来る | Tailscale のアカウント (無料) とアプリ、TOTP の認証アプリ | A1 (だめなら A2) + C |
| **G2: Tailscale を入れられない人** | 会社の PC しか持たない人、アプリを入れたくない人。ブラウザだけ | TOTP の認証アプリ (passkey でもよい) | B1 + C + Keycloak の守り |
| **G3: 1 回だけ、画面を見せたいだけの人** | ダッシュボードの 1 枚を見せたい。アカウントを作らせたくない | ブラウザだけ | 画面の共有 (会議ツール) か、スクリーンショット。仕組みは足さない。足すなら下の「G3 の控え」 |

G3 に仕組みを足さない理由は、どの候補もアカウント無しで見せる形になり、share をやめた理由 (URL を知る人が誰でも入れる・資格情報を別に管理する) に戻るため。

### G3 の控え (採らない)

- **Grafana の外部共有のダッシュボード** (以前の名前は Public dashboards): URL を知る人は誰でも、読むだけで 1 枚のダッシュボードを見られる [公式]。
  ただし変数を使うダッシュボードとクエリは動かない [公式]。観測スタックのダッシュボード 6 枚のうち 5 枚が変数を使う [観測]。
  OSS で使えるかは、文書に Enterprise の印が無いことからの推測 [未確認]。URL を届ける経路は結局 B1 の Funnel が要る
- **Cloudflare Quick Tunnel の `--allowed-mail`**: Quick Tunnel の前に、指定したメールアドレスへのワンタイム PIN の確認を置ける。訪問者に Cloudflare のアカウントは要らない [公式]。
  ただし Quick Tunnel は試験と開発用で、同時 200 要求までで、SSE を通さず、作り直すと URL が変わる [公式]。
  OIDC の UI は Keycloak (tailnet の中) に戻れないので、Grafana の Basic を差し込む proxy (share の caddy と同じもの) が要る

## 比較

### 届き方

| 候補 | ゲストが要るもの | 費用・人数 | URL (issuer をそのまま使えるか) | インターネットへの露出 | 取り消し | 判断 |
|---|---|---|---|---|---|---|
| **A1: Tailscale の端末共有** (Ingress の proxy の端末を、相手の tailnet に共有する) | Tailscale のアカウントとアプリ。相手は自分の tailnet の Owner・Admin・IT admin として招待を受ける [公式] | 全プランで使える [公式]。共有を受けた人が 6 ユーザーに数えられるかは、共有の文書には書いていない。Funnel と共有の比較の文書が「tailnet への招待 (ユーザーの数が増える)」と対比している [公式] ので、数えない見込み [未確認] | 使える。共有された端末は FQDN (`<名前>.<tailnet>.ts.net`) でだけ引ける [公式]。HTTPS の証明書の名前も同じ [未確認] | 無い | 管理画面の Machines から共有を取り消す [公式] | **G1 の推奨**。共有は端末ごとなので、見せる UI ごとと Keycloak の proxy の端末を 1 つずつ共有する [公式] |
| A2: tailnet への招待 (Invite external users) | Tailscale のアカウントとアプリ | 全プランで使える [公式]。招待した人は請求の対象 (= ユーザーに数える) [公式]。Personal は 6 ユーザーまで [公式] なので、自分を除いて 5 人 | 使える | 無い | Users から外す [未確認] | A1 ができなかったときの控え。grants でゲストを `tag:k8s` の 443 だけに絞る ([tailscale.md](tailscale.md) の ACL の案) |
| **B1: Tailscale Funnel** (Ingress に注釈 `tailscale.com/funnel: "true"`) | ブラウザだけ | 全プランで使える [公式]。帯域に設定できない上限がある (数字は公開していない) [公式] | 使える。Funnel が使える名前は tailnet のドメイン (`*.ts.net`) だけで、今の名前がそのまま外から引ける [公式] | **ある**。URL を知る人は誰でも届く。Funnel には tailnet のアクセス制御が効かない [公式] | 注釈を外す (Git の revert) | **G2 の推奨**。ポートは 443・8443・10000 だけ [公式] |
| B2: Cloudflare の名前付き Tunnel + Access | ブラウザだけ | Tunnel は全プランで無料 [公式]。Access の無料プランは 50 ユーザー [公式]。**Cloudflare に自分のドメインを足す必要がある** [公式]。ドメインは原価で売られ、無料ではない [公式] | 自分のドメインに変わる。issuer を変えると passkey の登録し直しと各 UI の設定の書き換えが要る ([keycloak.md](keycloak.md)) | ある。ただし Access のポリシー (メールアドレスなど) を前に置ける [公式] | Access のポリシーから外す | **外す** (無料に限る条件に合わない) |
| B3: Cloudflare Quick Tunnel (`--allowed-mail` を含む) | ブラウザだけ | 無料、アカウント不要 [公式] | **使えない**。作り直すたびに名前が変わる [公式] | ある (`--allowed-mail` でメールの PIN を前に置ける) | プロセスを止める | **外す** (上の「G3 の控え」) |

ほかに、Funnel を使わずに Tailscale を入れていない端末から tailnet に届ける方法は、公式の文書に見つからなかった [未確認]。
A1 と B1 は同じ Ingress の上で両立する。Funnel を付けた Ingress は、tailnet の端末からも今までどおり開ける [未確認]。

#### A1 で確かめること (タグ付きの端末を共有できるか)

[access-control.md](access-control.md) は「タグ付きのマシンは共有できない [公式]」と書いた。今回 (2026-10-11) 共有の文書を読み直すと、書いてあるのは次の 2 つだけだった。

- 共有の受け手にタグはなれない。共有した端末に、別の tailnet のタグ付きの端末からは届かない。共有を受けられるのはユーザーだけ [公式]
- 共有すると、タグ・グループ・サブネットの情報は受け手の側から外れる [公式]

2 つ目は、タグ付きの端末を共有できることを示唆するが、はっきりとは書いていない。operator の proxy は `tag:k8s` の付いた端末なので、実装の最初に 1 台 (Keycloak の proxy) を共有して確かめる。
共有を受けた側の端末からの接続は受けられるが、共有した端末から受け手の端末へは接続を始められない [公式]。今の UI の使い方 (ブラウザから proxy へ) では問題にならない。

### アカウント

| 候補 | ゲストの作り方 | 期限 | ゲストが要るもの | 費用 | 判断 |
|---|---|---|---|---|---|
| **C: Keycloak のグループ `guests`** (読むだけ) | 管理者が kcadm.sh (下の just レシピ) でユーザーを作り、`guests` に入れ、一時パスワードを付ける。必須アクションはパスワードの変更と TOTP の設定 (今の `yamakura-yuma` と同じ流れ、[keycloak.md](keycloak.md)) | Keycloak のユーザーに期限の項目は無い [公式]。**Workflows** (26.6 で正式、26.8 も既定で有効) で、`on: user-group-membership-added(/guests)` に `disable-user` を `after: 7d` で付ける [公式] | 一時パスワードを受け取る手段 (メール以外)、TOTP の認証アプリか passkey | 無料 | **推奨** |
| D: 外部 IdP の仲介 (Google・GitHub の Identity Brokering) | Keycloak の Identity Provider に Google か GitHub を足す。ゲストはそのボタンでログインする | C と同じ (Workflows) | Google か GitHub のアカウント | 無料 (Google Cloud・GitHub の OAuth の client は無料 [未確認]) | 今は入れない。下の「D の注意」 |
| 参考: Cloudflare Access のメールのワンタイム PIN | Access のポリシーにメールアドレスを書く [公式] | ポリシーから外す | メールを受け取れること | B2 と同じくドメインが要る | 外す (B2 と一緒) |

#### 期限の付け方 (C)

Keycloak のユーザーには、日付で失効させる項目が無い。上流の議論 (GitHub Discussion #10370「Expiration of users, clients, and sessions」) は、
2022 年の時点で期限を持つのはセッションだけと答えたまま、アーカイブに入っている [公式]。代わりの方法は 2 つある。

| 方法 | 書き方 | 長所 | 短所 |
|---|---|---|---|
| **Workflows (推奨)** | realm に YAML の workflow を 1 つ置く。例: `name: guest-expiry`、`on: user-group-membership-added(/guests)`、`steps: [{uses: disable-user, after: 7d}]` [公式] | Keycloak の中で完結し、クラスタに部品が増えない。`after` は `d`・`h` などで書ける [公式]。`delete-user` の step もある [公式] | 期限はグループ単位で 1 つ (人ごとに変えるなら、`guests-1d`・`guests-7d` のようにグループを分けるか、属性の条件を使う [未確認])。realm の import は realm があると効かない ([keycloak.md](keycloak.md)) ので、kcadm.sh か Admin Console でも入れる。`disable-user` が既存のセッションを切るかは [未確認] |
| CronJob + kcadm.sh | ユーザーの属性 `guest-expires-at` (UTC の日時) を発行のときに書き、1 時間ごとの CronJob が期限を過ぎた `guests` のユーザーを `enabled=false` にしてセッションを消す (`users/<id>/logout`) | 人ごとの期限を自由に書ける。セッションも確実に切れる | CronJob と、realm-management の権限を持つサービスアカウントの client が増える。秘密が 1 つ増える |

どちらでも、各 UI のセッションは各 UI の期限まで残りうる (Grafana・ArgoCD は [keycloak.md](keycloak.md) の「人を外したとき」)。
ゲストに見せる Grafana と Backstage のセッションの長さは、実装のときに確かめる [未確認]。

#### D の注意 (外部 IdP の仲介を足すとき)

- Keycloak の既定の first broker login の flow は、一致するアカウントが無いと自動でユーザーを作る [公式]。
  それを止めるには **Create User If Unique** と **Confirm Link Existing Account** を DISABLED にする [公式]。
  止めないと、Google か GitHub のアカウントを持つ誰でも realm `home-k8s` のユーザーになれる (グループが無いので、どの UI でも権限は無い。それでも Account Console には入れる)
- 先に管理者が C と同じくユーザーを作って `guests` に入れ、ゲストが初回に外部のアカウントと結び付ける形にする。結び付けの手順の細部は [未確認]
- ボタンをログイン画面に出したくないときは **Hide on Login Page** を ON にする [公式]
- MFA は外部 IdP の側に任せるか、Keycloak の TOTP も重ねるかを決める必要がある [未確認]

### 公開するときの Keycloak の守り (B1)

B1 では、ゲストのブラウザが Keycloak のログイン画面に届く必要があるので、Keycloak の Ingress にも Funnel を付ける。issuer の URL は変わらない。
代わりに、Keycloak の全部の経路がインターネットに出る。Keycloak の文書の勧めは次のとおり [公式]。

| 経路 | 出すか (Keycloak の勧め) | 理由 |
|---|---|---|
| `/realms/` | 出す | OIDC の endpoint に要る |
| `/resources/` | 出す | ログイン画面の静的ファイルに要る |
| `/.well-known/` | 出す | |
| `/admin/` | **出さない** | 「出すと不要な攻撃面になる」 |
| `/realms/master/` | **出さない** | 管理者の realm |
| `/` | 出さない | 管理の経路が出てしまう |
| `/metrics`・`/health` | 出さない | |

Tailscale の Funnel は Ingress の経路を選べず、Service を丸ごと出す [未確認]。そこで、次の形にする。

- **Keycloak の Ingress の先に、経路の許可リストを持つ caddy を挟む**。`/realms/home-k8s/`・`/resources/`・`/.well-known/` だけを通し、残り (`/admin/`・`/realms/master/` ほか) は 404 にする。
  Backstage の `backstage-tailnet` と同じ作り (caddy の `path` の matcher と、表記揺れを確かめる `just ci` の試験。[backstage.md](backstage.md))
- **Admin Console は tailnet の別の名前に出すか、`kubectl port-forward` で開く**。Keycloak の `hostname-admin` で管理の画面を別の名前に出せる。
  ただし `hostname-admin` を設定しても、フロントの URL から管理の REST API に届くのは止まらない。止めるのは reverse proxy の仕事 [公式]。だから上の caddy が要る
- **ブルートフォースの検知は有効のまま使う** (realm の宣言の `bruteForceProtected: true` [観測]。一時ロックと恒久ロックを選べる [公式])。ロックの回数と時間は既定のまま [観測] なので、公開の前に見直す。パスワードのポリシー (長さ・使い回しの禁止など) は宣言に無い [観測] ので足す [公式]
- **自分での登録とパスワードのリセットは無効のまま** ([keycloak.md](keycloak.md)。今も無効 [観測])
- **TOTP の必須アクションは残す** ([keycloak.md](keycloak.md)。新しいユーザーは最初のログインで OTP を設定させられる [観測])
- **Funnel を付けるのは Keycloak と、ゲストに見せる UI の Ingress だけ**。ArgoCD・Headlamp・Temporal UI・Grafana (dev・prod) の Ingress には付けない。
  見せる UI のログイン画面もインターネットに出るので、ローカルのログインフォーム (Grafana の `admin`) は Funnel の経路で塞ぐか、OIDC だけにする [未確認]
- tailnet のポリシーに `nodeAttrs` で `{"target": ["tag:k8s"], "attr": ["funnel"]}` を足す。`autogroup:member` はタグ付きの端末を含まないので、operator の proxy にはこの形で付ける [公式]。
  これで `tag:k8s` の proxy のどれもが Funnel を使える状態になるので、どの Ingress に注釈を付けたかを `just ci` で数える (share の段 6 と同じ考え)

### OIDC を持たない UI (headroom のダッシュボード)

headroom のダッシュボードは、ホストのプロセス (`127.0.0.1:8787`) で、`/v1/messages` などのプロキシ本体と同じポートにいる [観測]。
share の中継 (`home-k8s-share-relay`。GET の許可リストだけを通すホストの caddy) は `share-retire` で消える。見せる方法は次のとおり。

| 案 | 構成 | 判断 |
|---|---|---|
| **見せない (推奨、最初の形)** | — | headroom は自分の Claude Code の使い方の詳細で、ゲストに見せる必要が薄い [未確認]。見せたい数字が Grafana にあればそちらで見せる |
| oauth2-proxy (クラスタ) + ホストの中継 | tailnet (か Funnel) → Tailscale の Ingress → oauth2-proxy の Pod (`--provider=keycloak-oidc`、`--allowed-group`) → ホストの中継 (share の中継と同じ GET の許可リストとトークン) → headroom | 見せるならこれ。oauth2-proxy は MIT で無料 [公式]。グループで絞るには `groups` の client scope が要る [公式] (realm にもうある [観測])。realm の client `oauth2-proxy` は定義済みで、redirect URI を本番の名前に直す ([keycloak.md](keycloak.md))。中継は share のものを作り直すことになる |
| oauth2-proxy をホストに置き、ホストに Tailscale を入れる | ホストの `tailscale serve` → ホストの oauth2-proxy → headroom | ホストの設定を増やす。WSL2 のホストに Tailscale のクライアントを入れることになり、クラスタの外に部品が散る。採らない |

oauth2-proxy の `--allowed-group` の値は、Keycloak の Group Membership mapper の `full.path` で変わる (true なら `/guests`) [公式]。この realm は `full.path: false` なので `guests` になる見込み [未確認]。

### 各 UI の側の権限 (案)

グループ `guests` を realm に足し、各 UI に「読むだけ」として対応づける。`viewers` に入れない理由は、ゲストを自分の読み取り用の役割と分け、見せる UI を `guests` だけ絞れるようにするため。

| UI | `guests` の扱い (案) | 書くところ | 根拠 |
|---|---|---|---|
| Grafana (観測スタック) | Viewer。ダッシュボードを見てデータソースに問い合わせられる。Explore は使えない [公式] | `role_attribute_path` に `contains(groups[*], 'guests') && 'Viewer'` を足す | [公式] |
| Grafana (dev・prod) | 入れない (今の `role_attribute_strict: true` でログインを断る) | 変えない | [観測] |
| Backstage | 読むだけ (viewers と同じ)。カタログの全部が見える。特定のエンティティだけを見せるには、permission のポリシーで条件付きの判定 (例: `isEntityOwner`) を返す [公式] | `keycloakAuth.ts` の `ROLE_GROUPS` に `guests` を足す。ポリシーは `admins` 以外を読むだけにしているので変えなくてよい [観測] | [公式]・[観測] |
| ArgoCD | **見せない**。`policy.csv` に `guests` を書かず、`policy.default` を空のまま保つ。`policy.default` に何かを書くと、認証した全員に付き、deny でも取り消せない [公式] | 変えない | [公式] |
| Temporal UI | **見せない**。client role を `guests` に付けなければ、ログインできてもどの API も 403 ([temporal.md](temporal.md)) | 変えない | [観測] |
| Headlamp・kubectl | **見せない**。`oidc:guests` の RoleBinding を作らなければ、どの API も 403 ([kube-oidc.md](kube-oidc.md)) | 変えない | [観測] |
| Keycloak の Account Console | ゲスト本人がパスワード・TOTP・passkey を変えるのに使う | — | [観測] |

A1 では、共有するのは Grafana (観測スタック)・Backstage・Keycloak の proxy の 3 台だけにする。B1 では、Funnel を付けるのも同じ 3 つだけ。
「見せない」 UI は、届き方 (共有しない・Funnel を付けない) と権限 (グループを対応づけない) の 2 重で閉じる。

Backstage の中のリンクは localhost を指したまま ([tailscale.md](tailscale.md) の制約) なので、ゲストが Backstage から Grafana へリンクで飛ぶことはできない [観測]。

### 費用と手間

| 項目 | A1 + C | A2 + C | B1 + C |
|---|---|---|---|
| 費用 | 0。Personal は 6 ユーザー・端末無制限・タグ付きのリソース 50 まで・ACL のグループ 3 つまで・**非商用に限る** [公式] | 0。ゲスト 1 人につき 6 ユーザーの枠を 1 つ使う (自分を除いて 5 人まで) [公式] | 0。帯域の上限は公開されていない [公式] |
| ゲストが要るもの | Tailscale のアカウントとアプリ、TOTP の認証アプリ | 同左 | ブラウザと TOTP の認証アプリ |
| 発行の手間 | `just guest add` + 管理画面で端末 3 台を共有 (リンクの発行) | `just guest add` + 管理画面で招待 | `just guest add` だけ |
| 取り消しの手間 | `just guest delete` + 共有の取り消し (しなくても Keycloak で入れない) | `just guest delete` + Users から外す | `just guest delete` だけ |
| 最初の実装 | `guests` グループと各 UI の対応、Workflows、just レシピ | 同左 + grants の絞り込み | 同左 + Keycloak の前の caddy、ブルートフォースとパスワードのポリシー、`nodeAttrs`、Funnel の注釈 |
| 安全性 | 高い。インターネットに何も出ない。ゲストは共有された端末にしか届かない [公式] | 高い。grants で絞れば同等 | 中。ログイン画面がインターネットに出る。守りは Keycloak (TOTP 必須・ブルートフォースの検知・パスワードのポリシー) と経路の絞り込み |

Tailscale の共有の招待リンクは、使い回せるものなら 1,000 回まで使え、使われないまま 30 日で切れる [公式]。

## 発行・取り消しの手順 (just レシピの形)

実装は人が方式を選んでから。形だけを決めておく。`just share` と同じく、パスワードは引数にもコマンドラインにも出さない ([keycloak.md](keycloak.md) の「ユーザーを作る」の `kc` 関数と同じく、標準入力で渡す)。

```sh
just guest add <名前> [--days 7]   # realm home-k8s にユーザー guest-<名前> を作り、guests (と期限のグループ) に入れる。
                                     # 必須アクションは UPDATE_PASSWORD と CONFIGURE_TOTP。一時パスワードは
                                     # ~/.config/home-k8s/guests/<名前> (600) に書き、画面には出さない。
                                     # 最後に「届き方ごとの次の手順」(A1: 共有する端末 3 台の名前、B1: URL) を表示する
just guest list                      # guests の人と、有効か・期限 (Workflows の予定、または属性) を表示する
just guest delete <名前>             # ユーザーを消し、Keycloak のセッションを切る。A1 なら共有の取り消しを促す
just guest extend <名前> --days 7    # 期限を延ばす (Workflows なら、グループを外して入れ直すと予定が作り直される [未確認])
```

- 名前は `guest-` で始め、人のユーザー (`yamakura-yuma`) と取り違えないようにする
- ゲストのユーザーは Git に置かない (人のユーザーと同じ。[keycloak.md](keycloak.md))
- A1 の端末の共有は、Tailscale の管理画面の操作にする。API で共有の招待を作れるかは [未確認]
- 試験は `just ci` に足す。偽の `kubectl` で、パスワードが引数に残らないこと、名前の検査、期限の上限を確かめる (`just/test_share_cli.py` と同じ形)

## 推奨

| ゲスト像 | 届き方 | アカウント | 期限 | 見せる UI | 理由 |
|---|---|---|---|---|---|
| G1 (Tailscale を入れられる) | **A1 端末共有** (だめなら A2 招待) | **C `guests`** | Workflows (`disable-user`、既定 7 日) | Grafana (観測スタック)・Backstage | インターネットに何も出さない。費用 0。6 ユーザーの枠を使わない見込み。今の構成 (Ingress・issuer) をそのまま使う |
| G2 (ブラウザだけ) | **B1 Funnel** (Keycloak・Grafana・Backstage の 3 つだけ) | **C `guests`** | 同上 | 同上 | 無料で、issuer の URL も passkey の登録も変わらない。代わりに Keycloak の守り (経路の絞り込み・ブルートフォースの設定の見直し・TOTP) を先に入れる |
| G3 (1 回だけ見せる) | 仕組みを足さない | — | — | 画面の共有 | アカウント無しで見せる仕組みは share と同じ問題に戻る |

どの像でも外すもの: Cloudflare の名前付き Tunnel と Access (ドメインの費用)、Quick Tunnel (issuer を置けない)、外部 IdP の仲介 (今は手間が見合わない)、headroom の公開 (最初は見せない)。

## 人が決めること

1. ゲスト像 (G1・G2・両方)。G2 を入れるなら、Keycloak のログイン画面をインターネットに出すことを受け入れるか
2. 既定の期限 (案は 7 日) と、人ごとに期限を変えたいか (変えたいなら CronJob の形)
3. headroom を見せるか (見せるなら oauth2-proxy とホストの中継を作り直す)
4. Tailscale Personal は非商用に限る [公式]。ゲストに見せる目的が業務に当たらないか

## 実装の段取り (人が選んだ後の単位)

| # | 話題 | やること | 完了の確かめ方 |
|---|---|---|---|
| g1 | `guests` と各 UI | realm に `guests` を足す (Git と kcadm.sh)。Grafana (観測スタック) の `role_attribute_path`、Backstage の `ROLE_GROUPS` に足す。期限の Workflows を入れる。`just guest add/list/delete` | 使い捨てのテスト用ユーザー (`test-guest`) で、Grafana が Viewer、Backstage が読むだけ、ArgoCD・Temporal・Headlamp・Grafana (dev・prod) で何も見えないこと。期限を短くした workflow で無効になること |
| g2a | A1 (G1 を選んだら) | Keycloak の proxy の端末を 1 台共有して、タグ付きの端末を共有できるか確かめる。できたら Grafana・Backstage も共有し、手順を書く。できなければ A2 の grants を入れる。結果で [access-control.md](access-control.md) の「タグ付きのマシンは共有できない」の記述も直す | 別のアカウントの tailnet の端末から、3 つの URL が開き `test-guest` で入れる |
| g2b | B1 (G2 を選んだら) | Funnel が Ingress の経路を選べないこと (前の caddy が要るか) を最初に確かめる。Keycloak の前の caddy (経路の許可リストと `just ci` の試験)、ブルートフォースの設定の見直しとパスワードのポリシー、`nodeAttrs`、3 つの Ingress の Funnel の注釈 | tailnet の外 (スマホの回線など) から `/realms/home-k8s/` は開き、`/admin/`・`/realms/master/` は 404。ログインに TOTP が要る |
| g3 | headroom (見せると決めたら) | ホストの中継と oauth2-proxy | `guests` 以外は 403、許可リストの外は 404 |

## 出典

開いて内容を確かめたものに (確認)、検索の要約だけで本文を開いていないものに (要約) を付けた。調べた日は 2026-10-11。

- Tailscale
  - 料金 (Personal は 6 ユーザー、端末無制限、タグ付きのリソース 50、ACL のグループ 3、非商用に限る) (確認): <https://tailscale.com/pricing>
  - 端末の共有 (全プラン、受け手はユーザーだけ、タグの情報は外れる、FQDN でだけ引ける、取り消し、招待リンクは 1,000 回・30 日、共有した端末は接続を始められない) (確認): <https://tailscale.com/kb/1084/sharing>
  - Funnel と共有の比較 (招待はユーザーの数が増える、Funnel にはアクセス制御が効かない) (確認): <https://tailscale.com/docs/reference/funnel-vs-sharing>
  - tailnet への招待 (全プラン、招待した人は請求の対象) (確認): <https://tailscale.com/docs/features/sharing/how-to/invite-any-user>
  - Funnel (全プラン、ポート 443・8443・10000、`*.ts.net` だけ、帯域の上限、`funnel` の nodeAttrs) (確認): <https://tailscale.com/kb/1223/funnel>
  - Kubernetes operator の Ingress を Funnel で出す (`tailscale.com/funnel` の注釈、`tag:k8s` への nodeAttrs) (確認): <https://tailscale.com/docs/kubernetes-operator/ingress/expose-workload-to-internet>
- Cloudflare
  - Quick Tunnel (アカウント不要、名前が毎回変わる、同時 200 要求、SSE なし、試験と開発用、`--allowed-mail`) (確認): <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/>
  - 名前付き Tunnel (公開の前に Cloudflare にサイトを足す) (確認): <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/create-remote-tunnel/>
  - Tunnel は全プラン (確認): <https://developers.cloudflare.com/tunnel/>
  - Access の料金 (無料は 50 ユーザー) (確認): <https://www.cloudflare.com/sase/products/access/>
  - Access のワンタイム PIN (確認): <https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/>
  - Access のポリシー (メールアドレスのセレクター) (確認): <https://developers.cloudflare.com/cloudflare-one/access-controls/policies/>
  - Registrar (原価で売る) (確認): <https://www.cloudflare.com/products/registrar/>
- Keycloak
  - ユーザー・クライアント・セッションの期限の議論 (Discussion #10370) (確認): <https://github.com/keycloak/keycloak/discussions/10370>
  - 26.6.0 のリリース (Workflows が正式に) (確認): <https://www.keycloak.org/2026/04/keycloak-2660-released>
  - 機能の一覧 (`workflows:v1` が Supported) (確認): <https://www.keycloak.org/server/features>
  - サーバーの管理ガイド (Managing workflows の `on`・`steps`・`after`・`disable-user`・`delete-user`、Identity Brokering の first broker login・Create User If Unique・Hide on Login Page、パスワードのポリシー、ブルートフォースの検知、Organizations の招待) (確認): <https://www.keycloak.org/docs/latest/server_admin/index.html>
  - reverse proxy の公開する経路の勧め (確認): <https://www.keycloak.org/server/reverseproxy>
  - `hostname-admin` と、それだけでは管理の REST API を止めないこと (確認): <https://www.keycloak.org/server/hostname>
- oauth2-proxy
  - Keycloak OIDC の provider (`--provider=keycloak-oidc`、`--allowed-group`、groups の client scope) (確認): <https://oauth2-proxy.github.io/oauth2-proxy/configuration/providers/keycloak_oidc>
  - 設定 (`--upstream`、`--skip-provider-button`、`--email-domain`) (確認): <https://oauth2-proxy.github.io/oauth2-proxy/configuration/overview>
  - ライセンス (MIT) (確認): <https://github.com/oauth2-proxy/oauth2-proxy>
- Grafana
  - 外部共有のダッシュボード (誰でも・読むだけ・変数は使えない・Enterprise の印は特定の人への共有だけ) (確認): <https://grafana.com/docs/grafana/latest/dashboards/share-dashboards-panels/shared-dashboards/>
  - ロールと権限 (Viewer は Explore を使えない) (確認): <https://grafana.com/docs/grafana/latest/administration/roles-and-permissions/>
- Backstage の permission のポリシー (条件付きの判定、`isEntityOwner`) (確認): <https://backstage.io/docs/permissions/writing-a-policy>
- Argo CD の RBAC (`role:readonly`、`policy.default` は全員に付き deny で消せない) (確認): <https://argo-cd.readthedocs.io/en/stable/operator-manual/rbac/>
- この repo
  - [access-control.md](access-control.md)、[idp-options.md](idp-options.md)
  - [tailscale.md](tailscale.md)、[keycloak.md](keycloak.md)、[kube-oidc.md](kube-oidc.md)
  - [backstage.md](backstage.md)、[temporal.md](temporal.md)、[share.md](share.md)
