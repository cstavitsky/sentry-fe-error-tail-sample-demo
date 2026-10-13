#!/usr/bin/env python3
"""
Local mock sink for the no-Docker harness. Stands in for Sentry ingest, the OTel
Collector's OTLP/HTTP receiver, and the Go replica service. Stdlib only (optional:
`brotli` for br-compressed envelopes, `opentelemetry-proto` for protobuf OTLP).

  POST /api/<project_id>/envelope/      Sentry envelopes -> out/mock_sink/envelopes.jsonl
                                         (one line per envelope ITEM, payload parsed)
  POST /v1/traces, /otlp/v1/traces      OTLP JSON or protobuf -> out/mock_sink/otlp_traces.jsonl
                                         (one line per request, decoded to OTLP/JSON shape)
  GET  /replica/status?model_id=&fail=1  fake Go replica (fail=1 -> 500)
  GET  /billing/quote?delay_ms=          fake slow billing provider
  GET  /_mock/summary                    counts by item type / span names
  POST /_mock/reset                      truncate the JSONL files

Usage:  python3 tools/mock_sink.py [--port 9999] [--out out/mock_sink]
"""

import argparse
import gzip
import json
import re
import threading
import time
import zlib
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LOCK = threading.Lock()
OUT = Path("out/mock_sink")


def _decompress(body, encoding):
    encoding = (encoding or "").lower()
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body)
    if encoding == "br":
        import brotli  # pip install brotli

        return brotli.decompress(body)
    return body


def parse_envelope(raw):
    """Split a Sentry envelope into (header, [(item_header, payload_bytes), ...])."""
    nl = raw.find(b"\n")
    header = json.loads(raw[: nl if nl >= 0 else len(raw)] or b"{}")
    pos = nl + 1 if nl >= 0 else len(raw)
    items = []
    while pos < len(raw):
        nl = raw.find(b"\n", pos)
        if nl < 0:
            nl = len(raw)
        line = raw[pos:nl].strip()
        pos = nl + 1
        if not line:
            continue
        item_header = json.loads(line)
        length = item_header.get("length")
        if length is not None:
            payload = raw[pos : pos + length]
            pos += length + 1
        else:
            nl = raw.find(b"\n", pos)
            if nl < 0:
                nl = len(raw)
            payload = raw[pos:nl]
            pos = nl + 1
        items.append((item_header, payload))
    return header, items


def _payload_to_json(payload):
    try:
        return json.loads(payload)
    except Exception:
        return {"_binary_bytes": len(payload)}


def decode_otlp(body, content_type):
    if "json" in (content_type or ""):
        return json.loads(body or b"{}")
    # protobuf (what the Python/Go OTel exporters send)
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    req = ExportTraceServiceRequest()
    req.ParseFromString(body)
    data = MessageToDict(req)
    # protobuf JSON mapping encodes ids as base64; convert to hex like OTLP/JSON
    import base64

    for rs in data.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                for k in ("traceId", "spanId", "parentSpanId"):
                    if sp.get(k):
                        sp[k] = base64.b64decode(sp[k]).hex()
    return data


def _append(name, record):
    with LOCK:
        OUT.mkdir(parents=True, exist_ok=True)
        with open(OUT / name, "a") as f:
            f.write(json.dumps(record) + "\n")


def _read(name):
    p = OUT / name
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-sink/1"

    def log_message(self, fmt, *args):  # quieter
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _body(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        return _decompress(raw, self.headers.get("Content-Encoding"))

    def do_POST(self):
        path = urlparse(self.path).path
        m = re.match(r"^/api/(\d+)/envelope/?$", path)
        if m:
            header, items = parse_envelope(self._body())
            now = time.time()
            for item_header, payload in items:
                _append(
                    "envelopes.jsonl",
                    {
                        "received_at": now,
                        "project_id": m.group(1),
                        "user_agent": self.headers.get("User-Agent", ""),
                        "envelope_header": header,
                        "item_header": item_header,
                        "payload": _payload_to_json(payload),
                    },
                )
            return self._json(200, {"id": header.get("event_id")})

        if path in ("/v1/traces", "/otlp/v1/traces"):
            try:
                data = decode_otlp(self._body(), self.headers.get("Content-Type"))
            except Exception as exc:  # malformed -> 400, like a real receiver
                return self._json(400, {"error": str(exc)})
            _append("otlp_traces.jsonl", {"received_at": time.time(), "content_type": self.headers.get("Content-Type"), "body": data})
            return self._json(200, {"partialSuccess": {}})

        if path == "/_mock/reset":
            with LOCK:
                for name in ("envelopes.jsonl", "otlp_traces.jsonl"):
                    (OUT / name).unlink(missing_ok=True)
            return self._json(200, {"reset": True})
        return self._json(404, {"error": "not found"})

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/replica/status":
            time.sleep(0.02)
            if q.get("fail") == "1":
                return self._json(500, {"error": "replica pool unreachable"})
            return self._json(200, {"model_id": q.get("model_id"), "replicas": [{"id": "replica-a", "state": "ACTIVE"}]})
        if url.path == "/billing/quote":
            time.sleep(int(q.get("delay_ms", "1800")) / 1000)
            return self._json(200, {"currency": "USD", "monthly_estimate": 1234.56})
        if url.path == "/_mock/summary":
            items = _read("envelopes.jsonl")
            otlp = _read("otlp_traces.jsonl")
            span_names = Counter()
            services = Counter()
            for rec in otlp:
                for rs in rec["body"].get("resourceSpans", []):
                    svc = next((a["value"].get("stringValue") for a in rs.get("resource", {}).get("attributes", []) if a["key"] == "service.name"), "?")
                    for ss in rs.get("scopeSpans", []):
                        for sp in ss.get("spans", []):
                            span_names[sp.get("name")] += 1
                            services[svc] += 1
            return self._json(
                200,
                {
                    "envelope_items_by_type": Counter(i["item_header"].get("type") for i in items),
                    "otlp_requests": len(otlp),
                    "otlp_spans_by_service": services,
                    "otlp_span_names": span_names.most_common(50),
                },
            )
        if url.path == "/healthz":
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})


def main():
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--out", default="out/mock_sink")
    args = ap.parse_args()
    OUT = Path(args.out)
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"mock sink on http://{args.host}:{args.port}  ->  {OUT}/", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
