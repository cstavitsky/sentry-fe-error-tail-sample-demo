#!/usr/bin/env python3
"""
verify_sentry.py - checks the acceptance criteria that need a real Sentry org.

Run on the machine that ran the demo, from the repo root, AFTER one or more of:

    make demo                     -> out/loadgen/demo-latest.json     (AC1-sentry, AC2-AC5, AC7, AC8, AC11)
    make perf MODE=otlp           -> out/loadgen/perf-*.json           (AC6, otlp mode)
    make perf MODE=sentry         -> out/loadgen/perf-*.json           (AC6, control)
    make mixed                    -> out/loadgen/mixed-latest.json    (AC9, AC10)

    python3 scripts/verify_sentry.py                 # waits for decision_wait + ingestion, then checks
    python3 scripts/verify_sentry.py --no-wait       # check right away (results may be INCONCLUSIVE)
    python3 scripts/verify_sentry.py --only AC2,AC6 --json out/verify.json

Needs: Python 3.8+, `requests`. Reads .env:
    SENTRY_AUTH_TOKEN (scopes: org:read, project:read, event:read)   SENTRY_ORG
    SENTRY_PROJECT_FE  SENTRY_PROJECT_BE  DECISION_WAIT (default 30s)
    optional: SENTRY_API_URL (default: the org's region URL, discovered via /organizations/{org}/)

Only GET requests are made. Nothing in Sentry is changed.

Local inputs: out/loadgen/*.json (loadgen) and out/collector/{all,sentry}.jsonl. The
collector files are written only by the demo overlay (collector/demo.yaml, selected by
COLLECTOR_OVERLAY=demo.yaml, the default). Without them AC10 is INCONCLUSIVE and AC3/AC4/
AC7/AC8 skip their local cross-checks.

API endpoints used (checked against docs.sentry.io/api on 2026-10-07 unless marked):
    GET /api/0/organizations/{org}/                                 Retrieve an Organization (links.regionUrl)
    GET /api/0/projects/{org}/{project}/                            Retrieve a Project
    GET /api/0/organizations/{org}/events/?dataset=spans|errors|logs   Query Explore Events in Table Format
    GET /api/0/organizations/{org}/trace/{trace_id}/                Retrieve a Trace
    GET /api/0/organizations/{org}/trace-meta/{trace_id}/           Retrieve Trace Metadata
    GET /api/0/organizations/{org}/issues/                          List an Organization's Issues
    GET /api/0/organizations/{org}/issues/{id}/events/              List an Issue's Events
    GET /api/0/organizations/{org}/replays/{replay_id}/             Retrieve a Replay Instance
    GET /api/0/projects/{org}/{project}/performance-issues/configure/   UNCONFIRMED (internal, undocumented)
Field names that are NOT in the public docs and are therefore tried with fallbacks
(the one that worked is printed): log span id, error replay id, error span id.
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("pip install requests")

PASS, FAIL, INCONC = "PASS", "FAIL", "INCONCLUSIVE"
MARKER = "sentry.error_marker"
N1_TYPE = "performance_n_plus_one_db_queries"
SLOW_DB_TYPE = "performance_slow_db_query"


# ----------------------------------------------------------------------------- utils
def load_env(path):
    env = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in list(env):  # real environment wins
        if os.environ.get(k):
            env[k] = os.environ[k]
    for k in ("SENTRY_AUTH_TOKEN", "SENTRY_ORG", "SENTRY_PROJECT_FE", "SENTRY_PROJECT_BE", "SENTRY_API_URL", "DECISION_WAIT"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def parse_duration(s, default=30.0):
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$", str(s or ""))
    if not m:
        return default
    n, unit = float(m.group(1)), m.group(2) or "s"
    return n / 1000 if unit == "ms" else n * {"s": 1, "m": 60, "h": 3600}[unit]


def parse_ts(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s, tz=timezone.utc)
    s = str(s).strip().replace("Z", "+00:00")
    # Python < 3.11 fromisoformat: at most 6 fractional digits.
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def iso(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def chunks(xs, n):
    xs = list(xs)
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def norm_id(x):
    return str(x or "").replace("-", "").lower()


# ----------------------------------------------------------------------------- API
class Sentry:
    def __init__(self, token, org, api_url=None, verbose=False):
        self.org = org
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.verbose = verbose
        self.base = (api_url or "https://sentry.io").rstrip("/")
        if not api_url:
            try:
                info = self.get(f"/api/0/organizations/{org}/")
                region = (info.get("links") or {}).get("regionUrl")
                if region:
                    self.base = region.rstrip("/")
            except Exception as exc:  # keep the default
                print(f"warning: could not discover region URL ({exc}); using {self.base}", file=sys.stderr)
        self.ui = f"https://{org}.sentry.io"

    def get(self, path, params=None, retries=5):
        url = path if path.startswith("http") else self.base + path
        delay = 2
        for attempt in range(retries):
            r = self.s.get(url, params=params, timeout=60)
            if self.verbose:
                print(f"  GET {r.url} -> {r.status_code}", file=sys.stderr)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(float(r.headers.get("Retry-After") or delay))
                delay *= 2
                continue
            if r.status_code >= 400:
                raise ApiError(r.status_code, r.url, r.text[:300])
            return r.json()

    def events(self, dataset, fields, query, start, end, project=None, sort=None, per_page=100):
        params = [("dataset", dataset), ("query", query), ("start", iso(start)), ("end", iso(end)), ("per_page", per_page)]
        params += [("field", f) for f in fields]
        if project:
            params += [("project", p) for p in (project if isinstance(project, list) else [project])]
        if sort:
            params.append(("sort", sort))
        return self.get(f"/api/0/organizations/{self.org}/events/", params).get("data", [])

    def events_try_fields(self, dataset, base_fields, candidates, query, start, end, **kw):
        """Try optional (undocumented) field names one by one; return (rows, field_used)."""
        last = None
        for c in candidates:
            try:
                return self.events(dataset, base_fields + [c], query, start, end, **kw), c
            except ApiError as exc:
                last = exc
        try:
            return self.events(dataset, base_fields, query, start, end, **kw), None
        except ApiError as exc:
            raise last or exc


class ApiError(Exception):
    def __init__(self, status, url, body):
        super().__init__(f"HTTP {status} {url}: {body}")
        self.status = status


def poll(fn, ok, timeout, interval=15, label=""):
    """Call fn() until ok(result) or timeout. Returns the last result."""
    end = time.time() + timeout
    res = fn()
    while not ok(res) and time.time() < end:
        print(f"  waiting for {label} ...", file=sys.stderr)
        time.sleep(interval)
        res = fn()
    return res


# ----------------------------------------------------------------------------- local data
def load_runs(out_dir):
    """Return {'demo': report, 'mixed': report, 'perf': [reports]} from out/loadgen."""
    d = Path(out_dir) / "loadgen"
    res = {"demo": None, "mixed": None, "perf": []}
    for kind in ("demo", "mixed"):
        p = d / f"{kind}-latest.json"
        if p.exists():
            res[kind] = json.loads(p.read_text())
            res[kind]["_file"] = str(p)
    for p in sorted(d.glob("perf-2*.json")):
        r = json.loads(p.read_text())
        r["_file"] = str(p)
        res["perf"].append(r)
    return res


def runs_of(report):
    return [r for rs in (report or {}).get("phases", {}).values() for r in rs if r and r.get("traceId") and "error" not in r]


def window(report, before=10, after=30):
    s, f = parse_ts(report["started"]), parse_ts(report["finished"])
    return s - timedelta(minutes=before), f + timedelta(minutes=after)


def load_collector(out_dir):
    """Spans per trace in out/collector/{all,sentry}.jsonl (file exporter, OTLP/JSON per line)."""
    res = {}
    for name in ("all", "sentry"):
        p = Path(out_dir) / "collector" / f"{name}.jsonl"
        per_trace = defaultdict(list)
        if p.exists():
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        body = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for rs in body.get("resourceSpans", []):
                        svc = next((a["value"].get("stringValue") for a in rs.get("resource", {}).get("attributes", []) if a.get("key") == "service.name"), "?")
                        for ss in rs.get("scopeSpans", []):
                            for sp in ss.get("spans", []):
                                sp["_service"] = svc
                                per_trace[norm_id(sp.get("traceId"))].append(sp)
        res[name] = per_trace if p.exists() else None
    return res


# ----------------------------------------------------------------------------- checker
class Checker:
    @property
    def col(self):
        """Collector file-exporter output, re-read whenever the files change.

        Loaded lazily (not at startup) because the collector only writes sentry.jsonl after
        decision_wait, i.e. after the script has started waiting for the latest run.
        """
        d = Path(self.args.out) / "collector"
        mtime = tuple((d / f"{n}.jsonl").stat().st_mtime if (d / f"{n}.jsonl").exists() else None for n in ("all", "sentry"))
        if self._col is None or mtime != self._col_mtime:
            self._col, self._col_mtime = load_collector(self.args.out), mtime
        return self._col

    def __init__(self, args, env):
        self.args = args
        self.env = env
        self.org = env["SENTRY_ORG"]
        self.fe = env.get("SENTRY_PROJECT_FE", "tail-sample-demo-fe")
        self.be = env.get("SENTRY_PROJECT_BE", "tail-sample-demo-be")
        self.api = Sentry(env["SENTRY_AUTH_TOKEN"], self.org, env.get("SENTRY_API_URL"), args.verbose)
        self.runs = load_runs(args.out)
        self._col, self._col_mtime = None, None
        self.results = []
        self.decision_wait = parse_duration(env.get("DECISION_WAIT", "30s"))
        self.project_ids = {}
        for slug in (self.fe, self.be):
            try:
                self.project_ids[slug] = str(self.api.get(f"/api/0/projects/{self.org}/{slug}/")["id"])
            except Exception as exc:
                print(f"warning: project {slug}: {exc}", file=sys.stderr)

    # -- helpers ----------------------------------------------------------------
    def add(self, ac, status, summary, links=(), details=None):
        self.results.append({"ac": ac, "status": status, "summary": summary, "links": list(links), "details": details})

    def trace_url(self, tid):
        return f"{self.api.ui}/explore/traces/trace/{tid}/"

    def by_scenario(self, report):
        out = defaultdict(list)
        for r in runs_of(report):
            out[r["scenario"]].append(r)
        return out

    def span_counts(self, trace_ids, start, end):
        """{trace_id: Counter(project_slug -> span count)} from the spans dataset."""
        res = defaultdict(Counter)
        for ch in chunks(trace_ids, 40):
            rows = self.api.events("spans", ["trace", "project", "count()"], f"trace:[{','.join(ch)}]", start, end, per_page=100)
            for row in rows:
                res[norm_id(row.get("trace"))][row.get("project")] += int(row.get("count()") or 0)
        return res

    def errors_for(self, trace_ids, start, end):
        res = defaultdict(list)
        field_used = None
        for ch in chunks(trace_ids, 40):
            rows, field_used = self.api.events_try_fields(
                "errors", ["id", "trace", "project", "title", "issue", "timestamp"], ["trace.span"],
                f"trace:[{','.join(ch)}]", start, end, per_page=100)
            for row in rows:
                res[norm_id(row.get("trace"))].append(row)
        return res, field_used

    def trace_tree(self, tid, start, end):
        return self.api.get(f"/api/0/organizations/{self.org}/trace/{tid}/", {"start": iso(start), "end": iso(end)})

    @staticmethod
    def flatten(nodes, depth=0, out=None):
        out = [] if out is None else out
        for n in nodes or []:
            if not isinstance(n, dict):
                continue
            n["_depth"] = depth
            out.append(n)
            Checker.flatten(n.get("children"), depth + 1, out)
        return out

    @staticmethod
    def label(n):
        return " ".join(str(n.get(k) or "") for k in ("op", "description", "transaction", "name"))

    def wait_ready(self, report):
        if self.args.no_wait or not report:
            return
        ready = parse_ts(report["finished"]) + timedelta(seconds=self.decision_wait + self.args.ingest_delay)
        delta = (ready - datetime.now(timezone.utc)).total_seconds()
        if delta > 0:
            print(f"waiting {int(delta)} s for decision_wait ({int(self.decision_wait)} s) + ingestion ({self.args.ingest_delay} s) after {Path(report['_file']).name} ...", file=sys.stderr)
            time.sleep(delta)

    # -- AC1 (Sentry part) ----------------------------------------------------------
    def ac1(self):
        rep = self.runs["demo"]
        if not rep:
            return self.add("AC1", INCONC, "no out/loadgen/demo-latest.json (run `make demo`)")
        sc = self.by_scenario(rep)
        s5 = [r["traceId"] for r in sc.get("S5", [])]
        start, end = window(rep)
        if not s5:
            return self.add("AC1", INCONC, "no S5 run in demo-latest.json")
        errs, _ = poll(lambda: self.errors_for(s5, start, end), lambda r: all(any(e.get("project") == self.fe for e in r[0].get(t, [])) for t in s5),
                       self.args.timeout, label="S5 FE error")
        logs = []
        try:
            logs = self.api.events("logs", ["trace", "message", "project"], f"trace:[{','.join(s5)}]", start, end)
        except ApiError as exc:
            logs = [{"error": str(exc)}]
        fe_err = all(any(e.get("project") == self.fe for e in errs.get(t, [])) for t in s5)
        fe_log = any(l.get("project") == self.fe for l in logs)
        status = PASS if fe_err and fe_log else FAIL
        self.add("AC1", status, f"non-span items reached Sentry: FE error on S5 trace={fe_err}, FE log on S5 trace={fe_log} "
                 "(OTLP validity / ids / chunking are covered locally by tests/local/transport_strict.mjs)", [self.trace_url(t) for t in s5[:2]])

    # -- AC2 -------------------------------------------------------------------------
    def ac2(self):
        rep = self.runs["demo"]
        if not rep:
            return self.add("AC2", INCONC, "no demo-latest.json")
        s2 = [r["traceId"] for r in self.by_scenario(rep).get("S2", [])]
        if not s2:
            return self.add("AC2", INCONC, "no S2 run")
        start, end = window(rep)
        tid = s2[0]

        def fetch():
            try:
                return self.trace_tree(tid, start, end)
            except ApiError as exc:
                return {"_error": str(exc)}

        def classify(tree):
            nodes = self.flatten(tree if isinstance(tree, list) else [])
            fe = [n for n in nodes if n.get("project_slug") == self.fe]
            be = [n for n in nodes if n.get("project_slug") == self.be]
            dj = [n for n in be if re.search(r"graphql", self.label(n), re.I)]
            go = [n for n in be if re.search(r"/replica/status|replica\.pool\.lookup", self.label(n))]
            roots = [n for n in nodes if n["_depth"] == 0]
            errors = [e for n in nodes for e in (n.get("errors") or [])]
            return nodes, fe, dj, go, roots, errors

        tree = poll(fetch, lambda t: isinstance(t, list) and all(classify(t)[i] for i in (1, 2, 3)), self.args.timeout, label="S2 trace (FE+Django+Go)")
        if isinstance(tree, dict) and tree.get("_error"):
            return self.add("AC2", INCONC, tree["_error"], [self.trace_url(tid)])
        nodes, fe, dj, go, roots, errors = classify(tree)
        # Parent/child: one root, and the Django server span hangs below the FE span, Go below Django.
        def ancestors_ok(child_list, parent_list):
            ids = {n.get("event_id") for n in parent_list}
            def walk(nlist, inside):
                for n in nlist or []:
                    now_inside = inside or n.get("event_id") in ids
                    if n in child_list and now_inside and n.get("event_id") not in ids:
                        return True
                    if walk(n.get("children"), now_inside):
                        return True
                return False
            return walk(tree, False)
        linked = len(roots) == 1 and ancestors_ok(dj, fe) and ancestors_ok(go, dj)
        ok = bool(fe and dj and go) and linked
        self.add("AC2", PASS if ok else FAIL,
                 f"trace {tid}: {len(nodes)} nodes, FE={len(fe)} Django={len(dj)} Go={len(go)}, top-level roots={len(roots)}, "
                 f"FE->Django->Go nesting={'ok' if linked else 'BROKEN'}, errors on trace={len(errors)}",
                 [self.trace_url(tid)], {"roots": [self.label(r) for r in roots][:10]})

    # -- AC3 -------------------------------------------------------------------------
    def ac3(self):
        rep = self.runs["demo"]
        if not rep:
            return self.add("AC3", INCONC, "no demo-latest.json")
        s1 = [r["traceId"] for r in self.by_scenario(rep).get("S1", [])]
        if self.runs["mixed"]:
            s1 += [r["traceId"] for r in self.by_scenario(self.runs["mixed"]).get("S1", [])][:200]
        if not s1:
            return self.add("AC3", INCONC, "no S1 runs")
        start, end = window(rep)
        if self.runs["mixed"]:
            ms, me = window(self.runs["mixed"])
            start, end = min(start, ms), max(end, me)
        counts = self.span_counts(s1, start, end)
        in_sentry = [t for t in s1 if sum(counts.get(norm_id(t), {}).values())]
        allj = self.col["all"]
        jaeger_missing = None if allj is None else [t for t in s1 if not allj.get(norm_id(t))]
        jaeger_api = self.jaeger_has(s1[:3])
        ok = not in_sentry and (jaeger_missing == [] or jaeger_api is True)
        status = PASS if ok else (FAIL if in_sentry or jaeger_missing else INCONC)
        self.add("AC3", status,
                 f"{len(s1)} S1 traces: in Sentry={len(in_sentry)}; missing from out/collector/all.jsonl (Jaeger copy)="
                 f"{'n/a' if jaeger_missing is None else len(jaeger_missing)}; Jaeger API check={jaeger_api}",
                 [self.trace_url(t) for t in in_sentry[:3]] + [f"http://localhost:16686/trace/{s1[0]}"])

    def jaeger_has(self, tids):
        try:
            for t in tids:
                r = requests.get(f"{self.args.jaeger}/api/traces/{t}", timeout=5)
                if r.status_code != 200 or not r.json().get("data"):
                    return False
            return True
        except Exception:
            return None

    # -- AC4 / AC5 ----------------------------------------------------------------------
    def ac4(self):
        rep = self.runs["demo"]
        if not rep:
            return self.add("AC4", INCONC, "no demo-latest.json")
        sc = self.by_scenario(rep)
        start, end = window(rep)
        want = {s: [r["traceId"] for r in sc.get(s, [])] for s in ("S2", "S3", "S4", "S5", "S6", "S12")}
        # Expected error events per trace, as recorded by the loadgen (S4: Django + Go = 2 BE errors).
        exp_err = {norm_id(r["traceId"]): (r.get("errors") or {}) for s in want for r in sc.get(s, [])}
        tids = [t for ts in want.values() for t in ts]
        if not tids:
            return self.add("AC4", INCONC, "no S2-S6/S12 runs")
        sent = self.col["sentry"]
        allj = self.col["all"]

        def expected(t):
            if sent is None:
                return None
            return len([s for s in sent.get(norm_id(t), []) if s.get("name") != MARKER])

        def done(counts):
            return all(sum(counts.get(norm_id(t), {}).values()) >= (expected(t) or 1) for t in tids)

        counts = poll(lambda: self.span_counts(tids, start, end), done, self.args.timeout, label="S2-S6 spans")
        errs, _ = poll(lambda: self.errors_for(tids, start, end),
                       lambda r: all(sum(1 for e in r[0].get(norm_id(t), []) if e.get("project") == self.be) >= exp_err.get(norm_id(t), {}).get("be", 0) and
                                     sum(1 for e in r[0].get(norm_id(t), []) if e.get("project") == self.fe) >= exp_err.get(norm_id(t), {}).get("fe", 0) for t in tids),
                       self.args.timeout, label="S2-S6 errors on their traces")
        rows, bad = [], []
        for s, ts in want.items():
            for t in ts:
                c = counts.get(norm_id(t), Counter())
                n, exp = sum(c.values()), expected(t)
                all_n = None if allj is None else len([x for x in allj.get(norm_id(t), []) if x.get("name") != MARKER])
                fe_n, be_n = c.get(self.fe, 0), c.get(self.be, 0)
                problems = []
                if n == 0:
                    problems.append("not in Sentry")
                if exp is not None and n < exp:
                    problems.append(f"{n}/{exp} exported spans arrived")
                if all_n is not None and exp is not None and exp < all_n:
                    problems.append(f"collector exported {exp} of {all_n} spans (incomplete trace)")
                if s in ("S5", "S6") and be_n == 0:
                    problems.append("no BE spans")
                if s == "S4" and be_n == 0:
                    problems.append("no BE spans")
                if s == "S12" and (be_n == 0 or fe_n > 0):
                    problems.append(f"expected a backend-only trace (FE={fe_n}, BE={be_n})")
                # Errors must be ON this trace (catches e.g. Go errors landing on a random trace).
                te = errs.get(norm_id(t), [])
                ee = exp_err.get(norm_id(t), {})
                fe_e = sum(1 for e in te if e.get("project") == self.fe)
                be_e = sum(1 for e in te if e.get("project") == self.be)
                if fe_e < ee.get("fe", 0) or be_e < ee.get("be", 0):
                    problems.append(f"errors on trace FE {fe_e}/{ee.get('fe', 0)} BE {be_e}/{ee.get('be', 0)}"
                                    + (" (S4/S12 need Django + Go)" if s in ("S4", "S12") else ""))
                rows.append(f"{s} {t[:8]}: spans FE={fe_n} BE={be_n} expected={exp}/{all_n}, errors FE={fe_e} BE={be_e}" + (f" !! {', '.join(problems)}" if problems else ""))
                if problems:
                    bad.append(t)
        self.add("AC4", PASS if not bad else FAIL, f"{len(tids) - len(bad)}/{len(tids)} S2-S6 + S12 traces complete in Sentry; " + "; ".join(rows),
                 [self.trace_url(t) for t in (bad or tids)[:5]])

    def ac5(self):
        rep = self.runs["demo"]
        if not rep:
            return self.add("AC5", INCONC, "no demo-latest.json")
        s7 = [r["traceId"] for r in self.by_scenario(rep).get("S7", [])]
        if not s7:
            return self.add("AC5", INCONC, "no S7 run")
        start, end = window(rep)
        counts = poll(lambda: self.span_counts(s7, start, end), lambda c: all(sum(c.get(norm_id(t), {}).values()) for t in s7),
                      self.args.timeout, label="S7 spans")
        errs, _ = self.errors_for(s7, start, end)
        present = all(sum(counts.get(norm_id(t), {}).values()) for t in s7)
        n_err = sum(len(errs.get(norm_id(t), [])) for t in s7)
        self.add("AC5", PASS if present and n_err == 0 else FAIL, f"S7 present={present} ({dict(counts.get(norm_id(s7[0]), {}))}), error events on S7 traces={n_err}",
                 [self.trace_url(t) for t in s7[:2]])

    # -- AC6 -----------------------------------------------------------------------------
    def ac6(self):
        perf = self.runs["perf"]
        try:
            cfg = self.api.get(f"/api/0/projects/{self.org}/{self.be}/performance-issues/configure/")
            cfg_s = {k: v for k, v in cfg.items() if re.search(r"n_plus_one_db|slow_db", k)}
        except Exception as exc:
            cfg_s = f"unavailable ({exc})"
        if not perf:
            return self.add("AC6", INCONC, f"no out/loadgen/perf-*.json (run `make perf MODE=otlp` and `make perf MODE=sentry`); BE detector settings: {cfg_s}")
        # Filter by issue type, not `issue.category:performance`: Sentry now files N+1 / Slow DB
        # under the `db_query` category, so the category query silently returns nothing.
        issues = self.api.get(f"/api/0/organizations/{self.org}/issues/",
                              [("project", self.project_ids.get(self.be, "-1")),
                               ("query", f"issue.type:[{N1_TYPE},{SLOW_DB_TYPE}]"),
                               ("statsPeriod", "14d"), ("sort", "new"), ("limit", 100)])
        perf_issues = [i for i in issues if i.get("issueType") in (N1_TYPE, SLOW_DB_TYPE)]
        by_mode = {}
        for rep in perf:
            mode = rep.get("be_tracing_mode", "?")
            by_mode.setdefault(mode, []).append(rep)
        lines, statuses, links = [], [], []
        for mode, reps in sorted(by_mode.items()):
            rep = reps[-1]  # latest run per mode
            self.wait_ready(rep)
            start, end = window(rep, before=1, after=20)
            # Both modes share the Slow DB issue (same query fingerprint), and issue_events() only
            # samples the newest events in the window. Stop the window where the next perf run
            # starts, so a later run's events can't crowd this run's events out.
            later = [parse_ts(r["started"]) for r in perf if parse_ts(r["started"]) > parse_ts(rep["started"])]
            if later:
                end = min(end, min(later))
            sc = self.by_scenario(rep)
            for scen, itype, label in (("S8", N1_TYPE, "N+1 DB"), ("S9", SLOW_DB_TYPE, "Slow DB")):
                runs = sorted(sc.get(scen, []), key=lambda r: r.get("startedAt") or "")
                tids = {norm_id(r["traceId"]) for r in runs}
                diag = self.db_span_diag([r["traceId"] for r in runs[:20]], start, end)
                found = None
                for iss in [i for i in perf_issues if i.get("issueType") == itype]:
                    evs = self.issue_events(iss["id"], start, end)
                    hit = [e for e in evs if e["trace"] in tids]
                    no_trace_info = evs and not any(e["trace"] for e in evs)
                    fs = parse_ts(iss.get("firstSeen"))
                    # Attribute the issue to this mode by trace id; fall back to timing only when
                    # the issue events carry no trace id at all.
                    if hit or (no_trace_info and fs and start <= fs <= end):
                        found = (iss, evs, hit)
                        break
                if not found:
                    lines.append(f"[{mode}] {label}: NOT detected ({len(runs)} runs). DB spans in Sentry: {diag}")
                    statuses.append((mode, label, False))
                    continue
                iss, evs, hit = found
                first = parse_ts(iss.get("firstSeen"))
                created_here = first is not None and parse_ts(rep["started"]) - timedelta(minutes=1) <= first <= end
                n_before = sum(1 for r in runs if parse_ts(r.get("startedAt")) and parse_ts(r["startedAt"]) <= first) if created_here else None
                ttc = int((first - parse_ts(runs[0]["startedAt"])).total_seconds()) if created_here and runs else None
                lines.append(f"[{mode}] {label}: DETECTED {iss.get('shortId')} ({iss.get('issueType')}); "
                             + (f"created by this run at occurrence ~#{n_before} of {len(runs)}, {ttc}s after first request; " if created_here
                                else f"issue pre-existed (firstSeen {iss.get('firstSeen')}); ")
                             + f"{len(hit)}/{len(evs)} issue events in window are from this run's traces; issue count={iss.get('count')}")
                links.append(iss.get("permalink") or f"{self.api.ui}/issues/{iss['id']}/")
                statuses.append((mode, label, True))
        control = {lab: ok for m, lab, ok in statuses if m == "sentry"}
        otlp = {lab: ok for m, lab, ok in statuses if m == "otlp"}
        if not otlp:
            status = INCONC
            lines.append("no otlp-mode perf run found")
        elif any(not ok for ok in otlp.values()) and (not control or not all(control.values())):
            status = INCONC
            lines.append("otlp negative is inconclusive: the sentry-mode control did not detect both issue types (detector settings / thresholds / 5-per-hour cap)")
        else:
            status = PASS  # AC6 = "record whether"; recorded with a valid control
        self.add("AC6", status, f"BE detector settings: {cfg_s}. " + " | ".join(lines), links)

    def db_span_diag(self, tids, start, end):
        if not tids:
            return "n/a"
        try:
            rows = self.api.events("spans", ["span.op", "span.description", "count()", "avg(span.duration)"],
                                   f"trace:[{','.join(tids)}] project:{self.be}", start, end, sort="-count()", per_page=10)
        except ApiError as exc:
            return f"query failed ({exc.status})"
        return "; ".join(f"op={r.get('span.op')!r} n={r.get('count()')} avg={round(r.get('avg(span.duration)') or 0)}ms desc={str(r.get('span.description'))[:70]!r}" for r in rows[:5])

    def issue_events(self, issue_id, start, end):
        out = []
        try:
            evs = self.api.get(f"/api/0/organizations/{self.org}/issues/{issue_id}/events/", {"start": iso(start), "end": iso(end), "full": "true", "per_page": 10})
        except ApiError:
            return out
        for e in evs:
            tr = ((e.get("contexts") or {}).get("trace") or {}).get("trace_id")
            if not tr:
                tr = next((t.get("value") for t in e.get("tags", []) if t.get("key") == "trace"), None)
            out.append({"id": e.get("eventID") or e.get("id"), "trace": norm_id(tr), "date": e.get("dateCreated")})
        return out

    # -- AC7 -----------------------------------------------------------------------------
    def ac7(self):
        rep = self.runs["demo"]
        if not rep:
            return self.add("AC7", INCONC, "no demo-latest.json")
        sc = self.by_scenario(rep)
        tids = [r["traceId"] for s in ("S2", "S7") for r in sc.get(s, [])]
        if not tids:
            return self.add("AC7", INCONC, "no S2/S7 runs")
        start, end = window(rep)
        q = f"trace:[{','.join(tids)}] project:{self.be}"
        rows, field = poll(lambda: self._logs(q, start, end), lambda r: {norm_id(x.get('trace')) for x in r[0]} >= {norm_id(t) for t in tids},
                           self.args.timeout, label="S2/S7 logs")
        allj = self.col["all"]
        lines, ok = [], True
        for t in tids:
            ls = [r for r in rows if norm_id(r.get("trace")) == norm_id(t) and not re.match(r"^(Internal Server Error|Bad Gateway)", str(r.get("message")))]
            if not ls:
                ok = False
                lines.append(f"{t[:8]}: no BE logs on trace")
                continue
            expected = None
            if allj is not None:
                srv = [s for s in allj.get(norm_id(t), []) if s.get("_service") == "demo-api" and s.get("kind") in (2, "SPAN_KIND_SERVER")]
                # A trace can contain several Django requests (S2: GET /api/v1/models, then POST /graphql/),
                # so a log is correct if it sits on ANY demo-api SERVER span of that trace.
                expected = {norm_id(s["spanId"]) for s in srv} or None
            span_ids = {norm_id(r.get(field)) for r in ls} if field else set()
            span_ok = None if not (field and expected) else span_ids <= expected
            if span_ok is False:
                ok = False
            lines.append(f"{t[:8]}: {len(ls)} logs; span_id field={field or 'n/a'} values={sorted(span_ids)[:3]} expected one of={sorted(expected)[:4] if expected else None} -> {'ok' if span_ok else ('MISMATCH' if span_ok is False else 'not checked')}")
        status = PASS if ok else FAIL
        self.add("AC7", status, "logs on OTel trace in Sentry (ids also checked locally by tests/local/be_harness.py): " + "; ".join(lines),
                 [f"{self.api.ui}/explore/logs/?query=trace%3A{tids[0]}"] + [self.trace_url(t) for t in tids[:2]])

    def _logs(self, q, start, end):
        try:
            return self.api.events_try_fields("logs", ["trace", "message", "project", "timestamp"], ["span_id", "trace.span_id", "span", "sentry.span_id"], q, start, end)
        except ApiError as exc:
            print(f"  logs query failed: {exc}", file=sys.stderr)
            return [], None

    # -- AC8 -----------------------------------------------------------------------------
    def ac8(self):
        reps = [r for r in (self.runs["demo"], self.runs["mixed"]) if r]
        if not reps:
            return self.add("AC8", INCONC, "no demo/mixed run")
        start = min(window(r)[0] for r in reps)
        end = max(window(r)[1] for r in reps)
        hits = []
        for q in (f'span.description:"{MARKER}"', f'span.name:"{MARKER}"'):
            try:
                rows = self.api.events("spans", ["trace", "project", "count()"], q, start, end)
                hits += [r for r in rows if int(r.get("count()") or 0)]
            except ApiError:
                pass
        tree_hits = []
        if self.runs["demo"]:
            sc = self.by_scenario(self.runs["demo"])
            ds, de = window(self.runs["demo"])
            for r in sc.get("S5", [])[:1] + sc.get("S6", [])[:1]:
                try:
                    nodes = self.flatten(self.trace_tree(r["traceId"], ds, de))
                    tree_hits += [r["traceId"] for n in nodes if MARKER in self.label(n)]
                except ApiError:
                    pass
        sent, allj = self.col["sentry"], self.col["all"]
        local_sent = None if sent is None else sum(1 for sps in sent.values() for s in sps if s.get("name") == MARKER)
        local_all = None if allj is None else sum(1 for sps in allj.values() for s in sps if s.get("name") == MARKER)
        ok = not hits and not tree_hits and (local_sent in (0, None))
        sanity = "" if local_all else " (WARNING: no markers in all.jsonl either, so the check is vacuous)"
        self.add("AC8", PASS if ok and local_all else (FAIL if not ok else INCONC),
                 f"marker spans in Sentry spans dataset={len(hits)}, in S5/S6 trace trees={len(tree_hits)}; local: sentry.jsonl={local_sent}, all.jsonl(Jaeger)={local_all}{sanity}",
                 [self.trace_url(h.get("trace")) for h in hits[:3]])

    # -- AC9 / AC10 ---------------------------------------------------------------------
    def ac9(self):
        rep = self.runs["mixed"]
        if not rep:
            return self.add("AC9", INCONC, "no out/loadgen/mixed-latest.json (run `make mixed`)")
        self.wait_ready(rep)
        start, end = window(rep, before=1, after=15)
        runs = runs_of(rep)
        exp_fe = sum((r.get("errors") or {}).get("fe", 0) for r in runs)
        exp_be = sum((r.get("errors") or {}).get("be", 0) for r in runs)
        tids = [r["traceId"] for r in runs if (r.get("errors") or {}).get("fe", 0) + (r.get("errors") or {}).get("be", 0)]

        def got():
            errs, _ = self.errors_for(tids, start, end)
            c = Counter(e.get("project") for es in errs.values() for e in es)
            return c

        c = poll(got, lambda c: c.get(self.fe, 0) >= exp_fe and c.get(self.be, 0) >= exp_be, self.args.timeout, label="S11 errors")
        # Secondary: every error in the window, regardless of trace (catches errors that landed on a foreign trace, e.g. Go).
        win = Counter()
        try:
            for p in (self.fe, self.be):
                rows = self.api.events("errors", ["project", "count()"], f"project:{p}", parse_ts(rep["started"]), parse_ts(rep["finished"]) + timedelta(minutes=2))
                for r in rows:
                    win[r.get("project")] += int(r.get("count()") or 0)
        except ApiError:
            pass
        ok = c.get(self.fe, 0) == exp_fe and c.get(self.be, 0) == exp_be
        hint = "" if ok else (" | errors in the run window regardless of trace: " + dict(win).__repr__() +
                              " (BE window count == expected but trace-linked count lower => some BE errors are not on their trace, e.g. go-replica trace context)")
        self.add("AC9", PASS if ok else FAIL, f"S11 ({len(runs)} runs): expected errors FE={exp_fe} BE={exp_be}; received on the run's traces FE={c.get(self.fe, 0)} BE={c.get(self.be, 0)}{hint}",
                 [f"{self.api.ui}/issues/?project={self.project_ids.get(self.be, '')}&statsPeriod=1h"])

    def ac10(self):
        rep = self.runs["mixed"]
        if not rep:
            return self.add("AC10", INCONC, "no mixed-latest.json")
        allj, sent = self.col["all"], self.col["sentry"]
        if allj is None or sent is None:
            return self.add("AC10", INCONC, "out/collector/all.jsonl or sentry.jsonl missing: the collector must run with the demo overlay (COLLECTOR_OVERLAY=demo.yaml, the default), which adds the file exporters")
        runs = runs_of(rep)
        tids = {norm_id(r["traceId"]) for r in runs}
        s1 = {norm_id(r["traceId"]) for r in runs if r["scenario"] == "S1"}
        a_sp = sum(len(allj.get(t, [])) for t in tids)
        s_sp = sum(len(sent.get(t, [])) for t in tids)
        a_tr = sum(1 for t in tids if allj.get(t))
        s_tr = sum(1 for t in tids if sent.get(t))
        s1_leak = [t for t in s1 if sent.get(t)]
        # Traces not started by a scenario (page loads of the loadgen pages) in the same time window
        st, fi = parse_ts(rep["started"]).timestamp() * 1e9, parse_ts(rep["finished"]).timestamp() * 1e9 + 60e9
        other_all = [t for t, sps in allj.items() if t not in tids and any(st <= int(s.get("startTimeUnixNano", 0)) <= fi for s in sps)]
        other_sent = [t for t in other_all if sent.get(t)]
        sentry_side = None
        try:
            start, end = window(rep, before=1, after=15)
            counts = self.span_counts(sorted(tids), start, end)
            sentry_side = sum(sum(c.values()) for c in counts.values())
        except ApiError:
            pass
        red = (1 - s_sp / a_sp) * 100 if a_sp else 0
        self.add("AC10", PASS if a_sp and not s1_leak else FAIL,
                 f"S11 scenario traces: Jaeger {a_tr} traces/{a_sp} spans, Sentry {s_tr} traces/{s_sp} spans (collector copy) -> {red:.1f}% span reduction; "
                 f"Sentry API sees {sentry_side} spans on these traces; S1 traces leaked to Sentry={len(s1_leak)}; "
                 f"other traces in window (page loads): Jaeger {len(other_all)}, Sentry {len(other_sent)}",
                 [], {"jaeger_spans": a_sp, "sentry_spans": s_sp, "reduction_pct": round(red, 1)})

    # -- AC11 ----------------------------------------------------------------------------
    def ac11(self):
        rep = self.runs["demo"]
        if not rep:
            return self.add("AC11", INCONC, "no demo-latest.json")
        s5 = [r["traceId"] for r in self.by_scenario(rep).get("S5", [])]
        if not s5:
            return self.add("AC11", INCONC, "no S5 run")
        start, end = window(rep)
        tid = s5[0]
        try:
            rows, field = self.api.events_try_fields("errors", ["id", "trace", "project"], ["replayId", "replay.id", "replay_id"], f"trace:{tid} project:{self.fe}", start, end)
        except ApiError as exc:
            return self.add("AC11", INCONC, str(exc))
        if not rows:
            return self.add("AC11", FAIL, f"no FE error on S5 trace {tid}", [self.trace_url(tid)])
        ev = rows[0]
        rid = ev.get(field) if field else None
        if not rid:
            return self.add("AC11", FAIL if field else INCONC, f"FE error {ev.get('id')} has no replay id (field {field})", [self.trace_url(tid)])
        try:
            rp = self.api.get(f"/api/0/organizations/{self.org}/replays/{norm_id(rid)}/", {"start": iso(start), "end": iso(end)}).get("data", {})
        except ApiError as exc:
            return self.add("AC11", INCONC, f"replay {rid}: {exc}")
        err_ok = norm_id(ev.get("id")) in {norm_id(x) for x in rp.get("error_ids", [])}
        tr_ok = norm_id(tid) in {norm_id(x) for x in rp.get("trace_ids", [])}
        self.add("AC11", PASS if err_ok and tr_ok else FAIL, f"replay {norm_id(rid)}: linked to FE error={err_ok}, to S5 trace={tr_ok}",
                 [f"{self.api.ui}/explore/replays/{norm_id(rid)}/", self.trace_url(tid)])

    # -- AC12 (indirect) -------------------------------------------------------------------
    def ac12(self):
        rep = self.runs["demo"]
        if not rep:
            return self.add("AC12", INCONC, "no demo-latest.json: `make demo` did not complete")
        expected = {"S1": [200, 200], "S2": [200, 500], "S3": [200], "S4": [502], "S5": [200, 200], "S7": [200], "S8": [200], "S9": [200]}
        bad = []
        for r in runs_of(rep):
            exp = expected.get(r["scenario"])
            got = [c.get("status") for c in r.get("calls", [])]
            if exp and got != exp:
                bad.append(f"{r['scenario']} {got}!={exp}")
        failed = rep.get("summary", {}).get("failed_runs", [])
        have = {r["scenario"] for r in runs_of(rep)}
        missing = [s for s in ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9") if s not in have]
        ok = not bad and not failed and not missing
        self.add("AC12", PASS if ok else FAIL, "indirect: `make demo` produced every scenario with the expected HTTP statuses"
                 + ("" if ok else f" | problems: {bad + [str(f)[:120] for f in failed] + ['missing ' + m for m in missing]}")
                 + " (a clean-checkout run is still a manual step)")

    # -- run -----------------------------------------------------------------------------
    def run(self, only):
        order = ["AC1", "AC2", "AC3", "AC4", "AC5", "AC6", "AC7", "AC8", "AC9", "AC10", "AC11", "AC12"]
        if self.runs["demo"]:
            self.wait_ready(self.runs["demo"])
        for ac in order:
            if only and ac not in only:
                continue
            try:
                getattr(self, ac.lower())()
            except Exception as exc:  # never crash the table
                self.add(ac, INCONC, f"check crashed: {type(exc).__name__}: {exc}")


def print_table(results):
    w = max(len(r["summary"]) for r in results) if results else 0
    print("\n| AC | Status | Evidence | Links |")
    print("|---|---|---|---|")
    for r in results:
        links = " ".join(r["links"][:3])
        print(f"| {r['ac']} | {r['status']} | {r['summary'].replace('|', '/')} | {links} |")
    c = Counter(r["status"] for r in results)
    print(f"\n{c.get(PASS, 0)} PASS, {c.get(FAIL, 0)} FAIL, {c.get(INCONC, 0)} INCONCLUSIVE")
    return w


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=".env")
    ap.add_argument("--out", default="out", help="dir with loadgen/ and collector/ (default ./out)")
    ap.add_argument("--only", default="", help="comma-separated ACs, e.g. AC2,AC6")
    ap.add_argument("--no-wait", action="store_true", help="don't wait for decision_wait + ingestion delay")
    ap.add_argument("--ingest-delay", type=int, default=60, help="seconds to allow for Sentry ingestion after decision_wait (default 60)")
    ap.add_argument("--timeout", type=int, default=240, help="max seconds to poll for data that should appear (default 240)")
    ap.add_argument("--jaeger", default="http://localhost:16686")
    ap.add_argument("--json", default="", help="also write results as JSON to this file")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    env = load_env(args.env)
    missing = [k for k in ("SENTRY_AUTH_TOKEN", "SENTRY_ORG") if not env.get(k)]
    if missing:
        sys.exit(f"missing {missing} in {args.env} / environment")
    only = {x.strip().upper() for x in args.only.split(",") if x.strip()}
    ch = Checker(args, env)
    ch.run(only)
    print_table(ch.results)
    if args.json:
        Path(args.json).write_text(json.dumps(ch.results, indent=2, default=str))
        print(f"wrote {args.json}")
    sys.exit(1 if any(r["status"] == FAIL for r in ch.results) else 0)


if __name__ == "__main__":
    main()
