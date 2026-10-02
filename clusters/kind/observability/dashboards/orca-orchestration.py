"""Orca orchestration ダッシュボード (orca-orchestration.json) を作る。JSON を手で直さずにこれを直して作り直す:
    python3 clusters/kind/observability/dashboards/orca-orchestration.py
Grafana への反映は main に入れたあと ArgoCD が行う。データの出どころと各メトリクスは docs/observability/orca-orchestration.md。"""
import json, os, re

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
TEMPO = {"type": "tempo", "uid": "tempo"}
MIXED = {"type": "datasource", "uid": "-- Mixed --"}
SEL = '{service_namespace="home-k8s", service_name="orca"}'
MSG = SEL + ' | event_name="orca.message" | orca_worktree_name=~"$worker"'
FROM = "${__from:date:seconds}"
# 期間内に出したワーカー (Dispatch) だけに絞る
IN_RANGE = f'and on (orca_dispatch_id) (last_over_time(orca_dispatch_start_time_seconds[$__range]) >= {FROM})'
W = 'orca_worktree_name=~"$worker"'

panels = []
nid = [0]
y = [0]


def pid():
    nid[0] += 1
    return nid[0]


def row(title):
    panels.append({"id": pid(), "type": "row", "title": title, "collapsed": False, "panels": [],
                   "gridPos": {"x": 0, "y": y[0], "w": 24, "h": 1}})
    y[0] += 1


def prom(ref, expr, instant=True, fmt="table", legend="__auto"):
    t = {"refId": ref, "datasource": PROM, "expr": expr, "legendFormat": legend}
    if instant:
        t.update({"instant": True, "range": False, "format": fmt})
    else:
        t.update({"instant": False, "range": True})
    return t


def loki(ref, expr, instant=False, legend=None, qtype=None):
    t = {"refId": ref, "datasource": LOKI, "expr": expr, "queryType": qtype or ("instant" if instant else "range")}
    if legend:
        t["legendFormat"] = legend
    return t


def panel(type_, title, desc, x, w, h, targets, ds, **kw):
    p = {"id": pid(), "type": type_, "title": title, "description": desc, "datasource": ds,
         "gridPos": {"x": x, "y": y[0], "w": w, "h": h}, "targets": targets}
    p.update(kw)
    panels.append(p)
    return p


def stat(title, desc, x, w, target, unit="short", decimals=None, ds=PROM, thresholds=None, dy=0):
    """dy: 今の段の上端からずらす高さ (2 段目なら 4)"""
    d = {"unit": unit, "color": {"mode": "thresholds"},
         "thresholds": {"mode": "absolute", "steps": thresholds or [{"color": "text", "value": None}]}}
    if decimals is not None:
        d["decimals"] = decimals
    y[0] += dy
    p = panel("stat", title, desc, x, w, 4, [target], ds,
                 fieldConfig={"defaults": d, "overrides": []},
                 options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                          "colorMode": "value", "graphMode": "none", "textMode": "value", "justifyMode": "auto",
                          "orientation": "auto", "showPercentChange": False, "wideLayout": True})
    y[0] -= dy
    return p


def by_name(name, props):
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


# ---- 0. 使い方 -----------------------------------------------------------------
panels.append({"id": pid(), "type": "text", "title": "このダッシュボードの使い方",
               "gridPos": {"x": 0, "y": 0, "w": 24, "h": 7},
               "options": {"mode": "markdown", "content": """coordinator が Orca でワーカーをどう回したか (Run → Task → ワーカー (Dispatch) → メッセージ) と、**手戻り** (追加指示、worker_done までの時間、拒否・user_takeover) を見る。
データはホストの `orca-exporter` (systemd のユーザーユニット) が 30 秒ごとに `orca orchestration ... --json` を読んで送る。止まっていたら `just orca-exporter status`。詳しくはリポジトリの `docs/observability/orca-orchestration.md`、困りごと別の見方は `playbook.md` の「手戻りを減らす」。

- **追加指示** = coordinator からワーカーへの、返事ではないメッセージ (`orca orchestration send` の訂正・追加依頼)。`ask` への返事は数えない。**0 が理想**。1 以上は最初の依頼 (spec) に足りないものがあった印
- 数字は Orca にいま残っている Run が元。右上の期間は「その期間に**出した**ワーカー」で絞る (メッセージの推移は送った時刻で絞る)
- 上の「ワーカー」で絞ると全パネルが絞られる。ワーカー名は Claude Code 側の `orca_worktree_name` と同じなので、[improve](/d/claude-code-improve) や [usage](/d/claude-code-usage) にもそのまま持っていける"""}})
y[0] = 7

# ---- 1. 要約 -------------------------------------------------------------------
row("1. 要約 (期間中に出したワーカー)")
stat("ワーカー数", "期間中に出したワーカー (Dispatch) の数。Run の数ではない", 0, 6,
     prom("A", f'count(max by (orca_dispatch_id) (last_over_time(orca_dispatch_info{{{W}}}[$__range])) {IN_RANGE}) or vector(0)'), decimals=0)
stat("追加指示 (合計)", "coordinator からワーカーへの追加指示の合計。増えたら spec の書き方 (完了条件・範囲・前提) を疑う", 6, 6,
     prom("A", f'sum(last_over_time(orca_dispatch_followups{{{W}}}[$__range]) {IN_RANGE}) or vector(0)'), decimals=0,
     thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}])
stat("1 ワーカーあたりの追加指示", "追加指示 ÷ ワーカー数。0.5 を超えたら、spec の雛形 (完了条件・報告先・触ってよい範囲) を見直す", 12, 6,
     prom("A", f'avg(last_over_time(orca_dispatch_followups{{{W}}}[$__range]) {IN_RANGE})'), decimals=2,
     thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.5}, {"color": "red", "value": 1}])
stat("worker_done までの時間 (中央値)", "ワーカーを出してから最初の worker_done (受理) までの中央値。伸びたら依頼が大きすぎないか (分けられないか)、モデルが遅くないかを疑う", 18, 6,
     prom("A", f'quantile(0.5, last_over_time(orca_dispatch_time_to_done_seconds{{{W}}}[$__range]) {IN_RANGE})'), unit="s")
stat("拒否された worker_done", "Orca が受け取らなかった worker_done (capability 失効など)。出たら、coordinator が先に release / 作り直しをしていないか (Orca の運用) を疑う", 0, 8,
     prom("A", f'sum(last_over_time(orca_dispatch_worker_done_rejected{{{W}}}[$__range]) {IN_RANGE}) or vector(0)'), decimals=0,
     thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}], dy=4)
stat("user_takeover", "端末を人が引き取った (release されずに残った) ワーカー。多いのは、完了後に人が確かめる運用か、ワーカーが止まって人が入った印", 8, 8,
     prom("A", f'count(last_over_time(orca_dispatch_info{{{W}, orca_release_retained_reason="user_takeover"}}[15m]) {IN_RANGE}) or vector(0)'), decimals=0, dy=4)
stat("exporter の最終送信", "orca-exporter が最後に Orca を読んで送れた時刻からの経過。数分以上なら `just orca-exporter status` を見る", 16, 8,
     prom("A", 'time() - max(orca_exporter_last_success_seconds)'), unit="s",
     thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 120}, {"color": "red", "value": 600}], dy=4)
y[0] += 8

# ---- 2. Run ---------------------------------------------------------------------
row("2. Run (目的ごとの流れ)")
RUN_IN = f'and on (orca_run_id) (last_over_time(orca_run_start_time_seconds[$__range]) >= {FROM})'
panel("table", "Run 一覧", "目的・開始・所要時間・Task 数。「Run」列を押すと下のタイムラインにその Run が出る。完了が少なく Task が多い Run は、分け方 (1 Task の大きさ) を疑う",
      0, 24, 8, [
          prom("A", f'max by (orca_run_id, orca_run_objective, trace_id) (last_over_time(orca_run_info[15m]) {RUN_IN})'),
          prom("B", f'max by (orca_run_id) (last_over_time(orca_run_start_time_seconds[$__range]) {RUN_IN}) * 1000'),
          prom("C", f'max by (orca_run_id) (last_over_time(orca_run_duration_seconds[$__range]) {RUN_IN})'),
          prom("D", f'sum by (orca_run_id) (last_over_time(orca_run_tasks[$__range]) {RUN_IN})'),
          prom("E", f'sum by (orca_run_id) (last_over_time(orca_run_tasks{{orca_task_status="completed"}}[$__range]) {RUN_IN})'),
          prom("F", f'sum by (orca_run_id) (last_over_time(orca_run_tasks{{orca_task_status="failed"}}[$__range]) {RUN_IN})'),
          prom("G", f'count by (orca_run_id) (max by (orca_run_id, orca_dispatch_id) (last_over_time(orca_dispatch_info[$__range])))'),
          prom("H", f'sum by (orca_run_id) (last_over_time(orca_dispatch_followups[$__range]))'),
      ], PROM,
      transformations=[
          {"id": "joinByField", "options": {"byField": "orca_run_id", "mode": "outerTabular"}},
          {"id": "filterByValue", "options": {"filters": [{"fieldName": "orca_run_objective", "config": {"id": "isNull", "options": {}}}], "type": "exclude", "match": "any"}},
          {"id": "organize", "options": {
              "excludeByName": {"Time": True, "Time 1": True, "Time 2": True, "Time 3": True, "Time 4": True, "Time 5": True, "Time 6": True, "Time 7": True, "Time 8": True,
                                "__name__": True, "job": True, "Value #A": True},
              "indexByName": {"orca_run_objective": 0, "Value #B": 1, "Value #C": 2, "Value #D": 3, "Value #E": 4, "Value #F": 5, "Value #G": 6, "Value #H": 7, "orca_run_id": 8},
              "renameByName": {"orca_run_objective": "目的", "Value #B": "開始", "Value #C": "所要時間", "Value #D": "Task", "Value #E": "完了",
                               "Value #F": "失敗", "Value #G": "ワーカー", "Value #H": "追加指示", "orca_run_id": "Run", "trace_id": "trace_id"}}},
      ],
      fieldConfig={"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}, "unit": "short"},
                   "overrides": [
                       by_name("開始", [{"id": "unit", "value": "dateTimeAsLocalNoDateIfToday"}]),
                       by_name("所要時間", [{"id": "unit", "value": "s"}]),
                       by_name("目的", [{"id": "custom.width", "value": 520}]),
                       by_name("Run", [{"id": "links", "value": [{"title": "この Run のタイムラインを下に出す",
                                                                "url": "/d/orca-orchestration/orca-orchestration?${__url_time_range}&${worker:queryparam}&var-run=${__data.fields.trace_id}"}]}]),
                       by_name("trace_id", [{"id": "custom.hidden", "value": True}]),
                   ]},
      options={"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "開始", "desc": True}]})
y[0] += 8
# queryType は traceql にしてトレース ID をそのまま渡す (traceId だと Grafana 13.2 の traces パネルはクエリを投げない)
panel("traces", "Run のタイムライン", "上の「Run」で選んだ Run。親が Run、子が Task、孫がワーカー (Dispatch) で、メッセージはスパンのイベント (◆)。ワーカーの帯が長く ◆ (追加指示・question) が多いところが手戻り。Task / Run は終わってから送るので、動いている Run はまだ出ない (docs 参照)",
      0, 24, 14, [{"refId": "A", "datasource": TEMPO, "queryType": "traceql", "query": "$run"}], TEMPO)
y[0] += 14
panel("table", "Run のトレース一覧 (Tempo)", "Tempo に届いた Run。Trace ID を押すと Explore で開く。Orca にある Run より少なければ、その Run はまだ終わっていない (coordinator が束縛中か、ワーカーが動いている)",
      0, 24, 6, [{"refId": "A", "datasource": TEMPO, "queryType": "traceql", "query": '{resource.service.namespace="home-k8s" && resource.service.name="orca" && span.orca.span.kind="run"}', "limit": 50, "tableType": "traces"}], TEMPO)
y[0] += 6

# ---- 3. ワーカー別 ---------------------------------------------------------------
row("3. ワーカー別 (手戻り)")
Q = lambda m, ref: prom(ref, f'max by (orca_dispatch_id) (last_over_time({m}{{{W}}}[$__range]))')
panel("table", "ワーカーごとの手戻り", "1 行 = 1 ワーカー (Dispatch)。追加指示・question が多い行の spec を読み返し、最初に書けなかった条件を spec の雛形に足す。heartbeat の最大間隔が 10 分を大きく超える行は、ワーカーが止まっていた (人の確認待ち・権限の確認) 可能性",
      0, 24, 10, [
          prom("A", f'last_over_time(orca_dispatch_info{{{W}}}[15m]) {IN_RANGE}'),
          Q("orca_dispatch_followups", "B"), Q("orca_dispatch_replies", "C"), Q("orca_dispatch_questions", "D"),
          Q("orca_dispatch_worker_done_rejected", "E"), Q("orca_dispatch_heartbeat_max_gap_seconds", "F"),
          Q("orca_dispatch_time_to_done_seconds", "G"), Q("orca_dispatch_duration_seconds", "H"),
          prom("I", f'max by (orca_dispatch_id) (last_over_time(orca_dispatch_start_time_seconds{{{W}}}[$__range])) * 1000'),
      ], PROM,
      transformations=[
          {"id": "joinByField", "options": {"byField": "orca_dispatch_id", "mode": "inner"}},
          {"id": "organize", "options": {
              "excludeByName": {"Time": True, "Time 1": True, "Time 2": True, "Time 3": True, "Time 4": True, "Time 5": True, "Time 6": True, "Time 7": True, "Time 8": True, "Time 9": True,
                                "__name__": True, "job": True, "Value #A": True, "orca_task_id": True, "orca_resource_ownership": True,
                                "orca_terminal_state": True, "orca_dispatch_retry_of": True, "orca_attention": True, "orca_dispatch_termination_reason": True},
              "indexByName": {"orca_worktree_name": 0, "orca_model": 1, "Value #I": 2, "Value #B": 3, "Value #D": 4, "Value #C": 5, "Value #E": 6, "Value #G": 7, "Value #H": 8, "Value #F": 9,
                              "orca_dispatch_status": 10, "orca_worker_state": 11, "orca_release_state": 12, "orca_release_retained_reason": 13, "orca_run_id": 14, "orca_dispatch_id": 15},
              "renameByName": {"orca_worktree_name": "ワーカー", "orca_model": "モデル", "Value #I": "開始", "Value #B": "追加指示", "Value #C": "返事", "Value #D": "question",
                               "Value #E": "拒否された worker_done", "Value #F": "heartbeat 最大間隔", "Value #G": "worker_done まで", "Value #H": "所要時間",
                               "orca_dispatch_status": "Dispatch", "orca_worker_state": "ワーカー状態", "orca_release_state": "release",
                               "orca_release_retained_reason": "残った理由", "orca_run_id": "Run", "orca_dispatch_id": "Dispatch ID"}}},
      ],
      fieldConfig={"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}, "unit": "short"},
                   "overrides": [
                       by_name("開始", [{"id": "unit", "value": "dateTimeAsLocalNoDateIfToday"}]),
                       by_name("heartbeat 最大間隔", [{"id": "unit", "value": "s"}]),
                       by_name("worker_done まで", [{"id": "unit", "value": "s"}]),
                       by_name("所要時間", [{"id": "unit", "value": "s"}]),
                       by_name("追加指示", [{"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}},
                                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "transparent", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 3}]}}]),
                       by_name("ワーカー", [{"id": "links", "value": [{"title": "このワーカーの Claude Code (improve)",
                                                                   "url": "/d/claude-code-improve/claude-code-improve?${__url_time_range}&var-worker=${__value.raw}"}]},
                                            {"id": "custom.width", "value": 300}]),
                       by_name("モデル", [{"id": "custom.width", "value": 150}]),
                   ]},
      options={"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "開始", "desc": True}]})
y[0] += 10


def bars(title, desc, x, w, expr, unit="short"):
    return panel("bargauge", title, desc, x, w, 8, [prom("A", expr, fmt="time_series", legend="{{orca_worktree_name}}")], PROM,
                 fieldConfig={"defaults": {"unit": unit, "min": 0, "color": {"mode": "continuous-GrYlRd"}}, "overrides": []},
                 options={"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True, "valueMode": "color",
                          "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "namePlacement": "left", "sizing": "auto"})


bars("追加指示の多いワーカー", "ワーカーごとの追加指示 (上位 10)。上位に同じ種類の仕事 (調査・docs・ダッシュボード) が並ぶなら、その種類の spec の雛形を直す",
     0, 8, f'topk(10, sum by (orca_worktree_name) (last_over_time(orca_dispatch_followups{{{W}}}[$__range]) {IN_RANGE}) > 0)')
bars("worker_done までの時間", "ワーカーごとの、出してから worker_done まで (上位 10)。長いものは依頼の大きさ (1 ワーカーに詰め込みすぎ) か、途中の人の確認待ちを疑う。heartbeat の間隔と合わせて見る",
     8, 8, f'topk(10, max by (orca_worktree_name) (last_over_time(orca_dispatch_time_to_done_seconds{{{W}}}[$__range]) {IN_RANGE}))', unit="s")
bars("heartbeat の途切れ (最大間隔)", "heartbeat 同士 (と開始・終了) の最大の間隔 (上位 10)。5 分ごとに送る約束なので、15 分を超えるものはワーカーが止まっていた (権限の確認・長いコマンド) か、heartbeat を忘れた。後者なら spec の雛形に heartbeat の一文があるか確かめる",
     16, 8, f'topk(10, max by (orca_worktree_name) (last_over_time(orca_dispatch_heartbeat_max_gap_seconds{{{W}}}[$__range]) {IN_RANGE}))', unit="s")
y[0] += 8
panel("table", "拒否・失敗・user_takeover", "ワーカーの終わり方の内訳 (Dispatch の状態 × release の結果 × 残った理由)。failed / abandoned が出たら Task の spec と完了条件を、拒否された worker_done が出たら coordinator が先に作り直していないかを確かめる",
      0, 12, 7, [
          prom("A", f'count by (orca_dispatch_status, orca_worker_state, orca_release_state, orca_release_retained_reason) (last_over_time(orca_dispatch_info{{{W}}}[15m]) {IN_RANGE})'),
          prom("B", f'sum(last_over_time(orca_dispatch_worker_done_rejected{{{W}}}[$__range]) {IN_RANGE}) or vector(0)'),
      ], PROM,
      transformations=[{"id": "merge", "options": {}},
                       {"id": "organize", "options": {"excludeByName": {"Time": True},
                                                      "renameByName": {"orca_dispatch_status": "Dispatch", "orca_worker_state": "ワーカー状態",
                                                                       "orca_release_state": "release", "orca_release_retained_reason": "残った理由",
                                                                       "Value #A": "ワーカー数", "Value #B": "拒否された worker_done (合計)"}}}],
      fieldConfig={"defaults": {"unit": "short", "decimals": 0}, "overrides": []},
      options={"showHeader": True, "cellHeight": "sm"})
panel("logs", "拒否されたメッセージ", "Orca が受け取らなかった worker_done / heartbeat。理由 (rejection_code) が capability 失効なら、ワーカーが止まる前に coordinator が stop / release した",
      12, 12, 7, [loki("A", MSG + ' | orca_message_rejected="true" | line_format "{{.orca_worktree_name}} {{.orca_message_type}} {{.orca_message_rejection_code}}: {{.orca_message_subject}}"')], LOKI,
      options={"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True, "dedupStrategy": "none", "prettifyLogMessage": False})
y[0] += 7

# ---- 4. メッセージの流れ -----------------------------------------------------------
row("4. メッセージの流れ")
panel("timeseries", "メッセージ数の推移 (種別)", "送った時刻ごとの件数。heartbeat ばかりで worker_done が来ない時間帯は、ワーカーが長く動いている。status (追加指示) の山は手戻りの起きた時刻",
      0, 12, 8, [loki("A", f'sum by (orca_message_type) (count_over_time({MSG} [$__auto]))', legend="{{orca_message_type}}")], LOKI,
      fieldConfig={"defaults": {"custom": {"drawStyle": "bars", "stacking": {"mode": "normal"}, "fillOpacity": 80, "lineWidth": 1}, "unit": "short"}, "overrides": []},
      options={"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}, "tooltip": {"mode": "multi", "sort": "desc"}})
panel("timeseries", "追加指示と question の推移", "coordinator → ワーカーの追加指示と、ワーカー → coordinator の question。同じ時期に両方が多いなら、spec の曖昧さで往復している。ask への返事は含めない",
      12, 12, 8, [loki("A", f'sum(count_over_time({MSG} | orca_message_followup="true" [$__auto]))', legend="追加指示"),
                  loki("B", f'sum(count_over_time({MSG} | orca_message_type="question" [$__auto]))', legend="question"),
                  loki("C", f'sum(count_over_time({MSG} | orca_message_type="escalation" [$__auto]))', legend="escalation")], LOKI,
      fieldConfig={"defaults": {"custom": {"drawStyle": "bars", "stacking": {"mode": "normal"}, "fillOpacity": 80, "lineWidth": 1}, "unit": "short"}, "overrides": []},
      options={"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}, "tooltip": {"mode": "multi", "sort": "desc"}})
y[0] += 8
panel("timeseries", "送信 → 配達の遅れ (最大)", "メッセージを送ってから宛先に配達されるまで (種別ごとの最大)。数分を超えるなら、宛先の端末が止まっていた (coordinator が別の作業中、ワーカーが権限の確認待ち)",
      0, 12, 8, [loki("A", f'max by (orca_message_type) (max_over_time({MSG} | orca_message_delivery_delay_seconds!="" | unwrap orca_message_delivery_delay_seconds [$__auto]))', legend="{{orca_message_type}}")], LOKI,
      fieldConfig={"defaults": {"custom": {"drawStyle": "points", "pointSize": 6}, "unit": "s"}, "overrides": []},
      options={"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}, "tooltip": {"mode": "multi", "sort": "desc"}})
panel("logs", "coordinator ↔ ワーカーのやりとり (heartbeat 以外)", "追加指示・question・返事・worker_done の件名と本文。追加指示の本文から、最初の spec に書けたはずの条件を拾う",
      12, 12, 8, [loki("A", MSG + ' | orca_message_type!="heartbeat" | line_format `[{{.orca_message_type}}{{if eq .orca_message_followup "true"}} 追加指示{{end}}] {{.orca_worktree_name}}: {{.orca_message_subject}} | {{__line__}}`')], LOKI,
      options={"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True, "dedupStrategy": "none", "prettifyLogMessage": False})
y[0] += 8

# ---- 5. Claude Code との突き合わせ ------------------------------------------------------
row("5. Claude Code との突き合わせ (ワーカー名で結合)")
CW = 'orca_worktree_name=~"$worker", orca_worktree_name!=""'
panel("table", "ワーカーごとの手戻りとコスト", "Orca 側 (追加指示・question・所要時間) と Claude Code 側 (コスト・トークン・API 呼び出し・ツール失敗) をワーカー名で並べる。手戻りが多いワーカーがコストも高ければ、spec を直す効果が大きい。手戻りが少ないのにコストが高ければ、モデル選択か調べる範囲を疑う。片方だけの行は、期間外か Orca の外 (Claude だけ) のもの",
      0, 24, 12, [
          prom("A", f'sum by (orca_worktree_name) (last_over_time(orca_dispatch_followups{{{CW}}}[$__range]) {IN_RANGE})'),
          prom("B", f'sum by (orca_worktree_name) (last_over_time(orca_dispatch_questions{{{CW}}}[$__range]) {IN_RANGE})'),
          prom("C", f'sum by (orca_worktree_name) (last_over_time(orca_dispatch_duration_seconds{{{CW}}}[$__range]) {IN_RANGE})'),
          prom("D", f'count by (orca_worktree_name) (max by (orca_worktree_name, orca_dispatch_id) (last_over_time(orca_dispatch_info{{{CW}}}[$__range])) {IN_RANGE})'),
          prom("E", f'sum by (orca_worktree_name) (increase(claude_code_cost_usage_USD_total{{{CW}}}[$__range] anchored))'),
          prom("F", f'sum by (orca_worktree_name) (increase(claude_code_token_usage_tokens_total{{{CW}}}[$__range] anchored))'),
          {"refId": "G", "datasource": LOKI, "queryType": "instant", "expr": f'sum by (orca_worktree_name) (count_over_time({{service_name="claude-code"}} | {CW} | event_name="api_request" [$__range]))'},
          {"refId": "H", "datasource": LOKI, "queryType": "instant", "expr": f'sum by (orca_worktree_name) (count_over_time({{service_name="claude-code"}} | {CW} | event_name="tool_result" | success="false" [$__range]))'},
          prom("I", f'topk by (orca_worktree_name) (1, sum by (orca_worktree_name, model) (increase(claude_code_cost_usage_USD_total{{{CW}}}[$__range] anchored)))'),
      ], MIXED,
      transformations=[
          {"id": "joinByField", "options": {"byField": "orca_worktree_name", "mode": "outer"}},
          {"id": "organize", "options": {
              "excludeByName": {"Time": True, "Time 1": True, "Time 2": True, "Time 3": True, "Time 4": True, "Time 5": True, "Time 6": True, "Time 7": True, "Time 8": True, "Time 9": True, "Value #I": True},
              "indexByName": {"orca_worktree_name": 0, "Value #A": 1, "Value #E": 2, "Value #B": 3, "Value #C": 4, "Value #H": 5, "Value #G": 6, "Value #F": 7, "model": 8, "Value #D": 9},
              "renameByName": {"orca_worktree_name": "ワーカー", "model": "主なモデル (コスト最大)", "Value #D": "Dispatch", "Value #A": "追加指示", "Value #B": "question",
                               "Value #C": "所要時間 (Orca)", "Value #E": "コスト (USD)", "Value #F": "トークン", "Value #G": "API 呼び出し", "Value #H": "ツール失敗"}}},
      ],
      fieldConfig={"defaults": {"unit": "short", "custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                   "overrides": [
                       by_name("コスト (USD)", [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]),
                       by_name("所要時間 (Orca)", [{"id": "unit", "value": "s"}]),
                       by_name("トークン", [{"id": "decimals", "value": 0}]),
                       by_name("追加指示", [{"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}},
                                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "transparent", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 3}]}}]),
                       by_name("ワーカー", [{"id": "links", "value": [{"title": "このワーカーの Claude Code (improve)",
                                                                   "url": "/d/claude-code-improve/claude-code-improve?${__url_time_range}&var-worker=${__value.raw}"}]},
                                            {"id": "custom.width", "value": 300}]),
                   ]},
      options={"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "追加指示", "desc": True}]})
y[0] += 12
# ワーカーの主なモデル (Claude Code 側でコストが最大のモデル) で、Orca の数字を束ねる
MAIN = f'(topk by (orca_worktree_name) (1, sum by (orca_worktree_name, model) (increase(claude_code_cost_usage_USD_total{{{CW}}}[$__range] anchored))) * 0 + 1)'
OW = lambda m: f'sum by (orca_worktree_name) (max by (orca_worktree_name, orca_dispatch_id) (last_over_time({m}{{{CW}}}[$__range])) {IN_RANGE})'
panel("table", "モデル別の手戻り", "ワーカーの主なモデル (Claude Code 側でコストが最大のモデル) ごとに、ワーカー数・追加指示・1 ワーカーあたりの追加指示とコスト。安いモデルに回した仕事の 1 ワーカーあたりの追加指示が高いなら、その種類の仕事はモデルを上げるか spec を細かくする",
      0, 24, 6, [
          prom("A", f'count by (model) ({OW("orca_dispatch_info")} * on (orca_worktree_name) group_left (model) {MAIN})'),
          prom("B", f'sum by (model) ({OW("orca_dispatch_followups")} * on (orca_worktree_name) group_left (model) {MAIN})'),
          prom("C", f'avg by (model) ({OW("orca_dispatch_followups")} * on (orca_worktree_name) group_left (model) {MAIN})'),
          prom("D", f'avg by (model) (sum by (orca_worktree_name) (increase(claude_code_cost_usage_USD_total{{{CW}}}[$__range] anchored)) * on (orca_worktree_name) group_left (model) {MAIN} and on (orca_worktree_name) {OW("orca_dispatch_info")})'),
          prom("E", f'avg by (model) (max by (orca_worktree_name) (last_over_time(orca_dispatch_time_to_done_seconds{{{CW}}}[$__range]) {IN_RANGE}) * on (orca_worktree_name) group_left (model) {MAIN})'),
      ], PROM,
      transformations=[{"id": "merge", "options": {}},
                       {"id": "organize", "options": {"excludeByName": {"Time": True},
                                                      "renameByName": {"model": "主なモデル", "Value #A": "ワーカー", "Value #B": "追加指示",
                                                                       "Value #C": "1 ワーカーあたりの追加指示", "Value #D": "1 ワーカーあたりのコスト (USD)",
                                                                       "Value #E": "worker_done まで (平均)"}}}],
      fieldConfig={"defaults": {"unit": "short"},
                   "overrides": [by_name("1 ワーカーあたりのコスト (USD)", [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]),
                                 by_name("1 ワーカーあたりの追加指示", [{"id": "decimals", "value": 2}]),
                                 by_name("worker_done まで (平均)", [{"id": "unit", "value": "s"}])]},
      options={"showHeader": True, "cellHeight": "sm"})
y[0] += 8


# ---- 6. 話題別・役割別 ------------------------------------------------------------------
# 役割は worktree 名で決める (coordinator = main-chat、coordinator-chat-<topic> = 話題チャット、それ以外 = ワーカー)。
# 話題は Run の coordinator の worktree 名 (orca_run_info の orca_run_coordinator_worktree) から取る。docs/observability/orca-orchestration.md
row("6. 話題別・役割別 (トークン・コスト)")
panels.append({"id": pid(), "type": "text", "title": "読み方 (前提)",
               "gridPos": {"x": 0, "y": y[0], "w": 24, "h": 4},
               "options": {"mode": "markdown", "content": """役割は Orca の worktree 名で決める: `coordinator` = **main-chat**、`coordinator-chat-<topic>` = **topic-chat** (話題チャット)、それ以外 = **worker**。この名前の規約が崩れると判定が外れる。
話題ごとの合計 = 話題チャット本体 + その話題の Run のワーカー。ワーカーは「その時点で最新の Dispatch の Run」に寄せ、Run の話題は `orca_run_info` の `orca_run_coordinator_worktree` で決める。ラベルが付く前に閉じた Run と、main-chat が直接回した Run は話題に割れず、`(不明)` / `(main-chat 直)` に入る。
旧方式 (1 つの coordinator が複数の話題を回す) は話題に割れない。**期間単位の「調整コスト比」** で前後を比べる (旧は `coordinator` と、worktree 名が空で `vcs_repository_name="coordinator"` のもの)。期間は右上 (ワーカーの絞り込みは効かない)"""}})
y[0] += 4

COORD = "coordinator|coordinator-chat-.+"
COST, TOK = "claude_code_cost_usage_USD_total", "claude_code_token_usage_tokens_total"
TYPES = [("input", "input"), ("output", "output"), ("cacheRead", "cache 読み"), ("cacheCreation", "cache 作成")]


def by_run(metric, sel=""):
    """ワーカーの消費を Run ごとに。ワーカーは「その時点で最新の Dispatch」の Run に寄せる (同じ worktree が複数の Run に出ることがあるため)。
    rate を 1 分刻みで足した近似 (increase ではない)"""
    return (f'sum by (orca_run_id) (sum_over_time((sum by (orca_worktree_name) (rate({metric}{{{sel}}}[5m])) '
            f'* on (orca_worktree_name) group_left (orca_run_id) (0 * topk by (orca_worktree_name) (1, orca_dispatch_start_time_seconds) + 1))[$__range:1m]) * 60)')


# Run → 話題。Run ごとに最新の orca_run_coordinator_worktree を 1 つ選ぶ (run-use で渡した Run はラベルが変わる)。
# label_replace は後のものが勝つ: (不明) → main-chat 直 → 話題名
RUN_TOPIC = ('label_replace(label_replace(label_replace('
             '(0 * topk by (orca_run_id) (1, max_over_time(timestamp(orca_run_info)[$__range:1m])) + 1), '
             '"orca_topic", "(不明)", "orca_run_coordinator_worktree", ".*"), '
             '"orca_topic", "(main-chat 直)", "orca_run_coordinator_worktree", "coordinator"), '
             '"orca_topic", "$1", "orca_run_coordinator_worktree", "coordinator-chat-(.+)")')


def by_topic(metric, sel=""):
    """話題ごと = ワーカー分 (W) + 話題チャット本体 (C)。片方しか無い話題も残す"""
    w = f'sum by (orca_topic) ({by_run(metric, sel)} * on (orca_run_id) group_left (orca_topic) {RUN_TOPIC})'
    c_sel = ", ".join(['orca_worktree_name=~"coordinator-chat-.+"'] + ([sel] if sel else []))
    c = (f'sum by (orca_topic) (label_replace(increase({metric}{{{c_sel}}}[$__range]), '
         '"orca_topic", "$1", "orca_worktree_name", "coordinator-chat-(.+)"))')
    return f'({w} + {c}) or {w} or {c}'


def by_role(metric, sel=""):
    """役割 × モデル。label_replace は後のものが勝つ"""
    s = ", ".join(['orca_worktree_name!=""'] + ([sel] if sel else []))
    return ('sum by (role, model) (label_replace(label_replace(label_replace('
            f'sum by (orca_worktree_name, model) (increase({metric}{{{s}}}[$__range])), '
            '"role", "worker", "orca_worktree_name", ".+"), '
            '"role", "topic-chat", "orca_worktree_name", "coordinator-chat-.+"), '
            '"role", "main-chat", "orca_worktree_name", "coordinator"))')


def coord_cost(win):
    """調整コスト = main-chat + topic-chat。旧方式は worktree 名が空で vcs_repository_name=coordinator のものも足す"""
    return (f'((sum(increase({COST}{{orca_worktree_name=~"{COORD}"}}[{win}])) or vector(0)) + '
            f'(sum(increase({COST}{{orca_worktree_name="", vcs_repository_name="coordinator"}}[{win}])) or vector(0)))')


def worker_cost(win):
    return f'sum(increase({COST}{{orca_worktree_name!="", orca_worktree_name!~"{COORD}"}}[{win}]))'


NEW_DISPATCH = f'count(last_over_time(orca_dispatch_start_time_seconds[$__range]) >= {FROM})'
stat("調整コスト (USD)", "main-chat + topic-chat (旧方式は coordinator) の期間中のコスト", 0, 6,
     prom("A", coord_cost("$__range")), unit="currencyUSD", decimals=2)
stat("ワーカーのコスト (USD)", "ワーカー (coordinator 以外の worktree) の期間中のコスト", 6, 6,
     prom("A", worker_cost("$__range")), unit="currencyUSD", decimals=2)
stat("調整コスト比", "調整コスト ÷ ワーカーのコスト。話題チャット方式に切り替えた前後で比べる。下がれば、調整にかかるコストが仕事に対して減った",
     12, 6, prom("A", f'{coord_cost("$__range")} / {worker_cost("$__range")}'), decimals=2)
stat("Dispatch 1 件あたりの調整コスト (USD)", "調整コスト ÷ 期間中に出したワーカー (Dispatch) の数",
     18, 6, prom("A", f'{coord_cost("$__range")} / {NEW_DISPATCH}'), unit="currencyUSD", decimals=2)
y[0] += 4


def table6(title, desc, h, expr_of, keys, ren, sort):
    """instant の table を merge して、コスト・トークン (型別) の列に並べる"""
    t = [prom("A", expr_of(COST))] + [prom(chr(ord("B") + i), expr_of(TOK, f'type="{ty}"')) for i, (ty, _) in enumerate(TYPES)]
    rename = {**ren, "Value #A": "コスト (USD)",
              **{f"Value #{chr(ord('B') + i)}": f"{lab} (token)" for i, (_, lab) in enumerate(TYPES)}}
    over = [by_name("コスト (USD)", [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}])]
    over += [by_name(f"{lab} (token)", [{"id": "unit", "value": "short"}, {"id": "decimals", "value": 0}]) for _, lab in TYPES]
    panel("table", title, desc, 0, 24, h, t, PROM,
          transformations=[{"id": "merge", "options": {}},
                           {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": rename,
                                                          "indexByName": {k: i for i, k in enumerate(keys + ["コスト (USD)"] + [f"{lab} (token)" for _, lab in TYPES])}}}],
          fieldConfig={"defaults": {"unit": "short"}, "overrides": over},
          options={"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": sort, "desc": True}]})
    y[0] += h


table6("話題ごとのコストとトークン", "話題 = 話題チャット本体 + その話題の Run のワーカー。ワーカー分は rate を足した近似で、短いセッションでは誤差が出る。"
       "`(不明)` は Run の話題が分からないもの (ラベルが付く前に閉じた Run など)、`(main-chat 直)` は main-chat が直接回した Run。話題チャット本体しかない話題 (ワーカーを出していない) も出る。"
       "cache 読みはコストが小さくトークンは大きくなりやすいので、コストで見比べる", 10, by_topic, ["orca_topic"],
       {"orca_topic": "話題"}, "コスト (USD)")
table6("役割 × モデル別のコストとトークン", "役割は worktree 名で決める。main-chat = `coordinator`、topic-chat = `coordinator-chat-<topic>`、worker = それ以外。"
       "main-chat に Opus が入っていれば、main-chat を Sonnet で開く約束が守られていない", 8, by_role, ["role", "model"],
       {"role": "役割", "model": "モデル"}, "コスト (USD)")
DAILY = {"interval": "1d"}
panel("timeseries", "調整コスト比の日次", "日ごと (UTC) の 調整コスト ÷ ワーカーのコスト。切り替え (dotfiles PR #37 のマージ) の前後で比べる。旧方式は `coordinator` (と worktree 名が空で `vcs_repository_name=\"coordinator\"`) を調整コストに数える。"
      "分母のワーカーが少ない日は比が跳ねる",
      0, 12, 8, [{**prom("A", f'{coord_cost("1d")} / {worker_cost("1d")}', instant=False, legend="調整コスト比"), **DAILY}], PROM,
      fieldConfig={"defaults": {"custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 10, "showPoints": "always"}, "unit": "short", "min": 0}, "overrides": []},
      options={"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}, "tooltip": {"mode": "multi", "sort": "desc"}})
panel("timeseries", "役割別のコストの日次", "日ごと (UTC) のコストを役割別に積む。旧方式の coordinator (worktree 名が空) は「main-chat (worktree 名なし)」",
      12, 12, 8, [{**prom("A", f'sum by (role) (label_replace(label_replace(label_replace(sum by (orca_worktree_name) (increase({COST}{{orca_worktree_name!=""}}[1d])), '
                          '"role", "worker", "orca_worktree_name", ".+"), "role", "topic-chat", "orca_worktree_name", "coordinator-chat-.+"), "role", "main-chat", "orca_worktree_name", "coordinator"))',
                          instant=False, legend="{{role}}"), **DAILY},
                  {**prom("B", f'label_replace(sum(increase({COST}{{orca_worktree_name="", vcs_repository_name="coordinator"}}[1d])), "role", "main-chat (worktree 名なし)", "", "")',
                          instant=False, legend="{{role}}"), **DAILY}], PROM,
      fieldConfig={"defaults": {"custom": {"drawStyle": "bars", "stacking": {"mode": "normal"}, "fillOpacity": 80, "lineWidth": 1}, "unit": "currencyUSD"}, "overrides": []},
      options={"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}, "tooltip": {"mode": "multi", "sort": "desc"}})
y[0] += 8

dash = {
    "uid": "orca-orchestration",
    "title": "Orca orchestration",
    "description": "coordinator が Orca でワーカーをどう回したか (Run / Task / ワーカー / メッセージ) と手戻り",
    "tags": ["claude-code", "orca"],
    "timezone": "browser",
    "editable": True,
    "graphTooltip": 1,
    "schemaVersion": 41,
    "time": {"from": "now-7d", "to": "now"},
    "refresh": "",
    "links": [{"type": "dashboards", "title": "Claude Code", "tags": ["claude-code"], "asDropdown": False, "includeVars": False,
               "keepTime": True, "targetBlank": False, "icon": "external link"}],
    "templating": {"list": [
        {"type": "query", "name": "worker", "label": "ワーカー", "datasource": PROM,
         "query": {"qryType": 1, "query": "label_values(orca_dispatch_info, orca_worktree_name)", "refId": "PrometheusVariableQueryEditor-VariableQuery"},
         "definition": "label_values(orca_dispatch_info, orca_worktree_name)", "refresh": 2, "includeAll": True, "allValue": ".*",
         "multi": True, "sort": 1, "current": {"text": ["All"], "value": ["$__all"]}},
        # 値は Run のトレース ID、表示は目的。trace_id は ID の作り方を変えると変わるので、いま送られている (直近 15 分) 系列から取る
        {"type": "query", "name": "run", "label": "Run (タイムライン)", "datasource": PROM,
         "query": {"qryType": 3, "query": "query_result(last_over_time(orca_run_info{orca_run_state=\"closed\"}[15m]))", "refId": "PrometheusVariableQueryEditor-VariableQuery"},
         "definition": "query_result(last_over_time(orca_run_info{orca_run_state=\"closed\"}[15m]))",
         "regex": "/orca_run_objective=\"(?<text>[^\"]*)\".*trace_id=\"(?<value>[0-9a-f]+)\"/",
         "refresh": 2, "includeAll": False, "multi": False, "sort": 0,
         "description": "タイムラインに出す Run。トレースは終わった Run (coordinator が束縛していない、動いているワーカーが無い) だけにある"},
    ]},
    "panels": panels,
}
JOB = 'job="home-k8s/orca"'
METRICS = "|".join(sorted(["orca_run_info", "orca_run_start_time_seconds", "orca_run_duration_seconds", "orca_run_tasks", "orca_dispatch_info", "orca_dispatch_start_time_seconds", "orca_dispatch_duration_seconds", "orca_dispatch_time_to_done_seconds", "orca_dispatch_heartbeat_max_gap_seconds", "orca_dispatch_failures", "orca_dispatch_followups", "orca_dispatch_replies", "orca_dispatch_questions", "orca_dispatch_escalations", "orca_dispatch_heartbeats", "orca_dispatch_worker_done", "orca_dispatch_worker_done_rejected", "orca_dispatch_rejected", "orca_messages", "orca_exporter_last_success_seconds"], key=len, reverse=True))
def pin(e):
    e = re.sub(r'\b(' + METRICS + r')\{', lambda m: m.group(1) + '{' + JOB + ', ', e)
    return re.sub(r'\b(' + METRICS + r')(\[|\))', lambda m: m.group(1) + '{' + JOB + '}' + m.group(2), e)
for p in panels:
    for t in p.get("targets", []):
        if t["datasource"] == PROM:
            t["expr"] = pin(t["expr"])
for v in dash["templating"]["list"]:
    if v["datasource"] == PROM:
        v["definition"] = pin(v["definition"]); v["query"]["query"] = pin(v["query"]["query"])
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orca-orchestration.json")
with open(out, "w") as f:
    json.dump(dash, f, ensure_ascii=False, indent=2)
    f.write("\n")
