#!/usr/bin/env python3
"""Show what the collector did with each trace: sent to the trace store only, or also kept for Sentry.

Reads the demo overlay's file-exporter copies:
  out/collector/all.jsonl     everything sent to the existing trace store (Jaeger here)
  out/collector/sentry.jsonl  what tail sampling kept and sent to Sentry

Usage:
  python3 scripts/trace_fate.py                 # latest `make demo` run, one line per scenario
  python3 scripts/trace_fate.py --run mixed     # latest `make mixed` run (summary + first rows)
  python3 scripts/trace_fate.py --run scenario  # latest `make scenario S=..` run (e.g. S12)
  python3 scripts/trace_fate.py <trace_id> ...  # specific traces
"""
import argparse
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

MARKER = "sentry.error_marker"


def _decision_wait_s():
    """Collector `decision_wait` (DECISION_WAIT env, default 30s) + a little slack for export."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m)?\s*", os.environ.get("DECISION_WAIT", "30s"))
    if not m:
        return 35.0
    val = float(m.group(1)) * {"ms": 0.001, "s": 1, "m": 60, None: 1}[m.group(2)]
    return val + 5


def load(path):
    per_trace = defaultdict(list)
    if not path.exists():
        return per_trace
    with open(path) as f:
        for line in f:
            try:
                body = json.loads(line)
            except json.JSONDecodeError:
                continue
            for rs in body.get("resourceSpans", []):
                svc = next((a["value"].get("stringValue") for a in rs.get("resource", {}).get("attributes", [])
                            if a.get("key") == "service.name"), "?")
                for ss in rs.get("scopeSpans", []):
                    for sp in ss.get("spans", []):
                        browser = any(at.get("key") == "sentry.browser_trace" for at in sp.get("attributes", []))
                        per_trace[sp.get("traceId", "").lower()].append(
                            (svc, sp.get("name", ""), (sp.get("status") or {}).get("code", 0),
                             int(sp.get("startTimeUnixNano") or 0) / 1e9, browser))
    return per_trace


def rep_mode_sentry(a, rows, allj):
    """True if the run claims FE_SPANS=sentry but browser spans showed up in the collector."""
    if a.trace_ids:
        return False
    try:
        rep = json.loads((Path(a.out) / "loadgen" / f"{a.run}-latest.json").read_text())
    except (OSError, ValueError):
        return False
    return rep.get("fe_spans") == "sentry" and any(
        svc == "demo-frontend" for _, tid in rows for svc, *_ in allj.get(tid, []))


def describe(tid, allj, sent):
    a, s = allj.get(tid, []), sent.get(tid, [])
    by_svc = Counter(svc for svc, *_ in a)
    errors = sorted({f"{svc}:{'error-marker' if name == MARKER else name}" for svc, name, code, *_ in a
                     if code in (2, "STATUS_CODE_ERROR")})
    if s:
        # No error / slow span, but tagged by KeepBrowserTracesMiddleware -> browser-traces policy.
        fate = "KEPT (browser trace)" if not errors and any(sp[4] for sp in a) else "KEPT -> Sentry"
    elif not a:
        fate = "not seen by collector"
    elif time.time() - min(sp[3] for sp in a) < _decision_wait_s():
        # The tail sampler hasn't decided yet (decision_wait after the trace's first span).
        fate = "PENDING (re-run soon)"
    else:
        fate = "DROPPED (store only)"
    return fate, len(a), len(s), dict(by_svc), errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trace_ids", nargs="*")
    ap.add_argument("--run", default="demo", choices=["demo", "mixed", "perf", "scenario"])
    ap.add_argument("--out", default="out")
    ap.add_argument("--rows", type=int, default=15)
    a = ap.parse_args()

    col = Path(a.out) / "collector"
    allj, sent = load(col / "all.jsonl"), load(col / "sentry.jsonl")
    if not allj:
        raise SystemExit("no out/collector/all.jsonl - run with the demo overlay (COLLECTOR_OVERLAY=demo.yaml, the default)")

    rows = []
    if a.trace_ids:
        rows = [("-", t.lower().replace("-", "")) for t in a.trace_ids]
    else:
        report = Path(a.out) / "loadgen" / f"{a.run}-latest.json"
        if not report.exists():
            make = {"demo": "make demo", "mixed": "make mixed", "perf": "make perf",
                    "scenario": "make scenario S=S12"}[a.run]
            found = sorted(x.name.replace("-latest.json", "") for x in report.parent.glob("*-latest.json"))
            hint = f" Reports present: {', '.join(found)} (use RUN=<name>)." if found else ""
            raise SystemExit(f"no {report} - run `{make}` first, or pass trace ids.{hint}")
        rep = json.loads(report.read_text())
        fe_mode = rep.get("fe_spans", "otlp (not recorded)")
        print(f"frontend FE_SPANS={fe_mode} (read from the running frontend at run time)")
        if fe_mode == "sentry":
            print("  browser spans went straight to Sentry (100%), not via the collector: the outcome\n"
                  "  below is for the BACKEND spans only; the FE spans are in Sentry either way.")
        print()
        runs = [r for ph in rep["phases"].values() for r in ph if r and r.get("traceId")]
        rows = [(r["scenario"], r["traceId"].lower()) for r in runs]

    if a.run == "mixed" and not a.trace_ids:
        tally = defaultdict(Counter)
        for scen, tid in rows:
            tally[scen][describe(tid, allj, sent)[0]] += 1
        print(f"{'scenario':<9} {'traces':>6}  outcome")
        for scen in sorted(tally):
            print(f"{scen:<9} {sum(tally[scen].values()):>6}  " + ", ".join(f"{k}: {v}" for k, v in tally[scen].items()))
        print()
        rows = rows[: a.rows]

    if rep_mode_sentry(a, rows, allj):
        print("!! FE_SPANS=sentry recorded, but demo-frontend spans reached the collector: the frontend\n"
              "!! was rebuilt in otlp mode before/while this ran. Run `make fe-mode FE=sentry` and re-run.\n")
    print(f"{'scenario':<9} {'trace id':<32}  {'outcome':<22} {'store':>5} {'sentry':>6}  services in trace / error spans")
    for scen, tid in rows:
        fate, n_all, n_sent, svcs, errs = describe(tid, allj, sent)
        svc_s = " ".join(f"{k}={v}" for k, v in sorted(svcs.items()))
        print(f"{scen:<9} {tid:<32}  {fate:<22} {n_all:>5} {n_sent:>6}  {svc_s}" + (f"  | errors: {', '.join(errs)}" if errs else ""))


if __name__ == "__main__":
    main()
