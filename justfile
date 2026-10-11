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
import 'just/gitops-deployer.just'
import 'just/guest.just'
import 'just/keycloak.just'
import 'just/kind.just'
import 'just/observability.just'
import 'just/orca-exporter.just'
import 'just/tailscale.just'

# 公開レシピはこの 7 つだけ (just --list)。内部用は `_` 付きで隠してあり、just/ 以下に置く。
# up・down・show・ci はここ、orca-exporter・devcontainer・guest は just/ 以下。

# 中身は just/ci.sh。ツールは nix (devShells.ci) から入るので、開発用コンテナを通さず shebang で直に走らせる。

# PR のゲートと同じ静的チェック (yamllint・helm template・kubeconform・kube-linter)。クラスタは触らない
ci:
    #!/usr/bin/env bash
    exec just/ci.sh

# kind のクラスタを作り (API server は Keycloak の OIDC も受け入れる)、kubectl の OIDC の context oidc@<クラスタ> を kubeconfig に足し、Tailscale の operator の Secret を作り (ファイルが無ければここで止まる)、Backstage と gitops-deployer のイメージを入れ、ArgoCD を入れ、Secret を作り、
# ArgoCD・Grafana・Temporal UI・Backstage・Headlamp の OIDC の client の Secret と gitops-deployer の資格情報 (GitHub のトークン・E2E の client) の Secret を作り、Pod から Keycloak の issuer に届くよう CoreDNS を設定して、観測スタック・Headlamp・Backstage・Tailscale・Keycloak の同期を待つ。
# 打ち直しても同じ状態に戻るだけ。

# kind のクラスタを作り、ArgoCD・Secret・観測スタック・Headlamp・Backstage を立ち上げる
up: _kind-up _kube-oidc-context _tailscale-secrets _backstage-image _gitops-deployer-image _argocd-install _grafana-secrets _grafana-env-secrets _argocd-secrets _backstage-azure-secret _keycloak-secrets _oidc-secrets _gitops-deployer-secrets _coredns-tailnet && _headlamp-token
    kubectl --context {{kube_context}} apply -f clusters/kind/argocd/root.yaml
    @echo "ArgoCD が子の Application を同期するのを待つ (初回は image の pull で数分かかる)"
    bash just/argocd-wait.sh {{kube_context}} {{argocd_ns}}

# kind のクラスタを削除する
down:
    kind delete cluster --name {{kind_cluster}}

# grafana は localhost:3000 の viewer (閲覧用) で、grafana-admin は自分用。どちらも他の人には渡さない。
# 他の人 (ゲスト) は just guest add で Keycloak のアカウントを発行する (docs/cluster/guest-access.md。just share は廃止した、docs/cluster/share.md)。

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
    @{{ if what =~ '^(|backstage)$' { 'echo "  URL:      https://backstage.taild2b611.ts.net (Keycloak でログイン)"' } else { "" } }}
    @{{ if what =~ '^(|backstage)$' { 'echo "            http://localhost:7007 (ゲスト。読むだけ)"' } else { "" } }}
