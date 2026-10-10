# Tailscale (UI を tailnet に出す)

kind の UI を、tailnet の端末から `https://<名前>.<tailnet>.ts.net` で開けるようにする。
[access-control.md](access-control.md) と [idp-options.md](idp-options.md) の「移行の段取り」の単位 1 にあたる。
この単位では認証を変えていない。ArgoCD と Grafana ×3 は後の単位 3 で Keycloak の SSO にした (tailnet の URL で使う。[keycloak.md](keycloak.md) の「各 UI の OIDC」)。ほかの UI のログインは今までどおり (`just show`)。

- tailnet: `<tailnet>` = **`taild2b611.ts.net`** (Personal プラン、2026-10-10 に作成)。例: ArgoCD は `https://argocd.taild2b611.ts.net`
- 入れたもの: Tailscale の Kubernetes operator (chart `tailscale-operator` 1.102.4、namespace `tailscale`。[apps/tailscale-operator.yaml](../../clusters/kind/argocd/apps/tailscale-operator.yaml))
  と、UI ごとの Ingress ([clusters/kind/tailscale/ingress](../../clusters/kind/tailscale/ingress)。Application `tailscale-ingress`)
- localhost の URL (`localhost:8080` など) はそのまま残る。tailnet の URL は追加の経路

## URL

| UI | URL | Ingress (namespace/名前) | つなぐ Service |
|---|---|---|---|
| ArgoCD | `https://argocd.<tailnet>.ts.net` | argocd/argocd | argocd-server:80 |
| Backstage | `https://backstage.<tailnet>.ts.net` | backstage/backstage | backstage:7007 |
| Grafana (観測スタック) | `https://grafana.<tailnet>.ts.net` | observability/grafana | grafana:80 |
| Grafana (dev) | `https://grafana-dev.<tailnet>.ts.net` | dev/grafana-dev | grafana:80 |
| Grafana (prod) | `https://grafana-prod.<tailnet>.ts.net` | prod/grafana-prod | grafana:80 |
| Temporal UI (dev) | `https://temporal-dev.<tailnet>.ts.net` | dev/temporal-dev | temporal-web:8080 |
| Temporal UI (prod) | `https://temporal-prod.<tailnet>.ts.net` | prod/temporal-prod | temporal-web:8080 |
| Headlamp | `https://headlamp.<tailnet>.ts.net` | headlamp/headlamp | headlamp:80 |
| Keycloak | `https://keycloak.<tailnet>.ts.net` | auth/keycloak | keycloak-service:8080 |

ホスト名は Ingress の `tls.hosts` の 1 つ目で決まる (`<名前>.<tailnet>.ts.net`)。
各ホストの最初のアクセスで Tailscale が Let's Encrypt から証明書を取るので、1 回目は数秒〜数十秒かかる。

## 構成と、その形にした理由

```
tailnet の端末 ─ HTTPS (Tailscale の証明書) ─▶ proxy Pod (tag:k8s、Ingress ごとに 1 つ) ─ HTTP ─▶ UI の Service
                                              ▲ 作る・消す
                         operator (namespace tailscale、tag:k8s-operator、OAuth client で tailnet に入る)
```

- **公開は Ingress (`ingressClassName: tailscale`)、LoadBalancer にしない**。
  Ingress は L7 で、proxy が HTTPS を終端し Tailscale の証明書を付ける。LoadBalancer (`loadBalancerClass: tailscale`) は L3 の転送で、
  証明書を付けない ([公式] Tailscale の Ingress の説明)。Keycloak の issuer は `https://` で、UI の OIDC も HTTPS を前提にするので Ingress にする。
- **Ingress ごとに proxy を 1 つ (既定)。ProxyGroup (HA) は使わない**。ProxyGroup は Tailscale Services と autoApprovers の設定が増える。
  自宅の 1 クラスタで、proxy の再起動中の数秒の断は受け入れる。proxy は Ingress の 9 つ (Keycloak を含む) と egress の 1 つで、Personal のタグ付きリソースの上限 50 に収まる
- **Ingress は UI の chart の外に置く** (`clusters/kind/tailscale/ingress`、各 manifest が namespace を書く)。UI の chart と values を変えずに足し引きでき、
  tailnet に何を出しているかが 1 か所で見える。Ingress は Service と同じ namespace に要るので、namespace は manifest ごとに書く
- **Temporal UI は iframe 用の proxy (`temporal-ui-embed`) ではなく chart の Web UI (`temporal-web`) につなぐ**。
  `temporal-ui-embed` は Backstage のタブの iframe のための proxy で、`frame-ancestors` は `http://localhost:7007` だけを許す ([temporal.md](temporal.md))
- **operator の API server proxy は使わない** (`apiServerProxyConfig.mode: "false"`)。kind の API server は単位 6 で Keycloak の OIDC につなぐ

### tag と ACL (最小の形)

| tag | 付く端末 | 付けられる人 (tagOwners) |
|---|---|---|
| `tag:k8s-operator` | operator 自身 (端末名 `tailscale-operator`) | `autogroup:admin` (tailnet の管理者) |
| `tag:k8s` | Ingress ごとの proxy (UI と Keycloak) と、egress の proxy (Pod → Keycloak) | `tag:k8s-operator` (operator が proxy に付ける) |

ACL は新しい tailnet の既定 (全部許可) のまま残し、`tagOwners` だけを足した。
自分 1 人の tailnet で、tailnet の外には出ていない (Funnel は使わない) ため。
人を招待するときは、既定の全部許可を次の形に絞る (未実施。Keycloak の後の別話題):

```json
"grants": [
  {"src": ["autogroup:member"], "dst": ["autogroup:self"], "ip": ["*"]},
  {"src": ["autogroup:member"], "dst": ["tag:k8s"], "ip": ["tcp:443"]}
]
```

絞るときは、egress の proxy (`tag:k8s`) から Keycloak の Ingress の proxy (`tag:k8s`) への 443 も許す。無いとクラスタの中の Pod から issuer に届かなくなる ([keycloak.md](keycloak.md)):
`{"src":["tag:k8s"],"dst":["tag:k8s"],"ip":["tcp:443"]}`


## Keycloak を出すとき (単位 2b、実装済み)

単位 2b でほかの UI と同じ形の Ingress を足した ([ingress/keycloak.yaml](../../clusters/kind/tailscale/ingress/keycloak.yaml)、namespace `auth`、
Service `keycloak-service:8080`)。`https://keycloak.<tailnet>.ts.net` で開ける。設定と理由は [keycloak.md](keycloak.md)。

- ホスト名は `keycloak` で固定する。変えると issuer (`https://keycloak.<tailnet>.ts.net/realms/home-k8s`) が変わり、passkey も登録し直しになる ([idp-options.md](idp-options.md))
- tag は proxy の既定の `tag:k8s` のまま。tailnet のポリシーは変えていない
- proxy (tailscale serve) は `X-Forwarded-For`・`X-Forwarded-Host`・`X-Forwarded-Proto` を付ける。Keycloak は `proxy.headers: xforwarded` と `hostname: https://keycloak.<tailnet>.ts.net`
- Pod からは operator の **egress** (Service `auth/keycloak-tailnet`、注釈 `tailscale.com/tailnet-fqdn`) と CoreDNS の rewrite で同じ URL に届く ([keycloak.md](keycloak.md) の「Pod から issuer に届かせる」)。
  kind の API server からの届かせ方は単位 6
- proxy の端末は ephemeral ではない。`just down` (`kind delete cluster`) は端末を tailnet に残すので、作り直したクラスタの proxy が同じ名前を取れないことがある [未確認] ([keycloak.md](keycloak.md) の「鶏と卵」)

## 人の画面操作 (最初の 1 回)

アカウントを作って operator を tailnet に入れるまでは、Tailscale の管理画面での操作が要る (2026-10-10 に実施)。

1. <https://login.tailscale.com/start> で Personal のアカウントを作り、手元の端末 (PC かスマホ) を 1 台つなぐ
2. 管理画面の **DNS** で MagicDNS が有効か確かめ (新しい tailnet は既定で有効)、**HTTPS Certificates** の Enable HTTPS を押す。同じ画面の Tailnet name が `<tailnet>`
3. 管理画面の **Access controls** で、既定のポリシーを残したまま `tagOwners` を足して保存する

   ```json
   "tagOwners": {
     "tag:k8s-operator": ["autogroup:admin"],
     "tag:k8s": ["tag:k8s-operator"]
   }
   ```

4. 管理画面の **Settings → Trust credentials** (以前の名前は OAuth clients) で **+ Credential**。
   scope は **General > Services**、**Devices > Core**、**Keys > Auth Keys** をそれぞれ Read と Write にし、tag に `tag:k8s-operator` を選ぶ。
   Generate credential で出る Client ID と Client secret を控える (secret は一度しか出ない)
5. WSL2 のホストで、Git の外のファイルに置く (本人だけが読める権限)。`tailnet=` の行は控え (Secret には入らない)

   ```sh
   mkdir -p ~/.config/home-k8s
   (umask 077; cat > ~/.config/home-k8s/tailscale-operator.env) <<'EOF'
   tailnet=<tailnet 名>
   client_id=<Client ID>
   client_secret=<Client secret>
   EOF
   ```

   `~/.local/share/home-k8s` は `just up` (開発用コンテナの root) が作るので、人が書けないことがある。人が置く資格情報は `~/.config/home-k8s` に置き、
   開発用コンテナはここもホストと共有する (`just devcontainer up`。この行を足す前に作ったコンテナは `just devcontainer down && just devcontainer up` で作り直す)

6. `just up` を打つ (打ち直しでよい)

`just up` は `just/tailscale-secrets.sh` で、このファイルの `client_id`・`client_secret` から Secret `operator-oauth` (namespace `tailscale`、同じ名前のキー) を作る。
ファイルが無い・項目が欠けると、クラスタを作った直後に止まる (operator が起動できず、最後に ArgoCD の同期を 20 分待って落ちるのを避ける)。
client を作り直したら、ファイルを書き換えて `just up` を打ち、operator の Pod を作り直す
(`kubectl -n tailscale delete pod -l app=operator`)。operator は起動時にしか Secret を読まない。

## 確かめ方

```sh
kubectl -n tailscale get pods                     # operator が Running
kubectl get ingress -A                            # 各 Ingress の ADDRESS に <名前>.<tailnet>.ts.net
kubectl -n tailscale get statefulsets             # Ingress ごとの proxy (ts-<名前>-xxxxx)
```

tailnet の別の端末 (Tailscale のアプリを入れてログインした端末) のブラウザで、上の表の URL を開く。tailnet の外からは名前が引けない。

## 制約と、この単位でしていないこと

- **Backstage の中のリンクと埋め込みは localhost を指したまま**。Temporal のタブ (iframe、`http://localhost:8233`)・Grafana と ArgoCD へのリンクは、
  ホストの外の端末からは開けない。Backstage の画面そのものは、フロントエンドが `app.baseUrl` と `backend.baseUrl` を開いたオリジンに置き換えるので
  (`@backstage/frontend-defaults` の overrideBaseUrlConfigs) ts.net でも動く
- **share (`just share`) は触っていない**。Tailscale に招待できない相手に見せる経路として残る ([share.md](share.md))
- Funnel (インターネットへの公開) は使わない
- 証明書は Let's Encrypt の上限 (tailnet あたり週 50 のホスト名) に数える。クラスタを作り直しても同じ名前なら新しい名前は増えない
  (同じ名前の重複は週 5 まで [公式])。作り直しを 1 週間に何度も繰り返すと証明書が取れなくなりうる
- Tailscale Personal は非商用に限る ([access-control.md](access-control.md))

## 出典

- Kubernetes operator (確認): <https://tailscale.com/kb/1236/kubernetes-operator>
- operator の quickstart (tagOwners、Trust credentials の scope、Secret `operator-oauth`) (確認): <https://tailscale.com/docs/kubernetes-operator/quickstart>
- Ingress (L7・証明書・上限、LoadBalancer は L3) (確認): <https://tailscale.com/kb/1439/kubernetes-operator-cluster-ingress>
- chart の values (`oauth` を空にすると Secret `operator-oauth` を読む) (確認): `helm show values tailscale/tailscale-operator --version 1.102.4`
