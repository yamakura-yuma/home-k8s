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
- ホストにも `just` 自体は必要 (単一の静的バイナリなので導入コストは小さい)。
  `devcontainer` 以外のレシピは、ホストで打っても自動で開発用コンテナの中で実行される
  (下の「ホストで打つか、コンテナで打つか」)。
- プロジェクト固有のツール一式 (`just`, `kubectl`, `kind`) は `flake.nix` /
  `flake.lock` で宣言・バージョン固定されており、コンテナ内のNixから導入する。
- 開発用コンテナは `--network=host` で起動するため、クラスタのAPIサーバーは
  WSL2ホストの `127.0.0.1` にそのままbindされ、コンテナの外
  (他リポジトリや通常のWSL2シェル) からも `~/.kube/config` を使ってアクセスできる。

## クイックスタート

```sh
just devcontainer up      # イメージをbuildし、開発用コンテナを起動 (nix profile install も実行)
just devcontainer shell   # コンテナにシェルで入る
just kind-up              # kubeadmベースのマルチノードクラスタを起動
kubectl --context kind-study-kind get nodes -o wide   # ノード状態を確認
just observe-up           # OTel Collector/Tempo/Prometheus/Loki/Grafana を入れる
just observe-password     # Grafana の admin パスワードを表示
just observe-share        # Grafana を一時的に trycloudflare.com で公開し、URL とパスワードを表示 (Ctrl-C で停止)
just observe-down         # 観測スタックを消す (トレース・メトリクス・ログはホストに残る)
just kind-down             # クラスタを削除
just devcontainer down    # 開発用コンテナを削除
```

### ホストで打つか、コンテナで打つか

どちらで打っても同じに動く。ホストで打った `just kind-up` などは、`justfile` の
`set shell` が `just/dev-shell` を経由させ、開発用コンテナ `home-k8s-dev` の中の
同じディレクトリで各行を `docker exec` する (端末に繋がっていれば `-it` なので、
`just observe-share` の Ctrl-C も効く)。コンテナの中で打てばそのまま実行する。
ホストかコンテナかは環境変数 `HOME_K8S_DEV` で見分ける。Dockerfile の `ENV` と
`docker exec -e` で立てる明示的な印で、ツールの有無 (`cloudflared` が無い、など) や
`/.dockerenv` (どのコンテナにもある) では「この開発用コンテナか」が分からないため。

- コンテナが無い・止まっているときは `just devcontainer up` を先に打つよう案内して止まる。
- コンテナは起動時の checkout と main repo だけをマウントする。別の worktree で打つと
  マウントされていない旨を出して止まるので、その checkout で
  `just devcontainer down && just devcontainer up` と作り直す。
- レシピの各行が別々に `docker exec` されるので、コンテナに頼るレシピは shebang を使わず
  行で書く (長い処理は `just/grafana-admin-secret.sh` のようにスクリプトへ出す)。

kind自体は試験で問われないためクラスタの起動/削除は just で自動化しているが、
`kubectl` は試験で直接問われるため意図的に just でラップしていない。素手で
`kubectl --context kind-study-kind ...` を叩いて操作すること。

利用可能なrecipe一覧は `just --list` で確認できる。資格取得のロードマップは
`docs/certification/roadmap.md` を参照。Claude Code のトレース・メトリクス・ログを見る
観測スタックは `docs/observability/claude-code-traces.md`、ダッシュボードの見方は
`docs/observability/claude-code-usage.md` を参照。
