#!/usr/bin/env bash
# just up の一部。クラスタの中の Pod が Keycloak の issuer (https://<ホスト>/realms/home-k8s) を、ブラウザと同じ URL で引けるようにする。
# CoreDNS の Corefile に rewrite を 1 つ足し、<ホスト> の問い合わせを egress の Service (<Service>.<namespace>.svc.cluster.local) に読み替える。
# egress の Service は Tailscale の operator が tailnet の <ホスト> に中継する (clusters/kind/auth/keycloak/tailnet.yaml)。
# 理由と他の案は docs/cluster/keycloak.md の「Pod から issuer に届かせる」。
# Corefile は kind (kubeadm) が作る ConfigMap kube-system/coredns で、Git には置かない。印の行で囲んだ範囲だけを書き換えるので、打ち直しても同じになる。
# CoreDNS は reload プラグインで Corefile の変更を読み直す (kind の既定の Corefile にある)。Pod の作り直しは要らない。
# 引数: <ホスト> <読み替え先の名前> <kube context>
set -euo pipefail
host="$1"
target="$2"
ctx="$3"
begin='# home-k8s: begin (just/coredns-tailnet.sh)'
end='# home-k8s: end'
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
# 末尾の改行の有無で毎回「違う」にならないよう、改行 1 つにそろえる
printf '%s\n' "$(kubectl --context "$ctx" -n kube-system get configmap coredns -o 'jsonpath={.data.Corefile}')" >"$tmp/current"
# 前に足した範囲を除き、kubernetes プラグインの行の前に入れ直す。rewrite は kubernetes より前に置く (プラグインの順は CoreDNS の決まった順で、行の順ではないが、読みやすさのため)
awk -v host="$host" -v target="$target" -v begin="$begin" -v end="$end" '
    index($0, begin) { skip = 1; next }
    skip && index($0, end) { skip = 0; next }
    skip { next }
    /^[[:space:]]*kubernetes cluster\.local/ && !done {
        print "    " begin
        print "    rewrite stop {"
        print "        name exact " host " " target
        print "        answer auto"
        print "    }"
        print "    " end
        done = 1
    }
    { print }
    END { if (!done) exit 3 }
' "$tmp/current" >"$tmp/new" || {
    echo "CoreDNS の Corefile に kubernetes cluster.local の行が無い。kind の Corefile の形が変わったら just/coredns-tailnet.sh を直す" >&2
    exit 1
}
if cmp -s "$tmp/current" "$tmp/new"; then
    echo "CoreDNS: $host は $target に読み替え済み"
    exit 0
fi
# patch は data だけを替える (kubeadm が付けた metadata には触れない)。Corefile は秘密ではないが、長いのでファイルで渡す
{
    echo 'data:'
    echo '  Corefile: |'
    sed 's/^/    /' "$tmp/new"
} >"$tmp/patch.yaml"
kubectl --context "$ctx" -n kube-system patch configmap coredns --type merge --patch-file "$tmp/patch.yaml"
echo "CoreDNS: $host を $target に読み替える rewrite を入れた (reload で 30 秒ほどで効く)"
