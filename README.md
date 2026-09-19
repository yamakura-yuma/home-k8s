# home-k8s

Kubernetes資格 (KCNA → KCSA → CKA → CKAD → CKS → Kubestronaut/Golden Kubestronaut)
取得に向けた学習環境。演習問題や模擬シナリオは KodeKloud など外部プラットフォーム
を利用する前提で、このリポジトリは手元でクラスタを構築するための自動化 (IaC) のみを扱う。

クラスタ方式は **kind** に一本化している。kind は Kubernetes 本家
(kubernetes-sigs) が conformance test / e2e test 基盤として使っているツールで、
内部で実際に `kubeadm` を使ってクラスタを組むため、CKA で問われる kubeadm ベースの
運用・トラブルシュートの感覚に最も近い (設定の詳細は `docs/cluster/kind-cluster.md`)。

## 前提

- このリポジトリは **devcontainer で開く**こと (VS Code Dev Containers 拡張、
  または `devcontainer` CLI)。WSL2 本体には何もインストールしない。
- Nix 自体のブートストラップは devcontainer feature
  (`ghcr.io/devcontainers/features/nix`) が担う。`just`, `kubectl`, `kind`
  といったプロジェクト固有のツール一式は `flake.nix` / `flake.lock` で
  宣言・バージョン固定されており、コンテナ作成時に `postCreateCommand`
  (`nix profile install .`) で導入する。
- devcontainer は `--network=host` で起動するため、クラスタのAPIサーバーは
  WSL2ホストの `127.0.0.1` にそのままbindされ、devcontainerの外
  (他リポジトリや通常のWSL2シェル) からも `~/.kube/config` を使ってアクセスできる。

## クイックスタート

devcontainerを開くと `postCreateCommand` が自動的に `nix profile install .` を実行し、
`just`, `kubectl`, `kind` が使えるようになる (手動セットアップ不要)。

```sh
just kind-up      # kubeadmベースのマルチノードクラスタを起動 (control-plane x1 + worker x2)
kubectl --context kind-study-kind get nodes -o wide   # ノード状態を確認
just kind-down    # クラスタを削除
```

kind自体は試験で問われないためクラスタの起動/削除は just で自動化しているが、
`kubectl` は試験で直接問われるため意図的に just でラップしていない。素手で
`kubectl --context kind-study-kind ...` を叩いて操作すること。

利用可能なrecipe一覧は `just --list` で確認できる。資格取得のロードマップは
`docs/certification/roadmap.md` を参照。
