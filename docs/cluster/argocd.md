# ArgoCD で観測スタック・Headlamp・Backstage を入れる

kind クラスタの観測スタック (Tempo・Prometheus・Loki・OTel Collector・Grafana)・Headlamp・Backstage は、
ArgoCD が GitHub の `yamakura-yuma/home-k8s` の `main` から同期する。values やダッシュボードを
変えるときは `main` に入れるだけで、クラスタに手で helm を打たない。ArgoCD 自身も ArgoCD が
管理する (self-manage)。helm を打つのは最初の 1 回 (`just up`) だけ。

状態の確認は今までどおり kubectl を素手で叩く (`kubectl --context kind-study-kind -n argocd get applications`)。

## 構成

```
just up
  0. kind のクラスタを作る (kind-config.yaml)。PV の保存先ディレクトリを mkdir する
     Backstage のイメージを build して kind load する (backstage/、just/backstage.just)
  1. helm upgrade --install argocd (argo-cd chart、clusters/kind/argocd/values.yaml)
  2. Secret grafana-admin / grafana-viewer / grafana-backstage / backstage-grafana
     (Git の外のファイルから。just/grafana-secrets.sh)
     headroom の中継 (ホストの docker コンテナ、just/share-relay.sh) と、share の Secret (just/share-secrets.sh)
  3. kubectl apply -f clusters/kind/argocd/root.yaml
  4. 全 Application が Synced/Healthy になるまで待つ
  5. Headlamp にログインするトークン (just/headlamp-token.sh)

Application root (clusters/kind/argocd/apps を同期する app-of-apps)
  ├── argocd          argo-cd chart          (1 と同じ chart・版・values。以後は自分自身を同期する)
  ├── storage         clusters/kind/storage/ (StorageClass と PV。sync wave -1。persistence.md)
  ├── tempo           ┐
  ├── prometheus      │ chart + clusters/kind/observability/*-values.yaml
  ├── loki            │
  ├── otel-collector  ┘
  ├── grafana         chart + grafana-values.yaml + dashboards/kustomization.yaml (ダッシュボードの ConfigMap)
  ├── headlamp        chart + clusters/kind/headlamp/ (values と読み取り専用の RBAC)
  ├── backstage       chart + clusters/kind/backstage/values.yaml (イメージは 0 で入れたもの)
  ├── share           clusters/kind/share/ (Deployment `share`。他の人に見せる公開の本体。share.md)
  └── sample-api      ApplicationSet。services/sample-api/ を Application sample-api-dev・sample-api-prod として
                      namespace dev・prod に同期する (環境ごとの展開。environments.md)
```

| Application | chart | 版 | namespace |
|---|---|---|---|
| argocd | `argo-cd` (<https://argoproj.github.io/argo-helm>) | 10.9.6 (ArgoCD v3.5.3) | argocd |
| tempo / prometheus / loki / otel-collector / grafana | [claude-code-traces.md](../observability/claude-code-traces.md) の表 | 同左 | observability |
| headlamp | `headlamp` (<https://kubernetes-sigs.github.io/headlamp>) | 0.45.0 | headlamp |
| backstage | `backstage` (<https://backstage.github.io/charts>) | 2.10.2 | backstage |
| share | なし (マニフェストのみ。caddy・認証・cloudflared ×3 の 1 Pod) | - | share |
| storage | なし (マニフェストのみ) | - | なし (cluster-scoped) |
| sample-api-dev / sample-api-prod (ApplicationSet `sample-api`) | なし (マニフェストのみ。`services/sample-api`) | - | dev / prod |

子の Application は chart のリポジトリと home-k8s の 2 つをソースに持つ (multi-source)。
home-k8s 側は `ref: values` で、chart に渡す values を `$values/clusters/kind/...` で指す。
chart のリポジトリは今までの `helm --repo` と同じく URL で直接指定する。

## 画面

| 画面 | URL | ログイン |
|---|---|---|
| ArgoCD | <http://localhost:8080> | `admin`、パスワードは `just show argocd` |
| Headlamp | <http://localhost:4466> | `just show headlamp` のトークン |
| Grafana | <http://localhost:3000> | [claude-code-traces.md](../observability/claude-code-traces.md) の「Grafana の認証」 |
| Backstage | <http://localhost:7007> | ゲスト ([backstage.md](backstage.md)) |

どれも NodePort (30080・30466・30300・30707) を kind の `extraPortMappings` で `127.0.0.1` に出している
([kind-cluster.md](kind-cluster.md))。
ArgoCD は 127.0.0.1 にしか出さないので TLS を付けず、`server.insecure: true` で HTTP にしている。

Headlamp は ServiceAccount `headlamp` のトークンでログインする。権限は組み込みの `view` と、
Node・PersistentVolume・StorageClass・CRD・ClusterRole を読むだけの `headlamp-cluster-read` で、
書き込みはできない (chart の既定の cluster-admin は外した)。Secret も読めない。操作の練習は kubectl で行う。

## 秘密の置き場所

公開 repo には秘密を置かない。ArgoCD の管理対象は Secret を名前で参照するだけ。

| Secret | 作るもの | 中身の出どころ |
|---|---|---|
| `observability/grafana-admin`・`grafana-viewer`・`grafana-backstage` | `just up` | `~/.local/share/home-k8s/observability/grafana-*-password` (無ければ作る) |
| `backstage/backstage-grafana` | `just up` | 上の `grafana-backstage-password` から、Backstage のプロキシが使う Basic 認証の値を作る ([backstage.md](backstage.md)) |
| `share/share-credentials`・`share-session-key`・`share-grafana`・`share-host` | `just up`・`just share add` | 資格情報はハッシュだけ (`just share` が足す。空で作り、あれば触らない)。`share-grafana` は `grafana-viewer-password` の写し。`share-host` は headroom の中継の宛先とトークン ([share.md](share.md)) |
| `headlamp/headlamp-token` | `just up` | kube-controller-manager が ServiceAccount `headlamp` のトークンを入れる。取り出して `~/.local/share/home-k8s/headlamp/token` に書く |
| `argocd/argocd-initial-admin-secret` | ArgoCD (初回の起動時) | ArgoCD が乱数で作る |

`headlamp-token` は ArgoCD の追跡ラベルが付かないので、ArgoCD は prune しない。

## 設計の判断

### ダッシュボードの渡し方

ArgoCD の Helm のソースには `--set-file` に当たるものが無いので、JSON は kustomize の
`configMapGenerator` で ConfigMap にし、Grafana の chart の `dashboardsConfigMaps` で
provider のディレクトリにマウントする。JSON はそのまま repo のファイルが正本で、
`generate.py`・`fetch.py` は前のまま使える。provider とフォルダの設定も前のまま。
詳しくは [claude-code-traces.md](../observability/claude-code-traces.md) の「読み込みの仕組み」。

ConfigMap は Application grafana に 2 つ目のソース (`path: clusters/kind/observability/dashboards`) として
入れ、chart と一緒に同期する。大きいものは 256 KiB を超えるので `ServerSideApply=true` にしている。

### self-manage の組み方

`just up` の 1 は helm でリリース `argocd` を入れ、Application `argocd` は同じリリース名・chart・版・values で
同じものを描く。描いた結果が helm の入れたものと同じなので、ArgoCD は最初の同期で既存のリソースに
追跡用の注釈 (`argocd.argoproj.io/tracking-id`。ArgoCD 3 の既定) を足すだけで、作り直さずに引き継ぐ。
版を上げるときは `just/argocd.just` の `argocd_chart_version` と `clusters/kind/argocd/apps/argocd.yaml` の
`targetRevision` を揃えて `main` に入れる (ArgoCD が自分で上げる)。
自分自身を消すと戻せないので、Application `argocd` だけは `prune: false` にしている。

### 同期の順序 (sync wave)

sync wave は Application `storage` の `-1` だけ。root が子のヘルスを引き継ぐ設定 (下) があるので、
ArgoCD は `storage` が Healthy になってから観測スタックの Application を作る。PV の保存先は
`WaitForFirstConsumer` なので、wave が無くても PVC が Pending で待って最後はそろうが、先に置けば
Pod が「PV が無い」でスケジュールされない時間が出ない ([persistence.md](persistence.md))。

ほかに順序が要るのは次の 2 つで、どちらも wave なしで満たせる。

- Grafana は Secret `grafana-admin`・`grafana-viewer`・`grafana-backstage` を、Backstage は `backstage-grafana` を読む。
  `just up` が root を apply する前に作る。Backstage のイメージも root より先に kind load する。
- Grafana はダッシュボードの ConfigMap をマウントする。同じ Application に入れたので、ArgoCD は
  ConfigMap を Deployment より先に apply する (種類ごとの既定の順序)。

OTel Collector は送り先 (Tempo・Prometheus・Loki) より先に起動しても、送れるまで再送するだけなので待たない。

root が子のヘルスを引き継ぐよう、`values.yaml` に Application のヘルスの評価 (`resource.customizations.health.argoproj.io_Application`) を入れている。
既定では Application のヘルスを評価せず、子が Degraded でも root は Healthy のままになる。

## 本番のクラスタに反映する手順

(ArgoCD を入れた PR #23 のとき。Backstage を足すときの手順は [backstage.md](backstage.md)。)
kind-config にポートを足したので、クラスタの作り直しが要る。その間は観測スタックが止まるが、
トレース・メトリクス・ログはホストの `~/.local/share/home-k8s/observability` に残り、
Grafana のパスワードも同じファイルを使い続ける。orca-exporter は再開後にまた送る。

```sh
just down      # 今のクラスタを消す
just up        # ポートを足したクラスタを作り、ArgoCD を入れ、Secret を作り、root を apply し、同期を待つ
kubectl --context kind-study-kind -n argocd get applications   # 全部 Synced / Healthy
just show argocd     # ArgoCD (localhost:8080) の admin のパスワード
just show headlamp   # Headlamp (localhost:4466) のトークン
```

Secret も `just up` の中で作るので、別に打つものは無い。
初回は image の pull で数分かかる。打ち直しても同じ状態に戻るだけで、Secret の中身も変わらない。

## main 以外のブランチで確かめる

ArgoCD は `main` しか見ないので、マージ前の変更は次のように確かめる。

1. 作業ブランチから検証用のブランチを切り、`clusters/kind/argocd` の中の `targetRevision: main` を
   そのブランチ名に置き換えてコミットし、push する (マージしない)。Backstage が読む `catalog-info.yaml` の
   URL (`clusters/kind/backstage/values.yaml` の `blob/main/`) も同じブランチに向ける。
2. 検証用のクラスタを、別の名前・別の hostPort の kind-config で先に `kind create cluster --name <名前> --config <設定>` で作る
   (`up` は標準の kind-config で作るので、動いているクラスタと hostPort がぶつかる)。検証用のブランチを checkout して
   `HOME_K8S_KUBE_CONTEXT=kind-<名前> just up` を打つ。作成済みのクラスタは作り直さず、ArgoCD の導入から進む
   (`kube_context` の既定は `kind-study-kind`。`up`・`down` が作る・消す kind のクラスタは、この context の `kind-` を除いた名前)。
3. 終わったら検証用のブランチとクラスタを消す。
