"""設定項目 (環境変数) ごとのダッシュボード (このディレクトリの *.json) を作る。

13 枚がほぼ同じ形なので、JSON を手で直さずにこのスクリプトを直して作り直す:
    python3 clusters/kind/observability/dashboards/settings/generate.py
パネルの部品は lib.py。Grafana への反映は `just observe-up`。
"""
import copy, json, os, sys
sys.dont_write_bytecode = True  # lib.py の __pycache__ をリポジトリに作らない
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import *

OUT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(OUT, "../../../../.."))
os.makedirs(OUT, exist_ok=True)
raw = json.load(open(os.path.join(REPO, "clusters/kind/observability/dashboards/claude-code-raw.json")))
RAW = {p["id"]: p for p in raw["panels"]}
SERIES_ROOT = RAW[5]["targets"][0]["root_selector"]

RANGE = "[5m]"  # inf_loki_instant の step と揃える
# 識別用で、どのメトリクスにも同じ値で付くラベル。推移のグラフでは内訳から外す
IDENT = "job, instance, organization_id, user_account_id, user_account_uuid, user_email, user_id, terminal_type, vcs_owner_name, vcs_provider_name, vcs_repository_url_full, orca_worktree_id"

boards = []

EVENTS_DOC = ["user_prompt", "assistant_response", "tool_result", "api_request", "api_error", "api_refusal",
              "api_request_body", "api_response_body", "tool_decision", "permission_mode_changed", "auth",
              "mcp_server_connection", "internal_error", "plugin_installed", "plugin_loaded", "skill_activated",
              "at_mention", "api_retries_exhausted", "hook_registered", "hook_execution_start",
              "hook_execution_complete", "hook_plugin_metrics", "compaction", "subagent_completed",
              "feedback_survey", "retention_sweep", "managed_settings_resolved"]
METRICS_DOC = ["claude_code_session_count_total", "claude_code_lines_of_code_count_total",
               "claude_code_pull_request_count_total", "claude_code_commit_count_total",
               "claude_code_cost_usage_USD_total", "claude_code_token_usage_tokens_total",
               "claude_code_code_edit_tool_decision_total", "claude_code_active_time_seconds_total"]
SPANS_DOC = ["claude_code.interaction", "claude_code.llm_request", "claude_code.tool",
             "claude_code.tool.blocked_on_user", "claude_code.tool.execution", "claude_code.hook"]


# ---------- shared panel builders ----------

def event_attr_long(event_filter=""):
    """イベント種別 × 属性キー (値あり / <REDACTED>) の件数。Loki の label_format で属性名を文字列にして数える。"""
    expr = ("sum by (event_name, keys, rkeys) (count_over_time(" + SEL + event_filter +
            " | label_format keys=`{{ range $k, $v := . }}{{ if and (ne $v \"\") (ne $v \"<REDACTED>\") }}{{ $k }} {{ end }}{{ end }}`"
            ", rkeys=`{{ range $k, $v := . }}{{ if eq $v \"<REDACTED>\" }}{{ $k }} {{ end }}{{ end }}` " + RANGE + "))")
    root = ('( $rows := data.result.( $e := metric.event_name; $n := $sum(values.$number($[1])); '
            '$append($map($filter($split($trim($string(metric.keys)), " "), function($x){ $x != "" }), function($x){ {"event": $e, "key": $x, "state": "値あり", "n": $n} }), '
            '$map($filter($split($trim($string(metric.rkeys)), " "), function($x){ $x != "" }), function($x){ {"event": $e, "key": $x, "state": "<REDACTED>", "n": $n} })) ); '
            '$distinct($rows.(event & "|" & key & "|" & state)).( $k := $; $g := $rows[(event & "|" & key & "|" & state) = $k]; '
            '{"event": $g[0].event, "key": $g[0].key, "state": $g[0].state, "count": $sum($g.n)} ) )')
    return inf_loki_instant(expr, root)


def event_attr_tables(b, event_filter="", h=14, title_suffix=""):
    b.add(table("イベント種別 × 属性キー (縦持ち、件数つき)" + title_suffix, [event_attr_long(event_filter)],
                "Loki のイベントを種別ごとに数え、そのイベントが持っていた属性キーを 1 行ずつにした。"
                "状態 = 値が入っていたか、`<REDACTED>` (設定がオフで伏せられた) か。件数 = その状態でその属性を持っていたイベント数。",
                [organize({"event": "イベント種別", "key": "属性キー", "state": "状態", "count": "件数"}, {},
                          {"event": 0, "key": 1, "state": 2, "count": 3})], sort="件数"), 10, h)
    b.add(table("イベント種別 × 属性キー (表の形、セル = 値ありの件数)" + title_suffix, [event_attr_long(event_filter)],
                "左の表を、行 = 属性キー、列 = イベント種別に並べ替えた。空欄 = その種別では届いていない。",
                [{"id": "filterByValue", "options": {"type": "include", "match": "any", "filters": [
                    {"fieldName": "state", "config": {"id": "equal", "options": {"value": "値あり"}}}]}},
                 {"id": "groupingToMatrix", "options": {"columnField": "event", "rowField": "key", "valueField": "count",
                                                        "emptyValue": "empty"}}],
                [{"matcher": {"id": "byRegexp", "options": ".*"}, "properties": [{"id": "custom.width", "value": 110}]},
                 {"matcher": {"id": "byName", "options": "key\\event"}, "properties": [{"id": "custom.width", "value": 230}]}]),
          14, h)


def metric_label_tables(b, match='{__name__=~"claude_code_.*"}', h=14):
    t = inf(PROM_URL + "/api/v1/series", [{"key": "match[]", "value": match}, FROM, TO], SERIES_ROOT)
    b.add(table("メトリクス × ラベル (値の種類と値の例)", [t],
                "Prometheus の series API が返した全系列を、メトリクス × ラベルの 1 行にした。値の種類 = そのラベルが取る値の数。",
                [organize({"metric": "メトリクス", "label": "ラベル", "n": "値の種類", "examples": "値の例"}, {},
                          {"metric": 0, "label": 1, "n": 2, "examples": 3})], wrap=["値の例"]), 12, h)
    b.add(table("メトリクス × ラベル (表の形、セル = 値の種類)", [dict(t)],
                "行 = ラベル、列 = メトリクス。空欄 = そのメトリクスには付いていないラベル。",
                [{"id": "groupingToMatrix", "options": {"columnField": "metric", "rowField": "label", "valueField": "n",
                                                        "emptyValue": "empty"}},
                 {"id": "renameByRegex", "options": {"regex": "^claude_code_(.*?)(_total)?$", "renamePattern": "$1"}}],
                [{"matcher": {"id": "byRegexp", "options": ".*"}, "properties": [{"id": "custom.width", "value": 105}]}]),
          12, h)


def span_attr_targets(scope="span"):
    ts_ = []
    for i, n in enumerate(SPANS_DOC):
        ts_.append(inf(TEMPO_URL + "/api/v2/search/tags",
                       [{"key": "q", "value": '{name="%s"}' % n}, {"key": "scope", "value": scope}, FROM, TO],
                       'scopes.( $s := name; $map(tags, function($x){ {"span": "%s", "scope": $s, "key": $x} }) )' % n, chr(65 + i)))
    return ts_


def span_attr_tables(b, h=16):
    b.add(table("スパン名 × 属性キー (縦持ち)", span_attr_targets(),
                "Tempo のタグ一覧 API (`/api/v2/search/tags?q={name=\"...\"}&scope=span`) を公式のスパン名ごとに引いた。"
                "行が無いスパン名 = 期間中に届いていない。",
                [MERGE, organize({"span": "スパン名", "scope": "scope", "key": "属性キー"}, {},
                                 {"span": 0, "scope": 1, "key": 2}), sort_by("スパン名", False)]), 9, h)
    b.add(table("スパン名 × 属性キー (表の形)", span_attr_targets(),
                "行 = 属性キー、列 = スパン名。セルに span と出ているところが、そのスパンに付いている属性。",
                [MERGE, {"id": "groupingToMatrix", "options": {"columnField": "span", "rowField": "key",
                                                               "valueField": "scope", "emptyValue": "empty"}},
                 {"id": "renameByRegex", "options": {"regex": "^claude_code\\.(.*)$", "renamePattern": "$1"}}],
                [{"matcher": {"id": "byRegexp", "options": ".*"}, "properties": [{"id": "custom.width", "value": 120}]},
                 {"matcher": {"id": "byName", "options": "key\\span"}, "properties": [{"id": "custom.width", "value": 230}]}]),
          15, h)


def add_board(b, fname):
    boards.append((fname, b))


# ================= 0. 一覧 =================
SETTINGS = [
    ("CLAUDE_CODE_ENABLE_TELEMETRY", "1", "cc-setting-enable-telemetry", "テレメトリ全体の元スイッチ。これが無いと何も届かない"),
    ("OTEL_METRICS_EXPORTER", "otlp", "cc-setting-metrics-exporter", "メトリクス (`claude_code_*`) → Prometheus"),
    ("OTEL_LOGS_EXPORTER", "otlp", "cc-setting-logs-exporter", "ログ / イベント → Loki"),
    ("CLAUDE_CODE_ENHANCED_TELEMETRY_BETA + OTEL_TRACES_EXPORTER", "1 / otlp", "cc-setting-traces", "トレース (スパン) → Tempo"),
    ("OTEL_METRICS_INCLUDE_REPOSITORY", "true", "cc-setting-include-repository", "メトリクスとイベントに `vcs.*` (リポジトリ)"),
    ("OTEL_RESOURCE_ATTRIBUTES", "orca.worktree.id/name (Orca 端末から起動したときだけ)", "cc-setting-resource-attributes", "全信号に `orca.worktree.*` (ワーカー)"),
    ("OTEL_LOG_USER_PROMPTS", "1", "cc-setting-log-user-prompts", "依頼文 (`prompt` / `user_prompt`)"),
    ("OTEL_LOG_TOOL_DETAILS", "1", "cc-setting-log-tool-details", "コマンド・ファイルパス・skill / MCP 名・ツール入力"),
    ("OTEL_LOG_ASSISTANT_RESPONSES", "1", "cc-setting-log-assistant-responses", "応答文 (`response`)"),
    ("OTEL_LOG_TOOL_CONTENT", "1 (dotfiles で有効化中。届いていれば下の件数が 1 以上)", "cc-setting-log-tool-content", "ツール出力の中身 (スパンイベント `tool.output`)"),
    ("OTEL_METRICS_INCLUDE_SESSION_ID", "true", "cc-setting-include-session-id", "メトリクスとイベントに `session.id`"),
    ("既定値・オフのもの", "既定値ほか", "cc-setting-off", "オフなので届いていないもの、既定でオンのもの"),
]
b = Board("cc-settings", "Claude Code 設定項目別 (一覧)",
          "Claude Code の OTel 設定 (環境変数) ごとに、何が届いているかを見るダッシュボード群の入口",
          tags=["claude-code", "claude-code-setting"])
md = ("Claude Code の OTel 設定は環境変数 (dotfiles の `claude/telemetry-env.json` → `~/.claude/settings.json` の `env`) で"
      "項目ごとに決めている。ここから **設定項目 1 つにつき 1 枚** のダッシュボードへ飛ぶ。各ダッシュボードの先頭に、"
      "環境変数・いまの値・何が届くようになるか (公式 https://code.claude.com/docs/en/monitoring-usage)・止めると何が消えるかを書き、"
      "その下にその項目で届くようになった属性・ラベル・フィールドを **漏れなく** 表にした。\n\n"
      "| 環境変数 | いまの値 | 届くもの | ダッシュボード |\n|---|---|---|---|\n" +
      "\n".join(f"| `{e}` | {v} | {d} | [{u}](/d/{u}) |" for e, v, u, d in SETTINGS) +
      "\n\n対応表の全文 (どの属性がどの設定で増えるか、実データで確かめたか) はリポジトリの `docs/observability/claude-code-settings.md`。"
      "\n\nClaude Code の設定ではないが、`orca.worktree.name` で結合できる信号がもう 1 つある: Orca のオーケストレーション "
      "(Run / Task / ワーカー / メッセージ) をホストの `orca-exporter` が送っている (`service_name=\"orca\"`)。"
      "手戻り (追加指示の回数、worker_done までの時間) は [Orca orchestration](/d/orca-orchestration)。")
b.add(text("設定項目別ダッシュボードの一覧", md), 24, 21)
presence(b, "各設定で増えたものが届いているか (期間中の件数。0 = 届いていない)", [
    ("メトリクス系列 (METRICS_EXPORTER)", pc('{__name__=~"claude_code_.*"}')),
    ("イベント (LOGS_EXPORTER)", lc('event_name!=""')),
    ("スパン (TRACES)", tc('{resource.service.name="claude-code"}')),
    ("vcs ラベル付き系列 (INCLUDE_REPOSITORY)", pc('{__name__=~"claude_code_.*", vcs_repository_name!=""}')),
    ("orca ラベル付き系列 (RESOURCE_ATTRIBUTES)", pc('{__name__=~"claude_code_.*", orca_worktree_name!=""}')),
    ("依頼文が入った user_prompt (LOG_USER_PROMPTS)", lc('event_name="user_prompt" | prompt!="" | prompt!="<REDACTED>"')),
    ("tool_parameters 付き tool_result (LOG_TOOL_DETAILS)", lc('event_name="tool_result" | tool_parameters!=""')),
    ("応答文が入った assistant_response (LOG_ASSISTANT_RESPONSES)", lc('event_name="assistant_response" | response!="" | response!="<REDACTED>"')),
    ("tool.output スパンイベント (LOG_TOOL_CONTENT)", tc('{event:name="tool.output"}')),
    ("session_id ラベル付き系列 (INCLUDE_SESSION_ID)", pc('{__name__=~"claude_code_.*", session_id!=""}')),
])
add_board(b, "cc-settings.json")


# ================= 1. ENABLE_TELEMETRY =================
b = Board("cc-setting-enable-telemetry", "設定: CLAUDE_CODE_ENABLE_TELEMETRY", "テレメトリ全体の元スイッチで届くもの")
b.add(text("この設定について", header(
    "`CLAUDE_CODE_ENABLE_TELEMETRY`", "`1`",
    "テレメトリ収集そのものを有効にする (必須)。これだけでは送り先が決まらず、`OTEL_METRICS_EXPORTER` / `OTEL_LOGS_EXPORTER` / "
    "`OTEL_TRACES_EXPORTER` で信号ごとに送り先を選ぶ。どの信号にも共通して付く「標準属性」(`organization.id`, `user.*`, "
    "`terminal.type`) と、送り手のプロセスを表す resource 属性 (`service.name`, `service.version`, `os.*`, `host.arch`) はここで決まる。",
    "メトリクス・ログ・トレースのすべて。このスタックの全ダッシュボードが空になる。",
    "どの版の Claude Code から、どの端末・OS で、どのアカウントで送られているか。版の混在 (古いワーカーが残っている) の確認に使う。",
    "標準属性・resource 属性は実データの 3 信号すべてで確認済み。`user.email` / `user.id` / `organization.id` は常に付く (公式)。")), 24, 8)
presence(b, "信号ごとの件数", [
    ("メトリクス系列", pc('{__name__=~"claude_code_.*"}')),
    ("イベント", lc('event_name!=""')),
    ("スパン", tc('{resource.service.name="claude-code"}')),
])
b.add(ts("信号ごとの量の推移", [
    loki(f'sum(count_over_time({SEL} [$__interval]))', "イベント (件)", ref="A"),
    {**tempo_metrics('{resource.service.name="claude-code"} | count_over_time()', ref="B")},
    prom('count(claude_code_session_count_total)', "メトリクスの系列数", ref="C"),
], "イベントとスパンは時間あたりの件数、メトリクスはその時点の系列数。", bars=False), 24, 8)
b.row("resource 属性 (送り手のプロセス) と標準属性")
b.add(table("Claude Code の版・OS・端末ごとのイベント数", [
    loki(f'sum by (service_version, os_type, os_version, host_arch, wsl_version, terminal_type) (count_over_time({SEL} [$__range]))', instant=True)],
    "resource 属性はイベントのすべてに付く。版が混ざっていれば、古い版のワーカーが動いている。",
    [organize({"Value": "イベント数", "service_version": "版 (service.version)", "os_type": "os.type", "os_version": "os.version",
               "host_arch": "host.arch", "wsl_version": "wsl.version", "terminal_type": "terminal.type"}, {"Time": True})],
    sort="イベント数"), 12, 8)
b.add(table("Prometheus の target_info (resource 属性の全ラベル)", [prom('target_info', instant=True)],
            "OTLP で受けた resource 属性は Prometheus では `target_info` という系列のラベルになる。",
            [organize({}, {"Time": True, "Value": True, "__name__": True})]), 12, 8)
b.add(table("スパンの resource 属性キー", [inf(TEMPO_URL + "/api/v2/search/tags",
            [{"key": "q", "value": '{resource.service.name="claude-code"}'}, {"key": "scope", "value": "resource"}, FROM, TO],
            'scopes.( $s := name; $map(tags, function($x){ {"scope": $s, "key": $x} }) )')],
            "Tempo の resource scope の属性キー。"), 8, 8)
b.add(table("標準属性の値 (メトリクスのラベル)", [
    prom('count by (organization_id, user_account_id, user_account_uuid, user_email, user_id, terminal_type) (last_over_time(claude_code_cost_usage_USD_total[$__range]))', instant=True)],
    "どのメトリクスにも付く識別ラベル。`user.account_*` は `OTEL_METRICS_INCLUDE_ACCOUNT_UUID` (既定 true) で付く。",
    [organize({"Value": "系列数"}, {"Time": True})]), 16, 8)
add_board(b, "cc-setting-enable-telemetry.json")


# ================= 2. METRICS_EXPORTER =================
b = Board("cc-setting-metrics-exporter", "設定: OTEL_METRICS_EXPORTER", "メトリクスで届くものすべて",
          [var_prom("metric", "メトリクス", "metrics(claude_code_.*)")])
b.variables[0]["query"] = {"qryType": 2, "query": "metrics(claude_code_.*)", "refId": "PrometheusVariableQueryEditor-VariableQuery"}
b.add(text("この設定について", header(
    "`OTEL_METRICS_EXPORTER`", "`otlp` (送り先は `OTEL_EXPORTER_OTLP_ENDPOINT` = Collector、温度は `cumulative`)",
    "メトリクス (カウンタ) を送る。公式のメトリクスは 8 種: session / lines_of_code / pull_request / commit / cost / token / "
    "code_edit_tool_decision / active_time。60 秒ごと (`OTEL_METRIC_EXPORT_INTERVAL`) に送る。Prometheus では "
    "`claude_code_<名前>_<単位>_total` になり、属性の `.` は `_` になる。",
    "`claude_code_*` の全系列。usage ダッシュボードのコスト・トークン・アクティブ時間、はじめにの数字が消える (ログ・トレースは残る)。",
    "コスト・トークン・作業時間を、モデル・ワーカー・リポジトリ・skill・MCP サーバー別に長期間で見る。個々の依頼の中身は分からない。",
    "公式の 8 種のうち、実データで届いているのは下の表の通り。pull_request / commit は PR やコミットを作ったときだけ系列ができる。")), 24, 8)
b.row("公式のメトリクス一覧と届いているか")
doc_arr = json.dumps(METRICS_DOC)
b.add(table("公式のメトリクス 8 種 × 届いた系列数", [inf(PROM_URL + "/api/v1/series",
            [{"key": "match[]", "value": '{__name__=~"claude_code_.*"}'}, FROM, TO],
            '( $d := data; $names := $distinct($d.__name__); $append(%s, $names[$not($ in %s)]).( $m := $; '
            '{"metric": $m, "doc": $m in %s ? "公式" : "公式に無い", "series": $count($d[__name__ = $m])} ) )' % (doc_arr, doc_arr, doc_arr))],
            "0 = 公式にはあるが期間中に 1 度も送られていない (PR やコミットを作っていないなど)。",
            [organize({"metric": "メトリクス", "doc": "出どころ", "series": "系列数"})], sort="系列数"), 10, 9)
b.add(table("メトリクスごとの現在値と期間中の増加", [
    prom('count by (__name__) (last_over_time({__name__=~"claude_code_.*"}[$__range]))', instant=True, ref="A"),
    prom('sum by (__name__) (label_replace(sum by (m) (increase(label_replace({__name__=~"claude_code_.*"}, "m", "$1", "__name__", "(.*)")[$__range:1m])), "__name__", "$1", "m", "(.*)"))', instant=True, ref="B")],
    "系列数 = ラベルの組み合わせの数。増加 = 期間中に増えた量 (コストなら USD、トークンなら個数、時間なら秒)。",
    [MERGE, organize({"__name__": "メトリクス", "Value #A": "系列数", "Value #B": "期間中の増加"}, {"Time": True})],
    sort="系列数"), 14, 9)
b.row("全メトリクス × 全ラベル")
metric_label_tables(b)
b.row("各メトリクスの推移 ($metric、ラベルの内訳ごと)")
p = ts("$metric (1 分あたりの増加、内訳ごと)", [prom(
    f'sum without ({IDENT}) (rate({{__name__="$metric"}}[$__rate_interval])) * 60')],
    "識別用のラベル (ユーザー・組織・端末など、どの系列にも同じ値で付くもの) を外し、残りのラベルの組ごとに描いた。")
p["repeat"] = "metric"
p["repeatDirection"] = "h"
p["maxPerRow"] = 2
p["options"]["legend"]["placement"] = "bottom"
b.add(p, 12, 9)
add_board(b, "cc-setting-metrics-exporter.json")


# ================= 3. LOGS_EXPORTER =================
b = Board("cc-setting-logs-exporter", "設定: OTEL_LOGS_EXPORTER", "ログ / イベントで届くものすべて",
          [var_inf("event", "イベント種別", LOKI_URL + "/loki/api/v1/query_range",
                   [{"key": "query", "value": f'sum by (event_name) (count_over_time({SEL} {RANGE}))'},
                    FROM, TO, {"key": "step", "value": "300"}], "data.result.metric.event_name",
                   multi=False, include_all=False)])
b.variables[0]["current"] = {"text": "tool_result", "value": "tool_result"}
b.add(text("この設定について", header(
    "`OTEL_LOGS_EXPORTER`", "`otlp`",
    "イベント (OTel のログ) を送る。依頼 (`user_prompt`)、応答 (`assistant_response`)、API 呼び出し (`api_request` / `api_error`)、"
    "ツールの許可と結果 (`tool_decision` / `tool_result`)、hook・skill・MCP 接続・plugin など公式で 27 種。"
    "`prompt.id` で同じ依頼のイベントをまとめられ、`trace_id` / `span_id` でトレースに繋がる。Loki では属性が structured metadata になり、`.` は `_` になる。",
    "Loki のイベントすべて。依頼ごとのコスト (improve)、依頼文・応答文・コマンド・ツールの失敗の一覧が消える。"
    "`OTEL_LOG_USER_PROMPTS` / `OTEL_LOG_TOOL_DETAILS` / `OTEL_LOG_ASSISTANT_RESPONSES` はイベントの中身を増やす設定なので、これを止めると意味を失う。",
    "依頼 1 回ずつ、API 呼び出し 1 回ずつ、ツール呼び出し 1 回ずつの記録。コストの高い依頼、失敗の多いコマンド、使われていない skill を探す。",
    "公式の 27 種のうち届いているのは下の表の通り。属性キーはすべて実データから数えている。")), 24, 8)
b.row("公式のイベント種別と届いているか")
b.add(table("公式のイベント 27 種 × 届いた件数", [inf_loki_instant(
    f'sum by (event_name) (count_over_time({SEL} {RANGE}))',
    '( $r := data.result; $names := $r.metric.event_name; $doc := %s; $append($doc, $names[$not($ in $doc)]).( $e := $; '
    '{"event": $e, "doc": $e in $doc ? "公式" : "公式に無い", "count": $sum($r[metric.event_name = $e].$sum(values.$number($[1])))} ) )'
    % json.dumps(EVENTS_DOC))],
    "0 = 公式にはあるが期間中に起きていない (API エラー、compaction など) か、別の設定が要る (`api_request_body` は `OTEL_LOG_RAW_API_BODIES`)。",
    [organize({"event": "イベント種別", "doc": "出どころ", "count": "件数"})], sort="件数"), 8, 16)
b.add(ts("イベント種別ごとの件数の推移", [loki(f'sum by (event_name) (count_over_time({SEL} [$__interval]))', "{{event_name}}")],
         "", stack=True, bars=True), 16, 16)
b.row("全イベント種別 × 全属性")
event_attr_tables(b, h=18)
b.row("選んだイベント種別 ($event) の生データ")
p = copy.deepcopy(RAW[11])
p.pop("id", None); p.pop("gridPos", None)
b.add(p, 24, 12)
add_board(b, "cc-setting-logs-exporter.json")


# ================= 4. TRACES =================
b = Board("cc-setting-traces", "設定: CLAUDE_CODE_ENHANCED_TELEMETRY_BETA + OTEL_TRACES_EXPORTER", "トレースで届くものすべて")
b.add(text("この設定について", header(
    "`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA` と `OTEL_TRACES_EXPORTER`", "`1` / `otlp`",
    "トレース (beta)。依頼 1 回が `claude_code.interaction` の木になり、その下に `claude_code.llm_request` (API 呼び出し)、"
    "`claude_code.tool` (ツール呼び出し) とその子の `claude_code.tool.blocked_on_user` (許可待ち) / `claude_code.tool.execution` (実行) が付く。"
    "サブエージェントのスパンは親の `claude_code.tool` の下に入る。`claude_code.hook` は `ENABLE_BETA_TRACING_DETAILED=1` のときだけ。"
    "スパンイベントは再試行の `gen_ai.request.attempt` と、`OTEL_LOG_TOOL_CONTENT=1` のときの `tool.output`。",
    "Tempo のスパンすべて。traces ダッシュボードのウォーターフォール、所要時間の p50 / p95、ログからトレースへのリンクが消える。",
    "依頼の中で何がどの順に何秒かかったか。遅い依頼がモデル待ちか、ツール待ちか、許可待ちかを分ける。",
    "スパン名・属性は実データで確認。`claude_code.hook` は公式では detailed beta tracing が要るが、オンにしていないのに数件届いている (出どころ未確認)。"
    "公式の表に無い属性 (`queued_sends` など) も実データにあれば下の表に出る。")), 24, 8)
b.row("スパン名ごとの件数")
b.add(table("公式のスパン名 × 届いた件数", [inf(TEMPO_URL + "/api/metrics/query",
            [{"key": "q", "value": '{resource.service.name="claude-code"} | count_over_time() by (name)'},
             FROM, TO],
            '( $s := series; $names := $s.labels[key="name"].value.stringValue; $doc := %s; $append($doc, $names[$not($ in $doc)]).( $n := $; '
            '{"span": $n, "doc": $n in $doc ? "公式" : "公式に無い", "count": $sum($s[labels[key="name"].value.stringValue = $n].value)} ) )'
            % json.dumps(SPANS_DOC))],
            "TraceQL `count_over_time() by (name)` の期間合計。0 = 届いていない。",
            [organize({"span": "スパン名", "doc": "出どころ", "count": "件数"})], sort="件数"), 9, 9)
b.add(ts("スパン名ごとの件数の推移", [tempo_metrics('{resource.service.name="claude-code"} | count_over_time() by (name)')],
         "", stack=True, bars=True), 15, 9)
b.row("全スパン名 × 全属性")
span_attr_tables(b)
b.row("スパンイベントとリンク")
b.add(table("スパンイベントの種類と件数", [tempo_metrics('{event:name != nil} | count_over_time() by (event:name)', instant=True)],
            "`gen_ai.request.attempt` = API の再試行 1 回ごと、`tool.output` = ツールの出力 (`OTEL_LOG_TOOL_CONTENT`)。",
            [reduce_rows("sum"), organize({"Field": "イベント名", "Total": "件数"})]), 8, 8)
b.add(table("スパンイベントの属性キー", [
    inf(TEMPO_URL + "/api/v2/search/tags", [{"key": "q", "value": '{event:name="%s"}' % n}, {"key": "scope", "value": "event"}, FROM, TO],
        '$map(scopes.tags, function($x){ {"event": "%s", "key": $x} })' % n, chr(65 + i)) for i, n in enumerate(["gen_ai.request.attempt", "tool.output"])],
    "Tempo のタグ一覧 API を scope=event で引いた。", [MERGE]), 8, 8)
b.add(table("リンクの属性キー", [inf(TEMPO_URL + "/api/v2/search/tags", [{"key": "scope", "value": "link"}, FROM, TO],
                                  '$map(scopes.tags, function($x){ {"key": $x} })')],
            "`llm_request` は API 側のトレース (`traceresponse`) へのリンクを持つ。"), 8, 8)
b.add(table("最近のスパンイベント (gen_ai.request.attempt)", [tempo_search('{event:name="gen_ai.request.attempt"} | select(event.attempt, span.model)', 30, 3)],
            "再試行があったリクエスト。"), 24, 8)
b.row("スパン名ごとの所要時間")
b.add(ts("所要時間 p50 / p95 (スパン名ごと)", [tempo_metrics('{resource.service.name="claude-code"} | quantile_over_time(duration, .5, .95) by (name)')],
         "", unit="s"), 24, 8)
add_board(b, "cc-setting-traces.json")


# ================= 5. INCLUDE_REPOSITORY =================
b = Board("cc-setting-include-repository", "設定: OTEL_METRICS_INCLUDE_REPOSITORY", "vcs.* (リポジトリ) の属性で届くもの")
b.add(text("この設定について", header(
    "`OTEL_METRICS_INCLUDE_REPOSITORY`", "`true` (既定 false、v2.1.269 以降)",
    "セッションのリポジトリ (`origin` リモートから求める) を `vcs.repository.url.full`, `vcs.owner.name`, `vcs.repository.name`, "
    "`vcs.provider.name` としてメトリクスとイベントに付ける。",
    "メトリクス・イベントの `vcs_*` ラベル。リポジトリ別のコスト・トークンが出せなくなる。",
    "どのリポジトリの作業にいくらかかったか。git リポジトリの外 (`~` の coordinator など) で動いたものは値が付かない。",
    "メトリクスとイベントで実データを確認。**スパンにも `vcs.*` が付いている** (公式は metrics and events と書く。スパンへの付与が"
    "この設定によるかは未確認、推定)。git commit したときの `vcs.ref.head.*` は `OTEL_LOG_TOOL_DETAILS` 側 (そちらのダッシュボード)。")), 24, 8)
presence(b, "vcs が付いた件数", [
    ("vcs 付きメトリクス系列", pc('{__name__=~"claude_code_.*", vcs_repository_name!=""}')),
    ("vcs 無しメトリクス系列", pc('{__name__=~"claude_code_.*", vcs_repository_name=""}')),
    ("vcs 付きイベント", lc('vcs_repository_name!=""')),
    ("vcs 無しイベント", lc('vcs_repository_name=""')),
    ("vcs 付きスパン", tc('{span.vcs.repository.name != nil}')),
])
b.row("値の一覧")
b.add(table("vcs の値の一覧 (メトリクスの系列数、イベント数)", [
    prom('count by (vcs_provider_name, vcs_owner_name, vcs_repository_name, vcs_repository_url_full) (last_over_time({__name__=~"claude_code_.*"}[$__range]))', instant=True, ref="A"),
    loki(f'sum by (vcs_provider_name, vcs_owner_name, vcs_repository_name, vcs_repository_url_full) (count_over_time({SEL} [$__range]))', instant=True, ref="B")],
    "空欄の行 = リポジトリの外で動いたもの。", [MERGE, organize({"Value #A": "メトリクス系列数", "Value #B": "イベント数"}, {"Time": True})],
    sort="イベント数"), 24, 6)
b.row("リポジトリで分けたコスト・トークン")
b.add(table("リポジトリ別のコスト・トークン・作業時間 (期間中)", [
    prom('sum by (vcs_repository_name) (increase(claude_code_cost_usage_USD_total[$__range]))', instant=True, ref="A"),
    prom('sum by (vcs_repository_name) (increase(claude_code_token_usage_tokens_total[$__range]))', instant=True, ref="B"),
    prom('sum by (vcs_repository_name) (increase(claude_code_active_time_seconds_total[$__range]))', instant=True, ref="C"),
    prom('sum by (vcs_repository_name) (increase(claude_code_session_count_total[$__range]))', instant=True, ref="D"),
    loki(f'sum by (vcs_repository_name) (count_over_time({SEL} | event_name="user_prompt" [$__range]))', instant=True, ref="E")],
    "vcs_repository_name が空 = リポジトリの外。",
    [MERGE, organize({"vcs_repository_name": "リポジトリ", "Value #A": "コスト (USD)", "Value #B": "トークン",
                      "Value #C": "作業時間 (秒)", "Value #D": "セッション", "Value #E": "依頼"}, {"Time": True})],
    [{"matcher": {"id": "byName", "options": "コスト (USD)"}, "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 3}]},
     {"matcher": {"id": "byName", "options": "作業時間 (秒)"}, "properties": [{"id": "unit", "value": "s"}]}],
    sort="コスト (USD)"), 12, 8)
b.add(table("リポジトリ × トークンの種類", [
    prom('sum by (vcs_repository_name, type) (increase(claude_code_token_usage_tokens_total[$__range]))', instant=True)], "",
    [{"id": "groupingToMatrix", "options": {"columnField": "type", "rowField": "vcs_repository_name", "valueField": "Value"}}]), 12, 8)
b.add(ts("リポジトリ別のコスト (USD / 時)", [prom('sum by (vcs_repository_name) (rate(claude_code_cost_usage_USD_total[$__rate_interval])) * 3600', "{{vcs_repository_name}}")],
         unit="currencyUSD", stack=True), 12, 8)
b.add(ts("リポジトリ別のトークン (/分)", [prom('sum by (vcs_repository_name) (rate(claude_code_token_usage_tokens_total[$__rate_interval])) * 60', "{{vcs_repository_name}}")],
         stack=True), 12, 8)
b.add(ts("リポジトリ別のイベント数", [loki(f'sum by (vcs_repository_name) (count_over_time({SEL} [$__interval]))', "{{vcs_repository_name}}")],
         bars=True, stack=True), 12, 8)
b.add(ts("リポジトリ別のスパン数", [tempo_metrics('{resource.service.name="claude-code"} | count_over_time() by (span.vcs.repository.name)')],
         bars=True, stack=True), 12, 8)
add_board(b, "cc-setting-include-repository.json")


# ================= 6. RESOURCE_ATTRIBUTES =================
b = Board("cc-setting-resource-attributes", "設定: OTEL_RESOURCE_ATTRIBUTES", "orca.worktree.* (ワーカー) の属性で届くもの")
b.add(text("この設定について", header(
    "`OTEL_RESOURCE_ATTRIBUTES`", "`orca.worktree.id=<id>,orca.worktree.name=<名前>` (Orca の端末から起動したときだけ、dotfiles の `shell/prompt.sh` が立てる)",
    "任意の `key=value` を resource 属性として全信号に付ける。メトリクスでは `OTEL_METRICS_INCLUDE_RESOURCE_ATTRIBUTES` (既定 true) により"
    "データ点のラベルにもなる (`orca_worktree_id`, `orca_worktree_name`)。",
    "`orca_worktree_*` の全ラベル・属性。ワーカー別の集計 (usage / improve の「ワーカー」) がすべて「(Orca の外)」になる。",
    "どのワーカー (worktree) がいくら使い、何件依頼を受けたか。coordinator とワーカーの負担の比較。",
    "メトリクス・イベント・スパン (resource と span の両方) で実データを確認。値が無いもの = Orca の外 (ターミナル直、`claude -p` を素で打った回など)。")), 24, 8)
presence(b, "orca 属性が付いた件数", [
    ("orca 付きメトリクス系列", pc('{__name__=~"claude_code_.*", orca_worktree_name!=""}')),
    ("orca 無しメトリクス系列", pc('{__name__=~"claude_code_.*", orca_worktree_name=""}')),
    ("orca 付きイベント", lc('orca_worktree_name!=""')),
    ("orca 無しイベント", lc('orca_worktree_name=""')),
    ("orca 付きスパン (resource)", tc('{resource.orca.worktree.name != nil}')),
    ("orca 付きスパン (span 属性)", tc('{span.orca.worktree.name != nil}')),
])
b.row("値の一覧")
b.add(table("orca_worktree_* の値の一覧", [
    prom('count by (orca_worktree_id, orca_worktree_name) (last_over_time({__name__=~"claude_code_.*"}[$__range]))', instant=True, ref="A"),
    loki(f'sum by (orca_worktree_id, orca_worktree_name) (count_over_time({SEL} [$__range]))', instant=True, ref="B")],
    "空欄 = Orca の外。", [MERGE, organize({"Value #A": "メトリクス系列数", "Value #B": "イベント数"}, {"Time": True})],
    sort="イベント数"), 24, 8)
b.row("ワーカーで分けた主要指標")
b.add(table("ワーカー別の主要指標 (期間中)", [
    prom('sum by (orca_worktree_name) (increase(claude_code_cost_usage_USD_total[$__range]))', instant=True, ref="A"),
    prom('sum by (orca_worktree_name) (increase(claude_code_token_usage_tokens_total[$__range]))', instant=True, ref="B"),
    prom('sum by (orca_worktree_name) (increase(claude_code_active_time_seconds_total[$__range]))', instant=True, ref="C"),
    prom('sum by (orca_worktree_name) (increase(claude_code_lines_of_code_count_total[$__range]))', instant=True, ref="D"),
    loki(f'sum by (orca_worktree_name) (count_over_time({SEL} | event_name="user_prompt" [$__range]))', instant=True, ref="E"),
    loki(f'sum by (orca_worktree_name) (count_over_time({SEL} | event_name="tool_result" [$__range]))', instant=True, ref="F"),
    loki(f'sum by (orca_worktree_name) (count_over_time({SEL} | event_name="tool_result" | success="false" [$__range]))', instant=True, ref="G")],
    "orca_worktree_name が空 = Orca の外。",
    [MERGE, organize({"orca_worktree_name": "ワーカー", "Value #A": "コスト (USD)", "Value #B": "トークン", "Value #C": "作業時間 (秒)",
                      "Value #D": "変更行", "Value #E": "依頼", "Value #F": "ツール呼び出し", "Value #G": "ツールの失敗"}, {"Time": True})],
    [{"matcher": {"id": "byName", "options": "コスト (USD)"}, "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 3}]},
     {"matcher": {"id": "byName", "options": "作業時間 (秒)"}, "properties": [{"id": "unit", "value": "s"}]},
     {"matcher": {"id": "byName", "options": "ワーカー"}, "properties": [{"id": "custom.width", "value": 300}]}],
    sort="コスト (USD)"), 24, 9)
b.add(ts("ワーカー別のコスト (USD / 時)", [prom('sum by (orca_worktree_name) (rate(claude_code_cost_usage_USD_total[$__rate_interval])) * 3600', "{{orca_worktree_name}}")],
         unit="currencyUSD", stack=True), 12, 9)
b.add(ts("ワーカー別のイベント数", [loki(f'sum by (orca_worktree_name) (count_over_time({SEL} [$__interval]))', "{{orca_worktree_name}}")],
         bars=True, stack=True), 12, 9)
b.add(ts("ワーカー別の依頼の所要時間 p95 (スパン)", [tempo_metrics('{name="claude_code.interaction"} | quantile_over_time(duration, .95) by (resource.orca.worktree.name)')],
         unit="s"), 24, 8)
add_board(b, "cc-setting-resource-attributes.json")


# ================= 7. LOG_USER_PROMPTS =================
b = Board("cc-setting-log-user-prompts", "設定: OTEL_LOG_USER_PROMPTS", "依頼文で届くもの")
b.add(text("この設定について", header(
    "`OTEL_LOG_USER_PROMPTS`", "`1`",
    "依頼文の中身を送る。イベント `user_prompt` の `prompt` 属性と、スパン `claude_code.interaction` の `user_prompt` 属性。"
    "オフのときも `prompt_length` / `user_prompt_length` (長さ) は届き、中身は `<REDACTED>`。"
    "`OTEL_LOG_ASSISTANT_RESPONSES` が未設定なら、応答文もこの設定に従う。detailed beta tracing のときは `new_context` / `user_system_prompt` もこれで出る。",
    "依頼文の中身 (長さと件数は残る)。improve の「コストの高い依頼」「依頼文の検索」が `<REDACTED>` だけになる。",
    "どんな依頼にいくらかかったか、同じ依頼を何度も出していないか。依頼文の書き方とコストの関係を見る。",
    "イベントとスパンの両方で実データを確認。この設定が入る前のイベントは `<REDACTED>` で残っている (下の件数)。")), 24, 8)
presence(b, "依頼文が届いているか", [
    ("user_prompt (中身あり)", lc('event_name="user_prompt" | prompt!="" | prompt!="<REDACTED>"')),
    ("user_prompt (<REDACTED>)", lc('event_name="user_prompt" | prompt="<REDACTED>"')),
    ("interaction スパン (中身あり)", tc('{name="claude_code.interaction" && span.user_prompt != nil && span.user_prompt != "<REDACTED>"}')),
    ("interaction スパン (<REDACTED>)", tc('{name="claude_code.interaction" && span.user_prompt = "<REDACTED>"}')),
])
b.row("依頼文の一覧 (依頼ごとのコストつき)")
b.add(table("依頼文の一覧 (新しい順)", [
    loki(f'sum by (prompt_id) (sum_over_time({SEL} | event_name="api_request" | unwrap cost_usd [$__range]))', instant=True, ref="A"),
    loki(f'count by (prompt_id, prompt, prompt_length, trace_id, w) (count_over_time({SEL} | event_name="user_prompt" | label_format w=`{{{{ or .orca_worktree_name "(Orca の外)" }}}}` | label_format prompt=`{{{{ .prompt | replace "\\n" " " | trunc 1000 }}}}` [$__range]))', instant=True, ref="B"),
    loki(f'sum by (prompt_id) (count_over_time({SEL} | event_name="api_request" [$__range]))', instant=True, ref="C"),
    loki(f'sum by (prompt_id) (count_over_time({SEL} | event_name="tool_result" [$__range]))', instant=True, ref="D"),
    loki(f'min_over_time({SEL} | prompt_id!="" | unwrap observed_timestamp [$__range]) by (prompt_id) / 1e6', instant=True, ref="E")],
    "依頼 (prompt_id) ごとに、依頼文・長さ・コスト (その依頼の api_request の cost_usd の合計)・API / ツール呼び出し数を並べた。依頼文をクリックするとトレースへ。",
    [MERGE, {"id": "filterByValue", "options": {"type": "exclude", "match": "any", "filters": [
        {"fieldName": "trace_id", "config": {"id": "isNull", "options": {}}}]}},
     organize({"Value #E": "開始", "prompt": "依頼文", "prompt_length": "長さ", "w": "ワーカー", "Value #A": "コスト (USD)",
               "Value #C": "API 呼び出し", "Value #D": "ツール呼び出し"}, {"Time": True, "Value #B": True},
              {"Value #E": 0, "prompt": 1, "prompt_length": 2, "w": 3, "Value #A": 4, "Value #C": 5, "Value #D": 6, "trace_id": 7, "prompt_id": 8}),
     {"id": "convertFieldType", "options": {"conversions": [{"targetField": "長さ", "destinationType": "number"}]}},
     sort_by("開始")],
    [{"matcher": {"id": "byName", "options": "開始"}, "properties": [{"id": "unit", "value": "dateTimeAsLocal"}, {"id": "custom.width", "value": 150}]},
     {"matcher": {"id": "byName", "options": "依頼文"}, "properties": [
         {"id": "links", "value": [{"title": "トレースを開く", "url": "", "internal": {"datasourceUid": "tempo", "datasourceName": "Tempo",
                                                                               "query": {"queryType": "traceql", "query": "${__data.fields.trace_id}"}}}]},
         {"id": "custom.cellOptions", "value": {"type": "auto", "wrapText": True}}, {"id": "custom.minWidth", "value": 420}]},
     {"matcher": {"id": "byName", "options": "コスト (USD)"}, "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 3}]}]),
    24, 14)
b.row("件数・長さ・時系列")
b.add(ts("依頼の件数", [loki(f'sum(count_over_time({SEL} | event_name="user_prompt" [$__interval]))', "依頼")], bars=True), 8, 8)
b.add(ts("依頼文の長さ (最大・平均、文字)", [
    loki(f'max(max_over_time({SEL} | event_name="user_prompt" | unwrap prompt_length [$__interval]))', "最大", ref="A"),
    loki(f'avg(avg_over_time({SEL} | event_name="user_prompt" | unwrap prompt_length [$__interval]))', "平均", ref="B")]), 8, 8)
b.add(table("長さの分布", [
    loki(f'sum(count_over_time({SEL} | event_name="user_prompt" | prompt_length < 100 [$__range]))', instant=True, ref="A"),
    loki(f'sum(count_over_time({SEL} | event_name="user_prompt" | prompt_length >= 100 | prompt_length < 500 [$__range]))', instant=True, ref="B"),
    loki(f'sum(count_over_time({SEL} | event_name="user_prompt" | prompt_length >= 500 | prompt_length < 2000 [$__range]))', instant=True, ref="C"),
    loki(f'sum(count_over_time({SEL} | event_name="user_prompt" | prompt_length >= 2000 [$__range]))', instant=True, ref="D")],
    "prompt_length (文字数) の区間ごとの件数。", [MERGE, organize({"Value #A": "〜99 字", "Value #B": "100〜499 字", "Value #C": "500〜1999 字", "Value #D": "2000 字〜"}, {"Time": True})]), 8, 8)
b.add(table("interaction スパンの user_prompt (Tempo 側、新しい順)", [tempo_search(
    '{name="claude_code.interaction"} | select(span.user_prompt, span.user_prompt_length, span.interaction.duration_ms, resource.orca.worktree.name)', 50, 1)],
    "同じ依頼文がスパンの属性にも入る。"), 24, 9)
add_board(b, "cc-setting-log-user-prompts.json")


# ================= 8. LOG_TOOL_DETAILS =================
b = Board("cc-setting-log-tool-details", "設定: OTEL_LOG_TOOL_DETAILS", "ツールの詳細 (コマンド・パス・skill / MCP 名・入力) で届くもの")
b.add(text("この設定について", header(
    "`OTEL_LOG_TOOL_DETAILS`", "`1`",
    "ツールの引数と名前を送る。`tool_result` / `tool_decision` の `tool_parameters` (Bash の `bash_command` / `full_command` / `timeout` / "
    "`description`、MCP の `mcp_server_name` / `mcp_tool_name`、Skill の `skill_name`、Agent の `subagent_type`)、`tool_result` の `tool_input` "
    "(引数全体の JSON、4K 字まで) と `error` (失敗の全文) と git commit 時の `vcs.ref.head.*`。スパン `claude_code.tool` の `file_path` / "
    "`full_command` / `skill_name` / `subagent_type`。`user_prompt` の `command_name` (カスタム・MCP コマンドの実名)。コスト・トークンのメトリクスと "
    "`api_request` の `skill.name` / `mcp_server.name` / `mcp_tool.name` / `agent.name` / `plugin.name` の実名 (オフなら `custom` / `third-party`)。"
    "`mcp_server_connection` の `server_name`、`hook_registered` の `hook_matcher`、`skill_activated` の実名。",
    "上の属性すべて。improve の「失敗の多いコマンド」、skill / MCP の呼び出し回数、ファイルパスが消える。",
    "どのコマンドが失敗を繰り返しているか、どのファイルを何度読んでいるか、どの skill / MCP ツールが使われているか。",
    "下の件数で実データを確認。スパン属性と tool_parameters は公式どおり。`tool_input` のキーはツールごとの引数そのもの (下の表)。"
    "metric の `mcp_server_name` に `custom` が残っているのは、この設定より前 (または v2.1.273 より前) の系列 (推定)。")), 24, 10)
presence(b, "この設定で増える属性が届いた件数", [
    ("tool_result.tool_parameters", lc('event_name="tool_result" | tool_parameters!=""')),
    ("tool_decision.tool_parameters", lc('event_name="tool_decision" | tool_parameters!=""')),
    ("tool_result.tool_input", lc('event_name="tool_result" | tool_input!=""')),
    ("tool_result.error (全文)", lc('event_name="tool_result" | error!=""')),
    ("tool_result.vcs_ref_head_*", lc('event_name="tool_result" | vcs_ref_head_revision!=""')),
    ("user_prompt.command_name", lc('event_name="user_prompt" | command_name!=""')),
    ("api_request.skill_name", lc('event_name="api_request" | skill_name!=""')),
    ("api_request.mcp_server_name", lc('event_name="api_request" | mcp_server_name!=""')),
    ("api_request.agent_name", lc('event_name="api_request" | agent_name!=""')),
    ("skill_activated", lc('event_name="skill_activated"')),
    ("mcp_server_connection.server_name", lc('event_name="mcp_server_connection" | server_name!=""')),
    ("hook_registered.hook_matcher", lc('event_name="hook_registered" | hook_matcher!=""')),
    ("span tool.full_command", tc('{name="claude_code.tool" && span.full_command != nil}')),
    ("span tool.file_path", tc('{name="claude_code.tool" && span.file_path != nil}')),
    ("span tool.skill_name", tc('{name="claude_code.tool" && span.skill_name != nil}')),
    ("span tool.subagent_type", tc('{name="claude_code.tool" && span.subagent_type != nil}')),
    ("metric の skill_name 系列", pc('{__name__=~"claude_code_(cost|token).*", skill_name!=""}')),
    ("metric の mcp_server_name 系列", pc('{__name__=~"claude_code_(cost|token).*", mcp_server_name!=""}')),
    ("metric の agent_name 系列", pc('{__name__=~"claude_code_(cost|token).*", agent_name!=""}')),
])
b.row("ツール × 引数のキー (tool_parameters と tool_input の中身を展開)")
def json_keys_target(field, event_filter=""):
    """JSON 文字列の属性 (tool_parameters / tool_input) のキー名だけを正規表現で抜き出して数える。
    値まで json で展開すると系列が 500 を超えるため、キー名の並びだけをラベルにする。"""
    expr = ("sum by (event_name, tool_name, keys) (count_over_time(" + SEL + event_filter + " | " + field + '!="" '
            "| label_format keys=`{{ regexReplaceAll \"\\\"([^\\\"]+)\\\":(\\\"(\\\\\\\\.|[^\\\"\\\\\\\\])*\\\"|[^,}]*)[,}]?\" ." + field + " \"${1} \" }}` " + RANGE + "))")
    root = ('( $rows := data.result.( $m := metric; $n := $sum(values.$number($[1])); '
            '$map($filter($split($replace($string($m.keys), "{", ""), " "), function($x){ $x != "" and $x != "}" }), '
            'function($x){ {"event": $m.event_name, "tool": $m.tool_name, "key": $x, "n": $n} }) ); '
            '$distinct($rows.(event & "|" & tool & "|" & key)).( $k := $; $g := $rows[(event & "|" & tool & "|" & key) = $k]; '
            '{"event": $g[0].event, "tool": $g[0].tool, "key": $g[0].key, "count": $sum($g.n)} ) )')
    return inf_loki_instant(expr, root)


b.add(table("ツール × tool_parameters のキー", [json_keys_target("tool_parameters")],
    "tool_parameters (JSON) のキー名を抜き出し、イベント・ツールごとにどのキーが何件入っていたかを数えた。",
    [organize({"event": "イベント", "tool": "ツール", "key": "キー", "count": "件数"}, {}, {"event": 0, "tool": 1, "key": 2, "count": 3})],
    sort="件数"), 12, 10)
b.add(table("ツール × tool_input のキー", [json_keys_target("tool_input", ' | event_name="tool_result"')],
    "tool_input (ツールに渡した引数の JSON) の 1 段目のキー。ツールの引数そのものなので、ツールごとに違う。",
    [organize({"event": "イベント", "tool": "ツール", "key": "キー", "count": "件数"}, {"event": True}, {"tool": 0, "key": 1, "count": 2})],
    sort="件数"), 12, 10)
b.row("コマンド・ファイルパス")
b.add(table("Bash のコマンド (多い順、失敗数つき)", [
    loki(f'sum by (cmd) (count_over_time({SEL} | event_name="tool_result" | tool_name="Bash" | line_format `{{{{.tool_parameters}}}}` | json cmd="bash_command" | label_format cmd=`{{{{ .cmd | replace "\\n" " " | trunc 300 }}}}` [$__range]))', instant=True, ref="A"),
    loki(f'sum by (cmd) (count_over_time({SEL} | event_name="tool_result" | tool_name="Bash" | success="false" | line_format `{{{{.tool_parameters}}}}` | json cmd="bash_command" | label_format cmd=`{{{{ .cmd | replace "\\n" " " | trunc 300 }}}}` [$__range]))', instant=True, ref="B")],
    "tool_parameters.bash_command (最初の 300 字)。", [MERGE, organize({"cmd": "コマンド", "Value #A": "回数", "Value #B": "失敗"}, {"Time": True})],
    sort="回数", wrap=["コマンド"]), 12, 12)
b.add(table("ファイルパス (Read / Edit / Write など、多い順)", [loki(
    f'sum by (tool_name, path) (count_over_time({SEL} | event_name="tool_result" | tool_input=~".*file_path.*" | line_format `{{{{.tool_input}}}}` | json path="file_path" [$__range]))', instant=True)],
    "tool_input.file_path。", [organize({"tool_name": "ツール", "path": "ファイルパス", "Value": "回数"}, {"Time": True})],
    sort="回数", wrap=["ファイルパス"]), 12, 12)
b.row("skill・MCP・サブエージェント・git")
b.add(table("skill 名 (出どころ別)", [
    loki(f'sum by (skill_name) (count_over_time({SEL} | event_name="tool_result" | tool_name="Skill" | line_format `{{{{.tool_parameters}}}}` | json skill_name [$__range]))', instant=True, ref="A"),
    loki(f'sum by (skill_name) (count_over_time({SEL} | event_name="skill_activated" [$__range]))', instant=True, ref="B"),
    loki(f'sum by (skill_name) (count_over_time({SEL} | event_name="api_request" | skill_name!="" [$__range]))', instant=True, ref="C"),
    prom('sum by (skill_name) (increase(claude_code_cost_usage_USD_total{skill_name!=""}[$__range]))', instant=True, ref="D")],
    "Skill ツールの呼び出し、skill_activated イベント、skill 中の API 呼び出し数、skill 中のコスト。",
    [MERGE, organize({"skill_name": "skill", "Value #A": "Skill ツール", "Value #B": "skill_activated", "Value #C": "API 呼び出し", "Value #D": "コスト (USD)"}, {"Time": True})],
    [{"matcher": {"id": "byName", "options": "コスト (USD)"}, "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 3}]}]), 12, 8)
b.add(table("MCP サーバー / ツール (出どころ別)", [
    loki(f'sum by (mcp_server_name, mcp_tool_name) (count_over_time({SEL} | event_name="tool_result" | tool_name="mcp_tool" | line_format `{{{{.tool_parameters}}}}` | json mcp_server_name, mcp_tool_name [$__range]))', instant=True, ref="A"),
    loki(f'sum by (mcp_server_name, mcp_tool_name) (count_over_time({SEL} | event_name="api_request" | mcp_server_name!="" [$__range]))', instant=True, ref="B"),
    prom('sum by (mcp_server_name, mcp_tool_name) (increase(claude_code_cost_usage_USD_total{mcp_server_name!=""}[$__range]))', instant=True, ref="C")],
    "tool_result の tool_parameters、api_request の属性、コストのメトリクスのラベル。`custom` = 実名が伏せられた値。",
    [MERGE, organize({"mcp_server_name": "サーバー", "mcp_tool_name": "ツール", "Value #A": "呼び出し", "Value #B": "結果を読んだ API 呼び出し", "Value #C": "コスト (USD)"}, {"Time": True})],
    [{"matcher": {"id": "byName", "options": "コスト (USD)"}, "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 3}]}]), 12, 8)
b.add(table("サブエージェント・skill (スパン属性)", [tempo_metrics(
    '{name="claude_code.tool" && (span.subagent_type != nil || span.skill_name != nil)} | count_over_time() by (span.tool_name, span.subagent_type, span.skill_name)', instant=True)],
    "スパン `claude_code.tool` の subagent_type / skill_name。", [reduce_rows("sum"), organize({"Field": "組", "Total": "件数"})]), 12, 7)
b.add(table("git commit (vcs_ref_head_*、tool_parameters の git_commit_id)", [loki(
    f'sum by (vcs_ref_head_name, vcs_ref_head_revision, vcs_repository_name) (count_over_time({SEL} | event_name="tool_result" | vcs_ref_head_revision!="" [$__range]))', instant=True)],
    "Bash で git commit が成功したときだけ付く。", [organize({"Value": "件数"}, {"Time": True})]), 12, 7)
b.row("失敗の全文 (tool_result.error)")
b.add(table("ツールの失敗 (新しい順、最大 100 件)", [loki(
    f'{SEL} | event_name="tool_result" | error!="" | keep tool_name, error_type, error, orca_worktree_name, tool_parameters', max_lines=100)],
    "`error_type` は常に届く分類、`error` はこの設定で届く全文。",
    [{"id": "extractFields", "options": {"source": "labels", "format": "auto", "replace": False, "keepTime": True}},
     organize({"Time": "時刻"}, {"labels": True, "Line": True, "tsNs": True, "labelTypes": True, "id": True, "service_name": True, "detected_level": True})],
    [{"matcher": {"id": "byName", "options": "時刻"}, "properties": [{"id": "unit", "value": "dateTimeAsLocal"}, {"id": "custom.width", "value": 150}]}],
    wrap=["error", "tool_parameters"]), 24, 10)
add_board(b, "cc-setting-log-tool-details.json")


# ================= 9. LOG_ASSISTANT_RESPONSES =================
b = Board("cc-setting-log-assistant-responses", "設定: OTEL_LOG_ASSISTANT_RESPONSES", "応答文で届くもの")
b.add(text("この設定について", header(
    "`OTEL_LOG_ASSISTANT_RESPONSES`", "`1` (v2.1.193 以降。未設定なら `OTEL_LOG_USER_PROMPTS` に従う)",
    "イベント `assistant_response` の `response` 属性に、モデルの応答のテキストブロック (thinking とツール呼び出しは除く) を 60 KB まで入れる。"
    "オフのときも `response_length`・`model`・`query_source`・`request_id` は届き、`response` は `<REDACTED>`。",
    "応答文の中身 (長さと件数は残る)。",
    "モデルが何と答えたか、長すぎる応答が無いか。依頼文 (prompt_id が同じ) と並べて、依頼に対して応答が噛み合っているかを見る。",
    "実データで確認。`<REDACTED>` の分はこの設定が入る前のもの。スパン側に応答文を入れる属性 (`response.model_output`) は detailed beta tracing のときだけ (公式、未確認)。")), 24, 8)
presence(b, "応答文が届いているか", [
    ("assistant_response (中身あり)", lc('event_name="assistant_response" | response!="" | response!="<REDACTED>"')),
    ("assistant_response (<REDACTED>)", lc('event_name="assistant_response" | response="<REDACTED>"')),
    ("assistant_response (全体)", lc('event_name="assistant_response"')),
])
b.row("応答文の一覧")
b.add(table("応答文 (新しい順、最大 200 件、先頭 1000 字)", [loki(
    f'{SEL} | event_name="assistant_response" | label_format response=`{{{{ .response | trunc 1000 }}}}` | keep response, response_length, model, query_source, orca_worktree_name, prompt_id, trace_id', max_lines=200)],
    "全文はセルの目のアイコン、またはトレースへのリンクから。",
    [{"id": "extractFields", "options": {"source": "labels", "format": "auto", "replace": False, "keepTime": True}},
     organize({"Time": "時刻", "response": "応答文", "response_length": "長さ", "model": "モデル", "query_source": "発行元", "orca_worktree_name": "ワーカー"},
              {"labels": True, "Line": True, "tsNs": True, "labelTypes": True, "id": True, "service_name": True, "detected_level": True},
              {"Time": 0, "response": 1, "response_length": 2, "model": 3, "query_source": 4, "orca_worktree_name": 5})],
    [{"matcher": {"id": "byName", "options": "時刻"}, "properties": [{"id": "unit", "value": "dateTimeAsLocal"}, {"id": "custom.width", "value": 150}]},
     {"matcher": {"id": "byName", "options": "応答文"}, "properties": [{"id": "custom.minWidth", "value": 500}]},
     {"matcher": {"id": "byName", "options": "trace_id"}, "properties": [{"id": "links", "value": [{"title": "トレースを開く", "url": "", "internal": {
         "datasourceUid": "tempo", "datasourceName": "Tempo", "query": {"queryType": "traceql", "query": "${__value.raw}"}}}]}]}],
    wrap=["応答文"]), 24, 14)
b.row("件数・長さ")
b.add(ts("応答の件数 (モデル別)", [loki(f'sum by (model) (count_over_time({SEL} | event_name="assistant_response" [$__interval]))', "{{model}}")],
         bars=True, stack=True), 8, 8)
b.add(ts("応答文の長さ (最大・平均、文字)", [
    loki(f'max(max_over_time({SEL} | event_name="assistant_response" | unwrap response_length [$__interval]))', "最大", ref="A"),
    loki(f'avg(avg_over_time({SEL} | event_name="assistant_response" | unwrap response_length [$__interval]))', "平均", ref="B")]), 8, 8)
b.add(table("発行元・モデル別の件数と長さ", [
    loki(f'sum by (query_source, model) (count_over_time({SEL} | event_name="assistant_response" [$__range]))', instant=True, ref="A"),
    loki(f'avg by (query_source, model) (avg_over_time({SEL} | event_name="assistant_response" | unwrap response_length [$__range]))', instant=True, ref="B"),
    loki(f'max by (query_source, model) (max_over_time({SEL} | event_name="assistant_response" | unwrap response_length [$__range]))', instant=True, ref="C")],
    "", [MERGE, organize({"query_source": "発行元", "model": "モデル", "Value #A": "件数", "Value #B": "平均長", "Value #C": "最大長"}, {"Time": True})],
    [{"matcher": {"id": "byRegexp", "options": ".*長"}, "properties": [{"id": "decimals", "value": 0}]}], sort="件数"), 8, 8)
add_board(b, "cc-setting-log-assistant-responses.json")


# ================= 10. LOG_TOOL_CONTENT =================
b = Board("cc-setting-log-tool-content", "設定: OTEL_LOG_TOOL_CONTENT", "ツール出力の中身 (tool.output) で届くもの")
TO_SEL = ('select(span.tool_name, event.output, event.content, event.diff, event.file_path, event.bash_command, '
          'event.output_truncated, event.output_original_length, event.content_truncated, event.content_original_length, '
          'event.diff_truncated, event.diff_original_length)')
b.add(text("この設定について", header(
    "`OTEL_LOG_TOOL_CONTENT`", "`1` (dotfiles で有効化中。まだ settings.json に入っていなければ、下の件数は検証で `claude -p` に付けた回だけ)",
    "トレースが要る。スパン `claude_code.tool` にスパンイベント `tool.output` を付け、Read の `content`、Bash の `output`、"
    "MCP / WebFetch / WebSearch の `output` (v2.1.283 以降) を 60 KB まで入れる。Edit (`diff`) と Write (`content`) と、`file_path` / `bash_command` は "
    "`OTEL_LOG_TOOL_DETAILS=1` も要る。60 KB で切ったときは `<属性>_truncated` と `<属性>_original_length` が付く。"
    "detailed beta tracing のときは tool スパンの `new_context` もこれで出る。",
    "`tool.output` スパンイベントすべて (ツールの出力の中身)。ツールの呼び出し回数・結果の大きさ (`tool_result_size_bytes`) は残る。",
    "ツールが実際に何を返したか (コマンドの出力、読んだファイルの中身)。失敗していないのに結果がおかしい、巨大な出力で文脈を食っている、を確かめる。",
    "Read / Bash / Edit / Write / MCP で実データを確認 (検証の `claude -p`)。**Loki のイベントには出ない** (スパンイベントだけ)。")), 24, 10)
presence(b, "tool.output が届いているか", [
    ("tool.output (全体)", tc('{event:name="tool.output"}')),
    ("output あり (Bash / MCP / Web)", tc('{event:name="tool.output" && event.output != nil}')),
    ("content あり (Read / Write)", tc('{event:name="tool.output" && event.content != nil}')),
    ("diff あり (Edit)", tc('{event:name="tool.output" && event.diff != nil}')),
    ("切り詰めあり", tc('{event:name="tool.output" && (event.output_truncated = true || event.content_truncated = true || event.diff_truncated = true)}')),
])
b.row("ツール別の件数と属性")
b.add(table("ツール別の tool.output 件数", [tempo_metrics('{event:name="tool.output"} | count_over_time() by (span.tool_name)', instant=True)],
            "", [reduce_rows("sum"), {"id": "extractFields", "options": {"source": "Field", "format": "regexp", "regExp": "/tool_name=\"?([^\"}]+)\"?/", "replace": False}},
                 organize({"Field": "ツール", "Total": "件数"})], sort="件数"), 8, 8)
b.add(table("tool.output の属性キー", [inf(TEMPO_URL + "/api/v2/search/tags",
            [{"key": "q", "value": '{event:name="tool.output"}'}, {"key": "scope", "value": "event"}, FROM, TO], '$map(scopes.tags, function($x){ {"key": $x} })')],
            "公式の属性: content / output / diff / file_path / bash_command と、切ったときの *_truncated / *_original_length。"), 6, 8)
b.add(ts("tool.output の件数の推移 (ツール別)", [tempo_metrics('{event:name="tool.output"} | count_over_time() by (span.tool_name)')],
         bars=True, stack=True), 10, 8)
b.row("tool.output の中身の一覧")
b.add(table("tool.output の一覧 (長さ・切り詰めつき、先頭 300 字)", [inf_tempo_search(
    '{event:name="tool.output"} | ' + TO_SEL,
    'traces.( $t := traceID; spanSets.spans.( $a := $merge(attributes.{key: value.*}); $body := $a.output ? $a.output : ($a.content ? $a.content : $a.diff); '
    '{"time": $number(startTimeUnixNano) / 1000000, "tool": $a.tool_name, "attr": $a.output ? "output" : ($a.content ? "content" : ($a.diff ? "diff" : "")), '
    '"length": $length($string($body)), '
    '"original_length": $a.output_original_length ? $a.output_original_length : ($a.content_original_length ? $a.content_original_length : $a.diff_original_length), '
    '"truncated": ($a.output_truncated or $a.content_truncated or $a.diff_truncated) ? "切り詰め" : "", '
    '"file_path": $a.file_path, "bash_command": $a.bash_command, "preview": $substring($string($body), 0, 300), "trace_id": $t} ) )', 100, 50)],
    "Tempo の検索 API で tool.output を持つスパンを引き、スパンイベントの属性を 1 行にした。長さ = 届いた文字数、元の長さ = 切り詰め前 (切ったときだけ)。",
    [organize({"time": "時刻", "tool": "ツール", "attr": "属性", "length": "長さ", "original_length": "元の長さ", "truncated": "切り詰め",
               "file_path": "file_path", "bash_command": "bash_command", "preview": "中身 (先頭 300 字)"}, {},
              {"time": 0, "tool": 1, "attr": 2, "length": 3, "original_length": 4, "truncated": 5, "file_path": 6, "bash_command": 7, "preview": 8, "trace_id": 9}),
     sort_by("時刻")],
    [{"matcher": {"id": "byName", "options": "時刻"}, "properties": [{"id": "unit", "value": "dateTimeAsLocal"}, {"id": "custom.width", "value": 150}]},
     {"matcher": {"id": "byName", "options": "中身 (先頭 300 字)"}, "properties": [{"id": "custom.minWidth", "value": 400}]},
     {"matcher": {"id": "byName", "options": "trace_id"}, "properties": [{"id": "links", "value": [{"title": "トレースを開く", "url": "", "internal": {
         "datasourceUid": "tempo", "datasourceName": "Tempo", "query": {"queryType": "traceql", "query": "${__value.raw}"}}}]}]}],
    wrap=["中身 (先頭 300 字)", "bash_command"]), 24, 14)
b.add(table("ツール別の長さ (届いた文字数)", [inf_tempo_search(
    '{event:name="tool.output"} | ' + TO_SEL,
    '( $rows := traces.spanSets.spans.( $a := $merge(attributes.{key: value.*}); {"tool": $a.tool_name, '
    '"len": $length($string($a.output ? $a.output : ($a.content ? $a.content : $a.diff))), '
    '"tr": ($a.output_truncated or $a.content_truncated or $a.diff_truncated) ? 1 : 0} ); '
    '$distinct($rows.tool).( $k := $; $g := $rows[tool = $k]; {"tool": $k, "n": $count($g), "avg": $round($average($g.len)), "max": $max($g.len), "truncated": $sum($g.tr)} ) )', 100, 50)],
    "直近 100 トレースぶん。", [organize({"tool": "ツール", "n": "件数", "avg": "平均長", "max": "最大長", "truncated": "切り詰め"}, {},
                                    {"tool": 0, "n": 1, "avg": 2, "max": 3, "truncated": 4})], sort="件数"), 24, 7)
add_board(b, "cc-setting-log-tool-content.json")


# ================= 11. INCLUDE_SESSION_ID =================
b = Board("cc-setting-include-session-id", "設定: OTEL_METRICS_INCLUDE_SESSION_ID", "session.id (セッション) の属性で届くもの")
b.add(text("この設定について", header(
    "`OTEL_METRICS_INCLUDE_SESSION_ID`", "`true` (既定 true。dotfiles で明示。2026-10-01 に `false` から切り替えた)",
    "メトリクスとイベントに `session.id` (クラウドでは `ccr.session.id`) を付ける。Prometheus では `session_id` ラベル、"
    "Loki では structured metadata の `session_id` になる。`/clear` すると新しい `session.id` になる。",
    "メトリクス・イベントの `session_id`。grafana.com の Claude Code Metrics (25255) の Sessions・Top Sessions by Cost・"
    "Sessions by Terminal がセッションを数えられなくなる (ラベル無しの全系列が 1 セッションに見える)。",
    "セッションごとのコスト・トークン・依頼数。1 つのワーカーの中で `/clear` や resume をまたいだ内訳。",
    "メトリクスとイベントで実データを確認。スパンにはこの設定と関係なく `span.session.id` が付く (`false` の間も付いていた)。"
    "切り替えより前の系列には `session_id` が無く、保持期間の 14 日 (2026-10-15 ごろまで) は「session_id 無し」の件数に残る。")), 24, 8)
presence(b, "session_id が付いた件数", [
    ("session_id 付きメトリクス系列", pc('{__name__=~"claude_code_.*", session_id!=""}')),
    ("session_id 無しメトリクス系列 (切り替え前の分)", pc('{__name__=~"claude_code_.*", session_id=""}')),
    ("session_id 付きイベント", lc('session_id!=""')),
    ("session_id 無しイベント (切り替え前の分)", lc('session_id=""')),
    ("session.id 付きスパン (設定と関係なく付く)", tc('{span.session.id != nil}')),
])
b.row("セッションで分けた主要指標")
b.add(table("セッション別のコスト・トークン・作業時間 (期間中)", [
    prom('sum by (session_id, orca_worktree_name) (increase(claude_code_cost_usage_USD_total[$__range]))', instant=True, ref="A"),
    prom('sum by (session_id, orca_worktree_name) (increase(claude_code_token_usage_tokens_total[$__range]))', instant=True, ref="B"),
    prom('sum by (session_id, orca_worktree_name) (increase(claude_code_active_time_seconds_total[$__range]))', instant=True, ref="C"),
    loki(f'sum by (session_id, orca_worktree_name) (count_over_time({SEL} | event_name="user_prompt" [$__range]))', instant=True, ref="D")],
    "session_id が空の行 = 切り替え前の系列 (14 日で消える)。1 つのワーカーに複数のセッションがあれば、`/clear` か resume をまたいでいる。",
    [MERGE, organize({"session_id": "セッション", "orca_worktree_name": "ワーカー", "Value #A": "コスト (USD)", "Value #B": "トークン",
                      "Value #C": "作業時間 (秒)", "Value #D": "依頼"}, {"Time": True})],
    [{"matcher": {"id": "byName", "options": "コスト (USD)"}, "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 3}]},
     {"matcher": {"id": "byName", "options": "作業時間 (秒)"}, "properties": [{"id": "unit", "value": "s"}]}],
    sort="コスト (USD)"), 24, 9)
b.add(ts("動いているセッション数 (メトリクス)", [prom('count(count by (session_id) (claude_code_cost_usage_USD_total{session_id!=""}))', "セッション")],
         "その時点で系列があるセッションの数。"), 12, 8)
b.add(table("スパンの session.id の値", [tempo_metrics(
    '{name="claude_code.interaction"} | count_over_time() by (span.session.id)', instant=True)],
    "セッションごとの依頼数。スパンには切り替え前から付いているので、切り替え前のセッションもここで追える。",
    [reduce_rows("sum"), organize({"Field": "session.id", "Total": "依頼数"})], sort="依頼数"), 12, 8)
add_board(b, "cc-setting-include-session-id.json")


# ================= 12. オフ・既定値 =================
b = Board("cc-setting-off", "設定: 既定値のまま・オフの項目", "オフなので届いていないもの、既定でオンのもの")
md = ("dotfiles で明示していない項目は Claude Code の既定値のまま。"
      "**オフの項目は届いていない** ことを下の件数 (0) で示し、オンにすると何が増えるかを書いた。"
      "`OTEL_METRICS_INCLUDE_SESSION_ID` は `true` にしたので [cc-setting-include-session-id](/d/cc-setting-include-session-id) へ移した。\n\n"
      "| 環境変数 | いまの値 | オンにすると増えるもの (公式) | 実データ |\n|---|---|---|---|\n"
      "| `OTEL_METRICS_INCLUDE_VERSION` | 既定 false | メトリクスに `app.version` | 0 系列。版は resource 属性 `service.version` でイベント・スパン・`target_info` に届いている |\n"
      "| `OTEL_METRICS_INCLUDE_ENTRYPOINT` | 既定 false | メトリクスに `app.entrypoint` (`cli` / `sdk-cli` など。`claude -p` と対話の区別) | 0 系列 |\n"
      "| `OTEL_METRICS_INCLUDE_ACCOUNT_UUID` | 既定 **true** | `user.account_uuid` / `user.account_id` | 届いている (止めると消える) |\n"
      "| `OTEL_METRICS_INCLUDE_RESOURCE_ATTRIBUTES` | 既定 **true** | `OTEL_RESOURCE_ATTRIBUTES` のキーをメトリクスのラベルに | 届いている (`orca_worktree_*`)。止めると resource (`target_info`) にだけ残る |\n"
      "| `OTEL_LOG_RAW_API_BODIES` | 未設定 | イベント `api_request_body` / `api_response_body` (API の要求・応答の JSON 全体、60 KB まで。`file:<dir>` ならファイルに書いて `body_ref`) | 0 件 |\n"
      "| `OTEL_LOG_MANAGED_SETTINGS` | 未設定 | `managed_settings_resolved` に `managed_settings.settings` と `managed_settings.resolved_sha256` | イベントは届くが、この 2 属性は 0 件 |\n"
      "| `ENABLE_BETA_TRACING_DETAILED` + `BETA_TRACING_ENDPOINT` | 未設定 | スパン `claude_code.hook`、`llm_request` の `query_source`、`new_context` / `system_prompt_preview` / `tool_input` / `response.model_output` などのスパン属性。送り先が変わる | ほぼ 0。ただし `claude_code.hook` スパンと `llm_request.query_source` が数件だけ届いている (どのセッションが出したかは未確認。公式どおりならこの設定が要る) |\n"
      "| `CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH` | 既定 61440 (60 KB) | 長い属性を切る長さ。上げると tool.output / response の切り詰めが減る | 切り詰めの件数は tool-content のダッシュボード |\n")
b.add(text("オフ・既定値の項目", md), 24, 16)
presence(b, "オフの項目で増えるはずの属性 (0 = 届いていない)", [
    ("metric app_version (0 が正)", pc('{__name__=~"claude_code_.*", app_version!=""}')),
    ("metric app_entrypoint (0 が正)", pc('{__name__=~"claude_code_.*", app_entrypoint!=""}')),
    ("api_request_body (0 が正)", lc('event_name="api_request_body"')),
    ("api_response_body (0 が正)", lc('event_name="api_response_body"')),
    ("managed_settings_settings (0 が正)", lc('event_name="managed_settings_resolved" | managed_settings_settings!=""')),
    ("hook スパン (公式では DETAILED が要る。実データに少数あり、出どころ未確認) (0 が正)", tc('{name="claude_code.hook"}')),
    ("llm_request.query_source (hook と同じく少数あり) (0 が正)", tc('{name="claude_code.llm_request" && span.query_source != nil}')),
])
presence(b, "既定でオン / 別経路で届いているもの", [
    ("metric user_account_uuid (ACCOUNT_UUID)", pc('{__name__=~"claude_code_.*", user_account_uuid!=""}')),
    ("metric orca_worktree_name (INCLUDE_RESOURCE_ATTRIBUTES)", pc('{__name__=~"claude_code_.*", orca_worktree_name!=""}')),
    ("event service_version (版)", lc('service_version!=""')),
    ("managed_settings_resolved (イベント自体)", lc('event_name="managed_settings_resolved"')),
])
b.add(table("managed_settings_resolved の属性 (オフでも届く分)", [event_attr_long(' | event_name="managed_settings_resolved"')],
            "", [organize({"event": "イベント", "key": "属性キー", "state": "状態", "count": "件数"})]), 24, 8)
add_board(b, "cc-setting-off.json")


for fname, b in boards:
    with open(os.path.join(OUT, fname), "w") as f:
        json.dump(b.json(), f, ensure_ascii=False, indent=1)
        f.write("\n")
print("wrote", len(boards))
