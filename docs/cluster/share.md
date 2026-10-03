# 公開 (just share) を常駐させ、人ごとの資格情報で配る — 設計

状態: 設計。実装は GitHub の Issue ごとに進め、そのたびにこの文書を実際の構成に書き換える。実装済み: #38 (headroom の中継)。
図の HTML (変更前→変更後のアニメーション): [share-design.html](share-design.html)

## 何を変えるか

今の `just share` は、打つたびにホストで caddy と cloudflared を起動し、使い捨てのパスワードを 1 つ作って
Ctrl-C まで公開する (対象ごとに 1 回、フォアグラウンド)。これを次の形に置き換える。

| | 変更前 | 変更後 |
|---|---|---|
| 公開の本体 | ホストのプロセス (`just share <対象>` を打つ間だけ) | クラスタ内の Deployment `share` (`just up` で立ち、クラスタを落とすと止まる) |
| 資格情報 | 起動のたびに使い捨て 1 つ。Grafana は viewer のパスワードを作り直す | 人ごとの名前付きユーザー・パスワード。期限付き (既定 8h) と、期限なしの特権 viewer 1 つ |
| 操作 | `just share [grafana\|headroom\|backstage]` | `just share add\|delete\|list\|get\|rotate\|prune` |
| URL | 起動のたびに変わる | クラスタ (正確には `share` Pod) が生きている間は同じ。`get` / `list` で引く |
| 失効 | Ctrl-C | `delete` は数秒、期限切れは期限どおり (認証が要求のたびに判定する) |

```text
変更前                                         変更後
  ホスト                                         ホスト
  ├ cloudflared ─▶ caddy ─▶ localhost:3000        └ headroom 中継 (caddy、docker bridge 側、GET 許可リストだけ)
  ├ cloudflared ─▶ caddy ─▶ localhost:8787              ▲
  └ cloudflared ─▶ caddy ─▶ localhost:7007        kind クラスタ │
  (just share を打つ間だけ。3 回打つ)             └ Deployment share (namespace share)
                                                      ├ cloudflared ×3 ─▶ caddy ─┬▶ grafana.observability.svc
                                                      ├ auth (forward_auth)      ├▶ backstage.backstage.svc
                                                      │   └ Secret share-credentials を要求ごとに参照  └▶ 中継 ─▶ headroom (loopback)
```

## 決めたこと

### 1. 届き方 — headroom だけが外にある

| 対象 | 実体 | クラスタ内 Pod からの到達 |
|---|---|---|
| grafana | NodePort 30300 の Service `grafana` (ns `observability`) | `grafana.observability.svc:80` で届く。ホストの `localhost:3000` は kind の extraPortMappings の入口にすぎない |
| backstage | NodePort 30707 の Service `backstage` (ns `backstage`) | `backstage.backstage.svc:7007` で届く (同上) |
| headroom | ホストの `127.0.0.1:8787` (systemd `headroom-default.service`)。`/v1/messages` などのプロキシ本体と同居 | **届かない** (loopback 限定)。合意書 v2 の A 案で中継を置く |

headroom の中継 (ホスト側。#38 で実装済み):

- 中身は caddy (`caddy:2.11.6-alpine`、タグ固定)。`docker run -d --name home-k8s-share-relay --network host --restart unless-stopped`
  の専用コンテナで、`just up` の `_share-relay-up` が kind の bridge ができた後に起動し、`just down` の `_share-relay-down` が消す。
  systemd の unit は増やさない (開発用コンテナも `--network host` で同じ作りにしてある)。
  実体は `just/share-relay.sh` (レシピは `just/share.just`)、設定は `just/share-relay.Caddyfile`。
- 待ち受けは kind の bridge の IPv4 ゲートウェイだけ (`docker network inspect kind` から引く。既定は `172.18.0.1:8788`)。
  headroom 本体 (`127.0.0.1:8787`、systemd) は loopback のまま変えない。
- **共有トークンのヘッダ (`X-Share-Relay-Token`) が無い・違う要求は、経路に関わらず 401** (デフォルト拒否)。トークンが空のときは起動しない
  (スクリプトと Caddyfile の両方で止める)。
- トークンがあっても通すのは今と同じ **GET・HEAD の許可リスト** (`/dashboard /health /stats /stats-history /stats-lifetime /transformations/feed /favicon.ico`)
  だけで、残りは 404 (`/v1/messages`・`/stats/reset`・`/settings`・`/cache/clear` ほか)。bridge 上の他のコンテナが見られるのも、
  トークンを知っていればこの読み取りだけ。upstream へはトークンのヘッダを渡さず、`Host` は upstream のものに書き換える。
- クラスタ側 (Pod の caddy) も同じ許可リストをもう一度掛ける (二重。#40)。
- トークンは `just up` が `openssl rand -hex 16` で作り、`~/.local/share/home-k8s/share/relay-token` (権限 600) に置く。
  あれば再利用するので、`just up` を打ち直しても値は変わらない。`just down` はコンテナだけを消し、トークンのファイルは残す。
  Caddyfile は同じ場所に `relay.Caddyfile` としてコピーしてからマウントする (worktree を消しても `--restart` で読めるように)。
  トークンは `docker run` の引数には出さず (`ps` に残る)、環境変数で渡す。
- Pod への宛先とトークンは Secret `share/share-host` (namespace `share` は無ければ `just up` が作る) に入る。
  キーは `SHARE_RELAY_ADDR` (`<ゲートウェイ>:<ポート>`、例 `172.18.0.1:8788`) と `SHARE_RELAY_TOKEN`。
  #41 の Deployment が `envFrom` でそのまま環境変数にする。git には置かない (bridge のアドレスは環境で変わりうる)。
- `HOME_K8S_KUBE_CONTEXT` で別の kind クラスタに向けても、中継はホストに 1 つ (`home-k8s-share-relay`、8788) で、`up` が作り直し `down` が消す。
  中継は bridge のゲートウェイで待ち受けるので、同じ bridge の他のクラスタの Pod からも届く。受け入れる制約とする。
- 試験 (`just ci` の段 6、`just/test_share_relay.py`): caddy を 127.0.0.1 の空きポートで起動し、偽の upstream に対して
  トークン無し・違うトークンは全経路 401、トークンありは許可リストが GET・HEAD で 200 (upstream にトークンが渡らない)、
  `POST /v1/messages`・`/stats/reset` ほか許可リスト外は 404 で upstream に届かないことを確かめる。
  スクリプトは偽の `docker`・`kubectl`・`curl` で、IPv4 ゲートウェイの選択・トークンの再利用・Secret のキー・空トークンの拒否を確かめる。
  稼働中のクラスタ・ホストには触れない。Pod から中継への到達は稼働中でしか確かめられないので CI に入れず、下の「残る問題」に書く。

### 2. 期限切れ・削除の強制 — 認証を前段に置き、要求ごとに判定する

caddy の設定を書き換えて reload する方式 (basic_auth のハッシュ一覧を作り直す) は採らない。
basic_auth に期限の概念が無く、Secret のボリュームは kubelet の同期で 1〜2 分遅れ、設定を変えるたびに
reload の取りこぼしを心配することになるためである。代わりに caddy の `forward_auth` で、小さな認証サービスに毎回聞く。

```text
ブラウザ ─▶ cloudflared ─▶ caddy ──(1) forward_auth──▶ auth (127.0.0.1:9000)
                             │                             └ Secret share-credentials を K8s API で読む (キャッシュ 2 秒)
                             │◀── 200 / 401 ───────────────────┘
                             └──(2) 200 のときだけ 経路の許可リストを通して upstream へ
```

| 項目 | 内容 |
|---|---|
| 認証サービス | 標準ライブラリだけの Python (約 100 行)。ConfigMap に置き `python:3.13-alpine` で動かす。判定は純関数 `decide(credentials, request, now)` |
| 資格情報の置き場 | Secret `share-credentials`。キーが名前、値が JSON `{"hash": "<sha256(パスワード)>", "expires_at": <epoch秒\|null>, "privileged": <bool>, "created_at": <epoch秒>}` |
| パスワード | `openssl rand -hex 16` (128 bit) を `add` が 1 回だけ表示。保存はハッシュだけ。推測できない値なので遅い KDF は要らず、sha256 でよい |
| 判定 | Basic の名前で Secret を引く → ハッシュを `compare_digest` → `expires_at` が null か今より後。1 つでも欠ければ 401 |
| 反映の遅れ | **期限切れ: 0 秒** (要求ごとに現在時刻と比べる)。**delete: 最大 2 秒** (キャッシュの TTL)。**ローテーション: 同じ 2 秒** |
| デフォルト拒否 | Secret が無い・K8s API に届かない・JSON が壊れている・名前が無い、はすべて 401 (閉じる側に倒す)。認証サービスが落ちていても caddy は 401/502 で通さない |
| 掃除 | 期限切れの項目は `list` では「期限切れ」と出し、`add` と `prune` が消す (CronJob は置かない)。残っていても判定は拒否なので、掃除は見た目の整理にすぎない |
| RBAC | auth の ServiceAccount は Secret `share-credentials` と `share-session-key` の `get` だけ (resourceNames で限る) |

### 3. 1 つの資格情報で 3 対象 — トンネル 3 本、資格情報 1 つ

- トンネルは **3 本** (cloudflared ×3、1 つの Pod の別コンテナ)。1 本でパス分けにすると Grafana の `root_url`・
  `serve_from_sub_path` と Backstage の `app.baseUrl` をサブパスにする必要があり、`localhost:3000`・`7007` での
  今の使い方が壊れるため。Quick Tunnel は 1 プロセスに 1 ホスト名なので、3 本が現実解になる。
- 3 本とも同じ caddy の別ポート (`:8081 grafana` `:8082 headroom` `:8083 backstage`) に向け、**同じ認証サービス**を通る。
  人は名前・パスワードを 3 つの URL でそれぞれ打つ (ブラウザの Basic 認証はオリジンごと)。
- URL は cloudflared のログ (`https://*.trycloudflare.com`) から `kubectl logs` で引く。
  `/quicktunnel` (cloudflared のメトリクス) が使えるなら置き換えてよいが、distroless で exec できないので既定はログ。
- `share` Pod の再起動でも URL は変わる (クラスタの作り直しだけではない)。`get` / `list` は毎回その場で引き、
  Deployment は `replicas: 1`・`Recreate` にして、不要な再起動を作らない。
- `add` / `delete` は **Pod を再起動しない** (資格情報は Secret にあり、認証サービスが読み直すだけ)。このため URL は変わらない。

### 4. Grafana の viewer との関係 — viewer は内部の鍵にして、人には渡さない

今は Grafana だけ caddy の認証がなく、Grafana 自身のログイン (ユーザー `viewer`) が門になっている。
このままだと 1 つの資格情報で 3 対象を見せられないので、**門を caddy の認証に一本化し**、Grafana へは caddy が
`Authorization: Basic viewer:<viewer のパスワード>` を付けて渡す。

- viewer のパスワードは Secret `grafana-viewer` (今ある。`just up` が作り、サイドカーが Grafana に反映する) をそのまま使う。
  caddy の Pod が環境変数で読み、人の目には出ない。**共有のたびの作り直し (`grafana-viewer-rotate.sh`) は廃止**する。
  失効は caddy の側で起きるので、viewer のパスワードは変える必要がない。
- 人ごとの Grafana ユーザーは作らない (平文のパスワードを保存せずに済む。ユーザーの作成・削除の失敗で食い違うこともない)。
- Grafana の閲覧者は全員 `viewer` として入る (今も同じ)。見られる範囲は他の対象と同じく viewer の権限で、admin は出ない。
- 人が viewer のパスワードを変えて締め出さないよう、`/profile/password`・`/api/user/password` は 404 にする。
- 確認が要る点: Grafana が **ページの要求にも** Basic を受けること (API は今 Backstage が Basic で叩いている)。
  実装の Issue #40 の最初に確かめ、通らなければ「人ごとの Grafana ユーザーを API で作る」に切り替える (その場合は平文も Secret に持つ)。

### 5. 既存の経路の絞り込みを保つ

認証を通った要求にだけ許可リストを掛け、認証が通らなければ経路を問わず 401 を返す (経路の存在を探られない)。

| 対象 | 通す | 通さない (404) |
|---|---|---|
| grafana | すべて (viewer の権限の範囲)。`Location: http://localhost:3000/` は `/` に書き換える | `/profile/password` `/api/user/password` |
| headroom | GET・HEAD の `/dashboard /health /stats /stats-history /stats-lifetime /transformations/feed /favicon.ico` | それ以外 (`/v1/*`、`POST /settings`、`/stats/reset`、`/cache/clear` ほか) |
| backstage | GET・HEAD の `/api/proxy` 以外すべて。POST は `/api/auth/*` と `/api/catalog/entities/by-refs` だけ | `/api/proxy*` (Grafana の API を Backstage の資格情報で読ませない)、`POST /api/catalog/locations` ほか |

現在の `just/*.Caddyfile` の経路・書き換え (Backstage の `Set-Cookie` の `Domain=localhost` 外し) は、この ConfigMap の Caddyfile に移す。

Backstage の API 呼び出しは `Authorization: Bearer` を使うので、Basic は最初のページの読み込みでしか使えない。
今は通過後に共有ごとの合言葉の cookie を渡している。変更後は **署名つき cookie** にする
(`<名前>.<署名>`。鍵は Secret `share-session-key`。`just up` が作る)。認証サービスは cookie でも Basic でも、
**名前が Secret にあり期限内か**を毎回確かめるので、delete・期限切れが cookie 経路にも効く。
認証サービスの `Set-Cookie` を caddy が応答に載せる実装 (`forward_auth` の `handle_response`) は、実装で確かめる。

### 6. 特権 viewer

- `add <名前> --permanent` で作る。全体で 1 つだけで、2 つ目は CLI が断る (作る前に `privileged: true` を数える)。
- 期限がない (`expires_at: null`) 以外は他と同じ。経路も権限も増やさない。
- `rotate <名前>` は特権 viewer だけに使え、パスワードを作り直す (古い値は最大 2 秒で効かなくなる)。
  期限付きの人は `delete` → `add` で足りる。`delete` は特権 viewer にも使える。

## CLI

`justfile` には `share *args` の 1 本を置き、`just/share.sh` が振り分ける (今の公開レシピを 7 個にまとめた流儀に合わせる)。
ツールは開発用コンテナにある `kubectl` だけを使う。

| コマンド | 動き |
|---|---|
| `just share add <名前> [--ttl <期間>] [--permanent]` | 名前は `^[a-z0-9][a-z0-9-]{0,31}$`。`--ttl` は `30m`・`8h` の形で、既定 `8h`・**上限 24h** (超えたら拒否。`--permanent` と同時指定も拒否)。作って **名前・パスワード・3 つの URL・期限**を表示する。URL が外から引けるまで (DoH で名前解決を確かめて) 待つ |
| `just share delete <名前>` | Secret の項目を消す。最大 2 秒で失効 |
| `just share list` | 名前・種別 (期限付き/特権)・残り時間・期限切れかどうか、と 3 つの URL |
| `just share get <名前>` | その人の情報と 3 つの URL (パスワードは保存していないので出ない。忘れたら `delete` → `add`、特権は `rotate`) |
| `just share rotate <名前>` | 特権 viewer のパスワードを作り直して表示 |
| `just share prune` | 期限切れの項目を消す |

書き込みは項目ごとの `kubectl patch secret --type merge` で、同時に 2 つの `add` が来ても互いを消さない。
特権の 1 つ制限は「数えてから作る」ので、2 人が同時に打つと競合しうる (単独運用なので受け入れる)。

## manifest・ArgoCD

- `clusters/kind/share/` に Deployment・ConfigMap (Caddyfile と認証サービス)・ServiceAccount・Role・RoleBinding を置き、
  `clusters/kind/argocd/apps/share.yaml` (namespace `share`) で ArgoCD に同期させる。`just ci` はこの Application が指す
  path も描画して kubeconform・kube-linter に掛けるので、登録しないと静的チェックの対象外になる。
- Deployment `share` は 1 Pod・5 コンテナ (caddy、auth、cloudflared ×3)。**Service は作らない** (入口は Cloudflare からの外向き接続だけ。
  クラスタ内に待ち受けを開けない)。イメージはタグを固定する。`replicas: 1`・`strategy: Recreate`。
- Secret (`share-credentials`、`share-session-key`、`share-host`) は **git に置かない** (ArgoCD の selfHeal が中身を戻さないため)。
  `just up` の `_share-secrets` が無ければ作る (`just/grafana-secrets.sh` と同じ流儀。`share-credentials` は空で作る)。
- 起動順: `share` Pod は Secret が無いと起動できない (`optional: false`) ので、`just up` は ArgoCD の同期より前に作る。

## 試験 (just ci に入れるもの・入れないもの)

`just/ci.sh` はクラスタを立てず、ネットワークにも出ない (ツールは `nix develop .#ci`)。その範囲で次を入れる。
実装は #39・#40・#41 で、どれも `just ci` の 1 段として走る。

| 段 | 内容 | 拒否を確かめる点 |
|---|---|---|
| 認証の単体 (Python `unittest`) | `decide()` に資格情報と時刻を渡す。時刻は引数なので時間を進められる | 名前なし・誤パスワード・**期限の 1 秒前は通り 1 秒後は拒否**・**項目を消すと拒否**・`rotate` 後の古いパスワード・改ざんした cookie・Secret が空・資格情報が壊れている・上限 (24h) 超の `--ttl` |
| 経路の統合 (caddy + auth + 偽の upstream) | `caddy` を空きポートで起動し、認証サービスは K8s API の代わりに JSON ファイルを読むモード (`AUTH_SOURCE=file:...`)。upstream は標準ライブラリの HTTP サーバー | 認証なしは全経路 401。認証ありでも headroom の `POST /v1/messages`・`/stats/reset`、backstage の `/api/proxy`・`POST /api/catalog/locations`、grafana の `/profile/password` は 404。**ファイルから項目を消す・期限を過去にする → キャッシュ TTL (テストでは 0) の後 401** |
| 静的 | `caddy validate`。`share` namespace に NodePort・LoadBalancer・Ingress が無いこと (`yq`)。既存の kubeconform・kube-linter | 公開の入口を足していない |

`flake.nix` の `devShells.ci` に `caddy` を足す (今は `tools` 側にしかない)。

クラスタを立てて通す確認 (`just share smoke`、CI には入れない): 期限が 5 秒の資格情報を Secret に直接書き、
`kubectl port-forward` で caddy に当てて、200 → 期限後 401 → delete 後 401 を見る。トンネルは使わない。
外への公開を伴う確認を CI に入れない理由は、ネットワークと稼働中のクラスタに依存して不安定になるためである。

## 実装の分け方 (Issue)

| # | 内容 | 前提 |
|---|---|---|
| #38 ✅ | headroom 中継 (ホスト側の caddy コンテナ、`just up`・`just down` への組み込み、`share-host` Secret) | なし |
| #39 | 認証サービスと資格情報のモデル (Python、単体試験、`just ci` に組み込み、`caddy` を `devShells.ci` に追加) | なし |
| #40 | Caddyfile の経路 (許可リスト・Grafana の viewer 付与・Backstage の署名 cookie) と、経路の統合試験 | #39 |
| #41 | クラスタ内 Deployment `share` と ArgoCD Application、`_share-secrets`、URL の取得 | #38・#40 |
| #42 | `just share add\|delete\|list\|get\|rotate\|prune` | #39・#41 |
| #43 | 稼働中のクラスタでの確認 `just share smoke` | #42 |
| #44 | 旧 `just share` の廃止、docs・README の書き換え、CODEOWNERS | #42・#43 |

## 受け入れる制約・残る問題

- URL は `share` Pod の再起動でも変わる。配った URL が使えなくなるので、再起動の原因を作らない (ログのローテーションや probe の設定に注意)。
- headroom の中継はホストの常駐物で、クラスタと寿命が別になる (合意済み)。トークンで bridge 上の他のコンテナからは守る。
- Pod (kind のノードの外向き NAT) から `<ゲートウェイ>:8788` に届くかは、WSL2 の iptables 次第で、稼働中のクラスタでしか確かめられない。#38 の実装では確かめていない (`just up` 後に Pod から `wget http://172.18.0.1:8788/` が 401 を返すこと。届かなければ原因をここに書く)。
- Quick Tunnel は稼働の保証がない。クラスタの外向きの UDP 7844 (QUIC) が通らないときは `--protocol http2` にする。
- 特権 viewer のパスワードは保存しないので、忘れたら `rotate` する。`get` で再表示したいなら、平文を Secret に持つ方式へ変える判断が要る。
- Grafana への Basic の付与が通らなかったときの切り替え先 (人ごとの Grafana ユーザー) は、実装の Issue #40 で判断する。
- `stage C paths` の対象 (dotfiles の再利用 workflow) に `clusters/kind/share/`・`just/share*` を足すのは dotfiles 側の変更で、この repo の外。
