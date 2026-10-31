"""The sidecar smoke step on the kind gate (ADR-082): `check-logs` chains `run()` after
`syslog_probe.run`.

  part 1  the filters. The chart's ConfigMap, rendered by `helm template` with the main
          Fluent Bit at 127.0.0.1 (three values changed, the file as released), runs in the
          image the chart ships, as a native sidecar next to a Fluent Bit stand-in of the
          observability team's (a forward input, a file output named by the tag). A driver
          (gates/sidecar_smoke_driver.py) sends Polaris-shaped syslog frames, the BOM
          included, and 2000 more; `judge` reads what arrived per tag and answers ADR-082's
          NOT KNOWN items (a) to (e).
  part 2  the restart. A probe Deployment cloned from the release's Polaris (as
          syslog_probe's legs) with the fragment's real sidecar container: one request arrives,
          the sidecar is stopped (SIGTERM, from a helper in the same process namespace), and
          Polaris's own syslog handler must reconnect when it is back (ADR-081 Consequences 5).

The pure parts are unit-tested; the cluster behaviour is the smoke gate's. A check returns
(problems, notes); the gate prints the notes whatever the result.
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "install"))
import network  # noqa: E402
import release  # noqa: E402
import sidecar_smoke_driver as driver  # noqa: E402
import syslog_probe  # noqa: E402

CHART = os.path.join(ROOT, "charts", "polaris-log-sidecar")
POD = "sidecar-smoke"
CONFIG_NAME = "sidecar-smoke-config"
MAIN_NAME = "sidecar-smoke-main"
CODE_NAME = "sidecar-smoke-code"
PREFIX = "polaris"
POLICY = "/fluent-bit/policy"
FORWARD_PORT = 24224
UID = 10001
APP_NAME = "polaris"
MAX_LENGTH = 65536
ROUNDS = 6
WAIT = "300s"
STAGES = ("access", "post")
# Keys of ADR-081's first design that ADR-082 leaves to OpenSearch: none may be on a document.
DERIVED = ("index_class", "policy_rule", "policy", "event_id", "resource", "status_class")
BOM = "﻿"


def main_config():
    """The observability team's Fluent Bit as a stand-in: a forward input, a file per tag."""
    return "\n".join([
        "[SERVICE]", "    flush     1", "    log_level info", "",
        "[INPUT]", "    Name   forward", "    Listen 127.0.0.1", f"    Port   {FORWARD_PORT}", "",
        "[OUTPUT]", "    Name   file", "    Match  *", f"    Path   {driver.OUT}",
        "    Format plain", ""])


def helm_command(namespace, chart=CHART):
    """The chart as released but for the three values that put the main stand-in on localhost."""
    return ["helm", "template", POD, chart, "--namespace", namespace,
            "--set", f"configMapName={CONFIG_NAME}", "--set", "output.host=127.0.0.1",
            "--set", f"output.port={FORWARD_PORT}"]


def _configmap(namespace, name, data):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": namespace}, "data": data}


def support_configmaps(namespace, driver_source):
    return [_configmap(namespace, MAIN_NAME, {"fluent-bit.conf": main_config()}),
            _configmap(namespace, CODE_NAME, {"sidecar_smoke_driver.py": driver_source})]


def _main_container(pins):
    return {"name": "main", "image": pins["standin_images"]["fluent_bit_syslog"]["image"],
            "args": ["-c", "/main/fluent-bit.conf"], "restartPolicy": "Always",
            "securityContext": copy.deepcopy(syslog_probe.SECURITY),
            "volumeMounts": [{"name": MAIN_NAME, "mountPath": "/main", "readOnly": True},
                             {"name": "out", "mountPath": driver.OUT}]}


def _volumes():
    return [{"name": MAIN_NAME, "configMap": {"name": MAIN_NAME}},
            {"name": CODE_NAME, "configMap": {"name": CODE_NAME}},
            {"name": "out", "emptyDir": {}}]


def smoke_pod(pins, namespace):
    """Part 1's pod: the stand-in, the chart's sidecar (args as the fragment's), the driver."""
    image = pins["standin_images"]["fluent_bit_syslog"]["image"]
    python = pins["standin_images"]["recorder"]["image"]
    sidecar = {"name": "sidecar", "image": image, "args": ["-c", f"{POLICY}/fluent-bit.yaml"],
               "restartPolicy": "Always", "securityContext": copy.deepcopy(syslog_probe.SECURITY),
               "volumeMounts": [{"name": CONFIG_NAME, "mountPath": POLICY, "readOnly": True}]}
    driver_container = {
        "name": "driver", "image": python,
        "command": ["python3", "/probe/sidecar_smoke_driver.py", "send"],
        "securityContext": copy.deepcopy(syslog_probe.SECURITY),
        "volumeMounts": [{"name": CODE_NAME, "mountPath": "/probe", "readOnly": True},
                         {"name": "out", "mountPath": driver.OUT}]}
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": POD, "namespace": namespace,
                         "labels": {network.CHECK_LABEL: "true"}},
            "spec": {"restartPolicy": "Never",
                     "securityContext": {"runAsNonRoot": True, "runAsUser": UID,
                                         "fsGroup": UID},
                     "initContainers": [_main_container(pins), sidecar],
                     "containers": [driver_container],
                     "volumes": _volumes() + [{"name": CONFIG_NAME,
                                               "configMap": {"name": CONFIG_NAME}}]}}


KILL_SCRIPT = """\
import os, signal
mine, hit = os.getpid(), 0
for entry in os.listdir("/proc"):
    if not entry.isdigit() or int(entry) == mine:
        continue
    try:
        with open(f"/proc/{entry}/cmdline", "rb") as f:
            argv = f.read().split(b"\\0")
    except OSError:
        continue
    if b"/fluent-bit/policy/fluent-bit.yaml" in argv and b"-c" in argv:
        os.kill(int(entry), signal.SIGTERM)
        hit += 1
print(hit)
"""


def kill_command(context, namespace, pod):
    """SIGTERM to the sidecar's Fluent Bit only (the process whose arguments name its config),
    by a helper in the pod's shared process namespace."""
    return ["kubectl", "--context", context, "-n", namespace, "exec", pod, "-c", "helper",
            "--", "python3", "-c", KILL_SCRIPT]


def collect_command(context, namespace, pod):
    return ["kubectl", "--context", context, "-n", namespace, "exec", pod, "-c", "helper",
            "--", "python3", "/probe/sidecar_smoke_driver.py", "collect"]


def restart_deployment(source, pins, fragment, name="polaris-syslog-probe-c"):
    """The probe Deployment of part 2: syslog_probe's clone with the stand-in first, then the
    fragment's own sidecar container and volume (unchanged but for the ConfigMap's name), then a
    helper that reads /out and can signal the sidecar."""
    python = pins["standin_images"]["recorder"]["image"]
    dep = syslog_probe.probe_deployment(source, name, MAX_LENGTH, _main_container(pins),
                                        _volumes())
    pod = dep["spec"]["template"]["spec"]
    side = copy.deepcopy(fragment["container"])
    volume = copy.deepcopy(fragment["volume"])
    volume["configMap"]["name"] = CONFIG_NAME
    helper = {"name": "helper", "image": python, "restartPolicy": "Always",
              "command": ["python3", "-c", "import time\nwhile True: time.sleep(3600)"],
              "securityContext": {**copy.deepcopy(syslog_probe.SECURITY), "runAsUser": UID},
              "volumeMounts": [{"name": "out", "mountPath": driver.OUT, "readOnly": True},
                               {"name": CODE_NAME, "mountPath": "/probe", "readOnly": True}]}
    pod["initContainers"] = [c for c in pod["initContainers"] if c["name"] == "main"] \
        + [side, helper] + [c for c in pod["initContainers"] if c["name"] != "main"]
    pod["volumes"].append(volume)
    pod["shareProcessNamespace"] = True
    pod["securityContext"] = {**pod.get("securityContext", {}), "fsGroup": UID}
    polaris = next(c for c in pod["containers"] if c["name"] == syslog_probe.POLARIS_CONTAINER)
    polaris["env"].append({"name": "QUARKUS_LOG_SYSLOG_APP_NAME", "value": APP_NAME})
    return dep


def judge(files, prefix=PREFIX, burst=driver.BURST_COUNT, limit="8MB"):
    """The checks of part 1 on `files` ({tag: [record]}); returns (problems, notes)."""
    problems, notes = [], []
    where = {}
    for tag, records in files.items():
        for record in records:
            if not isinstance(record, dict):
                continue
            rid = driver.record_id(record)
            if rid is None:
                problems.append(f"a record without an ID arrived under {tag}: {json.dumps(record)[:120]}")
                continue
            where.setdefault(rid, []).append((tag, record))
    stage_tags = [f"{prefix}"] + [f"{prefix}.{s}" for s in STAGES]
    for tag in stage_tags:
        if files.get(tag):
            problems.append(f"{len(files[tag])} record(s) arrived under the stage tag {tag}")
    expected = {line.id: line for line in driver.lines()}
    expected.update({line.id: line for line in driver.burst(burst)})
    stage_arrivals = sum(len(files.get(tag, [])) for tag in stage_tags)
    duplicates = 0
    for rid, line in expected.items():
        got = [t for t, _ in where.get(rid, [])]
        if line.expect is None:
            if got:
                problems.append(f"{rid} should have been dropped, arrived under {got}")
            continue
        want = f"{prefix}.{line.expect}"
        if got.count(want) != 1 or any(t != want for t in got):
            duplicates += max(0, len(got) - 1)
            if not rid.startswith("smoke-burst-"):
                problems.append(f"{rid} should arrive once under {want}, arrived under {got}")
    for rid, line in expected.items():
        for tag, record in where.get(rid, []):
            text = record.get("message")
            if isinstance(text, str) and text.startswith(BOM):
                problems.append(f"{rid}: message starts with a BOM under {tag}")
            for key in DERIVED:
                if key in record:
                    problems.append(f"{rid}: the derived key {key} is on the record")
            if line.access:
                for key, value in line.access.items():
                    if record.get(key) != value:
                        problems.append(f"{rid}: {key} is {record.get(key)!r}, expected the "
                                        f"string {value!r}")
    s14 = [r for _, r in where.get("smoke-s14", [])]
    if any("http_status" in r for r in s14):
        problems.append("smoke-s14: the garbage access line gained access keys")
    foo = [t for t, rs in files.items() for r in rs if "foo" in r]
    if foo:
        problems.append(f"smoke-s16 (a JSON object no row takes) arrived under {foo}")
    burst_in = sum(1 for rid in where if rid.startswith("smoke-burst-")
                   for t, _ in where[rid] if t == f"{prefix}.error.r1")
    if burst_in != burst:
        text = f"the burst: {burst_in} of {burst} arrived under {prefix}.error.r1"
        problems.append(text)
    notes.append(f"burst (e): {burst_in} of {burst} lines arrived, emitterMemBufLimit {limit}")
    s12 = bool(where.get("smoke-s12"))
    notes += [
        "NOT KNOWN (a): the BOM optional group (?:\\xEF\\xBB\\xBF)? was "
        + ("accepted: no message starts with a BOM and s13 (no BOM) arrived"
           if not any("BOM" in p for p in problems) else "NOT accepted: see the problems"),
        f"NOT KNOWN (b): {duplicates} duplicate arrival(s) of {len(expected)} lines, "
        f"{stage_arrivals} arrival(s) under a stage tag",
        "NOT KNOWN (c): message " + ("survives" if s12 else "did NOT survive")
        + " a failed JSON parse (s12 arrived under " + f"{prefix}.error.r7)",
        "NOT KNOWN (d): avoided: one rule per rewrite_tag",
    ]
    return problems, notes


def _kubectl(context, namespace):
    return ["kubectl", "--context", context, "-n", namespace]


def _json_line(text):
    lines = [line for line in text.splitlines() if line.startswith("{")]
    if not lines:
        raise RuntimeError(f"no JSON line in: {text[-300:]}")
    return json.loads(lines[-1])


def _read_source(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def part_one(env, pins, context, runner, output):
    ns, problems, notes = env["NS_CATALOG_POLARIS"], [], []
    kc = _kubectl(context, ns)
    source = _read_source(os.path.join(HERE, "sidecar_smoke_driver.py"))
    items = [copy.deepcopy(d) for d in support_configmaps(ns, source)]
    try:
        rendered = output(helm_command(ns))
        runner(kc + ["apply", "-f", "-"], rendered)
        runner(kc + ["apply", "-f", "-"],
               json.dumps({"apiVersion": "v1", "kind": "List",
                           "items": items + [smoke_pod(pins, ns)]}))
        runner(kc + ["wait", "--for=jsonpath={.status.phase}=Succeeded", f"pod/{POD}",
                     f"--timeout={WAIT}"])
        files = _json_line(output(kc + ["logs", POD, "-c", "driver"]))
        found = judge(files)
        problems += found[0]
        notes += found[1]
        notes.append("arrivals per tag: " + ", ".join(f"{t}={len(r)}" for t, r in
                                                       sorted(files.items())))
    except (Exception, SystemExit) as error:
        problems.append(f"part 1 failed: {error}")
    finally:
        try:
            notes.append("the sidecar's own log: " + " | ".join(
                output(kc + ["logs", POD, "-c", "sidecar"]).splitlines()[:25]))
        except (Exception, SystemExit) as error:
            notes.append(f"the sidecar's log could not be read: {error}")
        for kind, name in (("pod", POD), ("configmap", MAIN_NAME), ("configmap", CODE_NAME),
                           ("configmap", CONFIG_NAME)):
            try:
                runner(syslog_probe.delete_command(context, ns, kind, name))
            except (Exception, SystemExit) as error:
                notes.append(f"deleting {kind}/{name} failed: {error}")
    return problems, notes


def fragment_containers():
    """The fragment's sidecar container and its ConfigMap volume, read through helm (the gate
    has no YAML library): a one-template chart prints the fragment as JSON."""
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "templates"))
        with open(os.path.join(d, "Chart.yaml"), "w", encoding="utf-8") as f:
            f.write("apiVersion: v2\nname: fragment\nversion: 0.0.0\n")
        with open(os.path.join(d, "templates", "x.yaml"), "w", encoding="utf-8") as f:
            f.write("{{ toJson (dict \"c\" (index .Values.extraInitContainers 0) "
                    "\"v\" .Values.extraVolumes) }}\n")
        out = subprocess.run(["helm", "template", "fragment", d, "-f",
                              os.path.join(CHART, "polaris-values.yaml")],
                             capture_output=True, text=True, check=True).stdout
    data = json.loads(out[out.index("{"):])
    (volume,) = [v for v in data["v"] if v["name"] == data["c"]["volumeMounts"][0]["name"]]
    return {"container": data["c"], "volume": volume}


class SidecarLeg(syslog_probe.Leg):
    """syslog_probe's leg with the real sidecar: `start` applies this module's objects."""

    def __init__(self, *args):
        super().__init__(*args, "c", os.getpid())

    def start(self):
        c, ns = self.context, self.ns
        source = json.loads(self.output(_kubectl(c, ns) + [
            "get", f"deployment/{release.POLARIS}", "-o", "json"]))
        dep = restart_deployment(source, self.pins, fragment_containers(), self.name)
        items = support_configmaps(ns, _read_source(os.path.join(HERE,
                                                                 "sidecar_smoke_driver.py")))
        self.runner(_kubectl(c, ns) + ["apply", "-f", "-"], self.output(helm_command(ns)))
        self.runner(syslog_probe.apply_command(c, ns), json.dumps(
            {"apiVersion": "v1", "kind": "List", "items": items + [dep]}))
        self.runner(syslog_probe.rollout_command(c, ns, self.name))
        pods = json.loads(self.output(syslog_probe.pods_command(c, ns, self.name)))["items"]
        running = [p for p in pods if p.get("status", {}).get("phase") == "Running"
                   and not p["metadata"].get("deletionTimestamp")]
        if len(running) != 1:
            raise RuntimeError(f"{len(running)} running pods for {self.name}, not 1")
        self.pod = running[0]["metadata"]["name"]
        self.url = f"http://{running[0]['status']['podIP']}:{syslog_probe.API_PORT}"

    def files(self):
        out = self.output(collect_command(self.context, self.ns, self.pod))
        return _json_line(out)

    def arrived(self, request_id):
        """The tags under which `request_id` is in /out now."""
        return [t for t, rs in self.files().items()
                if any(driver.record_id(r) == request_id for r in rs)]

    def sidecar_status(self):
        pods = json.loads(self.output(syslog_probe.pods_command(self.context, self.ns,
                                                                self.name)))["items"]
        for pod in pods:
            for s in pod.get("status", {}).get("initContainerStatuses", []):
                if s["name"] == "log-sidecar":
                    return s
        return {}


def part_two(env, pins, context, runner, output):
    leg = SidecarLeg(env, pins, context, runner, output)
    c, ns = context, leg.ns
    try:
        leg.start()
        leg.load([{"phase": "before", "count": 1}])
        before = leg.ids("before", 1)[0]
        time.sleep(driver.QUIET)
        tags = leg.arrived(before)
        if tags != [f"{PREFIX}.error.r3"]:
            leg.problems.append(f"the request before the restart arrived under {tags}, not "
                                f"[{PREFIX}.error.r3]: the steady path does not work")
            return leg.problems, leg.notes
        leg.notes.append("restart probe: the request before arrived through Polaris -> "
                         "sidecar -> main under the fragment's securityContext")
        killed = output(kill_command(c, ns, leg.pod)).strip()
        if killed != "1":
            leg.problems.append(f"the helper signalled {killed} process(es), not 1: the "
                                "sidecar cannot be signalled from the helper")
            return leg.problems, leg.notes
        leg.load([{"phase": "down", "count": 1}])
        end = time.monotonic() + 180
        while time.monotonic() < end:
            status = leg.sidecar_status()
            if status.get("restartCount", 0) >= 1 and status.get("started"):
                break
            time.sleep(2)
        else:
            leg.problems.append("the sidecar did not restart within 180 s")
            return leg.problems, leg.notes
        lost = 0
        for k in range(1, ROUNDS + 1):
            phase = f"after{k}"
            leg.load([{"phase": phase, "count": 1}])
            time.sleep(driver.QUIET)
            if leg.arrived(leg.ids(phase, 1)[0]):
                leg.notes.append(f"restart probe: Polaris's syslog handler reconnected; the "
                                 f"request of round {k} arrived, {lost} request(s) lost on the "
                                 "way after the restart, 1 while the sidecar was down")
                break
            lost += 1
        else:
            leg.problems.append("Polaris's syslog handler did not reconnect after a sidecar "
                                f"restart: no request of {ROUNDS} rounds arrived")
        leg.notes.append("the sidecar's log: " + " | ".join(
            output(syslog_probe.logs_command(c, ns, leg.pod, "log-sidecar")).splitlines()[-15:]))
    except (Exception, SystemExit) as error:
        leg.problems.append(f"part 2 failed: {error}")
    finally:
        for kind, name in (("deployment", leg.name), ("configmap", MAIN_NAME),
                           ("configmap", CODE_NAME), ("configmap", CONFIG_NAME)):
            try:
                runner(syslog_probe.delete_command(c, ns, kind, name))
            except (Exception, SystemExit) as error:
                leg.notes.append(f"deleting {kind}/{name} failed: {error}")
    return leg.problems, leg.notes


def run(env, pins, context, runner, output):
    """Both parts; returns (problems, notes). A part that fails in the harness is a problem and
    does not stop the other."""
    problems, notes = [], []
    for label, part in (("part 1, the filters", part_one), ("part 2, the restart", part_two)):
        start = time.monotonic()
        notes.append(f"== sidecar smoke {label}")
        found = part(env, pins, context, runner, output)
        problems += [f"sidecar smoke: {p}" for p in found[0]]
        notes += found[1] + [f"sidecar smoke {label} took "
                             f"{(time.monotonic() - start) / 60:.1f} minutes"]
    return problems, notes
