#!/usr/bin/env bash
# share Pod の Quick Tunnel の URL を、cloudflared のログ (https://*.trycloudflare.com) から引く (docs/cluster/share.md §3)。
# `just share get`・`list` (#42) が source して使う。直接打てば 3 つを表示する。
#   bash just/share-urls.sh <kube context>
# URL は Pod (cloudflared コンテナ) が再起動するたびに変わるので、保存せず毎回その場で引く。
# 関数は set -e を前提にしない (引けなかったら空を返すか、非 0 を返す)。

# share_url <kube context> <grafana|headroom|backstage>
# 現在のコンテナのログの最初の URL を 1 つ表示する。引けなければ何も表示せず 1 を返す。
# api.trycloudflare.com は cloudflared が登録に使う API の宛先 (失敗のログに出る) で、公開 URL ではないので除く
share_url() {
    local url
    url="$(kubectl --context "$1" -n share logs deploy/share -c "cloudflared-$2" 2>/dev/null \
        | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | grep -v '^https://api\.' | head -n 1 || true)"
    [ -n "$url" ] && printf '%s\n' "$url"
}

# share_urls <kube context>
# "<対象> <URL>" を grafana・headroom・backstage の順に 3 行表示する。1 つでも引けなければ、その行は "<対象> -" で、終了コードは 1
share_urls() {
    local target url rc=0
    for target in grafana headroom backstage; do
        if url="$(share_url "$1" "$target")"; then
            printf '%s %s\n' "$target" "$url"
        else
            printf '%s -\n' "$target"
            rc=1
        fi
    done
    return "$rc"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    [ $# -eq 1 ] || { echo "usage: share-urls.sh <kube-context>" >&2; exit 2; }
    share_urls "$1"
fi
