# kindクラスタの設定

`clusters/kind/kind-config.yaml` で定義しているクラスタの設定意図と、
今後変更する際に確認すべきポイントをまとめる。設定ファイル自体は
`clusters/kind/kind-config.yaml`、起動/削除は `just kind-up` / `just kind-down`
(`justfile` 参照)。

## なぜkindか

kind は Kubernetes 本家 (kubernetes-sigs) が conformance test / e2e test 基盤
として使っているツールで、各ノードが systemd 入りの Docker コンテナとして起動し、
コンテナ内部で実際に `kubeadm` を使ってクラスタを組み上げる。CKA
(Certified Kubernetes Administrator) の試験環境 (kubeadmベースの実クラスタ)
に最も近い構成が手元で作れるため採用している。

k3d/minikube も検討したが、複数方式を並行維持するコストが学習効率を下げると
判断し、kindのみに一本化した。

## 現在の設定 (`kind-config.yaml`)

| 項目 | 値 | 理由 |
|---|---|---|
| `name` | `study-kind` | クラスタ名。kubecontext名は `kind-study-kind` になる |
| `nodes` | control-plane x1, worker x2 | CKAで頻出する「control-planeとworkerの役割分担」「複数worker間のスケジューリング」を練習できる最小構成 |
| `networking.apiServerAddress` | `127.0.0.1` | 開発用コンテナ (`just devcontainer up`) を `--network=host` で起動しているため、WSL2ホストの `127.0.0.1` にそのままbindされ、他リポジトリ/別プロセスからも到達可能 (`README.md` 参照) |

## kind自体とkubectlの扱いの違い

- `kind create/delete cluster` は試験で問われる操作ではない (単なる環境構築) ため
  `just kind-up` / `just kind-down` で自動化している。
- `kubectl` は試験で直接操作を問われるため、意図的に just ではラップしていない。
  ノード確認などは素手で `kubectl --context kind-study-kind get nodes -o wide`
  のように叩くこと。

## 学習ポイント

- ノード内で `crictl`, `kubeadm`, `kubelet` の生ログ・設定を直接確認できる
  (`docker exec -it study-kind-control-plane bash`)。
- control-plane と worker の役割分担、`kubeadm join` の仕組みを observe しやすい。

## 将来の設定変更の候補 (未着手)

学習が進むにつれて、以下のような設定拡張が有効になったら追記していく。

- `extraPortMappings`: NodePort/Ingressの動作確認をホストから直接叩けるようにする。
- `kubeadmConfigPatches`: APIサーバーのfeature gateや証明書関連のオプションを試す。
- ノード数の変更: マルチetcd構成やcontrol-plane冗長化 (HA) の練習をしたくなった場合。

## 参考: 本物のkubeadmマルチVM構成について

WSL2単体ではネスト仮想化が不安定なため未対応。将来課題として
`docs/certification/roadmap.md` の「将来課題」に記載している。
