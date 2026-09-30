#!/usr/bin/env python3
"""Orca のオーケストレーション (Run / Task / Dispatch / メッセージ) を読み、OTLP/HTTP (JSON) で
OTel Collector に送る。Orca 自体は OTel を出さないので、`orca orchestration ... --json` を
定期的に読んで信号に直す。構成と決めごとは docs/observability/orca-orchestration.md。

- 読むだけ: run-list / task-list / inbox / worker-list / worker-show / dispatch-show と、terminal list しか叩かない。
  check (既読にする) と run-use (coordinator の束縛を奪う) は使わない。
- ログ (Loki): メッセージ 1 件、Task・Dispatch の状態変化 1 回ごとに 1 レコード。送ったキーを
  状態ファイルに残し、再起動しても二重に送らない。
- トレース (Tempo): Run を親、Task を子、Dispatch (ワーカー) を孫にし、メッセージをスパン
  イベントにする。終わったものだけを 1 回だけ送る (Tempo は同じスパンを後から直せない)。
- メトリクス (Prometheus): 毎回、いまの状態をゲージで送り直す。数え直しなので二重計上が無い。

依存は標準ライブラリだけ (systemd のユーザーユニットから python3 で直接動かす)。
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ORCA = os.environ.get("ORCA_BIN", str(Path.home() / ".orca-relay/bin/orca"))
ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318").rstrip("/")
STATE_DIR = Path(
    os.environ.get("ORCA_EXPORTER_STATE_DIR", Path.home() / ".local/share/home-k8s/observability/orca-exporter")
)
STATE_FILE = STATE_DIR / "state.json"
VERSION = "2"
# トレース ID とスパン ID の種。変えると、送り直したときに別のトレースになる
ID_SALT = "v3"

# Task の終わった状態 (task-update の --status のうち、この先動かないもの)
TASK_DONE = {"completed", "failed"}
# Dispatch の終わった状態 (worker-show の dispatch.status)
DISPATCH_DONE = {"completed", "failed", "abandoned", "stopped", "cancelled"}
# 配達されないまま、この秒数を過ぎたメッセージは配達を待たずに送る。Loki は同じストリームの
# 最新より 1 時間 (max_chunk_age の半分) 以上古いログを捨てるので、それより十分短くする
UNDELIVERED_GRACE = 600
# 取りこぼし防止に inbox を引く件数の上限 (CLI 側に上限は無い)
INBOX_LIMIT = 100000


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---- 時刻 ------------------------------------------------------------------

def parse_ts(v):
    """Orca の時刻 (ISO 8601 の Z 付き / "YYYY-MM-DD HH:MM:SS" (UTC) / epoch ms) を epoch 秒に"""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)
    s = str(v).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def ns(t):
    return str(int(t * 1e9))


# ---- Orca CLI (読むだけ) -----------------------------------------------------

class OrcaError(Exception):
    pass


def orca(*args, group="orchestration"):
    p = subprocess.run([ORCA, group, *args, "--json"], capture_output=True, text=True, timeout=120)
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise OrcaError(f"orca {args[0]}: exit {p.returncode}: {p.stderr.strip()[-300:]}")
    if not d.get("ok"):
        raise OrcaError(f"orca {args[0]}: {d.get('error', {}).get('code')}: {d.get('error', {}).get('message')}")
    return d["result"]


def payload_of(m):
    try:
        return json.loads(m["payload"]) if m.get("payload") else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def worktree_name(worktree_id):
    """Orca の worktree id (<uuid>::<path>) からワーカー名 (path の basename) を取る。
    Claude Code 側の orca.worktree.name (dotfiles の shell/prompt.sh) と同じ作り方"""
    if not worktree_id or "::" not in worktree_id:
        return ""
    return os.path.basename(worktree_id.split("::", 1)[1].rstrip("/"))


def coordinator_worktrees(runs, terminals, known):
    """Run ごとの coordinator の worktree 名 (話題チャットなら coordinator-chat-<topic>)。
    run-list の coordinator_handle は terminal list の handle で引く。handle は Run を閉じる・
    run-use で別のチャットに渡すと変わるので、引けた最後の値を known (状態ファイル) に残し、
    引けない間はそれを使う。known を書き換えて返す"""
    wt_of = {t["handle"]: worktree_name(t.get("worktreeId")) for t in terminals}
    for r in runs:
        name = wt_of.get(r.get("coordinator_handle"))
        if name:
            known[r["id"]] = name
    return known


def collect(cache):
    """Orca から全部読む。cache は終わった Dispatch の worker-show の結果 (もう変わらないので引き直さない)"""
    runs, cursor = [], None
    while True:
        r = orca("run-list", "--limit", "100", *(["--cursor", cursor] if cursor else []))
        runs += r["runs"]
        cursor = r.get("nextCursor")
        if not cursor or not r["runs"]:
            break
    tasks = []
    for run in runs:
        tasks += orca("task-list", "--run", run["id"])["tasks"]
    messages = orca("inbox", "--limit", str(INBOX_LIMIT))["messages"]
    # worker-list は --run が無いと、打った場所 (cwd) に束縛された Run だけを返すことがある
    # (systemd から $HOME で動かすと coordinator の Run に絞られた)。Run ごとに引く
    workers = []
    for run in runs:
        cursor = None
        while True:
            r = orca("worker-list", "--run", run["id"], "--limit", "100", *(["--cursor", cursor] if cursor else []))
            workers += r["workers"]
            cursor = (r.get("page") or {}).get("nextCursor")
            if not cursor:
                break

    # Dispatch の詳細 (開始・終了時刻、モデル、ワーカー名)。worker-list に無い Dispatch
    # (worker-start を使わない dispatch で出したもの) は dispatch-show で取る
    ids = {w["dispatchId"] for w in workers}
    ids |= {t["dispatch_id"] for t in tasks if t.get("dispatch_id")}
    ids |= {payload_of(m).get("dispatchId") for m in messages} - {None}
    ids |= {m["to_handle"][len("dispatch:"):] for m in messages if m["to_handle"].startswith("dispatch:")}
    task_of = {t["dispatch_id"]: t["id"] for t in tasks if t.get("dispatch_id")}
    for m in messages:
        p = payload_of(m)
        if p.get("dispatchId") and p.get("taskId"):
            task_of.setdefault(p["dispatchId"], p["taskId"])
    details = {}
    for d in sorted(ids):
        if d in cache:
            details[d] = cache[d]
            continue
        det = None
        try:
            det = normalize_worker_show(orca("worker-show", "--dispatch", d))
        except OrcaError:
            if d in task_of:
                try:
                    r = orca("dispatch-show", "--task", task_of[d])["dispatch"]
                    if r and r.get("id") == d:
                        det = normalize_dispatch_show(r)
                except OrcaError:
                    pass
        if det:
            details[d] = det
            if det["status"] in DISPATCH_DONE:
                cache[d] = det
    return runs, tasks, messages, workers, details


def normalize_worker_show(r):
    d, w = r.get("dispatch") or {}, r.get("worker") or {}
    opts = w.get("startOptions") or {}
    launch = (opts.get("launch") or {}).get("effective") or (opts.get("launch") or {}).get("requested") or {}
    wt = ""
    for res in (w.get("createdResources") or []) + (w.get("residualResources") or []):
        if res.get("kind") == "worktree":
            wt = res.get("id") or ""
    return {
        "id": d.get("id"),
        "run_id": d.get("runId"),
        "task_id": d.get("taskId") or d.get("task_id"),
        "status": d.get("status") or "",
        "assignee": d.get("assigneeHandle") or "",
        "dispatched_at": parse_ts(d.get("dispatchedAt") or d.get("createdAt")),
        "completed_at": parse_ts(d.get("completedAt")),
        "last_heartbeat_at": parse_ts(d.get("lastHeartbeatAt")),
        "failure_count": d.get("failureCount") or 0,
        "termination_reason": d.get("terminationReason") or "",
        "retry_of": d.get("retryOfDispatchId") or "",
        "model": launch.get("model") or "",
        "agent": launch.get("agent") or opts.get("agent") or "",
        "worktree_id": wt,
    }


def normalize_dispatch_show(d):
    return {
        "id": d.get("id"),
        "run_id": d.get("run_id"),
        "task_id": d.get("task_id"),
        "status": d.get("status") or "",
        "assignee": "",
        "dispatched_at": parse_ts(d.get("dispatched_at") or d.get("created_at")),
        "completed_at": parse_ts(d.get("completed_at")),
        "last_heartbeat_at": parse_ts(d.get("last_heartbeat_at")),
        "failure_count": d.get("failure_count") or 0,
        "termination_reason": d.get("termination_reason") or "",
        "retry_of": d.get("retry_of_dispatch_id") or "",
        "model": "",
        "agent": "",
        "worktree_id": "",
    }


# ---- 読んだものを組み立てる ---------------------------------------------------

def build(runs, tasks, messages, workers, details, now):
    """Run / Task / Dispatch / メッセージを互いに結び、手戻りの数字を数える"""
    tasks_by_id = {t["id"]: t for t in tasks}
    wl = {w["dispatchId"]: w for w in workers}
    disp = {}
    for did, det in details.items():
        w = wl.get(did) or {}
        res = w.get("resource") or {}
        wt = res.get("worktreeId") or det.get("worktree_id") or ""
        t = tasks_by_id.get(det.get("task_id")) or {}
        disp[did] = {
            **det,
            "run_id": det.get("run_id") or w.get("runId") or t.get("run_id") or "",
            "worker": worktree_name(wt),
            "worker_state": w.get("workerState") or "",
            "terminal_state": w.get("terminalState") or "",
            "release_state": res.get("releaseState") or "",
            "retained_reason": res.get("retainedReason") or "",
            "ownership": res.get("ownershipState") or "",
            "attention": ",".join(((w.get("projection") or {}).get("attention") or {}).get("categories") or []),
            "msgs": [],
        }
    by_terminal = {d["assignee"]: d for d in disp.values() if d["assignee"]}

    msgs = []
    for m in messages:
        p = payload_of(m)
        rej = p.get("_orcaLifecycleRejection") or {}
        did = p.get("dispatchId") or ""
        to, frm = m["to_handle"] or "", m["from_handle"] or ""
        if to.startswith("dispatch:"):
            did = did or to[len("dispatch:"):]
        elif not did and to in by_terminal:
            did = by_terminal[to]["id"]
        elif not did and frm.startswith("dispatch:"):
            did = frm[len("dispatch:"):]
        d = disp.get(did)
        worker_side = frm.startswith("dispatch:") or (d is not None and frm == d["assignee"])
        # coordinator からワーカーへの、返事ではない指示 (追加指示・訂正)。ask への返事は数えない
        to_worker = to.startswith("dispatch:") or (d is not None and to == d["assignee"])
        followup = to_worker and not worker_side and not m.get("thread_id")
        created = parse_ts(m["created_at"])
        delivered = parse_ts(m.get("delivered_at"))
        e = {
            "id": m["id"],
            "run_id": m.get("run_id") or (d or {}).get("run_id") or "",
            "task_id": p.get("taskId") or (d or {}).get("task_id") or "",
            "dispatch_id": did if d is not None or did else "",
            "worker": (d or {}).get("worker", ""),
            "model": (d or {}).get("model", ""),
            "type": m.get("type") or "",
            "subject": m.get("subject") or "",
            "body": m.get("body") or "",
            "from": frm,
            "to": to,
            "thread_id": m.get("thread_id") or "",
            "direction": "worker_to_coordinator" if worker_side else ("coordinator_to_worker" if to_worker else "other"),
            "followup": followup,
            "reply": to_worker and not worker_side and bool(m.get("thread_id")),
            "rejected": bool(rej),
            "rejection_code": rej.get("code") or "",
            "phase": p.get("phase") or "",
            "outcome": p.get("outcome") or "",
            "report_path": p.get("reportPath") or "",
            "files_modified": len(p.get("filesModified") or []),
            "created": created,
            "delivered": delivered,
            "delay": (delivered - created) if (created and delivered) else None,
        }
        msgs.append(e)
        if d is not None:
            d["msgs"].append(e)

    for d in disp.values():
        ms = sorted(d["msgs"], key=lambda e: e["created"])
        hb = [e["created"] for e in ms if e["type"] == "heartbeat" and not e["rejected"]]
        done = [e for e in ms if e["type"] == "worker_done" and not e["rejected"]]
        start = d["dispatched_at"]
        end = d["completed_at"] or (done[0]["created"] if done else None)
        d["end"] = end
        d["duration"] = (end - start) if (start and end) else ((now - start) if start else None)
        d["time_to_done"] = (done[0]["created"] - start) if (done and start) else None
        # heartbeat の途切れ: 開始 → 最初の heartbeat、heartbeat 同士、最後 → 終了 (動いていれば現在) の最大の間
        pts = ([start] if start else []) + hb + ([end or now] if (start or hb) else [])
        d["hb_max_gap"] = max((b - a for a, b in zip(pts, pts[1:])), default=None)
        d["n"] = {
            "followups": sum(e["followup"] for e in ms),
            "replies": sum(e["reply"] for e in ms),
            "questions": sum(e["type"] == "question" for e in ms),
            "escalations": sum(e["type"] == "escalation" for e in ms),
            "heartbeats": len(hb),
            "worker_done": len(done),
            "worker_done_rejected": sum(e["type"] == "worker_done" and e["rejected"] for e in ms),
            "rejected": sum(e["rejected"] for e in ms),
        }

    # Run の終わり: 束縛している coordinator がいない、かつ動いている Dispatch が無い
    run_info = {}
    for r in runs:
        rt = [t for t in tasks if t["run_id"] == r["id"]]
        rd = [d for d in disp.values() if d["run_id"] == r["id"]]
        rm = [e for e in msgs if e["run_id"] == r["id"]]
        start = parse_ts(r["created_at"])
        last = max(
            [parse_ts(r.get("updated_at")) or start]
            + [parse_ts(t.get("completed_at")) or parse_ts(t["created_at"]) for t in rt]
            + [e["created"] for e in rm]
            + [d["end"] for d in rd if d["end"]]
        )
        running = [d for d in rd if d["status"] not in DISPATCH_DONE]
        closed = not r.get("coordinator_handle") and not running
        run_info[r["id"]] = {"start": start, "last": last, "closed": closed, "tasks": rt, "dispatches": rd, "msgs": rm}
    return disp, msgs, run_info


# ---- OTLP/JSON ----------------------------------------------------------------

def av(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": "" if v is None else str(v)}


def attrs(d):
    return [{"key": k, "value": av(v)} for k, v in d.items() if v not in (None, "")]


RESOURCE = {
    "attributes": attrs({
        "service.name": "orca",
        # Loki のストリームのラベルになる (service_namespace)。ダッシュボードはこれで絞る
        "service.namespace": "home-k8s",
        "service.version": VERSION,
        "host.name": os.uname().nodename,
    })
}
SCOPE = {"name": "home-k8s/orca-exporter", "version": VERSION}


def post(signal, body):
    req = urllib.request.Request(
        f"{ENDPOINT}/v1/{signal}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def trace_id(run_id):
    return hashlib.md5(f"orca-run:{ID_SALT}:{run_id}".encode()).hexdigest()


def span_id(kind, key):
    return hashlib.md5(f"orca-{kind}:{ID_SALT}:{key}".encode()).hexdigest()[:16]


def ids(run_id="", task_id="", dispatch_id="", worker=""):
    return {"orca.run.id": run_id, "orca.task.id": task_id, "orca.dispatch.id": dispatch_id, "orca.worktree.name": worker}


# ---- ログ ---------------------------------------------------------------------

def log_records(disp, msgs, tasks, run_info, state, now):
    """まだ送っていないイベントを (キー, 時刻, LogRecord) で返す"""
    sent = state["logs_sent"]
    out = []

    def add(key, t, name, body, a):
        if key in sent:
            return
        out.append((key, t, {
            "timeUnixNano": ns(t),
            "observedTimeUnixNano": ns(now),
            "severityNumber": 9,
            "severityText": "INFO",
            "body": {"stringValue": body},
            "attributes": attrs({"event.name": name, **a}),
        }))

    for e in msgs:
        # 配達を待ってから送る (送信→配達の遅れを載せるため)。配達されないまま長く残るものは待たない
        if e["delivered"] is None and now - e["created"] < UNDELIVERED_GRACE:
            continue
        add(f"msg:{e['id']}", e["created"], "orca.message", e["body"] or e["subject"], {
            **ids(e["run_id"], e["task_id"], e["dispatch_id"], e["worker"]),
            "orca.message.id": e["id"],
            "orca.message.type": e["type"],
            "orca.message.subject": e["subject"],
            "orca.message.from": e["from"],
            "orca.message.to": e["to"],
            "orca.message.thread_id": e["thread_id"],
            "orca.message.direction": e["direction"],
            "orca.message.followup": e["followup"],
            "orca.message.reply": e["reply"],
            "orca.message.rejected": e["rejected"],
            "orca.message.rejection_code": e["rejection_code"],
            "orca.message.delivery_delay_seconds": e["delay"],
            "orca.phase": e["phase"],
            "orca.outcome": e["outcome"],
            "orca.report_path": e["report_path"],
            "orca.files_modified": e["files_modified"],
            "orca.model": e["model"],
        })

    # Task: 作られた (created_at) と、状態が変わるたび。終わった状態は completed_at、それ以外は観測した時刻
    last = state["task_status"]
    for t in tasks:
        d = disp.get(t.get("dispatch_id") or "") or {}
        a = {
            **ids(t["run_id"], t["id"], t.get("dispatch_id") or "", d.get("worker", "")),
            "orca.task.title": t.get("task_title") or "",
            "orca.task.parent_id": t.get("parent_id") or "",
            "orca.model": d.get("model", ""),
        }
        created = parse_ts(t["created_at"])
        add(f"task:{t['id']}:created", created, "orca.task.status", t.get("task_title") or "", {**a, "orca.task.status": "created"})
        st = t["status"]
        if last.get(t["id"]) != st:
            done_at = parse_ts(t.get("completed_at")) if st in TASK_DONE else None
            res = {}
            try:
                res = json.loads(t["result"]) if t.get("result") else {}
            except (TypeError, json.JSONDecodeError):
                pass
            first = t["id"] not in last
            at = done_at or (created if first and st in ("pending", "ready") else None) \
                or (d.get("dispatched_at") if first and st == "dispatched" else None) or now
            add(f"task:{t['id']}:{st}", at,
                "orca.task.status", (res.get("subject") or t.get("task_title") or ""), {
                    **a, "orca.task.status": st, "orca.outcome": res.get("outcome", ""),
                    "orca.task.previous_status": last.get(t["id"], ""),
                })

    # Dispatch: 開始と、終わったとき (状態・release の結果)
    for d in disp.values():
        a = {
            **ids(d["run_id"], d["task_id"], d["id"], d["worker"]),
            "orca.model": d["model"],
            "orca.dispatch.retry_of": d["retry_of"],
        }
        if d["dispatched_at"]:
            add(f"dispatch:{d['id']}:started", d["dispatched_at"], "orca.dispatch.status", f"worker {d['worker'] or d['id']} started",
                {**a, "orca.dispatch.status": "dispatched"})
        if d["status"] in DISPATCH_DONE and d["end"]:
            add(f"dispatch:{d['id']}:{d['status']}", d["end"], "orca.dispatch.status", f"worker {d['worker'] or d['id']} {d['status']}", {
                **a, "orca.dispatch.status": d["status"], "orca.worker.state": d["worker_state"],
                "orca.dispatch.termination_reason": d["termination_reason"],
                "orca.dispatch.failure_count": d["failure_count"],
                "orca.release.state": d["release_state"], "orca.release.retained_reason": d["retained_reason"],
                "orca.dispatch.followups": d["n"]["followups"], "orca.dispatch.duration_seconds": d["duration"],
            })
    out.sort(key=lambda x: x[1])
    return out


# ---- トレース -------------------------------------------------------------------

def span(tid, sid, parent, name, start, end, a, events=(), error=False):
    s = {
        "traceId": tid,
        "spanId": sid,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": ns(start),
        "endTimeUnixNano": ns(max(end, start)),
        "attributes": attrs(a),
        "events": [{"timeUnixNano": ns(t), "name": n, "attributes": attrs(ea)} for t, n, ea in events],
        "status": {"code": 2 if error else 1},
    }
    if parent:
        s["parentSpanId"] = parent
    return s


def msg_event(e):
    name = e["type"] + (" (rejected)" if e["rejected"] else "") + (" [followup]" if e["followup"] else "")
    return (e["created"], name, {
        "orca.message.id": e["id"], "orca.message.subject": e["subject"], "orca.message.direction": e["direction"],
        "orca.phase": e["phase"], "orca.outcome": e["outcome"], "orca.message.delivery_delay_seconds": e["delay"],
    })


def spans(runs, disp, run_info, state):
    """終わった Task (とその Dispatch)、閉じた Run のスパンを (キー, span) で返す。
    Run が閉じたら、終わっていない Task も Run の終わりで区切って送る"""
    sent = state["spans_sent"]
    out = []
    for r in runs:
        ri = run_info[r["id"]]
        tid = trace_id(r["id"])
        rsid = span_id("run", r["id"])
        placed = set()
        for t in ri["tasks"]:
            done = t["status"] in TASK_DONE
            if f"task:{t['id']}" in sent or not (done or ri["closed"]):
                continue
            tsid = span_id("task", t["id"])
            td = sorted([d for d in ri["dispatches"] if d["task_id"] == t["id"]], key=lambda d: d["dispatched_at"] or 0)
            t_start = parse_ts(t["created_at"])
            t_end = (parse_ts(t.get("completed_at")) if done else None) or max(
                [ri["last"]] if not done else [t_start] + [d["end"] or t_start for d in td])
            cur = disp.get(t.get("dispatch_id") or "") or (td[-1] if td else {})
            for d in td:
                ev = [msg_event(e) for e in d["msgs"]]
                placed |= {e["id"] for e in d["msgs"]}
                out.append((f"dispatch:{d['id']}", span(
                    tid, span_id("dispatch", d["id"]), tsid, f"worker {d['worker'] or d['id']}",
                    d["dispatched_at"] or t_start, d["end"] or t_end, {
                        **ids(r["id"], t["id"], d["id"], d["worker"]),
                        "orca.span.kind": "dispatch", "orca.model": d["model"], "orca.agent": d["agent"],
                        "orca.dispatch.status": d["status"], "orca.worker.state": d["worker_state"],
                        "orca.dispatch.termination_reason": d["termination_reason"],
                        "orca.release.state": d["release_state"], "orca.release.retained_reason": d["retained_reason"],
                        **{f"orca.dispatch.{k}": v for k, v in d["n"].items()},
                        "orca.dispatch.heartbeat_max_gap_seconds": d["hb_max_gap"],
                        "orca.dispatch.unfinished": d["end"] is None,
                    }, ev, error=d["status"] == "failed")))
            ev = [msg_event(e) for e in ri["msgs"] if e["task_id"] == t["id"] and e["id"] not in placed]
            placed |= {e[2]["orca.message.id"] for e in ev}
            out.append((f"task:{t['id']}", span(tid, tsid, rsid, t.get("task_title") or t["id"], t_start, t_end, {
                **ids(r["id"], t["id"], t.get("dispatch_id") or "", cur.get("worker", "")),
                "orca.span.kind": "task", "orca.task.status": t["status"], "orca.task.unfinished": not done,
                "orca.model": cur.get("model", ""), "orca.task.dispatches": len(td),
            }, ev, error=t["status"] == "failed")))
        if ri["closed"] and f"run:{r['id']}" not in sent:
            ev = [msg_event(e) for e in ri["msgs"] if not e["task_id"] and e["id"] not in placed]
            st = {}
            for t in ri["tasks"]:
                st[t["status"]] = st.get(t["status"], 0) + 1
            out.append((f"run:{r['id']}", span(tid, rsid, None, r.get("objective") or r["id"], ri["start"], ri["last"], {
                **ids(r["id"]),
                "orca.span.kind": "run", "orca.run.objective": r.get("objective") or "",
                "orca.run.tasks": len(ri["tasks"]), "orca.run.dispatches": len(ri["dispatches"]),
                **{f"orca.run.tasks.{k}": v for k, v in st.items()},
            }, ev)))
    return out


# ---- メトリクス -----------------------------------------------------------------

def metrics(runs, disp, msgs, run_info, now, coordinators):
    t = ns(now)
    series = {}

    def g(name, unit, desc, value, a):
        if value is None:
            return
        m = series.setdefault(name, {"name": name, "unit": unit, "description": desc, "gauge": {"dataPoints": []}})
        m["gauge"]["dataPoints"].append({"timeUnixNano": t, "attributes": attrs(a), "asDouble": float(value)})

    for r in runs:
        ri = run_info[r["id"]]
        a = {"orca.run.id": r["id"]}
        g("orca.run.info", "", "Run の目的と、トレース ID (値は常に 1)", 1, {
            **a, "orca.run.objective": r.get("objective") or "", "trace_id": trace_id(r["id"]),
            "orca.run.state": "closed" if ri["closed"] else "open",
            "orca.run.coordinator_worktree": coordinators.get(r["id"], "")})
        g("orca.run.start_time", "s", "Run を作った時刻 (epoch 秒)", ri["start"], a)
        g("orca.run.duration", "s", "Run を作ってから最後の動きまで", ri["last"] - ri["start"], a)
        counts = {}
        for tk in ri["tasks"]:
            counts[tk["status"]] = counts.get(tk["status"], 0) + 1
        for st, n in counts.items():
            g("orca.run.tasks", "", "Run の Task 数 (状態別)", n, {**a, "orca.task.status": st})
    for d in disp.values():
        a = {**ids(d["run_id"], d["task_id"], d["id"], d["worker"]), "orca.model": d["model"]}
        g("orca.dispatch.info", "", "Dispatch (ワーカー) の状態 (値は常に 1)", 1, {
            **a, "orca.dispatch.status": d["status"], "orca.worker.state": d["worker_state"],
            "orca.terminal.state": d["terminal_state"], "orca.release.state": d["release_state"],
            "orca.release.retained_reason": d["retained_reason"], "orca.resource.ownership": d["ownership"],
            "orca.dispatch.termination_reason": d["termination_reason"], "orca.attention": d["attention"],
            "orca.dispatch.retry_of": d["retry_of"]})
        g("orca.dispatch.start_time", "s", "ワーカーを出した時刻 (epoch 秒)", d["dispatched_at"], a)
        g("orca.dispatch.duration", "s", "ワーカーを出してから終わるまで (動いていれば現在まで)", d["duration"], a)
        g("orca.dispatch.time_to_done", "s", "ワーカーを出してから最初の worker_done (受理) まで", d["time_to_done"], a)
        g("orca.dispatch.heartbeat_max_gap", "s", "heartbeat の最大の間隔", d["hb_max_gap"], a)
        g("orca.dispatch.failures", "", "Dispatch の失敗回数", d["failure_count"], a)
        for k, v in d["n"].items():
            g(f"orca.dispatch.{k}", "", f"Dispatch あたりのメッセージ数 ({k})", v, a)
    counts = {}
    for e in msgs:
        k = (e["run_id"], e["type"], e["direction"], e["rejected"])
        counts[k] = counts.get(k, 0) + 1
    for (run, typ, direction, rej), n in counts.items():
        g("orca.messages", "", "メッセージ数 (Run・種別・向き・拒否の別)", n, {
            "orca.run.id": run, "orca.message.type": typ, "orca.message.direction": direction,
            "orca.message.rejected": str(rej).lower()})
    g("orca.exporter.last_success", "s", "exporter が最後に Orca を読めた時刻 (epoch 秒)", now, {})
    return list(series.values())


# ---- 状態 -----------------------------------------------------------------------

def load_state():
    try:
        s = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("logs_sent", [])
    s.setdefault("spans_sent", [])
    s.setdefault("task_status", {})
    s.setdefault("dispatch_cache", {})
    s.setdefault("run_coordinator", {})
    s["logs_sent"], s["spans_sent"] = set(s["logs_sent"]), set(s["spans_sent"])
    return s


def save_state(s):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    out = {**s, "logs_sent": sorted(s["logs_sent"]), "spans_sent": sorted(s["spans_sent"])}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    tmp.replace(STATE_FILE)


def chunks(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def cycle(state, dry_run=False):
    now = time.time()
    runs, tasks, messages, workers, details = collect(state["dispatch_cache"])
    try:
        terminals = orca("list", "--limit", "100000", group="terminal")["terminals"]
    except OrcaError as e:
        # 引けなくても他の信号は送る。coordinator の worktree は状態ファイルに残した値を使う
        log(f"terminal list: {e}")
        terminals = []
    coordinators = coordinator_worktrees(runs, terminals, state["run_coordinator"])
    disp, msgs, run_info = build(runs, tasks, messages, workers, details, now)
    logs = log_records(disp, msgs, tasks, run_info, state, now)
    sp = spans(runs, disp, run_info, state)
    ms = metrics(runs, disp, msgs, run_info, now, coordinators)
    stats = {"runs": len(runs), "tasks": len(tasks), "messages": len(messages), "dispatches": len(disp),
             "new_logs": len(logs), "new_spans": len(sp)}
    if dry_run:
        return stats, logs, sp, ms

    # Collector に届かなければ例外で抜け、送れた分だけが状態に残る (次の回で残りを送る)
    post("metrics", {"resourceMetrics": [{"resource": RESOURCE, "scopeMetrics": [{"scope": SCOPE, "metrics": ms}]}]})
    for part in chunks(logs, 200):
        post("logs", {"resourceLogs": [{"resource": RESOURCE, "scopeLogs": [{"scope": SCOPE, "logRecords": [x[2] for x in part]}]}]})
        state["logs_sent"] |= {x[0] for x in part}
        save_state(state)
    for part in chunks(sp, 200):
        post("traces", {"resourceSpans": [{"resource": RESOURCE, "scopeSpans": [{"scope": SCOPE, "spans": [x[1] for x in part]}]}]})
        state["spans_sent"] |= {x[0] for x in part}
        save_state(state)
    state["task_status"] = {t["id"]: t["status"] for t in tasks}
    state["last_success"] = now
    state["last_stats"] = stats
    state.pop("last_error", None)
    save_state(state)
    return stats, logs, sp, ms


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--interval", type=float, default=30, help="読む間隔 (秒)")
    ap.add_argument("--once", action="store_true", help="1 回だけ読んで送る")
    ap.add_argument("--dry-run", action="store_true", help="送らずに、送るはずのものを JSON で標準出力に出す (状態は変えない)")
    ap.add_argument("--status", action="store_true", help="状態ファイルの要約を出す")
    args = ap.parse_args()

    if args.status:
        s = load_state()
        last = s.get("last_success")
        print(json.dumps({
            "state_file": str(STATE_FILE),
            "endpoint": ENDPOINT,
            "last_success": datetime.fromtimestamp(last).astimezone().isoformat() if last else None,
            "seconds_since_last_success": round(time.time() - last) if last else None,
            "last_stats": s.get("last_stats"),
            "last_error": s.get("last_error"),
            "logs_sent": len(s["logs_sent"]),
            "spans_sent": len(s["spans_sent"]),
            "dispatches_cached": len(s["dispatch_cache"]),
            "run_coordinators": len(s["run_coordinator"]),
        }, ensure_ascii=False, indent=1))
        return

    if args.dry_run:
        stats, logs, sp, ms = cycle(load_state(), dry_run=True)
        json.dump({"stats": stats, "logs": [x[2] for x in logs], "spans": [x[1] for x in sp], "metrics": ms},
                  sys.stdout, ensure_ascii=False, indent=1)
        return

    state = load_state()
    while True:
        try:
            stats = cycle(state)[0]
            log(f"ok: {json.dumps(stats)}")
        except (OrcaError, OSError, urllib.error.URLError, subprocess.TimeoutExpired) as e:
            # Orca の relay が居ない・Collector が止まっている: 落ちずに次の回でやり直す
            log(f"skip: {type(e).__name__}: {e}")
            state["last_error"] = {"at": time.time(), "error": f"{type(e).__name__}: {e}"}
            save_state(state)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
