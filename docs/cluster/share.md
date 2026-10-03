# 公開 (just share) を常駐させ、人ごとの資格情報で配る — 設計

状態: 設計。実装は GitHub の Issue ごとに進め、そのたびにこの文書を実際の構成に書き換える。実装済み: #38 (headroom の中継)、認証サービス (#39、`clusters/kind/share/share_auth.py`)、Caddyfile の経路と統合試験 (#40、`clusters/kind/share/Caddyfile`・`test_share_caddy.py`)。
図の HTML (変更前→変更後のアニメーション): [share-design.html](share-design.html)
認証サービス (#39) の diff の図 (判定を動かせるアニメーション): [share-auth.html](share-auth.html)
Caddyfile の経路 (#40) の diff の図 (要求を流して通る・通らないを見るアニメーション): [share-routes.html](share-routes.html)

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
  HEAD を使う用途は無い (Pod の probe は `tcpSocket`) ので、許可リストから外して GET に絞る。HEAD→GET への書き換えは、HEAD の応答に本文を
  返しうるので採らない。
- クラスタ側 (Pod の caddy、`clusters/kind/share/Caddyfile` の `:8082`) も同じ許可リストをもう一度掛ける (二重。#40)。
- トークンは `just up` が `openssl rand -hex 16` で作り、`~/.local/share/home-k8s/share/relay-token` (権限 600) に置く。
  あれば再利用するので、`just up` を打ち直しても値は変わらない。`just down` はコンテナだけを消し、トークンのファイルは残す。
  Caddyfile は同じ場所に `relay.Caddyfile` としてコピーしてからマウントする (worktree を消しても `--restart` で読めるように)。
  トークンは `docker run` の引数にも `kubectl create secret` の引数にも出さず (`ps` に残る)、環境変数と `--from-file` で渡す。
- Pod への宛先とトークンは Secret `share/share-host` (namespace `share` は無ければ `just up` が作る) に入る。
  キーは `SHARE_RELAY_ADDR` (`<ゲートウェイ>:<ポート>`、例 `172.18.0.1:8788`) と `SHARE_RELAY_TOKEN`。
  #41 の Deployment が `envFrom` でそのまま環境変数にする。git には置かない (bridge のアドレスは環境で変わりうる)。
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
| 認証サービス | 標準ライブラリだけの Python (`clusters/kind/share/share_auth.py`)。ConfigMap に置き `python:3.13-alpine` で動かす (ConfigMap は #41)。判定は純関数 `decide(credentials, request, now, session_key, verifier)` で、`(status, headers)` を返す。`request` は `Authorization` と `Cookie` のヘッダ値だけ |
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
  viewer は共有相手全員で 1 人なので、ログイン名 (`PUT /api/user`) を変えられても Basic の付与が壊れる (#51)。使い捨ての Grafana 13.2.3 に viewer の Basic で書き込みを当てて確かめたところ、
  `PUT /api/user`・`PUT`/`PATCH /api/user/preferences`・`POST /api/user/using/<org>`・`POST`/`DELETE /api/user/stars/*`、新しい preferences API の `PUT /apis/preferences.grafana.app/*/preferences/<自分>` が通った。
  このため `/api/user` 以下と `/apis/preferences.grafana.app/` 以下は、GET・HEAD 以外を 404 にする (読み取りは画面が使うので通す)。ほかの書き込み (`/api/org/preferences`・`/api/users/*`・`POST /api/dashboards/db`・`/api/snapshots`) は Grafana が viewer に 401・403 を返すので、caddy では足さない。
- 確認済み (#40): Grafana は **ページの要求にも** Basic を受ける。稼働中の Grafana (`localhost:3000`) に viewer の Basic を付けて
  `/`・`/dashboards`・`/profile` が 200、付けない `/` は 302 (ログイン画面)、誤ったパスワードは 302 だった。さらに実際の Caddyfile の `:8081`
  を稼働中の Grafana に向け (読み取りの要求だけ)、人の資格情報で `/`・`/dashboards`・`/api/user` が 200、`/profile/password`・`PUT /api/user/password` が
  404、認証なし・誤ったパスワードが 401 になることを見た。したがって「人ごとの Grafana ユーザーを作る」への切り替えは要らず、設計は変わらない。
  viewer の権限で `/explore` は 302 になる (Grafana 側の権限の挙動で、Basic の受理とは別)。
- caddy は Basic の値を作れない (Caddyfile に base64 の関数が無い) ので、Deployment (#41) が起動時に `SHARE_GRAFANA_BASIC` =
  `base64("viewer:<Secret grafana-viewer のパスワード>")` を組み立てて渡す。人の `Authorization` は Grafana に渡さず、これに差し替える。

### 5. 既存の経路の絞り込みを保つ

認証を通った要求にだけ許可リストを掛け、認証が通らなければ経路を問わず 401 を返す (経路の存在を探られない)。

| 対象 | 通す | 通さない (404) |
|---|---|---|
| grafana | すべて (viewer の権限の範囲)。`Location: http://localhost:3000/` は `/` に書き換える | `/profile/password` `/api/user/password`、`/api/user` 以下と `/apis/preferences.grafana.app/` 以下の GET・HEAD 以外 (#51) |
| headroom | GET の `/dashboard /health /stats /stats-history /stats-lifetime /transformations/feed /favicon.ico` (大文字小文字を区別する完全一致。`/Health` は 404、#53) | それ以外 (`/v1/*`、`POST /settings`、`/stats/reset`、`/cache/clear`、許可リストの経路への HEAD ほか) |
| backstage | GET・HEAD の `/api/proxy` 以外すべて。POST は `/api/auth/*` と `/api/catalog/entities/by-refs` だけ (POST の許可は大文字小文字を区別する) | `/api/proxy*` (表記揺れも) (Grafana の API を Backstage の資格情報で読ませない)、`POST /api/catalog/locations` ほか |

現在の `just/*.Caddyfile` の経路・書き換え (Backstage の `Set-Cookie` の `Domain=localhost` 外し) は、`clusters/kind/share/Caddyfile` に移した
(#41 がこれを ConfigMap にする。旧 `just/*.Caddyfile` は旧 `just share` が使うので #44 まで残す)。3 サイト (`:8081 grafana` `:8082 headroom` `:8083 backstage`)
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
| 認証の単体 (Python `unittest`、`clusters/kind/share/test_share_auth.py`。#39 で実装済みで `just ci` の最後の段) | `decide()` に資格情報と時刻を渡す。時刻は引数なので時間を進められる。K8s API は偽の HTTP サーバーで、`forward_auth` が見る応答は実際の HTTP で確かめる | 名前なし・誤パスワード・**期限の 1 秒前は通り 1 秒後は拒否**・**項目を消すと拒否**・`rotate` 後の古いパスワード・改ざんした cookie・Secret が空・資格情報が壊れている・改ざんした cookie の付け替え・`rotate` / 期限切れ / `delete` 後の cookie。上限 (24h) 超の `--ttl` は CLI の試験 (#42) |
| 経路の統合 (caddy + auth + 偽の upstream。`clusters/kind/share/test_share_caddy.py`。#40 で実装済みで `just ci` の段 6) | `caddy` を空きポートで起動し、認証サービスは K8s API の代わりに JSON ファイルを読むモード (`AUTH_SOURCE=file:...`)。upstream は標準ライブラリの HTTP サーバー | 認証なしは全経路 401。認証ありでも headroom の `POST /v1/messages`・`/stats/reset`、backstage の `/api/proxy`・`POST /api/catalog/locations`、grafana の `/profile/password` は 404。**ファイルから項目を消す・期限を過去にする → キャッシュ TTL (テストでは 0) の後 401**。旧形式 (sha256)・反復が少ない hash は 401。照合結果の覚えがあっても、delete・期限切れ・ローテーションは次の要求から 401 |
| 静的 | `caddy validate` (経路の統合の試験に含む)。`share` namespace に NodePort・LoadBalancer・Ingress が無いこと (`yq`)。既存の kubeconform・kube-linter | 公開の入口を足していない |

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
- Grafana への Basic の付与は #40 で確かめ、通った (§4)。人ごとの Grafana ユーザーへの切り替えは要らない。
- 大文字小文字 (#53): caddy の `path` matcher は区別しないので、許可リストに使うと `GET /Health` が通って upstream に `/Health` のまま届く (headroom は区別するので、未知の経路としてプロキシ本体の受け口に落ちうる)。
  許可は `path_regexp` (区別する・全体一致) で書く (headroom は中継と share の両方、backstage の POST)。拒否の `path` (grafana の password・`/api/user`、backstage の `/api/proxy`) は区別しないままにして、表記揺れの素通りを止める。
- `stage C paths` の対象 (dotfiles の再利用 workflow) に `clusters/kind/share/`・`just/share*` を足すのは dotfiles 側の変更で、この repo の外。
