# データの永続化

kind のクラスタを `just down` → `just up` で作り直しても、状態を持つアプリのデータが残るようにする。
手段は StorageClass・PV・PVC を正しく使う形にそろえる (CKA の Storage の範囲をそのまま練習できるように)。

下の推奨案 D を実装した。マニフェストは `clusters/kind/storage/` と各 `clusters/kind/observability/*-values.yaml`、
Tempo を例にしたつながりの説明は [claude-code-traces.md](../observability/claude-code-traces.md) の「永続化の仕組み」。
実クラスタでの確認結果は末尾の「確認の結果」。

## 結論

**静的に作る `local` PV + PVC + StorageClass `home-k8s-local` (no-provisioner、`WaitForFirstConsumer`、`Retain`)**
にする。PV は今の hostPath と同じノード上のディレクトリを指すので、既存のトレース・メトリクス・ログは
そのまま引き継げる。Grafana も同じ形で `grafana.db` を残す。

```
WSL2 ホスト                                  kind ノード study-kind-worker            クラスタ (ArgoCD が Git から作る)
~/.local/share/home-k8s/observability/  ──extraMounts──▶ /var/local/home-k8s/observability/
  ├── tempo/        ◀──────────────────────────────────  tempo/        ◀── PV tempo      ◀─claimRef─ PVC storage-tempo-0  ◀── StatefulSet tempo
  ├── prometheus/   ◀──────────────────────────────────  prometheus/   ◀── PV prometheus ◀─claimRef─ PVC prometheus-server ◀── Deployment prometheus-server
  ├── loki/         ◀──────────────────────────────────  loki/         ◀── PV loki       ◀─claimRef─ PVC storage-loki-0   ◀── StatefulSet loki
  └── grafana/ (新規)◀─────────────────────────────────  grafana/      ◀── PV grafana    ◀─claimRef─ PVC grafana          ◀── Deployment grafana
                                                                           │
                                     ノードのラベル home-k8s/observability-storage=true ◀── PV の nodeAffinity
```

クラスタを消すと PV・PVC のオブジェクトは消えるが、ホストのディレクトリは残る。
`just up` のあと ArgoCD が同じ名前の PV と PVC を作り直し、同じディレクトリにまた結び付く。

## 実装前の状態

| アプリ | 保存の仕方 | ノードへの固定 | 所有者の調整 | 作り直し後 |
|---|---|---|---|---|
| Tempo | hostPath `/var/local/home-k8s/observability/tempo` → `/var/tempo` (`extraVolumes`) | `nodeSelector: home-k8s/observability-storage` | initContainer で `chown 10001` | 残る |
| Prometheus | hostPath `.../prometheus` → `/prometheus-data` (`persistentVolume.enabled: false`、`storagePath`) | 同上 | initContainer で `chown 65534` | 残る |
| Loki | hostPath `.../loki` → `/var/loki` (`singleBinary.persistence.dataVolumeParameters`) | 同上 | initContainer で `chown 10001` | 残る |
| Grafana | なし (emptyDir) | なし | なし | **消える** (UI で作ったダッシュボード・設定・ユーザー) |

- ホストとノードのつなぎは `clusters/kind/kind-config.yaml` の 1 つ目の worker の `extraMounts`
  (`${HOME}/.local/share/home-k8s/observability` → `/var/local/home-k8s/observability`) とラベル。
- 各 values は `clusters/kind/observability/{tempo,prometheus,loki,grafana}-values.yaml`。
- Grafana は「設定は provisioning で入るので永続化しない」前提で、viewer ユーザーはサイドカーが
  起動のたびに作り直している (`grafana-values.yaml` の `extraContainers`)。

今の形は動くが、PV・PVC を使っていない。hostPath を Pod から直接書くので、
StorageClass・PV・PVC・`nodeAffinity`・`reclaimPolicy` を触る機会が無い。Kubernetes の文書も
hostPath は避け、`local` PV を使うよう勧めている (出典 1)。

## 選択肢の比較

| 案 | 中身 | 作り直し後も残る | 既存データの引き継ぎ | 学習 (SC・PV・PVC) | ArgoCD との相性 | 今後のアプリへの広げやすさ |
|---|---|---|---|---|---|---|
| A. いまの hostPath 直書き | Pod の volume に hostPath | ○ | ○ (そのまま) | × PV・PVC が出てこない | ○ | △ アプリごとに nodeSelector と chown |
| B. kind 標準の local-path (`standard`) をそのまま | 動的プロビジョニング | **×** (ノードのディレクトリごと消える) | × | ○ 動的プロビジョニング | ○ | ○ |
| C. local-path の `nodePathMap` を extraMounts 先に向ける | 動的プロビジョニング + 保存先の付け替え | △ (`Retain` の SC を別に作り、`pathPattern` を固定名にする必要がある) | △ (固定名にするには `allowUnsafePathPattern`) | ○ | △ kind が入れる ConfigMap を後から書き換える | ○ |
| **D. 静的 `local` PV + PVC + SC (推奨)** | PV を Git に書き、PVC と `claimRef` で 1 対 1 に結ぶ | ○ | ○ (PV の `path` を今のディレクトリに向ける) | ◎ SC・PV・PVC・`nodeAffinity`・WFFC・`Retain`・事前バインド | ○ PV も SC も Git の YAML | ○ PV を 1 枚足すだけ |
| E. 静的 `hostPath` PV + PVC | D の `local` を `hostPath` に替える | ○ | ○ | ○ | ○ | ○ |
| F. NFS (ホストの NFS サーバー + nfs-subdir-external-provisioner / csi-driver-nfs) | ネットワーク越しの RWX | ○ | △ (NFS に移す) | ○ RWX を試せる | ○ | ○ |
| G. 分散ストレージ (Longhorn・Rook/Ceph) | ノードのディスクを束ねる | △ (kind のノードは毎回消えるので、データをノードの外に置く工夫が別に要る) | × | ○ CSI | ○ | ○ |

案ごとの判断:

- **B** は kind の `standard` が `reclaimPolicy: Delete` で (出典 3)、local-path-provisioner は PV を消すと
  teardown で `rm -rf "$VOL_DIR"` を打つ (いまのクラスタの `local-path-config` で確認)。保存先も
  ノードのコンテナの中 (`/var/local-path-provisioner`) なので、クラスタを消せば消える。
  永続化の要らない一時的な PVC には今後も使ってよい。
- **C** は保存先のディレクトリ名が既定で `<PV 名>_<namespace>_<PVC 名>` になり、PV 名は
  `pvc-<UID>` でクラスタごとに変わる (出典 4)。毎回同じディレクトリを使うには `pathPattern` を
  固定し、さらに名前空間の接頭辞の検査を `allowUnsafePathPattern` で外すことになる。kind が作る
  `local-path-storage/local-path-config` を `just up` の後で書き換える手順も増える。
- **E** は D とほぼ同じだが、Kubernetes の文書は hostPath PV を「単一ノードのテスト用。複数ノードでは動かない」
  としている (出典 2)。`local` は PV の `nodeAffinity` でスケジューラにノードの制約を伝えられるので、
  アプリ側の `nodeSelector` が要らなくなる (出典 1)。
- **F** は WSL2 に NFS サーバーを立てる手間と、既存データの移し替えが要る。RWX が要るアプリが出たら検討する。
- **G** は比較のために載せただけで採用しない。

## 推奨案 D の中身

### 置くもの

```
clusters/kind/storage/                 (新規。Application storage が同期する)
  storageclass.yaml   StorageClass home-k8s-local
  pv-tempo.yaml       PV tempo       → /var/local/home-k8s/observability/tempo
  pv-prometheus.yaml  PV prometheus  → .../prometheus
  pv-loki.yaml        PV loki        → .../loki
  pv-grafana.yaml     PV grafana     → .../grafana
clusters/kind/argocd/apps/storage.yaml (新規。Application storage)
```

```yaml
# StorageClass (clusters/kind/storage/storageclass.yaml からコメントを除いたもの)
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: home-k8s-local
provisioner: kubernetes.io/no-provisioner   # 動的プロビジョニングしない (出典 1・3)
volumeBindingMode: WaitForFirstConsumer     # Pod のスケジュールまでバインドを待つ (出典 1)
reclaimPolicy: Retain
---
# PV (1 アプリ 1 枚)
apiVersion: v1
kind: PersistentVolume
metadata:
  name: tempo
spec:
  capacity:
    storage: 10Gi                 # local は容量を強制しない。目安として PVC の要求以上にする
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: home-k8s-local
  claimRef:                       # この PVC 以外とは結ばない (事前バインド。出典 2)
    namespace: observability
    name: storage-tempo-0
  local:
    path: /var/local/home-k8s/observability/tempo
  nodeAffinity:                   # local では必須 (出典 1)
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: home-k8s/observability-storage
              operator: In
              values: ["true"]
```

### 評価軸ごとの扱い

| 軸 | D での扱い |
|---|---|
| 作り直し後も残る | データはホストのディレクトリにあり、PV オブジェクトは Git から毎回作り直す。no-provisioner なので Kubernetes がディレクトリを消すことは無い |
| 既存データの引き継ぎ | PV の `local.path` を今の hostPath と同じパスにする。マウント先も今と同じなので (下の表)、ディレクトリの移動は要らない |
| reclaimPolicy | `Retain`。PVC を消しても PV は `Released` になるだけで中身は残る (出典 2)。`kind down` ではそもそも PV ごと消えて作り直されるので、この状態にはならない |
| 所有権 | `local` は Pod の `fsGroup` で volume のグループを付け替える (出典 6)。hostPath は付け替えない。chart はすでに `fsGroup` を持つ (Grafana 472、Tempo・Loki 10001、Prometheus 65534) ので、chown の initContainer は外した。既存ディレクトリはすでに各 UID の所有だが、グループの書き込みと setgid が無いので、最初の起動で 1 度だけ付け替わる。毎回の再帰的な付け替えを避けるため `fsGroupChangePolicy: OnRootMismatch` を付ける |
| ノードへの固定 | PV の `nodeAffinity` に任せ、各 values の `nodeSelector` を外す。WFFC なので、スケジューラは PV の置けるノードに Pod を置く |
| ArgoCD | SC と PV は cluster-scoped のリソースとして Application `storage` が持つ。PVC は各 chart が作る |
| 今後のアプリ | PV を 1 枚足し、chart の PVC 名を `claimRef` に書くだけ |

### 同期の順序

`docs/cluster/argocd.md` の「同期の順序」には今は wave が無い。WFFC なので PV が後から来ても PVC は
Pending のまま待ち、PV ができたところで結ばれるため、wave が無くても最後はそろう。
ただし初回の同期で一時的に Pending が出るので、Application `storage` に
`argocd.argoproj.io/sync-wave: "-1"` を付けて先に作る。ArgoCD は子の Application のヘルスを評価する設定
(`clusters/kind/argocd/values.yaml`) なので、wave -1 の `storage` が Healthy になってから観測スタックを作る。

## 移行手順の概要

kind-config の `extraMounts` はそのまま使えるので、kind-config の変更は無い。ただし Tempo と Loki は
StatefulSet の `volumeClaimTemplates` を足すことになり、これは既存の StatefulSet では変えられない
(出典 7)。ArgoCD の同期で上書きできないので、**`just down` → `just up` で作り直して移行する**。
これが下の確認手順も兼ねる。

| アプリ | 値で変えること (概要) | 結果の PVC 名 | マウント先 (今と同じか) | 既存データ |
|---|---|---|---|---|
| Tempo (chart `tempo` 3.0.0) | `persistence.enabled: true`、`storageClassName: home-k8s-local`。`extraVolumes`・`tempo.extraVolumeMounts`・`initContainers`・`nodeSelector` を消す | `storage-tempo-0` | `/var/tempo` (同じ) | `traces/`・`wal/`・`live-store/` をそのまま読む |
| Prometheus (chart `prometheus` 29.35.0) | `server.persistentVolume.enabled: true`、`storageClass: home-k8s-local`、`volumeName: prometheus`。`storagePath`・`extraVolumes`・`extraVolumeMounts`・`extraInitContainers`・`nodeSelector` を消す | `prometheus-server` | `/data` (変わる。`--storage.tsdb.path` も `/data` になる) | PV の根に TSDB があるので、マウント先が変わっても同じファイルを読む |
| Loki (chart `loki` 18.13.7) | `singleBinary.persistence.enabled: true`、`storageClass: home-k8s-local`。`dataVolumeParameters`・`initContainers`・`nodeSelector` を消す | `storage-loki-0` | `/var/loki` (同じ) | そのまま読む |
| Grafana (chart `grafana` 13.2.7) | `persistence.enabled: true`、`storageClassName: home-k8s-local`、`volumeName: grafana`、`deploymentStrategy.type: Recreate` | `grafana` | `/var/lib/grafana` | 新規。初回は空の `grafana.db` から始まる |

補足:

- Tempo・Loki の StatefulSet は `persistentVolumeClaimRetentionPolicy` を出さない
  (Tempo は `enableStatefulSetAutoDeletePVC: false`、Loki も同じ既定)。Kubernetes の既定は
  `Retain` なので、StatefulSet を消しても PVC は残る (出典 7)。
- Grafana の chart は `lookupVolumeName: true` で既存 PVC の `volumeName` を `lookup` で引くが、
  ArgoCD は `helm template` で描くので `lookup` は空になる。`volumeName` を明示すれば
  そちらが優先される (chart の `templates/pvc.yaml`)。
- Grafana は RWO の sqlite を 2 つの Pod が同時に開かないよう `Recreate` にする
  (chart の既定は `RollingUpdate`)。Prometheus の chart は既定で `Recreate`。
- `local` PV のパスはノードに先にあること (kubelet がマウント前にパスの種類を調べ、無ければ失敗する。出典 6)。
  `tempo/`・`prometheus/`・`loki/` は今あるが、`grafana/` は無い。`just up` がホストで
  `mkdir -p ~/.local/share/home-k8s/observability/grafana` を打つ。

### Grafana のユーザーを Secret にそろえる

永続化すると `grafana.db` が残るので、今の「作り直せば Secret の値で始まる」前提が崩れる。

| ユーザー | パスワードの正本 | 今 | 永続化後に起きること | 永続化後のそろえ方 |
|---|---|---|---|---|
| admin | ホストのファイル → Secret `grafana-admin` | Grafana が初回起動で Secret の値で作る | `admin_password` は初回起動のときしか使われない (出典 5)。ファイルを作り直すと Secret と DB がずれ、サイドカーも admin で入れなくなる | initContainer で `grafana cli admin reset-admin-password` を打ち、起動のたびに Secret の値にそろえる |
| viewer | ホストのファイル → Secret `grafana-viewer` | サイドカーが「無ければ作る」 | DB に残るので作られない。viewer のパスワードを変えるのはファイルを書き換えて `just up` を打つときだけで、Secret にも share の写しにも同じ値が入る。DB へは Pod の起動時にそろえる | サイドカーを Pod の起動ごとに一度だけ「無ければ作る、居ればパスワードを Secret の値に更新する」(`PUT /api/admin/users/:id/password`) に変える |
| backstage (argocd-grafana-backstage で追加予定) | ホストのファイル → Secret | サイドカーが「無ければ作る」予定 | viewer と同じ | viewer と同じ処理に載せる |

admin をサイドカーでそろえないのは、サイドカーが admin の資格情報で API を叩くため、
ずれた後は入れないから。Grafana のイメージは distroless で `sh` が無いので、initContainer は
同じイメージで exec 形式の `/usr/share/grafana/bin/grafana cli --homepath=/usr/share/grafana --config=/etc/grafana/grafana.ini admin reset-admin-password $(ADMIN_PASSWORD)`
とし、`config` (grafana.ini) と `storage` (PV) を本体と同じ場所にマウントする。DB の場所は
イメージの環境変数 `GF_PATHS_DATA` の既定 `/var/lib/grafana` がそのまま PV のマウント先になる。
DB が空の初回 (Grafana がまだ一度も起動していない) でも動き、そのとき admin はこのコマンドが作る
(13.2.3-distroless で、空の DB と既存の DB の両方を docker で確かめた)。

サイドカーがそろえるのは起動時の一度だけで、繰り返さない。サイドカーの環境変数は Pod の起動時の Secret の値のままで、
実行中に変わった値を読み直せないため、繰り返しても意味が無い。

## 実装時の確認方法

検証用のクラスタで (`docs/cluster/argocd.md` の「main 以外のブランチで確かめる」) 次を順に見る。

```sh
# 1. 作り直す前に印を付ける
#    Grafana の UI で空のダッシュボードを 1 つ保存する (名前: persistence-check)
#    最新のトレース・メトリクス・ログの時刻を控える

# 2. 作り直す
just down && just up

# 3. PV と PVC が 1 対 1 で結ばれている
kubectl --context kind-study-kind get sc home-k8s-local
kubectl --context kind-study-kind get pv              # 4 枚とも Bound、RECLAIM POLICY は Retain
kubectl --context kind-study-kind -n observability get pvc   # 4 つとも Bound、VOLUME が同名の PV

# 4. データが残っている
#    Grafana に admin でログインし、persistence-check が残っている
#    1 で控えた時刻より前のトレース (Tempo)・メトリクス (Prometheus)・ログ (Loki) が引ける

# 5. Secret 由来のユーザーがファイルの値にそろう
#    admin・viewer・backstage のパスワードファイルを書き換え、just up を打ち直し、
#    Grafana の Pod を消して作り直させる
kubectl --context kind-study-kind -n observability delete pod -l app.kubernetes.io/name=grafana
#    新しい値で 3 ユーザーともログイン (API なら curl -u <user>:<新しい値> localhost:3000/api/user) できる
```

## 運用上の注意

- **PVC を手で消したとき**: `Retain` の PV は `Released` になり、前の PVC の UID を `claimRef` に持ったまま
  なので、同じ名前の PVC を作り直しても結ばれない (出典 2)。ArgoCD の selfHeal も `claimRef.uid` は
  消さない。中身を残したまま結び直すには UID だけ消す。

  ```sh
  kubectl --context kind-study-kind patch pv <name> --type json \
    -p '[{"op":"remove","path":"/spec/claimRef/uid"},{"op":"remove","path":"/spec/claimRef/resourceVersion"}]'
  ```

  `just down` → `just up` では PV ごと作り直すので、この操作は要らない。
- **データを全部消したいとき**: 今と同じく `sudo rm -rf ~/.local/share/home-k8s/observability/<アプリ>`。
  `local` のパスは先にあることが前提なので、消したあとは `just up` (`mkdir -p`) を打つ。
- **容量**: `local` も local-path-provisioner も容量を強制しない (出典 4)。PV の `capacity` は
  結び付けの照合に使う目安で、保持期間 (14 日) で量を抑える。

## 未決の論点

- ラベル名 `home-k8s/observability-storage` とホストのディレクトリ `observability/` を、Backstage の DB などを
  載せるときに一般的な名前 (例: `home-k8s/storage`、`~/.local/share/home-k8s/volumes`) に替えるか。
  替えるとクラスタの作り直しとディレクトリの移動が要るので、最初のアプリを足すときに決める。

設計の時点で未決だった次の 2 つは、実装で決めた。

- Grafana の `reset-admin-password` は distroless のイメージの initContainer で打てる (上の「Grafana のユーザーを Secret にそろえる」)。
- Application `storage` に sync wave `-1` を付けた (上の「同期の順序」)。

## 確認の結果

2026-10-02 (UTC) に本番のクラスタ `study-kind` で、上の「実装時の確認方法」を次の順に行った
(ArgoCD の参照先を検証用のブランチに向け、`just down` → `just up` を 3 回)。
1 回目は hostPath から PV への移行で、この時点の Grafana は emptyDir なので、ダッシュボードが残るかは 2 回目で見た。
3 回目は Backstage (#27) が `main` に入った後の、backstage ユーザーをサイドカーにまとめた形で行った。

| 確認 | 結果 |
|---|---|
| 1 回目の作り直し (移行) | `just up` 2 分 40 秒で 9 つの Application が Synced / Healthy。PV 4 枚は PVC より先に `Available` になり (wave -1)、PVC 4 つは同名の PV と `Bound`、RECLAIM POLICY は `Retain`。Pod は PV の nodeAffinity で `study-kind-worker` に置かれた |
| 移行前のデータ | 作り直しの前後で同じ。Prometheus は 2026-09-30 00:17 から、Tempo は 2026-09-19 からのトレース、Loki は 2026-09-30 00:16 からのログが引ける |
| Grafana の保存 | `persistence-check` ダッシュボードを保存 (UI の操作ではなく admin で HTTP API から作成) → 2 回目・3 回目の作り直し → 同じ作成時刻のまま残っていた |
| サイドカー | 1 回目は「viewer を作った」、2 回目は「viewer のパスワードを Secret の値にそろえた」。3 回目は backstage を作り、Backstage のプロキシから Grafana の検索が 200 |
| パスワードの書き換え | admin・viewer・backstage のファイルを書き換え → `just up` (Secret が `configured`) → Grafana の Pod を削除。3 ユーザーとも新しい値で 200、古い値で 401。Backstage の Pod を作り直すとプロキシも 200 |

## 出典

1. Kubernetes「Volumes」の hostPath と local: <https://kubernetes.io/docs/concepts/storage/volumes/#hostpath>、<https://kubernetes.io/docs/concepts/storage/volumes/#local>
   (hostPath の警告と local PV の推奨、local は静的な PV のみ・`nodeAffinity` 必須・WFFC の推奨)
2. Kubernetes「Persistent Volumes」: <https://kubernetes.io/docs/concepts/storage/persistent-volumes/>
   (Retain と Released、Reserving a PersistentVolume (`claimRef`・`volumeName`)、hostPath は単一ノードのテスト用)
3. Kubernetes「Storage Classes」の local: <https://kubernetes.io/docs/concepts/storage/storage-classes/#local>。
   kind の `standard` は `rancher.io/local-path`・`Delete`・`WaitForFirstConsumer` (いまのクラスタの
   `kubectl get sc standard -o yaml` で確認。kind v0.32.0)
4. local-path-provisioner の README: <https://github.com/rancher/local-path-provisioner/blob/master/README.md>
   (`nodePathMap`、`pathPattern` と `allowUnsafePathPattern`、容量を強制しないこと、teardown)
5. Grafana の `conf/defaults.ini`: <https://github.com/grafana/grafana/blob/main/conf/defaults.ini>
   (`admin_password` は「初回起動の前か、プロフィールで変えられる」、`[database] type = sqlite3`・`path = grafana.db`)
6. Kubernetes のソース `pkg/volume/local/local.go`: <https://github.com/kubernetes/kubernetes/blob/master/pkg/volume/local/local.go>
   (マウント前に `GetFileType` でパスを調べる、`fsGroup` で所有グループを付け替える。`pkg/volume/hostpath` には付け替えが無い)
7. Kubernetes「StatefulSets」: <https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/>
   (`persistentVolumeClaimRetentionPolicy` の既定は `Retain`)。`volumeClaimTemplates` を更新で変えられないことは
   `pkg/apis/apps/validation/validation.go` の `ValidateImmutableField(... volumeClaimTemplates)`:
   <https://github.com/kubernetes/kubernetes/blob/master/pkg/apis/apps/validation/validation.go>
8. 各 Helm chart の values と templates (`helm pull` した版で確認): `tempo` 3.0.0 (`templates/statefulset.yaml`)、
   `loki` 18.13.7 (`templates/_workload.tpl`・`_pod.tpl`)、`prometheus` 29.35.0 (`templates/pvc.yaml`・`deploy.yaml`)、
   `grafana` 13.2.7 (`templates/pvc.yaml`、`values.yaml` の `persistence`・`initChownData`・`deploymentStrategy`)
