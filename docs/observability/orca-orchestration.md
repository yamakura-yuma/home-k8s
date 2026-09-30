# Orca のオーケストレーションを観測スタックに送る (orca-exporter)

coordinator が Orca でワーカーをどう回したか (Run → Task → ワーカー (Dispatch) → メッセージ) を
Grafana の「Orca orchestration」(`/d/orca-orchestration`) で見るための仕組み。とくに**手戻り**
(1 ワーカーあたりの追加指示、worker_done までの時間、拒否・user_takeover) を測る。見方は
[playbook.md](playbook.md) の場面 11。

Orca 自体は OTel を出さない。一方 `orca orchestration ... --json` で Run・Task・Dispatch・メッセージの
状態が取れるので、ホストの exporter (`tools/orca-exporter/orca_exporter.py`) が 30 秒ごとにそれを読み、
OTLP/HTTP (JSON) で Collector (`http://localhost:4318`) に送る。依存は Python の標準ライブラリだけ。

```
orca CLI (relay 経由) --読むだけ--> orca_exporter.py (systemd のユーザーユニット)
                                     |  OTLP/HTTP JSON, service.name=orca, service.namespace=home-k8s
                                     v
                          OTel Collector --> Loki (イベント) / Prometheus (ゲージ) / Tempo (Run のトレース)
```

## 入れる・止める

ホスト (WSL) の、Orca の端末で打つ。

```sh
just orca-exporter-install    # ~/.config/systemd/user/orca-exporter.service を置いて起動 (入れ直しも同じ)
just orca-exporter-status     # systemd の状態、直近のログ、送った件数、最後に送れた時刻
just orca-exporter-uninstall  # 止めて消す。送信済みの状態は残す (消すと入れ直したときに二重に送る)
```

- ユニットはこの checkout の `orca_exporter.py` を直接指す。worktree で入れたら、main に戻ってから入れ直す。
- 状態は `~/.local/share/home-k8s/observability/orca-exporter/state.json` (リポジトリの外)。
  送ったログのキー、送ったスパンのキー、Task の前回の状態、終わった Dispatch の詳細を持つ。
  このディレクトリの親は開発用コンテナ (root) が作るので、install は状態のディレクトリだけを
  `docker run` 経由で自分の持ち物として作る。
- Orca の relay が居ない、Collector が止まっているときは、その回を飛ばして 30 秒後にやり直す (プロセスは落ちない)。
  送れなかったログとスパンは状態に「送った」と残らないので、Collector が戻った回にまとめて送る。

### ホストで動かす理由 (開発用コンテナへの転送の例外)

このリポジトリの just レシピは、ホストで打っても開発用コンテナ `home-k8s-dev` の中で動く (README の
「ホストで打つか、コンテナで打つか」)。`orca-exporter-*` の 3 つだけは例外で、ホストで動かす。

- `orca` CLI は `~/.orca-relay/bin/orca` のラッパで、Orca が WSL に置いた relay の Unix ソケットと
  資格情報 (`~/.orca-remote/relay-<版>/`) を使う。開発用コンテナにはこれが無く、マウントしても
  Orca の版が上がるとパスが変わる。
- 常駐させる先がホストの systemd (`systemctl --user`) である。

レシピは shebang で書いている。shebang のレシピは `set shell` (`just/dev-shell`) を通らず、打った場所で動く。
コンテナの中で打つと、ホストで打つよう案内して止まる。

## 読むもの (読むだけ)

| コマンド | 取るもの |
|---|---|
| `run-list` | Run (目的、作った時刻、束縛中の coordinator) |
| `task-list --run <id>` | Task (状態、作った・終わった時刻、担当の Dispatch、結果) |
| `inbox --limit 100000` | 全宛先のメッセージ (種別、件名、本文、payload、送信・配達の時刻、スレッド) |
| `worker-list --run <id>` | ワーカーの端末の扱い (worktree、release の結果、残った理由) |
| `worker-show --dispatch <id>` | Dispatch の開始・終了時刻、失敗回数、起動したモデル。終わった Dispatch は状態に保存して引き直さない |
| `dispatch-show --task <id>` | worker-show で見えない Dispatch (worker-start を使わない `dispatch` で出したもの) |

使わないもの: `check` (既読にし、配達の状態を変える)、`run-use` / `run-create` (稼働中の coordinator の
束縛を奪う)、状態を変える他のコマンド。`worker-list` は `--run` を付けないと、打った場所 (cwd) に束縛された
Run だけを返すことがある (systemd から `$HOME` で動かすと 3 件だけだった) ので、Run ごとに引く。

## 送るもの

共通の属性: `orca.run.id`、`orca.task.id`、`orca.dispatch.id`、`orca.worktree.name` (ワーカー名)、`orca.model`。
resource は `service.name=orca`、`service.namespace=home-k8s`、`service.version` (exporter の版)、`host.name`。

`orca.worktree.name` は Claude Code 側と同じキーで、値も同じ作り方 (worktree id `<uuid>::<path>` の path の
basename。dotfiles の `shell/prompt.sh`)。Prometheus では `orca_worktree_name` ラベル、Loki では
`orca_worktree_name` の structured metadata になり、Claude Code のメトリクス・イベントとワーカー名で結合できる。

### ログ (Loki、`{service_namespace="home-k8s", service_name="orca"}`)

1 件 = 1 イベント。時刻は出来事の時刻 (送った時刻ではない)。本文はそのまま送り、秘密の伏せ字は Collector の
`transform/redact` が当てる。

| `event_name` | いつ | 主な属性 |
|---|---|---|
| `orca.message` | メッセージ 1 件ごと (配達されてから。10 分配達されなければ待たずに) | `orca_message_type`、`orca_message_subject`、`orca_message_direction` (`coordinator_to_worker` / `worker_to_coordinator` / `other`)、`orca_message_followup`、`orca_message_reply`、`orca_message_rejected`、`orca_message_rejection_code`、`orca_message_delivery_delay_seconds`、`orca_phase`、`orca_outcome`、`orca_report_path` |
| `orca.task.status` | Task を作ったとき (`created`) と、状態が変わるたび | `orca_task_status`、`orca_task_previous_status`、`orca_task_title`、`orca_outcome` |
| `orca.dispatch.status` | ワーカーを出したとき (`dispatched`) と、終わったとき | `orca_dispatch_status`、`orca_worker_state`、`orca_release_state`、`orca_release_retained_reason`、`orca_dispatch_followups`、`orca_dispatch_duration_seconds` |

Task の状態の時刻: 終わった状態 (`completed` / `failed`) は `completed_at`、初めて見た `pending` / `ready` は
作った時刻、初めて見た `dispatched` は Dispatch の開始、途中で変わったものは exporter が気づいた時刻
(最大 30 秒遅れ)。途中の状態の履歴は Orca に残っていないので、exporter を入れる前の変化は取れない。

### メトリクス (Prometheus、`job="home-k8s/orca"`)

すべてゲージで、毎回いまの状態を数え直して送る。累計を足し込まないので、exporter を再起動しても二重に数えない。
exporter が止まると系列は 5 分で途切れる。ダッシュボードは `last_over_time(...[$__range])` で期間内の最後の値を使う。

| メトリクス | 値 | ラベル (共通の属性のほか) |
|---|---|---|
| `orca_run_info` | 1 | `orca_run_objective`、`trace_id`、`orca_run_state` (`open` / `closed`) |
| `orca_run_start_time_seconds` / `orca_run_duration_seconds` | 作った時刻 / 最後の動きまで | |
| `orca_run_tasks` | Task 数 | `orca_task_status` |
| `orca_dispatch_info` | 1 | `orca_dispatch_status`、`orca_worker_state`、`orca_terminal_state`、`orca_release_state`、`orca_release_retained_reason` (`user_takeover` など)、`orca_attention` |
| `orca_dispatch_start_time_seconds` | ワーカーを出した時刻 | |
| `orca_dispatch_duration_seconds` | 出してから終わるまで (動いていれば現在まで) | |
| `orca_dispatch_time_to_done_seconds` | 出してから最初の worker_done (受理されたもの) まで | |
| `orca_dispatch_heartbeat_max_gap_seconds` | 開始・heartbeat・終了 (動いていれば現在) の間の最大の間隔 | |
| `orca_dispatch_followups` | 追加指示の数 | |
| `orca_dispatch_replies` / `orca_dispatch_questions` / `orca_dispatch_escalations` / `orca_dispatch_heartbeats` / `orca_dispatch_worker_done` | 各メッセージの数 | |
| `orca_dispatch_worker_done_rejected` / `orca_dispatch_rejected` | 拒否された worker_done / 拒否されたメッセージ全部 | |
| `orca_dispatch_failures` | Dispatch の失敗回数 (`failureCount`) | |
| `orca_messages` | メッセージ数 | `orca_message_type`、`orca_message_direction`、`orca_message_rejected` |
| `orca_exporter_last_success_seconds` | 最後に Orca を読んで送れた時刻 | |

**追加指示** (`followup`) の定義: 宛先がワーカー (`dispatch:<id>` かワーカーの端末) で、送り手がワーカー自身
ではなく、スレッドの返事 (`thread_id` あり) でもないメッセージ。`orca orchestration send` で出した訂正・
追加依頼が当たる。`ask` への返事は `replies` に数える。

**拒否**: Orca が受け付けなかったメッセージは、payload に `_orcaLifecycleRejection` (`code` と `reason`) が付いて
inbox に残る。実データでは `dispatch_capability_invalid` (Dispatch の capability が失効した後の worker_done /
heartbeat) だけだった。

### トレース (Tempo)

1 Run = 1 トレース。Run が親スパン、Task が子、ワーカー (Dispatch) が孫で、メッセージはワーカー
(ワーカーが分からなければ Task、Task も分からなければ Run) のスパンイベントになる。トレース ID は Run id の
ハッシュなので、`orca_run_info` の `trace_id` ラベルからトレースを開ける。

**終わってから送る**。Tempo は同じスパンを後から送り直しても置き換えない (同じ ID は重複として捨てる) ので、
動いている Task を「現在まで」で区切って送ると、終わった後の長さに直せない。そこで:

- Task とそのワーカーのスパンは、Task が `completed` / `failed` になった回に 1 回だけ送る。
- Run のスパンは、Run が閉じた回に 1 回だけ送る。閉じた = 束縛している coordinator がいない
  (`coordinator_handle` が空) かつ動いている Dispatch が無い。
- Run が閉じた時点で終わっていない Task (`ready` / `pending` のまま残ったもの) は、Run の最後の動きで区切って
  送り、`orca.task.unfinished=true` を付ける。
- 動いている Run はまだトレースに出ない。その間の流れは Loki のイベントで見る。
- Run が閉じた後に再び束縛されて Task が増えた場合、増えた Task は同じトレースに子として加わるが、
  Run のスパンの長さは閉じた時点のまま。

Tempo の `ingestion_time_range_slack` (既定 2 分) を保存期間と同じ 336h にしている
(`tempo-values.yaml`)。既定のままだと、数日前に始まった Run のスパンのブロックが受け取った時刻の範囲に
丸められ、その Run の時刻を指定した検索で見つからない。

## 二重送信を防ぐしくみ

- ログ: キー (`msg:<id>`、`task:<id>:<状態>`、`dispatch:<id>:<状態>`) を状態に残し、送ったものは送らない。
  200 件ずつ送り、送れた分だけを状態に書く。
- スパン: キー (`run:<id>`、`task:<id>`、`dispatch:<id>`) を同じように残す。スパン ID も Run / Task / Dispatch の
  id のハッシュなので、万一送り直しても Tempo が同じスパンとして捨てる。
- メトリクス: 毎回数え直すので、キーは要らない。

状態ファイルを消すと全部を送り直し、Loki には同じイベントが 2 行入る。やり直したいときは状態を消す前に
Loki の削除 API (`/loki/api/v1/delete`、`{service_namespace="home-k8s", service_name="orca"}`) で消す。
トレースを別物として送り直したいときは `orca_exporter.py` の `ID_SALT` を変える (トレース ID が変わる)。

## Loki が古いログを捨てないための設定

初回は Orca に残っている全部 (実データで 10 日前のメッセージから) をまとめて送る。Loki の既定では
1 週間より古いログを捨てる (`reject_old_samples_max_age: 1w`) ので、保存期間と同じ 336h にした
(`loki-values.yaml`)。

Loki は同じストリームの最新の行より 1 時間以上古い行も捨てる (`max_chunk_age` の半分)。exporter のストリームは
`service_name` と `service_namespace` で決まる 1 本なので、初回は時刻順に並べて送り、以降は新しい出来事だけを
送る。配達を待つメッセージは 10 分で待つのをやめ、1 時間の窓を越えないようにしている。

## ダッシュボード

`clusters/kind/observability/dashboards/orca-orchestration.json` (`just observe-up` で入る)。

| 節 | パネル | 使うデータ |
|---|---|---|
| 1. 要約 | ワーカー数、追加指示 (合計・1 ワーカーあたり)、worker_done までの中央値、拒否された worker_done、user_takeover、exporter の最終送信 | Prometheus |
| 2. Run | Run 一覧 (目的・開始・所要時間・Task 数・完了/失敗・ワーカー・追加指示)、Run のタイムライン (変数「Run」で選ぶ)、Run のトレース一覧 | Prometheus / Tempo |
| 3. ワーカー別 | ワーカーごとの手戻り (1 行 = 1 Dispatch)、追加指示・worker_done までの時間・heartbeat の途切れの上位、終わり方の内訳、拒否されたメッセージ | Prometheus / Loki |
| 4. メッセージの流れ | 種別ごとの件数の推移、追加指示と question の推移、送信→配達の遅れ、やりとりの本文 | Loki |
| 5. Claude Code との突き合わせ | ワーカーごとの手戻りとコスト (Orca の追加指示・question・所要時間 × Claude のコスト・トークン・API 呼び出し・ツール失敗)、モデル別の手戻り | Prometheus + Loki (Mixed) |

期間 (右上) は「その期間に出したワーカー」で絞る。Run 一覧は「その期間に作った Run」。メッセージの推移は送った時刻。
「モデル別の手戻り」のモデルは、Claude Code 側でそのワーカーのコストが最も大きいモデル (Orca の `worker-show`
は古い Dispatch だとモデルを返さないため)。Claude Code のテレメトリを入れる前のワーカーは突き合わせの表で
Claude 側が空になる。

## 実データで分かったこと

- `inbox` は既読 (`read`) を変えない。`--limit` に上限は無い (`run-list` / `worker-list` は 100 まで)。
- `task-list` と `worker-list` は、Run が束縛されていない場所で `--run` 無しに打つと `run_required` で失敗するか、
  束縛された Run に絞られる。
- 時刻の形が混ざっている: `2026-09-30T16:01:10Z`、`2026-09-30 04:10:21` (UTC、タイムゾーン無し)、
  `2026-09-30T15:49:39.729Z`、epoch ミリ秒 (`liveness.observedAt`)。exporter はすべて UTC として読む。
- 古い Dispatch は `worker-show` に `startOptions.launch` が無く、モデルが分からない。
