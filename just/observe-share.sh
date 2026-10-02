#!/usr/bin/env bash
# just share の本体。
# 引数: <admin のパスワードファイル> <viewer のパスワードファイル> <namespace> <ログファイル> <状態ファイル> <対象>
# 対象は grafana (localhost:3000)・headroom (localhost:8787)・backstage (localhost:7007)。Cloudflare Quick Tunnel で公開し、
# URL が外から引けて応答するようになってから URL とユーザー・パスワードだけを表示する。
# grafana は viewer のパスワードを作り直してから公開する。admin の資格情報は表示しない
# (共有相手に渡すのは閲覧用の viewer だけ)。headroom は共有のたびに使い捨てのパスワードを作り、
# ダッシュボードに要る GET だけを通す (プロキシ本体の /v1/* などは通さない)。
# backstage も使い捨てのパスワードを作り、画面と TechDocs に要る経路だけを通す (Grafana へのプロキシなどは通さない)。
# cloudflared と caddy のログはファイルへ流す。Ctrl-C (または TERM) で両方を止めてから抜ける。
# 公開中は状態ファイルに URL と cloudflared の pid を書き、just show grafana が読む (grafana のときだけ)。
set -euo pipefail
admin_file="$1"
viewer_file="$2"
ns="$3"
log="$4"
state="$5"
target="$6"
dir="$(cd "$(dirname "$0")" && pwd)"
if ! command -v caddy >/dev/null; then
    echo "caddy が無い (flake.nix に足す前のコンテナ)。just devcontainer up を打ち直して入れる" >&2
    exit 1
fi
case "$target" in
grafana)
    if [ ! -s "$admin_file" ] || [ ! -s "$viewer_file" ]; then
        echo "$admin_file か $viewer_file が無い。先に just up を打つ" >&2
        exit 1
    fi
    if ! curl -sf -o /dev/null --max-time 5 http://localhost:3000/api/health; then
        echo "Grafana (localhost:3000) が応答しない。just up を打ったか確かめる" >&2
        exit 1
    fi
    # 前回の共有相手が今回の URL で入れないよう、共有のたびに viewer のパスワードを変える
    bash "$dir/grafana-viewer-rotate.sh" "$admin_file" "$viewer_file" "$ns"
    # cloudflared → caddy → 対象。対象ごとに待ち受けるポートを分ける (grafana と headroom を同時に共有できる)
    export SHARE_PROXY_PORT=3001
    caddyfile="$dir/observe-share.Caddyfile"
    health=/api/health
    user=viewer
    password="$(cat "$viewer_file")"
    ;;
headroom)
    if ! curl -sf -o /dev/null --max-time 5 http://localhost:8787/health; then
        echo "headroom (localhost:8787) が応答しない。headroom.cli proxy が動いているか確かめる" >&2
        exit 1
    fi
    export SHARE_PROXY_PORT=3002
    # ログも分ける (同じファイルだと、後から起動した側が上書きし、URL の取り違えも起きる)
    log="${log%.log}-headroom.log"
    caddyfile="$dir/share-headroom.Caddyfile"
    health=/health
    user=viewer
    password="$(openssl rand -hex 16)"
    # caddy の basic_auth は bcrypt のハッシュを取る (hash-password は標準入力から読めないので引数で渡す。
    # 使い捨てのパスワードで、引数に出るのはこのコンテナの中の一瞬だけ)
    SHARE_USER="$user"
    SHARE_PASSWORD_HASH="$(caddy hash-password --plaintext "$password")"
    export SHARE_USER SHARE_PASSWORD_HASH
    ;;
backstage)
    if ! curl -sf -o /dev/null --max-time 5 http://localhost:7007/.backstage/health/v1/readiness; then
        echo "Backstage (localhost:7007) が応答しない。just up を打ったか確かめる" >&2
        exit 1
    fi
    export SHARE_PROXY_PORT=3003
    log="${log%.log}-backstage.log"
    caddyfile="$dir/share-backstage.Caddyfile"
    health=/.backstage/health/v1/readiness
    user=viewer
    password="$(openssl rand -hex 16)"
    SHARE_USER="$user"
    SHARE_PASSWORD_HASH="$(caddy hash-password --plaintext "$password")"
    # basic_auth を通ったブラウザに渡す cookie の値 (share-backstage.Caddyfile)。共有のたびに作り直す
    SHARE_SESSION="$(openssl rand -hex 16)"
    export SHARE_USER SHARE_PASSWORD_HASH SHARE_SESSION
    ;;
*)
    echo "対象は grafana・headroom・backstage のどれか: $target" >&2
    exit 1
    ;;
esac
# caddy は SO_REUSEPORT で同じポートに重ねて bind できてしまい、2 つの共有の接続が混ざる。使用中なら断る
if (exec 3<>"/dev/tcp/127.0.0.1/$SHARE_PROXY_PORT") 2>/dev/null; then
    echo "127.0.0.1:$SHARE_PROXY_PORT が使用中 (同じ対象の just share が動いている)" >&2
    exit 1
fi

mkdir -p "$(dirname "$log")"
: > "$log"
pids=()
stop() {
    trap - INT TERM EXIT
    rm -f "$state"
    [ ${#pids[@]} -gt 0 ] && kill "${pids[@]}" 2>/dev/null || true
    wait 2>/dev/null || true
    echo "停止しました (URL は無効になった)"
}
# Ctrl-C は止め方として正しいので、just に失敗と表示させない
trap 'exit 0' INT TERM
trap stop EXIT

caddy run --adapter caddyfile --config "$caddyfile" >> "$log" 2>&1 &
pids+=($!)
# 閉じるときに開いたままのブラウザの接続を待たない (既定は 30 秒待つ)
cloudflared tunnel --no-autoupdate --grace-period 1s --url "http://127.0.0.1:$SHARE_PROXY_PORT" >> "$log" 2>&1 &
pids+=($!)

fail() {
    echo "$1 (ログ: $log)" >&2
    tail -n 20 "$log" >&2
    exit 1
}

url=
for _ in $(seq 60); do
    url=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$log" | head -n 1 || true)
    [ -n "$url" ] && break
    kill -0 "${pids[@]}" 2>/dev/null || fail "cloudflared か caddy が止まった"
    sleep 1
done
[ -n "$url" ] || fail "60 秒待っても URL が発行されなかった"

# URL が出た直後はまだ名前が引けない。そのとき開くと NXDOMAIN がブラウザや DNS に
# キャッシュされ (trycloudflare.com の否定キャッシュは 60 秒)、しばらく開けなくなる。
# 手元のリゾルバに NXDOMAIN を覚えさせないよう、1.1.1.1 に DoH で引いて応答まで確かめる
for _ in $(seq 120); do
    if curl -sf -o /dev/null --max-time 5 --doh-url https://1.1.1.1/dns-query -u "$user:$password" "$url$health"; then
        # 強制終了で残ったときに古い URL を出さないよう、pid も書いて生死を確かめられるようにする
        [ "$target" = grafana ] && printf 'url=%s\ncloudflared_pid=%s\n' "$url" "${pids[1]}" > "$state"
        cat <<EOF
$target を公開しました（Ctrl-C で停止）
  URL:        $url
  ユーザー:   $user
  パスワード: $password
  ログ:       $log
EOF
        # どちらかが止まったら (トンネルが切れたら) 抜ける
        wait -n "${pids[@]}" || true
        fail "cloudflared か caddy が止まった"
    fi
    kill -0 "${pids[@]}" 2>/dev/null || fail "cloudflared か caddy が止まった"
    sleep 1
done
fail "URL が 2 分以上応答しなかった"
