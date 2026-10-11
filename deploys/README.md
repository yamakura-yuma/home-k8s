# deploys/

gitops-deployer の Workflow (git-activity) が PR で足し・消すディレクトリ。`deploys/<名前>/app.yaml` (Deployment と Service) ごとに、
ApplicationSet `gitops-deployer-apps` (`clusters/kind/argocd/apps/gitops-deployer-apps.yaml`) が Application `deploy-<名前>` を作り、
namespace `gitops-deployer-apps` に同期する。手で直さない。構成は [docs/cluster/gitops-deployer.md](../docs/cluster/gitops-deployer.md)。
