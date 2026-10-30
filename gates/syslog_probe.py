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
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "install"))
import network  # noqa: E402
import release  # noqa: E402
import syslog_load  # noqa: E402
import syslog_receiver  # noqa: E402

PORT = 5140
API_PORT = 8181
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
# The release's requests and limits are sized for production; a runner also holds two Polaris
# pods, Datahub and the stand-ins.
RESOURCES = {"requests": {"cpu": "250m", "memory": "768Mi"}, "limits": {"memory": "2Gi"}}
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
    polaris["resources"] = copy.deepcopy(RESOURCES)
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


def count_matching_command(context, namespace, pod, text):
    """How many lines of the receiver's file hold `text`; 0, not a failure, when none."""
    return _exec(context, namespace, pod, "sh", "-c",
                 f"grep -c -F -e '{text}' {RESULTS_FILE} || true")


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


def log_problems(polaris_log):
    """The lines of Polaris's own log that are an error about syslog."""
    # The load's request ids start with `syslog-` and appear in Polaris's lines.
    return [f"Polaris's log has an error about syslog: {line[:200]}"
            for line in polaris_log.splitlines()
            if "syslog" in syslog_receiver.REQUEST_ID.sub("", line).lower()
            and ("ERROR" in line or "Exception" in line)]


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
    return problems + log_problems(polaris_log), notes


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


def classify_long(records, prefix, length, max_length, whole=False, required=True):
    """Proof 3: what became of the request whose ID has `length` characters (`prefix` starts
    it) at max-length `max_length`. `whole` makes anything but an intact line a problem;
    without `required` a line that never arrived is only observed."""
    frames = [r for r in records if (r.get("request_id") or "").startswith(prefix)]
    problems = [f"frame {r.get('n')} of the {length}-character line is not framed"
                for r in frames if not r.get("framing_ok")]
    if not frames:
        label = "dropped"
        if required:
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
    problems, found, body_keys, sent_set = [], {}, set(), set(sent)
    for r in records:
        key, body = _body(r)
        if body is None:
            ids = set(syslog_receiver.REQUEST_ID.findall(json.dumps(r))) & sent_set
            if ids:
                problems.append(f"the body of {sorted(ids)[0]} is not a JSON object: "
                                f"{json.dumps(r)[:200]}")
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


# The driver.

NORMAL = {"phase": "normal", "count": 80, "workers": 1, "token": 20, "pad": 0}
BASELINE = {"phase": "base", "count": 1000, "workers": 8, "pad": 1500}
STALL_ROUNDS = (2000, 4000, 8000)
LONG_A, LONG_B = (3000,), (9000, 19000)
MAX_LENGTH = {"a": 2048, "b": 16384}
SETTLE_QUIET, SETTLE_MAX, SETTLE_POLL = 6, 90, 2


def settle(count, quiet=None, timeout=None, poll=None, sleep=None, clock=None):
    """Calls `count()` until it has not changed for `quiet` seconds, or `timeout` seconds have
    passed; returns the last count."""
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    quiet = SETTLE_QUIET if quiet is None else quiet
    timeout = SETTLE_MAX if timeout is None else timeout
    poll = SETTLE_POLL if poll is None else poll
    end, last, since = clock() + timeout, None, clock()
    while True:
        n = count()
        if n != last:
            last, since = n, clock()
        elif clock() - since >= quiet:
            return n
        if clock() >= end:
            return n
        sleep(poll)


def fluent_bit_records(records):
    """Fluent Bit's records as the receiver's: the body's request id and fields when its JSON
    parses, the id found in the text when it does not (a line cut by the input)."""
    out = []
    for i, r in enumerate(records, 1):
        key, body = _body(r)
        text = r.get(key) if key and isinstance(r.get(key), str) else json.dumps(r)
        record = {"n": i, "framing_ok": True, "json_ok": body is not None,
                  "declared": len(text.encode("utf-8")), "msg_len": len(text.encode("utf-8")),
                  "head": text[:160], "tail": text[-120:]}
        if body is not None:
            mdc = body.get("mdc")
            record["request_id"] = mdc.get("requestId") if isinstance(mdc, dict) else None
            record["fields"] = {k: body[k] for k in ("mdc", "loggerName") if k in body}
        else:
            found = syslog_receiver.REQUEST_ID.search(text)
            record["request_id"] = found.group(0) if found else None
        out.append(record)
    return out


class Leg:
    """One leg: the objects it makes, its pod, and the loads it sends."""

    def __init__(self, env, pins, context, runner, output, leg, pid):
        self.env, self.pins, self.context = env, pins, context
        self.runner, self.output, self.leg, self.pid = runner, output, leg, pid
        self.ns, self.name, self.loads = env["NS_CATALOG_POLARIS"], probe_name(leg), 0
        self.pod = self.url = None
        self.problems, self.notes = [], []

    def start(self):
        c, ns = self.context, self.ns
        source = json.loads(self.output(_kubectl(c, ns) + [
            "get", f"deployment/{release.POLARIS}", "-o", "json"]))
        if self.leg == "a":
            sidecar, volumes = receiver_container(self.pins)
            with open(os.path.join(HERE, "syslog_receiver.py"), encoding="utf-8") as f:
                configmap = code_configmap(ns, f.read())
        else:
            sidecar, volumes = fluent_bit_container(self.pins)
            configmap = fluent_bit_configmap(ns)
        items = [configmap, probe_deployment(source, self.name, MAX_LENGTH[self.leg], sidecar,
                                             volumes)]
        self.runner(apply_command(c, ns), json.dumps({"apiVersion": "v1", "kind": "List",
                                                      "items": items}))
        self.runner(rollout_command(c, ns, self.name))
        pods = json.loads(self.output(pods_command(c, ns, self.name)))["items"]
        running = [p for p in pods if p.get("status", {}).get("phase") == "Running"
                   and not p["metadata"].get("deletionTimestamp")]
        if len(running) != 1:
            raise RuntimeError(f"{len(running)} running pods for {self.name}, not 1")
        self.pod = running[0]["metadata"]["name"]
        self.url = f"http://{running[0]['status']['podIP']}:{API_PORT}"

    def load(self, phases):
        """Sends `phases` from one load pod; returns its summary lines by phase name."""
        self.loads += 1
        spec = {"url": self.url, "realm": release.POLARIS_REALM, "pid": self.pid,
                "leg": self.leg, "phases": phases}
        with open(os.path.join(HERE, "syslog_load.py"), encoding="utf-8") as f:
            script = f.read()
        image = self.pins["standin_images"]["recorder"]["image"]
        name = f"syslog-load-{self.pid}-{self.leg}-{self.loads}"
        out = self.output(load_command(self.env, image, self.context, name, spec), script)
        lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        if len(lines) != len(phases):
            raise RuntimeError(f"{name} printed {len(lines)} phase lines for {len(phases)} "
                               f"phases: {out[-300:]}")
        for line in lines:
            self.notes.append(f"load {line['phase']}: {line['sent']} sent, statuses "
                              f"{line['counts']}, timeouts {line['timeouts']}, errors "
                              f"{line['errors']}")
        return {line["phase"]: line for line in lines}

    def ids(self, phase, count):
        return [syslog_load.request_id(self.pid, self.leg, phase, i) for i in range(count)]

    def receiver_records(self):
        c, ns = self.context, self.ns
        settle(lambda: int(self.output(count_command(c, ns, self.pod)).split()[0]))
        text = self.output(results_command(c, ns, self.pod))
        return [json.loads(line) for line in text.splitlines() if line.startswith("{")]

    def judge(self, found):
        self.problems += found[0]
        self.notes += found[1]


def _leg_a(leg):
    c, ns = leg.context, leg.ns
    phases = leg.load([NORMAL, {"phase": "long", "lengths": list(LONG_A)}])
    sent = leg.ids("normal", NORMAL["count"])
    records = leg.receiver_records()
    log = leg.output(logs_command(c, ns, leg.pod, POLARIS_CONTAINER))
    leg.judge(check_started(records, log))
    long_status = phases["long"]["counts"]
    answered = long_status == {"401": len(LONG_A)}
    if not answered:
        leg.problems.append(f"the long request was answered {long_status}, not 401: the server "
                            "may have refused it before Polaris logged it, so the truncation "
                            "proof is inconclusive (use a shorter id)")
    leg.judge(classify_long(records, f"syslog-{leg.pid}-a-long-{LONG_A[0]}-", LONG_A[0],
                            MAX_LENGTH["a"], required=answered))
    leg.judge(check_fields(records, sent, leg.pod))
    base = leg.load([BASELINE])["base"]
    baseline = latency_summary(base["ms"])
    records = leg.receiver_records()
    leg.judge(check_framing(records, sent + leg.ids("base", BASELINE["count"])))
    rounds = []
    for i, size in enumerate(STALL_ROUNDS, 1):
        phase = {"phase": f"stall{i}", "count": size, "workers": 8, "pad": BASELINE["pad"]}
        leg.runner(stall_command(c, ns, leg.pod, True))
        try:
            line = leg.load([phase])[phase["phase"]]
        finally:
            leg.runner(stall_command(c, ns, leg.pod, False))
        settle(lambda: int(leg.output(count_command(c, ns, leg.pod)).split()[0]))
        got = int(leg.output(count_matching_command(c, ns, leg.pod,
                                                    f"-{phase['phase']}-")).strip() or 0)
        missing = max(size - got, 0)
        rounds.append({"sent": size, "missing": missing, "counts": line["counts"],
                       "timeouts": line["timeouts"] + line["errors"],
                       **{k: v for k, v in latency_summary(line["ms"]).items()
                          if k in ("p50", "p99", "max")}})
        if missing:
            break
    leg.judge(check_stall(baseline, rounds))
    leg.load([{"phase": "after", "count": 1}])
    arrived = any(r.get("request_id") == syslog_load.request_id(leg.pid, "a", "after", 0)
                  for r in leg.receiver_records())
    leg.notes.append("after the stall was released, one more request "
                     + ("arrived" if arrived else "did not arrive")
                     + " at the receiver (observed, not asserted)")


def _leg_b(leg):
    c, ns = leg.context, leg.ns
    lengths = list(LONG_B)
    phases = leg.load([{**NORMAL, "phase": "normal"}, {"phase": "long", "lengths": lengths}])
    answered = phases["long"]["counts"] == {"401": len(lengths)}
    if not answered:
        leg.problems.append(f"the long requests were answered {phases['long']['counts']}, not "
                            "401: the server may have refused one before Polaris logged it, so "
                            "the truncation proof is inconclusive")
    sent = leg.ids("normal", NORMAL["count"])

    def lines():
        return json_lines(leg.output(logs_command(c, ns, leg.pod, FLUENT_BIT)))

    settle(lambda: len(lines()))
    log = leg.output(logs_command(c, ns, leg.pod, FLUENT_BIT))
    records = json_lines(log)
    own = [line for line in log.splitlines() if not line.startswith("{")]
    leg.notes.append("fluent-bit's own log: " + (" | ".join(own[:12]) or "nothing"))
    leg.judge(check_fluent_bit([r for r in records if "-long-" not in json.dumps(r)], sent))
    converted = fluent_bit_records(records)
    for length in lengths:
        leg.judge(classify_long(converted, f"syslog-{leg.pid}-b-long-{length}-", length,
                                MAX_LENGTH["b"], whole=length < MAX_LENGTH["b"],
                                required=answered and length < MAX_LENGTH["b"]))
    leg.problems += log_problems(leg.output(logs_command(c, ns, leg.pod, POLARIS_CONTAINER)))


def _cleanup(leg_obj):
    c, ns = leg_obj.context, leg_obj.ns
    for kind, name in (("deployment", leg_obj.name), ("configmap", CODE_CONFIGMAP),
                       ("configmap", FB_CONFIGMAP)):
        try:
            leg_obj.runner(delete_command(c, ns, kind, name))
        except (Exception, SystemExit) as error:
            leg_obj.notes.append(f"deleting {kind}/{name} failed: {error}")


def run(env, pins, context, runner, output):
    """Both legs, one after the other, each deleted when it ends. Returns (problems,
    notes); a leg that fails in the harness is a problem and does not stop the other."""
    problems, notes, pid = [], [], os.getpid()
    for leg_id, body in (("a", _leg_a), ("b", _leg_b)):
        leg = Leg(env, pins, context, runner, output, leg_id, pid)
        start = time.monotonic()
        notes.append(f"== syslog leg {leg_id.upper()}: Polaris's own image, "
                     + ("a plain receiver" if leg_id == "a" else "Fluent Bit's syslog input")
                     + f", max-length {MAX_LENGTH[leg_id]}")
        try:
            leg.start()
            body(leg)
        except (Exception, SystemExit) as error:
            leg.problems.append(f"leg {leg_id.upper()}: the probe failed: {error}")
        finally:
            _cleanup(leg)
        notes += leg.notes + [f"leg {leg_id.upper()} took {(time.monotonic() - start) / 60:.1f} "
                              "minutes"]
        problems += [f"leg {leg_id.upper()}: {p}" if not p.startswith("leg ") else p
                     for p in leg.problems]
    return problems, notes
