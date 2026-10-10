# Keycloak (namespace auth) の Secret と、Pod から issuer に届かせる CoreDNS の設定。公開レシピの `just up` (justfile) が呼ぶ内部用。
# operator・Keycloak・realm・PostgreSQL は ArgoCD が同期する (clusters/kind/argocd/apps/keycloak*.yaml)。構成は docs/cluster/keycloak.md。

# DB・初期の管理者・各 client の secret のファイル。無ければ `just up` が作り、以後は再利用する (DB はホストに残るので、作り直すとずれる)。
# 各 UI (単位 3〜6) も clients/<clientId> から自分の Secret を作る
keycloak_secret_dir := env_var("HOME") + "/.local/share/home-k8s/keycloak"
# Keycloak の tailnet の名前 (issuer は https://<これ>/realms/home-k8s)。clusters/kind/auth/keycloak/keycloak.yaml の hostname と同じ
keycloak_host := "keycloak.taild2b611.ts.net"

# 上のディレクトリのファイルから Secret keycloak-postgres・keycloak-bootstrap-admin・keycloak-clients を作る。ArgoCD が Keycloak を同期する前に打つ
_keycloak-secrets:
    bash just/keycloak-secrets.sh "{{keycloak_secret_dir}}" "{{justfile_directory()}}" {{kube_context}}

# CoreDNS で keycloak の tailnet の名前を egress の Service (clusters/kind/auth/keycloak/tailnet.yaml) に読み替える
_coredns-tailnet:
    bash just/coredns-tailnet.sh {{keycloak_host}} keycloak-tailnet.auth.svc.cluster.local {{kube_context}}

# ArgoCD・Grafana ×3・Temporal UI ×2・Backstage の OIDC の client の secret を、上のディレクトリの clients/<clientId> から各 namespace の Secret にする
# (argocd/argocd-oidc-keycloak、observability・dev・prod/grafana-oidc、dev・prod/temporal-oidc、backstage/backstage-oidc。docs/cluster/keycloak.md の「各 UI の OIDC」)。_keycloak-secrets の後に打つ
_oidc-secrets:
    bash just/oidc-secrets.sh "{{keycloak_secret_dir}}/clients" {{kube_context}}

# kubectl を Keycloak の OIDC でつなぐ context oidc@<クラスタ> を kubeconfig (~/.kube/config) に足す。既存の context・user は変えない (足すか、同じ名前を上書きするだけ)。
# ログインは kubelogin (kubectl oidc-login、開発用コンテナにある) が client kubernetes (public + PKCE) で行い、ID トークンを API server に渡す。
# 開発用コンテナにブラウザは無いので URL を表示するだけにし、人がブラウザで開く (callback は localhost:8000)。手順は docs/cluster/kube-oidc.md
_kube-oidc-context:
    kubectl config set-credentials oidc@{{kind_cluster}} --exec-api-version=client.authentication.k8s.io/v1 --exec-interactive-mode=IfAvailable --exec-command=kubectl --exec-arg=oidc-login --exec-arg=get-token --exec-arg=--oidc-issuer-url=https://{{keycloak_host}}/realms/home-k8s --exec-arg=--oidc-client-id=kubernetes --exec-arg=--oidc-pkce-method=S256 --exec-arg=--skip-open-browser
    kubectl config set-context oidc@{{kind_cluster}} --cluster={{kube_context}} --user=oidc@{{kind_cluster}}
