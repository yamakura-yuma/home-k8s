# 環境へのアクセス権の一元管理 — 比較と推奨

状態: 調査 (実装はしていない。下の「移行の段取り」の単位ごとに別の話題で行う)。調べた日は 2026-10-10。

> **IdP の推奨は差し替えた (2026-10-10)**: 下の Dex + GitHub に代えて、Keycloak (DB は PostgreSQL) を唯一の issuer にする。
> 比較と移行の段取りは [idp-options.md](idp-options.md)。到達経路・各 UI の OIDC の設定・ワーカーと Azure の権限は、この文書のまま。

人 (自分と他の人) と Orca のワーカー (エージェント) が、次の対象に対して何を見られて何を操作できるかを、1 か所で決めるための比較と推奨。

- Backstage と、その裏にある UI (ArgoCD・Grafana・Temporal UI・Headlamp・Swagger)
- kind クラスタの Kubernetes API (RBAC)
- Azure

選ぶ基準は 3 つ。軽くて運用が楽なこと、無料で済むこと、業務でも通用する構成 (OIDC などの実務の標準) であること。

表の記号は次のとおり。

- **[公式]**: 公式ドキュメントで今回確認した仕様。出典は末尾にある
- **[観測]**: この repo とその docs から読み取った現状。実機の状態は今回確かめていない
- **[未確認]**: 公式の記載を今回確かめられなかったもの、または推測

## 構成

### 現状 [観測]

```text
人 (自分) ── 127.0.0.1 ──┬─▶ Backstage :7007   ゲストログインだけ (guest provider、dangerouslyAllowOutsideDevelopment)
                         │     ├ /api/proxy/grafana*  ──Basic (ユーザー backstage、Viewer)──▶ Grafana ×3
                         │     ├ /api/proxy/argocd    ──Bearer (アカウント backstage、get だけ)──▶ ArgoCD
                         │     └ iframe ─▶ Temporal UI :8233/8234 (認証なし)
                         ├─▶ ArgoCD           admin + 初期パスワード (dex.enabled: false)
                         ├─▶ Grafana          admin / viewer (Basic)、匿名は無効
                         ├─▶ Headlamp :4466   ServiceAccount headlamp のトークン (view + 読み取りの ClusterRole)
                         └─▶ kubectl          kind が作った admin の kubeconfig (~/.kube/config、644)

他の人 ── Cloudflare Quick Tunnel (*.trycloudflare.com、Pod を作り直すと URL が変わる)
          └▶ share Pod: caddy + forward_auth (人ごとの Basic、期限付き)  ─▶ Grafana・Backstage・headroom だけ

Orca のワーカー ── 人と同じ admin の kubeconfig (専用の ServiceAccount・RBAC は無い)
Azure ── Backstage 用のサービスプリンシパル (Reader、サブスクリプション全体)。人とワーカーの割り当ては repo に記載なし
```

アクセス権は UI ごとにばらばらに持っている (admin のパスワード、Basic、SA トークン、share の資格情報)。
人を 1 人足すときも外すときも、触る場所が UI の数だけある。ロールの区別もほぼ無い。share の利用者は全員が同じ範囲を見る。

### 推奨 (下の「推奨構成」の図)

```text
                       上流の IdP: GitHub (organization と team。無料)  ※後から Entra ID も connector で足せる
                              ▲ OAuth
人 ─ Tailscale (tailnet) ─▶ Dex (namespace auth、唯一の OIDC issuer: https://dex.<tailnet>.ts.net)
 │    https://*.<tailnet>.ts.net     │ ID トークン (groups: "<org>:<team>")
 │                                   ├─▶ Backstage   oidc provider + sign-in resolver + permission framework
 │                                   ├─▶ ArgoCD      oidc.config + policy.csv (g, <org>:<team>, role:…)
 │                                   ├─▶ Grafana ×3  generic_oauth + role_attribute_path (groups → Admin/Viewer)
 │                                   ├─▶ Temporal UI TEMPORAL_AUTH_* (UI のログインだけ)
 │                                   ├─▶ Headlamp    config.oidc ─▶ ID トークンを API server へそのまま渡す
 │                                   ├─▶ kubectl     kubelogin (exec plugin)
 │                                   ├─▶ oauth2-proxy  OIDC を持たないもの (headroom など) の前に置く
 │                                   └─▶ kind の API server  AuthenticationConfiguration (jwt[].issuer)
 │                                         issuer.url = 公開の URL / discoveryURL = クラスタ内の Dex
 │                                         groups → RBAC の Group (prefix oidc:)
 └─ Azure: Entra ID のユーザー (他の人は B2B ゲスト) ─▶ セキュリティグループ ─▶ Azure RBAC (リソースグループに割り当てる)

Orca のワーカー
  ├─ kind: ServiceAccount orca-worker + RoleBinding (dev は edit・prod は view)
  │         kubectl create token --duration で短命のトークンを作り、worktree ごとの kubeconfig にする
  └─ Azure: ワーカー専用のサービスプリンシパル (Reader、リソースグループに割り当てる。Git の外に置く)
```

## 決め手になる制約

候補を比べる前に、どの IdP を選んでも満たす必要がある条件を書く。

1. **issuer を 1 つにする**。Backstage・ArgoCD・Grafana・Temporal UI・Headlamp・kind の API server が、同じ issuer の ID トークンを受け取る。グループは IdP 側で 1 回決め、各 UI はそのグループを自分のロールに対応づけるだけにする
2. **groups クレームをそのまま RBAC に書けること**。Kubernetes の RoleBinding、ArgoCD の policy.csv、Grafana の role_attribute_path は、どれもクレームの文字列で照合する。Entra ID の生のクレームはグループの objectId (GUID) になり、書けるが読めない [公式]
3. **issuer の URL が固定で、他の人のブラウザとクラスタの両方から届くこと**。OIDC はトークンの `iss` と設定の issuer URL の一致を検証する。いまの公開経路 (Cloudflare Quick Tunnel) は share Pod を作り直すと URL が変わる [観測] ので、issuer を置けない。
   **到達経路の決定が IdP より先に要る**
4. **kind の API server からクラスタ内の IdP に届くこと**。API server は kind のノードのコンテナの中で動く。Structured Authentication Configuration (v1.34 で GA。kind が既定で立てる 1.35 で使える) では次の 2 つを別々に書ける [公式]。
   - `issuer.url`: トークンの `iss` と照合する URL
   - `issuer.discoveryURL`: discovery の取得先

   公開の URL のままで、取得先だけクラスタ内の Dex にできる。`audiences` も複数並べられる (`audienceMatchPolicy: MatchAny`) [公式] ので、kubectl 用と Headlamp 用の client ID を両方受け付けられる
5. **有料機能を前提にしない**。Entra ID の P1/P2 (グループのアプリへの割り当て、条件付きアクセス、PIM)、Grafana の Team Sync (Enterprise) がこれにあたる

## 比較

### IdP

| 候補 | OIDC | グループ (RBAC に書く値) | 無料か | 重さ・運用 | 業務での通用 |
|---|---|---|---|---|---|
| **Entra ID Free を直接** | 可 (アプリの登録) | `groupMembershipClaims: SecurityGroup` で groups クレームを出せる。値は objectId、上限 200 [公式] | 無料。ただし**グループのアプリへの割り当ては P1** [公式]。条件付きアクセスは P1 [公式] | クラスタに何も置かない。他の人は B2B ゲストとして招待する (50,000 MAU まで無料) [公式] | 高い (Azure と ID が同じ) |
| **GitHub を直接** | **不可**。OAuth App は OAuth2 だけで、ID トークンを出さない [未確認] | team を引くには API を呼ぶ必要がある | 無料 | クラスタに何も置かない | 中 |
| **Dex + GitHub connector** | 可 (Dex が issuer) | `"<org>:<team>"`。team の名前または slug を選べる [公式] | 無料 (OSS) | Dex 1 つ。ユーザーを保存しない仲介役で、ユーザーとグループの正本は GitHub [公式]。メモリは数十 MB [未確認] | 高い (OIDC の標準。上流は差し替えられる) |
| Dex + Microsoft connector | 可 | グループ名を引ける。ただし tenant の指定と、`Directory.Read.All` への管理者の同意が要る [公式] | 無料 | 同上 | 高い |
| Keycloak | 可 | 自前のグループ | 無料 (OSS) | JVM のため 512MB〜1GB 以上 [未確認]。DB とバージョンアップの運用が要る | 高い |
| Authentik | 可 | 自前のグループ | OSS 版は無料、Enterprise は有料 [未確認] | server・worker・PostgreSQL [未確認] | 中〜高 |
| Pocket ID / Kanidm | 可 | 自前のグループ | 無料 (OSS) | 軽い [未確認] | 低〜中 |

### 到達経路 (他の人がどう入るか)

| 候補 | 費用 | 人数の上限 | 固定の URL (issuer を置けるか) | 備考 |
|---|---|---|---|---|
| 127.0.0.1 だけ (いま) | 無料 | 自分だけ | 置けない (他の人は届かない) | |
| Cloudflare Quick Tunnel + share (いま) | 無料・アカウント不要 | 制限なし | **置けない** (Pod を作り直すと URL が変わる) [観測] | 見せるだけの用途には向く |
| **Tailscale Personal** | 無料 | **6 ユーザー**。端末は無制限。タグ付きのリソースは 50 まで [公式] | 置ける (`*.<tailnet>.ts.net`。Kubernetes operator の Ingress で HTTPS) | **非商用に限る** [公式]。ACL グループは 3 つまで [公式]。ノードの共有は相手のユーザー数に数えないが、タグ付きのマシンは共有できない [公式]。operator の proxy はタグ付きなので、他の人は tailnet に招待する形になる [未確認] |
| Cloudflare 名前付き Tunnel + Access | Zero Trust の無料プラン。**Cloudflare 上の自分のドメインが要る** (Tunnel のアプリの公開にも Access にも) [公式]。ドメインの登録料がかかる | 無料プランの人数の上限 (50 と言われる) は**未確認** | 置ける (自分のドメイン) | Access の IdP に Entra ID・汎用 OIDC・GitHub を使える [公式]。インターネットに公開する形になる |

### 各 UI と OIDC

| 対象 | OIDC への対応 | グループからロールへ | 無料か | 備考 |
|---|---|---|---|---|
| Backstage | `@backstage/plugin-auth-backend-module-oidc-provider` (`auth.providers.oidc`、`metadataUrl`) [公式] | sign-in resolver で User エンティティに対応づけ、権限は permission framework のポリシーで決める。**permission framework は既定で無効で、何もしなければ誰でも何でもできる** [公式] | 無料 (OSS) | resolver はカタログの User を引く。`dangerouslyAllowSignInWithoutUserInCatalog` は本番では非推奨 [公式]。ゲストは本番で出さない想定 [公式] |
| ArgoCD | `argocd-cm` の `oidc.config` で外の issuer を直接使う。同梱の Dex を使う方法もある [公式] | `argocd-rbac-cm` の policy.csv に `g, <group>, role:<name>` と書く [公式] | 無料 (OSS) | グループはログインのときにだけ更新される [公式] |
| Grafana (OSS) ×3 | Generic OAuth [公式] | `role_attribute_path` (JMESPath) で Admin/Editor/Viewer を決め、`allowed_groups` でログインを絞る [公式] | 無料。**Team Sync だけ Enterprise (有料)** [公式] | 3 つの Grafana は client を分けるか、redirect URI を並べる |
| Temporal Web UI | `TEMPORAL_AUTH_ENABLED`・`_PROVIDER_URL`・`_CLIENT_ID`・`_CLIENT_SECRET`・`_CALLBACK_URL`・`_SCOPES` [公式] | **UI へのログインだけを守る。Temporal Service の認可とは別物** [公式]。サーバーの認可の既定の claim mapper は `permissions` クレーム (`<ns>:read` など) を読む [公式] | 無料 (OSS) | サーバー (gRPC) はクラスタの中にだけ置けば、人は UI を通るしかない |
| Headlamp | `-oidc-client-id`・`-oidc-idp-issuer-url` など (Helm では `config.oidc`) [公式] | ID トークンを API server にそのまま渡すので、権限は Kubernetes の RBAC で決まる [公式] | 無料 (OSS) | API server 側の OIDC 設定と、issuer・audience が合っている必要がある |
| Swagger (sample-api) | Backstage の中のタブ。Backstage の proxy を通る [観測] | Backstage のログインと permission framework に従う | — | 別に守る必要はない |
| headroom などの素の UI | 無い | oauth2-proxy を forward-auth として前に置く。provider に OIDC・GitHub・Entra ID がある [未確認] | 無料 (MIT) [未確認] | |

### Kubernetes と Azure

| 対象 | 人 | ワーカー | 無料か |
|---|---|---|---|
| kind の API server | `--authentication-config` に `jwt[]` (issuer は最大 64、`claimMappings.groups` の prefix は必須で、要らなければ `""`) [公式]。設定ファイルは書き換えると読み直される [公式]。kind では `kubeadmConfigPatches` と `extraMounts` で渡す [未確認]。kubectl は kubelogin を exec plugin にする [未確認] | ServiceAccount + RoleBinding。`kubectl create token <sa> --duration=…` で期限付きのトークンを作る [未確認] | 無料 |
| RBAC | `subjects: - kind: Group, name: oidc:<org>:<team>` | `kind: ServiceAccount` | 無料 |
| Azure | Entra ID のユーザー・ゲストをセキュリティグループに入れ、Azure RBAC のロール (Reader・Contributor) をリソースグループに割り当てる。ロールはユーザー・グループ・サービスプリンシパル・マネージド ID に割り当てられる [公式]。ライセンスが要るとは書かれていない (P2 が要るのは eligible・期限付きの割り当て = PIM [公式]) | ワーカー専用のサービスプリンシパル。スコープはリソースグループまで絞る | 無料 (PIM は P2) |
| Azure の workload identity federation | — | クラスタの ServiceAccount の issuer を、インターネットから届く https で公開する必要がある [公式]。**ワーカーはホストで動くプロセス (Pod ではない) なので当てはまらない** | 無料 |

## 推奨構成

**GitHub を上流にした Dex を唯一の OIDC issuer とし、Tailscale の tailnet の中で全部の UI と kind の API server をそれにつなぐ。**
Azure は Entra ID のグループに Azure RBAC を割り当てる。ワーカーには人と別の、短命で範囲を絞った資格情報を配る。

| 層 | 選ぶもの | 選んだ理由 |
|---|---|---|
| IdP | **Dex** (namespace `auth`) + GitHub connector | 6 つの消費者すべてが標準の OIDC で読める。groups が `<org>:<team>` という読める文字列で出て、そのまま RBAC に書ける。ユーザーを保存しないので、DB とバックアップが要らない。上流は後から Microsoft connector (Entra ID) に足し替えられ、各 UI の設定は変わらない |
| ユーザーとグループの正本 | **GitHub** (アクセス管理専用の org と team。2026-10-10 に決定) | 他の人も GitHub のアカウントなら持っている見込みが高い。個人のアカウントを専用の org (リポジトリなし、base permission はなし) に招待し、外すのも org の操作 1 つで済む (下の「IdP は GitHub に決めた」) |
| 到達経路 | **Tailscale Personal** + Kubernetes operator の Ingress | 無料で URL が固定になり (`*.<tailnet>.ts.net`、HTTPS)、issuer を置ける。インターネットに出さない。6 人と非商用という条件は、自宅のクラスタの規模なら収まる |
| Backstage | oidc provider + GitHub org のユーザーとチームをカタログに取り込む + permission framework | resolver が引く User と Group をカタログに揃える。ポリシーは「org のメンバーは読める、管理の team だけが書ける」から始める |
| ArgoCD・Grafana・Temporal UI・Headlamp | 各自の OIDC 設定で Dex を見る | どれも無料版の機能で足りる (Grafana の Team Sync は使わない) |
| Kubernetes API | AuthenticationConfiguration (公開の issuer.url とクラスタ内の discoveryURL) + kubelogin | 人の kubectl と Headlamp が同じ Group の RBAC に従う。kind の admin の kubeconfig は非常用として残す |
| Azure (人) | Entra ID のセキュリティグループに Azure RBAC を割り当てる | 無料。Azure の ID は Entra ID から外せないので、Dex に寄せず Azure の側で持つ |
| ワーカー (kind) | ServiceAccount `orca-worker`、dev は `edit`・prod は `view`。トークンは `kubectl create token --duration=8h` [未確認] | admin の kubeconfig を使わせない。worktree ごとの kubeconfig (Git の外) を、worktree の setup が作る |
| ワーカー (Azure) | 専用のサービスプリンシパル (Reader、リソースグループに割り当てる) | workload identity federation はホストのプロセスには当てはまらない。スコープを絞り、シークレットに期限を付ける |
| `just share` | 当面は残す (アカウントを持たない人に見せるだけの用途) | Tailscale に招待できない相手向けの経路になる。招待で足りるようになったら廃止を決める |

### IdP は GitHub に決めた (2026-10-10)

Dex の上流の IdP は **GitHub** に決めた (アクセス管理専用の org と team)。理由は、いちばんわかりやすいこと。

- **招待先はアクセス管理専用の org**: リポジトリを置かず、メンバーの base permission を「No permission」(なし) にする。
  理由は、相手の個人アカウントを招待しても org のリポジトリが見えず、org を team の名簿としてだけ使えるため。
  - base permission の既定は Read で、none に下げられる [公式]
  - internal のリポジトリは、base permission が none でも read になる [公式]。専用の org にリポジトリを置かないのはこのためでもある
- **他の人の入れ方**: 相手の**個人の** GitHub アカウントを専用の org に招待し、team で権限を分ける。会社のアカウントは招待しない。
  - 所属を公開するかは本人が決められ、Private にできる [公式]。既定が非公開かどうかは、公式に記載が無い
- **Dex の `orgs` にはその専用の org だけを書く**:
  - `orgs` を書くと、そこに並べた org (team を書けばその team) のどれかに属していないユーザーはログインできない [公式]
  - team を書くと、groups クレームに入るのはその team だけになる [公式]
- **相手の会社の org を許可リストに足さない**: Dex の GitHub connector の注意書きには次のようにある [公式]。
  - ユーザーは、Dex がリソースにアクセスすることを org に明示的に求める必要がある
  - org が承認するまで、Dex は所属を確かめられず、そのユーザーはログインできない

  GitHub の OAuth App access restrictions がこれにあたる。相手の会社の org がこれを有効にしていると、その org の管理者が承認するまで、相手はログインできない
- **Enterprise Managed Users (EMU) のアカウントは使えない**: EMU のアカウントは、enterprise の外の org にもリポジトリにも招待できない [公式]。
  専用の org に招待できないので、相手が会社の EMU のアカウントしか持っていない場合は、個人のアカウントを用意してもらう

採らなかった理由は次のとおり。

- **Entra ID を直接 issuer にする案**: 無料ではグループをアプリに割り当てられない (P1)。生の groups クレームは objectId なので、RBAC が GUID の羅列になる。業務に寄せたくなったら、Dex に Microsoft connector を足せば各 UI は変えずに済む
- **Keycloak・Authentik**: 自分でユーザーを持てる点は強いが、DB・バージョンアップ・メモリの負担が軽さの基準に合わない。
  この判断は [idp-options.md](idp-options.md) で見直し、Keycloak を推奨に差し替えた
- **Cloudflare Access**: 業務らしい構成ではあるが、ドメインが要る (登録料がかかる)。無料プランの人数の上限も今回確かめられなかった。ドメインを持つ段になったら、到達経路だけを差し替える候補にする

### 推奨構成の注意点 (実装の話題で解く)

- **クラスタの中から issuer に届くか** [未確認]: Backstage・ArgoCD・Grafana のバックエンドは、トークンを検証するために issuer の discovery と JWKS を取りに行く。Pod から `dex.<tailnet>.ts.net` に届かせる方法は 2 つある。どちらにするかは単位 2 で決める
  - CoreDNS の rewrite でクラスタ内の Dex に向け、Dex に同じ名前の証明書を持たせる
  - operator の egress を使う

  API server だけは `discoveryURL` で避けられる [公式]
- **kind の作り直しが要る**: API server の起動引数を変えるので、単位 5 は `just down`・`just up` を伴う
- **Temporal のサーバーの認可**: UI の OIDC はサーバーを守らない [公式]。サーバーをクラスタの外に出さない前提を保つ
- **非商用の条件**: Tailscale Personal は非商用に限る [公式]。業務で使うなら Cloudflare Access (ドメインが要る) か、Tailscale の有料プランに差し替える

## 移行の段取り (実装の話題に分ける単位)

上から順に進める。それぞれ単独でマージできる。単位 7 と 8 は人の認証と関係しないので、先に進めてもよい。

| # | 話題 | やること | 完了の確かめ方 |
|---|---|---|---|
| 1 | 到達経路 (Tailscale) | Tailscale の Kubernetes operator を入れ、UI と後で置く Dex を `*.<tailnet>.ts.net` に出す。認証はまだ変えない | tailnet の別の端末から固定の URL で開ける |
| 2 | Dex | namespace `auth` に Dex を置き、GitHub connector (org と team) を設定する。クラスタ内から issuer に届かせる方法を決める (上の注意点) | `/.well-known/openid-configuration` がブラウザからも Pod からも引ける |
| 3 | ArgoCD・Grafana の OIDC | `oidc.config` と policy.csv、3 つの Grafana の generic_oauth と role_attribute_path。admin のパスワードは非常用として残す | 管理の team の人は Admin、それ以外は Viewer になる |
| 4 | Backstage の OIDC と permission framework | oidc provider、GitHub org のユーザーとチームの取り込み、ポリシーを入れる。ゲストは 127.0.0.1 の開発用に限る | org の外の人は入れない。管理の team 以外は書き込みが拒否される |
| 5 | kind の API server と Headlamp | AuthenticationConfiguration (kind-config)、kubelogin、Headlamp の `config.oidc`、Group の RBAC | `kubectl auth whoami` が `oidc:` の Group を返す。Headlamp の権限が RBAC どおりになる |
| 6 | Temporal UI・oauth2-proxy・share の扱い | `TEMPORAL_AUTH_*` を設定し、headroom の前に oauth2-proxy を置く。`just share` を残すか廃止するかを決める | どの UI も Dex のログインを経ないと開けない |
| 7 | ワーカーの kind の権限 | ServiceAccount `orca-worker` と RoleBinding を作り、worktree の setup が短命の kubeconfig を書くようにする。admin の kubeconfig の読み取りを止める (Claude の deny 設定など) | ワーカーから prod に書き込めない。トークンが期限で切れる |
| 8 | Azure の権限 | 人: Entra ID のセキュリティグループ (他の人は B2B ゲスト) を作り、リソースグループにロールを割り当てる。ワーカー: 専用のサービスプリンシパルを作る。Backstage の SP のスコープも、サブスクリプションからリソースグループに絞るか検討する | `az role assignment list` が想定どおりになる |

## 出典

開いて内容を確かめたものに (確認)、URL だけを挙げたもの (既存の知識による) に (未確認) を付けた。

- Backstage
  - 認証の概要とプロバイダーの一覧、ゲストの扱い (確認): <https://backstage.io/docs/auth/>
  - OIDC provider (確認): <https://backstage.io/docs/auth/oidc>
  - sign-in resolver (確認): <https://backstage.io/docs/auth/identity-resolver>
  - permission framework (確認): <https://backstage.io/docs/permissions/overview>
- Argo CD
  - SSO (確認): <https://argo-cd.readthedocs.io/en/stable/operator-manual/user-management/>
  - RBAC (未確認): <https://argo-cd.readthedocs.io/en/stable/operator-manual/rbac/>
- Grafana
  - Generic OAuth (確認): <https://grafana.com/docs/grafana/latest/setup-grafana/configure-access/configure-authentication/generic-oauth/>
  - Team Sync (確認): <https://grafana.com/docs/grafana/latest/setup-grafana/configure-access/configure-team-sync/>
- Temporal
  - Web UI の環境変数 (確認): <https://docs.temporal.io/references/web-ui-environment-variables>
  - サーバーの認可 (確認): <https://docs.temporal.io/self-hosted-guide/security>
- Headlamp の OIDC (確認): <https://headlamp.dev/docs/latest/installation/in-cluster/oidc/>
- oauth2-proxy
  - providers (未確認): <https://oauth2-proxy.github.io/oauth2-proxy/configuration/providers/>
  - forward-auth (未確認): <https://oauth2-proxy.github.io/oauth2-proxy/configuration/integration>
- Dex
  - GitHub connector (`orgs` の許可リスト、org の承認が要る点) (確認): <https://dexidp.io/docs/connectors/github/>
  - Microsoft connector (確認): <https://dexidp.io/docs/connectors/microsoft/>
  - ローカルのユーザー (未確認): <https://dexidp.io/docs/connectors/local/>
- GitHub の org の base permission (既定は Read、none に下げられる、internal は read) (確認): <https://docs.github.com/en/organizations/managing-user-access-to-your-organizations-repositories/managing-repository-roles/setting-base-permissions-for-an-organization>
- GitHub の org の所属の公開・非公開 (確認): <https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-your-membership-in-organizations/publicizing-or-hiding-organization-membership>
- GitHub の Enterprise Managed Users の制限 (確認): <https://docs.github.com/en/enterprise-cloud@latest/admin/managing-iam/understanding-iam-for-enterprises/abilities-and-restrictions-of-managed-user-accounts>
- Keycloak (未確認): <https://www.keycloak.org/documentation>
- Authentik (未確認): <https://docs.goauthentik.io/docs/install-config/install/kubernetes>
- Kubernetes
  - 認証、Structured Authentication Configuration (確認): <https://kubernetes.io/docs/reference/access-authn-authz/authentication/>
  - RBAC (未確認): <https://kubernetes.io/docs/reference/access-authn-authz/rbac/>
  - `kubectl create token` (未確認): <https://kubernetes.io/docs/reference/kubectl/generated/kubectl_create/kubectl_create_token/>
  - kind の kubeadmConfigPatches (未確認): <https://kind.sigs.k8s.io/docs/user/configuration/#kubeadm-config-patches>
  - kubelogin (未確認): <https://github.com/int128/kubelogin>
- Microsoft Entra ID と Azure
  - optional claims、groups クレーム (確認): <https://learn.microsoft.com/en-us/entra/identity-platform/optional-claims>
  - アプリへのユーザーとグループの割り当て (グループは P1) (確認): <https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/assign-user-or-group-access-portal>
  - ライセンス (条件付きアクセスは P1、PIM は P2) (確認): <https://learn.microsoft.com/en-us/entra/fundamentals/licensing>
  - External ID (B2B) の料金 (確認): <https://learn.microsoft.com/en-us/entra/external-id/external-identities-pricing>
  - Azure RBAC のロールの割り当て (確認): <https://learn.microsoft.com/en-us/azure/role-based-access-control/role-assignments-portal>
  - 組み込みロール (未確認): <https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles>
  - AKS 以外の workload identity (確認): <https://azure.github.io/azure-workload-identity/docs/installation/self-managed-clusters.html>
- Tailscale
  - 料金 (Personal は 6 ユーザー、非商用) (確認): <https://tailscale.com/pricing>
  - 共有 (確認): <https://tailscale.com/docs/features/sharing>
  - Kubernetes operator (確認): <https://tailscale.com/kb/1236/kubernetes-operator>
- Cloudflare
  - Tunnel (ドメインが要る) (確認): <https://developers.cloudflare.com/tunnel/get-started/>
  - Access の IdP (確認): <https://developers.cloudflare.com/cloudflare-one/identity/idp-integration/>
  - Zero Trust のプラン (無料の人数の上限は読めなかった) (未確認): <https://www.cloudflare.com/plans/zero-trust-services/>
- この repo
  - [backstage.md](backstage.md)
  - [environments.md](environments.md)
  - [share.md](share.md)
  - [backstage-argocd.md](backstage-argocd.md)
  - [backstage-azure.md](backstage-azure.md)
  - [temporal.md](temporal.md)
