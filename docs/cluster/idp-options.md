# 自前でホストする IdP (Keycloak など) — 比較と推奨

状態: 調査は 2026-10-10。実装は下の「移行の段取り」の単位ごとに進めている (単位 1・2a・2b は実装済み。Keycloak と PostgreSQL は [keycloak.md](keycloak.md))。

[access-control.md](access-control.md) は、Dex + GitHub を唯一の OIDC issuer にする構成を推奨した。
そのとき Keycloak・Authentik は「重い」という理由で外したが、重さは確かめていなかった ([未確認])。
この文書はその判断を見直し、自前でホストする OSS の IdP を Dex + GitHub と並べて比べ、推奨を 1 つ出す。

見直す理由は 3 つある (2026-10-10 のユーザーの回答)。

- 他の人に GitHub のアカウントと org への招待を求めたくない
- 業務で使われる構成を学びたい
- Dex の機能では足りない (ユーザーを自分で持てない、管理画面が無い)

重さは学習の価値を優先して受け入れる (JVM や PostgreSQL も可)。ただし、要るメモリと運用の手間は数字で示す。
到達経路 (Tailscale)、各 UI の OIDC の設定、ワーカーと Azure の権限は [access-control.md](access-control.md) のまま変えない。
変わるのは issuer の中身だけになる。

表の記号は次のとおり。

- **[公式]**: 公式ドキュメント、公式のリポジトリのソース・Helm chart・リリースノートで今回確認した仕様。出典は末尾にある
- **[観測]**: この repo とホストから読み取った現状
- **[未確認]**: 一次資料で確かめられなかったもの、または推測

## 結論

- **IdP は Keycloak にする** (公式の Keycloak Operator で入れる)。管理画面、TOTP と WebAuthn、GitHub・Google・Microsoft を並べたログイン、セッションの管理、イベントの監査が、どれも無料の OSS の機能に入っている。業務での採用も候補の中でいちばん多い。
  メモリは Keycloak の Pod 1 つで 1.25〜2 GB を見込む
- **DB は PostgreSQL の素の StatefulSet** にする (公式の `postgres` イメージ、1 つ)。データは [persistence.md](persistence.md) の静的な `local` PV に置き、`pg_dump` の CronJob でホストのディレクトリに毎日書き出す。
  無料で続けられ、`just down`・`just up` でもデータが残る
- **軽さ重視の次点は Zitadel**。Go の 1 プロセスで約 512 MB と軽く、管理画面・MFA・外部 IdP 連携もそろっている。
  ただし groups クレームを標準で出さないので、各 UI で照合できる形に直す手間が要る (下の比較)
- **Dex は外す**。Keycloak を唯一の issuer にし、Dex を前段にも後段にも置かない。
  [access-control.md](access-control.md) の推奨は、この文書の推奨に差し替える

## 構成

### 推奨構成 (Keycloak を唯一の issuer にする)

```text
                  上流の IdP (任意。ログイン画面にボタンとして並ぶ): GitHub / Google / Microsoft (Entra ID)
                         ▲ OIDC / OAuth (Identity Brokering)
人 ─ Tailscale ─▶ Keycloak (namespace auth、realm home-k8s、唯一の OIDC issuer:
 │  (tailnet)       https://keycloak.<tailnet>.ts.net/realms/home-k8s)
 │                 │  ユーザー・グループ・MFA (TOTP / WebAuthn)・セッション・イベントをここで持つ
 │                 │  ID トークン (groups: ["admins", "viewers"]。Group Membership mapper、full.path=false)
 │                 ├─▶ Backstage   oidc provider + sign-in resolver + permission framework
 │                 ├─▶ ArgoCD      oidc.config + policy.csv
 │                 ├─▶ Grafana ×3  generic_oauth + role_attribute_path
 │                 ├─▶ Temporal UI TEMPORAL_AUTH_*
 │                 ├─▶ Headlamp    config.oidc ─▶ API server
 │                 ├─▶ kubectl     kubelogin
 │                 ├─▶ oauth2-proxy  OIDC を持たない UI の前に置く
 │                 └─▶ kind の API server  AuthenticationConfiguration (issuer.url = 公開の URL /
 │                                          discoveryURL = クラスタ内の Keycloak)
 │
 └─ Azure は Entra ID のまま (access-control.md の単位 8)

namespace auth
  Keycloak Operator ─▶ Keycloak (Pod 1 つ、requests 1700Mi / limits 2Gi が Operator の既定)
                         │ JDBC
                         ▼
                       PostgreSQL (StatefulSet 1 つ) ─▶ PVC ─claimRef─ PV keycloak-postgres (home-k8s-local, Retain)
                         │                                              │ local.path
                         │                                              ▼
                         │                         ノード /var/local/home-k8s/... ◀─extraMounts─ ホスト ~/.local/share/home-k8s/...
                         └─ CronJob pg-dump (毎日) ──▶ 同じホストの別のディレクトリに pg_dump の .sql.gz
```

各 UI が見る値は、[access-control.md](access-control.md) の「各 UI と OIDC」と同じ設定項目になる。
変わるのは次の 2 つだけ。

- issuer の URL: `https://dex.<tailnet>.ts.net` から `https://keycloak.<tailnet>.ts.net/realms/home-k8s` になる
- groups の値: `<org>:<team>` から Keycloak のグループ名になる

### 他の人の入れ方 (GitHub が要らなくなる)

Keycloak は自分でユーザーを持つので、他の人に GitHub のアカウントを求めなくてよい。流れは次のとおり。

1. 管理者が Admin Console でユーザーを作り、グループに入れる。一時パスワードを付ける
2. 必須アクション (Required actions) に「パスワードの変更」と「OTP の設定」(または WebAuthn の登録) を付ける。
   Keycloak は必須アクションを、ログインの途中で利用者に必ず行わせる [公式]
3. 一時パスワードを、メール以外の手段で相手に渡す。相手は最初のログインでパスワードを変え、TOTP か passkey を登録する

この流れはメールを送らないので、SMTP は要らない [未確認]。

- パスワードのリセットのメールや、Organizations の招待のメールを使うには、SMTP の設定が要る [公式]
- クラスタに SMTP は無い [観測]。メールの機能は使わない前提にする

GitHub・Google・Microsoft のアカウントを持つ人には、Identity Brokering でそのボタンをログイン画面に並べられる [公式]。
外部のアカウントで入った人も、Keycloak のユーザーとして同じグループに入れる。
外すときは、Keycloak でユーザーを無効にし、セッションを切る (Admin Console の操作 1 回)。

## 比較

### IdP の機能

| 候補 | ユーザー・グループの管理画面 | MFA (TOTP・WebAuthn) | 外部 IdP をログイン手段に並べる | セッション管理・監査ログ | groups クレーム (RBAC に書く値) | 無料か |
|---|---|---|---|---|---|---|
| **Keycloak** | Admin Console でユーザー・グループ・ロール・クライアントを管理する [公式] | TOTP/HOTP・passkey・リカバリーコード [公式] | Social Login (GitHub・Google ほか) と OIDC・SAML の Identity Brokering [公式]。Microsoft も一覧にある [公式] | 管理者も本人もセッションを見て切れる。Events (ログインのイベントと管理のイベント) を監査に使える [公式] | Group Membership mapper を client scope に足す。`full.path` の既定は true で `/top/level1` の形になる。false で名前だけになる [公式] | 無料 (Apache-2.0) [公式] |
| Authentik | Admin Interface の Directory > Users [公式] | TOTP・WebAuthn/passkey・静的コード・Duo・SMS・メール。Enterprise の印は無い [公式] | GitHub などの Sources をログインのフローに足す [公式]。Entra ID の OAuth の Source もある [公式]。ただし料金表は「Microsoft Entra ID integration」を Enterprise にしている。ログインの Source がそれに含まれるかは [未確認] | 管理者がセッションを消せる [公式]。Events を全部記録し、既定で 365 日残す。「Enhanced audit logging」は Enterprise [公式] | `profile` scope の既定の mapping でグループ名のリストを出す [公式] | OSS 版は無料。Enterprise は $5/ユーザー/月 [公式] |
| Zitadel | Console [公式] | TOTP・U2F・passkey、MFA の強制 [公式] | Google・Entra ID・GitHub・GitLab・Apple などのガイドがある [公式] | [未確認] | **groups クレームは無い**。ロールを `urn:zitadel:iam:org:project:roles` のオブジェクト (map) で出す。平たいリストにするには Actions で claim を足す [公式] | 無料 (AGPL-3.0、v3 で Apache-2.0 から変わった) [公式] |
| Kanidm | 管理は主に CLI。Web UI は利用者のセルフサービス向け [公式] | passkey・TOTP [公式] | OIDC の上流は PR で入ったが、公式の対応と文書は無い [公式] | `kanidm session list` がある。監査ログは [未確認] | `groups` (uuid と spn)、`groups_name` (名前だけ) [公式] | 無料 (MPL-2.0) [公式] |
| Authelia | 無い (ユーザーは YAML のファイルか LDAP)。管理者の画面はロードマップの Active [公式] | TOTP・WebAuthn・passkey・Duo [公式] | 無い。OIDC の Relying Party はロードマップの Planning [公式] | [未確認] | `groups` は既定で UserInfo に出る。ID トークンに入れるのは「break-glass」扱い [公式]。**API server は ID トークンしか見ない** | 無料 (Apache-2.0) [公式] |
| Ory (Kratos + Hydra) | 自前でホストする版には公式の管理画面が無い。API だけ (headless) [未確認] | Kratos が持つ [未確認] | Kratos の social sign-in [未確認] | [未確認] | Kratos は OIDC を出さないので、Hydra と login/consent のアプリを自分でつなぐ [未確認] | 無料 (Apache-2.0) [公式] |
| Pocket ID | 管理画面でユーザーと許可するグループを持つ [公式] | **passkey だけ**。パスワードは無い [公式] | [未確認] (公式の説明に記載が無い) | [未確認] | トークンに名前・メール・グループを入れる [公式] | 無料 (BSD-2-Clause) [公式] |
| Dex + GitHub (前回の推奨) | 無い。ローカルのユーザーは設定ファイルか gRPC API で足す [公式]。正本は GitHub の org と team | **v2.46.0 (2026-10-07) で TOTP と WebAuthn が入った** [公式]。設定の文書は [未確認] | connector で GitHub・Microsoft・汎用 OIDC などを並べられる [公式] | v2.46.0 で認証のセッションが入った [公式]。監査ログは [未確認] | `<org>:<team>` [公式] | 無料 (Apache-2.0) [公式] |

### 重さ・運用・普及度

メモリは、公式の要件か、公式の Helm chart・Operator の既定値から引いた。chart の多くは `resources: {}` (既定なし) で、例をコメントで示すだけなので、その例の値を「目安」として書く。

| 候補 | 構成要素 | メモリ | DB | バージョンアップ・バックアップ | 業務での普及度 (GitHub の star、2026-10-10) |
|---|---|---|---|---|---|
| **Keycloak** | Keycloak + DB (+ Operator) | 公式のサイズの目安は、realm のキャッシュと 10,000 セッション込みで Pod 1 つ 1250 MB。コンテナではメモリの上限の 70% をヒープに使う [公式]。Operator の既定は requests 1700Mi・limits 2Gi [公式]。codecentric の keycloakx chart は既定なし (例は 1024Mi) [公式] | PostgreSQL 14〜18、MariaDB、MySQL など。`dev-file` は本番には使えない [公式]。SQLite は無い | realm の設定は export / import (Operator の KeycloakRealmImport) で Git に置ける [未確認]。DB は自分でダンプする | 37,269。CNCF のプロジェクト (成熟度は [未確認]) |
| Authentik | server + worker + PostgreSQL | chart は既定なし (例は server・worker とも 512Mi)。Docker Compose の要件は 2 コア・2 GB [公式] | PostgreSQL 14〜18 だけ [公式]。2025.10 で Redis が要らなくなった [公式] | メジャー版を飛ばして上げられない。下げられない。マイグレーションは起動時に自動 [公式]。設定は blueprints で Git に置ける [公式]。バックアップは `pg_dump` [公式] | 25,899 |
| Zitadel | Zitadel + Login UI + init ジョブ + PostgreSQL | Zitadel 自体は約 512 MB、1 コア未満で動く [公式]。chart の本番の例は requests 512Mi・limits 2Gi、Login UI は 128Mi [公式] | PostgreSQL 14〜18 だけ。CockroachDB は v3 で外れた [公式] | [未確認] | 15,269 |
| Kanidm | 1 プロセス (DB は内蔵) | エントリ 1 つあたり 64 KB [公式] | 内蔵 [公式] | 公式の Helm chart は無い [公式] | 5,450 |
| Authelia | 1 プロセス | chart は既定なし (例は requests 50Mi・limits 125Mi) [公式] | SQLite・PostgreSQL・MySQL [公式] | [未確認] | 29,225 |
| Ory (Kratos + Hydra) | Kratos + Hydra + login/consent の UI + DB | [未確認] | [未確認] | [未確認] | Kratos 13,913・Hydra 17,598 |
| Pocket ID | 1 プロセス | [未確認] | [未確認] | [未確認] | 9,515 |
| Dex | 1 プロセス | chart は既定なし (例は 128Mi) [公式] | Kubernetes の CRD、SQLite (本番向きでない)、PostgreSQL、MySQL、etcd [公式] | 状態をほぼ持たない。正本は GitHub | 11,177 |

ホストの WSL2 のメモリは 31 GB で、空きは 16 GB ほど [観測]。Keycloak の 2 GB と PostgreSQL の 256 MB (この repo の Temporal の PostgreSQL と同じ上限 [観測]) は収まる。

### 候補ごとの判断

- **Keycloak**: 必須の軸 (管理画面、MFA、外部 IdP、セッションと監査) を全部、無料で満たすと今回確かめられたのは Keycloak と Authentik だけ (Zitadel はセッションと監査が [未確認])。
  Keycloak は機能で分けた有料版を持たない (Red Hat の有償サポート版はあるが、機能は同じ [未確認])。どの機能も有料になる心配が無い。業務での採用と学べることも多い (realm、client scope、mapper、Identity Brokering、Operator)。
  重さは Pod 1 つで 1.25〜2 GB。JVM のため起動に時間がかかる [未確認]
- **Authentik**: 機能は Keycloak に並ぶ。一方で次の点がある
  - Enterprise の線引きが Entra ID の連携と監査の強化にかかる (どこまでが有料かは [未確認])
  - server と worker の 2 つの Pod が要るので、合計のメモリは Keycloak と大差ない
- **Zitadel (軽さ重視の次点)**: 軽く、機能もそろう。ただし groups クレームを出さないので、次のどちらかが要る
  - Actions (webhook) で平たい claim を足す
  - 各 UI でロールの map を読み替える。API server は CEL の `claimMappings.groups.expression` で map のキーを取り出せる [未確認]。ArgoCD・Backstage でも同じことができるかは [未確認]

  ライセンスが AGPL-3.0 なのは、自宅で使うだけなら問題にならない
- **Kanidm・Pocket ID**: 軽いが、外部 IdP を並べられない (Kanidm は公式に未対応、Pocket ID は記載なし)。
  Kanidm は管理が CLI、Pocket ID は passkey しか使えない。「業務の構成を学ぶ」に合わない
- **Authelia**: 管理画面と上流の IdP が無い。groups を ID トークンに入れるのが非推奨の扱いなので、API server の RBAC と相性が悪い
- **Ory**: OIDC の issuer にするには、Kratos と Hydra と login/consent の UI を自分でつなぐ必要がある。管理画面も無い。部品として学ぶ価値はあるが、今回の目的には手間が大きい
- **Dex + GitHub**: 2026-10-07 の v2.46.0 で TOTP と WebAuthn が入り、「MFA が無い」は当たらなくなった。
  しかし、ユーザーの正本が GitHub にあるという形は変わらない。他の人に GitHub を求めないという今回の動機と合わない
- **Dex を Keycloak と組み合わせる**: 次の 2 つの置き方を考えたが、どちらも足し算が無いので採らない
  - Dex を前段に置き、Keycloak を Dex の OIDC connector にする: ホップが 1 つ増えるだけで、Keycloak の MFA やセッションの情報は Dex の後ろに隠れる
  - ArgoCD に同梱の Dex を Keycloak に向ける: ArgoCD は `oidc.config` で外の issuer を直接使える ([access-control.md](access-control.md))

### DB の用意の手段

Keycloak・Authentik・Zitadel はどれも、本番には外の PostgreSQL を求める。組み込みの DB は使えない。

- Keycloak の `dev-file` は本番には使えない [公式]
- Authentik と Zitadel は、chart が同梱する PostgreSQL をデモ用・開発用としている [公式]

| 手段 | 無料で続けられるか | `just down`・`just up` で残るか | バックアップ | 運用の手間 | 判断 |
|---|---|---|---|---|---|
| **素の StatefulSet (公式の `postgres` イメージ) + 静的 `local` PV** | 無料 (OSS) | 残る。PV の `local.path` をホストのディレクトリに向ける ([persistence.md](persistence.md) の案 D)。公式イメージは `$PGDATA/PG_VERSION` があれば初期化を飛ばす [公式] | 自分で作る。`pg_dump` の CronJob でホストに書き出す | メジャー版を上げるのは `pg_dump` と restore か `pg_upgrade` を自分で行う。18 からデータのパスが `/var/lib/postgresql/18/docker` に変わった [公式] | **推奨**。この repo に Temporal 用の同じ形がある [観測] |
| CloudNativePG (Operator) | 無料 (Apache-2.0、CNCF Sandbox) [公式] | PVC を Operator が直に作る (名前は `<cluster>-1` の形 [未確認])。事前に作った PV は使えるが勧めていない [公式]。クラスタを作り直したとき、新しい Cluster が残ったデータを引き継げるかは [未確認] | 物理バックアップはオブジェクトストレージ (Barman Cloud Plugin) か CSI の VolumeSnapshot [公式]。kind の `local` PV は snapshot に対応しない。`pg_dump` は Operator の管理の外 [公式] | Operator のメモリは既定なし (例は 100Mi〜200Mi) [公式]。メジャー版を宣言的に上げられる (1.26 から、オフライン) [公式] | 次点。業務の構成に近いが、作り直しとバックアップがこの環境の前提 (snapshot もオブジェクトストレージも無い) と噛み合わない |
| Bitnami の PostgreSQL chart (IdP の chart の同梱を含む) | **続けられない**。2025-08-28 から (告知の issue は 2025-07-16 に立った) 無料のイメージは `latest` タグだけになった。既存のタグは更新の無い `bitnamilegacy` に移った。本番向けは有料の Bitnami Secure Images [公式] | chart の設定による | chart による | 版を固定すると更新が止まる | 採らない |
| Azure Database for PostgreSQL Flexible Server | **12 か月だけ**。新規の Azure の顧客に、B1MS を月 750 時間、ストレージ 32 GB、バックアップ 32 GB [公式]。期間の後は従量課金で動き続ける [公式] | クラスタの外なので残る | マネージドのバックアップ | 公開の endpoint に自宅の IP の firewall の規則を足し、`sslmode=require` でつなぐ [公式]。自宅の IP が変わると切れる | 期限付きなので推奨しない |
| Neon の Free プラン | 無料のまま (期限なし)。1 プロジェクト 1 GB、100 CU 時間/月。5 分使わないと止まる [公式] | 外なので残る | [未確認] | インターネット越しになる。止まった後の最初の接続が遅くなる [未確認] | 比較だけ |
| Supabase の Free プラン | 500 MB、2 プロジェクト。1 週間使わないと一時停止する。自動バックアップは無い [公式] | 外なので残る | 無い [公式] | 1 週間の停止は IdP に向かない | 比較だけ |

素の StatefulSet を推奨する理由は次のとおり。

- **作り直しに強い**: データを置くディレクトリはホストに残り、PV は Git から毎回作り直される。PostgreSQL は既存のデータのディレクトリをそのまま開く。
  観測スタックで確かめた形 ([persistence.md](persistence.md) の「確認の結果」) をそのまま使える
- **バックアップをこの環境で完結できる**: `pg_dump` の出力をホストの別のディレクトリに置けば、オブジェクトストレージも snapshot も要らない。
  加えて、Keycloak の realm の設定を Git に置けば (realm の export、または KeycloakRealmImport)、DB を失っても設定は戻せる。戻らないのはユーザー・パスワード・MFA の登録とセッション
- **CloudNativePG は後から移れる**: `initdb` の import で既存の PostgreSQL から取り込める [公式]。オブジェクトストレージを持つ段になったら移る候補にする

### kind の中に IdP を置くときの鶏と卵

API server の OIDC は、クラスタの中の Keycloak に依存する。クラスタを作り直すとき、Keycloak が立つまでの間は次のようになる。

- **API server は起動する**: Structured Authentication Configuration の JWT の認証器は、裏で非同期に初期化され、10 秒ごとに再試行する。初期化が済むまで、その issuer のトークンだけが拒否される。
  クライアント証明書の認証器は別に並んでいるので、kind の admin の kubeconfig はそのまま使える [公式] (kubernetes のソース。文書には記載が無いので、実装の単位で一度確かめる)
- **`just up` は admin の kubeconfig で進める**: ArgoCD の導入から Keycloak の同期まで、OIDC に頼らない。Keycloak が立ったあとで、人の kubectl と Headlamp が OIDC で入れるようになる
- **DB を失ったときの戻し方**: realm の設定は Git から戻る。ユーザーは `pg_dump` から戻すか、作り直す。admin の kubeconfig と Keycloak の初期の管理者 (Operator が作る Secret [未確認]) は、非常用として残す
- **WebAuthn は issuer のホスト名に結び付く**: passkey は Relying Party の ID (ホスト名) ごとに登録される [未確認]。
  `keycloak.<tailnet>.ts.net` を後から変えると、登録し直しになる。到達経路 (単位 1) で名前を決めてから MFA を配る

## 推奨

| 層 | 選ぶもの | 選んだ理由 |
|---|---|---|
| IdP | **Keycloak** (公式の Keycloak Operator、namespace `auth`、realm `home-k8s`) | 必須の軸を全部、無料で満たす。機能で分けた有料版が無い。業務での採用が多い。Operator は CRD で Keycloak と realm の import を宣言でき、ArgoCD で同期できる |
| ユーザーとグループの正本 | **Keycloak** (外部 IdP は任意のログイン手段) | 他の人に GitHub を求めない。持っている人は GitHub・Google・Microsoft でも入れる |
| MFA | TOTP を必須アクションにし、WebAuthn (passkey) も選べるようにする | メールが要らず、一時パスワードと組み合わせて最初のログインで登録させられる |
| DB | **PostgreSQL の素の StatefulSet** + 静的 `local` PV (`home-k8s-local`) + `pg_dump` の CronJob | 無料で続けられ、作り直しでも残る。既存の永続化の方針どおり |
| 軽さ重視の次点 | Zitadel | Keycloak のメモリが問題になったとき。groups を Actions で足す手間を受け入れる前提 |
| Dex | 置かない | Keycloak が issuer を兼ねる。ArgoCD に同梱の Dex も使わない |

Dex + GitHub から変わる運用の手間は次のとおり。

| 項目 | Dex + GitHub | Keycloak + PostgreSQL |
|---|---|---|
| メモリ | 数十〜128 MB | Keycloak 1.25〜2 GB + PostgreSQL 64〜256 MB |
| 状態 | ほぼ持たない (正本は GitHub) | ユーザー・パスワードのハッシュ・MFA の登録・セッションを DB に持つ |
| バックアップ | 要らない | `pg_dump` の CronJob と、realm の設定の Git |
| バージョンアップ | イメージの差し替え | Keycloak は Operator の版を上げる。DB は PostgreSQL のメジャー版を上げるとき dump と restore を行う |
| 人を足す・外す | GitHub の org と team | Keycloak の Admin Console |

## 移行の段取り (実装の話題に分ける単位)

[access-control.md](access-control.md) の単位 2 (Dex) を、下の単位 2a〜2c に置き換える。
単位 1 (Tailscale) と単位 3〜8 (各 UI、API server、ワーカー、Azure) はそのまま進め、issuer の URL と groups の値だけを Keycloak のものにする。
Dex をまだ入れていない (2026-10-10 時点で未実装 [観測]) ので、Dex からデータを移す作業は無い。

| # | 話題 | やること | 完了の確かめ方 |
|---|---|---|---|
| 1 | 到達経路 (Tailscale) | access-control.md の単位 1 のまま。Keycloak のホスト名 (`keycloak.<tailnet>.ts.net`) もここで決める。**実装済み** ([tailscale.md](tailscale.md)。公開は Ingress、proxy の tag は `tag:k8s`、Keycloak の Ingress の形も同じ文書) | tailnet の別の端末から固定の URL で開ける |
| 2a | PostgreSQL | namespace `auth` に StatefulSet、静的 PV `keycloak-postgres`、`pg_dump` の CronJob を置く。[persistence.md](persistence.md) の「未決の論点」(ラベルとディレクトリの名前を一般的なものに替えるか) をここで決める。**実装済み** ([keycloak.md](keycloak.md)。名前は替えず、観測スタックと同じ extraMounts の下に置いた。作り直しは tailnet の端末の名前に当たるため) | `just down`・`just up` のあとも DB のデータが残る。dump のファイルがホストにできる |
| 2b | Keycloak | Keycloak Operator と `Keycloak` の CR を置き、PostgreSQL につなぐ。realm `home-k8s`、グループ、client scope (Group Membership mapper、`full.path: false`)、各 UI の client を KeycloakRealmImport で Git に置く。クラスタ内から issuer に届かせる方法を決める (access-control.md の注意点と同じ)。**実装済み** ([keycloak.md](keycloak.md)。Pod からは Tailscale の operator の egress と CoreDNS の rewrite で、ブラウザと同じ URL に届く。KeycloakRealmImport は realm が無いときしか効かないので、後からの変更は Admin Console か kcadm.sh でも入れる) | `/realms/home-k8s/.well-known/openid-configuration` がブラウザからも Pod からも引ける |
| 2c | ユーザーの登録と MFA | 自分のユーザーを作り、必須アクション (パスワードの変更、OTP の設定) を通す。必要なら GitHub などの Identity Provider を足す | 一時パスワードから TOTP の登録を経てログインでき、ID トークンの `groups` にグループ名が入る |
| 3〜6 | 各 UI と API server | access-control.md の単位 3〜6 と同じ。issuer を Keycloak にし、policy.csv や RoleBinding には Keycloak のグループ名を書く。Backstage のカタログのユーザーとグループは、GitHub の org からではなく Keycloak から取り込む (Backstage の Keycloak のプラグインを使う [未確認]) | access-control.md の確かめ方と同じ |
| 7・8 | ワーカーと Azure | access-control.md のまま (IdP と関係しない) | 同じ |

## 出典

開いて内容を確かめたものに (確認)、検索の要約だけで本文を開いていないものに (要約) を付けた。
GitHub の star の数とライセンスは、2026-10-10 に GitHub の API (`gh api repos/<owner>/<repo>`) で引いた。

- Keycloak
  - サーバーの管理ガイド (Admin Console、2 要素認証、Social Login、セッション、Events、必須アクション、SMTP) (確認): <https://www.keycloak.org/docs/latest/server_admin/index.html>
  - メモリと CPU のサイズの目安 (確認): <https://www.keycloak.org/high-availability/multi-cluster/concepts-memory-and-cpu-sizing>
  - DB (対応する DB、`dev-file` は本番に使えない) (確認): <https://www.keycloak.org/server/db>
  - Operator の既定のメモリ (requests 1700Mi、limits 2Gi) (確認): `operator/src/main/resources/application.properties` と `docs/guides/operator/advanced-configuration.adoc` (<https://github.com/keycloak/keycloak>)
  - Group Membership mapper の `full.path` の既定 (true) (確認): `services/src/main/java/org/keycloak/protocol/oidc/mappers/GroupMembershipMapper.java` (<https://github.com/keycloak/keycloak>)
  - codecentric の keycloakx chart (確認): <https://github.com/codecentric/helm-charts/tree/master/charts/keycloakx>
- Authentik
  - Helm chart の values と Chart.yaml (2026.8.3) (確認): <https://github.com/goauthentik/helm/tree/main/charts/authentik>
  - Kubernetes への導入 (同梱の PostgreSQL はデモ用) (確認): <https://docs.goauthentik.io/install-config/install/kubernetes/>
  - Docker Compose の要件 (確認): <https://docs.goauthentik.io/install-config/install/docker-compose/>
  - 2025.10 で Redis が不要に (確認): <https://docs.goauthentik.io/releases/2025.10/>
  - MFA のステージ (確認): <https://docs.goauthentik.io/add-secure-apps/flows-stages/stages/authenticator_validate/>
  - Sources (確認): <https://docs.goauthentik.io/users-sources/sources/>
  - Events (確認): <https://docs.goauthentik.io/sys-mgmt/events/>
  - OAuth2 provider と既定の scope の mapping (確認): <https://docs.goauthentik.io/add-secure-apps/providers/oauth2/>、<https://github.com/goauthentik/authentik/blob/main/blueprints/system/providers-oauth2.yaml>
  - バージョンアップ (確認): <https://docs.goauthentik.io/install-config/upgrade/>
  - バックアップ (確認): <https://docs.goauthentik.io/sys-mgmt/ops/backup-restore/>
  - blueprints (確認): <https://docs.goauthentik.io/customize/blueprints/>
  - 招待 (確認): <https://docs.goauthentik.io/users-sources/user/invitations/>
  - 料金 (Enterprise の機能) (確認): <https://goauthentik.io/pricing/>
  - ユーザーの操作 (セッションの削除) (要約): <https://docs.goauthentik.io/users-sources/user/user_basic_operations/>
- Zitadel
  - 本番の要件 (約 512 MB) (確認): <https://zitadel.com/docs/self-hosting/manage/production>
  - DB (PostgreSQL 14〜18) (確認): <https://zitadel.com/docs/self-hosting/manage/database>
  - Helm chart (確認): <https://github.com/zitadel/zitadel-charts/tree/main/charts/zitadel>
  - Console の既定の設定 (MFA) (確認): <https://zitadel.com/docs/guides/manage/console/default-settings>
  - 外部 IdP (確認): <https://zitadel.com/docs/guides/integrate/identity-providers/introduction>
  - ロールのクレーム (確認): <https://zitadel.com/docs/guides/integrate/retrieve-user-roles>
  - ライセンス (AGPL-3.0) (確認): <https://github.com/zitadel/zitadel/blob/main/LICENSING.md>
  - ユーザーの作成 (確認): <https://zitadel.com/docs/guides/manage/user/reg-create-user>
  - v3 で CockroachDB を外した (要約): <https://github.com/zitadel/zitadel/releases/tag/v3.0.0>
- Kanidm
  - 本 (サイズの目安、MFA、セッション、資格情報のリセット) (確認): <https://kanidm.github.io/kanidm/stable/print.html>
  - OAuth2 と groups のクレーム (確認): <https://kanidm.github.io/kanidm/stable/integrations/oauth2.html>
  - リポジトリ (管理は主に CLI) (確認): <https://github.com/kanidm/kanidm>
  - 上流の OIDC の扱い (要約): <https://github.com/kanidm/kanidm/issues/4015>
- Authelia
  - リポジトリ (MFA) (確認): <https://github.com/authelia/authelia>
  - Helm chart の values (確認): <https://github.com/authelia/chartrepo/blob/master/charts/authelia/values.yaml>
  - ロードマップ (管理画面、OIDC の Relying Party) (確認): <https://www.authelia.com/roadmap/>
  - OIDC のクレーム (groups は UserInfo) (確認): <https://www.authelia.com/integration/openid-connect/openid-connect-1.0-claims/>
  - OIDC provider (open beta、OpenID Certified) (確認): <https://www.authelia.com/integration/openid-connect/introduction/>
- Ory
  - Kratos と Hydra の役割分担、自前のホストに管理画面が無いこと (要約): <https://www.ory.com/kratos>、<https://github.com/ory/hydra>
- Pocket ID (確認): <https://pocket-id.org/docs/introduction>
- Dex
  - v2.46.0 のリリースノート (TOTP、WebAuthn、認証のセッション。2026-10-07) (確認): <https://github.com/dexidp/dex/releases/tag/v2.46.0>
  - ストレージ (確認): <https://dexidp.io/docs/configuration/storage/>
  - ローカルのユーザー (確認): <https://dexidp.io/docs/connectors/local/>
  - Helm chart の values (確認): <https://github.com/dexidp/helm-charts/blob/master/charts/dex/values.yaml>
- PostgreSQL
  - CloudNativePG のリポジトリ (Apache-2.0、CNCF Sandbox) (確認): <https://github.com/cloudnative-pg/cloudnative-pg>
  - CloudNativePG の chart の values (確認): <https://github.com/cloudnative-pg/charts/blob/main/charts/cloudnative-pg/values.yaml>
  - CloudNativePG のストレージ、バックアップ、メジャー版の更新、bootstrap (確認): <https://cloudnative-pg.io/docs/devel/storage>、<https://cloudnative-pg.io/docs/devel/backup>、<https://cloudnative-pg.io/docs/devel/postgres_upgrades>、<https://cloudnative-pg.io/docs/devel/bootstrap>
  - Bitnami のカタログの変更 (確認): <https://github.com/bitnami/containers/issues/83267>
  - 公式の `postgres` イメージ (PGDATA、18 からのパス) (確認): <https://github.com/docker-library/docs/blob/master/postgres/README.md>
  - 公式の `postgres` イメージの entrypoint (`PG_VERSION` があれば初期化しない) (確認): <https://github.com/docker-library/postgres/blob/master/docker-entrypoint.sh>
  - Azure の無料アカウント (Flexible Server の 12 か月) (確認): <https://azure.microsoft.com/en-us/pricing/purchase-options/azure-account>
  - Azure の Flexible Server の作成と firewall の規則 (確認): <https://learn.microsoft.com/en-us/azure/postgresql/configure-maintain/quickstart-create-server>
  - Neon の料金 (確認): <https://neon.com/pricing>
  - Supabase の料金 (確認): <https://supabase.com/pricing>
- Kubernetes
  - 認証、Structured Authentication Configuration (確認): <https://kubernetes.io/docs/reference/access-authn-authz/authentication/>
  - JWT の認証器の非同期の初期化 (確認): `staging/src/k8s.io/apiserver/plugin/pkg/authenticator/token/oidc/oidc.go`、`pkg/kubeapiserver/authenticator/config.go` (<https://github.com/kubernetes/kubernetes>)
- この repo
  - [access-control.md](access-control.md)
  - [persistence.md](persistence.md)
  - [temporal.md](temporal.md) (同じ形の PostgreSQL)
