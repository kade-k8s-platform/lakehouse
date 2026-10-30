"""The contract v3 check of `gate.py check-logs` (ADR-081, ADR-031): the contract's templates,
ISM policies and transform go into the OpenSearch stand-in, the 15 records the sidecar keeps go
through the main Fluent Bit stand-in, and the routing, the document shapes, ISM's attachment and
one transform's aggregate are proven. The functions here are pure; `check` takes its cluster
access as arguments (`http`, `forward`, `logs`), so this module does not import gate.py.
"""

import copy
import datetime as dt
import json
import re
import time

import contract

PROBE_PREFIX = "probe:"
PROBE_HOST = "probe-missing-group-field"
POLICY_IDS = {"candidate": "polaris-logs-candidate", "error": "polaris-logs-error"}
TRANSFORM_ID = "polaris-logs-aggregate-v3"
AGGREGATE = "polaris-logs-aggregate"
ALIAS = "polaris-logs-source"
ISM_POLICY_KEY = "index.plugins.index_state_management.policy_id"
TIME_FIELDS = ("window_start", "min_record_time", "max_record_time")
_STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z$")
_UNITS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}


def records_jsonl(records):
    """One JSON object per line, in order."""
    return "".join(json.dumps(r) + "\n" for r in records)


def pass2_records(kept):
    """Pass 1's records again, byte for byte, then one more 404 for the same bucket as "GET 404"
    (a new event_id and sequence), then a probe: "PUT 200" without `resource`, from another
    host, its event_id starting `probe:`."""
    out = [copy.deepcopy(r) for r in kept]
    base = next(r for r in kept if r["status_class"] == "404")
    extra = copy.deepcopy(base)
    extra["sequence"] = base["sequence"] + 1000
    extra["event_id"] = f"{base['hostName']}:{base['timestamp']}:{extra['sequence']}"
    put = next(r for r in kept if r.get("http_method") == "PUT" and r["status_class"] == "ok")
    probe = copy.deepcopy(put)
    del probe["resource"]
    probe["hostName"] = PROBE_HOST
    probe["event_id"] = f"{PROBE_PREFIX}{put['event_id']}"
    return out + [extra, probe]


def _is_probe(doc):
    return str(doc.get("event_id", "")).startswith(PROBE_PREFIX)


def routing_problems(sent, found):
    """`found` is {"candidate": [documents], "error": [...]}. Each record sent must be in the
    index of its own index_class once and nowhere else; a candidate line is no ERROR or WARN
    line and no failed request but a 404 (ADR-081 Decision 3)."""
    problems = []
    where = {kind: {} for kind in ("candidate", "error")}
    for kind, docs in found.items():
        for doc in docs:
            where[kind][doc.get("event_id")] = where[kind].get(doc.get("event_id"), 0) + 1
    for record in sent:
        eid, want = record["event_id"], record["index_class"]
        for kind in where:
            count = where[kind].get(eid, 0)
            if kind == want and count == 0:
                problems.append(f"{eid}: missing from the {kind} index")
            elif kind == want and count > 1:
                problems.append(f"{eid}: {count} times in the {kind} index")
            elif kind != want and count:
                problems.append(f"{eid}: in the {kind} index, not the {want} index")
    for doc in found.get("candidate", []):
        status = doc.get("http_status")
        if doc.get("level") in ("ERROR", "WARN", "FATAL"):
            problems.append(f"{doc.get('event_id')}: a {doc['level']} line in the candidate index")
        elif isinstance(status, int) and status >= 400 and status != 404:
            problems.append(f"{doc.get('event_id')}: status {status} in the candidate index")
    return problems


def document_problems(found):
    """Every document but a probe against the line schema of its index kind, @timestamp
    (Fluent Bit's) removed."""
    problems = []
    for docs in found.values():
        for doc in docs:
            if _is_probe(doc):
                continue
            stripped = {k: v for k, v in doc.items() if k != "@timestamp"}
            problems += [f"{doc.get('event_id')}: {p}" for p in contract.line_problems(stripped)]
    return problems


def instant(value):
    """A time as epoch milliseconds, from an ISO string (cut to the millisecond, as OpenSearch's
    `date` keeps it) or from a number of milliseconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(round(value))
    match = _STAMP.match(value)
    if not match:
        raise ValueError(f"not a time: {value!r}")
    base = dt.datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=dt.timezone.utc)
    millis = int((match.group(2) or "0").ljust(3, "0")[:3])
    return int(base.timestamp()) * 1000 + millis


def _iso(ms):
    base = dt.datetime.fromtimestamp(ms // 1000, dt.timezone.utc)
    return base.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}Z"


def _interval(text):
    return int(text[:-1]) * _UNITS[text[-1]]


def expected_buckets(records, transform):
    """The buckets the transform should write for `records` (probes left out): grouped by the
    groups the transform file names, with the aggregations it names. -> {key: {"groups": {...},
    "lines", "response_bytes", "min_record_time", "max_record_time"}}. A record lacking a group
    field is in no bucket here."""
    spec = transform["transform"]
    buckets = {}
    for record in records:
        if _is_probe(record):
            continue
        groups = {}
        for group in spec["groups"]:
            if "date_histogram" in group:
                g = group["date_histogram"]
                step = _interval(g["fixed_interval"])
                groups[g["target_field"]] = instant(record[g["source_field"]]) // step * step
            else:
                g = group["terms"]
                if g["source_field"] not in record:
                    break
                groups[g["target_field"]] = record[g["source_field"]]
        else:
            key = tuple(groups.items())
            bucket = buckets.setdefault(key, {"groups": groups, "_values": {}})
            for name, agg in spec["aggregations"].items():
                (kind, body), = agg.items()
                value = record.get(body["field"])
                if value is not None:
                    bucket["_values"].setdefault(name, []).append(
                        instant(value) if kind in ("min", "max") else value)
    for bucket in buckets.values():
        values = bucket.pop("_values")
        for name, agg in spec["aggregations"].items():
            (kind, _), = agg.items()
            seen = values.get(name, [])
            bucket[name] = {"value_count": lambda: len(seen), "sum": lambda: sum(seen),
                            "min": lambda: min(seen, default=None),
                            "max": lambda: max(seen, default=None)}[kind]()
    return buckets


def bucket_docs(buckets, as_float=False):
    """The documents the transform writes for `buckets`, times as ISO strings."""
    docs = []
    for bucket in buckets.values():
        doc = dict(bucket["groups"])
        doc["window_start"] = _iso(doc["window_start"])
        doc["lines"] = bucket["lines"]
        doc["response_bytes"] = float(bucket["response_bytes"]) if as_float \
            else bucket["response_bytes"]
        doc["min_record_time"] = _iso(bucket["min_record_time"])
        doc["max_record_time"] = _iso(bucket["max_record_time"])
        docs.append(doc)
    return docs


def aggregate_run_problems(expected, docs):
    """`docs` (a probe's bucket left out) against the buckets `expected_buckets` computed."""
    problems = []
    dates = {k for b in expected.values() for k, v in b["groups"].items()
             if k in TIME_FIELDS and isinstance(v, int)}
    docs = [d for d in docs if d.get("hostName") != PROBE_HOST]
    seen = {}
    for i, doc in enumerate(docs):
        problems += [f"aggregate document {i}: {p}" for p in contract.aggregate_problems(doc)]
        try:
            key = tuple((k, instant(doc[k]) if k in dates else doc[k])
                        for k in next(iter(expected.values()))["groups"]) if expected else ()
        except (KeyError, ValueError):
            continue
        seen[key] = doc
    for key, bucket in expected.items():
        doc = seen.get(key)
        if doc is None:
            problems.append(f"missing bucket {dict(key)}")
            continue
        for name in ("lines", "response_bytes"):
            if doc.get(name) != bucket[name]:
                problems.append(f"bucket {dict(key)}: {name} is {doc.get(name)!r}, "
                                f"expected {bucket[name]}")
        for name in ("min_record_time", "max_record_time"):
            try:
                same = instant(doc[name]) == bucket[name]
            except (KeyError, ValueError):
                same = False
            if not same:
                problems.append(f"bucket {dict(key)}: {name} is {doc.get(name)!r}, "
                                f"expected {_iso(bucket[name])}")
    for key in seen:
        if key not in expected:
            problems.append(f"unexpected bucket {dict(key)}")
    return problems


def counters(rows, routing):
    """The contract's counters over `rows` (aggregate documents, or records, each worth
    `lines`, default 1): each name of routing["counters"] by status_class, `errors_kept` for
    the error index, `by_pod` by hostName."""
    out = {}
    for name, classes in routing["counters"].items():
        out[name] = sum(r.get("lines", 1) for r in rows if r["status_class"] in classes)
    out["errors_kept"] = sum(r.get("lines", 1) for r in rows if r["index_class"] == "error")
    by_pod = {}
    for r in rows:
        by_pod[r["hostName"]] = by_pod.get(r["hostName"], 0) + r.get("lines", 1)
    out["by_pod"] = by_pod
    return out


def ism_problems(explain, policy_ids):
    """ISM's explain answer: each index of a kind is managed by that kind's policy, and each
    kind has an index."""
    problems, kinds = [], set()
    for name, info in explain.items():
        if not name.startswith("polaris-logs-") or not isinstance(info, dict):
            continue
        kind = name.split("-")[2]
        kinds.add(kind)
        got = info.get(ISM_POLICY_KEY)
        if got != policy_ids.get(kind):
            problems.append(f"{name}: policy {got!r}, expected {policy_ids.get(kind)!r}")
    for kind in policy_ids:
        if kind not in kinds:
            problems.append(f"no {kind} index in ISM's answer")
    return problems


def probe_outcome(docs):
    """What the transform did with the probe, as a sentence; never a problem."""
    probes = [d for d in docs if d.get("hostName") == PROBE_HOST]
    if not probes:
        return "no bucket"
    if all(d.get("resource") is None for d in probes):
        return "a bucket without resource"
    return f"a bucket with resource {probes[0].get('resource')!r}"


def _wait(poll, what, timeout, clock, sleep, step=3):
    """poll() every `step` s until it gives a value -> (value, None); (None, problem) after
    `timeout` s."""
    end = clock() + timeout
    while True:
        result = poll()
        if result:
            return result, None
        if clock() > end:
            return None, f"{what}: not done after {timeout} s"
        sleep(step)


def _clean(http):
    """A re-run on the same cluster: whatever an earlier run left, gone -> the problems of an
    index deletion that was neither done nor needless (a 404); the other answers are ignored."""
    http("POST", f"/_plugins/_transform/{TRANSFORM_ID}/_stop")
    http("DELETE", f"/_plugins/_transform/{TRANSFORM_ID}")
    problems = []
    for pattern in ("polaris-logs-candidate-*", "polaris-logs-error-*", AGGREGATE):
        status, body = http("DELETE", f"/{pattern}")
        if status not in (200, 404):
            problems.append(f"cleaning {pattern}: {status} {body}")
    for policy in POLICY_IDS.values():
        http("DELETE", f"/_plugins/_ism/policies/{policy}")
    return problems


def _search(http, pattern):
    status, body = http("GET", f"/{pattern}/_search",
                        {"size": 1000, "query": {"match_all": {}}})
    return [h for h in body.get("hits", {}).get("hits", [])] if status == 200 else []


def _found(http):
    """The line indices' documents -> ({"candidate": [...], "error": [...]}, ids differing from
    event_id)."""
    found, odd = {}, []
    for kind in ("candidate", "error"):
        hits = _search(http, f"polaris-logs-{kind}-*")
        found[kind] = [h["_source"] for h in hits]
        odd += [h["_id"] for h in hits if h["_id"] != h["_source"].get("event_id")]
    return found, odd


def _aggregate(http):
    http("POST", f"/{AGGREGATE}/_refresh")
    return _search(http, AGGREGATE)


def check(http, forward, logs, sleep=time.sleep, clock=time.monotonic, timeout=300,
          ism_timeout=120, read_timeout=60):
    """The check of the module's docstring -> (problems, notes). `http(method, path, body)` ->
    (status, JSON) for the OpenSearch stand-in; `forward(text)` sends `text`, JSON lines, through
    the main Fluent Bit stand-in and gives the forwarder pod's phase; `logs(name)` the log of the
    stand-in's Deployment `name`. A step whose result the rest needs ends the check."""
    problems, notes = [], []
    kept = [e["output"] for e in contract.kept_examples()]
    routing, transform = contract.routing(), contract.transform()
    problems += _clean(http)
    if problems:
        return problems, notes
    for kind, policy in contract.ism_policies().items():
        status, body = http("PUT", f"/_plugins/_ism/policies/{POLICY_IDS[kind]}", policy)
        if status >= 300:
            return [f"ISM policy {POLICY_IDS[kind]} refused ({status}): {body}"], notes
    for kind, template in contract.templates().items():
        status, body = http("PUT", f"/_index_template/polaris-logs-{kind}", template)
        if status >= 300:
            return [f"index template polaris-logs-{kind} refused ({status}): {body}"], notes

    def send(records, what):
        phase = forward(records_jsonl(records))
        if phase != "Succeeded":
            return [f"{what}: the forwarder ended {phase}"]
        http("POST", "/_refresh")
        return []

    def have(total):
        def poll():
            found, odd = _found(http)
            return (found, odd) if sum(map(len, found.values())) >= total else None
        return poll

    failed = send(kept, "pass 1")
    if failed:
        return failed, notes
    got, why = _wait(have(len(kept)), "the 15 records in the line indices", read_timeout,
                     clock, sleep)
    if why:
        return [why], notes
    found, odd = got
    problems += routing_problems(kept, found) + document_problems(found)
    problems += [f"document _id {i} is not its event_id" for i in odd]
    status, aliased = http("GET", f"/_alias/{ALIAS}")
    names = sorted(aliased) if status == 200 else []
    if not (any(n.startswith("polaris-logs-candidate-") for n in names)
            and any(n.startswith("polaris-logs-error-") for n in names)) or AGGREGATE in names:
        problems.append(f"alias {ALIAS} holds {names}: expected a candidate and an error index "
                        f"and not {AGGREGATE}")
    notes.append(f"alias {ALIAS} holds {names}")

    def attached():
        status, explain = http("GET", "/_plugins/_ism/explain/polaris-logs-*")
        return explain if status == 200 and not ism_problems(explain, POLICY_IDS) else None
    explain, why = _wait(attached, "ISM attached to both line indices", ism_timeout, clock,
                         sleep)
    if why:
        status, last = http("GET", "/_plugins/_ism/explain/polaris-logs-*")
        problems += [why] + ism_problems(last, POLICY_IDS)
    status, body = http("PUT", f"/_plugins/_transform/{TRANSFORM_ID}", transform)
    if status >= 300:
        reason = body.get("error", {}).get("reason", body) if isinstance(body, dict) else body
        return problems + [f"the transform was refused ({status}): {reason}"], notes
    interval = transform["transform"]["schedule"]["interval"]
    notes.append(f"transform accepted with interval {interval['period']} {interval['unit']}")
    expected = expected_buckets(kept, transform)

    def aggregated(want, bucket_lines=None):
        def poll():
            hits = _aggregate(http)
            docs = [h["_source"] for h in hits if h["_source"].get("hostName") != PROBE_HOST]
            if len(docs) < want:
                return None
            if bucket_lines and not any(d.get("status_class") == "404"
                                        and d.get("lines") == bucket_lines for d in docs):
                return None
            return hits
        return poll
    hits, why = _wait(aggregated(len(expected)), f"the {len(expected)} buckets in {AGGREGATE}",
                      timeout, clock, sleep)
    if why:
        status, explain = http("GET", f"/_plugins/_transform/{TRANSFORM_ID}/_explain")
        return problems + [f"{why}; transform explain: {json.dumps(explain)[:500]}"], notes
    docs = [h["_source"] for h in hits]
    problems += aggregate_run_problems(expected, docs)
    rows = [d for d in docs if d.get("hostName") != PROBE_HOST]
    expected_counters = counters(kept, routing)
    if counters(rows, routing) != expected_counters:
        problems.append(f"counters differ: aggregate {counters(rows, routing)}, "
                        f"records {expected_counters}")
    ids = {h["_id"] for h in hits}
    sample = sorted(ids)[0] if ids else None
    if not rows:
        return problems + [f"{AGGREGATE} holds no bucket"], notes
    types = {k: type(rows[0].get(k)).__name__ for k in TIME_FIELDS}
    notes.append(f"time fields: {types}; response_bytes: {rows[0].get('response_bytes')!r}")

    again = pass2_records(kept)
    failed = send(again, "pass 2")
    if failed:
        return problems + failed, notes
    got, why = _wait(have(len(again)), "the 17 records in the line indices", read_timeout,
                     clock, sleep)
    if why:
        return problems + [why], notes
    found2, _ = got
    problems += [f"pass 2: {p}" for p in routing_problems(again, found2)]
    expected2 = expected_buckets(again, transform)
    hits2, why = _wait(aggregated(len(expected2), 2), "the 404 bucket counting 2 lines", timeout,
                       clock, sleep)
    if why:
        return problems + [why], notes
    docs2 = [h["_source"] for h in hits2]
    problems += [f"pass 2: {p}" for p in aggregate_run_problems(expected2, docs2)]
    rows2 = [d for d in docs2 if d.get("hostName") != PROBE_HOST]
    want2 = counters([r for r in again if not _is_probe(r)], routing)
    if counters(rows2, routing) != want2:
        problems.append(f"pass 2: counters differ: aggregate {counters(rows2, routing)}, "
                        f"records {want2}")
    lost = ids - {h["_id"] for h in hits2}
    if lost:
        problems.append(f"pass 2: bucket _id(s) {sorted(lost)} are gone: a bucket's _id is not "
                        f"stable")
    notes.append(f"sample bucket _id {sample}; "
                 f"{'the same ids after the second pass' if not lost else 'ids changed'}")
    notes.append(f"probe (a line without resource): {probe_outcome(docs2)}")
    for name in ("logs-opensearch", "main-fluent-bit-standin"):
        errors = [line for line in logs(name).splitlines() if "[error]" in line.lower()]
        problems += [f"{name}: {line}" for line in errors[:5]]
    return problems, notes
