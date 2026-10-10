# kind の API server・kubectl・Headlamp の OIDC (Keycloak)

kind の API server が Keycloak (realm `home-k8s`) の ID トークンを受け入れるようにし、kubectl は kubelogin で、Headlamp は自分の OIDC でログインする。
権限はどちらも Kubernetes の RBAC で、Keycloak のグループ `admins`・`viewers` に結ぶ。
[idp-options.md](idp-options.md) の「移行の段取り」の単位 5・6 の残り (access-control.md の単位 5) にあたる。

```text
kubectl ─exec─▶ kubelogin (開発用コンテナ、localhost:8000 で callback を待つ) ─PKCE─▶ Keycloak (client kubernetes)
   │ ID トークン (aud kubernetes)
   ▼
kind の API server ── AuthenticationConfiguration ──▶ username oidc:<preferred_username>、groups oidc:<groups>
   ▲ ID トークン (aud headlamp)                         │ discovery・JWKS: CoreDNS の rewrite → Tailscale の egress → Keycloak
Headlamp (https://headlamp.taild2b611.ts.net) ─▶ Keycloak (client headlamp)    ▼
                                                                    RBAC: oidc:admins → cluster-admin、oidc:viewers → view + 読み取りの ClusterRole
```

| 置いたもの | 場所 |
|---|---|
| AuthenticationConfiguration | [clusters/kind/kube-apiserver/authentication-config.yaml](../../clusters/kind/kube-apiserver/authentication-config.yaml) |
| kube-apiserver の static Pod への patch (`dnsPolicy`) | [clusters/kind/kube-apiserver/patches/kube-apiserver+strategic.yaml](../../clusters/kind/kube-apiserver/patches/kube-apiserver+strategic.yaml) |
| 上の 2 つを API server に読ませる設定 | [kind-config.yaml](../../clusters/kind/kind-config.yaml) の control-plane の `extraMounts`・`kubeadmConfigPatches`。ファイルは `just up` (`_kind-up`) がホストの `~/.local/share/home-k8s/kube-apiserver/` に置く |
| グループの RBAC | [clusters/kind/auth/rbac/oidc-groups.yaml](../../clusters/kind/auth/rbac/oidc-groups.yaml) (Application `oidc-rbac`) |
| kubectl の context `oidc@study-kind` | `just up` の `_kube-oidc-context` ([just/keycloak.just](../../just/keycloak.just)) |
| kubelogin | 開発用コンテナ (flake.nix の `kubelogin-oidc`。`kubectl oidc-login`) |
| Headlamp の OIDC | [clusters/kind/headlamp/values.yaml](../../clusters/kind/headlamp/values.yaml) の `config.oidc`・`env`。client の secret は Secret `headlamp/headlamp-oidc` (`just up` の `_oidc-secrets`) |

## API server の設定

| 項目 | 値 | 理由 |
|---|---|---|
| `issuer.url` | `https://keycloak.taild2b611.ts.net/realms/home-k8s` | トークンの `iss` と同じ文字列。API server は discovery の `issuer` との一致も確かめる |
| `audiences` | `kubernetes`・`headlamp` (`audienceMatchPolicy: MatchAny`) | ID トークンの `aud` は client の名前。kubectl (kubelogin) と Headlamp の 2 つ |
| username | `preferred_username` に接頭辞 `oidc:` | Keycloak のユーザー名 (realm の `editUsernameAllowed` は既定の false で、本人は変えられない)。接頭辞で ServiceAccount や `system:` の名前とぶつからない |
| groups | `groups` に接頭辞 `oidc:` | realm の client scope `groups` が `["admins"]` の形で入れる。RBAC には `oidc:admins`・`oidc:viewers` と書く |
| CA | 書かない | Keycloak の Ingress の proxy が Let's Encrypt の証明書を出すので、API server のイメージの既定の CA で通る |

API server は `--authentication-config` のファイルを読み直すので、書き換えても再起動は要らない。
ファイルが壊れていると API server が起動しないので、直すときは `kubectl --context kind-study-kind get --raw /readyz` で戻ったことを確かめる。

### API server から issuer に届かせる

**選んだ形: kube-apiserver の static Pod を `dnsPolicy: ClusterFirstWithHostNet` にし、Pod と同じ経路を使う。**

API server は control-plane のホストのネットワークで動き、既定 (`ClusterFirst`) ではノードの resolv.conf を引く。
`ClusterFirstWithHostNet` にすると kubelet が CoreDNS (`10.96.0.10`) を resolv.conf に書く。CoreDNS には `just up` が
`keycloak.taild2b611.ts.net` → egress の Service の rewrite を入れてあるので ([keycloak.md](keycloak.md) の「Pod から issuer に届かせる」)、
API server も Pod と同じく egress の proxy → tailnet → Keycloak の Ingress の proxy を通る。issuer の文字列も証明書もブラウザと同じ。

| 案 | 採らなかった理由 |
|---|---|
| **`dnsPolicy: ClusterFirstWithHostNet` (採用)** | — 新しい Service・固定の IP・CA の配布が要らない。Pod の経路と 1 つにそろう |
| ノードの resolv.conf のまま (何もしない) | 今のホストでは届く (WSL2 が mirrored networking で、Windows の Tailscale の MagicDNS が `100.x` を返す)。ただし API server の認証がホストの Tailscale と WSL の設定に黙って依存する。ホストの Tailscale を止めると kubectl の OIDC が全部止まる |
| `issuer.discoveryURL` でクラスタ内の別の URL を指す | discovery の中の `jwks_uri` は issuer のホストのまま (Keycloak の `hostname` を 1 つにそろえたため) なので、名前の解決は結局要る。discoveryURL の先にも、同じ名前の証明書が要る |
| static Pod の `hostAliases` で egress の proxy の IP を書く | egress の Service は headless で、proxy の Pod の IP は作り直すと変わる。固定の ClusterIP の Service を足すと、置き場所と IP の管理が増える |

- CoreDNS の rewrite が無い (`just up` の `_coredns-tailnet` より前) と、OIDC のトークンだけが拒まれる。API server は起動し、admin の kubeconfig は使える。
  JWT の認証器は裏で初期化を繰り返すので、rewrite が入れば何もしなくてもつながる ([idp-options.md](idp-options.md) の「鶏と卵」)
- API server の名前の解決はほかに使っていない (etcd は `127.0.0.1`、webhook と aggregated API は Service の IP)。CoreDNS が落ちても OIDC 以外は動く [未確認: CoreDNS を止めて試していない]

### kind-config と、作り直さずに入れた今のノード

kind-config の control-plane は、ホストの `~/.local/share/home-k8s/kube-apiserver` をノードの `/etc/kubernetes/home-k8s` に見せ (`extraMounts`、読み取りだけ)、
kubeadm に次の 2 つを渡す (`kubeadmConfigPatches`)。

- `ClusterConfiguration`: `apiServer.extraArgs` に `authentication-config`、`extraVolumes` で上のディレクトリを API server の Pod に見せる
- `InitConfiguration`: `patches.directory` で `patches/kube-apiserver+strategic.yaml` (dnsPolicy) を当てる

ファイルはクラスタを作る前に要る (無いと API server が起動しない) ので、`_kind-up` が `kind create cluster` の前に repo からコピーする。毎回コピーし直すので、
**このディレクトリを見せているクラスタ**では、`just up` を打てば API server が新しい設定を読む。

今のクラスタ (2026-10-09 に作成) は kind-config に `extraMounts` が無いころに作ったので、作り直さずに次のように入れた。
ノードの中のパスと static Pod のマニフェストは、kind-config で作ったときと同じになる。違いは、ノードの `/etc/kubernetes/home-k8s` がホストのディレクトリではなくコピーであること。
**今のクラスタで `authentication-config.yaml` を変えたときは、下の 2 行目 (`docker cp`) を打ち直す** (作り直せば不要になる)。

```sh
N=study-kind-control-plane
docker exec $N mkdir -p /etc/kubernetes/home-k8s && docker cp clusters/kind/kube-apiserver/. $N:/etc/kubernetes/home-k8s/ && docker exec $N chown -R root:root /etc/kubernetes/home-k8s
# kind が渡した kubeadm の設定 (/kind/kubeadm.conf) に、kind-config の kubeadmConfigPatches と同じ 2 つを足したものを /root/kubeadm.conf.oidc に置き、
# kubeadm にマニフェストを作らせる (--dry-run なので書き換えない)。出力先は kubeadm が表示する
docker exec $N kubeadm init phase control-plane apiserver --config /root/kubeadm.conf.oidc --dry-run
# 元のマニフェストを控えてから (/root/kube-apiserver.yaml.orig-before-oidc)、作らせたマニフェストを同じファイルシステムの中から mv で置き換える (kubelet が途中の状態を読まない)
```

作らせたマニフェストと元のマニフェストの差は、`--authentication-config` の引数、`home-k8s` の volume と volumeMount、`dnsPolicy: ClusterFirstWithHostNet` の 4 つだけだった。
kubelet が API server を作り直し、約 40 秒で `/readyz` が `ok` に戻った。

**戻し方** (OIDC を外す。admin の kubeconfig はどちらの状態でも使える):

```sh
N=study-kind-control-plane
docker exec $N cp /root/kube-apiserver.yaml.orig-before-oidc /etc/kubernetes/tmp/kube-apiserver.yaml
docker exec $N mv /etc/kubernetes/tmp/kube-apiserver.yaml /etc/kubernetes/manifests/kube-apiserver.yaml
kubectl --context kind-study-kind get --raw /readyz   # 1 分ほどで ok
```

API server が起動しなくなったときも同じ手順で戻る (kubelet はマニフェストのファイルだけを見るので、API server が落ちていても効く)。
kind-config の形を確かめるために、同じ control-plane の設定で別の kind のクラスタを作ろうとしたが、ホストの inotify の上限 (`fs.inotify.max_user_instances` 128) でノードが起動せず、作れなかった。
kind が `kubeadmConfigPatches` を `/kind/kubeadm.conf` にどう重ねるか (`extraArgs` の配列を置き換えるか足すか) は [未確認]。
置き換えると `--runtime-config=` (値は空) が消えるが、空の値なので動きは変わらない。

## RBAC

| グループ (Keycloak) | Kubernetes の Group | ClusterRole | できること |
|---|---|---|---|
| `admins` | `oidc:admins` | `cluster-admin` | 何でもできる (admin の kubeconfig と同じ) |
| `viewers` | `oidc:viewers` | `view` と `headlamp-cluster-read` | 名前空間の中を読むだけ (Secret は読めない)。Node・PV・StorageClass・CRD・ClusterRole を読む |
| どちらでもない | (`system:authenticated` だけ) | (なし) | 自分の権限の問い合わせ (`auth whoami`・`auth can-i`) などの既定のものだけ |

`headlamp-cluster-read` は今までの Headlamp の ServiceAccount 用の ClusterRole ([clusters/kind/headlamp/manifests/rbac.yaml](../../clusters/kind/headlamp/manifests/rbac.yaml)) をそのまま使う。

## kubectl でログインする (kubelogin)

`just up` が `~/.kube/config` に context `oidc@study-kind` と user `oidc@study-kind` を足す (`kubectl config set-credentials`・`set-context`。
既存の `kind-study-kind` (admin) は変えず、current-context も変えない)。足すだけなら次でもよい。

```sh
just _kube-oidc-context
```

足される user は exec plugin で、`kubectl oidc-login get-token --oidc-issuer-url=https://keycloak.taild2b611.ts.net/realms/home-k8s --oidc-client-id=kubernetes --oidc-pkce-method=S256 --skip-open-browser` を呼ぶ。
client `kubernetes` は public (secret を持たない) で PKCE を要求する。kubelogin は `127.0.0.1:8000` で待ち、使えなければ `18000` で待つ (realm の redirect URI と同じ)。
kubelogin は開発用コンテナにだけ入っているので、kubectl も開発用コンテナで打つ。ホストの設定ファイル (~/.bashrc など) は触らない。

```sh
just devcontainer shell
kubectl --context oidc@study-kind auth whoami
# → "Please visit the following URL in your browser: http://localhost:8000/" と出る。Windows のブラウザで開き、Keycloak でログインする (パスワード + TOTP か passkey)
# → ATTRIBUTE / Username oidc:yamakura-yuma / Groups [oidc:admins system:authenticated]
kubectl --context oidc@study-kind auth can-i '*' '*' --all-namespaces   # admins なら yes
```

- 開発用コンテナはホストのネットワークで動く。WSL2 が mirrored networking (`~/.wslconfig` の `networkingMode=mirrored`) なので、
  Windows の `localhost:8000` が WSL の kubelogin に届く (Windows の `curl.exe` で callback を送って確かめた。ブラウザでは [未確認])。
  単位 2c で届かなかったときとの違いは調べていない [未確認]
- トークンは `~/.kube/cache/oidc-login/` にキャッシュされる (ホストと開発用コンテナで共有)。ID トークンの期限は realm の既定 (5 分) で、切れると kubelogin が refresh token で取り直す。
  ログインし直すときは `kubectl oidc-login clean`
- グループを変えたら、次にトークンを取り直したときから効く (API server はトークンの groups を毎回読む)

## Headlamp

`https://headlamp.taild2b611.ts.net` を開き、「Sign In」で Keycloak に入る。Headlamp は ID トークン (`aud: headlamp`) を cookie (`headlamp-auth-main.0`) に持ち、
API server にそのまま渡す。Headlamp の画面でできることは、上の RBAC のとおり (viewers は読むだけで、Secret の一覧と書き込みは 403)。

| 設定 (values.yaml) | 値 |
|---|---|
| `config.oidc.externalSecret` | Secret `headlamp-oidc` (キー `OIDC_CLIENT_SECRET`) を envFrom で読む。`secret.create: false` |
| `env` の `OIDC_CLIENT_ID` | `headlamp` |
| `env` の `OIDC_ISSUER_URL` | issuer。Headlamp の Pod は CoreDNS の rewrite で引く |
| `env` の `OIDC_CALLBACK_URL` | `https://headlamp.taild2b611.ts.net/oidc-callback` (realm の client `headlamp` の redirect URI) |
| `env` の `OIDC_USE_PKCE` | `true` |

- client の secret は Git に置かない。`just up` の `_oidc-secrets` が `~/.local/share/home-k8s/keycloak/clients/headlamp` から Secret を作る。secret を変えたら `just up` のあと `kubectl -n headlamp rollout restart deploy/headlamp`
- callback は tailnet の URL だけ。`http://localhost:4466` から OIDC でログインしても、戻り先は tailnet の URL になる
- **ServiceAccount `headlamp` のトークン (`just show headlamp`) は非常用に残した**。読むだけの権限で、Keycloak が落ちていても `localhost:4466` から使える [未確認: OIDC を設定した Headlamp の画面に、トークンを貼る入口が出るかはブラウザで見ていない]

## 確かめ方

```sh
# API server の設定
docker exec study-kind-control-plane grep -E 'authentication-config|dnsPolicy' /etc/kubernetes/manifests/kube-apiserver.yaml
kubectl --context kind-study-kind get --raw /readyz        # admin の kubeconfig (非常用) がそのまま使える
# ログイン (開発用コンテナで)
kubectl --context oidc@study-kind auth whoami              # oidc:<ユーザー名>、oidc:<グループ>
kubectl --context oidc@study-kind auth can-i get secrets -n auth   # admins は yes、viewers は no
```

## 確かめた結果

2026-10-11 (JST) に `kind-study-kind` で確かめた (クラスタは作り直していない)。ログインは使い捨てのテスト用ユーザー (`test-admin` は admins、`test-viewer` は viewers、
`test-nogroup` はグループなし。OTP の必須アクションを外して作り、確かめた後に消した) で、ブラウザを使わずに流した。
kubectl は本物の kubelogin を動かし、ブラウザの代わりに Python の標準ライブラリで `http://localhost:8000/` から Keycloak のログインフォームまでを流した。

| 確認 | test-admin | test-viewer | test-nogroup |
|---|---|---|---|
| `kubectl auth whoami` (kubelogin) | `oidc:test-admin`、`[oidc:admins system:authenticated]` | `oidc:test-viewer`、`[oidc:viewers system:authenticated]` | `oidc:test-nogroup`、`[system:authenticated]` |
| `can-i get pods -A`・`list nodes` | yes・yes | yes・yes | no・no |
| `can-i get secrets -A` | yes | no | no |
| `can-i create deployments`・`delete pods`・`patch applications` | yes | no | no |
| ConfigMap の作成 (server の dry run) | 作れる | 403 | 403 |
| Headlamp: ログイン | `/auth` に戻り、cookie に ID トークン (`aud headlamp`、`groups [admins]`) | 同じ (`groups [viewers]`) | 同じ (`groups` なし) |
| Headlamp: Pod・Node の一覧 | 200・200 | 200・200 | 403・403 |
| Headlamp: Secret の一覧 (auth)・ConfigMap の作成 (dry run) | 200・201 | 403・403 | 403・403 |

- API server の Pod の resolv.conf は `nameserver 10.96.0.10` (CoreDNS)。API server のログに OIDC の認証器の失敗は出ていない (`OIDC: No x509 certificates provided, will use host's root CA set` だけ)
- Windows の `curl.exe` で kubelogin の callback (`http://localhost:8000/?code=…`) を送り、WSL の開発用コンテナの kubelogin がトークンを受け取った (`whoami` が `oidc:test-viewer`)

## 人が戻ってから確かめること (yamakura-yuma、admins)

1. 開発用コンテナで `kubectl --context oidc@study-kind auth whoami` → 表示された `http://localhost:8000/` を **Windows のブラウザ**で開き、パスワード + TOTP (か passkey) でログイン
   → 端末に `oidc:yamakura-yuma` と `oidc:admins`。`kubectl --context oidc@study-kind get nodes` が通る
2. tailnet の端末のブラウザで <https://headlamp.taild2b611.ts.net> → Sign In → Keycloak → クラスタ `main` の画面が出る。Secrets の一覧が見え、Pod の削除などのボタンが押せる (押さなくてよい)
3. (任意) <http://localhost:4466> で、`just show headlamp` のトークンでも入れるか (非常用の入口)
