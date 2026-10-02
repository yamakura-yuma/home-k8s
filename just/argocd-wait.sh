#!/usr/bin/env bash
# just up の後段。root と子の Application がすべて Synced/Healthy になるまで待つ。
# 引数: <kube context> <ArgoCD の namespace>
set -euo pipefail
ctx="$1"
ns="$2"
# 子の Application の数は root が apps/ から作るまで決まらないので、root の Healthy も条件に入れる
for _ in $(seq 120); do
    states=$(kubectl --context "$ctx" -n "$ns" get applications \
        -o jsonpath='{range .items[*]}{.metadata.name}={.status.sync.status}/{.status.health.status}{"\n"}{end}')
    if grep -q '^root=Synced/Healthy$' <<<"$states" && ! grep -qv '=Synced/Healthy$' <<<"$states"; then
        kubectl --context "$ctx" -n "$ns" get applications
        exit 0
    fi
    sleep 10
done
echo "20 分待っても Synced/Healthy にならなかった。kubectl -n $ns get applications で確かめる" >&2
kubectl --context "$ctx" -n "$ns" get applications >&2
exit 1
