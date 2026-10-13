#!/usr/bin/env python3
"""
Independent backend harness (no Docker). Runs the real Django app under gunicorn
(+ opentelemetry-instrument in otlp mode), with tools/mock_sink.py as Sentry ingest
and OTLP receiver and a header-recording stand-in for the Go service. Sends one
request per scenario with its own W3C traceparent (and sentry-trace, like the
browser), then checks errors / logs / spans per trace.

  python3 tests/local/be_harness.py --python python3 --mode otlp --fix 1 --db sqlite
  python3 tests/local/be_harness.py --mode otlp --db postgres --pg-bin /path/to/pg/bin
  python3 tests/local/be_harness.py --mode sentry --db sqlite

Prints one JSON document with per-check PASS/FAIL and evidence.
"""
import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[2]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------- Go stand-in that records incoming propagation headers ----------
GO_HEADERS = []


class GoStub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        GO_HEADERS.append({"path": u.path, "traceparent": self.headers.get("traceparent"),
                           "sentry-trace": self.headers.get("sentry-trace"), "baggage": self.headers.get("baggage")})
        if u.path == "/billing/quote":
            time.sleep(int(q.get("delay_ms", "300")) / 1000)
            body, code = {"currency": "USD"}, 200
        elif q.get("fail") == "1":
            body, code = {"error": "replica pool unreachable"}, 500
        else:
            time.sleep(0.02)
            body, code = {"model_id": q.get("model_id"), "replicas": [{"id": "a"}]}, 200
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


SCENARIOS = [
    ("S1a", "GET", "/api/v1/models", None, False),
    ("S1b", "GET", "/api/v1/models/1/deployments", None, False),
    ("S2", "POST", "/graphql/", {"operationName": "ModelReplicaDetails", "variables": {"modelId": 1}}, False),
    ("S3", "GET", "/api/v1/models/3/config", None, False),
    ("S4", "GET", "/api/v1/models/2/deployments?replica_fail=1", None, False),
    ("S7", "GET", "/api/settings/billing", None, False),
    ("S8", "GET", "/api/v1/models/1/deployment-history", None, True),
    ("S9", "GET", "/api/v1/deployments/status-report", None, True),
]


def send(base, method, path, body, force_keep, trace_id, parent):
    headers = {"traceparent": f"00-{trace_id}-{parent}-01"}
    if os.environ.get("BEH_NO_SENTRY_TRACE") != "1":  # the browser sends both headers
        headers["sentry-trace"] = f"{trace_id}-{parent}-1"
    if force_keep:
        headers["x-demo-force-keep"] = "1"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    return code, round((time.time() - t0) * 1000)


def wait_http(url, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(0.3)
    return False


def read_jsonl(p):
    p = Path(p)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def attrs_of(sp):
    out = {}
    for a in sp.get("attributes", []):
        v = a.get("value", {})
        out[a["key"]] = next(iter(v.values())) if v else None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--bindir", default=None, help="dir with gunicorn/opentelemetry-instrument (default: next to --python, then ~/.local/bin)")
    ap.add_argument("--mode", default="otlp", choices=["otlp", "sentry"])
    ap.add_argument("--fix", default="1")
    ap.add_argument("--db", default="sqlite", choices=["sqlite", "postgres"])
    ap.add_argument("--pg-bin", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    work = Path(a.out or f"/tmp/beh_{a.mode}_{a.db}_{a.fix}_{int(time.time())}")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    procs = []
    sink_port, go_port, dj_port = free_port(), free_port(), free_port()

    def bin_(name):
        for d in filter(None, [a.bindir, str(Path(a.python).parent), os.path.expanduser("~/.local/bin")]):
            if (Path(d) / name).exists():
                return str(Path(d) / name)
        return name

    try:
        procs.append(subprocess.Popen([sys.executable, str(REPO / "tools/mock_sink.py"), "--port", str(sink_port), "--out", str(work / "sink")],
                                      stdout=open(work / "sink.log", "w"), stderr=subprocess.STDOUT))
        go = ThreadingHTTPServer(("127.0.0.1", go_port), GoStub)
        threading.Thread(target=go.serve_forever, daemon=True).start()

        env = dict(os.environ)
        for k in list(env):
            if k.startswith(("OTEL_", "SENTRY_", "POSTGRES_")):
                env.pop(k)
        env.update({
            "DJANGO_SETTINGS_MODULE": "demo.settings",
            "SENTRY_DSN_BE": f"http://publickey@127.0.0.1:{sink_port}/2",
            "GO_REPLICA_URL": f"http://127.0.0.1:{go_port}",
            "BILLING_DELAY_MS": "300",
            "BE_TRACING_MODE": a.mode,
            "FIX_LOG_SPAN_ID": a.fix,
            "PYTHONDONTWRITEBYTECODE": "1",
            "OTEL_SERVICE_NAME": "demo-api",
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_METRICS_EXPORTER": "none",
            "OTEL_LOGS_EXPORTER": "none",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{sink_port}",
            "OTEL_PROPAGATORS": "tracecontext,baggage",
            "OTEL_TRACES_SAMPLER": "parentbased_always_on",
            "OTEL_BSP_SCHEDULE_DELAY": "300",
        })
        if a.mode == "sentry":
            env["OTEL_SDK_DISABLED"] = "true"
        if a.db == "sqlite":
            env["SQLITE_PATH"] = str(work / "db.sqlite3")
        else:
            pg_port = free_port()
            pgdata = work / "pgdata"
            pgenv = dict(os.environ, LD_LIBRARY_PATH=str(Path(a.pg_bin).parent / "lib"))
            subprocess.run([f"{a.pg_bin}/initdb", "-D", str(pgdata), "-U", "demo", "--auth=trust"], env=pgenv, check=True, stdout=subprocess.DEVNULL)
            subprocess.run([f"{a.pg_bin}/pg_ctl", "-D", str(pgdata), "-o", f"-p {pg_port} -k {work}", "-l", str(work / "pg.log"), "-w", "start"], env=pgenv, check=True, stdout=subprocess.DEVNULL)
            procs.append(("pg", pgdata, pgenv))
            subprocess.run([f"{a.pg_bin}/createdb", "-h", "127.0.0.1", "-p", str(pg_port), "-U", "demo", "demo"], env=pgenv, check=True) if Path(f"{a.pg_bin}/createdb").exists() else \
                subprocess.run([a.python, "-c", f"import psycopg;c=psycopg.connect('host=127.0.0.1 port={pg_port} user=demo dbname=postgres');c.autocommit=True;c.execute('create database demo')"], check=True)
            env.update({"POSTGRES_HOST": "127.0.0.1", "POSTGRES_PORT": str(pg_port), "POSTGRES_PASSWORD": "x"})

        backend = REPO / "backend"
        mig = subprocess.run([a.python, "manage.py", "migrate", "--noinput"], cwd=backend, env=env, capture_output=True, text=True)
        if mig.returncode:
            print(json.dumps({"fatal": "migrate failed", "stderr": mig.stderr[-2000:]}))
            return
        # reset the sink: migrate doesn't send anything, but be safe
        gun = [bin_("gunicorn"), "demo.wsgi:application", "--bind", f"127.0.0.1:{dj_port}", "--worker-class", "gthread", "--workers", "1", "--threads", "8", "--timeout", "60"]
        cmd = ([bin_("opentelemetry-instrument")] + gun) if a.mode == "otlp" else gun
        dj = subprocess.Popen(cmd, cwd=backend, env=env, stdout=open(work / "django.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
        procs.append(dj)
        base = f"http://127.0.0.1:{dj_port}"
        if not wait_http(base + "/api/healthz"):
            print(json.dumps({"fatal": "django did not start", "log": (work / "django.log").read_text()[-3000:]}))
            return
        time.sleep(0.5)
        urllib.request.urlopen(f"http://127.0.0.1:{sink_port}/_mock/reset", data=b"")
        sent = {}
        for sid, method, path, body, fk in SCENARIOS:
            tid, parent = secrets.token_hex(16), secrets.token_hex(8)
            code, ms = send(base, method, path, body, fk, tid, parent)
            sent[sid] = {"trace_id": tid, "parent": parent, "status": code, "ms": ms, "path": path}
        time.sleep(4)  # BSP 300 ms + Sentry transport flush
        os.killpg(dj.pid, signal.SIGTERM)  # graceful: flush BSP + sentry
        dj.wait(timeout=20)
        time.sleep(0.5)

        items = read_jsonl(work / "sink/envelopes.jsonl")
        otlp = read_jsonl(work / "sink/otlp_traces.jsonl")
        report = analyse(a, sent, items, otlp)
        report["go_headers"] = GO_HEADERS
        report["workdir"] = str(work)
        sdk_ver = subprocess.run([a.python, "-c", "import sentry_sdk;print(sentry_sdk.VERSION)"], capture_output=True, text=True).stdout.strip()
        report["sentry_sdk"] = sdk_ver
        print(json.dumps(report, indent=1, default=str))
    finally:
        for p in procs:
            if isinstance(p, tuple):
                subprocess.run([f"{a.pg_bin}/pg_ctl", "-D", str(p[1]), "-m", "fast", "stop"], env=p[2], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    p.kill()


def analyse(a, sent, items, otlp):
    checks = []

    def check(name, ok, evidence=None):
        checks.append({"check": name, "result": "PASS" if ok else "FAIL", "evidence": evidence})

    spans = []
    for rec in otlp:
        for rs in rec["body"].get("resourceSpans", []):
            res = {x["key"]: next(iter(x["value"].values())) for x in rs.get("resource", {}).get("attributes", [])}
            for ss in rs.get("scopeSpans", []):
                for sp in ss.get("spans", []):
                    sp["_scope"] = ss.get("scope", {}).get("name")
                    sp["_service"] = res.get("service.name")
                    spans.append(sp)
    events = [i["payload"] for i in items if i["item_header"].get("type") == "event"]
    txns = [i["payload"] for i in items if i["item_header"].get("type") == "transaction"]
    logs = []
    for i in items:
        if i["item_header"].get("type") == "log":
            logs.extend(i["payload"].get("items", []))
    item_types = sorted({i["item_header"].get("type") for i in items})
    out = {"mode": a.mode, "db": a.db, "fix": a.fix, "item_types": item_types, "sent": sent,
           "counts": {"otlp_spans": len(spans), "events": len(events), "logs": len(logs), "transactions": len(txns)}}

    def server_span(tid):
        ss = [s for s in spans if s["traceId"] == tid and s.get("kind") in (2, "SPAN_KIND_SERVER")]
        return ss[0] if ss else None

    if a.mode == "otlp":
        check("no transaction items sent to Sentry (traces_sample_rate unset)", not txns, item_types)
        for sid, s in sent.items():
            sv = server_span(s["trace_id"])
            check(f"{sid}: Django SERVER span continues incoming traceparent", sv is not None and sv.get("parentSpanId") == s["parent"],
                  sv and {"name": sv["name"], "parent": sv.get("parentSpanId"), "status": sv.get("status")})
        # errors
        for sid, expect in (("S2", 1), ("S3", 1), ("S4", 1)):
            tid = sent[sid]["trace_id"]
            evs = [e for e in events if (e.get("contexts", {}).get("trace", {}).get("trace_id") == tid)]
            sv = server_span(tid)
            ok = len(evs) >= expect and sv is not None and all(e["contexts"]["trace"].get("span_id") == sv["spanId"] for e in evs)
            check(f"{sid}: Sentry error event(s) carry OTel trace_id + server span_id", ok,
                  {"events": [(e.get("exception", {}).get("values", [{}])[-1].get("type"), e["contexts"]["trace"]) for e in evs], "server_span": sv and sv["spanId"],
                   "demo_team": [e.get("tags", {}).get("demo_team") if isinstance(e.get("tags"), dict) else e.get("tags") for e in evs]})
            st = (sv or {}).get("status", {}).get("code")
            check(f"{sid}: server span status ERROR", st in (2, "STATUS_CODE_ERROR"), (sv or {}).get("status"))
        orphan = [e for e in events if e.get("contexts", {}).get("trace", {}).get("trace_id") not in {s["trace_id"] for s in sent.values()}]
        check("no error events with a foreign (non-OTel) trace_id", not orphan, [(e.get("message") or e.get("exception", {}).get("values", [{}])[-1].get("value"), e.get("contexts", {}).get("trace")) for e in orphan])
        # logs
        for sid in ("S2", "S3", "S4", "S7"):
            tid = sent[sid]["trace_id"]
            sv = server_span(tid)
            lname = lambda l: (l.get("attributes", {}).get("logger.name") or {}).get("value")
            # django.request logs after the OTel middleware span ended (documented limitation).
            late = [l for l in logs if l.get("trace_id") == tid and lname(l) in ("django.request", "django.server")]
            out.setdefault("late_django_request_logs", {})[sid] = [(l.get("body"), l.get("span_id"), lname(l)) for l in late]
            ls = [l for l in logs if l.get("trace_id") == tid and l not in late]
            check(f"{sid}: logs carry OTel trace_id", len(ls) >= 1, [l.get("body") for l in ls])
            check(f"{sid}: logs carry OTel server span_id", bool(ls) and sv is not None and all(l.get("span_id") == sv["spanId"] for l in ls),
                  {"log_span_ids": sorted({l.get("span_id") for l in ls}), "server_span": sv and sv["spanId"]})
        foreign_logs = [l for l in logs if l.get("trace_id") not in {s["trace_id"] for s in sent.values()}]
        out["foreign_logs"] = [(l.get("body"), l.get("trace_id"), (l.get("attributes", {}).get("logger.name") or {}).get("value")) for l in foreign_logs]
        # force keep
        for sid in ("S8", "S9"):
            sv = server_span(sent[sid]["trace_id"])
            check(f"{sid}: server span has demo.force_keep='true' (string)", sv is not None and attrs_of(sv).get("demo.force_keep") == "true", sv and attrs_of(sv).get("demo.force_keep"))
        for sid in ("S1a", "S1b", "S7"):
            sv = server_span(sent[sid]["trace_id"])
            check(f"{sid}: server span NOT error and no force_keep", sv is not None and (sv.get("status", {}).get("code") in (None, 0, 1, "STATUS_CODE_UNSET", "STATUS_CODE_OK")) and "demo.force_keep" not in attrs_of(sv), sv and sv.get("status"))
        # Go propagation
        tids_go = {h["traceparent"].split("-")[1] for h in GO_HEADERS if h.get("traceparent")}
        for sid in ("S1b", "S2", "S4", "S7"):
            check(f"{sid}: outgoing call to Go carries W3C traceparent with same trace id", sent[sid]["trace_id"] in tids_go)
        # N+1 shape
        out["n_plus_one"] = n1 = n_plus_one_shape([s for s in spans if s["traceId"] == sent["S8"]["trace_id"]])
        check("S8: N+1 shape (1 source + >=5 identical repeats, same parent, >=50ms, no non-db between)", n1.get("ok"), n1)
        out["slow_db"] = sd = slow_db_shape([s for s in spans if s["traceId"] == sent["S9"]["trace_id"]])
        check("S9: SELECT db span >= 1000 ms", sd.get("ok"), sd)
    else:
        by_trace = {t.get("contexts", {}).get("trace", {}).get("trace_id"): t for t in txns}
        for sid, s in sent.items():
            t = by_trace.get(s["trace_id"])
            check(f"{sid}: Sentry transaction continues incoming sentry-trace", t is not None and t["contexts"]["trace"].get("parent_span_id") == s["parent"], t and t.get("transaction"))
        t8 = by_trace.get(sent["S8"]["trace_id"]) or {}
        db8 = [sp for sp in t8.get("spans", []) if (sp.get("op") or "").startswith("db")]
        rep = [sp for sp in db8 if "pg_sleep(0.01)" in (sp.get("description") or "")]
        check("S8 (sentry mode): transaction has >=5 identical N+1 db spans", len(rep) >= 5 and len({sp["description"] for sp in rep}) == 1,
              {"db_spans": len(db8), "repeats": len(rep), "ops": sorted({sp.get("op") for sp in db8}), "desc": rep[0]["description"] if rep else None,
               "repeat_ms": round(sum(_dur(sp) * 1000 for sp in rep), 1)})
        t9 = by_trace.get(sent["S9"]["trace_id"]) or {}
        slow = [sp for sp in t9.get("spans", []) if "pg_sleep(1.2)" in (sp.get("description") or "")]
        check("S9 (sentry mode): slow db span >= 1000 ms", bool(slow) and _dur(slow[0]) >= 1.0,
              slow and {"op": slow[0].get("op"), "ms": round(_dur(slow[0]) * 1000)})
        check("no OTLP spans exported in sentry mode", not spans, len(spans))
        for sid in ("S2", "S3", "S4"):
            tid = sent[sid]["trace_id"]
            evs = [e for e in events if e.get("contexts", {}).get("trace", {}).get("trace_id") == tid]
            check(f"{sid} (sentry mode): error event on the incoming trace", bool(evs), len(evs))
    out["checks"] = checks
    out["summary"] = {"pass": sum(c["result"] == "PASS" for c in checks), "fail": sum(c["result"] == "FAIL" for c in checks)}
    return out


def _ts(x):
    if isinstance(x, (int, float)):
        return float(x)
    from datetime import datetime
    return datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()


def _dur(sp):
    return _ts(sp["timestamp"]) - _ts(sp["start_timestamp"])


def _ns(x):
    return int(x)


def n_plus_one_shape(trace_spans):
    db = [s for s in trace_spans if "db.system" in attrs_of(s) or "db.system.name" in attrs_of(s)]
    db.sort(key=lambda s: _ns(s["startTimeUnixNano"]))
    stmts = [attrs_of(s).get("db.statement") or attrs_of(s).get("db.query.text") for s in db]
    rep = [s for s, st in zip(db, stmts) if st and "pg_sleep(0.01)" in st]
    if not rep:
        return {"ok": False, "db_spans": len(db), "statements": stmts[:5], "all_span_names": [s["name"] for s in trace_spans]}
    first_rep_start = _ns(rep[0]["startTimeUnixNano"])
    src = [s for s in db if _ns(s["endTimeUnixNano"]) <= first_rep_start and "pg_sleep(0.01)" not in (attrs_of(s).get("db.statement") or "")]
    parents = {s.get("parentSpanId") for s in rep + src[-1:]}
    lo, hi = _ns(rep[0]["startTimeUnixNano"]), _ns(rep[-1]["endTimeUnixNano"])
    between = [s for s in trace_spans if s not in db and lo <= _ns(s["startTimeUnixNano"]) <= hi and s.get("parentSpanId") in parents]
    rep_ms = sum((_ns(s["endTimeUnixNano"]) - _ns(s["startTimeUnixNano"])) / 1e6 for s in rep)
    stmt = attrs_of(rep[0]).get("db.statement")
    res = {
        "db_spans": len(db), "repeats": len(rep), "source_spans_before": len(src),
        "identical_statements": len({attrs_of(s).get("db.statement") for s in rep}) == 1,
        "same_parent": len(parents) == 1, "parent_names": [s["name"] for s in trace_spans if s["spanId"] in parents],
        "repeat_total_ms": round(rep_ms, 1), "non_db_spans_between": [s["name"] for s in between],
        "span_name": rep[0]["name"], "db.system": attrs_of(rep[0]).get("db.system"), "statement": stmt,
        "statement_untruncated": stmt is not None and stmt.endswith("= %s"), "sqlcommenter": "/*" in (stmt or ""),
        "attr_keys": sorted(attrs_of(rep[0]).keys()), "scope": rep[0].get("_scope"),
    }
    res["ok"] = (res["repeats"] >= 5 and res["source_spans_before"] >= 1 and res["identical_statements"] and res["same_parent"]
                 and rep_ms >= 50 and not between and res["db.system"] and res["statement_untruncated"] and not res["sqlcommenter"])
    return res


def slow_db_shape(trace_spans):
    db = [s for s in trace_spans if "db.system" in attrs_of(s)]
    slow = [s for s in db if "pg_sleep(1.2)" in (attrs_of(s).get("db.statement") or "")]
    if not slow:
        return {"ok": False, "db_spans": [(s["name"], attrs_of(s).get("db.statement")) for s in db], "all": [s["name"] for s in trace_spans]}
    s = slow[0]
    ms = (_ns(s["endTimeUnixNano"]) - _ns(s["startTimeUnixNano"])) / 1e6
    st = attrs_of(s).get("db.statement")
    return {"ok": ms >= 1000 and st.strip().upper().startswith("SELECT"), "ms": round(ms), "name": s["name"], "statement": st,
            "db.system": attrs_of(s).get("db.system"), "attr_keys": sorted(attrs_of(s).keys())}


if __name__ == "__main__":
    main()
