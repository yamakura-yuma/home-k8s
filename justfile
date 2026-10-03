# ツールは開発用コンテナ (home-k8s-dev) の中にある。レシピの各行は just/dev-shell を通り、
# ホストで打たれたらコンテナの中で実行される (コンテナの中ならそのまま実行)。
# 1 行ごとに docker exec するので、コンテナに頼るレシピは shebang を使わず行で書くこと。
set shell := ["just/dev-shell", "-cu"]

# 操作する kind クラスタの context。検証用の別クラスタに向けるときだけ環境変数で変える
kube_context := env_var_or_default("HOME_K8S_KUBE_CONTEXT", "kind-study-kind")
# kind のクラスタ名 (context は `kind-<名前>`)。up・down が作る・消すのはこのクラスタだけ
kind_cluster := trim_start_match(kube_context, "kind-")

[private]
default:
    #!/usr/bin/env bash
    just --list

import 'just/argocd.just'
import 'just/backstage.just'
import 'just/devcontainer.just'
import 'just/kind.just'
import 'just/observability.just'
import 'just/orca-exporter.just'
import 'just/share.just'

# 公開レシピはこの 7 つだけ (just --list)。内部用は `_` 付きで隠してあり、just/ 以下に置く。
# up・down・show・share・ci はここ、orca-exporter と devcontainer は just/ 以下。

# 中身は just/ci.sh。ツールは nix (devShells.ci) から入るので、開発用コンテナを通さず shebang で直に走らせる。

# PR のゲートと同じ静的チェック (yamllint・helm template・kubeconform・kube-linter)。クラスタは触らない
ci:
    #!/usr/bin/env bash
    exec just/ci.sh

# kind のクラスタを作り、Backstage のイメージを入れ、ArgoCD を入れ、Secret を作って、観測スタック・Headlamp・Backstage の同期を待つ。
# 打ち直しても同じ状態に戻るだけ。

# kind のクラスタを作り、ArgoCD・Secret・観測スタック・Headlamp・Backstage を立ち上げる
up: _kind-up _backstage-image _argocd-install _grafana-secrets _share-relay-up _share-secrets && _headlamp-token
    kubectl --context {{kube_context}} apply -f clusters/kind/argocd/root.yaml
    @echo "ArgoCD が子の Application を同期するのを待つ (初回は image の pull で数分かかる)"
    bash just/argocd-wait.sh {{kube_context}} {{argocd_ns}}

# kind のクラスタと、headroom の中継 (ホスト側の caddy コンテナ) を削除する
down: _share-relay-down
    kind delete cluster --name {{kind_cluster}}

# grafana は localhost:3000 の viewer (閲覧用) で、grafana-admin は自分用。どちらも他の人には渡さない。
# 他の人に見せる資格情報と公開 URL は `just share add`・`just share get` (docs/cluster/share.md)。

# 接続先と資格情報を表示する (what: grafana | grafana-admin | argocd | headlamp | backstage。省略で全部)
show what="":
    {{ if what =~ '^(|grafana|grafana-admin|argocd|headlamp|backstage)$' { "" } else { error("usage: just show [grafana|grafana-admin|argocd|headlamp|backstage]") } }}
    @{{ if what == "" { "echo '== grafana (viewer) =='" } else { "" } }}
    @{{ if what =~ '^(|grafana)$' { 'echo "  URL:        http://localhost:3000"' } else { "" } }}
    @{{ if what =~ '^(|grafana)$' { 'echo "  ユーザー:   viewer"' } else { "" } }}
    @{{ if what =~ '^(|grafana)$' { 'echo "  パスワード: $(cat "' + grafana_viewer_password_file + '")"' } else { "" } }}
    @{{ if what == "" { "echo '== grafana-admin =='" } else { "" } }}
    @{{ if what =~ '^(|grafana-admin)$' { 'echo "  URL:        http://localhost:3000"' } else { "" } }}
    @{{ if what =~ '^(|grafana-admin)$' { 'echo "  ユーザー:   admin"' } else { "" } }}
    @{{ if what =~ '^(|grafana-admin)$' { 'echo "  パスワード: $(cat "' + grafana_password_file + '")"' } else { "" } }}
    @{{ if what == "" { "echo '== argocd =='" } else { "" } }}
    @{{ if what =~ '^(|argocd)$' { 'echo "  URL:        http://localhost:8080"' } else { "" } }}
    @{{ if what =~ '^(|argocd)$' { 'echo "  ユーザー:   admin"' } else { "" } }}
    @{{ if what =~ '^(|argocd)$' { 'echo "  パスワード: $(kubectl --context ' + kube_context + ' -n ' + argocd_ns + " get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d)\"" } else { "" } }}
    @{{ if what == "" { "echo '== headlamp =='" } else { "" } }}
    @{{ if what =~ '^(|headlamp)$' { 'echo "  URL:      http://localhost:4466"' } else { "" } }}
    @{{ if what =~ '^(|headlamp)$' { 'echo "  トークン: $(cat "' + headlamp_token_file + '")"' } else { "" } }}
    @{{ if what == "" { "echo '== backstage =='" } else { "" } }}
    @{{ if what =~ '^(|backstage)$' { 'echo "  URL:      http://localhost:7007"' } else { "" } }}
    @{{ if what =~ '^(|backstage)$' { 'echo "  ログイン: ゲスト"' } else { "" } }}

# 公開の本体は常駐する share Pod (clusters/kind/share)。人ごとの資格情報を Secret share-credentials に足す・消す・引く (docs/cluster/share.md)。
# 検査 (名前・--ttl・--permanent の併用) はクラスタに触れる前に just/share.sh が行う。

# 共有の資格情報を操作する (add <名前> [--ttl 30m|8h] [--permanent] | delete <名前> | list | get <名前> | rotate <名前> | prune | smoke)
share *args:
    @bash just/share.sh {{kube_context}} {{args}}
