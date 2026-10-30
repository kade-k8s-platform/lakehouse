"""The Polaris 1.6.0 syslog proofs on the kind gate (ADR-081, Decision 1 and Consequences 1 to 4).

`check-logs` chains `run()` after its own checks. Each of two legs clones the release's Polaris
Deployment into a one-replica probe Deployment of its own (the syslog handler set by
`QUARKUS_LOG_SYSLOG_*` environment variables, the log claim and the file appender off), with a
native sidecar listening on 127.0.0.1:5140 as the real sidecar will:

  leg A  gates/syslog_receiver.py, a plain receiver, max-length 2048: the handler starts with the
         keys, frames are octet-counted and whole, a line over max-length is cut (and how), a
         stalled receiver with async.overflow=discard does not block request threads, the JSON
         carries hostName, sequence, mdc and loggerName.
  leg B  Fluent Bit's syslog input (the pinned `fluent_bit_syslog`), max-length 16384: every
         line arrives framed and whole through the input; the body's key is recorded.

The load is gates/syslog_load.py in a check-labelled pod. The pure parts (the clone, the
commands, the checks) are unit-tested; the cluster behaviour is the smoke gate's. A check
returns (problems, observations); the gate prints every leg's observations before judging.
"""

import copy
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "install"))
import network  # noqa: E402
import release  # noqa: E402

PORT = 5140
RECEIVER = "receiver"
FLUENT_BIT = "fluent-bit"
POLARIS_CONTAINER = "polaris"
PROBE_APP = "polaris-syslog-probe"
CODE_CONFIGMAP = "syslog-probe-code"
FB_CONFIGMAP = "syslog-probe-fb"
READY_FILE = "/tmp/syslog-receiver-ready"
STALL_FILE = "/tmp/syslog-stall"
RESULTS_FILE = "/tmp/syslog-received.jsonl"
ROLLOUT_TIMEOUT = "300s"
ACCESS_LOGGER = "io.quarkus.http.access-log"
# The release's JVM options (ZGC, half the memory) would put a third large JVM on a runner.
JAVA_OPTS = "-XX:MaxRAMPercentage=25.0 -XX:+UseSerialGC -XX:+ExitOnOutOfMemoryError"
# The properties ADR-081 Decision 1 sets, with the value each leg gives them; `max-length` is
# the leg's. `truncate` and `block-on-reconnect` stay unset so their defaults are measured.
SYSLOG_PROPERTIES = (
    ("quarkus.log.syslog.enabled", "true"),
    ("quarkus.log.syslog.endpoint", f"localhost:{PORT}"),
    ("quarkus.log.syslog.protocol", "tcp"),
    ("quarkus.log.syslog.syslog-type", "rfc5424"),
    ("quarkus.log.syslog.use-counting-framing", "true"),
    ("quarkus.log.syslog.json.enabled", "true"),
    ("quarkus.log.syslog.max-length", None),
    ("quarkus.log.syslog.async.enabled", "true"),
    ("quarkus.log.syslog.async.queue-length", "512"),
    ("quarkus.log.syslog.async.overflow", "discard"),
)
SECURITY = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]}}


def env_name(key):
    """SmallRye Config's environment form of a property: upper case, `.` and `-` as `_`."""
    return re.sub(r"[.\-]", "_", key).upper()


def quarkus_env(max_length):
    """The ten properties as environment variables, and the file appender off (the probe
    must not write to the release's shared claim)."""
    env = {env_name(key): str(max_length if value is None else value)
           for key, value in SYSLOG_PROPERTIES}
    env["QUARKUS_LOG_FILE_ENABLED"] = "false"
    return env


def probe_name(leg):
    return f"polaris-syslog-probe-{leg}"


def receiver_container(pins):
    """The plain receiver as a native sidecar on the pinned Python image, its code from the
    ConfigMap; (container, volumes)."""
    container = {
        "name": RECEIVER, "image": pins["standin_images"]["recorder"]["image"],
        "command": ["python3", "/probe/syslog_receiver.py"], "restartPolicy": "Always",
        "startupProbe": {"exec": {"command": ["test", "-f", READY_FILE]}, "periodSeconds": 1,
                         "failureThreshold": 60},
        "securityContext": copy.deepcopy(SECURITY),
        "volumeMounts": [{"name": CODE_CONFIGMAP, "mountPath": "/probe", "readOnly": True},
                         {"name": "syslog-probe-tmp", "mountPath": "/tmp"}]}
    volumes = [{"name": CODE_CONFIGMAP, "configMap": {"name": CODE_CONFIGMAP}},
               {"name": "syslog-probe-tmp", "emptyDir": {}}]
    return container, volumes


def fluent_bit_config():
    """Leg B's Fluent Bit: the syslog input as ADR-081 sets it for the sidecar, the lines
    printed as JSON for the gate to read."""
    return "\n".join([
        "[SERVICE]",
        "    flush        1",
        "    log_level    info",
        "    parsers_file /fluent-bit/etc/parsers.conf",
        "",
        "[INPUT]",
        "    Name         syslog",
        "    Mode         tcp",
        "    Listen       127.0.0.1",
        f"    Port         {PORT}",
        "    Parser       syslog-rfc5424",
        "    Format       octet_counting",
        "    Buffer_Chunk_Size 32KB",
        "    Buffer_Max_Size   64KB",
        "",
        "[OUTPUT]",
        "    Name         stdout",
        "    Match        *",
        "    Format       json_lines",
        ""])


def fluent_bit_container(pins):
    """Fluent Bit 5.1.4 as a native sidecar, its config from the ConfigMap; (container,
    volumes). The image has no shell, so no exec probe: the start order is the container
    order."""
    container = {
        "name": FLUENT_BIT, "image": pins["standin_images"]["fluent_bit_syslog"]["image"],
        "args": ["-c", "/fb/fluent-bit.conf"], "restartPolicy": "Always",
        "securityContext": copy.deepcopy(SECURITY),
        "volumeMounts": [{"name": FB_CONFIGMAP, "mountPath": "/fb", "readOnly": True}]}
    return container, [{"name": FB_CONFIGMAP, "configMap": {"name": FB_CONFIGMAP}}]


def code_configmap(namespace, source):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": CODE_CONFIGMAP, "namespace": namespace},
            "data": {"syslog_receiver.py": source}}


def fluent_bit_configmap(namespace):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": FB_CONFIGMAP, "namespace": namespace},
            "data": {"fluent-bit.conf": fluent_bit_config()}}


def probe_deployment(source, name, max_length, sidecar, volumes):
    """A one-replica Deployment cloned from the release's Polaris `source`, as `name`: Helm's
    metadata and Polaris's labels gone (so the Service, the budget and the gate's own pod
    queries never see it), the log claim and the anti-affinity gone, the syslog handler on
    through the environment, `sidecar` first among the init containers. `source` is not
    changed."""
    pod = copy.deepcopy(source["spec"]["template"]["spec"])
    template = source["spec"]["template"]["metadata"]
    selector = {"app.kubernetes.io/name": PROBE_APP, "app.kubernetes.io/instance": name}
    kept = {k: v for k, v in template.get("labels", {}).items() if k.startswith("lakehouse/")}
    pod.pop("affinity", None)
    pod.pop("topologySpreadConstraints", None)
    pod["volumes"] = [v for v in pod.get("volumes", []) if v["name"] != "polaris-logs"] + volumes
    # A log sidecar of the release (ADR-081's placeholder name) would hold the port.
    pod["initContainers"] = [sidecar] + [c for c in pod.get("initContainers", [])
                                         if c["name"] != "log-sidecar"]
    env = quarkus_env(max_length)
    for container in pod["containers"]:
        container["volumeMounts"] = [m for m in container.get("volumeMounts", [])
                                     if m["name"] != "polaris-logs"]
    polaris = next((c for c in pod["containers"] if c["name"] == POLARIS_CONTAINER),
                   pod["containers"][0])
    drop = set(env) | {"JAVA_OPTS"}
    polaris["env"] = ([e for e in polaris.get("env", []) if e["name"] not in drop]
                      + [{"name": "JAVA_OPTS", "value": JAVA_OPTS}]
                      + [{"name": k, "value": v} for k, v in env.items()])
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": source["metadata"]["namespace"],
                         "labels": dict(selector)},
            "spec": {"replicas": 1, "selector": {"matchLabels": dict(selector)},
                     "template": {"metadata": {"labels": {**selector, **kept}}, "spec": pod}}}


def _kubectl(context, namespace):
    return ["kubectl", "--context", context, "-n", namespace]


def apply_command(context, namespace):
    return _kubectl(context, namespace) + ["apply", "-f", "-"]


def rollout_command(context, namespace, name):
    return _kubectl(context, namespace) + ["rollout", "status", f"deployment/{name}",
                                           f"--timeout={ROLLOUT_TIMEOUT}"]


def pods_command(context, namespace, name):
    return _kubectl(context, namespace) + [
        "get", "pods", "-l", f"app.kubernetes.io/instance={name}", "-o", "json"]


def _exec(context, namespace, pod, *command):
    return _kubectl(context, namespace) + ["exec", pod, "-c", RECEIVER, "--", *command]


def stall_command(context, namespace, pod, on):
    """Closes (or reopens) the receiver's window: while the file exists it reads nothing."""
    return _exec(context, namespace, pod, *(["touch", STALL_FILE] if on
                                            else ["rm", "-f", STALL_FILE]))


def results_command(context, namespace, pod):
    return _exec(context, namespace, pod, "cat", RESULTS_FILE)


def count_command(context, namespace, pod):
    return _exec(context, namespace, pod, "wc", "-l", RESULTS_FILE)


def logs_command(context, namespace, pod, container):
    return _kubectl(context, namespace) + ["logs", pod, "-c", container]


def delete_command(context, namespace, kind, name):
    return _kubectl(context, namespace) + ["delete", kind, name, "--ignore-not-found"]


def load_command(env, image, context, name, spec):
    """The pod that runs gates/syslog_load.py (on stdin) with root's credentials from their
    Secret, told `spec`; it carries the check label the gate's policy lets reach every pod."""
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": release.POLARIS_ROOT_SECRET,
                                                         "key": key}}}
             for var, key in (("ROOT_CLIENT_ID", "clientId"),
                              ("ROOT_CLIENT_SECRET", "clientSecret"))]
    overrides = {"spec": {"containers": [{"name": name, "image": image, "env": creds,
                                          "stdin": True, "stdinOnce": True}]}}
    return _kubectl(context, env["NS_CATALOG_POLARIS"]) + [
        "run", name, f"--labels={network.CHECK_LABEL}=true", f"--image={image}",
        "--restart=Never", "--rm", "-i", "--quiet", "--pod-running-timeout=3m",
        "--override-type=strategic", "--overrides", json.dumps(overrides),
        "--command", "--", "python3", "-", json.dumps(spec)]


# The checks. Each returns (problems, observations), both lists of text; the gate prints the
# observations of a leg before it judges them.

STALL_MAX_MS = 5000
STALL_FACTOR = 3
STALL_MORE_MS = 250


def percentile(values, p):
    """The nearest-rank percentile."""
    ordered = sorted(values)
    return ordered[max(math.ceil(p / 100 * len(ordered)), 1) - 1]


def latency_summary(ms):
    return {"n": len(ms), "p50": percentile(ms, 50), "p99": percentile(ms, 99), "max": max(ms)}


def json_lines(log):
    """The JSON objects among the lines of a container's log; Fluent Bit's own log lines are
    skipped."""
    found = []
    for line in log.splitlines():
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                found.append(value)
    return found


def _logger(record):
    return (record.get("fields") or {}).get("loggerName")


def _fields(record):
    return record.get("fields") or {}


def check_started(records, polaris_log):
    """Proof 1: the handler starts with the keys."""
    if not records:
        return (["the handler did not start with the keys (or the names are wrong): no frame "
                 "reached the receiver"], [])
    first, problems = records[0], []
    notes = [f"first frame: pri {first.get('pri')}, version {first.get('version')}, host "
             f"{first.get('host')}, app {first.get('app')}, procid {first.get('procid')}, "
             f"declared {first.get('declared')}, msg_len {first.get('msg_len')}",
             f"first frame head: {first.get('head')}", f"first frame tail: {first.get('tail')}"]
    if first.get("version") != 1:
        problems.append(f"the first frame's RFC 5424 version is {first.get('version')!r}, not 1")
    if not isinstance(first.get("pri"), int):
        problems.append(f"the first frame's PRI is {first.get('pri')!r}")
    if not first.get("json_ok"):
        problems.append("the first frame's message is not a JSON object")
    for line in polaris_log.splitlines():
        if "syslog" in line.lower() and ("ERROR" in line or "Exception" in line):
            problems.append(f"Polaris's log has an error about syslog: {line[:200]}")
    return problems, notes


def _logger_ok(record):
    return _logger(record) in (None, ACCESS_LOGGER)


def check_framing(records, sent):
    """Proof 2 on the plain receiver: every frame framed, every request's access line in
    exactly one frame, every such body one JSON object."""
    problems = []
    for r in records:
        if not r.get("framing_ok"):
            problems.append(f"frame {r.get('n')} is not framed: {r.get('error', 'no reason')}")
    sent_set = set(sent)
    for request_id in sent:
        lines = [r for r in records if r.get("request_id") == request_id and _logger_ok(r)]
        if len(lines) != 1:
            problems.append(f"{request_id} is in {len(lines)} frames, not 1")
        problems += [f"{request_id}: the body of frame {r['n']} is not one JSON object"
                     for r in lines if not r.get("json_ok")]
    other = [r for r in records if r.get("request_id") not in sent_set and not r.get("json_ok")]
    notes = [f"{len(records)} frames, {len(sent)} requests sent, "
             f"{sum(1 for r in records if r.get('request_id') in sent_set)} frames carry one",
             f"{len(other)} frame(s) outside the requests are not a JSON object"
             + "".join(f"; frame {r['n']} declared {r.get('declared')}: {r.get('head', '')[:80]}"
                       for r in other[:2])]
    return problems, notes


def classify_long(records, prefix, length, max_length, whole=False):
    """Proof 3: what became of the request whose ID has `length` characters (`prefix` starts
    it) at max-length `max_length`. `whole` makes anything but an intact line a problem."""
    frames = [r for r in records if (r.get("request_id") or "").startswith(prefix)]
    problems = [f"frame {r.get('n')} of the {length}-character line is not framed"
                for r in frames if not r.get("framing_ok")]
    if not frames:
        label = "dropped"
        problems.append(f"the {length}-character line was dropped: no frame at max-length "
                        f"{max_length}")
    elif len(frames) > 1:
        label = f"split in {len(frames)} frames"
    elif not frames[0].get("json_ok"):
        label = "cut, JSON invalid"
    else:
        mdc = _fields(frames[0]).get("mdc")
        got = mdc.get("requestId") if isinstance(mdc, dict) else None
        label = "not cut" if isinstance(got, str) and len(got) >= length else "cut, JSON valid"
    if whole and frames and label != "not cut":
        problems.append(f"the {length}-character line did not arrive whole at max-length "
                        f"{max_length}: {label}")
    notes = [f"{length}-character request id at max-length {max_length}: {label}"]
    for r in frames[:3]:
        counts = r.get("declared", 0) > r.get("msg_len", 0)
        notes.append(f"  frame {r.get('n')}: declared {r.get('declared')}, msg_len "
                     f"{r.get('msg_len')}, declared counts the envelope: "
                     f"{'yes' if counts else 'no'}; head {r.get('head', '')[:100]!r}; "
                     f"tail {r.get('tail', '')[-80:]!r}")
    return problems, notes


def check_stall(baseline, rounds):
    """Proof 4: over the stalled rounds every request answered 401 (none timed out, none took
    5000 ms), p99 stayed within the bound, and the queue did overflow (a stall that lost
    nothing proves nothing). `baseline` and each round hold p50, p99 and max in ms; a round
    also its `sent`, the `missing` IDs at the receiver, status `counts` and `timeouts`."""
    bound = max(STALL_FACTOR * baseline["p99"], baseline["p99"] + STALL_MORE_MS)
    problems, notes = [], [f"baseline: p50 {baseline['p50']} ms, p99 {baseline['p99']} ms, "
                           f"max {baseline['max']} ms; the stalled p99 may be {bound} ms"]
    for i, r in enumerate(rounds, 1):
        notes.append(f"stalled round {i}: {r['sent']} requests, {r['missing']} lines lost, "
                     f"p50 {r['p50']} ms, p99 {r['p99']} ms, max {r['max']} ms, counts "
                     f"{r['counts']}, timeouts {r['timeouts']}")
        for status, count in sorted(r["counts"].items()):
            if status != "401":
                problems.append(f"stalled round {i}: {count} request(s) answered {status}, "
                                "not 401")
        if r["timeouts"]:
            problems.append(f"stalled round {i}: {r['timeouts']} request(s) timed out")
        if r["max"] >= STALL_MAX_MS:
            problems.append(f"stalled round {i}: the slowest request took {r['max']} ms, "
                            f"not under {STALL_MAX_MS} ms")
        if r["p99"] > bound:
            problems.append(f"stalled round {i}: p99 {r['p99']} ms is over the bound "
                            f"{bound} ms (baseline p99 {baseline['p99']} ms)")
    if not any(r["missing"] for r in rounds):
        problems.append(f"NOT SATURATED: {len(rounds)} stalled round(s) lost no line, so the "
                        "queue never overflowed and the stall proves nothing")
    return problems, notes


def _sample(record, name):
    return (record.get("sample") or {}).get(name)


def check_fields(records, sent, pod):
    """Proof 5 on the plain receiver: hostName is the pod, sequence an integer rising in
    arrival order, (hostName, sequence) unique, mdc the request's, loggerName a string."""
    problems, sent_set = [], set(sent)
    lines = [r for r in records if r.get("request_id") in sent_set and r.get("json_ok")]
    json_records = [r for r in records if r.get("json_ok")]
    last = None
    for r in lines:
        f, rid = _fields(r), r["request_id"]
        if f.get("hostName") != pod:
            problems.append(f"{rid}: hostName is {f.get('hostName')!r}, not the pod {pod!r}")
        if r.get("host") != f.get("hostName"):
            problems.append(f"{rid}: the envelope host {r.get('host')!r} differs from hostName "
                            f"{f.get('hostName')!r}")
        seq = f.get("sequence")
        if not isinstance(seq, int) or isinstance(seq, bool):
            problems.append(f"{rid}: sequence is {seq!r}, not an integer")
        elif last is not None and seq <= last:
            problems.append(f"{rid}: sequence {seq} does not rise after {last}")
        else:
            last = seq
        mdc = f.get("mdc")
        if not isinstance(mdc, dict) or mdc.get("requestId") != rid:
            problems.append(f"{rid}: mdc is {mdc!r}, its requestId should be the id sent")
        if not isinstance(f.get("loggerName"), str):
            problems.append(f"{rid}: loggerName is {f.get('loggerName')!r}, not a string")
    pairs = [(_fields(r).get("hostName"), _fields(r).get("sequence")) for r in json_records]
    pairs = [p for p in pairs if p[1] is not None]
    repeated = len(pairs) - len(set(pairs))
    if repeated:
        problems.append(f"(hostName, sequence) repeats {repeated} time(s) in {len(pairs)} lines")
    keys = {}
    for r in json_records:
        for name, kind in (r.get("keys") or {}).items():
            entry = keys.setdefault(name, [set(), None])
            entry[0].add(kind)
            entry[1] = entry[1] or _sample(r, name)
    notes = ["json keys: " + (", ".join(
        f"{name}: {'|'.join(sorted(kinds))}" + (f" (e.g. {sample})" if sample else "")
        for name, (kinds, sample) in sorted(keys.items())) or "none")]
    first = lines[0] if lines else (json_records[0] if json_records else {})
    notes.append(f"timestamp form: {_fields(first).get('timestamp')!r}")
    outside = [r for r in json_records if not r.get("request_id")]
    if outside:
        form = _fields(outside[0]).get("mdc", "absent")
        notes.append(f"mdc outside a request: {form!r} (first of {len(outside)} lines)")
    else:
        notes.append("mdc outside a request: no line seen")
    notes.append(f"loggerName of the request lines: {sorted({_logger(r) for r in lines}, key=str)}")
    notes.append(f"envelope: app {first.get('app')!r}, procid {first.get('procid')!r}, host "
                 f"{first.get('host')!r}")
    if len(pairs) == len(json_records) and not repeated and pairs:
        notes.append("event_id <hostName>:<timestamp>:<sequence> is viable: sequence is on "
                     f"all {len(pairs)} lines and (hostName, sequence) is unique")
    else:
        notes.append("event_id <hostName>:<timestamp>:<sequence> is not viable: "
                     f"{len(json_records) - len(pairs)} line(s) lack sequence, {repeated} "
                     "repeat(s)")
        if len(pairs) < len(json_records):
            problems.append(f"sequence is missing from {len(json_records) - len(pairs)} "
                            "line(s)")
    return problems, notes


def _body(record):
    """(key, parsed body) of a Fluent Bit syslog record, whichever key holds the JSON text."""
    keys = [k for k in ("message", "log", "msg") if k in record] + [
        k for k, v in record.items() if isinstance(v, str) and v.startswith("{")]
    for key in keys:
        try:
            body = json.loads(record[key])
        except (TypeError, ValueError):
            continue
        if isinstance(body, dict):
            return key, body
    return (keys[0] if keys else None), None


def check_fluent_bit(records, sent):
    """Proof 2 on leg B: every request's line once in Fluent Bit's output with a JSON body."""
    problems, found, body_keys = [], {}, set()
    for r in records:
        key, body = _body(r)
        if body is None:
            problems.append(f"a record's body is not a JSON object: {json.dumps(r)[:200]}")
            continue
        body_keys.add(key)
        mdc = body.get("mdc")
        if isinstance(mdc, dict) and isinstance(mdc.get("requestId"), str):
            found.setdefault(mdc["requestId"], []).append(r)
    for request_id in sent:
        count = len(found.get(request_id, []))
        if count != 1:
            problems.append(f"{request_id} is in {count} of Fluent Bit's records, not 1")
    notes = [f"Fluent Bit wrote {len(records)} records; the body is in {sorted(body_keys)}",
             "record keys: " + ", ".join(sorted(records[0])) if records else "no record"]
    return problems, notes
