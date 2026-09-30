#!/usr/bin/env bash
# just observe-share の本体。引数: <パスワードファイル> <ログファイル> <状態ファイル>
# Grafana (localhost:3000) を Cloudflare Quick Tunnel で公開し、URL が外から引けて応答するように
# なってから URL・ユーザー・パスワードだけを表示する。cloudflared と caddy のログはファイルへ流す。
# Ctrl-C (または TERM) で両方を止めてから抜ける。
# 公開中は状態ファイルに URL と cloudflared の pid を書き、just observe-show-connection が読む。
set -euo pipefail
password_file="$1"
log="$2"
state="$3"
dir="$(cd "$(dirname "$0")" && pwd)"
# cloudflared → caddy → Grafana。caddy がリダイレクト先の localhost:3000 を相対パスに直す
export SHARE_PROXY_PORT=3001

if [ ! -s "$password_file" ]; then
    echo "$password_file が無い。先に just observe-up を打つ" >&2
    exit 1
fi
if ! command -v caddy >/dev/null; then
    echo "caddy が無い (flake.nix に足す前のコンテナ)。just devcontainer up を打ち直して入れる" >&2
    exit 1
fi
if ! curl -sf -o /dev/null --max-time 5 http://localhost:3000/api/health; then
    echo "Grafana (localhost:3000) が応答しない。just observe-up を打ったか確かめる" >&2
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

caddy run --adapter caddyfile --config "$dir/observe-share.Caddyfile" >> "$log" 2>&1 &
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
    if curl -sf -o /dev/null --max-time 5 --doh-url https://1.1.1.1/dns-query "$url/api/health"; then
        # 強制終了で残ったときに古い URL を出さないよう、pid も書いて生死を確かめられるようにする
        printf 'url=%s\ncloudflared_pid=%s\n' "$url" "${pids[1]}" > "$state"
        cat <<EOF
Grafana を公開しました（Ctrl-C で停止）
  URL:        $url
  ユーザー:   admin
  パスワード: $(cat "$password_file")
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
