# Claude Code の OTel 設定項目ごとに届くもの

Claude Code が OpenTelemetry に何を送るかは、環境変数 (dotfiles の `claude/telemetry-env.json` →
`~/.claude/settings.json` の `env`) で項目ごとに決めている。この文書は「設定項目 → 届くもの →
見るダッシュボード」の対応表で、Grafana のフォルダ「Claude Code 設定項目別」に同じ単位の
ダッシュボードがある (入口は `/d/cc-settings`)。どのダッシュボードも先頭に環境変数・いまの値・
何が届くようになるか・止めると何が消えるかを書き、その下にその設定で届いた属性を件数つきで並べる。

属性の意味の正本は公式の <https://code.claude.com/docs/en/monitoring-usage>。下の表の「実データ」は
2026-09-30 に Claude Code 2.1.285 のデータ (直近 7 日) で確かめた結果で、確かめられなかった対応は
「推定」と書いた。名前は信号ごとに書き方が違う。Prometheus と Loki では `.` が `_` になり
(`vcs.repository.name` → `vcs_repository_name`)、Tempo では元の名前に `span.` / `resource.` / `event.` を付ける。

## 対応表

| 環境変数 | いまの値 | 届くようになるもの (公式) | 実データ | ダッシュボード |
|---|---|---|---|---|
| `CLAUDE_CODE_ENABLE_TELEMETRY` | `1` | テレメトリ全体の元スイッチ。全信号の標準属性 (`organization.id`, `user.*`, `terminal.type`) と resource 属性 (`service.*`, `os.*`, `host.arch`) | 3 信号とも届いている | `cc-setting-enable-telemetry` |
| `OTEL_METRICS_EXPORTER` | `otlp` | メトリクス 8 種 (session / lines_of_code / pull_request / commit / cost / token / code_edit_tool_decision / active_time) | 8 種とも系列あり。resource 属性は `target_info` のラベルにもなる | `cc-setting-metrics-exporter` |
| `OTEL_LOGS_EXPORTER` | `otlp` | イベント 27 種 (`user_prompt`, `api_request`, `tool_result`, hook・skill・MCP 接続・plugin など) | 15 種が届いている。`api_error` / `compaction` などは起きていないので 0 | `cc-setting-logs-exporter` |
| `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA` + `OTEL_TRACES_EXPORTER` | `1` / `otlp` | スパン `interaction` / `llm_request` / `tool` / `tool.blocked_on_user` / `tool.execution`、スパンイベント `gen_ai.request.attempt` | すべて届いている。公式の表に無い `queued_sends` (interaction) もある。`claude_code.hook` は公式では detailed beta tracing が要るが、オフなのに数件届いている (出どころ未確認) | `cc-setting-traces` |
| `OTEL_METRICS_INCLUDE_REPOSITORY` | `true` | メトリクスとイベントに `vcs.repository.url.full` / `vcs.owner.name` / `vcs.repository.name` / `vcs.provider.name` | メトリクス・イベントに届いている。**スパンにも付いている** (公式はメトリクスとイベントとだけ書く。この設定で付くのかは推定) | `cc-setting-include-repository` |
| `OTEL_RESOURCE_ATTRIBUTES` | `orca.worktree.id` / `orca.worktree.name` (Orca の端末から起動したときだけ) | 全信号の resource 属性。メトリクスでは `OTEL_METRICS_INCLUDE_RESOURCE_ATTRIBUTES` (既定 true) によりラベルにもなる | 3 信号に届いている。Tempo では resource と span の両方に付いている | `cc-setting-resource-attributes` |
| `OTEL_LOG_USER_PROMPTS` | `1` | `user_prompt.prompt`、`interaction` スパンの `user_prompt` (オフだと `<REDACTED>`、長さだけ届く) | 届いている。設定前のイベントは `<REDACTED>` で残っている | `cc-setting-log-user-prompts` |
| `OTEL_LOG_TOOL_DETAILS` | `1` | `tool_result` / `tool_decision` の `tool_parameters`、`tool_result` の `tool_input` と `error` (全文) と git commit 時の `vcs.ref.head.*`、`tool` スパンの `file_path` / `full_command` / `skill_name` / `subagent_type`、`user_prompt.command_name` の実名、コスト・トークンと `api_request` の `skill.name` / `mcp_server.name` / `mcp_tool.name` / `agent.name` の実名、`mcp_server_connection.server_name`、`hook_registered.hook_matcher` | `command_name` 以外はすべて届いている (`claude -p "/skill 名"` では `command_name` が付かなかった)。メトリクスの `mcp_server_name="custom"` は v2.1.273 より前か、この設定より前の系列 (推定) | `cc-setting-log-tool-details` |
| `OTEL_LOG_ASSISTANT_RESPONSES` | `1` | `assistant_response.response` (オフだと `<REDACTED>`。未設定なら `OTEL_LOG_USER_PROMPTS` に従う) | 届いている | `cc-setting-log-assistant-responses` |
| `OTEL_LOG_TOOL_CONTENT` | dotfiles で有効化中 | `tool` スパンのスパンイベント `tool.output` (`content` / `output` / `diff` / `file_path` / `bash_command`、切ったときの `*_truncated` / `*_original_length`) | 検証で `OTEL_LOG_TOOL_CONTENT=1` を付けた `claude -p` の分だけ届いている (Read / Bash / Edit / Write / MCP)。Loki のイベントには出ない | `cc-setting-log-tool-content` |
| `OTEL_METRICS_INCLUDE_SESSION_ID` | `false` | オンにするとメトリクスに `session.id` | メトリクス 0 系列、**イベントにも付いていない**。スパンには `session.id` が付いている (推定: この設定はメトリクスとイベントに効き、スパンには効かない) | `cc-setting-off` |
| `OTEL_METRICS_INCLUDE_VERSION` / `_ENTRYPOINT` | 既定 false | メトリクスに `app.version` / `app.entrypoint` | 0 系列。版は `service.version` で届いている | `cc-setting-off` |
| `OTEL_METRICS_INCLUDE_ACCOUNT_UUID` / `_RESOURCE_ATTRIBUTES` | 既定 true | `user.account_uuid` / `user.account_id`、resource 属性のラベル | 届いている | `cc-setting-off` |
| `OTEL_LOG_RAW_API_BODIES` / `OTEL_LOG_MANAGED_SETTINGS` | 未設定 | `api_request_body` / `api_response_body` イベント、`managed_settings.settings` / `resolved_sha256` | 0 件 | `cc-setting-off` |
| `ENABLE_BETA_TRACING_DETAILED` + `BETA_TRACING_ENDPOINT` | 未設定 | `claude_code.hook` スパン、`llm_request.query_source`、`new_context` などのスパン属性 | ほぼ 0。hook スパンと `query_source` が数件だけ届いている (出どころ未確認) | `cc-setting-off` |

## ダッシュボードの作り

- 12 枚の JSON は `clusters/kind/observability/dashboards/settings/` にあり、同じディレクトリの
  `generate.py` (部品は `lib.py`) で作る。直すときは JSON ではなく `generate.py` を直し、
  `python3 clusters/kind/observability/dashboards/settings/generate.py` で作り直してコミットする。
- `just observe-up` は `settings/*.json` をすべて `dashboards.settings.<名前>.json` として Grafana に渡す。
  Grafana 側ではプロバイダ `settings` がフォルダ「Claude Code 設定項目別」に置く。ファイルを足せば
  レシピを直さずに入る。
- 「届いているか」のタイルは、その設定で増える属性ごとに期間中の件数を数える
  (Loki: `count_over_time(... | 属性!="")`、Prometheus: 系列数、Tempo: TraceQL の `count_over_time()`)。
  赤は届くはずなのに 0、灰色は「オフなので 0 が正しい」もの。
- 属性キーの一覧は、raw ダッシュボードと同じく Backend API データソース (Infinity) で各バックエンドの
  HTTP API を引き、JSONata で表にしている。イベントの属性は `label_format` で「値が入っていた属性名」と
  「`<REDACTED>` だった属性名」を別々に数えるので、伏せ字になっている属性も分かる。
- `tool_parameters` / `tool_input` のキーは、値まで `json` で展開すると Loki の系列上限 (500) を超えるため、
  `regexReplaceAll` でキー名だけを抜き出して数えている (1 段目のキーだけ)。
- `tool.output` の中身と長さは、Tempo の検索 API で `select(event.*)` したスパンイベントの属性から
  JSONata で長さを数えている。検索の上限で直近 100 トレースぶんになる。
