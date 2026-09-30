#!/usr/bin/env bash
# just observe-share の前段。viewer のパスワードを作り直し、前の共有相手が次の共有で入れないようにする。
# 引数: <admin のパスワードファイル> <viewer のパスワードファイル> <namespace>
# 1. 新しいパスワードをファイルと Secret grafana-viewer に書く (Pod を作り直したときにサイドカーが使う)
# 2. 動いている Grafana の viewer のパスワードを API で変え、ログイン中のセッションも切る
set -euo pipefail
admin_file="$1"
viewer_file="$2"
ns="$3"
api=http://localhost:3000/api
auth="admin:$(cat "$admin_file")"

# viewer はサイドカーが Grafana の起動後に作るので、居なければ少し待つ
id=
for _ in $(seq 30); do
    id=$(curl -sf -u "$auth" "$api/users/lookup?loginOrEmail=viewer" | grep -o '"id":[0-9]*' | head -n 1 | cut -d: -f2 || true)
    [ -n "$id" ] && break
    sleep 1
done
if [ -z "$id" ]; then
    echo "Grafana に viewer が居ない。just observe-up を打ち直す (サイドカー viewer-user が作る)" >&2
    exit 1
fi

new=$(openssl rand -base64 24)
(umask 077; printf '%s' "$new" > "$viewer_file.new")
chown --reference="$viewer_file" "$viewer_file.new"
mv "$viewer_file.new" "$viewer_file"
kubectl --context kind-study-kind -n "$ns" create secret generic grafana-viewer \
    --from-file=password="$viewer_file" \
    --dry-run=client -o yaml | kubectl --context kind-study-kind apply -f - >/dev/null
printf '{"password":"%s"}' "$new" \
    | curl -sf -o /dev/null -u "$auth" -X PUT -H 'Content-Type: application/json' \
        --data-binary @- "$api/admin/users/$id/password"
curl -sf -o /dev/null -u "$auth" -X POST "$api/admin/users/$id/logout"
