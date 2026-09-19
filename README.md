# home-k8s

Kubernetes資格 (KCNA → KCSA → CKA → CKAD → CKS → Kubestronaut/Golden Kubestronaut)
取得に向けた学習環境。演習問題や模擬シナリオは KodeKloud など外部プラットフォーム
を利用する前提で、このリポジトリは手元でクラスタを構築するための自動化 (IaC) のみを扱う。

クラスタ方式は **kind** に一本化している。kind は Kubernetes 本家
(kubernetes-sigs) が conformance test / e2e test 基盤として使っているツールで、
内部で実際に `kubeadm` を使ってクラスタを組むため、CKA で問われる kubeadm ベースの
運用・トラブルシュートの感覚に最も近い (設定の詳細は `docs/cluster/kind-cluster.md`)。

## 前提

- ホスト(WSL2)には **`docker` のみ**あればよい。それ以外のツール
  (`just`, `kubectl`, `kind` 等) はすべて `Dockerfile` で構築する開発用コンテナの中に
  閉じ込め、WSL2 本体には何もインストールしない。
- `just` は開発用コンテナの起動/停止という「外側」の操作でしか使わないため、
  ホストにも `just` 自体は必要 (単一の静的バイナリなので導入コストは小さい)。
- プロジェクト固有のツール一式 (`just`, `kubectl`, `kind`) は `flake.nix` /
  `flake.lock` で宣言・バージョン固定されており、コンテナ内のNixから導入する。
- 開発用コンテナは `--network=host` で起動するため、クラスタのAPIサーバーは
  WSL2ホストの `127.0.0.1` にそのままbindされ、コンテナの外
  (他リポジトリや通常のWSL2シェル) からも `~/.kube/config` を使ってアクセスできる。

## クイックスタート

```sh
just devcontainer up      # イメージをbuildし、開発用コンテナを起動 (nix profile install も実行)
just devcontainer shell   # コンテナにシェルで入る
just kind-up              # (コンテナ内で) kubeadmベースのマルチノードクラスタを起動
kubectl --context kind-study-kind get nodes -o wide   # ノード状態を確認
just kind-down             # クラスタを削除
just devcontainer down    # 開発用コンテナを削除
```

kind自体は試験で問われないためクラスタの起動/削除は just で自動化しているが、
`kubectl` は試験で直接問われるため意図的に just でラップしていない。素手で
`kubectl --context kind-study-kind ...` を叩いて操作すること。

利用可能なrecipe一覧は `just --list` で確認できる。資格取得のロードマップは
`docs/certification/roadmap.md` を参照。
