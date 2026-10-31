"""Tests for sidecar_smoke.py and its driver. Run: python3 -m unittest discover -s gates"""

import copy
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import network  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
with open(os.path.join(HERE, "pins.json"), encoding="utf-8") as _f:
    PINS = json.load(_f)
ENV = {"NS_CATALOG_POLARIS": "polaris-ns"}
CHART = os.path.join(ROOT, "charts", "polaris-log-sidecar")

try:
    import sidecar_smoke as smoke
    import sidecar_smoke_driver as driver
except ImportError:  # the first red commit: the module does not exist yet
    smoke = driver = None


def result_for(prefix="polaris", burst=None):
    """A result holding exactly the arrivals the table expects."""
    files = {}
    for line in driver.lines() + list(driver.burst(burst if burst is not None
                                                  else driver.BURST_COUNT)):
        if line.expect is None:
            continue
        record = {"mdc": {"requestId": line.id}, "level": "INFO", "message": "text"}
        if line.access:
            record.update(line.access)
        files.setdefault(f"{prefix}.{line.expect}", []).append(record)
    return files


class Exists(unittest.TestCase):
    def test_the_modules_exist(self):
        self.assertTrue(smoke is not None and hasattr(smoke, "judge"), "gates/sidecar_smoke.py")
        self.assertTrue(driver is not None and hasattr(driver, "build_frame"),
                        "gates/sidecar_smoke_driver.py")


class Frames(unittest.TestCase):
    def test_the_prefix_counts_the_frame_without_itself(self):
        text = driver.build_frame('{"a":1}', bom=True)
        n, frame = text.split(" ", 1)
        self.assertEqual(int(n), len(frame.encode("utf-8")))
        self.assertTrue(frame.startswith("<14>1 "))
        self.assertIn("﻿{", frame)
        plain = driver.build_frame('{"a":1}', bom=False).split(" ", 1)[1]
        self.assertNotIn("﻿", plain)
        self.assertEqual(len(frame.encode()) - len(plain.encode()), 3)

    def test_l3bs_first_frame_declares_642_for_a_477_octet_body(self):
        body = "x" * 477
        n, frame = driver.build_frame(body, bom=True, host="p" * 14).split(" ", 1)
        envelope = len(frame.encode()) - 477
        # L3b measured 165 octets of envelope with its pod name; the host here is the free part.
        self.assertEqual(int(n), 477 + envelope)
        header = frame.split("﻿")[0]
        self.assertEqual(len(header.encode()) + 3, envelope)
        self.assertEqual(len(driver.JAVA_APP), 48)


class Table(unittest.TestCase):
    def test_sixteen_lines_and_a_burst(self):
        lines = driver.lines()
        self.assertEqual(len(lines), 16)
        ids = [line.id for line in lines]
        self.assertEqual(ids, [f"smoke-s{i:02d}" for i in range(1, 17)])
        self.assertEqual(driver.BURST_COUNT, 2000)
        self.assertEqual(len({line.id for line in driver.burst(50)}), 50)

    def test_the_expected_tags(self):
        want = {1: None, 2: "error.r1", 3: "error.r1", 4: "candidate.r3p", 5: "error.r3",
                6: "candidate.r4", 7: None, 8: "candidate.r5", 9: None, 10: "candidate.r2b",
                11: None, 12: "error.r7", 13: "error.r1", 14: "error.r7a", 15: "error.r7a",
                16: None}
        got = {int(line.id[-2:]): line.expect for line in driver.lines()}
        self.assertEqual(got, want)

    def test_only_the_last_line_without_json_has_no_bom_or_object(self):
        lines = {line.id: line for line in driver.lines()}
        self.assertFalse(lines["smoke-s13"].bom)
        self.assertEqual(sum(1 for line in lines.values() if not line.bom), 1)
        self.assertRaises(ValueError, json.loads, lines["smoke-s12"].body)
        self.assertIn("smoke-s12", lines["smoke-s12"].body)

    def test_ids_are_found_in_json_and_text(self):
        for line in driver.lines():
            if line.id in ("smoke-s12", "smoke-s16"):
                continue
            self.assertEqual(driver.record_id(json.loads(line.body)), line.id)
        self.assertEqual(driver.record_id({"message": "this is not json (smoke-s12)"}),
                         "smoke-s12")


class Judge(unittest.TestCase):
    def problems(self, files):
        return smoke.judge(files)[0]

    def test_exactly_the_expected_arrivals(self):
        self.assertEqual(self.problems(result_for()), [])

    def mutated(self, change):
        files = copy.deepcopy(result_for())
        change(files)
        return self.problems(files)

    def test_a_dropped_id_that_arrived(self):
        got = self.mutated(lambda f: f["polaris.error.r1"].append(
            {"mdc": {"requestId": "smoke-s07"}}))
        self.assertTrue(any("smoke-s07" in p for p in got), got)

    def test_an_id_in_two_tags(self):
        got = self.mutated(lambda f: f["polaris.error.r3"].append(
            {"mdc": {"requestId": "smoke-s02"}}))
        self.assertTrue(any("smoke-s02" in p for p in got), got)

    def test_an_id_missing(self):
        got = self.mutated(lambda f: f["polaris.candidate.r4"].clear())
        self.assertTrue(any("smoke-s06" in p for p in got), got)

    def test_an_arrival_in_a_stage_tag(self):
        got = self.mutated(lambda f: f.setdefault("polaris.access", []).append(
            {"mdc": {"requestId": "smoke-s16"}, "foo": "bar"}))
        self.assertTrue(any("polaris.access" in p for p in got), got)

    def test_a_bom_at_the_start_of_message(self):
        got = self.mutated(lambda f: f["polaris.error.r1"][0].update(message="﻿text"))
        self.assertTrue(any("smoke-s02" in p and "BOM" in p for p in got), got)

    def test_status_as_a_number(self):
        def change(f):
            for r in f["polaris.candidate.r3p"]:
                r["http_status"] = 404
        got = self.mutated(change)
        self.assertTrue(any("smoke-s04" in p and "http_status" in p for p in got), got)

    def test_a_derived_key(self):
        got = self.mutated(lambda f: f["polaris.error.r3"][0].update(index_class="error"))
        self.assertTrue(any("smoke-s05" in p and "index_class" in p for p in got), got)

    def test_a_short_burst(self):
        def change(f):
            del f["polaris.error.r1"][-3:]
        files = copy.deepcopy(result_for())
        change(files)
        problems, notes = smoke.judge(files)
        self.assertTrue(any("burst" in p and "1997" in p for p in problems), problems)
        self.assertTrue(any("burst" in n and "1997" in n for n in notes), notes)

    def test_the_prefix_follows(self):
        self.assertEqual(smoke.judge(result_for("other"), prefix="other")[0], [])

    def test_the_notes_answer_the_five_items(self):
        notes = " ".join(smoke.judge(result_for())[1])
        for item in ("(a)", "(b)", "(c)", "(d)", "(e)"):
            self.assertIn(item, notes)


class Builders(unittest.TestCase):
    def test_the_helm_command_sets_exactly_three_values(self):
        command = smoke.helm_command("polaris-ns")
        sets = [command[i + 1] for i, a in enumerate(command) if a == "--set"]
        self.assertEqual(sorted(s.split("=")[0] for s in sets),
                         ["configMapName", "output.host", "output.port"])
        self.assertIn(CHART, command)
        self.assertIn("polaris-ns", command)

    def test_the_pod(self):
        pod = smoke.smoke_pod(PINS, "polaris-ns")
        spec = pod["spec"]
        self.assertEqual([c["name"] for c in spec["initContainers"]], ["main", "sidecar"])
        self.assertTrue(all(c["restartPolicy"] == "Always" for c in spec["initContainers"]))
        self.assertEqual([c["name"] for c in spec["containers"]], ["driver"])
        self.assertEqual(pod["metadata"]["labels"][network.CHECK_LABEL], "true")
        side = spec["initContainers"][1]
        self.assertEqual(side["image"], PINS["standin_images"]["fluent_bit_syslog"]["image"])
        self.assertEqual(side["args"], ["-c", "/fluent-bit/policy/fluent-bit.yaml"])
        self.assertEqual(spec["containers"][0]["image"],
                         PINS["standin_images"]["recorder"]["image"])

    def test_the_chart_ships_the_pinned_image(self):
        with open(os.path.join(CHART, "values.yaml"), encoding="utf-8") as f:
            image = re.search(r"^image: (\S+)$", f.read(), re.M).group(1)
        self.assertEqual(image, PINS["standin_images"]["fluent_bit_syslog"]["image"])

    def fragment(self):
        return {"container": {
            "name": "log-sidecar", "image": "fluent/fluent-bit:5.1.4@sha256:" + "a" * 64,
            "restartPolicy": "Always", "args": ["-c", "/fluent-bit/policy/fluent-bit.yaml"],
            "volumeMounts": [{"name": "log-sidecar-config", "mountPath": "/fluent-bit/policy",
                              "readOnly": True}],
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001}},
            "volume": {"name": "log-sidecar-config",
                       "configMap": {"name": "polaris-log-sidecar"}}}

    def source(self):
        return {"metadata": {"name": "polaris", "namespace": "polaris-ns"},
                "spec": {"template": {"metadata": {"labels": {}}, "spec": {
                    "containers": [{"name": "polaris", "image": "p", "env": []}],
                    "volumes": [{"name": "polaris-logs",
                                 "persistentVolumeClaim": {"claimName": "x"}}]}}}}

    def test_the_restart_deployment(self):
        fragment = self.fragment()
        before = copy.deepcopy(fragment)
        dep = smoke.restart_deployment(self.source(), PINS, fragment)
        spec = dep["spec"]["template"]["spec"]
        self.assertIs(spec["shareProcessNamespace"], True)
        names = [c["name"] for c in spec["initContainers"]]
        self.assertEqual(names, ["main", "log-sidecar", "helper"])
        side = spec["initContainers"][1]
        for key in ("args", "securityContext", "restartPolicy"):
            self.assertEqual(side[key], before["container"][key])
        self.assertEqual(side["volumeMounts"], before["container"]["volumeMounts"])
        self.assertEqual(fragment, before)
        volumes = [v["name"] for v in spec["volumes"]]
        self.assertNotIn("polaris-logs", volumes)
        self.assertIn("log-sidecar-config", volumes)
        config = [v for v in spec["volumes"] if v["name"] == "log-sidecar-config"][0]
        self.assertEqual(config["configMap"]["name"], smoke.CONFIG_NAME)
        env = {e["name"]: e["value"] for e in spec["containers"][0]["env"]}
        self.assertEqual(env["QUARKUS_LOG_SYSLOG_APP_NAME"], "polaris")
        self.assertEqual(env["QUARKUS_LOG_SYSLOG_MAX_LENGTH"], "65536")
        helper = spec["initContainers"][2]
        self.assertTrue(all(m.get("readOnly") for m in helper["volumeMounts"]
                            if m["mountPath"] == "/out"))

    def test_the_restart_deployment_replaces_the_releases_own_sidecar_and_volume(self):
        source = self.source()
        pod = source["spec"]["template"]["spec"]
        pod["initContainers"] = [{"name": "log-sidecar", "image": "release", "restartPolicy": "Always"}]
        pod["volumes"].append({"name": "log-sidecar-config",
                               "configMap": {"name": "polaris-log-sidecar"}})
        source["spec"]["template"]["metadata"]["annotations"] = {"checksum/log-sidecar": "x" * 64}
        spec = smoke.restart_deployment(source, PINS, self.fragment())["spec"]["template"]["spec"]
        self.assertEqual([c["name"] for c in spec["initContainers"]].count("log-sidecar"), 1)
        configs = [v for v in spec["volumes"] if v["name"] == "log-sidecar-config"]
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["configMap"]["name"], smoke.CONFIG_NAME)

    def test_the_kill_command_targets_only_the_sidecar(self):
        command = smoke.kill_command("ctx", "ns", "pod-1")
        self.assertIn("helper", command)
        script = command[-1]
        self.assertIn("/fluent-bit/policy/fluent-bit.yaml", script)
        self.assertNotIn("fluent-bit.conf", script)
        self.assertNotIn("kill -9", script)

    def test_gate_calls_it_once(self):
        with open(os.path.join(HERE, "gate.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(len(re.findall(r"^import sidecar_smoke\b", text, re.M)), 1)
        self.assertEqual(text.count("sidecar_smoke.run("), 1)


if __name__ == "__main__":
    unittest.main()
