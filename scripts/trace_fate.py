#!/usr/bin/env python3
"""Show what the collector did with each trace: sent to the trace store only, or also kept for Sentry.

Reads the demo overlay's file-exporter copies:
  out/collector/all.jsonl     everything sent to the existing trace store (Jaeger here)
  out/collector/sentry.jsonl  what tail sampling kept and sent to Sentry

Usage:
  python3 scripts/trace_fate.py                 # latest `make demo` run, one line per scenario
  python3 scripts/trace_fate.py --run mixed     # latest `make mixed` run (summary + first rows)
  python3 scripts/trace_fate.py <trace_id> ...  # specific traces
"""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

MARKER = "sentry.error_marker"


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
                        per_trace[sp.get("traceId", "").lower()].append(
                            (svc, sp.get("name", ""), (sp.get("status") or {}).get("code", 0)))
    return per_trace


def describe(tid, allj, sent):
    a, s = allj.get(tid, []), sent.get(tid, [])
    by_svc = Counter(svc for svc, _, _ in a)
    errors = sorted({f"{svc}:{'error-marker' if name == MARKER else name}" for svc, name, code in a
                     if code in (2, "STATUS_CODE_ERROR")})
    fate = "KEPT -> Sentry" if s else ("DROPPED (store only)" if a else "not seen by collector")
    return fate, len(a), len(s), dict(by_svc), errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trace_ids", nargs="*")
    ap.add_argument("--run", default="demo", choices=["demo", "mixed", "perf"])
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
        rep = json.loads((Path(a.out) / "loadgen" / f"{a.run}-latest.json").read_text())
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

    print(f"{'scenario':<9} {'trace id':<32}  {'outcome':<22} {'store':>5} {'sentry':>6}  services in trace / error spans")
    for scen, tid in rows:
        fate, n_all, n_sent, svcs, errs = describe(tid, allj, sent)
        svc_s = " ".join(f"{k}={v}" for k, v in sorted(svcs.items()))
        print(f"{scen:<9} {tid:<32}  {fate:<22} {n_all:>5} {n_sent:>6}  {svc_s}" + (f"  | errors: {', '.join(errs)}" if errs else ""))


if __name__ == "__main__":
    main()
