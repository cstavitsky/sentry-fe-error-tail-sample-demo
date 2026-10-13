#!/usr/bin/env python3
"""
Strict OTLP/JSON validator (independent of the transport code).

Input: a JSON file holding a list of ExportTraceServiceRequest bodies (OTLP/JSON),
or a JSONL file (one {"body": ...} or one request per line, e.g. mock_sink's
otlp_traces.jsonl).

Checks per OTLP/JSON spec (opentelemetry-proto docs/specification.md "JSON Protobuf
Encoding"): lowerCamelCase field names; traceId 32 lowercase hex, spanId/parentSpanId
16 hex (parentSpanId omitted or valid, never ""), *UnixNano as decimal strings,
end >= start, kind int 0..5, status.code int 0..2, AnyValue has exactly one value
field, intValue a decimal string. Then round-trips each request through the real
protobuf ExportTraceServiceRequest via json_format.ParseDict (ids converted
hex->base64 first) so unknown or mistyped fields fail.
"""
import base64
import json
import re
import sys

HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX16 = re.compile(r"^[0-9a-f]{16}$")
DEC = re.compile(r"^\d+$")
SPAN_KEYS = {"traceId", "spanId", "parentSpanId", "traceState", "name", "kind", "startTimeUnixNano",
             "endTimeUnixNano", "attributes", "droppedAttributesCount", "events", "droppedEventsCount",
             "links", "droppedLinksCount", "status", "flags"}
ANY_KEYS = {"stringValue", "boolValue", "intValue", "doubleValue", "arrayValue", "kvlistValue", "bytesValue"}


def any_ok(v, path, errs):
    if not isinstance(v, dict) or len(v) != 1 or next(iter(v)) not in ANY_KEYS:
        errs.append(f"{path}: AnyValue must have exactly one of {sorted(ANY_KEYS)}: {v!r}"[:300])
        return
    k, x = next(iter(v.items()))
    if k == "stringValue" and not isinstance(x, str):
        errs.append(f"{path}: stringValue not a string: {x!r}"[:300])
    if k == "intValue" and not (isinstance(x, str) and re.match(r"^-?\d+$", x)) and not isinstance(x, int):
        errs.append(f"{path}: intValue not int64 decimal: {x!r}")
    if k == "intValue" and isinstance(x, int):
        errs.append(f"{path}: intValue should be a JSON string (int64): {x!r}")
    if k == "doubleValue" and not isinstance(x, (int, float)):
        errs.append(f"{path}: doubleValue not a number: {x!r}")
    if k == "boolValue" and not isinstance(x, bool):
        errs.append(f"{path}: boolValue not bool: {x!r}")
    if k == "arrayValue":
        for i, e in enumerate(x.get("values", [])):
            any_ok(e, f"{path}[{i}]", errs)


def validate(req, errs, stats):
    for ri, rs in enumerate(req.get("resourceSpans", [])):
        for a in rs.get("resource", {}).get("attributes", []):
            any_ok(a.get("value"), f"resource.{a.get('key')}", errs)
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                stats["spans"] += 1
                p = f"span[{sp.get('name')}]"
                extra = set(sp) - SPAN_KEYS
                if extra:
                    errs.append(f"{p}: unknown fields {extra}")
                if not HEX32.match(str(sp.get("traceId", ""))):
                    errs.append(f"{p}: bad traceId {sp.get('traceId')!r}")
                if not HEX16.match(str(sp.get("spanId", ""))):
                    errs.append(f"{p}: bad spanId {sp.get('spanId')!r}")
                if "parentSpanId" in sp and not HEX16.match(str(sp["parentSpanId"])):
                    errs.append(f"{p}: bad parentSpanId {sp['parentSpanId']!r}")
                s, e = sp.get("startTimeUnixNano"), sp.get("endTimeUnixNano")
                if not (isinstance(s, str) and DEC.match(s) and isinstance(e, str) and DEC.match(e)):
                    errs.append(f"{p}: *UnixNano must be decimal strings: {s!r} {e!r}")
                elif int(e) < int(s):
                    errs.append(f"{p}: end < start")
                elif len(s) != 19:
                    errs.append(f"{p}: startTimeUnixNano not ns-scale: {s}")
                if not isinstance(sp.get("kind"), int) or not 0 <= sp["kind"] <= 5:
                    errs.append(f"{p}: bad kind {sp.get('kind')!r}")
                st = sp.get("status", {})
                if not isinstance(st.get("code", 0), int) or st.get("code", 0) not in (0, 1, 2):
                    errs.append(f"{p}: bad status {st!r}")
                for a in sp.get("attributes", []):
                    any_ok(a.get("value"), f"{p}.{a.get('key')}", errs)
    # Round-trip through the real protobuf message.
    try:
        from google.protobuf.json_format import ParseDict
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    except ImportError:
        stats["proto"] = "skipped (opentelemetry-proto not installed)"
        return
    clone = json.loads(json.dumps(req))
    for rs in clone.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                for k in ("traceId", "spanId", "parentSpanId"):
                    if k in sp:
                        try:
                            sp[k] = base64.b64encode(bytes.fromhex(sp[k])).decode()
                        except Exception:
                            pass
    try:
        ParseDict(clone, ExportTraceServiceRequest())
        stats["proto_ok"] += 1
    except Exception as exc:
        errs.append(f"protobuf ParseDict failed: {exc}"[:400])


def load(path):
    text = open(path).read()
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        out = []
        for line in text.splitlines():
            if line.strip():
                rec = json.loads(line)
                out.append(rec.get("body", rec))
        return out


def main():
    reqs = load(sys.argv[1])
    errs, stats = [], {"spans": 0, "proto_ok": 0}
    for r in reqs:
        validate(r, errs, stats)
    print(f"requests={len(reqs)} spans={stats['spans']} protobuf_roundtrip_ok={stats['proto_ok']}/{len(reqs)}")
    for e in errs[:40]:
        print("ERROR", e)
    print("RESULT", "PASS" if not errs else f"FAIL ({len(errs)} errors)")
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
