# Keycloak (namespace auth) の Secret と、Pod から issuer に届かせる CoreDNS の設定。公開レシピの `just up` (justfile) が呼ぶ内部用。
# operator・Keycloak・realm・PostgreSQL は ArgoCD が同期する (clusters/kind/argocd/apps/keycloak*.yaml)。構成は docs/cluster/keycloak.md。

# DB・初期の管理者・各 client の secret のファイル。無ければ `just up` が作り、以後は再利用する (DB はホストに残るので、作り直すとずれる)。
# 各 UI (単位 3〜6) も clients/<clientId> から自分の Secret を作る
keycloak_secret_dir := "/tmp/claude-1000/-home-yyamakura-k8s-workspace-home-k8s-gitops-deployer-platform/0672a78f-2795-41c8-9f24-1ef269a3e538/scratchpad/verify-share/keycloak"
# Keycloak の tailnet の名前 (issuer は https://<これ>/realms/home-k8s)。clusters/kind/auth/keycloak/keycloak.yaml の hostname と同じ
keycloak_host := "keycloak.taild2b611.ts.net"

# 上のディレクトリのファイルから Secret keycloak-postgres・keycloak-bootstrap-admin・keycloak-clients を作る。ArgoCD が Keycloak を同期する前に打つ
_keycloak-secrets:
    bash just/keycloak-secrets.sh "{{keycloak_secret_dir}}" "{{justfile_directory()}}" {{kube_context}}

# CoreDNS で keycloak の tailnet の名前を egress の Service (clusters/kind/auth/keycloak/tailnet.yaml) に読み替える
_coredns-tailnet:
    bash just/coredns-tailnet.sh {{keycloak_host}} keycloak-tailnet.auth.svc.cluster.local {{kube_context}}
