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
just up                   # kind のクラスタを作り (control-plane x1 + worker x2)、ArgoCD・Secret・観測スタック
                          # (OTel Collector/Tempo/Prometheus/Loki/Grafana)・Headlamp・Backstage を立ち上げて同期を待つ
kubectl --context kind-study-kind get nodes -o wide   # ノード状態を確認
just show                 # Grafana・ArgoCD・Headlamp・Backstage の接続先と資格情報を全部表示 (下の表)
just share                # Grafana を一時的に trycloudflare.com で公開し、URL と閲覧用 (viewer) のパスワードを表示 (Ctrl-C で停止)
just orca-exporter install  # Orca のオーケストレーションを観測スタックに送る exporter を systemd で常駐させる (ホストで動く例外)
just down                 # クラスタを削除
just devcontainer down    # 開発用コンテナを削除
```

公開レシピは次の 6 つだけ (`just --list`)。kind の作成や ArgoCD の導入などの内部用は `_` 付きで隠してあり、
`up` が呼ぶ。サブコマンド形のものは、引数を間違えると使い方を出して止まる。

| レシピ | 中身 |
|---|---|
| `just up` | kind のクラスタを作り、Backstage のイメージを build して kind load し、ArgoCD を入れ、Secret を作り、同期を待つ。打ち直しても同じ状態に戻る |
| `just down` | kind のクラスタを削除する |
| `just show [grafana\|grafana-admin\|argocd\|headlamp\|backstage]` | 接続先と資格情報を表示する。省略で全部。`grafana` は共有相手に渡してよい viewer の接続先 (公開中なら公開 URL、止めていれば localhost:3000)、`grafana-admin` は自分用 |
| `just share` | Grafana (localhost:3000) だけを Cloudflare Quick Tunnel で一時的に公開する |
| `just orca-exporter <install\|uninstall\|status>` | Orca の exporter を systemd のユーザーユニットとして入れる・消す・状態を見る |
| `just devcontainer <up\|shell\|down>` | 開発用コンテナを起動する・シェルで入る・削除する |

### ホストで打つか、コンテナで打つか

どちらで打っても同じに動く。ホストで打った `just up` などは、`justfile` の
`set shell` が `just/dev-shell` を経由させ、開発用コンテナ `home-k8s-dev` の中の
同じディレクトリで各行を `docker exec` する (端末に繋がっていれば `-it` なので、
`just share` の Ctrl-C も効く)。コンテナの中で打てばそのまま実行する。
ホストかコンテナかは環境変数 `HOME_K8S_DEV` で見分ける。Dockerfile の `ENV` と
`docker exec -e` で立てる明示的な印で、ツールの有無 (`cloudflared` が無い、など) や
`/.dockerenv` (どのコンテナにもある) では「この開発用コンテナか」が分からないため。

- コンテナが無い・止まっているときは `just devcontainer up` を先に打つよう案内して止まる。
- コンテナは起動時の checkout と main repo だけをマウントする。別の worktree で打つと
  マウントされていない旨を出して止まるので、その checkout で
  `just devcontainer down && just devcontainer up` と作り直す。
- レシピの各行が別々に `docker exec` されるので、コンテナに頼るレシピは shebang を使わず
  行で書く (長い処理は `just/grafana-secrets.sh` のようにスクリプトへ出す)。

kind自体は試験で問われないためクラスタの起動/削除は just で自動化しているが、
`kubectl` は試験で直接問われるため意図的に just でラップしていない。素手で
`kubectl --context kind-study-kind ...` を叩いて操作すること。

観測スタック・Headlamp・Backstage は ArgoCD が GitHub の `main` から同期する (GitOps)。values や
ダッシュボードの変更は `main` に入れれば反映される。ArgoCD の入れ方・構成・本番のクラスタへの
反映手順は `docs/cluster/argocd.md`。データの永続化の設計 (StorageClass・PV・PVC) は `docs/cluster/persistence.md`。
Backstage (localhost:7007) は home-k8s のエンティティのページに Grafana のダッシュボードの一覧を出す。
アプリは `backstage/` にあり、イメージは `just up` が手元で build する。構成と本番への反映手順は `docs/cluster/backstage.md`。

利用可能なrecipe一覧は `just --list` で確認できる。資格取得のロードマップは
`docs/certification/roadmap.md` を参照。Claude Code のトレース・メトリクス・ログを見る
観測スタックは `docs/observability/claude-code-traces.md`、ダッシュボードの見方は
`docs/observability/claude-code-usage.md`、ハーネスの直し方は
`docs/observability/claude-code-improve.md` を参照。困りごとからどのダッシュボードを見るかは
`docs/observability/playbook.md` (Grafana のホーム「Claude Code はじめに」にも同じ内容の短縮版がある)。
OTel の設定 (環境変数) ごとに何が届くかは `docs/observability/claude-code-settings.md`
(Grafana のフォルダ「Claude Code 設定項目別」)。
coordinator が Orca でワーカーをどう回したか (手戻り) は `docs/observability/orca-orchestration.md`
(Grafana の「Orca orchestration」)。exporter は `orca` CLI を使うためホスト (WSL) で動かし、
`just orca-exporter` だけは開発用コンテナに転送しない。
Kubernetes・OTel Collector・Claude Code の grafana.com 公開ダッシュボード (フォルダ「grafana.com / …」) の構成と更新手順は
`docs/observability/grafana-com-dashboards.md`。
