# 公開 (just share) を常駐させ、人ごとの資格情報で配る — 設計

状態: 設計。実装は GitHub の Issue ごとに進め、そのたびにこの文書を実際の構成に書き換える。実装済み: #38 (headroom の中継)、認証サービス (#39、`clusters/kind/share/share_auth.py`)、Caddyfile の経路と統合試験 (#40、`clusters/kind/share/Caddyfile`・`test_share_caddy.py`)、クラスタ内の Deployment `share`・ArgoCD Application・Secret・URL を引く関数 (#41、稼働中のクラスタでは未確認で、「受け入れる制約・残る問題」に書いた。`clusters/kind/share/{deployment,rbac,kustomization}.yaml`・`just/share-secrets.sh`・`just/share-urls.sh`)、`just share` の CLI (#42、`just/share.sh`・`just/test_share_cli.py`。稼働中のクラスタでは未確認で、確認は #43 に引き継ぐ。あわせて #56: `grafana-secrets.sh` の Backstage の Basic を引数に出さない)。
図の HTML (変更前→変更後のアニメーション): [share-design.html](share-design.html)
認証サービス (#39) の diff の図 (判定を動かせるアニメーション): [share-auth.html](share-auth.html)
Caddyfile の経路 (#40) の diff の図 (要求を流して通る・通らないを見るアニメーション): [share-routes.html](share-routes.html)
クラスタ内の Deployment (#41) の diff の図 (Pod の中身と `just up` の順序を動かすアニメーション): [share-deploy.html](share-deploy.html)
CLI (#42) の diff の図 (add・delete・期限切れの流れと、拒否される指定を動かすアニメーション): [share-cli.html](share-cli.html)

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
- トークンがあっても通すのは **GET の許可リスト** (`/dashboard /health /stats /stats-history /stats-lifetime /transformations/feed /favicon.ico`)
  だけで、残りは 404 (`/v1/messages`・`/stats/reset`・`/settings`・`/cache/clear` ほか)。bridge 上の他のコンテナが見られるのも、
  トークンを知っていればこの読み取りだけ。upstream へはトークンのヘッダを渡さず、`Host` は upstream のものに書き換える。
  **HEAD は通さない (#48)**: 実機の headroom は許可リストの経路の HEAD に 404 を返し (`server: uvicorn`)、中継越しの HEAD 応答には
  `Cf-Ray` が付いていた。headroom が経路に無い HEAD を Anthropic 側へ素通ししているとみられ、「GET の読み取りだけを通す」趣旨からずれる。
  HEAD を使う用途は無い (Pod の probe は `exec`) ので、許可リストから外して GET に絞る。HEAD→GET への書き換えは、HEAD の応答に本文を
  返しうるので採らない。
- クラスタ側 (Pod の caddy、`clusters/kind/share/Caddyfile` の `:8082`) も同じ許可リストをもう一度掛ける (二重。#40)。
- トークンは `just up` が `openssl rand -hex 16` で作り、`~/.local/share/home-k8s/share/relay-token` (権限 600) に置く。
  あれば再利用するので、`just up` を打ち直しても値は変わらない。`just down` はコンテナだけを消し、トークンのファイルは残す。
  Caddyfile は同じ場所に `relay.Caddyfile` としてコピーしてからマウントする (worktree を消しても `--restart` で読めるように)。
  トークンは `docker run` の引数にも `kubectl create secret` の引数にも出さず (`ps` に残る)、環境変数と `--from-file` で渡す。
  Secret は `kubectl apply` ではなく replace・create で入れる (apply は値を `kubectl.kubernetes.io/last-applied-configuration` の注釈に残す。#49。下の「Secret の作り方」)。
- Pod への宛先とトークンは Secret `share/share-host` (namespace `share` は無ければ `just up` が作る) に入る。
  キーは `SHARE_RELAY_ADDR` (`<ゲートウェイ>:<ポート>`、例 `172.18.0.1:8788`) と `SHARE_RELAY_TOKEN`。
  Deployment `share` (#41) の caddy が `envFrom` でそのまま環境変数にする。git には置かない (bridge のアドレスは環境で変わりうる)。
- `HOME_K8S_KUBE_CONTEXT` で別の kind クラスタに向けても、中継はホストに 1 つ (`home-k8s-share-relay`、8788) で、`up` が作り直し `down` が消す。
  中継は bridge のゲートウェイで待ち受けるので、同じ bridge の他のクラスタの Pod からも届く。受け入れる制約とする。
- 試験 (`just ci` の段 6、`just/test_share_relay.py`): caddy を 127.0.0.1 の空きポートで起動し、偽の upstream に対して
  トークン無し・違うトークンは全経路 401、トークンありは許可リストが GET で 200 (upstream にトークンが渡らない)、
  `POST /v1/messages`・`/stats/reset` ほか許可リスト外と、許可リストの経路への HEAD は 404 で upstream に届かないことを確かめる。
  スクリプトは偽の `docker`・`kubectl`・`curl` で、IPv4 ゲートウェイの選択・トークンの再利用・Secret のキー・空トークンの拒否を確かめる。
  稼働中のクラスタ・ホストには触れない。試験の caddy は `devShells.ci` の版 (nixpkgs)、コンテナは `caddy:2.11.6-alpine` で、パッチの版が違いうる。
  Pod から中継への到達は稼働中でしか確かめられないので CI に入れず、下の「残る問題」に書く。

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
| 認証サービス | 標準ライブラリだけの Python (`clusters/kind/share/share_auth.py`)。ConfigMap `share-auth` に置き `python:3.13.16-alpine` で動かす (#41。`python -B /app/share_auth.py`)。判定は純関数 `decide(credentials, request, now, session_key, verifier)` で、`(status, headers)` を返す。`request` は `Authorization` と `Cookie` のヘッダ値だけ |
| 資格情報の置き場 | Secret `share-credentials`。キーが名前、値が JSON `{"hash": "pbkdf2_sha256$<反復回数>$<salt の hex 32 桁>$<鍵の hex 64 桁>", "expires_at": <epoch秒\|null>, "privileged": <bool>, "created_at": <epoch秒>}`。型を 1 つでも外した項目 (`expires_at: true` を含む) はその項目だけ拒否し、ほかの項目には響かない。cookie の署名鍵は Secret `share-session-key` のキー `key`。反復回数が 100,000 未満の項目は拒否する |
| パスワード | `openssl rand -hex 16` (128 bit) を `add` が 1 回だけ表示。保存はハッシュだけで、**PBKDF2-HMAC-SHA256 (項目ごとのランダムな 16 byte のソルト、反復 600,000 回)**。反復回数は [OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html) の PBKDF2-HMAC-SHA256 の推奨値。反復回数は項目に書くので、将来上げても古い項目は読める。1 回の計算は開発機で約 70ms (Python の `hashlib.pbkdf2_hmac`)。sha256 1 回の保存は CodeQL (`py/weak-sensitive-data-hashing`) に指摘されたので、遅い KDF にした |
| 判定 | Basic の名前 (`^[a-z0-9][a-z0-9-]{0,31}$`) で Secret を引く → `expires_at` が null か今より後 (**期限ちょうども拒否**) → パスワードを PBKDF2 で照合 (`compare_digest`)。1 つでも欠ければ 401。名前が無いときもダミーの hash で同じ反復の PBKDF2 を計算する。`Authorization` が Basic ならそれだけで決め、Basic でなければ (Backstage の Bearer など) cookie で決める |
| 照合結果の覚え | PBKDF2 は 1 回約 70ms で、ページを開くと要求が連発するため、**照合に成功した結果だけ**を名前ごとに 1 つ、プロセス内に **30 秒** (環境変数 `AUTH_VERIFY_TTL`、0 で無効) 覚える。覚えるのは (hash, パスワード, 時刻) で、パスワードは最大 30 秒だけメモリに平文で残る (キーをパスワードのハッシュにすると、同じ指摘を招き、弱いハッシュを足すことにもなる)。使う条件は 4 つ全部: 項目が今も Secret にあり期限内 (毎回先に確かめる) / hash が覚えた値と同じ (ローテーション・`delete` → `add` で変わる) / パスワードが覚えた値と `compare_digest` で一致 / 30 秒以内。失敗は覚えず毎回計算するので、誤パスワードを連打されると 1 要求あたり約 70ms の CPU を使う (単独運用なので受け入れる)。**Basic で通った後は cookie が使われる**ので、覚えが効くのは主にブラウザ以外の Basic の連続呼び出し |
| 反映の遅れ | **期限切れ: 0 秒** (要求ごとに現在時刻と比べる)。**delete: 最大 2 秒** (キャッシュの TTL、環境変数 `AUTH_CACHE_TTL`)。**ローテーション: 同じ 2 秒** (照合結果の覚えは、これらを遅らせない) |
| 設定 | `AUTH_SOURCE` (既定は K8s API。`file:<path>` で JSON ファイル。形は `{"credentials": {名前: 項目}, "session_key": "..."}`、項目は JSON 文字列でも dict でもよい)、`AUTH_CACHE_TTL` (既定 2)、`AUTH_VERIFY_TTL` (既定 30)、`AUTH_LISTEN` (既定 `127.0.0.1:9000`)、`AUTH_NAMESPACE` (既定は ServiceAccount の namespace) |
| 応答 | 200 か 401 だけ (本文なし)。どのメソッド・パスでも同じ判定。401 には `WWW-Authenticate: Basic` を付ける。Basic で通ったときだけ `Set-Cookie` (§5) を返す |
| デフォルト拒否 | Secret が無い・K8s API に届かない・JSON が壊れている・名前が無い、はすべて 401 (閉じる側に倒す)。読めなかったときに古い値は使い回さない。認証サービスが落ちていても caddy は 401/502 で通さない。認証なしで 200 を返す経路 (health など) は持たない (Pod の probe は tcpSocket にする) |
| 掃除 | 期限切れの項目は `list` では「期限切れ」と出し、`add` と `prune` が消す (CronJob は置かない)。残っていても判定は拒否なので、掃除は見た目の整理にすぎない |
| RBAC | auth の ServiceAccount は Secret `share-credentials` と `share-session-key` の `get` だけ (resourceNames で限る) |

### 3. 1 つの資格情報で 3 対象 — トンネル 3 本、資格情報 1 つ

- トンネルは **3 本** (cloudflared ×3、1 つの Pod の別コンテナ)。1 本でパス分けにすると Grafana の `root_url`・
  `serve_from_sub_path` と Backstage の `app.baseUrl` をサブパスにする必要があり、`localhost:3000`・`7007` での
  今の使い方が壊れるため。Quick Tunnel は 1 プロセスに 1 ホスト名なので、3 本が現実解になる。
- 3 本とも同じ caddy の別ポート (`:8081 grafana` `:8082 headroom` `:8083 backstage`) に向け、**同じ認証サービス**を通る。
  人は名前・パスワードを 3 つの URL でそれぞれ打つ (ブラウザの Basic 認証はオリジンごと)。
- URL は cloudflared のログ (`https://*.trycloudflare.com`) から `kubectl logs -c cloudflared-<対象>` で引く。`just/share-urls.sh` の `share_url`・`share_urls` が引く関数で、
  CLI (#42) が source して使う。`api.trycloudflare.com` (cloudflared が登録に使う API の宛先で、失敗のログに出る) は URL に数えない。
  `/quicktunnel` (cloudflared のメトリクス) が使えるなら置き換えてよいが、distroless で exec できないので既定はログ。
- `share` Pod の再起動でも URL は変わる (クラスタの作り直しだけではない)。`get` / `list` は毎回その場で引き、
  Deployment は `replicas: 1`・`Recreate` にして、不要な再起動を作らない。cloudflared には probe を付けず、caddy と auth には liveness を付けない (probe の失敗で再起動しない)。
- **設定を変えて ArgoCD が Pod を作り直したときも URL は変わる**。Caddyfile と認証サービスの ConfigMap は名前にハッシュが付き (kustomize の `configMapGenerator`)、
  中身が変わると Deployment の参照が変わって Pod が入れ替わる。固定名にすると、caddy も python も起動時にしか読まないので、Caddyfile の変更 (許可リストの修正) が Pod の再起動まで効かない。
  配った URL より、許可リストの修正が確実に効くことを優先した。
- `add` / `delete` は **Pod を再起動しない** (資格情報は Secret にあり、認証サービスが読み直すだけ)。このため URL は変わらない。

### 4. Grafana の viewer との関係 — viewer は内部の鍵にして、人には渡さない

今は Grafana だけ caddy の認証がなく、Grafana 自身のログイン (ユーザー `viewer`) が門になっている。
このままだと 1 つの資格情報で 3 対象を見せられないので、**門を caddy の認証に一本化し**、Grafana へは caddy が
`Authorization: Basic viewer:<viewer のパスワード>` を付けて渡す。

- viewer のパスワードは Secret `grafana-viewer` (今ある。`just up` が作り、サイドカーが Grafana に反映する) と同じ値を使う。
  `grafana-viewer` は namespace `observability` にあり、Pod は別 namespace の Secret を参照できないので、`just up` の `_share-secrets` が同じパスワードのファイルから
  Secret `share/share-grafana` (キー `viewer-password`) に写す。caddy の Pod が環境変数で読み、人の目には出ない。**共有のたびの作り直し (`grafana-viewer-rotate.sh`) は廃止**する。
  失効は caddy の側で起きるので、viewer のパスワードは変える必要がない。
- 人ごとの Grafana ユーザーは作らない (平文のパスワードを保存せずに済む。ユーザーの作成・削除の失敗で食い違うこともない)。
- Grafana の閲覧者は全員 `viewer` として入る (今も同じ)。見られる範囲は他の対象と同じく viewer の権限で、admin は出ない。
- 人が viewer のパスワードを変えて締め出さないよう、`/profile/password`・`/api/user/password` は 404 にする。
  viewer は共有相手全員で 1 人なので、ログイン名 (`PUT /api/user`) を変えられても Basic の付与が壊れる (#51)。使い捨ての Grafana 13.2.3 に viewer の Basic で書き込みを当てて確かめたところ、
  `PUT /api/user`・`PUT`/`PATCH /api/user/preferences`・`POST /api/user/using/<org>`・`POST`/`DELETE /api/user/stars/*`、新しい preferences API の `PUT /apis/preferences.grafana.app/*/preferences/<自分>` が通った。
  このため `/api/user` 以下と `/apis/preferences.grafana.app/` 以下は、GET・HEAD 以外を 404 にする (読み取りは画面が使うので通す)。ほかの書き込み (`/api/org/preferences`・`/api/users/*`・`POST /api/dashboards/db`・`/api/snapshots`) は Grafana が viewer に 401・403 を返すので、caddy では足さない。
- 確認済み (#40): Grafana は **ページの要求にも** Basic を受ける。稼働中の Grafana (`localhost:3000`) に viewer の Basic を付けて
  `/`・`/dashboards`・`/profile` が 200、付けない `/` は 302 (ログイン画面)、誤ったパスワードは 302 だった。さらに実際の Caddyfile の `:8081`
  を稼働中の Grafana に向け (読み取りの要求だけ)、人の資格情報で `/`・`/dashboards`・`/api/user` が 200、`/profile/password`・`PUT /api/user/password` が
  404、認証なし・誤ったパスワードが 401 になることを見た。したがって「人ごとの Grafana ユーザーを作る」への切り替えは要らず、設計は変わらない。
  viewer の権限で `/explore` は 302 になる (Grafana 側の権限の挙動で、Basic の受理とは別)。
- caddy は Basic の値を作れない (Caddyfile に base64 の関数が無い) ので、Deployment の caddy コンテナの起動コマンドが `SHARE_GRAFANA_BASIC` =
  `base64("viewer:<Secret share-grafana のパスワード>")` を組み立ててから `caddy run` する (`printf | base64 | tr -d '\n'`)。人の `Authorization` は Grafana に渡さず、これに差し替える。

### 5. 既存の経路の絞り込みを保つ

認証を通った要求にだけ許可リストを掛け、認証が通らなければ経路を問わず 401 を返す (経路の存在を探られない)。

| 対象 | 通す | 通さない (404) |
|---|---|---|
| grafana | すべて (viewer の権限の範囲)。`Location: http://localhost:3000/` は `/` に書き換える | `/profile/password` `/api/user/password`、`/api/user` 以下と `/apis/preferences.grafana.app/` 以下の GET・HEAD 以外 (#51) |
| headroom | GET の `/dashboard /health /stats /stats-history /stats-lifetime /transformations/feed /favicon.ico` (大文字小文字を区別する完全一致。`/Health` は 404、#53) | それ以外 (`/v1/*`、`POST /settings`、`/stats/reset`、`/cache/clear`、許可リストの経路への HEAD ほか) |
| backstage | GET・HEAD の `/api/proxy` 以外すべて。POST は `/api/auth/*` と `/api/catalog/entities/by-refs` だけ (POST の許可は大文字小文字を区別する) | `/api/proxy*` (表記揺れも) (Grafana の API を Backstage の資格情報で読ませない)、`POST /api/catalog/locations` ほか |

現在の `just/*.Caddyfile` の経路・書き換え (Backstage の `Set-Cookie` の `Domain=localhost` 外し) は、`clusters/kind/share/Caddyfile` に移した
(#41 がこれを ConfigMap `share-caddy` にした。旧 `just/*.Caddyfile` と `observe-share.sh` は、旧 `just share <対象>` のレシピを #42 で `share *args` に置き換えたので呼べなくなっている。ファイルは #44 で消す)。3 サイト (`:8081 grafana` `:8082 headroom` `:8083 backstage`)
が共通の `share_auth` を最初に通す。

- **認証**: `forward_auth` の短縮形は、認証サービスの応答のヘッダを upstream への要求に写すだけで、クライアントへの応答には載せられない。
  署名つき cookie の `Set-Cookie` を応答に載せる (Basic で通ったときだけ認証サービスが付ける) ため、短縮形の中身を書き下した
  `reverse_proxy <認証サービス> { method GET; rewrite /; handle_response @authenticated { header +Set-Cookie {rp.header.Set-Cookie} } }` にしてある。
  2xx 以外 (401 と `WWW-Authenticate`) は認証サービスの応答がそのまま返り、許可リストにも upstream にも進まない。認証サービスに届かなければ
  通さない (試験は 502)。`header +Set-Cookie` は追加なので、Backstage 自身の `Set-Cookie` (`Domain` を外したもの) と並ぶ。
- **許可リストは認証のあと**: 認証が通らなければ経路を問わず 401、通ったあとで許可リストの外が 404 (`route` で順序を固定)。
- 起動の番人: `SHARE_GRAFANA_BASIC`・`SHARE_RELAY_TOKEN`・`SHARE_RELAY_ADDR` が空なら設定の読み込みで落ちる (空の Basic・空のトークンで立ち上がらない)。
  環境変数の一覧は Caddyfile の先頭にある。

Backstage の API 呼び出しは `Authorization: Bearer` を使うので、Basic は最初のページの読み込みでしか使えない。
今は通過後に共有ごとの合言葉の cookie を渡している。変更後は **署名つき cookie** にする
(`<名前>.<署名>`。鍵は Secret `share-session-key`。`just up` が作る)。署名は `HMAC-SHA256(鍵, "<名前>:<その資格情報の salt>")` で、
名前だけでなく項目の salt (hash の 3 つ目の要素。秘密ではない) にも束ねる。salt は `add`・`rotate` のたびに変わるので、`rotate` や、
`delete` → `add` で同じ名前を作り直したときも (同じパスワードでも)、古い cookie は効かなくなる。
これは **MAC であってパスワードのハッシュ化ではない**。CodeQL の `py/weak-sensitive-data-hashing` は以前、保存 hash 全体をこの HMAC に入れた行も
指摘した (パスワードの派生物を sha256 系に通す流れに見えたため)。パスワードやその派生物を入れず salt だけにして、その流れをなくした。
認証サービスは cookie でも Basic でも、**名前が Secret にあり期限内か**を毎回確かめるので、delete・期限切れが cookie 経路にも効く。
Cookie の属性は `Path=/; HttpOnly; Secure; SameSite=Lax` (期限は cookie に持たせず、Secret の `expires_at` だけを正とする)。
認証サービスの `Set-Cookie` を caddy が応答に載せる実装 (`forward_auth` の `handle_response`) は、実装で確かめる。

### 6. 特権 viewer

- `add <名前> --permanent` で作る。全体で 1 つだけで、2 つ目は CLI が断る (作る前に `privileged: true` を数える)。
- 期限がない (`expires_at: null`) 以外は他と同じ。経路も権限も増やさない。
- `rotate <名前>` は特権 viewer だけに使え、パスワードを作り直す (古い値は最大 2 秒で効かなくなる)。
  期限付きの人は `delete` → `add` で足りる。`delete` は特権 viewer にも使える。

## CLI

`justfile` には `share *args` の 1 本を置き、`just/share.sh <kube context> <サブコマンド> ...` が振り分ける (今の公開レシピを 7 個にまとめた流儀に合わせる)。
ツールは開発用コンテナにある `kubectl` だけを使う (ほかは `openssl`・`curl`・`date`・`base64`。Python も jq も要らない)。
**引数の検査 (名前・`--ttl`・`--permanent` の併用) はクラスタに触れる前に終える** (終了コード 2)。Secret が読めなければ止まる (1)。

| コマンド | 動き |
|---|---|
| `just share add <名前> [--ttl <期間>] [--permanent]` | 名前は `^[a-z0-9][a-z0-9-]{0,31}$`。`--ttl` は `30m`・`8h` の形 (分か時間だけ) で、既定 `8h`・**上限 24h** (0・超過・形の誤りは拒否。`--permanent` と同時指定も拒否)。先に期限切れの項目を掃除し、**同じ名前が既にあれば断る** (作り直すなら `delete` → `add`)。`--permanent` は全体で 1 つ (特権が既にあれば断る)。作って **名前・パスワード・期限**を先に表示し (パスワードはここでしか出せないので、URL を待つ間に中断されても失わない)、そのあと URL が出て外から引けるまで待ち (DoH で名前解決まで確かめる。`SHARE_WAIT_SECONDS`、既定 120 秒)、**3 つの URL** を表示する。待っても引けなければ、その旨を警告して終える (資格情報は作ってある) |
| `just share delete <名前>` | Secret の項目を消す (特権にも使える)。最大 2 秒で失効。無い名前は断る |
| `just share list` | 名前・種別 (期限付き/特権)・残り時間・状態 (有効/期限切れ/不正) と、3 つの URL |
| `just share get <名前>` | その人の情報 (種別・作成・期限・残り) と 3 つの URL (パスワードは保存していないので出ない。忘れたら `delete` → `add`、特権は `rotate`) |
| `just share rotate <名前>` | 特権 viewer のパスワードを作り直して表示 (期限付きに使うと、書く前に断る) |
| `just share prune` | 期限切れの項目を 1 回の patch で消す |

- **ハッシュは Pod で作る**: `kubectl exec -i deploy/share -c auth -- python -B -c '... share_auth.hash_password(sys.stdin.read())'` で、パスワードを標準入力から渡す。
  開発用コンテナに Python が無くても、デプロイされた認証サービスと同じ関数・同じ形式 (`pbkdf2_sha256$600000$...`) になり、パスワードは引数にも注釈にも出ない。
  このため `add`・`rotate` は share Pod が Ready であることを要る (落ちていれば何も書かずに止まる)。`openssl kdf` は `pass:` を引数に取るので使わない。
- **書き込みは項目ごとの `kubectl patch secret --type merge`**。`--patch-file` で本人だけが読める一時ファイルから渡し (hash を `ps` に出さない)、
  削除は `{"data": {"<名前>": null}}`。`apply` は使わない (注釈に値が残る。#49)。同時に 2 つの `add` が来ても、別の名前の項目は互いを消さない。
- 項目の読み取りは `kubectl get secret -o go-template` (`base64decode`) で、1 項目 1 行の JSON を正規表現で読む。`add` が書く形 (`{"hash","expires_at","privileged","created_at"}`) だけを前提にし、
  読めない項目は種別を「不正」として `list` に出す (認証サービスは拒否する。`delete` で消す。`prune` は期限切れだけを消し、不正な項目には触れない)。
- 特権の 1 つ制限は「数えてから作る」ので、2 人が同時に打つと競合しうる (単独運用なので受け入れる)。

## manifest・ArgoCD

- `clusters/kind/share/` に `kustomization.yaml`・`deployment.yaml`・`rbac.yaml` (ServiceAccount `share`・Role `share-auth`・RoleBinding) を置き、
  `clusters/kind/argocd/apps/share.yaml` (namespace `share`、`CreateNamespace=true`) で ArgoCD に同期させる。`just ci` はこの Application が指す
  path も描画して kubeconform・kube-linter に掛けるので、登録しないと静的チェックの対象外になる。
  Caddyfile と `share_auth.py` は、`kustomization.yaml` の `configMapGenerator` が repo のファイルから ConfigMap `share-caddy`・`share-auth` にする (写しを持たない)。
- Deployment `share` は 1 Pod・5 コンテナ (caddy、auth、cloudflared ×3)。**Service は作らない** (入口は Cloudflare からの外向き接続だけ。
  クラスタ内に待ち受けを開けない)。イメージはタグを固定する (`caddy:2.11.6-alpine`・`python:3.13.16-alpine`・`cloudflare/cloudflared:2026.9.3`)。`replicas: 1`・`strategy: Recreate`。
  - cloudflared は `tunnel --no-autoupdate --metrics 127.0.0.1:2024<n> --url http://127.0.0.1:808<n>` (コンテナ名 `cloudflared-grafana`・`-headroom`・`-backstage` が :8081・:8082・:8083)。
    metrics は同じ Pod の中でぶつからないよう別ポートにする。`--protocol` は既定 (自動) のまま。UDP 7844 が外に通らないときは args に `--protocol http2` を足す。
  - 全コンテナで `allowPrivilegeEscalation: false`・`readOnlyRootFilesystem: true`・`capabilities.drop: [ALL]`・`runAsNonRoot` (uid 65532)。
    caddy だけ `NET_BIND_SERVICE` を残す (公式イメージの caddy には file capability が付いていて、bounding set から落とすと exec が EPERM になる。1024 未満のポートは使わない)。
    caddy が書く場所 (`XDG_CONFIG_HOME`・`XDG_DATA_HOME`) は emptyDir の `/tmp`。
  - **probe は `exec` の readiness だけ**: kubelet の probe は Pod の IP から来るので、`127.0.0.1` だけで待ち受ける caddy・auth に `tcpSocket`・`httpGet` は届かない。
    caddy は `nc -z 127.0.0.1 8081/8082/8083`、auth は `python -c 'socket.create_connection(...)'`。liveness は付けない (probe の失敗で再起動すると URL が変わる)。cloudflared は distroless で exec できず、何も付けない。
- Secret は **git に置かない** (ArgoCD の selfHeal が中身を戻さないため)。Pod は次の 4 つが無いと起動できない (`optional: false`):

  | Secret (namespace `share`) | 作る処理 | 中身 | 読むもの |
  |---|---|---|---|
  | `share-host` | `_share-relay-up` (`just/share-relay.sh`、#38) | `SHARE_RELAY_ADDR`・`SHARE_RELAY_TOKEN` | caddy (`envFrom`) |
  | `share-grafana` | `_share-secrets` (`just/share-secrets.sh`) | `viewer-password` (Grafana の viewer のパスワードの写し) | caddy (起動コマンドが `SHARE_GRAFANA_BASIC` を組み立てる) |
  | `share-credentials` | 同上 | 空で作る。あれば何もしない (`just share add` の項目を、`just up` の打ち直しで消さない) | auth (K8s API の `get`) |
  | `share-session-key` | 同上 | `key` (`openssl rand -hex 32`)。あれば何もしない (cookie を無効にしない) | auth (K8s API の `get`) |

- 起動順: `just up` は `_grafana-secrets` (viewer のパスワードのファイル) → `_share-relay-up` (`share-host`) → `_share-secrets` を、ArgoCD の同期 (`root.yaml` の apply) より前に打つ。
- `share-grafana` は写しなので、viewer のパスワードを変えたら (旧 `just share` の `grafana-viewer-rotate.sh` が変える。#44 で廃止) `just up` を打ち直して写し直す。それまで Grafana の経路は 401 ではなく
  Grafana 側の拒否 (302 のログイン画面) になる。
- **Secret の作り方 (#49)**: `kubectl apply` は Secret の中身を注釈 `kubectl.kubernetes.io/last-applied-configuration` に残す。Secret を作る処理は `just/secret-lib.sh` の
  `put_secret` (あれば `replace`、無ければ `create`) か `create_secret_if_missing` (`create` だけ) を使う。どちらも注釈を付けず、`replace` は metadata を置き換えるので以前の `apply` が残した注釈も消える。
  `replace --force` (消して作り直す) は動いている Pod の下で消えるので使わない。対象は `grafana-secrets.sh`・`grafana-viewer-rotate.sh`・`share-relay.sh`・`share-secrets.sh`。
  `headlamp-token.sh` は `data` の無い Secret (トークンは controller が入れる) を `apply` するので、注釈に値は残らず、対象にしない。

## 試験 (just ci に入れるもの・入れないもの)

`just/ci.sh` はクラスタを立てず、ネットワークにも出ない (ツールは `nix develop .#ci`)。その範囲で次を入れる。
実装は #39・#40・#41 で、どれも `just ci` の段として走る。

| 段 | 内容 | 拒否を確かめる点 |
|---|---|---|
| 認証の単体 (Python `unittest`、`clusters/kind/share/test_share_auth.py`。#39 で実装済みで `just ci` の最後の段) | `decide()` に資格情報と時刻を渡す。時刻は引数なので時間を進められる。K8s API は偽の HTTP サーバーで、`forward_auth` が見る応答は実際の HTTP で確かめる | 名前なし・誤パスワード・**期限の 1 秒前は通り 1 秒後は拒否**・**項目を消すと拒否**・`rotate` 後の古いパスワード・改ざんした cookie・Secret が空・資格情報が壊れている・改ざんした cookie の付け替え・`rotate` / 期限切れ / `delete` 後の cookie。上限 (24h) 超の `--ttl` は CLI の試験 (#42) |
| 経路の統合 (caddy + auth + 偽の upstream。`clusters/kind/share/test_share_caddy.py`。#40 で実装済みで `just ci` の段 6) | `caddy` を空きポートで起動し、認証サービスは K8s API の代わりに JSON ファイルを読むモード (`AUTH_SOURCE=file:...`)。upstream は標準ライブラリの HTTP サーバー | 認証なしは全経路 401。認証ありでも headroom の `POST /v1/messages`・`/stats/reset`、backstage の `/api/proxy`・`POST /api/catalog/locations`、grafana の `/profile/password` は 404。**ファイルから項目を消す・期限を過去にする → キャッシュ TTL (テストでは 0) の後 401**。旧形式 (sha256)・反復が少ない hash は 401。照合結果の覚えがあっても、delete・期限切れ・ローテーションは次の要求から 401 |
| 静的 | `caddy validate` (経路の統合の試験に含む)。`share` namespace に NodePort・LoadBalancer の Service と Ingress が無いこと (`just/ci.sh` の `yq`。#41)。既存の kubeconform・kube-linter | 公開の入口を足していない |
| manifest (`clusters/kind/share/test_share_manifest.py`、#41) | `kustomize build` した結果を読む | Service・Ingress・containerPort が無い。Caddyfile の必須の環境変数が Pod に渡る。cloudflared が 3 つのポートに 1 本ずつ向く。Role は 2 つの Secret の `get` だけ。タグ固定 |
| 認証サービスに届かない (`test_share_caddy.py`、#52) | `SHARE_AUTH_ADDR` が空、または閉じたポートの caddy を別に立て、全対象の全経路に、認証なし・正しい Basic・cookie を当てる | 全部 401・502・503 で、upstream に届かない |
| Secret を作る処理 (`just/test_share_secrets.py`、#41・#49) | 偽の `kubectl` で `share-secrets.sh`・`grafana-secrets.sh`・`grafana-viewer-rotate.sh` を実行 | `apply` に Secret を渡さない・注釈が無い・`share-secrets.sh` の値が引数に出ない・`grafana-secrets.sh` の Backstage の Basic が引数に出ない (`--from-file` と一時ファイル。#56)・Secret を作る処理に `--from-literal` で渡す秘密が無い (秘密でない値の鍵だけを許す静的な見張り)・あれば資格情報と鍵を置き換えない。URL を引く関数が `api.` を除く |
| CLI (`just/test_share_cli.py`、#42) | 偽の `kubectl` (`exec` は実物の `share_auth` を手元の python3 で動かす) で `just/share.sh` を実行 | `--ttl 25h`・`1441m`・`0`・形の誤り、不正な名前、`--permanent` と `--ttl` の併用は **kubectl を 1 回も呼ばずに**拒否。特権の 2 つ目・期限付きへの `rotate`・既にある名前の `add` は何も書かずに拒否。パスワードが Secret・kubectl の引数・curl の引数に出ない。`add` が作った項目は実物の `decide()` で、正しいパスワードが 200・誤りと期限後が 401、`delete` 後が 401、`rotate` 後の古いパスワードが 401。`get` はパスワードも hash も出さない |

`flake.nix` の `devShells.ci` に `caddy` を足す (今は `tools` 側にしかない)。

クラスタを立てて通す確認 (`just share smoke`、CI には入れない): 期限が 5 秒の資格情報を Secret に直接書き、
`kubectl port-forward` で caddy に当てて、200 → 期限後 401 → delete 後 401 を見る。トンネルは使わない。
外への公開を伴う確認を CI に入れない理由は、ネットワークと稼働中のクラスタに依存して不安定になるためである。

## 実装の分け方 (Issue)

| # | 内容 | 前提 |
|---|---|---|
| #38 ✅ | headroom 中継 (ホスト側の caddy コンテナ、`just up`・`just down` への組み込み、`share-host` Secret) | なし |
| #39 ✅ | 認証サービスと資格情報のモデル (Python、単体試験、`just ci` に組み込み、`caddy` を `devShells.ci` に追加) | なし |
| #40 ✅ | Caddyfile の経路 (許可リスト・Grafana の viewer 付与・Backstage の署名 cookie) と、経路の統合試験 (あわせて #48: headroom の HEAD を許可リストから外す) | #39 |
| #41 (実装済み・稼働中は未確認。確認は #43 に引き継ぐ) | クラスタ内 Deployment `share` と ArgoCD Application、`_share-secrets`、URL の取得 (あわせて #49: Secret を注釈に値が残らない作り方に、#52: 認証サービスに届かないときの試験) | #38・#40 |
| #42 ✅ (稼働中は未確認。確認は #43 に引き継ぐ) | `just share add\|delete\|list\|get\|rotate\|prune` (あわせて #56: `grafana-secrets.sh` の Backstage の Basic を引数に出さない) | #39・#41 |
| #43 | 稼働中のクラスタでの確認 `just share smoke` | #42 |
| #44 | 旧 `just share` の廃止、docs・README の書き換え、CODEOWNERS | #42・#43 |

## 受け入れる制約・残る問題

- URL は `share` Pod の再起動でも変わる。配った URL が使えなくなるので、再起動の原因を作らない (ログのローテーションや probe の設定に注意)。
- headroom の中継はホストの常駐物で、クラスタと寿命が別になる (合意済み)。トークンで bridge 上の他のコンテナからは守る。
- Pod (kind のノードの外向き NAT) から `<ゲートウェイ>:8788` に届くかは、WSL2 の iptables 次第で、稼働中のクラスタでしか確かめられない。#38 の実装では確かめていない (`just up` 後に Pod から `wget http://172.18.0.1:8788/` が 401 を返すこと。届かなければ原因をここに書く)。
- Quick Tunnel は稼働の保証がない。クラスタの外向きの UDP 7844 (QUIC) が通らないときは `--protocol http2` にする。
- #41 は稼働中のクラスタで確かめていない (依頼で `just up` を打たない)。確かめるのは稼働中のクラスタでの確認 (#43) に引き継ぐ。検証用のクラスタ (`HOME_K8S_KUBE_CONTEXT`) で `just up` し、次を見る: share Pod が Ready になる (caddy の `nc -z` の readiness、cloudflared が uid 65532・読み取り専用 root で起動、起動コマンドの `base64`)、`just/share-urls.sh` で 3 つの URL が引ける、資格情報が空で全経路が 401、Pod から中継 (#38) に届く、既存の `share-host` の `last-applied-configuration` が `replace` で消える (#49)。
- #42 の CLI も稼働中のクラスタで確かめていない (依頼で `just up` を打たない)。#43 で、検証用のクラスタ (`HOME_K8S_KUBE_CONTEXT`) に対して次を見る:
  `add` → 3 つの URL が 200 (認証あり)・認証なしが 401 → `delete` → 数秒後に 401、`kubectl exec -i deploy/share -c auth` でパスワードを標準入力から渡せること
  (`kubectl exec` の RBAC と `readOnlyRootFilesystem` の下の `python -B -c`)、`kubectl patch --patch-file` と `go-template` の `base64decode` が実物の kubectl で通ること、
  DoH (`curl --doh-url https://1.1.1.1/dns-query`) で Quick Tunnel の名前が引けること。
- 旧 `just share <対象>` は #42 で呼べなくなる (同じ名前のレシピを置き換えたため)。`observe-share.sh`・`share-headroom.Caddyfile` などの旧ファイルは #44 まで残る (呼ばれない)。
  旧レシピを残す経路は作らない (`grafana-viewer-rotate.sh` を共有のたびに走らせると、Secret `share-grafana` の写しが古くなる)。
- 特権 viewer のパスワードは保存しないので、忘れたら `rotate` する。`get` で再表示したいなら、平文を Secret に持つ方式へ変える判断が要る。
- Grafana への Basic の付与は #40 で確かめ、通った (§4)。人ごとの Grafana ユーザーへの切り替えは要らない。
- 大文字小文字 (#53): caddy の `path` matcher は区別しないので、許可リストに使うと `GET /Health` が通って upstream に `/Health` のまま届く (headroom は区別するので、未知の経路としてプロキシ本体の受け口に落ちうる)。
  許可は `path_regexp` (区別する・全体一致) で書く (headroom は中継と share の両方、backstage の POST)。拒否の `path` (grafana の password・`/api/user`、backstage の `/api/proxy`) は区別しないままにして、表記揺れの素通りを止める。
  `path_regexp` は caddy が整えた path に当たるが、`reverse_proxy` は生の path を送るので、`/x/../health` のようなドットセグメントが許可リストを通って headroom にそのまま届く。headroom の許可では `rewrite * /{re.dash.1}` で許可した経路そのものに書き換えて渡す (クエリは残る)。
- `stage C paths` の対象 (dotfiles の再利用 workflow) に `clusters/kind/share/`・`just/share*` を足すのは dotfiles 側の変更で、この repo の外。
