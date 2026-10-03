#!/usr/bin/env bash
# just share の本体 (docs/cluster/share.md の「CLI」)。人ごとの資格情報を Secret share-credentials に足す・消す・引く。
#   bash just/share.sh <kube context> add <名前> [--ttl <30m|8h>] [--permanent]
#                                     delete <名前> | list | get <名前> | rotate <名前> | prune
# ツールは kubectl だけ (ほかは openssl・curl・date・base64)。パスワードは保存せず、Pod の認証サービス (share_auth.py) の
# hash_password で PBKDF2 のハッシュにして入れる。パスワードは kubectl exec の標準入力で渡し、コマンドラインにも注釈にも出さない。
# Secret への書き込みは項目ごとの `kubectl patch --type merge` (同時に 2 つの add が来ても互いを消さない)。
# 引数の検査 (名前・--ttl・--permanent の併用) はクラスタに触れる前に終える。終了コード: 2 は使い方の誤り、1 は実行できなかった。
set -euo pipefail

SECRET=share-credentials
TARGETS=(grafana headroom backstage)
DEFAULT_TTL_MINUTES=480 # 8h
MAX_TTL_MINUTES=1440    # 24h
NAME_RE='^[a-z0-9][a-z0-9-]{0,31}$'
HASH_RE='^pbkdf2_sha256\$[0-9]+\$[0-9a-f]{32}\$[0-9a-f]{64}$'

usage() {
    cat >&2 <<'EOF'
usage: just share add <名前> [--ttl <30m|8h>] [--permanent]   資格情報を作る (既定 8h、上限 24h。--permanent は特権 viewer で全体に 1 つ)
       just share delete <名前>                               消す (最大 2 秒で失効)
       just share list                                        名前・種別・残り時間と URL
       just share get <名前>                                  その人の情報と URL (パスワードは出ない)
       just share rotate <名前>                               特権 viewer のパスワードを作り直す
       just share prune                                       期限切れの項目を消す
       just share smoke                                       稼働中の share Pod の caddy に port-forward で当てて確かめる
EOF
    exit 2
}

# die <メッセージ> [終了コード]
die() {
    echo "$1" >&2
    exit "${2:-1}"
}

[ $# -ge 2 ] || usage
ctx="$1"
cmd="$2"
shift 2

script_dir="$(cd "$(dirname "$0")" && pwd)"
. "$script_dir/share-urls.sh"

kc() { kubectl --context "$ctx" -n share "$@"; }

check_name() {
    [[ "$1" =~ $NAME_RE ]] || die "名前は ^[a-z0-9][a-z0-9-]{0,31}\$ に合う文字列: $1" 2
}

# parse_ttl <期間> -> TTL_MINUTES。30m・8h の形で、0 と 24h 超は断る
parse_ttl() {
    [[ "$1" =~ ^([0-9]{1,4})([mh])$ ]] || die "--ttl は 30m・8h の形で指定する: $1" 2
    TTL_MINUTES=$((10#${BASH_REMATCH[1]}))
    [ "${BASH_REMATCH[2]}" = h ] && TTL_MINUTES=$((TTL_MINUTES * 60))
    [ "$TTL_MINUTES" -gt 0 ] || die "--ttl は 0 より長くする: $1" 2
    [ "$TTL_MINUTES" -le "$MAX_TTL_MINUTES" ] || die "--ttl の上限は 24h (期限のない資格情報は --permanent): $1" 2
}

# 項目 (name -> JSON)。Secret が読めなければ止まる (閉じる側に倒す。古い値は使わない)
declare -A ENTRY=()
NAMES=()
load_entries() {
    local out line
    out="$(kc get secret "$SECRET" -o go-template='{{range $k, $v := .data}}{{$k}}{{"\t"}}{{$v | base64decode}}{{"\n"}}{{end}}')" \
        || die "Secret $SECRET が読めない。just up を打ったか、HOME_K8S_KUBE_CONTEXT ($ctx) を確かめる"
    ENTRY=()
    while IFS= read -r line; do
        [ -n "$line" ] && ENTRY["${line%%$'\t'*}"]="${line#*$'\t'}"
    done <<<"$out"
    NAMES=()
    if [ "${#ENTRY[@]}" -gt 0 ]; then
        mapfile -t NAMES < <(printf '%s\n' "${!ENTRY[@]}" | sort)
    fi
}

# describe <名前> -> KIND (期限付き|特権|不正)・EXPIRES (epoch か空)・CREATED・EXPIRED (1|0)。項目は `add` が書く 1 行の JSON
describe() {
    local json="${ENTRY[$1]}"
    KIND=不正 EXPIRES="" CREATED="" EXPIRED=0
    local ex_re='"expires_at"[[:space:]]*:[[:space:]]*([0-9]+|null)'
    local pr_re='"privileged"[[:space:]]*:[[:space:]]*(true|false)'
    local cr_re='"created_at"[[:space:]]*:[[:space:]]*([0-9]+)'
    [[ "$json" =~ $cr_re ]] && CREATED="${BASH_REMATCH[1]}"
    if [[ "$json" =~ $pr_re ]]; then
        [ "${BASH_REMATCH[1]}" = true ] && KIND=特権 || KIND=期限付き
    fi
    if [[ "$json" =~ $ex_re ]]; then
        [ "${BASH_REMATCH[1]}" = null ] || EXPIRES="${BASH_REMATCH[1]}"
    fi
    # 認証サービスと同じ: 期限ちょうども拒否
    if [ -n "$EXPIRES" ] && [ "$now" -ge "$EXPIRES" ]; then EXPIRED=1; fi
}

duration() {
    local s="$1"
    if [ "$s" -ge 3600 ]; then
        printf '%d時間%d分' $((s / 3600)) $((s % 3600 / 60))
    elif [ "$s" -ge 60 ]; then
        printf '%d分' $((s / 60))
    else
        printf '%d秒' "$s"
    fi
}

stamp() { date -d "@$1" '+%Y-%m-%d %H:%M:%S %Z'; }

# remaining_text: describe の結果から「残り」の文字列
remaining_text() {
    if [ "$KIND" = 不正 ]; then
        echo "-"
    elif [ -z "$EXPIRES" ]; then
        echo "期限なし"
    elif [ "$EXPIRED" = 1 ]; then
        echo "期限切れ"
    else
        duration $((EXPIRES - now))
    fi
}

# patch_secret <data の JSON オブジェクト>: 項目ごとの merge patch。値 (hash) をコマンドラインに出さないよう、本人だけが読める一時ファイルで渡す
patch_secret() {
    local file
    file="$(umask 077; mktemp)"
    printf '{"data":%s}' "$1" >"$file"
    kc patch secret "$SECRET" --type merge --patch-file "$file" >/dev/null || { rm -f "$file"; die "Secret $SECRET に書けなかった"; }
    rm -f "$file"
}

# put_entry <名前> <hash> <expires_at (数字か null)> <privileged (true|false)>
put_entry() {
    local json
    json="$(printf '{"hash":"%s","expires_at":%s,"privileged":%s,"created_at":%s}' "$2" "$3" "$4" "$now" | base64 -w0)"
    patch_secret "{\"$1\":\"$json\"}"
}

# drop_entries <名前>...
drop_entries() {
    local name sep="" body=""
    for name in "$@"; do
        body+="$sep\"$name\":null"
        sep=,
    done
    patch_secret "{$body}"
}

# new_password: 128 bit の乱数 (hex)
new_password() { openssl rand -hex 16; }

# hash_password <パスワード>: Pod の認証サービスと同じ関数で PBKDF2 にする。パスワードは標準入力で渡す (引数に出ない)
hash_password() {
    local out
    out="$(printf '%s' "$1" | kc exec -i deploy/share -c auth -- python -B -c \
        'import sys; sys.path.insert(0, "/app"); import share_auth; print(share_auth.hash_password(sys.stdin.read()))')" \
        || die "share Pod の認証サービスでハッシュを作れなかった。Pod が Ready か確かめる (kubectl -n share get pod)"
    [[ "$out" =~ $HASH_RE ]] || die "認証サービスのハッシュの形が想定と違う"
    printf '%s\n' "$out"
}

# 外から引けるか。URL が出た直後は名前が引けず、そのとき開くと NXDOMAIN が手元のリゾルバに残るので、DoH (1.1.1.1) で引いて確かめる。
# 応答があればよい (認証なしの 401 でよい)
reachable() { curl -s -o /dev/null --max-time 5 --doh-url https://1.1.1.1/dns-query "$1/"; }

# wait_for_urls: 3 つの URL が出て、外から引けるまで待つ。SHARE_WAIT_SECONDS (既定 120) で諦める
wait_for_urls() {
    local deadline=$((SECONDS + ${SHARE_WAIT_SECONDS:-120})) target url ok
    while :; do
        ok=1
        for target in "${TARGETS[@]}"; do
            url="$(share_url "$ctx" "$target")" && reachable "$url" || ok=0
        done
        [ "$ok" = 1 ] && return 0
        [ "$SECONDS" -lt "$deadline" ] || return 1
        sleep 2
    done
}

print_urls() {
    share_urls "$ctx" | sed 's/^/  /' || true
}

# smoke の対象ごとの経路。OK は許可リストの内 (認証が通れば 200)、DENY は外 (認証が通っても 404)。PORT は caddy の待ち受け
declare -A SMOKE_OK=([grafana]=/api/search [headroom]=/health [backstage]=/)
declare -A SMOKE_DENY=([grafana]=/profile/password [headroom]=/v1/messages [backstage]=/api/proxy/grafana/api/health)
declare -A SMOKE_PORT=([grafana]=8081 [headroom]=8082 [backstage]=8083)
declare -A SMOKE_LOCAL=()
SMOKE_TTL_SECONDS=10
# 認証サービスは Secret を最大 2 秒 (AUTH_CACHE_TTL) 覚えるので、項目を書いた・消した直後の確認はこの秒数まで当て直す
SMOKE_SETTLE_SECONDS=5
SMOKE_CRED=""
SMOKE_FAILED=0

# smoke_check <期待するコード> <対象> <経路> [当て直す秒数]: 資格情報は $SMOKE_CRED (curl の設定ファイル。空なら認証なし)
smoke_check() {
    local want="$1" target="$2" path="$3" deadline=$((SECONDS + ${4:-0})) code auth=()
    [ -z "$SMOKE_CRED" ] || auth=(-K "$SMOKE_CRED")
    while :; do
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${auth[@]}" \
            "http://127.0.0.1:${SMOKE_LOCAL[$target]}$path" || true)"
        if [ "$code" = "$want" ] || [ "$SECONDS" -ge "$deadline" ]; then
            break
        fi
        sleep 0.5
    done
    if [ "$code" = "$want" ]; then
        printf '  ok  %-9s %-32s %s\n' "$target" "$path" "$code"
    else
        printf '  NG  %-9s %-32s %s (期待は %s)\n' "$target" "$path" "$code" "$want"
        SMOKE_FAILED=$((SMOKE_FAILED + 1))
    fi
}

# smoke_all <許可リストの内のコード> <外のコード> [当て直す秒数]: 3 つの対象の内と外に当てる
smoke_all() {
    local target
    for target in "${TARGETS[@]}"; do
        smoke_check "$1" "$target" "${SMOKE_OK[$target]}" "${3:-0}"
        smoke_check "$2" "$target" "${SMOKE_DENY[$target]}" "${3:-0}"
    done
}

# smoke: 稼働中の share Pod の caddy に port-forward で直に当て、認証と許可リストを確かめる。トンネル (外への公開) は使わない。
# 名前 smoke-<乱数> の資格情報を Secret に直接書き (期限は数秒)、終わったら (途中で落ちても) 消す。CI には入れない (docs/cluster/share.md)
smoke() {
    # dir・pf_pid・name は EXIT の trap が使うので local にしない (trap は関数を抜けたあとに走る)
    local password hash expires target port deadline t0
    dir="$(umask 077; mktemp -d)"
    name="smoke-$(openssl rand -hex 4)"
    # 項目を消すのは失敗しても止めない (Secret が読めないなら、そもそも書けていない)
    trap '[ -z "${pf_pid:-}" ] || kill "$pf_pid" 2>/dev/null; (drop_entries "$name") >/dev/null 2>&1 || true; rm -rf "$dir"' EXIT

    # ローカルのポートは kubectl に選ばせる (手元で 8081 などが使われていてもぶつからない)
    # kc (関数) を & にするとサブシェルの pid になり、kill しても kubectl が残るので、kubectl を直に起動する
    kubectl --context "$ctx" -n share port-forward deploy/share \
        ":${SMOKE_PORT[grafana]}" ":${SMOKE_PORT[headroom]}" ":${SMOKE_PORT[backstage]}" >"$dir/pf" 2>&1 &
    pf_pid=$!
    deadline=$((SECONDS + 15))
    while [ "$(grep -c '^Forwarding from 127\.0\.0\.1:' "$dir/pf")" -lt 3 ]; do
        kill -0 "$pf_pid" 2>/dev/null && [ "$SECONDS" -lt "$deadline" ] \
            || die "share Pod に port-forward できなかった: $(cat "$dir/pf")"
        sleep 0.5
    done
    for target in "${TARGETS[@]}"; do
        port="$(sed -n "s/^Forwarding from 127\.0\.0\.1:\([0-9]*\) -> ${SMOKE_PORT[$target]}\$/\1/p" "$dir/pf")"
        SMOKE_LOCAL[$target]="$port"
    done

    echo "== 認証なし: 許可リストの内も外も 401"
    smoke_all 401 401

    password="$(new_password)"
    hash="$(hash_password "$password")"
    printf 'user = "%s:%s"\n' "$name" "$password" >"$dir/cred"
    SMOKE_CRED="$dir/cred"
    expires=$(($(date +%s) + SMOKE_TTL_SECONDS))
    put_entry "$name" "$hash" "$expires" false
    echo "== 期限 ${SMOKE_TTL_SECONDS} 秒の資格情報 $name: 内は 200、外は 404"
    smoke_all 200 404 "$SMOKE_SETTLE_SECONDS"

    while [ "$(date +%s)" -le "$expires" ]; do sleep 0.5; done
    echo "== 期限後: 401"
    smoke_all 401 401

    put_entry "$name" "$hash" $(($(date +%s) + 60)) false
    echo "== 期限を 60 秒に延ばして 200 に戻してから delete"
    smoke_check 200 grafana "${SMOKE_OK[grafana]}" "$SMOKE_SETTLE_SECONDS"
    drop_entries "$name"
    t0=$SECONDS
    echo "== delete 後: 401"
    smoke_check 401 grafana "${SMOKE_OK[grafana]}" "$SMOKE_SETTLE_SECONDS"
    echo "  (delete から 401 になるまで約 $((SECONDS - t0)) 秒)"
    smoke_all 401 401

    if [ "$SMOKE_FAILED" -gt 0 ]; then
        die "smoke: $SMOKE_FAILED 件が期待と違う"
    fi
    echo "smoke: すべて期待どおり"
}

now="$(date +%s)"

case "$cmd" in
add)
    name="" ttl="" ttl_given=0 permanent=0
    while [ $# -gt 0 ]; do
        case "$1" in
        --ttl)
            [ $# -ge 2 ] || die "--ttl に期間が要る (30m・8h)" 2
            ttl="$2"
            ttl_given=1
            shift 2
            ;;
        --permanent)
            permanent=1
            shift
            ;;
        -*) die "知らないオプション: $1" 2 ;;
        *)
            [ -z "$name" ] || usage
            name="$1"
            shift
            ;;
        esac
    done
    [ -n "$name" ] || usage
    check_name "$name"
    if [ "$permanent" = 1 ]; then
        [ "$ttl_given" = 0 ] || die "--permanent と --ttl は同時に指定できない" 2
        expires=null
        privileged=true
    else
        if [ "$ttl_given" = 1 ]; then parse_ttl "$ttl"; else parse_ttl "${DEFAULT_TTL_MINUTES}m"; fi
        expires=$((now + TTL_MINUTES * 60))
        privileged=false
    fi

    load_entries
    # 期限切れは認証が拒否するので残っても害はないが、見た目のために先に消す
    expired=()
    for n in "${NAMES[@]}"; do
        describe "$n"
        [ "$EXPIRED" = 1 ] && expired+=("$n")
    done
    if [ "${#expired[@]}" -gt 0 ]; then
        drop_entries "${expired[@]}"
        echo "期限切れの項目を消した: ${expired[*]}"
        for n in "${expired[@]}"; do unset 'ENTRY[$n]'; done
    fi
    [ -z "${ENTRY[$name]+x}" ] || die "$name は既にある。作り直すなら先に just share delete $name"
    if [ "$permanent" = 1 ]; then
        for n in "${!ENTRY[@]}"; do
            describe "$n"
            [ "$KIND" != 特権 ] || die "特権 viewer は全体で 1 つだけで、既に $n がある (忘れたなら rotate、要らなければ delete)"
        done
    fi

    password="$(new_password)"
    # 代入にするのは、$(...) を引数に直接置くと hash_password が失敗しても put_entry が空の hash で走るため (set -e が効かない)
    hash="$(hash_password "$password")"
    put_entry "$name" "$hash" "$expires" "$privileged"

    # パスワードはここでしか出せない。URL を待つ間に中断されても失わないよう、先に表示する
    echo "資格情報を作りました"
    echo "  名前:       $name"
    echo "  パスワード: $password (保存はハッシュだけ。この 1 回しか表示しない)"
    if [ "$permanent" = 1 ]; then
        echo "  期限:       なし (特権 viewer)"
    else
        echo "  期限:       $(stamp "$expires") ($(duration $((TTL_MINUTES * 60))) 後)"
    fi
    echo "URL が外から引けるのを待つ …"
    if wait_for_urls; then
        echo "URL (名前・パスワードは 3 つの URL でそれぞれ打つ):"
    else
        echo "外から引けると確かめられなかった URL がある (Pod の起動直後か、Quick Tunnel の登録待ち)。少し待ってから just share get $name:" >&2
    fi
    print_urls
    ;;
delete)
    [ $# -eq 1 ] || usage
    check_name "$1"
    load_entries
    [ -n "${ENTRY[$1]+x}" ] || die "$1 という資格情報は無い (just share list)"
    drop_entries "$1"
    echo "$1 を消しました (最大 2 秒で効かなくなる)"
    ;;
list)
    [ $# -eq 0 ] || usage
    load_entries
    if [ "${#NAMES[@]}" -eq 0 ]; then
        echo "資格情報は無い (just share add)"
    else
        printf '%-32s %-8s %-14s %s\n' 名前 種別 残り 状態
        for n in "${NAMES[@]}"; do
            describe "$n"
            state=有効
            [ "$EXPIRED" = 1 ] && state=期限切れ
            [ "$KIND" = 不正 ] && state="不正 (認証は拒否する。delete で消す)"
            printf '%-32s %-8s %-14s %s\n' "$n" "$KIND" "$(remaining_text)" "$state"
        done
    fi
    echo "URL:"
    print_urls
    ;;
get)
    [ $# -eq 1 ] || usage
    check_name "$1"
    load_entries
    [ -n "${ENTRY[$1]+x}" ] || die "$1 という資格情報は無い (just share list)"
    describe "$1"
    echo "名前: $1"
    echo "種別: $KIND"
    [ -z "$CREATED" ] || echo "作成: $(stamp "$CREATED")"
    if [ -n "$EXPIRES" ]; then echo "期限: $(stamp "$EXPIRES")"; elif [ "$KIND" = 特権 ]; then echo "期限: なし"; fi
    echo "残り: $(remaining_text)"
    [ "$EXPIRED" = 0 ] || echo "状態: 期限切れ (認証は拒否する。prune か add で消える)"
    echo "パスワードは保存していないので出ない (期限付きは delete → add、特権は rotate)"
    echo "URL:"
    print_urls
    ;;
rotate)
    [ $# -eq 1 ] || usage
    check_name "$1"
    load_entries
    [ -n "${ENTRY[$1]+x}" ] || die "$1 という資格情報は無い (just share list)"
    describe "$1"
    [ "$KIND" = 特権 ] || die "rotate は特権 viewer だけに使える。期限付きの $1 は delete → add で作り直す" 2
    password="$(new_password)"
    hash="$(hash_password "$password")"
    put_entry "$1" "$hash" null true
    echo "パスワードを作り直しました (古いパスワードは最大 2 秒で効かなくなる)"
    echo "  名前:       $1"
    echo "  パスワード: $password (保存はハッシュだけ。この 1 回しか表示しない)"
    echo "  期限:       なし (特権 viewer)"
    echo "URL:"
    print_urls
    ;;
prune)
    [ $# -eq 0 ] || usage
    load_entries
    expired=()
    for n in "${NAMES[@]}"; do
        describe "$n"
        [ "$EXPIRED" = 1 ] && expired+=("$n")
    done
    if [ "${#expired[@]}" -eq 0 ]; then
        echo "期限切れの項目は無い"
    else
        drop_entries "${expired[@]}"
        echo "期限切れの項目を消した: ${expired[*]}"
    fi
    ;;
smoke)
    [ $# -eq 0 ] || usage
    smoke
    ;;
*) usage ;;
esac
