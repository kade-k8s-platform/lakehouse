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
