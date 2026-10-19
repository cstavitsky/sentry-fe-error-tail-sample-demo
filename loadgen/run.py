#!/usr/bin/env python3
"""
Playwright load generator: drives the demo UI like real users and writes a JSON
record of every run (scenario, trace id, HTTP statuses, expected errors) to
$OUT_DIR (default /out/loadgen) so a test can reconcile Sentry / Jaeger against it.

  python run.py demo                 every scenario once (S1..S9 + S12, S13, S6 via page reload)
  python run.py scenario S2 [n]      one scenario n times (default 1)
  python run.py perf                 AC6 recipe: S8 x10, then S9 x120 (10 in parallel)
  python run.py mixed [n]            S11: n traces (default 500), ~90% S1
  options: --base-url URL  --concurrency N  --seed N

Each scenario runs in the page via window.__demo.run(id), i.e. through the real
Sentry browser SDK + OTLP split transport.
"""

import argparse
import asyncio
import json
import os
import random
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

ALL = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9"]
# Backend-only scenarios: plain HTTP from the loadgen, no browser, no traceparent.
BACKEND_ONLY = {"S12", "S13"}
MIXED_ERRORS_AND_SLOW = ["S2", "S3", "S4", "S5", "S7"]


async def open_page(browser, base_url):
    page = await browser.new_page()
    page.on("pageerror", lambda e: None)  # S6 throws on purpose
    await page.goto(base_url + "/", wait_until="networkidle")
    await page.wait_for_function("() => !!window.__demo")
    return page


async def run_in_page(page, scenario):
    return await page.evaluate("id => window.__demo.run(id)", scenario)


async def run_s6(browser, base_url):
    """S6 needs a fresh page load: the error fires ~5 s after the page-load span ends."""
    page = await browser.new_page()
    page.on("pageerror", lambda e: None)
    await page.goto(base_url + "/?run=S6")
    await page.wait_for_function(
        "() => (window.__demo?.results || []).some(r => r.scenario === 'S6' && !r.pending)", timeout=60_000
    )
    await page.wait_for_timeout(1500)  # let the error event + marker span go out
    result = await page.evaluate("() => window.__demo.results.find(r => r.scenario === 'S6')")
    await page.evaluate("() => window.__demo.flush(5000)")
    await page.close()
    return result


async def run_fresh(browser, base_url, scenario):
    """Run one scenario in its own page.

    Used for S5: the SDK's default Dedupe integration drops an error that is identical
    (same message + stack) to the previous one captured by the same page. Re-running S5
    in a reused page would therefore under-count FE errors - correct SDK behaviour,
    but it breaks the 'errors sent == errors received' reconciliation (AC9).
    """
    page = await open_page(browser, base_url)
    try:
        return await run_in_page(page, scenario)
    finally:
        await page.evaluate("() => window.__demo.flush(5000)")
        await page.wait_for_timeout(1500)  # span buffer flushes 500 ms after segment end
        await page.close()


FRESH_PAGE = {"S5"}

# Backend-only scenarios: a direct API call (like curl, a cron job or another service)
# with NO traceparent header, so the trace begins in Django. Django returns the trace
# id it started in `x-demo-trace-id` so the run can be reconciled.
BACKEND_ONLY_SPEC = {
    # S12: Django -> Go, Go fails, Django returns 502. Kept by the `errors` policy:
    # Go + Django issues on a trace with no FE spans.
    "S12": {"path": "/api/v1/models/2/deployments?replica_fail=1", "expectSentry": True, "errors": {"fe": 0, "be": 2}},
    # S13: Django only (no Go call, no error, fast). Matches no policy: DROPPED from the
    # Sentry path, still 100% in the trace store. Also with KEEP_BROWSER_TRACES=1: no
    # browser, so no `sentry-trace` header and nothing for the browser-traces policy.
    "S13": {"path": "/api/v1/models", "expectSentry": False, "errors": {"fe": 0, "be": 0}},
}
PW = None  # set in main(): the Playwright instance, for API requests outside a browser


async def run_backend_only(base_url, scenario):
    spec = BACKEND_ONLY_SPEC[scenario]
    ctx = await PW.request.new_context(base_url=base_url)
    try:
        started = datetime.now(timezone.utc).isoformat()
        r = await ctx.get(spec["path"])
        return {
            "scenario": scenario,
            "traceId": r.headers.get("x-demo-trace-id", ""),
            "startedAt": started,
            "calls": [{"url": spec["path"], "status": r.status}],
            "expectSentry": spec["expectSentry"],
            "errors": dict(spec["errors"]),
        }
    finally:
        await ctx.dispose()


async def run_many(browser, base_url, scenarios, concurrency):
    """Run a list of scenario ids with N pages in parallel; returns results in order."""
    queue = asyncio.Queue()
    for i, s in enumerate(scenarios):
        queue.put_nowait((i, s))
    results = [None] * len(scenarios)

    async def worker():
        page = await open_page(browser, base_url)
        while True:
            try:
                i, s = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                if s == "S6":
                    results[i] = await run_s6(browser, base_url)
                elif s in BACKEND_ONLY:
                    results[i] = await run_backend_only(base_url, s)
                elif s in FRESH_PAGE:
                    results[i] = await run_fresh(browser, base_url, s)
                else:
                    results[i] = await run_in_page(page, s)
            except Exception as exc:  # record and keep going
                results[i] = {"scenario": s, "error": str(exc)}
        await page.evaluate("() => window.__demo.flush(5000)")
        await page.wait_for_timeout(1500)  # span buffer flushes 500 ms after segment end
        await page.close()

    await asyncio.gather(*(worker() for _ in range(max(1, concurrency))))
    return results


def fe_spans_mode(base_url):
    """FE_SPANS the *running* frontend was built with (nginx serves it as /fe-spans.txt)."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{base_url}/fe-spans.txt", timeout=5) as r:
            v = r.read(32).decode().strip()
        return v if v in ("otlp", "sentry") else "unknown"
    except Exception:
        return "unknown"


def summarize(runs):
    by = Counter(r.get("scenario") for r in runs)
    fe = sum((r.get("errors") or {}).get("fe", 0) for r in runs if "error" not in r)
    be = sum((r.get("errors") or {}).get("be", 0) for r in runs if "error" not in r)
    return {
        "runs": len(runs),
        "by_scenario": dict(sorted(by.items())),
        "expected_in_sentry": sum(1 for r in runs if r.get("expectSentry")),
        "expected_errors": {"fe": fe, "be": be},
        "failed_runs": [r for r in runs if "error" in r],
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["demo", "scenario", "perf", "mixed"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://nginx"))
    ap.add_argument("--concurrency", type=int, default=int(os.environ.get("CONCURRENCY", "4")))
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    base = a.base_url.rstrip("/")

    out_dir = Path(os.environ.get("OUT_DIR", "/out/loadgen"))
    out_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    phases = {}

    global PW
    async with async_playwright() as p:
        PW = p
        browser = await p.chromium.launch()
        if a.mode == "demo":
            phases["demo"] = await run_many(browser, base, [s for s in ALL if s != "S6"], 1)
            phases["demo"].append(await run_s6(browser, base))
            phases["demo"].append(await run_backend_only(base, "S12"))
            phases["demo"].append(await run_backend_only(base, "S13"))
        elif a.mode == "scenario":
            sid = (a.args[0] if a.args else "S1").upper()
            n = int(a.args[1]) if len(a.args) > 1 else 1
            phases[sid] = await run_many(browser, base, [sid] * n, a.concurrency if sid != "S6" else 1)
        elif a.mode == "perf":
            phases["n_plus_one"] = await run_many(browser, base, ["S8"] * 10, 1)
            # 120 Slow DB requests in parallel (issue is created on the 100th occurrence)
            phases["slow_db"] = await run_many(browser, base, ["S9"] * 120, 10)
        elif a.mode == "mixed":
            n = int(a.args[0]) if a.args else 500
            rng = random.Random(a.seed)
            plan = ["S1" if rng.random() < 0.9 else rng.choice(MIXED_ERRORS_AND_SLOW) for _ in range(n)]
            phases["mixed"] = await run_many(browser, base, plan, a.concurrency)
        await browser.close()

    runs = [r for rs in phases.values() for r in rs if r]
    report = {
        "mode": a.mode,
        "args": a.args,
        "base_url": base,
        "be_tracing_mode": os.environ.get("BE_TRACING_MODE", "otlp"),
        "fe_spans": fe_spans_mode(base),
        "started": started,
        "finished": datetime.now(timezone.utc).isoformat(),
        "duration_s": round(time.time() - t0, 1),
        "summary": summarize(runs),
        "phases": phases,
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{a.mode}-{stamp}.json"
    path.write_text(json.dumps(report, indent=2))
    (out_dir / f"{a.mode}-latest.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report["summary"], indent=2))
    print(f"wrote {path}")


if __name__ == "__main__":
    asyncio.run(main())
