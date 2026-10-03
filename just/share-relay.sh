#!/usr/bin/env bash
# headroom の中継 (ホスト側の caddy コンテナ) を起動・削除する。`just up` の `_share-relay-up`、`just down` の `_share-relay-down` が呼ぶ。
# 構成は docs/cluster/share.md。headroom (127.0.0.1:8787) は loopback のまま変えず、kind の bridge のゲートウェイだけで
# 待ち受ける caddy が、共有トークン付きの GET の許可リストだけを 8787 へ転送する (just/share-relay.Caddyfile)。
# up: トークンを (無ければ生成して) ファイルと Secret share/share-host に入れ、コンテナを作り直す。トークンは作り直しても変えない。
# down: コンテナを消す。トークンのファイルは残す (次の up で同じ値を使う)。
# 引数: up   <トークンのファイル> <リポジトリの所有者を調べるディレクトリ> <kube context> <待ち受けのポート>
#       down
set -euo pipefail

container=home-k8s-share-relay
# 公式イメージ。タグは固定する (上げるときは just/share-relay.Caddyfile が動くことを試験で確かめる)
image=caddy:2.11.6-alpine
network=kind

action="${1:-}"
case "$action" in
    up | down) ;;
    *) echo "usage: share-relay.sh up <token-file> <owner-ref> <kube-context> <port> | down" >&2; exit 2 ;;
esac

if [ "$action" = down ]; then
    docker rm -f "$container" >/dev/null 2>&1 || true
    echo "中継 $container を消した"
    exit 0
fi

token_file="$2"
owner_ref="$3"
ctx="$4"
port="$5"
script_dir="$(cd "$(dirname "$0")" && pwd)"
. "$script_dir/secret-lib.sh"

# 開発用コンテナでは ~/.local/share/home-k8s をホストと共有していないと、トークンがコンテナの中にだけ残る (grafana-secrets.sh と同じ)
if [ -f /.dockerenv ] && ! mountpoint -q "$HOME/.local/share/home-k8s"; then
    echo "~/.local/share/home-k8s がホストと共有されていない。just devcontainer down && just devcontainer up で作り直す" >&2
    exit 1
fi

# kind の bridge は IPv4 と IPv6 の両方を持ちうるので、IPv4 のゲートウェイを選ぶ
gateway="$(docker network inspect "$network" -f '{{range .IPAM.Config}}{{.Gateway}}{{"\n"}}{{end}}' | grep -F . | head -n 1 || true)"
if [ -z "$gateway" ]; then
    echo "docker network $network のゲートウェイが引けない。先に kind のクラスタを作る (just up の順序)" >&2
    exit 1
fi

if [ ! -s "$token_file" ]; then
    mkdir -p "$(dirname "$token_file")"
    (umask 077; printf '%s' "$(openssl rand -hex 16)" > "$token_file")
    # コンテナは root で動くので、ホストのユーザー (リポジトリの所有者) からも読めるようにする
    chown "$(stat -c %u:%g "$owner_ref")" "$token_file"
fi
chmod 600 "$token_file"
token="$(cat "$token_file")"
# 空のトークンでは起動しない (Caddyfile 側も、空だと設定を読めず終了する。二重にしてある)
if [ -z "$token" ]; then
    echo "トークンが空: $token_file" >&2
    exit 1
fi

# docker run の -v はホストの Docker デーモンがパスを解決する。開発用コンテナはリポジトリをホストと同じ絶対パスで
# マウントしているので $script_dir のままで渡せるが、worktree を消すと再起動 (--restart) で読めなくなる。
# なので Caddyfile はトークンと同じ置き場にコピーして、そちらを渡す
caddyfile="$(dirname "$token_file")/relay.Caddyfile"
cp "$script_dir/share-relay.Caddyfile" "$caddyfile"
chmod 644 "$caddyfile"

# Pod が環境変数 (envFrom) でそのまま読めるキー名にする。トークンは --from-file で渡す (引数に出さない。ps に残る)
kubectl --context "$ctx" create namespace share --dry-run=client -o yaml \
    | kubectl --context "$ctx" apply -f - >/dev/null
# put_secret は apply ではなく replace・create で入れる (apply は last-applied-configuration の注釈にトークンを残す。#49)
kubectl --context "$ctx" -n share create secret generic share-host \
    --from-literal=SHARE_RELAY_ADDR="$gateway:$port" --from-file=SHARE_RELAY_TOKEN="$token_file" \
    --dry-run=client -o yaml | put_secret "$ctx" share share-host

docker rm -f "$container" >/dev/null 2>&1 || true
# トークンはコマンドラインに出さない (ps に残る)。-e NAME だけ渡せば、この shell の環境から取る
SHARE_RELAY_BIND="$gateway" SHARE_RELAY_PORT="$port" SHARE_RELAY_TOKEN="$token" \
    docker run -d --name "$container" --network host --restart unless-stopped \
    -e SHARE_RELAY_BIND -e SHARE_RELAY_PORT -e SHARE_RELAY_TOKEN \
    -v "$caddyfile":/etc/caddy/Caddyfile:ro "$image" >/dev/null

# 待ち受けの確認。トークン無しで 401 が返れば caddy が起動している (headroom が動いていなくても確かめられる)
for _ in $(seq 1 20); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://$gateway:$port/dashboard" || true)"
    [ "$code" = 401 ] && break
    sleep 0.5
done
if [ "$code" != 401 ]; then
    echo "中継が $gateway:$port で待ち受けていない (トークン無しで $code)。docker logs $container を見る" >&2
    exit 1
fi
echo "中継 $container: $gateway:$port -> 127.0.0.1:8787 (GET の許可リストだけ、トークン付き)"
