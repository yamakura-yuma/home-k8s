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

# 公開レシピはこの 6 つだけ (just --list)。内部用は `_` 付きで隠してあり、just/ 以下に置く。
# up・down・show・share はここ、orca-exporter と devcontainer は just/ 以下。

# kind のクラスタを作り、Backstage のイメージを入れ、ArgoCD を入れ、Secret を作って、観測スタック・Headlamp・Backstage の同期を待つ。
# 打ち直しても同じ状態に戻るだけ。

# kind のクラスタを作り、ArgoCD・Secret・観測スタック・Headlamp・Backstage を立ち上げる
up: _kind-up _backstage-image _argocd-install _grafana-secrets && _headlamp-token
    kubectl --context {{kube_context}} apply -f clusters/kind/argocd/root.yaml
    @echo "ArgoCD が子の Application を同期するのを待つ (初回は image の pull で数分かかる)"
    bash just/argocd-wait.sh {{kube_context}} {{argocd_ns}}

# kind のクラスタを削除する
down:
    kind delete cluster --name {{kind_cluster}}

# grafana は viewer の接続先 (共有中なら公開 URL) で、共有相手に渡してよい。grafana-admin は自分用で渡さない。

# 接続先と資格情報を表示する (what: grafana | grafana-admin | argocd | headlamp | backstage。省略で全部)
show what="":
    {{ if what =~ '^(|grafana|grafana-admin|argocd|headlamp|backstage)$' { "" } else { error("usage: just show [grafana|grafana-admin|argocd|headlamp|backstage]") } }}
    @{{ if what == "" { "echo '== grafana (viewer) =='" } else { "" } }}
    @{{ if what =~ '^(|grafana)$' { "bash just/observe-show-connection.sh \"" + grafana_viewer_password_file + "\" \"" + share_state_file + "\"" } else { "" } }}
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

# grafana は viewer のパスワードを作り直してから、headroom と backstage は使い捨てのパスワードを作って公開する。
# URL が開けるようになった時点で URL と資格情報を表示する。Ctrl-C で止めれば URL は無効になる。

# localhost のサービスを Cloudflare Quick Tunnel で一時公開する (what: grafana (localhost:3000) | headroom (localhost:8787) | backstage (localhost:7007))
share what="grafana":
    {{ if what =~ '^(grafana|headroom|backstage)$' { "" } else { error("usage: just share [grafana|headroom|backstage]") } }}
    @bash just/observe-share.sh "{{grafana_password_file}}" "{{grafana_viewer_password_file}}" {{observe_ns}} "{{share_log_file}}" "{{share_state_file}}" {{what}}
