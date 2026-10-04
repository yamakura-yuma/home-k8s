#!/bin/sh
# share Pod の caddy の起動 (Deployment の command。ConfigMap share-caddy の entrypoint.sh を /etc/caddy に置く。docs/cluster/share.md)。
# Secret share-host は optional で、まだ無いと環境変数が空のまま Pod が起動する (CreateContainerConfigError で止まらない)。
# 中継の宛先・トークン (SHARE_RELAY_ADDR・SHARE_RELAY_TOKEN) が揃っていれば Caddyfile、1 つでも空なら全拒否の Caddyfile.closed で caddy を起動する。
# Caddyfile は {$VAR} が空だと起動しない (require_env) ので、空のまま渡さない。Secret ができたあとは、閉じて起動した Pod を just up が作り直して読ませる
# (just/secret-lib.sh の restart_share_pod_if_closed。下の「全経路を 503 で拒否」の行を、caddy のログから探す)。
# Grafana の viewer のパスワード (Secret share-grafana) はここでは読まない (#58): 認証サービスが要求のたびに API で読み、Basic の値を組んで caddy に渡す。
# 無い・空のあいだは Grafana の経路だけが 503 で、Pod の作り直しは要らない (headroom・backstage の経路には関わらない)。
dir="${SHARE_CADDY_DIR:-/etc/caddy}"

if [ -n "$SHARE_RELAY_TOKEN" ] && [ -n "$SHARE_RELAY_ADDR" ]; then
    config="$dir/Caddyfile"
else
    echo "Secret (share-host) が揃っていない: 全経路を 503 で拒否して起動する" >&2
    config="$dir/Caddyfile.closed"
fi
exec caddy run --config "$config" --adapter caddyfile
