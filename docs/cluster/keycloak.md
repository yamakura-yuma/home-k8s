# Keycloak と PostgreSQL (IdP)

kind のクラスタの namespace `auth` に、IdP の Keycloak と、その DB の PostgreSQL を置いた。
[idp-options.md](idp-options.md) の「移行の段取り」の単位 2a (PostgreSQL) と 2b (Keycloak) にあたる。

- issuer: **`https://keycloak.taild2b611.ts.net/realms/home-k8s`** (ブラウザからも Pod からも同じ URL)
- Admin Console: `https://keycloak.taild2b611.ts.net/admin/` (realm `master`、ユーザー `admin`。パスワードは下の「Secret」)
- ユーザーは `yamakura-yuma` (グループ `admins`、TOTP 必須、passkey も登録できる。下の「人のユーザーと MFA」。単位 2c)。各 UI と API server を OIDC につなぐのは単位 3〜6

## 構成

```text
tailnet の端末 ─HTTPS─▶ proxy (ts-keycloak-*、Ingress auth/keycloak) ─HTTP─▶ Service keycloak-service:8080 ─▶ Pod keycloak-0
                                   ▲ tailnet                                                                  │ JDBC
クラスタの Pod ─DNS─▶ CoreDNS (rewrite) ─▶ egress の proxy (ts-keycloak-tailnet-*) ──┘                         ▼
                                                                         Service keycloak-postgres:5432 ─▶ Pod keycloak-postgres-0
                                                                                                                │ PVC data-keycloak-postgres-0
                                                                                                                ▼
          CronJob keycloak-pg-dump (毎日 04:00 JST) ─pg_dump─▶ PVC keycloak-postgres-dump      PV keycloak-postgres (home-k8s-local, Retain)
                                                                 │                                              │
                                                                 ▼                                              ▼
                     ホスト ~/.local/share/home-k8s/observability/keycloak-postgres-dump/   ~/.local/share/home-k8s/observability/keycloak-postgres/
```

| 置いたもの | 場所 | Application |
|---|---|---|
| Keycloak Operator (公式、26.8.0) と CRD | [clusters/kind/auth/operator](../../clusters/kind/auth/operator) (上流の manifest をタグで取り込む kustomization) | `keycloak-operator` (sync wave -1) |
| PostgreSQL (公式の `postgres:17.6-alpine` の StatefulSet) と `pg_dump` の CronJob | [clusters/kind/auth/postgres](../../clusters/kind/auth/postgres) | `keycloak-postgres` |
| PV `keycloak-postgres`・`keycloak-postgres-dump` | [clusters/kind/storage](../../clusters/kind/storage) | `storage` |
| `Keycloak` の CR、realm の `KeycloakRealmImport`、egress の Service | [clusters/kind/auth/keycloak](../../clusters/kind/auth/keycloak) | `keycloak` |
| tailnet への Ingress | [clusters/kind/tailscale/ingress/keycloak.yaml](../../clusters/kind/tailscale/ingress/keycloak.yaml) | `tailscale-ingress` |
| Secret と CoreDNS の設定 | `just up` の `_keycloak-secrets`・`_coredns-tailnet` ([just/keycloak.just](../../just/keycloak.just)) | (ArgoCD の外) |

- **operator は上流の manifest をそのまま使う**。Helm chart は公式に無い。kustomization で namespace を `keycloak` から `auth` に替え、
  ClusterRoleBinding の subjects も替える (上流の kustomization.yml と同じ `setRoleBindingSubjects`)。CRD が 400 KB を超えるので、Application は ServerSideApply にする
- **Keycloak は HTTP で受け、TLS は Tailscale の proxy が終端する**。`hostname` に `https://keycloak.taild2b611.ts.net` を丸ごと書き、
  フロントの URL も backchannel (token・userinfo・jwks) の URL もこの 1 つにそろえる (`backchannelDynamic: false`)。
  proxy (tailscale serve) は `X-Forwarded-For`・`X-Forwarded-Host`・`X-Forwarded-Proto` を付けるので (tailscale の `ipn/ipnlocal/serve.go`)、`proxy.headers: xforwarded`
- **operator の既定の Ingress は切る** (`ingress.enabled: false`)。kind に Ingress controller は無く、公開は Tailscale の Ingress だけにする
- **メモリ**: Keycloak は requests 1Gi・limits 2Gi (operator の既定の requests 1700Mi を下げた)。PostgreSQL は requests 64Mi・limits 256Mi (Temporal の DB と同じ)

## Pod から issuer に届かせる

各 UI のバックエンドは、トークンを確かめるために issuer の discovery と JWKS を取りに行き、issuer の文字列の一致を確かめる。
Pod からも `https://keycloak.taild2b611.ts.net` で、同じ証明書で届く必要がある。

**選んだ形: Tailscale の operator の egress + CoreDNS の rewrite**。

1. `auth/keycloak-tailnet` (ExternalName の Service、注釈 `tailscale.com/tailnet-fqdn: keycloak.taild2b611.ts.net`。[tailnet.yaml](../../clusters/kind/auth/keycloak/tailnet.yaml)) を置く。
   operator が egress の proxy (`tailscale/ts-keycloak-tailnet-*`) を立て、Service の `externalName` をその proxy に書き換える
   (ArgoCD が戻さないよう、Application `keycloak` の `ignoreDifferences` で外す)
2. `just up` が CoreDNS の Corefile に `rewrite name exact keycloak.taild2b611.ts.net keycloak-tailnet.auth.svc.cluster.local` を足す
   ([just/coredns-tailnet.sh](../../just/coredns-tailnet.sh)。印の行で囲み、打ち直しても同じになる。CoreDNS は `reload` で読み直す)
3. Pod の `keycloak.taild2b611.ts.net` → egress の proxy → tailnet → Keycloak の Ingress の proxy (TLS の終端、Let's Encrypt の証明書) → Keycloak

選んだ理由と、採らなかった案は次のとおり。

| 案 | 採らなかった理由 |
|---|---|
| **egress + CoreDNS の rewrite (採用)** | — 証明書も issuer の文字列もブラウザと同じ。Keycloak にも各 UI にも、内向きの別の URL や CA の配布が要らない |
| CoreDNS の rewrite で Keycloak の Service に直に向ける | Service は HTTP。HTTPS で受けるには Keycloak に同じ名前の証明書を持たせる必要があり、Tailscale の証明書はクラスタの中の Pod には出せない |
| operator の DNSConfig (nameserver) に CoreDNS から forward する | nameserver の Service の ClusterIP を Corefile に書くことになり、クラスタを作り直すと変わる。rewrite は名前で書ける |
| Keycloak の `backchannelDynamic: true` で、Pod には `http://keycloak-service.auth:8080` を使わせる | discovery の `issuer` は変わらないが、各 UI は issuer の URL から discovery を引くので、結局 Pod から issuer の URL に届く必要がある |

- egress の proxy は tailnet の端末 (tag `tag:k8s`) で、Keycloak の Ingress の proxy (同じ `tag:k8s`) に 443 でつなぐ。tailnet のポリシーは既定 (全部許可) のままでよい
- egress の proxy は初回に 3 回再起動してから安定した。Keycloak の Ingress の端末が tailnet に出るのを待っていたと見ている [未確認]
- **kind の API server (単位 6) はこの経路を使えない**。API server は control-plane のホストのネットワークで動き、CoreDNS を引かない。
  AuthenticationConfiguration の `issuer.url` には上の issuer を書き、`issuer.discoveryURL` でクラスタ内の別の URL を指す形になる
  ([access-control.md](access-control.md) の注意点)。ただし discoveryURL の先も、issuer と同じ値を返す HTTPS で、API server が信頼する証明書が要る。単位 6 で決める

## realm home-k8s

[realm-home-k8s.yaml](../../clusters/kind/auth/keycloak/realm-home-k8s.yaml) の `KeycloakRealmImport` で宣言する。operator が import の Job (`auth/home-k8s`) を一度走らせる。

| 項目 | 値 |
|---|---|
| グループ | `admins`、`viewers` (各 UI の RBAC にこの名前を書く) |
| client scope `groups` | Group Membership mapper (`full.path: "false"`、claim `groups`、ID トークン・アクセストークン・userinfo)。realm の既定の client scope に足したので、全部の client に付く |
| 既定の client scope | Keycloak の既定 (basic・profile・email・roles・web-origins・acr など) + groups。`clientScopes` を書くと既定のものが作られなくなるので、realm の属性 `CreateDefaultClientScopes: "true"` で作らせた |
| 自分での登録・パスワードのリセット | 無効 (人は管理者が作る。SMTP は無い) |
| MFA | TOTP (HmacSHA1・6 桁・30 秒) が既定の必須アクション (`CONFIGURE_TOTP` の `defaultAction`)。passkey (WebAuthn) も登録できる。下の「人のユーザーと MFA」 |

client (単位 3〜6 で使うものを先に全部置いた)。redirect URI は tailnet の URL。

| clientId | 種類 | 使う単位 | redirect URI |
|---|---|---|---|
| `argocd` | confidential | 3 | `https://argocd.taild2b611.ts.net/auth/callback` |
| `argocd-cli` | public + PKCE | 3 (`argocd login --sso`、oidc.config の `cliClientID`) | `http://localhost:8085/auth/callback` |
| `grafana`・`grafana-dev`・`grafana-prod` | confidential | 3 | `https://grafana{,-dev,-prod}.taild2b611.ts.net/login/generic_oauth` |
| `temporal-dev`・`temporal-prod` | confidential | 4 | `https://temporal-{dev,prod}.taild2b611.ts.net/auth/sso/callback` |
| `backstage` | confidential | 4 | `https://backstage.taild2b611.ts.net/api/auth/*` (provider の名前で callback のパスが変わるため) |
| `oauth2-proxy` | confidential | 4 | `https://oauth2-proxy.taild2b611.ts.net/oauth2/callback` (**仮**。置くホストは単位 4 で決める) |
| `headlamp` | confidential | 5 | `https://headlamp.taild2b611.ts.net/oidc-callback` |
| `kubernetes` | public + PKCE | 6 (kubelogin) | `http://localhost:8000`、`http://localhost:18000` |

- API server (単位 6) の `audiences` には、ID トークンを渡してくる client の名前 (`kubernetes` と `headlamp`) を書く。ID トークンの `aud` は client の名前になる
- client の secret は Git に置かない。realm の `${CLIENT_SECRET_*}` を、import のときに Secret `keycloak-clients` が埋める (`spec.placeholders`)

### realm を後から変える

**`KeycloakRealmImport` は realm が無いときにしか効かない** (operator の文書の Realm Import の制約。realm がすでにあると上書きしない)。
DB はホストに残るので、作り直しても import は 2 回目以降は何もしない。後から client や redirect URI を変えるときは、次の 2 つを両方行う。

1. `realm-home-k8s.yaml` を直す (DB を失ったときに戻る正本)
2. 動いている Keycloak にも同じ変更を入れる。Admin Console で直すか、`kcadm.sh` を使う

```sh
kubectl --context kind-study-kind -n auth exec -it keycloak-0 -- bash -c '
  kcadm=/opt/keycloak/bin/kcadm.sh
  $kcadm config credentials --config /tmp/k --server http://localhost:8080 --realm master \
    --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD"
  id=$($kcadm get clients -r home-k8s -q clientId=oauth2-proxy --fields id --format csv --noquotes --config /tmp/k)
  $kcadm update clients/$id -r home-k8s -s "redirectUris=[\"https://<ホスト>/oauth2/callback\"]" --config /tmp/k
  rm -f /tmp/k'
```

operator 26.8.0 には、client を CR ごとに作り・更新する `KeycloakOIDCClient` (v2alpha1、preview) もある。
`client-admin-api:v2` の feature と operator 用の管理の client が要り、preview なので今回は使っていない。CRD だけは operator が controller を起動するので入れてある。

## 人のユーザーと MFA

ユーザーは Git に置かない (realm の宣言にも書かない)。DB を失うと戻らないので、dump (下の「バックアップと戻し方」) が頼り。

| 項目 | 値 |
|---|---|
| ユーザー | `yamakura-yuma` (グループ `admins`)。作り方は下の「ユーザーを作る」 |
| ログイン | パスワード + TOTP (既定の browser の flow の `Browser - Conditional 2FA`。OTP を登録したユーザーに OTP を聞く)。または passkey だけ |
| TOTP | realm の必須アクション `CONFIGURE_TOTP` を `defaultAction: true` にした。新しいユーザーは最初のログインで OTP を設定させられる |
| passkey | `webAuthnPolicyPasswordlessPasskeysEnabled: true` (Keycloak 26.8.0 の feature `PASSKEYS`、既定で有効)。登録は Account Console の「アカウントセキュリティ → サインイン」。登録するとログイン画面で passkey が選べ、パスワードと OTP を飛ばす (passkey は端末の所持と生体認証か PIN を兼ねる) |
| Relying Party の ID | `keycloak.taild2b611.ts.net` (2FA 用と passwordless 用の両方)。ブラウザが開くホスト名と合わないと登録できず、変えると登録済みの passkey は全部使えなくなる |
| Account Console | `https://keycloak.taild2b611.ts.net/realms/home-k8s/account` (パスワード・OTP・passkey を自分で変える) |

- 認証の flow は realm の宣言に書かない。`authenticationFlows` を書くと、import で Keycloak の既定の flow (browser など) が作られなくなる。
  同じ理由で `requiredActions` は既定の 14 個を全部並べた (一部だけ書くと、書かなかったものは作られない。scratch の realm に import して確かめた)
- passkey を 2 つ目の要素 (パスワード + passkey) に使う形は入れていない。そのためには browser の flow の `WebAuthn Authenticator` を有効にする必要があり、上の理由で flow を宣言に書くことになる

以下の手順は開発用コンテナ (`just devcontainer shell`。kubectl があり、`~/.config/home-k8s` も見える) で打つ。どれも `kcadm.sh` を Keycloak の Pod の中で打つ (管理者のパスワードは Pod の環境変数から読み、コマンドラインに出さない)。
Admin Console (`https://keycloak.taild2b611.ts.net/admin/` → realm `home-k8s` → Users) でも同じことができる。

```sh
# 下の各手順の前に定義する。kc '<kcadm の引数>' で、realm master の admin でログインした kcadm.sh を打つ
kc() {
  kubectl --context kind-study-kind -n auth exec -i keycloak-0 -- bash -c '
    kcadm() { /opt/keycloak/bin/kcadm.sh "$@" --config /tmp/k; }
    kcadm config credentials --server http://localhost:8080 --realm master \
      --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null 2>&1
    '"$1"'
    rm -f /tmp/k'
}
```

### ユーザーを作る

一時パスワードは本人だけが読めるホストのファイルで渡す (チャットやメッセージに書かない)。必須アクションは OTP の設定とパスワードの変更。

```sh
user=<ユーザー名>; group=admins   # または viewers
f=~/.config/home-k8s/keycloak-initial-password
(umask 077; head -c 18 /dev/urandom | base64 | tr '+/' '-_' > "$f")   # 英数字と - _ だけ (JSON に入れても崩れない)
id=$(kc "kcadm create users -r home-k8s -s username=$user -s enabled=true -s 'groups=[\"/$group\"]' \
  -s 'requiredActions=[\"UPDATE_PASSWORD\",\"CONFIGURE_TOTP\"]' -i")
# パスワードは標準入力で渡す (Pod のプロセスの引数に出さない)
printf '{"type": "password", "temporary": true, "value": "%s"}' "$(cat "$f")" | kc "kcadm update users/$id/reset-password -r home-k8s -f -"
kc "kcadm get users/$id/groups -r home-k8s --fields name"   # [{"name":"admins"}]
```

本人は Account Console を開き、一時パスワードでログインし、OTP の設定 (認証アプリで QR を読む) と新しいパスワードの設定を済ませる。
済んだら一時パスワードのファイルは消す (`rm "$f"`。パスワードを変えた時点で使えなくなっている)。

### ユーザーを外す

```sh
id=$(kc "kcadm get users -r home-k8s -q username=<ユーザー名> -q exact=true --fields id --format csv --noquotes")
kc "kcadm delete users/$id -r home-k8s"                 # ユーザーごと消す (セッションも消える)
# 消さずに止めるだけなら: kc "kcadm update users/$id -r home-k8s -s enabled=false"
# グループだけ外すなら:   gid=$(kc "kcadm get groups -r home-k8s -q search=admins --fields id --format csv --noquotes")
#                         kc "kcadm delete users/$id/groups/$gid -r home-k8s"
```

各 UI のセッションは、各 UI の側の期限まで残りうる (単位 3〜6 で UI ごとに確かめる)。Keycloak のセッションは `kc "kcadm create users/$id/logout -r home-k8s"` で消せる。

### MFA を設定し直す (認証アプリや passkey の端末を失くした)

```sh
id=$(kc "kcadm get users -r home-k8s -q username=<ユーザー名> -q exact=true --fields id --format csv --noquotes")
kc "kcadm get users/$id/credentials -r home-k8s --fields id,type,userLabel"          # otp・webauthn-passwordless・password
kc "kcadm delete users/$id/credentials/<消す credential の id> -r home-k8s"          # 失くした OTP や passkey を消す
kc "kcadm update users/$id -r home-k8s -s 'requiredActions=[\"CONFIGURE_TOTP\"]'"   # 次のログインで OTP を設定し直させる
kc "kcadm create users/$id/logout -r home-k8s"
```

パスワードも忘れたときは、上の「ユーザーを作る」の一時パスワードの手順 (ファイルを作り、`reset-password` に流す) を同じ `id` に対して行い、
`requiredActions` に `UPDATE_PASSWORD` も足す。自分しか管理者がいないので、自分のユーザーで入れないときは realm master の `admin` (下の「非常用の管理者」) を使う。

### realm の MFA の設定を動いている Keycloak に入れる

realm の import は realm があると何もしない (上の「realm を後から変える」) ので、単位 2c の設定は宣言を直したうえで次のコマンドで入れた。

```sh
kc "kcadm update realms/home-k8s -s otpPolicyType=totp -s otpPolicyAlgorithm=HmacSHA1 -s otpPolicyDigits=6 -s otpPolicyPeriod=30 \
  -s webAuthnPolicyRpEntityName=home-k8s -s webAuthnPolicyRpId=keycloak.taild2b611.ts.net \
  -s webAuthnPolicyPasswordlessRpEntityName=home-k8s -s webAuthnPolicyPasswordlessRpId=keycloak.taild2b611.ts.net \
  -s webAuthnPolicyPasswordlessPasskeysEnabled=true
  kcadm update authentication/required-actions/CONFIGURE_TOTP -r home-k8s -s defaultAction=true"
```

## Secret

秘密は Git に置かない。`just up` の `_keycloak-secrets` ([just/keycloak-secrets.sh](../../just/keycloak-secrets.sh)) が、
ホストのファイル (無ければ生成し、以後は再利用する) から Secret を作る。値はコマンドラインに出さず、`last-applied-configuration` の注釈も付けない (既存の `_*-secrets` と同じ)。

| Secret (namespace auth) | キー | 元のファイル (`~/.local/share/home-k8s/keycloak/`) | 読むもの |
|---|---|---|---|
| `keycloak-postgres` | `username` (`keycloak`)・`password` | `postgres-password` | PostgreSQL (初回の initdb)、Keycloak の `db`、`pg_dump` の CronJob |
| `keycloak-bootstrap-admin` | `username` (`admin`)・`password` | `admin-password` | Keycloak の `bootstrapAdmin` (DB が空のときの最初の起動で、realm master に作る) |
| `keycloak-clients` | clientId ごと (confidential の 9 つ) | `clients/<clientId>` | realm の import の placeholders。各 UI (単位 3〜6) も同じファイルから自分の namespace の Secret を作る |

- **ファイルを消さない**。DB とその中の realm はホストのディレクトリに残る。ファイルを作り直すと、Secret の値と DB の中の値 (DB のパスワード、管理者のパスワード、client の secret) がずれる
- DB のパスワードと管理者のパスワードは、DB が空のときにしか使われない。変えたいときは DB の側を先に変えてから (`ALTER USER`、Admin Console)、ファイルを書き換えて `just up`
- client の secret を変えるときは、Admin Console (client の Credentials) で再生成した値をファイルに書き、`just up` を打ち、各 UI の Pod を作り直す

## 非常用の管理者

- **realm master の `admin`** (`keycloak-bootstrap-admin`)。自分のユーザー (realm home-k8s) が使えなくなったときに Admin Console に入るためのもの。
  Keycloak 26 は bootstrap の管理者を「一時的」として画面に警告を出す。単位 2c では恒久の管理者を作らず、この admin を非常用に残した (realm home-k8s の自分のユーザーは master の管理者ではない)
- 管理者のパスワードを失ったときは、Keycloak の Pod で `kc.sh bootstrap-admin user` を打って別の一時的な管理者を作る (公式の Bootstrapping の手順)
- **kind の admin の kubeconfig** は OIDC に依存しないので、Keycloak が落ちていてもクラスタを操作できる ([idp-options.md](idp-options.md) の鶏と卵)

## バックアップと戻し方

- **毎日**: CronJob `keycloak-pg-dump` が 04:00 (Asia/Tokyo) に `pg_dump --clean --if-exists` を gzip で書き出す。
  先はホストの `~/.local/share/home-k8s/observability/keycloak-postgres-dump/keycloak-<UTC の時刻>.sql.gz`。14 日より古いものは消す。
  ファイルは uid 70 (postgres) の持ち物で本人だけが読める (パスワードのハッシュを含むため)。ホストから読むときは `sudo`
- **手で走らせる**

```sh
kubectl --context kind-study-kind -n auth create job --from=cronjob/keycloak-pg-dump keycloak-pg-dump-manual-$(date +%s)
```

- **戻す** (DB を失った、または壊れたとき)。dump を流し込んでから、Keycloak の Pod を作り直してキャッシュを捨てる。
  `--clean --if-exists` で書き出してあるので、同じ DB に流し直せる。Keycloak を 0 台にして止める手は使えない
  (`Keycloak` の CR を変えても ArgoCD の selfHeal が戻し、StatefulSet を変えても operator が戻す)。開発用コンテナ (`just devcontainer shell`) で打つ (root なので dump を読める)

```sh
k="kubectl --context kind-study-kind -n auth"
f=$(ls ~/.local/share/home-k8s/observability/keycloak-postgres-dump/keycloak-*.sql.gz | tail -1)
gunzip -c "$f" | $k exec -i keycloak-postgres-0 -- psql -v ON_ERROR_STOP=1 -U keycloak -d keycloak
$k delete pod keycloak-0
```

  確かめたのは別の DB (`restore_check`) への流し込みまで。動いている DB への流し込みは試していない [未確認]

- dump も DB も無いときは、DB を空から作り直せば realm の設定は Git から戻る (import が走る)。戻らないのはユーザー・パスワード・MFA の登録・セッション

## 鶏と卵

- **`just up` は Keycloak に依存しない**。admin の kubeconfig で ArgoCD の導入から Keycloak の同期まで進む。
  Keycloak の Secret と CoreDNS の設定は ArgoCD の同期 (root.yaml) より前に作る
- **同期の順**: `storage` (PV) と `keycloak-operator` (CRD) が wave -1。`keycloak-postgres` と `keycloak` は wave 0。
  Keycloak は DB が立つまで再起動を繰り返してつなぐ [未確認: 今回は DB が先に Ready になった]
- **DB は作り直しても残る**。PV の `local.path` はホストのディレクトリで、公式の postgres イメージは `$PGDATA/PG_VERSION` があれば初期化を飛ばす。
  realm の import も、realm があれば何もしない
- **issuer の名前 (`keycloak.taild2b611.ts.net`) を変えない**。各 UI の設定、API server の設定、passkey の登録 (Relying Party の ID はホスト名) が全部やり直しになる
- **`just down` に注意**。Tailscale の operator が作る proxy の端末は ephemeral ではない (operator の `newAuthKey`)。
  `kind delete cluster` は端末を tailnet から消さないので、作り直したクラスタの proxy は同じ名前を取れず `keycloak-1` のような名前になりうる [未確認]。
  作り直す前に Tailscale の管理画面で `tag:k8s` の端末を消すか、先に Ingress を消して operator に端末を消させる。この単位では `just down` をしていない (下の「確かめた結果」)

## データの置き場 ([persistence.md](persistence.md) の未決の論点)

PV は観測スタックと同じ extraMounts (`~/.local/share/home-k8s/observability` → ノードの `/var/local/home-k8s/observability`) の下に置き、
ラベル `home-k8s/observability-storage` もそのまま使った。名前を一般的なもの (`volumes` など) に替えるには、kind-config の変更とクラスタの作り直しが要る。
作り直しは上の `just down` の注意 (tailnet の端末の名前) に当たるので、この単位ではしなかった。

## 確かめ方

```sh
kubectl --context kind-study-kind -n auth get pods,pvc                                  # keycloak-0・keycloak-postgres-0・operator が Running、PVC 2 つが Bound
kubectl --context kind-study-kind -n auth get keycloak keycloak -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'   # True
kubectl --context kind-study-kind -n auth get keycloakrealmimport home-k8s -o jsonpath='{.status.conditions[?(@.type=="Done")].status}'  # True
# Pod から (issuer と TLS の検証)
kubectl --context kind-study-kind run kc-curl --rm -i --restart=Never --image=curlimages/curl:8.16.0 --command -- \
  curl -sS https://keycloak.taild2b611.ts.net/realms/home-k8s/.well-known/openid-configuration
```

tailnet の端末のブラウザで `https://keycloak.taild2b611.ts.net/realms/home-k8s/.well-known/openid-configuration` を開き、`issuer` が同じ値か見る。

## 確かめた結果

2026-10-11 (JST) に `kind-study-kind` で確かめた (クラスタは作り直していない)。

| 確認 | 結果 |
|---|---|
| Application | `keycloak-operator`・`keycloak-postgres`・`keycloak` が Synced / Healthy |
| Keycloak | CR の Ready=True、realm の import の Job が Complete (33 秒) |
| realm | グループ admins・viewers。既定の client scope は既定のもの + groups。client 11 個。client の secret は Secret の値と一致 |
| Pod から | `curlimages/curl` の Pod で discovery が 200、TLS の検証が通る (`ssl_verify_result` 0)、`issuer` が `https://keycloak.taild2b611.ts.net/realms/home-k8s` |
| tailnet から | 別の proxy の Pod (tailnet 側) から同じ discovery が引けた |
| pg_dump | 手で走らせた Job でホストに `keycloak-<時刻>.sql.gz` (約 74 KB) ができた。別の DB に流し込んで realm 2 つ (home-k8s・master) が戻ることを確かめ、その DB は消した |
| 作り直し | StatefulSet・PVC・PV を消して作り直した (`just down` で起きることと同じ) → 新しい PVC が同じ PV に結ばれ、PostgreSQL は初期化を飛ばし、realm の id が前と同じ。Keycloak の Pod も作り直し、admin で入れた |

単位 2c (人のユーザーと MFA) は 2026-10-11 (JST) に確かめた。

| 確認 | 結果 |
|---|---|
| realm の宣言 | `realm-home-k8s.yaml` の realm を名前だけ替えて scratch の realm に import → 必須アクション 14 個 (`CONFIGURE_TOTP` だけ defaultAction)、RP ID、passkey、既定の browser の flow、client 11 個、既定の client scope が今の realm と同じ。確かめた後に消した |
| 動いている realm | 上の「realm の MFA の設定を動いている Keycloak に入れる」を打ち、`kcadm.sh` で値が入ったことを確かめた |
| ユーザー | `yamakura-yuma` をグループ `admins`、必須アクション `UPDATE_PASSWORD`・`CONFIGURE_TOTP`、一時パスワード (本人だけが読めるホストのファイル) で作った |
| 人のログイン | 本人が tailnet の端末で Account Console に入り、OTP の設定とパスワードの変更、passkey の登録、サインアウトしてからの passkey でのログインまでできた。ユーザーの credential は `password`・`otp`・`webauthn-passwordless`、必須アクションは空 |
| groups | Admin Console の client の「Client scopes → Evaluate」と同じ API (`evaluate-scopes/generate-example-id-token`) で、client `kubernetes`・`argocd-cli` の ID トークンに `groups: ["admins"]`・`preferred_username`・`aud` (client 名)・issuer が入る。実際のログインで取った ID トークンでは見ていない [未確認] (kubelogin の localhost の callback に Windows のブラウザから WSL へ届かなかった。単位 3・6 で確かめる) |

## 出典

- Keycloak Operator のインストール・基本の導入・Realm Import (realm があれば上書きしない、placeholders)・Managing Clients (preview)・Advanced configuration (bootstrapAdmin) (確認):
  `docs/guides/operator/*.adoc` (<https://github.com/keycloak/keycloak>、タグ 26.8.0)、<https://www.keycloak.org/operator/realm-import>
- 上流の manifest と kustomization.yml (確認): <https://github.com/keycloak/keycloak-k8s-resources/tree/26.8.0/kubernetes>
- import のときの既定の client scope (`CreateDefaultClientScopes`) (確認): `services/src/main/java/org/keycloak/services/managers/RealmManager.java` (Keycloak 26.8.0)
- Keycloak の hostname と reverse proxy (確認): <https://www.keycloak.org/server/hostname>、<https://www.keycloak.org/server/reverseproxy>
- Tailscale の egress (tailnet-fqdn) (確認): <https://tailscale.com/kb/1438/kubernetes-operator-cluster-egress>
- tailscale serve が付けるヘッダー (確認): `ipn/ipnlocal/serve.go` (<https://github.com/tailscale/tailscale>、v1.102.4)
- operator の proxy の auth key が ephemeral でないこと (確認): `cmd/k8s-operator/sts.go` の `newAuthKey` (v1.102.4)
- CoreDNS の rewrite (確認): <https://coredns.io/plugins/rewrite/>
- 単位 2c の MFA (観測): feature `PASSKEYS` が既定で有効なこと (`kcadm.sh get serverinfo`)、`RealmRepresentation` に `webAuthnPolicyPasswordlessPasskeysEnabled` があること (keycloak-core 26.8.0 の jar)、
  `requiredActions` を一部だけ書くと残りが作られないこと (scratch の realm への import)。いずれも Keycloak 26.8.0 の Pod で確かめた。
  passkey と必須アクションの説明 (未確認: 読んでいない): <https://www.keycloak.org/docs/latest/server_admin/#passkeys>
