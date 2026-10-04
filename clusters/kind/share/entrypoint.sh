#!/bin/sh
# share Pod の caddy の起動 (Deployment の command。ConfigMap share-caddy の entrypoint.sh を /etc/caddy に置く。docs/cluster/share.md)。
# Secret share-host・share-grafana は optional で、まだ無いと環境変数が空のまま Pod が起動する (CreateContainerConfigError で止まらない)。
# 3 つ (中継の宛先・トークン・viewer のパスワード) が揃っていれば Caddyfile、1 つでも空なら全拒否の Caddyfile.closed で caddy を起動する。
# Caddyfile は {$VAR} が空だと起動しない (require_env) ので、空のまま渡さない。Secret ができたあとは、閉じて起動した Pod を just up が作り直して読ませる
# (just/secret-lib.sh の restart_share_pod_if_closed。下の「全経路を 503 で拒否」の行を、caddy のログから探す)。
# Caddyfile は base64 の関数を持たないので、Grafana に渡す Basic の値はここで組み立てる (viewer の鍵。人の資格情報は Grafana に渡さない)。
# base64("viewer:") は空でないので、パスワードが空かどうかは組み立てる前に確かめる。
dir="${SHARE_CADDY_DIR:-/etc/caddy}"

if [ -n "$GRAFANA_VIEWER_PASSWORD" ] && [ -n "$SHARE_RELAY_TOKEN" ] && [ -n "$SHARE_RELAY_ADDR" ]; then
    SHARE_GRAFANA_BASIC="$(printf 'viewer:%s' "$GRAFANA_VIEWER_PASSWORD" | base64 | tr -d '\n')"
    export SHARE_GRAFANA_BASIC
    config="$dir/Caddyfile"
else
    echo "Secret (share-host・share-grafana) が揃っていない: 全経路を 503 で拒否して起動する" >&2
    config="$dir/Caddyfile.closed"
fi
exec caddy run --config "$config" --adapter caddyfile
