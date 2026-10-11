#!/usr/bin/env bash
# just guest の本体。realm home-k8s のゲスト (他の人。グループ guests) を発行・一覧・取り消しする。設計は docs/cluster/guest-access.md。
#   add <名前>     ユーザー guest-<名前> を作って guests に入れ、一時パスワードを 1 回だけ画面に出す。必須アクションはパスワードの変更と TOTP の設定。
#                  期限は「発行した日の次の 3:00 JST」で、ユーザーの属性 guest-expires-at (UTC) に書く。期限を過ぎたら CronJob guest-expiry
#                  (clusters/kind/auth/guest-expiry。5 分ごと) がセッションを切ってユーザーを消す。人ごとの期限は持たない
#   list           guests のユーザーと、有効か・期限 (日本時間) を出す
#   delete <名前>  guest-<名前> の Keycloak のセッションを切ってユーザーを消す (guests のユーザーだけ)
# kcadm.sh は Keycloak の Pod の中で、realm master の admin (Pod の環境変数) で打つ (docs/cluster/keycloak.md の kc 関数と同じ)。
# 時刻は日本時間で出す (開発用コンテナに tzdata が無いので TZ は POSIX の形の JST-9)。
# 一時パスワードは引数にもファイルにも置かない。この端末で作り、kubectl exec の標準入力で Pod に渡し、最後に 1 回だけ表示する。
# 引数: <kube context> <add|list|delete> [<名前>]
# 環境変数 GUEST_NOW_EPOCH は試験 (just/test_guest.py) が「今」を固定するためのもの
set -euo pipefail
ctx="$1"
action="${2:-}"
shift 2 || true
usage() {
    echo "usage: just guest add <名前> | just guest list | just guest delete <名前>" >&2
    exit 2
}
# 名前は英小文字・数字・- だけ (Keycloak のユーザー名と Backstage のカタログの名前に使える形)。ユーザー名は guest-<名前> になる
check_name() {
    [[ "$1" =~ ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ ]] || { echo "名前は英小文字・数字・- の 1〜32 文字 (先頭と末尾は英数字): '$1'" >&2; exit 2; }
}

# Pod の中で kcadm.sh を打つ。$1 は Pod の bash のスクリプト (中で kcadm 関数が使える)、残りはその位置引数。標準入力は Pod に渡る
kc() {
    local script="$1"
    shift
    kubectl --context "$ctx" -n auth exec -i keycloak-0 -- bash -c '
        set -euo pipefail
        cfg=$(mktemp)
        trap '\''rm -f "$cfg" "$cfg.err"'\'' EXIT
        kcadm() { /opt/keycloak/bin/kcadm.sh "$@" --config "$cfg"; }
        # 「Logging into ...」は出さず、失敗したときだけ理由を出す
        kcadm config credentials --server http://localhost:8080 --realm master \
            --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null 2>"$cfg.err" \
            || { cat "$cfg.err" >&2; exit 1; }
        '"$script" _ "$@"
}

case "$action" in
add)
    [ $# -eq 1 ] || usage
    check_name "$1"
    user="guest-$1"
    # 期限: 今より後で最初の 18:00 UTC (= 3:00 JST)。0:00〜3:00 JST に発行すると、その日の 3:00 で終わる
    now=${GUEST_NOW_EPOCH:-$(date -u +%s)}
    expires=$(date -u -d "@$now" +%Y-%m-%dT18:00:00Z)
    [ "$(date -u -d "$expires" +%s)" -gt "$now" ] || expires=$(date -u -d "@$((now + 86400))" +%Y-%m-%dT18:00:00Z)
    # 英数字と - _ だけ (JSON にそのまま入れられる)。変数は bash の中だけで、コマンドの引数には出さない
    password=$(head -c 18 /dev/urandom | base64 | tr '+/' '-_')
    printf '%s' "$password" | kc '
        user=$1 expires=$2
        password=$(cat)
        if [ -n "$(kcadm get users -r home-k8s -q username="$user" -q exact=true --fields id --format csv --noquotes)" ]; then
            echo "$user はもういる (just guest delete で消してから作り直す)" >&2
            exit 1
        fi
        # 期限の属性は user profile に宣言していない (unmanaged)。Keycloak 26 の既定 (Disabled) では保存されないので、
        # 管理者だけが読み書きできる ADMIN_EDIT にする (ゲストは自分の期限を見られず、変えられない)。DB を作り直した後もここで戻る
        kcadm get users/profile -r home-k8s | grep -q "\"unmanagedAttributePolicy\" : \"ADMIN_EDIT\"" \
            || kcadm update users/profile -r home-k8s -s unmanagedAttributePolicy=ADMIN_EDIT
        id=$(kcadm create users -r home-k8s -s username="$user" -s enabled=true -s "groups=[\"/guests\"]" \
            -s "requiredActions=[\"UPDATE_PASSWORD\",\"CONFIGURE_TOTP\"]" -s "attributes.guest-expires-at=[\"$expires\"]" -i)
        # 期限が入ったことを確かめる。入っていないゲストは CronJob が期限切れとみなしてすぐ消すので、ここで止める
        u=$(kcadm get "users/$id" -r home-k8s 2>"$cfg.err") || { cat "$cfg.err" >&2; exit 1; }
        if ! grep -q "\"guest-expires-at\" : \[ \"$expires\" \]" <<<"$u"; then
            kcadm delete "users/$id" -r home-k8s
            echo "$user に期限の属性 guest-expires-at を書けなかったので消した (realm の user profile の unmanagedAttributePolicy を見る)" >&2
            exit 1
        fi
        printf "{\"type\": \"password\", \"temporary\": true, \"value\": \"%s\"}" "$password" \
            | kcadm update "users/$id/reset-password" -r home-k8s -f -
    ' "$user" "$expires"
    cat <<EOF
$user を作った (グループ guests)。$(TZ=JST-9 date -d "$expires" '+%Y-%m-%d %H:%M %Z') に消える (それから 5 分以内)。
一時パスワード (この 1 回だけ表示する。ここから写してゲストに渡し、どこにも保存しない):

  $password

ゲストの最初のログインで、パスワードの変更と TOTP (認証アプリ) の設定を求められる。
  ログイン・パスワードと TOTP の変更: https://keycloak.taild2b611.ts.net/realms/home-k8s/account
  見せる UI:                        https://grafana.taild2b611.ts.net (Viewer)、https://backstage.taild2b611.ts.net (読むだけ)
取り消すとき: just guest delete $1
EOF
    ;;
list)
    [ $# -eq 0 ] || usage
    # 1 行に 1 人: ユーザー名、有効か、期限 (UTC の ISO 8601。無ければ空)
    rows=$(kc '
        gid=$(kcadm get groups -r home-k8s -q search=guests -q exact=true --fields id --format csv --noquotes)
        [ -n "$gid" ] || { echo "realm home-k8s にグループ guests が無い" >&2; exit 1; }
        kcadm get "groups/$gid/members" -r home-k8s --fields id,username,enabled --format csv --noquotes | while IFS=, read -r id user enabled; do
            # get users/<id> は標準エラーに空の行を出すので、失敗したときだけ見せる
            u=$(kcadm get "users/$id" -r home-k8s 2>"$cfg.err") || { cat "$cfg.err" >&2; exit 1; }
            at=$(grep -o "\"guest-expires-at\" : \[ \"[^\"]*\"" <<<"$u" | grep -o "[^\"]*\"$" | tr -d "\"" || true)
            echo "$user,$enabled,$at"
        done')
    # 見出しは全角 (1 文字 2 桁) なので、桁を手で合わせる
    printf '%s%28s%s%4s%s\n' ユーザー "" 有効 "" 期限
    [ -n "$rows" ] || exit 0
    while IFS=, read -r user enabled at; do
        # 期限が無い・読めない人は、次の CronJob で消える (guest- で始まらない人は guests から外れる)
        when="無し (次の実行で消える)"
        [ -z "$at" ] || when=$(TZ=JST-9 date -d "$at" '+%Y-%m-%d %H:%M %Z' 2>/dev/null) || when="読めない: $at (次の実行で消える)"
        printf '%-36s %-8s %s\n' "$user" "$enabled" "$when"
    done <<<"$rows"
    ;;
delete)
    [ $# -eq 1 ] || usage
    check_name "$1"
    kc '
        user=$1
        id=$(kcadm get users -r home-k8s -q username="$user" -q exact=true --fields id --format csv --noquotes)
        [ -n "$id" ] || { echo "$user はいない" >&2; exit 1; }
        # guests 以外の人 (admins・viewers) を消さない
        kcadm get "users/$id/groups" -r home-k8s --fields name --format csv --noquotes | grep -qx guests \
            || { echo "$user はグループ guests に入っていないので消さない" >&2; exit 1; }
        kcadm create "users/$id/logout" -r home-k8s
        kcadm delete "users/$id" -r home-k8s
        echo "$user のセッションを切って消した"
    ' "guest-$1"
    ;;
*) usage ;;
esac
