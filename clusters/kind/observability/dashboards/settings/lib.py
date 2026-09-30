"""generate.py が使う、Grafana のダッシュボード JSON の部品。"""
import copy, json

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
TEMPO = {"type": "tempo", "uid": "tempo"}
INF = {"type": "yesoreyeram-infinity-datasource", "uid": "backend-api"}
MIXED = {"type": "datasource", "uid": "-- Mixed --"}

PROM_URL = "http://prometheus-server.observability.svc.cluster.local"
TEMPO_URL = "http://tempo.observability.svc.cluster.local:3200"
LOKI_URL = "http://loki.observability.svc.cluster.local:3100"

SEL = '{service_name="claude-code"}'
FROM = {"key": "start", "value": "${__from:date:seconds}"}
TO = {"key": "end", "value": "${__to:date:seconds}"}


class Board:
    def __init__(self, uid, title, description, variables=None, tags=None):
        self.uid, self.title, self.description = uid, title, description
        self.variables = variables or []
        self.tags = tags or ["claude-code-setting"]
        self.panels = []
        self.x = self.y = self.rowh = 0

    def add(self, p, w=24, h=8):
        if p.get("title") == "この設定について":
            h = max(h, 12)
        if self.x + w > 24:
            self.y += self.rowh
            self.x = self.rowh = 0
        p = dict(p)
        p["id"] = len(self.panels) + 1
        p["gridPos"] = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.panels.append(p)
        self.x += w
        self.rowh = max(self.rowh, h)
        return p

    def row(self, title):
        if self.x:
            self.y += self.rowh
            self.x = self.rowh = 0
        self.add({"type": "row", "title": title, "collapsed": False, "panels": []}, 24, 1)
        self.y += 1
        self.x = self.rowh = 0

    def json(self):
        return {
            "uid": self.uid,
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "timezone": "browser",
            "editable": True,
            "graphTooltip": 1,
            "schemaVersion": 41,
            "time": {"from": "now-7d", "to": "now"},
            "refresh": "",
            "links": [
                {"type": "link", "title": "設定項目別の一覧", "url": "/d/cc-settings", "icon": "dashboard",
                 "keepTime": True, "targetBlank": False, "asDropdown": False, "includeVars": False, "tags": []},
                {"type": "dashboards", "title": "設定項目別", "tags": ["claude-code-setting"], "asDropdown": True,
                 "includeVars": False, "keepTime": True, "targetBlank": False, "icon": "external link"},
                {"type": "dashboards", "title": "Claude Code", "tags": ["claude-code"], "asDropdown": True,
                 "includeVars": False, "keepTime": True, "targetBlank": False, "icon": "external link"},
            ],
            "templating": {"list": self.variables},
            "panels": self.panels,
        }


# ---- targets ----

def prom(expr, legend="__auto", instant=False, ref="A", fmt=None):
    t = {"refId": ref, "datasource": PROM, "expr": expr, "legendFormat": legend}
    if instant:
        t.update({"instant": True, "range": False, "format": fmt or "table"})
    else:
        t.update({"range": True})
    return t


def loki(expr, legend=None, instant=False, ref="A", max_lines=None):
    t = {"refId": ref, "datasource": LOKI, "expr": expr, "queryType": "instant" if instant else "range"}
    if legend:
        t["legendFormat"] = legend
    if max_lines:
        t["maxLines"] = max_lines
    return t


def tempo_metrics(query, instant=False, ref="A"):
    return {"refId": ref, "datasource": TEMPO, "queryType": "traceql", "query": query,
            "metricsQueryType": "instant" if instant else "range", "tableType": "traces", "limit": 20}


def tempo_search(query, limit=50, spss=5, ref="A"):
    return {"refId": ref, "datasource": TEMPO, "queryType": "traceql", "query": query,
            "limit": limit, "spss": spss, "tableType": "spans"}


def inf(url, params, root, ref="A"):
    return {"refId": ref, "datasource": INF, "type": "json", "source": "url", "parser": "backend",
            "format": "table", "url": url, "url_options": {"method": "GET", "params": params},
            "root_selector": root, "columns": []}


def inf_loki_instant(expr, root, ref="A"):
    """Loki instant query over the dashboard range, via the HTTP API (for JSONata pivots)."""
    # Infinity は ${__range_s} を展開しないので、5 分刻みの query_range を引いて JSONata 側で足す
    return inf(LOKI_URL + "/loki/api/v1/query_range",
               [{"key": "query", "value": expr}, FROM, TO, {"key": "step", "value": "300"}], root, ref)


def inf_tempo_search(q, root, limit=100, spss=20, ref="A"):
    return inf(TEMPO_URL + "/api/search",
               [{"key": "q", "value": q}, {"key": "limit", "value": str(limit)}, {"key": "spss", "value": str(spss)},
                FROM, TO], root, ref)


# ---- panels ----

def text(title, md):
    return {"type": "text", "title": title, "options": {"mode": "markdown", "content": md}}


def table(title, targets, desc="", transformations=None, overrides=None, sort=None, wrap=None):
    ov = list(overrides or [])
    for name in wrap or []:
        ov.append({"matcher": {"id": "byName", "options": name},
                   "properties": [{"id": "custom.cellOptions", "value": {"type": "auto", "wrapText": True}}]})
    p = {"type": "table", "title": title, "description": desc,
         "datasource": targets[0]["datasource"] if len({json.dumps(t["datasource"]) for t in targets}) == 1 else MIXED,
         "targets": targets,
         "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": True,
                                                 "minWidth": 80}},
                         "overrides": ov},
         "options": {"showHeader": True, "cellHeight": "sm"},
         "transformations": transformations or []}
    if sort:
        p["options"]["sortBy"] = [{"displayName": sort, "desc": True}]
    return p


def ts(title, targets, desc="", unit="short", stack=False, bars=False):
    custom = {"drawStyle": "bars" if bars else "line", "lineWidth": 1, "fillOpacity": 60 if bars else 10,
              "showPoints": "never", "stacking": {"mode": "normal" if stack else "none", "group": "A"}}
    return {"type": "timeseries", "title": title, "description": desc,
            "datasource": targets[0]["datasource"] if len({json.dumps(t["datasource"]) for t in targets}) == 1 else MIXED,
            "targets": targets,
            "fieldConfig": {"defaults": {"unit": unit, "custom": custom}, "overrides": []},
            "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["sum"]},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def stat(title, targets, desc="", unit="short", color="blue"):
    return {"type": "stat", "title": title, "description": desc,
            "datasource": targets[0]["datasource"] if len({json.dumps(t["datasource"]) for t in targets}) == 1 else MIXED,
            "targets": targets,
            "fieldConfig": {"defaults": {"unit": unit, "noValue": "0", "decimals": None,
                                         "color": {"mode": "fixed", "fixedColor": color}},
                            "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "textMode": "value_and_name", "colorMode": "background", "graphMode": "none",
                        "justifyMode": "auto", "orientation": "auto", "wideLayout": True,
                        "showPercentChange": False}}


def barchart(title, targets, desc="", unit="short"):
    return {"type": "barchart", "title": title, "description": desc,
            "datasource": targets[0]["datasource"], "targets": targets,
            "fieldConfig": {"defaults": {"unit": unit, "custom": {"fillOpacity": 80}}, "overrides": []},
            "options": {"orientation": "horizontal", "showValue": "auto", "legend": {"showLegend": False},
                        "xTickLabelRotation": 0, "barWidth": 0.8}}


def organize(rename=None, exclude=None, index=None):
    # 1 クエリだけの instant は列名が Value にも Value #A にもなるので、両方に同じ名前を当てる
    for d in (rename, exclude, index):
        if d and "Value" in d:
            d.setdefault("Value #A", d["Value"])
    return {"id": "organize", "options": {"renameByName": rename or {}, "excludeByName": exclude or {},
                                          "indexByName": index or {}}}


MERGE = {"id": "merge", "options": {}}


def sort_by(field, desc=True):
    return {"id": "sortBy", "options": {"sort": [{"field": field, "desc": desc}]}}


def reduce_rows(calc="lastNotNull"):
    return {"id": "reduce", "options": {"reducers": [calc], "mode": "seriesToRows", "includeTimeField": False}}


def presence(b, title, items, desc="", w=6, h=4):
    """件数のタイルを並べる。1 件ごとに stat パネルを分け、パネル名に何の件数かを書く
    (Tempo のメトリクスはフレームに refId が付かず、1 枚の stat に並べると名前を付け分けられないため)。"""
    b.row(title)
    for legend, t in items:
        # 0 が正しい (オフなので届かない) ものは灰色、届くはずなのに 0 のものは赤
        expect_zero = legend.endswith("(0 が正)")
        p = stat("", [copy.deepcopy(t)], desc or "期間中の件数。0 = 1 件も届いていない。")
        p["options"]["textMode"] = "value_and_name"
        # Tempo はフレームに displayName を入れてくるので、既定値ではなく override で上書きする
        p["fieldConfig"]["overrides"] = [{"matcher": {"id": "byType", "options": "number"},
                                          "properties": [{"id": "displayName", "value": legend}]}]
        p["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
        p["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [
            {"color": "text" if expect_zero else "red", "value": None},
            {"color": "orange" if expect_zero else "green", "value": 1}]}
        p["options"]["text"] = {"titleSize": 13, "valueSize": 34}
        b.add(p, w, h)


# ---- presence count expressions ----

def lc(filters):
    """Loki: number of events matching the filters over the range."""
    return loki(f'sum(count_over_time({SEL} | {filters} [$__range])) or vector(0)', instant=True)


def pc(expr):
    """Prometheus: number of series matching the selector over the range."""
    return prom(f'count(last_over_time({expr}[$__range])) or vector(0)', instant=True, fmt="time_series")


def tc(q):
    """Tempo: number of spans matching the TraceQL filter over the range."""
    return tempo_metrics(f'{q} | count_over_time()', instant=True)


def var_custom(name, label, values, current=None, hide=0):
    opts = [{"text": v, "value": v, "selected": v == (current or values[0])} for v in values]
    return {"type": "custom", "name": name, "label": label, "query": ",".join(values), "options": opts,
            "current": {"text": current or values[0], "value": current or values[0]}, "hide": hide}


def var_inf(name, label, url, params, root, multi=True, include_all=True, hide=0, all_value=None):
    v = {"type": "query", "name": name, "label": label, "datasource": INF,
         "query": {"queryType": "infinity", "refId": "variable",
                   "infinityQuery": inf(url, params, root, "variable")},
         "refresh": 2, "sort": 1, "includeAll": include_all, "multi": multi, "hide": hide,
         "current": {"text": ["All"], "value": ["$__all"]} if include_all else {}}
    if all_value:
        v["allValue"] = all_value
    return v


def var_prom(name, label, query, multi=True, include_all=True, hide=0):
    return {"type": "query", "name": name, "label": label, "datasource": PROM,
            "query": {"qryType": 1, "query": query, "refId": "PrometheusVariableQueryEditor-VariableQuery"},
            "definition": query, "refresh": 2, "sort": 1, "includeAll": include_all, "multi": multi, "hide": hide,
            "current": {"text": ["All"], "value": ["$__all"]} if include_all else {}}


def header(env, value, arrives, stops, use, check):
    return (f"**環境変数**: {env}\n\n**いまの値**: {value}\n\n"
            f"**何が届くようになるか (公式)**: {arrives}\n\n**止めると消えるもの**: {stops}\n\n"
            f"**何が分かる・何に使えるか**: {use}\n\n**公式と実データの突き合わせ**: {check}")
