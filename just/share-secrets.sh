#!/usr/bin/env bash
# just up の `_share-secrets` の本体。share Pod (clusters/kind/share) が起動に要る Secret のうち、中継 (share-relay.sh) が作る share-host 以外を作る。
# Secret の中身は git に置かない (ArgoCD の selfHeal が中身を戻さない)。Pod は Secret が無くても起動する (参照は optional で、揃うまで全経路 503) ので、
# ArgoCD の同期が先でも止まらない。caddy は環境変数を起動時にしか読まないので、Secret が揃う前に起動した Pod は最後に作り直す (restart_share_pod_if_closed、#64)。
#   share-credentials  人ごとの資格情報 (just share add が項目を足す)。空で作る。あれば何もしない (打ち直しで人の資格情報を消さない)
#   share-session-key  Backstage の cookie の署名鍵 (キー key)。無ければ作り、あれば何もしない
#   share-grafana      Grafana の viewer のパスワード (キー viewer-password)。Secret grafana-viewer は別 namespace (observability) にあって
#                      Pod からは読めないので、同じパスワードのファイルから写す。viewer のパスワードを変えたら、打ち直して写し直す
# Secret は last-applied-configuration の注釈に値が残らない作り方 (secret-lib.sh、#49)。
# 引数: <viewer のパスワードファイル> <kube context>
set -euo pipefail
viewer_file="$1"
ctx="$2"
script_dir="$(cd "$(dirname "$0")" && pwd)"
. "$script_dir/secret-lib.sh"

# viewer のパスワードは just up の _grafana-secrets が (無ければ) 作る。空のまま立ち上げて Grafana が 401 になるのを避ける
if [ ! -s "$viewer_file" ]; then
    echo "viewer のパスワードのファイルが無い・空: $viewer_file (先に _grafana-secrets が作る)" >&2
    exit 1
fi

kubectl --context "$ctx" create namespace share --dry-run=client -o yaml \
    | kubectl --context "$ctx" apply -f - >/dev/null

# 署名鍵は --from-file で渡す (引数に出すと ps に残る)。一時ファイルは本人だけが読めるようにして、終わったら消す
tmp="$(umask 077; mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
printf '%s' "$(openssl rand -hex 32)" > "$tmp/key"

create_secret_if_missing "$ctx" share share-credentials
create_secret_if_missing "$ctx" share share-session-key --from-file=key="$tmp/key"
kubectl --context "$ctx" -n share create secret generic share-grafana \
    --from-file=viewer-password="$viewer_file" \
    --dry-run=client -o yaml | put_secret "$ctx" share share-grafana

# share-host は _share-relay-up (just up のこの前の段) が作る。無いまま作り直しても閉じたままなので、あるときだけ。
# share-grafana は上で作った (set -e)。閉じて起動していた Pod だけを作り直し、開いている Pod (2 回目以降の just up) には触れない
if secret_exists "$ctx" share share-host; then
    restart_share_pod_if_closed "$ctx"
fi
