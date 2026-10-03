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
