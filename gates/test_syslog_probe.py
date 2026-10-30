"""Tests for syslog_probe.py. Run: python3 -m unittest discover -s gates"""

import copy
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import network  # noqa: E402
import syslog_probe as probe  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "pins.json"), encoding="utf-8") as _f:
    PINS = json.load(_f)
ENV = {"NS_CATALOG_POLARIS": "polaris-ns"}
POD = "polaris-syslog-probe-a-5d8f-abcde"
ACCESS = "io.quarkus.http.access-log"
KEYS = ["QUARKUS_LOG_SYSLOG_ENABLED", "QUARKUS_LOG_SYSLOG_ENDPOINT",
        "QUARKUS_LOG_SYSLOG_PROTOCOL", "QUARKUS_LOG_SYSLOG_SYSLOG_TYPE",
        "QUARKUS_LOG_SYSLOG_USE_COUNTING_FRAMING", "QUARKUS_LOG_SYSLOG_JSON_ENABLED",
        "QUARKUS_LOG_SYSLOG_MAX_LENGTH", "QUARKUS_LOG_SYSLOG_ASYNC_ENABLED",
        "QUARKUS_LOG_SYSLOG_ASYNC_QUEUE_LENGTH", "QUARKUS_LOG_SYSLOG_ASYNC_OVERFLOW"]


def source():
    labels = {"app.kubernetes.io/name": "polaris", "app.kubernetes.io/instance": "polaris"}
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "polaris", "namespace": "polaris-ns", "uid": "u", "generation": 4,
                     "resourceVersion": "9", "creationTimestamp": "t",
                     "managedFields": [{"manager": "helm"}],
                     "labels": {**labels, "helm.sh/chart": "polaris-1.6.0"},
                     "annotations": {"meta.helm.sh/release-name": "polaris",
                                     "deployment.kubernetes.io/revision": "3"}},
        "spec": {"replicas": 2, "strategy": {"type": "RollingUpdate"},
                 "selector": {"matchLabels": labels},
                 "template": {
                     "metadata": {"labels": {**labels, "lakehouse/polaris-pg-client": "true"},
                                  "annotations": {"checksum/config": "abc"}},
                     "spec": {
                         "serviceAccountName": "polaris",
                         "affinity": {"podAntiAffinity": {"required": []}},
                         "containers": [{
                             "name": "polaris", "image": "apache/polaris:1.6.0",
                             "env": [{"name": "JAVA_OPTS", "value": "-XX:+UseZGC"},
                                     {"name": "A", "value": "1"},
                                     {"name": "S", "valueFrom": {"secretKeyRef": {
                                         "name": "polaris-db", "key": "k"}}}],
                             "volumeMounts": [{"name": "polaris-logs", "mountPath": "/logs"},
                                              {"name": "config", "mountPath": "/c"}]}],
                         "volumes": [{"name": "polaris-logs",
                                      "persistentVolumeClaim": {"claimName": "polaris-logs"}},
                                     {"name": "config", "configMap": {"name": "polaris"}}]}}},
        "status": {"readyReplicas": 2}}


def access(i, host=POD, seq=None, **extra):
    request_id = f"syslog-1-a-normal-{i}"
    fields = {"hostName": host, "sequence": 100 + i if seq is None else seq,
              "loggerName": ACCESS, "level": "INFO", "threadName": "t",
              "timestamp": "2026-10-30T10:00:00.1Z", "mdc": {"requestId": request_id}}
    fields.update(extra)
    return {"n": i, "framing_ok": True, "json_ok": True, "pri": 14, "version": 1, "host": host,
            "app": "polaris", "procid": "1", "request_id": request_id, "declared": 300,
            "actual": 300, "msg_len": 250, "head": "{", "tail": "}", "fields": fields,
            "keys": {"timestamp": "str", "sequence": "int", "mdc": "dict"}}


class Names(unittest.TestCase):
    def test_environment_names(self):
        self.assertEqual(probe.env_name("quarkus.log.syslog.use-counting-framing"),
                         "QUARKUS_LOG_SYSLOG_USE_COUNTING_FRAMING")
        self.assertEqual(probe.env_name("quarkus.log.syslog.async.queue-length"),
                         "QUARKUS_LOG_SYSLOG_ASYNC_QUEUE_LENGTH")

    def test_the_ten_keys_and_the_file_off(self):
        env = probe.quarkus_env(max_length=2048)
        self.assertEqual(sorted(env), sorted(KEYS + ["QUARKUS_LOG_FILE_ENABLED"]))
        self.assertEqual(env["QUARKUS_LOG_SYSLOG_MAX_LENGTH"], "2048")
        self.assertEqual(env["QUARKUS_LOG_SYSLOG_ENDPOINT"], "localhost:5140")
        self.assertEqual(env["QUARKUS_LOG_SYSLOG_ASYNC_OVERFLOW"], "discard")
        self.assertEqual(env["QUARKUS_LOG_SYSLOG_ASYNC_QUEUE_LENGTH"], "512")
        self.assertEqual(env["QUARKUS_LOG_FILE_ENABLED"], "false")
        self.assertTrue(all(isinstance(v, str) for v in env.values()))
        self.assertEqual(probe.quarkus_env(max_length=16384)[
            "QUARKUS_LOG_SYSLOG_MAX_LENGTH"], "16384")


class Clone(unittest.TestCase):
    def setUp(self):
        self.src = source()
        self.before = copy.deepcopy(self.src)
        self.container, self.volumes = probe.receiver_container(PINS)
        self.dep = probe.probe_deployment(self.src, "polaris-syslog-probe-a", 2048,
                                          self.container, self.volumes)
        self.pod = self.dep["spec"]["template"]["spec"]

    def test_the_source_is_not_mutated(self):
        self.assertEqual(self.src, self.before)

    def test_metadata_and_status_are_gone(self):
        self.assertEqual(self.dep["metadata"]["name"], "polaris-syslog-probe-a")
        self.assertEqual(self.dep["metadata"]["namespace"], "polaris-ns")
        text = json.dumps(self.dep)
        for gone in ("uid", "resourceVersion", "managedFields", "meta.helm.sh", "helm.sh/chart",
                     '"status"', "creationTimestamp"):
            self.assertNotIn(gone, text)

    def test_one_replica_and_its_own_labels(self):
        self.assertEqual(self.dep["spec"]["replicas"], 1)
        selector = self.dep["spec"]["selector"]["matchLabels"]
        labels = self.dep["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(selector, {"app.kubernetes.io/name": "polaris-syslog-probe",
                                    "app.kubernetes.io/instance": "polaris-syslog-probe-a"})
        self.assertEqual({k: labels[k] for k in selector}, selector)
        self.assertEqual(labels["lakehouse/polaris-pg-client"], "true")
        self.assertNotIn("polaris", set(labels.values()))

    def test_no_affinity_and_no_log_claim(self):
        self.assertNotIn("affinity", self.pod)
        names = [v["name"] for v in self.pod["volumes"]]
        self.assertNotIn("polaris-logs", names)
        self.assertIn("config", names)
        polaris = self.pod["containers"][0]
        self.assertEqual([m["name"] for m in polaris["volumeMounts"]], ["config"])

    def test_the_probe_does_not_ask_for_the_releases_resources(self):
        src = source()
        src["spec"]["template"]["spec"]["containers"][0]["resources"] = {
            "requests": {"memory": "8Gi"}, "limits": {"memory": "8Gi", "cpu": "4"}}
        dep = probe.probe_deployment(src, "p", 2048, self.container, self.volumes)
        resources = dep["spec"]["template"]["spec"]["containers"][0]["resources"]
        self.assertEqual(resources, probe.RESOURCES)

    def test_polaris_env(self):
        env = self.pod["containers"][0]["env"]
        by_name = {e["name"]: e for e in env}
        for key in KEYS + ["QUARKUS_LOG_FILE_ENABLED"]:
            self.assertIn(key, by_name)
        self.assertIn("MaxRAMPercentage", by_name["JAVA_OPTS"]["value"])
        self.assertNotIn("ZGC", by_name["JAVA_OPTS"]["value"])
        self.assertEqual(by_name["A"], {"name": "A", "value": "1"})
        self.assertEqual(by_name["S"], self.before["spec"]["template"]["spec"][
            "containers"][0]["env"][2])
        self.assertEqual([e["name"] for e in env].count("JAVA_OPTS"), 1)

    def test_a_log_sidecar_of_the_release_is_not_cloned(self):
        src = source()
        src["spec"]["template"]["spec"]["initContainers"] = [
            {"name": "log-sidecar", "image": "x"}, {"name": "wait", "image": "y"}]
        dep = probe.probe_deployment(src, "p", 2048, self.container, self.volumes)
        self.assertEqual([c["name"] for c in dep["spec"]["template"]["spec"]["initContainers"]],
                         ["receiver", "wait"])

    def test_the_receiver_is_a_native_sidecar_with_a_ready_probe(self):
        (init,) = self.pod["initContainers"]
        self.assertEqual(init["name"], "receiver")
        self.assertEqual(init["restartPolicy"], "Always")
        self.assertEqual(init["image"], PINS["standin_images"]["recorder"]["image"])
        self.assertEqual(init["startupProbe"]["exec"]["command"],
                         ["test", "-f", "/tmp/syslog-receiver-ready"])
        self.assertEqual(init["command"], ["python3", "/probe/syslog_receiver.py"])
        self.assertTrue(init["securityContext"]["readOnlyRootFilesystem"])
        volumes = {v["name"] for v in self.pod["volumes"]}
        self.assertTrue({m["name"] for m in init["volumeMounts"]} <= volumes)


class LegB(unittest.TestCase):
    def test_fluent_bit_container(self):
        container, volumes = probe.fluent_bit_container(PINS)
        self.assertEqual(container["image"], PINS["standin_images"]["fluent_bit_syslog"]["image"])
        self.assertEqual(container["restartPolicy"], "Always")
        self.assertNotIn("command", container)
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertTrue(all(m.get("readOnly") for m in container["volumeMounts"]))
        self.assertEqual({v["name"] for v in volumes}, {m["name"] for m in
                                                       container["volumeMounts"]})

    def test_the_pin(self):
        pin = PINS["standin_images"]["fluent_bit_syslog"]["image"]
        self.assertRegex(pin, r"^fluent/fluent-bit:5\.\d+\.\d+@sha256:[0-9a-f]{64}$")
        version = tuple(int(x) for x in re.match(r"[^:]+:(\d+)\.(\d+)\.(\d+)", pin).groups())
        self.assertGreaterEqual(version, (5, 1, 3))

    def test_the_config(self):
        config = probe.fluent_bit_config()
        self.assertIn("Name         syslog", config)
        self.assertIn("Listen       127.0.0.1", config)
        self.assertIn("Port         5140", config)
        self.assertIn("Mode         tcp", config)
        self.assertIn("Format       octet_counting", config)
        self.assertIn("Parser       syslog-rfc5424", config)
        self.assertIn("parsers_file /fluent-bit/etc/parsers.conf", config)
        self.assertIn("Name         stdout", config)
        self.assertIn("Format       json_lines", config)
        size = re.search(r"Buffer_Max_Size\s+(\d+)KB", config)
        self.assertGreater(int(size.group(1)) * 1024, 16384)

    def test_config_maps(self):
        code = probe.code_configmap("polaris-ns", "print('x')")
        self.assertEqual(code["data"], {"syslog_receiver.py": "print('x')"})
        fb = probe.fluent_bit_configmap("polaris-ns")
        self.assertEqual(fb["metadata"]["namespace"], "polaris-ns")
        self.assertIn("fluent-bit.conf", fb["data"])


class Checks(unittest.TestCase):
    def test_percentile_is_nearest_rank(self):
        self.assertEqual(probe.percentile(list(range(1, 101)), 99), 99)
        self.assertEqual(probe.percentile(list(range(1, 101)), 50), 50)
        self.assertEqual(probe.percentile([5], 99), 5)

    def test_latency_summary(self):
        got = probe.latency_summary([10, 20, 30, 40])
        self.assertEqual((got["p50"], got["p99"], got["max"], got["n"]), (20, 40, 40, 4))

    def stall(self, **over):
        rnd = {"sent": 2000, "missing": 12, "counts": {"401": 2000}, "timeouts": 0,
               "p50": 20, "p99": 100, "max": 300}
        rnd.update(over)
        return probe.check_stall({"p50": 10, "p99": 40, "max": 90}, [rnd])

    def test_stall_passes(self):
        problems, notes = self.stall()
        self.assertEqual(problems, [])
        self.assertTrue(any("12" in n for n in notes))

    def test_stall_failures_each_have_their_message(self):
        self.assertIn("p99", " ".join(self.stall(p99=400)[0]))
        self.assertIn("200", " ".join(self.stall(counts={"401": 1999, "200": 1})[0]))
        self.assertIn("timed out", " ".join(self.stall(timeouts=1)[0]))
        self.assertIn("5000", " ".join(self.stall(max=6000)[0]))

    def test_stall_without_loss_is_not_saturated(self):
        problems, _ = self.stall(missing=0)
        self.assertEqual(len(problems), 1)
        self.assertIn("NOT SATURATED", problems[0])

    def test_the_bound_is_three_times_or_250_ms_more(self):
        self.assertEqual(self.stall(p99=290)[0], [])
        self.assertNotEqual(self.stall(p99=291)[0], [])

    def test_started(self):
        ok = access(1)
        self.assertEqual(probe.check_started([ok], "")[0], [])
        self.assertIn("did not start with the keys", probe.check_started([], "")[0][0])
        self.assertNotEqual(probe.check_started([{**ok, "version": 0}], "")[0], [])
        self.assertNotEqual(probe.check_started([{**ok, "json_ok": False}], "")[0], [])
        log = "2026 ERROR [io.quarkus.logging] could not open syslog socket"
        self.assertNotEqual(probe.check_started([ok], log)[0], [])
        self.assertEqual(probe.check_started([ok], "INFO started")[0], [])
        # the load's own request ids start with `syslog-`
        own = '{"level":"ERROR","message":"denied","mdc":{"requestId":"syslog-1-a-stall1-5"}}'
        self.assertEqual(probe.check_started([ok], own)[0], [])

    def test_framing(self):
        records = [access(1), access(2)]
        sent = ["syslog-1-a-normal-1", "syslog-1-a-normal-2"]
        self.assertEqual(probe.check_framing(records, sent)[0], [])
        self.assertIn("syslog-1-a-normal-1", " ".join(
            probe.check_framing(records + [access(3, seq=999) | {"n": 3,
                                "request_id": sent[0]}], sent)[0]))
        self.assertIn("syslog-1-a-normal-3", " ".join(
            probe.check_framing(records, sent + ["syslog-1-a-normal-3"])[0]))
        self.assertNotEqual(probe.check_framing(
            [records[0], {**records[1], "framing_ok": False}], sent)[0], [])
        self.assertNotEqual(probe.check_framing(
            [records[0], {**records[1], "json_ok": False}], sent)[0], [])

    def long(self, count=1, json_ok=False, request_id_len=None, framing=True, length=3000):
        prefix = "syslog-1-a-long-3000-"
        rid = prefix + "x" * ((request_id_len or length) - len(prefix))
        return [{"n": i, "framing_ok": framing, "json_ok": json_ok, "request_id": rid,
                 "declared": 2100, "msg_len": 2048, "head": "h", "tail": "t",
                 "fields": {"mdc": {"requestId": rid}} if json_ok else {}}
                for i in range(count)]

    def test_classify_long(self):
        def label(records):
            return probe.classify_long(records, "syslog-1-a-long-3000-", 3000, 2048)

        problems, notes = label(self.long())
        self.assertEqual(problems, [])
        self.assertTrue(any("cut, JSON invalid" in n for n in notes))
        self.assertTrue(any("declared" in n and "2100" in n for n in notes))
        self.assertTrue(any("cut, JSON valid" in n for n in label(
            self.long(json_ok=True, request_id_len=1500))[1]))
        self.assertTrue(any("split in 2 frames" in n for n in label(self.long(count=2))[1]))
        self.assertTrue(any("not cut" in n for n in label(self.long(json_ok=True))[1]))
        problems, notes = label([])
        self.assertTrue(any("dropped" in n for n in notes + problems))
        self.assertNotEqual(problems, [])
        self.assertNotEqual(label(self.long(framing=False))[0], [])

    def test_a_line_that_never_arrived_is_only_observed_when_not_required(self):
        problems, notes = probe.classify_long([], "syslog-1-a-long-3000-", 3000, 16384,
                                              required=False)
        self.assertEqual(problems, [])
        self.assertIn("dropped", notes[0])

    def test_whole_long_line_is_required_when_asked(self):
        got = probe.classify_long(self.long(json_ok=False), "syslog-1-a-long-3000-", 3000,
                                  16384, whole=True)
        self.assertNotEqual(got[0], [])

    def test_fields(self):
        records = [access(i) for i in range(1, 4)]
        sent = [r["request_id"] for r in records]
        problems, notes = probe.check_fields(records, sent, POD)
        self.assertEqual(problems, [])
        text = "\n".join(notes)
        self.assertIn("sequence: int", text)
        self.assertIn("2026-10-30T10:00:00.1Z", text)
        self.assertIn("event_id", text)
        self.assertNotIn("not viable", text)

    def test_fields_failures(self):
        records = [access(i) for i in range(1, 4)]
        sent = [r["request_id"] for r in records]

        def problems_of(records):
            return " ".join(probe.check_fields(records, sent, POD)[0])

        self.assertIn("hostName", problems_of([access(1, host="other"), *records[1:]]))
        self.assertIn("sequence", problems_of([records[0], access(2, seq=101), records[2]]))
        self.assertIn("sequence", problems_of([records[0], access(2, seq=50), records[2]]))
        bad = access(2)
        bad["fields"]["mdc"] = {"requestId": "other"}
        self.assertIn("requestId", problems_of([records[0], bad, records[2]]))
        bad = access(2)
        del bad["fields"]["loggerName"]
        self.assertIn("loggerName", problems_of([records[0], bad, records[2]]))

    def test_without_sequence_the_event_id_is_not_viable(self):
        records = [access(i) for i in range(1, 3)]
        for r in records:
            del r["fields"]["sequence"]
        _, notes = probe.check_fields(records, [r["request_id"] for r in records], POD)
        self.assertIn("not viable", "\n".join(notes))

    def test_fluent_bit(self):
        def line(i, key="message"):
            rid = f"syslog-1-b-normal-{i}"
            return {"date": 1.0, "host": "h", key: json.dumps({"mdc": {"requestId": rid}})}

        sent = [f"syslog-1-b-normal-{i}" for i in range(2)]
        problems, notes = probe.check_fluent_bit([line(0), line(1)], sent)
        self.assertEqual(problems, [])
        self.assertIn("message", "\n".join(notes))
        self.assertNotEqual(probe.check_fluent_bit([line(0), line(0)], sent)[0], [])
        self.assertNotEqual(probe.check_fluent_bit([line(0)], sent)[0], [])
        bad = line(1)
        bad["message"] = "{not json"
        self.assertNotEqual(probe.check_fluent_bit([line(0), bad], sent)[0], [])

    def test_fluent_bit_ignores_a_record_that_is_no_request(self):
        sent = ["syslog-1-b-normal-0"]
        good = {"date": 1.0, "message": json.dumps({"mdc": {"requestId": sent[0]}})}
        stray = {"date": 1.0, "message": "plain text before json logging"}
        self.assertEqual(probe.check_fluent_bit([good, stray], sent)[0], [])

    def test_the_request_id_pattern_takes_phase_names_with_a_digit(self):
        import syslog_receiver
        self.assertEqual(syslog_receiver.REQUEST_ID.search('x "syslog-12-a-stall3-7" y').group(0),
                         "syslog-12-a-stall3-7")

    def test_fluent_bit_log_parsing(self):
        log = '[2026/10/30] [ info] started\n{"date":1.0,"message":"x"}\nnoise\n'
        self.assertEqual(probe.json_lines(log), [{"date": 1.0, "message": "x"}])


class Commands(unittest.TestCase):
    def test_every_kubectl_names_context_and_namespace(self):
        for command in (probe.apply_command("ctx", "ns"), probe.rollout_command("ctx", "ns", "d"),
                        probe.pods_command("ctx", "ns", "d"),
                        probe.stall_command("ctx", "ns", "pod", True),
                        probe.results_command("ctx", "ns", "pod"),
                        probe.count_command("ctx", "ns", "pod"),
                        probe.logs_command("ctx", "ns", "pod", "polaris"),
                        probe.delete_command("ctx", "ns", "deployment", "d")):
            self.assertEqual(command[:5], ["kubectl", "--context", "ctx", "-n", "ns"], command)

    def test_stall_toggles_exec_into_the_receiver(self):
        on = probe.stall_command("ctx", "ns", "pod", True)
        off = probe.stall_command("ctx", "ns", "pod", False)
        self.assertEqual(on[on.index("-c") + 1], "receiver")
        self.assertEqual(on[on.index("--") + 1:], ["touch", "/tmp/syslog-stall"])
        self.assertEqual(off[off.index("--") + 1:], ["rm", "-f", "/tmp/syslog-stall"])

    def test_the_load_pod(self):
        command = probe.load_command(ENV, "img:1", "ctx", "load-1", {"x": 1})
        text = json.dumps(command)
        self.assertIn(f"--labels={network.CHECK_LABEL}=true", command)
        self.assertIn("polaris-root", text)
        self.assertIn("clientId", text)
        self.assertIn("clientSecret", text)
        self.assertEqual(command[:5], ["kubectl", "--context", "ctx", "-n", "polaris-ns"])
        self.assertEqual(command[command.index("--") + 1:][:2], ["python3", "-"])
        self.assertIn(json.dumps({"x": 1}), command)

    def test_delete_is_idempotent(self):
        self.assertIn("--ignore-not-found", probe.delete_command("c", "n", "deployment", "d"))


class Run(unittest.TestCase):
    def test_a_leg_that_raises_still_deletes_its_objects(self):
        calls = []

        def run(argv, stdin=None):
            calls.append(argv)

        def output(argv, stdin=None):
            calls.append(argv)
            if "get" in argv and "deployment/polaris" in argv:
                raise SystemExit("boom")
            return ""

        problems, _ = probe.run(ENV, PINS, "ctx", run, output)
        deletes = [c for c in calls if "delete" in c]
        names = " ".join(" ".join(c) for c in deletes)
        for name in ("polaris-syslog-probe-a", "polaris-syslog-probe-b", "syslog-probe-code",
                     "syslog-probe-fb"):
            self.assertIn(name, names)
        self.assertTrue(any("boom" in p for p in problems))


class Settle(unittest.TestCase):
    def test_returns_once_the_count_has_been_quiet(self):
        now = [0.0]
        counts = iter([1, 5, 9, 9, 9, 9, 9, 9, 9, 9])
        got = probe.settle(lambda: next(counts), quiet=6, timeout=90, poll=2,
                           sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
        self.assertEqual(got, 9)
        self.assertLess(now[0], 30)

    def test_gives_up_at_the_timeout(self):
        now = [0.0]
        n = [0]

        def count():
            n[0] += 1
            return n[0]

        probe.settle(count, quiet=6, timeout=10, poll=2,
                     sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
        self.assertLessEqual(now[0], 12)


class Cluster:
    """A fake cluster for the driver: kubectl's answers and the receiver's file, made from the
    load each leg sends."""

    def __init__(self, lose_in_stall=True, cut_long=True):
        self.calls, self.sent, self.stalled, self.lose = [], [], False, lose_in_stall
        self.cut_long = cut_long
        self.seq = 0

    def runner(self, argv, stdin=None):
        self.calls.append(argv)
        if "exec" in argv and "touch" in argv:
            self.stalled = True
        if "exec" in argv and "rm" in argv:
            self.stalled = False

    def output(self, argv, stdin=None):
        self.calls.append(argv)
        text = " ".join(argv)
        if "deployment/polaris" in text and " get " in text:
            return json.dumps(source())
        if " get pods " in text:
            name = POD if "probe-a" in text else POD.replace("-a-", "-b-")
            return json.dumps({"items": [{"metadata": {"name": name},
                                          "status": {"phase": "Running", "podIP": "10.0.0.9"}}]})
        if argv[3:5] == ["-n", "polaris-ns"] and "run" in argv:
            return self.load(json.loads(argv[-1]))
        if "grep" in text and "sh" in argv:
            needle = argv[-1].split("-e '")[1].split("'")[0]
            return f"{sum(1 for r in self.lines() if needle in r.get('request_id', ''))}\n"
        if "wc" in argv:
            return f"{len(self.lines())} /tmp/syslog-received.jsonl\n"
        if "cat" in argv:
            return "\n".join(json.dumps(r) for r in self.lines())
        if "logs" in argv and "fluent-bit" in argv:
            return self.fluent_bit()
        return ""

    def load(self, spec):
        import syslog_load
        out = []
        for ph in spec["phases"]:
            ids = ([syslog_load.long_id(spec["pid"], spec["leg"], n) for n in ph["lengths"]]
                   if "lengths" in ph else
                   [syslog_load.request_id(spec["pid"], spec["leg"], ph["phase"], i)
                    for i in range(ph["count"])])
            self.sent += [(i, ph["phase"], self.stalled) for i in ids]
            out.append(json.dumps({"phase": ph["phase"], "sent": len(ids),
                                   "counts": {"401": len(ids)}, "timeouts": 0, "errors": 0,
                                   "ms": [5.0] * len(ids)}))
        return "\n".join(out) + "\n"

    def lines(self):
        records, seq = [], 100
        lost = 0
        for i, (rid, phase, stalled) in enumerate(self.sent, 1):
            if stalled and self.lose and i % 10 == 0:
                continue
            seq += 1
            if "-long-" in rid:
                if not self.cut_long:
                    pass
                records.append({"n": len(records) + 1, "framing_ok": True, "json_ok": False,
                                "request_id": rid[:1500], "declared": 2100, "msg_len": 2048,
                                "head": "h", "tail": "t"})
                continue
            f = {"hostName": POD, "sequence": seq, "loggerName": ACCESS, "mdc": {"requestId": rid},
                 "timestamp": "t"}
            records.append({"n": len(records) + 1, "framing_ok": True, "json_ok": True,
                            "pri": 14, "version": 1, "host": POD, "app": "polaris",
                            "procid": "1", "request_id": rid, "declared": 300, "actual": 300,
                            "msg_len": 250, "head": "{", "tail": "}", "fields": f,
                            "keys": {"sequence": "int"}})
        return records

    def fluent_bit(self):
        out = ["[2026/10/30] [ info] [input:syslog:syslog.0] listening on 127.0.0.1:5140"]
        for rid, phase, _ in self.sent:
            body = json.dumps({"mdc": {"requestId": rid}, "pad": "y"})
            if rid.startswith(("syslog-%d-b-long-19000" % os.getpid())):
                body = body[:80]
            out.append(json.dumps({"date": 1.0, "message": body}))
        return "\n".join(out)


class Driver(unittest.TestCase):
    def go(self, **kwargs):
        cluster = Cluster(**kwargs)
        with mock.patch.object(probe, "SETTLE_QUIET", 0), mock.patch.object(
                probe.time, "sleep", lambda s: None):
            problems, notes = probe.run(ENV, PINS, "ctx", cluster.runner, cluster.output)
        return cluster, problems, notes

    def test_both_legs_pass_on_a_product_that_does_what_adr_081_assumed(self):
        cluster, problems, notes = self.go()
        self.assertEqual(problems, [], problems)
        text = "\n".join(notes)
        self.assertIn("syslog leg A", text)
        self.assertIn("syslog leg B", text)
        self.assertIn("cut, JSON invalid", text)
        self.assertIn("lines lost", text)

    def test_the_stall_is_released_and_each_leg_cleans_up(self):
        cluster, _, _ = self.go()
        stalls = [c for c in cluster.calls if "exec" in c and c[-1] == "/tmp/syslog-stall"]
        self.assertGreater(len(stalls), 0)
        self.assertEqual(sum("touch" in c for c in stalls), sum("rm" in c for c in stalls))
        deleted = [c[c.index("delete") + 2] for c in cluster.calls if "delete" in c]
        self.assertEqual(deleted.count("polaris-syslog-probe-a"), 1)
        self.assertEqual(deleted.count("polaris-syslog-probe-b"), 1)

    def test_no_loss_in_three_rounds_is_not_saturated(self):
        _, problems, _ = self.go(lose_in_stall=False)
        self.assertTrue(any("NOT SATURATED" in p for p in problems), problems)


class Wiring(unittest.TestCase):
    def main(self, own, probed):
        sys.path.insert(0, HERE)
        import gate
        with mock.patch.object(gate, "check_logs", return_value=own), \
                mock.patch.object(gate.syslog_probe, "run", return_value=probed) as syslog, \
                mock.patch.object(gate.release, "load_env", return_value=ENV), \
                mock.patch("builtins.print") as printed:
            code = gate.main(["gate.py", "check-logs"])
        return code, syslog, [c.args[0] for c in printed.call_args_list]

    def test_both_run_and_both_are_printed(self):
        code, syslog, lines = self.main(([], ["note a"]), ([], ["note b"]))
        self.assertEqual(code, 0)
        syslog.assert_called_once()
        self.assertEqual(lines, ["note a", "note b"])

    def test_a_problem_in_either_exits_1(self):
        self.assertEqual(self.main((["p"], []), ([], []))[0], 1)
        code, _, lines = self.main(([], []), (["q"], ["n"]))
        self.assertEqual(code, 1)
        self.assertEqual(lines, ["n", "q"])


if __name__ == "__main__":
    unittest.main()
