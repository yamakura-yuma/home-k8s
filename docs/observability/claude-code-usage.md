# Claude Code のメトリクス・ログ・トレースを見る

Claude Code は OpenTelemetry で 3 種類のデータを出す。このリポジトリの観測スタック
([claude-code-traces.md](claude-code-traces.md)) はそれぞれを別の保存先に入れ、Grafana の
3 枚のダッシュボードで見る。ハーネス (rule / skill / 指示の書き方 / モデル選択) を直すための
`Claude Code improve` は [claude-code-improve.md](claude-code-improve.md) にまとめた。

| 種類 | 何か | 保存先 | 向いている問い | ダッシュボード |
|---|---|---|---|---|
| メトリクス | 数値の累計 (コスト、トークン、行数など) を 60 秒ごとに送ったもの | Prometheus | どれだけ使ったか、増えているか | Claude Code usage の 1〜3 |
| ログ (イベント) | 出来事 1 件ごとの記録 (ツールを実行した、API を呼んだ、など) | Loki | 何が起きたか、どれが失敗したか | Claude Code usage の 4 |
| トレース | 1 回の依頼の中の処理 (スパン) の親子関係と所要時間 | Tempo | どこで時間がかかったか、何の順に動いたか | Claude Code traces |
| ログ + メトリクス | 依頼ごとのコスト、繰り返す失敗、使われない skill / MCP | Loki、Prometheus | ハーネスのどこを直すか | Claude Code improve |

メトリクスは件数が多くても軽いが、個々の出来事は分からない。ログは 1 件ずつ見られるが、
集計すると重い。トレースは 1 回の依頼を分解して見られるが、全体の合計には向かない。
上から順に「全体 → 出来事 → 1 回の中身」と絞り込んでいく使い方になる。

```
Claude Code usage (Prometheus)      コストが跳ねた時間帯を見つける
        │ 同じ時間帯のまま下へ
Claude Code usage 4. イベント (Loki)  その時間帯の失敗・遅い API を 1 件ずつ見る
        │ ログ行を開いて「トレースを開く」
Tempo のトレース                      その依頼の中で何が何秒かかったか
        │ スパンの詳細のログへのリンク
Loki                                 同じトレースのイベントを全部
```

## 3 種類をつなぐもの

- ログ → トレース: Claude Code のイベントは `trace_id` と `span_id` を持つ。Loki
  データソースの derived field (`matcherType: label`、`trace_id`) が、ログ行の詳細に
  「トレースを開く」リンクを出し、Tempo で同じトレースを開く。
- トレース → ログ: Tempo データソースの `tracesToLogsV2` が、スパンの詳細に
  Loki へのリンクを出す。クエリは
  `{service_name="claude-code"} | trace_id="<トレース ID>"` で、同じ依頼のイベントが並ぶ。
- トレース → メトリクス: `tracesToMetrics` が、スパンの `model` 属性でコストとトークンの
  推移を引く。
- ダッシュボード同士: 上部の「Claude Code」リンクで usage と traces を行き来する。
  時間範囲は引き継ぐ。

メトリクスからトレースへは直接飛べない。Claude Code のメトリクスには exemplar
(代表となるトレース ID) が付かないため。時間帯で合わせてログを経由する。

## ワーカーの見分け方

Orca の端末から起動した Claude Code には `orca.worktree.name` (worktree 名) と
`orca.worktree.id` (Orca のリポジトリ ID) のリソース属性が付く。

- Prometheus: `promote_resource_attributes` で全系列のラベル `orca_worktree_name` /
  `orca_worktree_id` になる。
- Loki: structured metadata の `orca_worktree_name` / `orca_worktree_id` になる。
- Tempo: `resource.orca.worktree.name` で引ける。

usage ダッシュボード上部の変数「ワーカー」と「リポジトリ」(`vcs_repository_name`) は
メトリクスとイベントの両方のパネルに効く。Orca の外 (普通の端末) から起動したセッションは
`orca_worktree_name` が空で、表では「(Orca の外)」と出る。ただし変数で特定のワーカーを
選ぶと、空のものは外れる。

試しにワーカーを名乗らせるには環境変数で属性を足す。

```sh
OTEL_RESOURCE_ATTRIBUTES=orca.worktree.name=test-worker claude -p "hello"
```

## 各パネルで分かること

「Claude Code usage」(<http://localhost:3000/d/claude-code-usage>) のパネル。
値はすべて右上の期間で集計する (今日・7 日のパネルだけ期間を固定している)。

### 1. 概要

| パネル | 分かること | 跳ねたら疑うこと |
|---|---|---|
| 今日のコスト / 7 日間のコスト / 期間中のコスト | API 料金の見積もり (USD)。サブスクリプションの請求額ではない | 大きなファイルを何度も読ませていないか、長い会話を続けていないか |
| セッション数 | 起動した Claude Code の数 (`claude -p` も 1 と数える) | ループや hook から `claude` を呼んでいないか |
| トークン合計 | 入力・出力・キャッシュ読み書きの合計 | コストと一緒に見る |
| キャッシュ読み出し割合 | 入力側のうちキャッシュから読んだ割合。高いほど安い | 下がったら、システムプロンプトや CLAUDE.md が毎回変わっていないか。短いセッションが多いと低く出る |
| コストの推移 (モデル別) | 5 分ごとのコスト | 棒が高い時間帯を traces ダッシュボードで開く |
| トークンの推移 (種類別) | 5 分ごとのトークン | `cacheCreation` ばかり続くならキャッシュが効いていない |
| モデル別コスト / 種類別トークン | 期間中の内訳 | 意図しないモデル (Opus など) が多くないか |

### 2. 成果

| パネル | 分かること | 跳ねたら疑うこと |
|---|---|---|
| 追加した行 / 削除した行 | Edit・Write で変えた行数 | 生成物やロックファイルを書かせていないか |
| コミット数 / PR 数 | Claude Code が作ったコミットと PR | 0 のままなら、コミットは人が打っている |
| アクティブ時間 (人) / (Claude) | 人が操作していた時間と Claude が動いていた時間 | Claude の時間だけ長いなら、待たされている |
| 編集の許可 / 却下 | Edit・Write の許可 (accept) と却下 (reject)。`config` は設定での自動許可、`user` は手で押したもの | reject が多いなら、指示が曖昧か、許可の設定が狭い |
| コード行数の推移 | 5 分ごとの追加・削除行数 | 一度に大量なら生成物を疑う |

### 3. ワーカー別

| パネル | 分かること | 跳ねたら疑うこと |
|---|---|---|
| ワーカーごとの集計 | worktree ごとのコスト・トークン・セッション・Claude の稼働時間 | 特定のワーカーだけ高いなら、その worktree の依頼が大きすぎないか |
| ワーカー別コストの推移 | 5 分ごとのコストをワーカーで積み上げる | 並列に走らせた数と合っているか |
| ワーカー × モデルのトークン | どのワーカーがどのモデルを使ったか | 軽い作業のワーカーが重いモデルを使っていないか |

### 4. イベント

ログのパネルは行をクリックすると詳細が開き、Links の「トレースを開く」で Tempo に飛ぶ。

| パネル | 分かること | 跳ねたら疑うこと |
|---|---|---|
| ツール実行の成否 | 5 分ごとの `tool_result` をツールと成否で数える | `success=false` が増えたら下のパネルで中身を見る |
| ツール別の所要時間 (p95) | ツールごとの所要時間の 95 パーセンタイル | Bash が長いならテストやビルドの待ち |
| 失敗したツール | `success=false` の `tool_result`。Bash の非 0 終了や却下 | 同じ失敗の繰り返しなら、手順か許可の設定を直す |
| API エラー | `api_error` イベント。HTTP のステータスとエラー | 429 (レート制限) や 529 (過負荷) が続くなら時間を置く |
| 遅い API リクエスト (10 秒以上) | `api_request` のうち 10 秒以上かかったもの。`ttft` は最初のトークンまでの時間 | 入力トークンが多いものが並ぶなら文脈が膨らんでいる |

## Claude Code のイベントの種類

Loki の `event_name` で絞れる。2.1.285 で観測したもの:

| event_name | 内容 | 主な属性 |
|---|---|---|
| `user_prompt` | 依頼を受けた | `prompt_length` (本文は `<REDACTED>`) |
| `api_request` | API を呼んだ | `model`、`duration_ms`、`ttft_ms`、`input_tokens`、`output_tokens`、`cache_read_tokens`、`cost_usd` |
| `api_error` | API がエラーを返した | `status_code`、`error`、`attempt` |
| `tool_decision` | ツールの許可を決めた | `tool_name`、`decision`、`source` |
| `tool_result` | ツールを実行した | `tool_name`、`success`、`duration_ms`、`error_type` |
| `hook_execution_start` / `hook_execution_complete` | hook を実行した | `hook_name`、`total_duration_ms` |
| `assistant_response`、`mcp_server_connection`、`plugin_loaded` など | 起動や応答の記録 | |

Explore で `{service_name="claude-code"} | event_name="api_request"` のように打つと並ぶ。

## 参考にしたダッシュボード

パネルの選び方は次の 2 つを参考にした (どちらも MIT License)。JSON はコピーしておらず、
クエリはこのスタックのラベル名 (`orca_worktree_name` など) と Prometheus 3 の
`anchored` に合わせて書き直している。

- Grafana 公式ギャラリーの Claude Code Metrics (ID 25255、
  <https://github.com/rockdarko/claude-code-metrics-prometheus>): 概要の数値、モデル別コスト、
  成果の数値
- ColeMurray/claude-code-otel (<https://github.com/ColeMurray/claude-code-otel>):
  Loki の `tool_result` / `api_error` のパネル

どちらもセッション ID (`session_id`) やユーザー (`user_email`) で分けているが、このダッシュボードは
ワーカー (`orca_worktree_name`) で分けている。利用者は 1 人で、ワーカーとセッションはほぼ 1 対 1 に
なるため。`session_id` は 2026-10-01 から付いている (`OTEL_METRICS_INCLUDE_SESSION_ID=true`)。
セッション別の内訳は設定項目別ダッシュボードの `cc-setting-include-session-id` にある。

## 定義の場所

- ダッシュボード: `clusters/kind/observability/dashboards/claude-code-usage.json`
- データソースと相互リンク: `clusters/kind/observability/grafana-values.yaml` の `datasources`

UI で直しても Pod の再起動で消えるので、JSON か values を直して `main` に入れる (ArgoCD が同期する)。
