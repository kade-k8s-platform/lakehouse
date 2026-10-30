"""Tests for charts/polaris-log-sidecar (ADR-081, ADR-009, ADR-016): the Fluent Bit sidecar's
ConfigMap and the values fragment that adds it to Polaris as a native sidecar and points
Quarkus's syslog handler at it. Nothing installs the chart yet (L4), so Polaris's own gates are
untouched; the Lua filter has its own tests (test_polaris_log_sidecar_lua.py).

UNVERIFIED, and proved by the gate that runs the sidecar (L3): that Fluent Bit starts with this
YAML and under this securityContext, that `octet_counting` reads what Quarkus sends, and that
Polaris 1.6.0 accepts the `quarkus.log.syslog.*` keys below (their names are from Quarkus's
logging guide, as ADR-081 lists them).

Run: python3 -m unittest discover -s charts
Needs helm and kubeconform on PATH (`python3 gates/gate.py tools BIN_DIR helm kubeconform`) and
PyYAML (`pip install --require-hashes -r charts/requirements.txt`).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "gates"))
sys.path.insert(0, os.path.join(ROOT, "install"))
import gate  # noqa: E402
import release  # noqa: E402

NAME = "polaris-log-sidecar"
CHART = os.path.join(HERE, NAME)
FRAGMENT = os.path.join(CHART, "polaris-values.yaml")
LUA = os.path.join(CHART, "files", "policy_v5.lua")
NAMESPACE = "catalog-polaris"
MOUNT = "/fluent-bit/policy"
SCHEMAS = None


def setUpModule():
    global SCHEMAS
    missing = [t for t in ("helm", "kubeconform") if not shutil.which(t)]
    if missing:
        raise RuntimeError(f"{', '.join(missing)} not on PATH: run "
                           "`python3 gates/gate.py tools BIN_DIR helm kubeconform` first")
    SCHEMAS = tempfile.TemporaryDirectory()
    pins = gate.load_pins()["kubernetes_schemas"]
    for name in ("configmap", "deployment"):
        # Each file under its upstream name, configmap-v1.json and deployment-apps-v1.json.
        path = os.path.join(SCHEMAS.name, pins[name]["url"].rsplit("/", 1)[1])
        with open(path, "wb") as f:
            f.write(gate.fetch(pins[name]["url"], pins[name]["sha256"]))


def tearDownModule():
    SCHEMAS.cleanup()


def helm(*argv):
    return subprocess.run(["helm", *argv], capture_output=True, text=True, check=False)


def template(*sets, strings=()):
    argv = ["template", NAME, CHART, "--namespace", NAMESPACE]
    for s in sets:
        argv += ["--set", s]
    for s in strings:
        argv += ["--set-string", s]
    return helm(*argv)


def render(*sets):
    result = template(*sets)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def values():
    with open(os.path.join(CHART, "values.yaml")) as f:
        return yaml.safe_load(f)


def configmap(*sets):
    (cm,) = render(*sets)
    return cm


def fluent_bit(*sets):
    return yaml.safe_load(configmap(*sets)["data"]["fluent-bit.yaml"])


def polaris_docs():
    """Polaris as release.py installs it, plus the fragment as a third values file."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(release.polaris_values({"POLARIS_DB_HOST": "",
                                          "POLARIS_OPA_URL": "http://opa:8181/x"}), f)
    try:
        out = subprocess.run(["helm", "template", release.POLARIS, release.POLARIS_CHART,
                              "--namespace", NAMESPACE, "-f", release.POLARIS_VALUES,
                              "-f", FRAGMENT, "-f", f.name],
                             capture_output=True, text=True, check=True).stdout
    finally:
        os.unlink(f.name)
    return [d for d in yaml.safe_load_all(out) if d]


def one(docs, kind):
    (doc,) = [d for d in docs if d["kind"] == kind]
    return doc


def kubeconform(manifests_text):
    # kubeconform's own naming, which also covers core kinds: -apps-v1 or -v1.
    location = os.path.join(SCHEMAS.name, "{{ .ResourceKind }}{{ .KindSuffix }}.json")
    return subprocess.run(["kubeconform", "-strict", "-summary", "-schema-location", location,
                           "-"], input=manifests_text, capture_output=True, text=True,
                          check=False)


def properties(docs):
    cm = [d for d in docs if d["kind"] == "ConfigMap"
          and "application.properties" in d.get("data", {})]
    (props,) = [c["data"]["application.properties"] for c in cm]
    return dict(line.split("=", 1) for line in props.splitlines() if "=" in line)


class Rendered(unittest.TestCase):
    def test_one_configmap_with_the_three_files_and_the_lua_unchanged(self):
        cm = configmap()
        self.assertEqual(cm["kind"], "ConfigMap")
        self.assertEqual(cm["metadata"]["name"], NAME)
        self.assertEqual(set(cm["data"]), {"fluent-bit.yaml", "parsers.yaml", "policy_v5.lua"})
        with open(LUA) as f:
            self.assertEqual(cm["data"]["policy_v5.lua"].rstrip("\n"), f.read().rstrip("\n"))

    def test_lint_strict(self):
        result = helm("lint", "--strict", CHART)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_config_map_name_follows_the_value(self):
        self.assertEqual(configmap("configMapName=other")["metadata"]["name"], "other")


class Schema(unittest.TestCase):
    def refused(self, *sets, key, strings=()):
        result = template(*sets, strings=strings)
        self.assertNotEqual(result.returncode, 0, f"{sets} {strings} was accepted")
        self.assertIn(key, result.stderr)

    def test_an_unknown_key(self):
        self.refused("extra=1", key="extra")

    def test_a_syslog_port_of_zero_or_a_string(self):
        self.refused("syslog.port=0", key="/syslog/port")
        self.refused(strings=["syslog.port=5140"], key="/syslog/port")

    def test_an_image_without_a_digest(self):
        self.refused("image=fluent/fluent-bit:5.1.4", key="/image")

    def test_tls_as_a_string(self):
        self.refused(strings=["output.tls=yes"], key="/output/tls")


class Config(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.values = values()
        cls.cfg = fluent_bit()

    def test_the_image_is_pinned_by_tag_and_digest_at_5_1_3_or_later(self):
        m = re.fullmatch(r"fluent/fluent-bit:(\d+)\.(\d+)\.(\d+)@sha256:[0-9a-f]{64}",
                         self.values["image"])
        self.assertIsNotNone(m, self.values["image"])
        self.assertGreaterEqual(tuple(map(int, m.groups())), (5, 1, 3))

    def test_one_syslog_input_on_localhost(self):
        (inp,) = self.cfg["pipeline"]["inputs"]
        self.assertEqual(inp["name"], "syslog")
        self.assertEqual(inp["mode"], "tcp")
        self.assertEqual(inp["listen"], "127.0.0.1")
        self.assertEqual(inp["format"], "octet_counting")
        self.assertEqual(inp["port"], self.values["syslog"]["port"])
        self.assertEqual(inp["parser"], "polaris-syslog")

    def test_a_line_fits_the_input_buffer(self):
        (inp,) = self.cfg["pipeline"]["inputs"]
        size = {"KB": 1024, "MB": 1024 ** 2}
        n, unit = re.fullmatch(r"(\d+)(KB|MB)", inp["buffer_max_size"]).groups()
        self.assertGreater(int(n) * size[unit], self.values["syslog"]["maxLength"])

    def test_memory_only(self):
        def keys(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from keys(v)
            elif isinstance(node, list):
                for v in node:
                    yield from keys(v)
        self.assertEqual([k for k in keys(self.cfg) if str(k).startswith("storage")], [])

    def test_filters_parse_the_body_then_apply_the_policy(self):
        parser, lua = self.cfg["pipeline"]["filters"]
        self.assertEqual((parser["name"], parser["parser"]), ("parser", "polaris-json"))
        self.assertEqual(parser["reserve_data"], False)
        self.assertEqual(lua["name"], "lua")
        self.assertEqual(lua["script"], f"{MOUNT}/policy_v5.lua")
        self.assertEqual(lua["call"], "polaris_filter")

    def test_numbers_leave_the_lua_filter_as_integers(self):
        # The filter's numbers are doubles unless Fluent Bit is told which keys are integers
        # (type_int_key, a space-delimited string in the image's schema.json).
        _, lua = self.cfg["pipeline"]["filters"]
        self.assertEqual(set(lua["type_int_key"].split()),
                         {"sequence", "threadId", "http_status", "response_size"})

    def test_one_forward_output_from_the_values(self):
        (out,) = self.cfg["pipeline"]["outputs"]
        o = self.values["output"]
        self.assertEqual(out["name"], "forward")
        self.assertEqual((out["host"], out["port"]), (o["host"], o["port"]))
        self.assertEqual(out["tls"], o["tls"])
        self.assertEqual(out["tls.verify"], o["tlsVerify"])

    def test_the_values_reach_the_config(self):
        cfg = fluent_bit("syslog.port=5999", "output.host=elsewhere", "output.port=1")
        (inp,) = cfg["pipeline"]["inputs"]
        (out,) = cfg["pipeline"]["outputs"]
        self.assertEqual(inp["port"], 5999)
        self.assertEqual((out["host"], out["port"]), ("elsewhere", 1))

    def test_the_parsers(self):
        parsers = yaml.safe_load(configmap()["data"]["parsers.yaml"])["parsers"]
        by_name = {p["name"]: p for p in parsers}
        self.assertEqual(by_name["polaris-json"]["format"], "json")
        self.assertEqual(by_name["polaris-syslog"]["format"], "regex")


class Fragment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.values = values()
        with open(FRAGMENT) as f:
            cls.fragment = yaml.safe_load(f)
        cls.docs = polaris_docs()
        cls.pod = one(cls.docs, "Deployment")["spec"]["template"]["spec"]

    def test_no_template_syntax_since_upstream_runs_tpl_over_it(self):
        with open(FRAGMENT) as f:
            self.assertNotIn("{{", f.read())

    def test_the_sidecar_is_the_only_init_container_and_restarts_always(self):
        (side,) = self.pod["initContainers"]
        self.assertEqual(side["name"], "log-sidecar")
        self.assertEqual(side["restartPolicy"], "Always")
        self.assertEqual(side["image"], self.values["image"])
        self.assertEqual(side["args"], ["-c", f"{MOUNT}/fluent-bit.yaml"])
        self.assertEqual(side["resources"], self.values["resources"])
        sc = side["securityContext"]
        self.assertIs(sc["readOnlyRootFilesystem"], True)
        self.assertIs(sc["allowPrivilegeEscalation"], False)
        self.assertEqual(sc["capabilities"]["drop"], ["ALL"])
        self.assertIs(sc["runAsNonRoot"], True)
        self.assertNotIn("ports", side)

    def test_polaris_container_is_unchanged_by_the_fragment(self):
        (main,) = self.pod["containers"]
        self.assertEqual(main["name"], "polaris")
        self.assertIn("/deployments/logs", [m["mountPath"] for m in main["volumeMounts"]])

    def test_the_claim_is_not_clobbered_and_the_configmap_is_mounted(self):
        volumes = {v["name"]: v for v in self.pod["volumes"]}
        with open(release.POLARIS_VALUES) as f:
            lakehouse = yaml.safe_load(f)
        for v in lakehouse["extraVolumes"]:
            self.assertIn(v, self.pod["volumes"])
        (side,) = self.pod["initContainers"]
        (mount,) = side["volumeMounts"]
        self.assertEqual((mount["mountPath"], mount["readOnly"]), (MOUNT, True))
        self.assertEqual(volumes[mount["name"]]["configMap"]["name"], self.values["configMapName"])

    def test_no_log_file_for_this_path(self):
        (side,) = self.pod["initContainers"]
        self.assertEqual(len(side["volumeMounts"]), 1)
        with open(release.POLARIS_VALUES) as f:
            lakehouse = yaml.safe_load(f)
        added = [v for v in self.fragment["extraVolumes"] if v not in lakehouse["extraVolumes"]]
        self.assertEqual([list(set(v) - {"name"}) for v in added], [["configMap"]])
        for v in added:
            for kind in ("emptyDir", "persistentVolumeClaim", "hostPath"):
                self.assertNotIn(kind, v)

    def test_the_quarkus_keys_meet_the_sidecar(self):
        # UNVERIFIED on Polaris 1.6.0: the key names are Quarkus's, proved by L3's gate.
        p, v = properties(self.docs), self.values["syslog"]
        self.assertEqual(p["quarkus.log.syslog.enabled"], "true")
        self.assertEqual(p["quarkus.log.syslog.endpoint"], f"localhost:{v['port']}")
        self.assertEqual(p["quarkus.log.syslog.protocol"], "tcp")
        self.assertEqual(p["quarkus.log.syslog.syslog-type"], "rfc5424")
        self.assertEqual(p["quarkus.log.syslog.use-counting-framing"], "true")
        self.assertEqual(p["quarkus.log.syslog.json.enabled"], "true")
        self.assertEqual(p["quarkus.log.syslog.max-length"], str(v["maxLength"]))
        self.assertEqual(p["quarkus.log.syslog.async.enabled"], "true")
        self.assertEqual(p["quarkus.log.syslog.async.queue-length"], str(v["queueLength"]))
        self.assertEqual(p["quarkus.log.syslog.async.overflow"], "discard")
        self.assertEqual(fluent_bit()["pipeline"]["inputs"][0]["port"], v["port"])
        self.assertIn("quarkus.log.file.enabled", p, "the file appender stays until L7")

    def test_the_fragment_and_the_chart_agree_on_the_names(self):
        for k in ("image",):
            self.assertIn(self.values[k], json.dumps(self.fragment))
        self.assertIn(self.values["configMapName"], json.dumps(self.fragment))


class Untouched(unittest.TestCase):
    def test_upstream_chart_and_the_lakehouse_values_are_unchanged(self):
        if not os.path.isdir(os.path.join(ROOT, ".git")) or not shutil.which("git"):
            self.skipTest("not a git checkout")
        base = subprocess.run(["git", "-C", ROOT, "rev-parse", "--verify", "-q", "origin/main"],
                              capture_output=True, text=True)
        if base.returncode != 0:
            self.skipTest("no origin/main to compare with")
        diff = subprocess.run(["git", "-C", ROOT, "diff", "--quiet", "origin/main", "--",
                               "charts/polaris"], check=False)
        self.assertEqual(diff.returncode, 0, "charts/polaris differs from origin/main")

    def test_nothing_installs_it_yet(self):
        # L4 adds the install step and flips this test in its own commit.
        with open(os.path.join(ROOT, "install", "release.py")) as f:
            self.assertNotIn(NAME, f.read())


class Kubeconform(unittest.TestCase):
    def test_the_configmap(self):
        out = subprocess.run(["helm", "template", NAME, CHART, "--namespace", NAMESPACE],
                             capture_output=True, text=True, check=True).stdout
        result = kubeconform(out)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_merged_polaris_deployment(self):
        text = yaml.safe_dump(one(polaris_docs(), "Deployment"))
        result = kubeconform(text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
