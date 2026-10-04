# Secret を作る・更新するスクリプトが source する関数 (grafana-secrets.sh・share-relay.sh・share-secrets.sh)。
# `kubectl apply` で Secret を入れると、中身 (data) が注釈 kubectl.kubernetes.io/last-applied-configuration に残り、
# `kubectl get secret -o yaml` に 2 回出る (#49)。注釈の付かない作り方にそろえる。

# put_secret <kube context> <namespace> <名前>
# 標準入力の Secret の manifest (`kubectl create secret ... --dry-run=client -o yaml`) を入れる。
# あれば replace、無ければ create。どちらも注釈を付けず、replace は metadata を丸ごと置き換えるので、
# 以前の apply が残した last-applied-configuration も消える。`replace --force` (消して作り直す) は使わない: 動いている Pod の下で消えるため。
# manifest はコマンドラインに出さず、here-string (一時ファイル) で渡す (ps に残る)。
put_secret() {
    local ctx="$1" ns="$2" name="$3" manifest
    manifest="$(cat)"
    if kubectl --context "$ctx" -n "$ns" get secret "$name" >/dev/null 2>&1; then
        kubectl --context "$ctx" -n "$ns" replace -f - <<<"$manifest"
    else
        kubectl --context "$ctx" -n "$ns" create -f - <<<"$manifest"
    fi
}

# create_secret_if_missing <kube context> <namespace> <名前> [kubectl create secret generic への引数...]
# あれば何もしない (人ごとの資格情報や署名鍵を、just up の打ち直しで消さない)。create は注釈を付けない。
create_secret_if_missing() {
    local ctx="$1" ns="$2" name="$3"
    shift 3
    if kubectl --context "$ctx" -n "$ns" get secret "$name" >/dev/null 2>&1; then
        return 0
    fi
    kubectl --context "$ctx" -n "$ns" create secret generic "$name" "$@"
}

# secret_exists <kube context> <namespace> <名前>
# Secret があれば 0。
secret_exists() {
    kubectl --context "$1" -n "$2" get secret "$3" >/dev/null 2>&1
}

# restart_share_pod_if_closed <kube context>
# share Pod の caddy は Secret share-host を環境変数で起動時にしか読まない (share-grafana は認証サービスが実行時に読む。#58)。share-host が無いまま起動した Pod (全経路 503、
# clusters/kind/share/entrypoint.sh) に読ませるには作り直すしかないので、**いま動いている caddy が閉じて起動した**ときだけ Pod を消す (#64)。
# 判定は caddy のログ (entrypoint.sh が閉じて起動するときに書く行)。Secret が初めてできたかではなく Pod の状態で決めるので、途中で失敗しても
# 打ち直せば直り、既に開いている Pod には触れない (作り直すと Quick Tunnel の URL が変わり、配った URL が使えなくなる)。
# Deployment の spec には触れず Pod だけを消す (rollout restart は template に注釈を足し、ArgoCD の selfHeal が戻して 2 度目の作り直しになりうる)。
# Pod が無い (ArgoCD の同期の前)・ログがまだ無いときは何もしない。呼ぶ前に、Secret が揃っていることを確かめる (揃わないまま作り直しても閉じたまま)。
# ログは一度変数に取る (grep -q で読み切らずに閉じると、kubectl が SIGPIPE で落ちて pipefail に当たる)。
share_closed_marker='全経路を 503 で拒否'
restart_share_pod_if_closed() {
    local ctx="$1" log
    log="$(kubectl --context "$ctx" -n share logs deploy/share -c caddy 2>/dev/null || true)"
    case "$log" in
        *"$share_closed_marker"*)
            echo "share Pod の caddy は Secret が揃う前に起動していた (全経路 503)。Pod を作り直して Secret を読ませる"
            kubectl --context "$ctx" -n share delete pod -l app=share --wait=false
            ;;
    esac
}
