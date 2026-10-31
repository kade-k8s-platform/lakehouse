"""Tests for charts/polaris-log-sidecar (ADR-081, ADR-009, ADR-016): the Fluent Bit sidecar's
ConfigMap and the values fragment that adds it to Polaris as a native sidecar and points
Quarkus's syslog handler at it. Nothing installs the chart yet (L4), so Polaris's own gates are
untouched. The filters are the built-in `parser`, `grep` and `rewrite_tag` only (ADR-082): the
tests check the text of every rule and regex; what Fluent Bit's engine (Onigmo) does with them
is gates/sidecar_smoke.py's.

UNVERIFIED, and proved by the gate that runs the sidecar: that Fluent Bit starts with this YAML
and under this securityContext, that `octet_counting` reads what Quarkus sends, and that
Polaris 1.6.0 accepts the `quarkus.log.syslog.*` keys below (their names are from Quarkus's
logging guide, as ADR-081 lists them).

Run: python3 -m unittest discover -s charts
Needs helm and kubeconform on PATH (`python3 gates/gate.py tools BIN_DIR helm kubeconform`) and
PyYAML (`pip install --require-hashes -r charts/requirements.txt`).
"""

import fnmatch
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
NAMESPACE = "catalog-polaris"
MOUNT = "/fluent-bit/policy"
STAGES = ("access", "post")

# Sizing (plans/polaris-logging/L2b.md): Quarkus's `max-length` bounds the whole frame body,
# header and message ("the length includes the header and the message", its logging guide).
ENVELOPE = 165          # octets of syslog header around the body, measured by L3b (lakehouse#243)
LONGEST_JSON = 40 * 1024  # the contract's measured worst exception line
HEADROOM = 1024         # placeholder for the keys the syslog JSON adds
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


def parser_regex(name):
    """The regex of a parsers.yaml parser, translated for Python's re on UTF-8 bytes."""
    parsers = yaml.safe_load(configmap()["data"]["parsers.yaml"])["parsers"]
    (p,) = [p for p in parsers if p["name"] == name]
    text = p["regex"].replace("(?<", "(?P<").replace("\\x{FEFF}", "\\xEF\\xBB\\xBF")
    return re.compile(text.encode())


def expected_filters(prefix, limit):
    """ADR-082's twelve rows as fifteen filters, in the ADR's order: (name, match, settings)."""
    tag, access, post = prefix, f"{prefix}.access", f"{prefix}.post"
    err, cand = f"{prefix}.error", f"{prefix}.candidate"

    def rw(match, em, rule, to):
        return ("rewrite_tag", match, {"rule": f"{rule} {to} false", "emitter_name": f"e_{em}",
                                       "emitter_mem_buf_limit": limit})

    def gr(match, what):
        return ("grep", match, {"exclude": what})
    return [
        ("parser", tag, {"key_name": "message", "parser": "polaris-json",
                         "reserve_data": False, "preserve_key": False}),
        rw(tag, "r1", "$level ^(ERROR|WARN)$", f"{err}.r1"),
        rw(tag, "access", "$loggerName ^io\\.quarkus\\.http\\.access-log$", access),
        rw(tag, "r2b", "$loggerName (^|\\.)(IcebergExceptionMapper|PolarisServiceImpl)$",
           f"{cand}.r2b"),
        gr(tag, "loggerName .+"),
        rw(tag, "r7", "$message .+", f"{err}.r7"),
        ("parser", access, {"key_name": "message", "parser": "polaris-access",
                            "reserve_data": True, "preserve_key": True}),
        rw(access, "r3p", "$http_status ^404$", f"{cand}.r3p"),
        rw(access, "r3", "$http_status ^[45][0-9][0-9]$", f"{err}.r3"),
        rw(access, "r4", "$http_method ^(PUT|DELETE|PATCH)$", f"{cand}.r4"),
        gr(access, "http_method ^(GET|HEAD)$"),
        rw(access, "post", "$http_method ^POST$", post),
        rw(post, "r5", "$api_path ^/api/management/", f"{cand}.r5"),
        gr(post, "api_path .+"),
        rw(access, "r7a", "$message .+", f"{err}.r7a"),
    ]


def simplified(filters):
    out = []
    for f in filters:
        rest = {k: v for k, v in f.items() if k not in ("name", "match")}
        out.append((f["name"], f["match"], rest))
    return out


def check_graph(tc, cfg, prefix):
    """ADR-082's no-loop check, read as plans/polaris-logging/L2c.md reads it."""
    filters = cfg["pipeline"]["filters"]
    (out,) = cfg["pipeline"]["outputs"]
    stages = {f["match"] for f in filters}
    edges = {}
    for f in filters:
        tc.assertTrue(f["match"] == f["match"].strip() and "*" not in f["match"], f["match"])
        if f["name"] != "rewrite_tag":
            continue
        emitted = f["rule"].split()[2]
        tc.assertNotEqual(emitted, f["match"], "a rewrite_tag emits its own Match")
        tc.assertTrue(emitted.startswith(prefix + "."), emitted)
        edges.setdefault(f["match"], set()).add(emitted)
        if emitted in stages:
            continue
        tc.assertTrue(fnmatch.fnmatchcase(emitted, out["match"]), (emitted, out["match"]))
    for stage in stages:
        tc.assertFalse(fnmatch.fnmatchcase(stage, out["match"]), stage)
    # acyclic: a depth-first walk from the input tag never meets a tag on its own path
    def walk(tag, path):
        tc.assertNotIn(tag, path)
        for nxt in edges.get(tag, ()):
            walk(nxt, path | {tag})
    walk(prefix, frozenset())
    tc.assertEqual(out["match"], f"{prefix}.*.*")
    tc.assertEqual(cfg["pipeline"]["inputs"][0]["tag"], prefix)


class Rendered(unittest.TestCase):
    def test_one_configmap_with_exactly_the_two_files(self):
        cm = configmap()
        self.assertEqual(cm["kind"], "ConfigMap")
        self.assertEqual(cm["metadata"]["name"], NAME)
        self.assertEqual(set(cm["data"]), {"fluent-bit.yaml", "parsers.yaml"})

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

    def test_the_new_values_are_required_and_checked(self):
        self.refused("syslog.appName=", key="/syslog/appName")
        self.refused(strings=["syslog.appName=has space"], key="/syslog/appName")
        self.refused(strings=["syslog.appName=" + "a" * 49], key="/syslog/appName")
        self.refused("routing.tagPrefix=a.b", key="/routing/tagPrefix")
        self.refused("routing.emitterMemBufLimit=10", key="/routing/emitterMemBufLimit")
        self.refused("routing=null", key="routing")
        self.refused("syslog.appName=null", key="appName")


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

    def test_sizing(self):
        # Quarkus counts the header in max-length, so the whole frame body is bounded by it; the
        # decimal prefix and its space add at most len(str(maxLength)) + 1 octets. That Fluent Bit
        # reads a 64 KiB frame at this buffer_max_size is not proved here.
        (inp,) = self.cfg["pipeline"]["inputs"]
        size = {"KB": 1024, "MB": 1024 ** 2}

        def octets(v):
            n, unit = re.fullmatch(r"(\d+)(KB|MB)", v).groups()
            return int(n) * size[unit]
        max_length = self.values["syslog"]["maxLength"]
        self.assertGreaterEqual(max_length, LONGEST_JSON + ENVELOPE + HEADROOM)
        self.assertGreater(octets(inp["buffer_max_size"]), max_length + len(str(max_length)) + 1)
        self.assertLessEqual(octets(inp["buffer_chunk_size"]), octets(inp["buffer_max_size"]))

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

    def test_the_image_is_the_smoke_steps_pin(self):
        pin = gate.load_pins()["standin_images"]["fluent_bit_syslog"]["image"]
        self.assertEqual(self.values["image"], pin)

    def test_no_lua(self):
        filters = self.cfg["pipeline"]["filters"]
        self.assertLessEqual({f["name"] for f in filters}, {"parser", "grep", "rewrite_tag"})

        def keys(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from keys(v)
            elif isinstance(node, list):
                for v in node:
                    yield from keys(v)
        bad = {"script", "call", "code", "type_int_key"}
        self.assertEqual(bad & set(keys(self.cfg)), set())
        self.assertFalse(os.path.exists(os.path.join(CHART, "files")))
        cm = configmap()
        for text in cm["data"].values():
            self.assertNotIn("lua", text.lower())

    def test_the_twelve_rows_in_order(self):
        self.assertEqual(simplified(self.cfg["pipeline"]["filters"]),
                         expected_filters("polaris", self.values["routing"]["emitterMemBufLimit"]))

    def test_the_order_dependent_pairs(self):
        rules = [(f["match"], f.get("rule", f.get("exclude", f.get("parser"))))
                 for f in self.cfg["pipeline"]["filters"]]

        def at(text):
            (i,) = [i for i, r in enumerate(rules) if text in str(r[1])]
            return i
        self.assertLess(at("$http_status ^404$"), at("$http_status ^[45][0-9][0-9]$"))
        self.assertLess(at("io\\.quarkus\\.http\\.access-log"), at("IcebergExceptionMapper"))
        self.assertLess(at("IcebergExceptionMapper"), at("loggerName .+"))
        self.assertLess(at("$http_method ^(PUT|DELETE|PATCH)$"), at("http_method ^(GET|HEAD)$"))

    def test_one_rule_each_memory_only(self):
        rewrites = [f for f in self.cfg["pipeline"]["filters"] if f["name"] == "rewrite_tag"]
        self.assertEqual(len(rewrites), 10)
        names = [f["emitter_name"] for f in rewrites]
        self.assertEqual(len(set(names)), len(names))
        for f in rewrites:
            self.assertIsInstance(f["rule"], str)
            fields = f["rule"].split()
            self.assertEqual(len(fields), 4, f["rule"])
            self.assertEqual(fields[3], "false")
            self.assertEqual(f["emitter_mem_buf_limit"], self.values["routing"]["emitterMemBufLimit"])
        text = configmap()["data"]["fluent-bit.yaml"]
        self.assertNotIn("storage", text)

    def test_the_values_reach_the_rewrites(self):
        cfg = fluent_bit("routing.emitterMemBufLimit=2MB")
        for f in cfg["pipeline"]["filters"]:
            if f["name"] == "rewrite_tag":
                self.assertEqual(f["emitter_mem_buf_limit"], "2MB")

    def test_no_loop_no_orphan(self):
        for prefix in ("polaris", "other"):
            check_graph(self, fluent_bit(f"routing.tagPrefix={prefix}"), prefix)

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
        self.assertEqual(by_name["polaris-access"]["format"], "regex")
        self.assertNotIn("types", by_name["polaris-access"])
        self.assertNotIn("time_key", by_name["polaris-access"])

    def test_the_syslog_regex_takes_the_bom(self):
        # L3b measured the BOM on 89 of 89 records. Whether Onigmo takes this spelling is the smoke
        # step's; here the text of the pattern is run by Python's re.
        rx = parser_regex("polaris-syslog")
        body = '{"timestamp":"2026-10-30T19:15:02.536706745Z","level":"INFO"}'
        head = "<14>1 2026-10-30T19:15:02.536+00:00 pod-1 /usr/lib/jvm/java-21-openjdk-21.0.11.0.10-2.el9. 1 - - "
        for bom in (b"\xef\xbb\xbf", b""):
            m = rx.match(head.encode() + bom + body.encode())
            self.assertIsNotNone(m)
            self.assertEqual(m["pri"], b"14")
            self.assertEqual(m["host"], b"pod-1")
            self.assertEqual(m["ident"], b"/usr/lib/jvm/java-21-openjdk-21.0.11.0.10-2.el9.")
            self.assertEqual(m["message"], body.encode())
            self.assertEqual(json.loads(m["message"])["level"], "INFO")
        two = rx.match(head.encode() + b"\xef\xbb\xbf\xef\xbb\xbf" + body.encode())
        self.assertEqual(two["message"][:3], b"\xef\xbb\xbf")  # one BOM is allowed (RFC 5424)

    def test_the_access_regex(self):
        rx = parser_regex("polaris-access")
        m = rx.match(b'172.17.0.1 - - [30/Oct/2026:12:40:00 +0000] '
                     b'"GET /api/management/v1/catalogs HTTP/1.1" 401 -')
        self.assertEqual({k: v.decode() for k, v in m.groupdict().items()},
                         {"client_ip": "172.17.0.1", "user_principal_name": "-",
                          "http_method": "GET", "api_path": "/api/management/v1/catalogs",
                          "http_status": "401", "response_size": "-"})
        m = rx.match(b'10.0.0.2 - root [30/Oct/2026:12:40:00 +0000] '
                     b'"PUT /api/catalog/v1/x/namespaces/n/tables/t?a=b HTTP/1.1" 200 512')
        self.assertEqual(m["http_method"], b"PUT")
        self.assertEqual(m["api_path"], b"/api/catalog/v1/x/namespaces/n/tables/t?a=b")
        self.assertEqual(m["response_size"], b"512")
        self.assertIsNone(rx.match(b"garbage"))

    def test_each_row_regex_takes_its_example_and_refuses_a_near_miss(self):
        def rx(key):
            for f in self.cfg["pipeline"]["filters"]:
                if f["name"] == "rewrite_tag" and f["rule"].split()[0] == key:
                    return f["rule"].split()[1]
        cases = [
            ("$level", ["ERROR", "WARN"], ["ERRORS", "INFO"]),
            ("$loggerName", ["io.quarkus.http.access-log"], ["io.quarkus.http.access-logger"]),
            ("$http_status", ["404", "500"], ["200", "4040"]),
            ("$http_method", ["PUT", "DELETE", "PATCH", "POST"], ["GETX", "OPTIONS"]),
            ("$api_path", ["/api/management/v1/catalogs"], ["/api/catalog/"]),
        ]
        for key, ok, bad in cases:
            patterns = [f["rule"].split()[1] for f in self.cfg["pipeline"]["filters"]
                        if f["name"] == "rewrite_tag" and f["rule"].split()[0] == key]
            self.assertTrue(patterns, key)
            for text in ok:
                self.assertTrue(any(re.search(p, text) for p in patterns), (key, text))
            for text in bad:
                self.assertFalse(any(re.search(p, text) for p in patterns), (key, text))
        mapper = [f["rule"].split()[1] for f in self.cfg["pipeline"]["filters"]
                  if f["name"] == "rewrite_tag" and "IcebergExceptionMapper" in f["rule"]]
        (mapper,) = mapper
        for name in ("org.apache.polaris.service.exception.IcebergExceptionMapper",
                     "IcebergExceptionMapper", "org.apache.polaris.service.admin.PolarisServiceImpl"):
            self.assertTrue(re.search(mapper, name), name)
        self.assertFalse(re.search(mapper, "org.x.LocalIcebergCatalog"))
        self.assertFalse(re.search(mapper, "x.NotIcebergExceptionMapper"))
        gets = [f["exclude"].split()[1] for f in self.cfg["pipeline"]["filters"]
                if f["name"] == "grep" and f["exclude"].startswith("http_method")]
        (gets,) = gets
        self.assertTrue(re.search(gets, "GET") and re.search(gets, "HEAD"))
        self.assertFalse(re.search(gets, "GETX"))


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
        # UNVERIFIED on Polaris 1.6.0: L3b set its ten other keys by environment variable only.
        self.assertEqual(p["quarkus.log.syslog.app-name"], v["appName"])
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
