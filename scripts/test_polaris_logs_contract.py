"""Tests for contract v3 of the Polaris logs (docs/contracts/polaris-logs.md, ADR-081).
Run: python3 -m unittest discover -s scripts"""

import copy
import fnmatch
import glob
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONTRACTS = os.path.join(ROOT, "docs", "contracts")
sys.path.insert(0, os.path.join(ROOT, "gates"))
import contract  # noqa: E402

# The reference router below is a stateless port of AuditPolicy._decide in
# images/polaris-log-batch/polaris_log_batch.py:262-266 and :649-696 (no counters, no hold).
# Its patterns are copied, so removing the batch (L7) does not break this test.
ACCESS_LOGGER = "io.quarkus.http.access-log"
ACCESS_RE = re.compile(r'^(\S+) \S+ (\S+) \[[^\]]*\] "([A-Z]+) (\S+)[^"]*" (\d+) (\S+)')
APP_ALLOW = frozenset({"org.apache.polaris.service.exception.IcebergExceptionMapper",
                       "org.apache.polaris.service.admin.PolarisServiceImpl"})
COMMIT_LOGGER = "org.apache.polaris.service.catalog.iceberg.LocalIcebergCatalog"
SECRET_RE = re.compile(r"(clientSecret:\s*)(\S+)")
KEEP_METHODS = frozenset({"PUT", "DELETE", "PATCH"})
READ_METHODS = frozenset({"GET", "HEAD"})

V3_FILES = ["routing.json", "candidate-line.schema.json", "error-line.schema.json",
            "aggregate.schema.json", "aggregate-template.json", "candidate-template.json",
            "error-template.json", "candidate-ism.json", "error-ism.json", "transform.json"]
METRICS = {"lines", "response_bytes", "min_record_time", "max_record_time"}
GROUPS = {"window_start", "hostName", "index_class", "resource", "resource_kind", "api_kind",
          "user_principal_name", "status_class", "level"}
SOURCE_ALIAS = "polaris-logs-source"


def v3(name):
    with open(os.path.join(CONTRACTS, f"polaris-logs-v3-{name}"), encoding="utf-8") as f:
        return json.load(f)


def route(line):
    """(rule, route) of a Polaris line: the redaction, then the rules of policy-v5, first match."""
    rec = copy.deepcopy(line)
    msg = rec.get("message")
    if isinstance(msg, str):
        rec["message"] = SECRET_RE.sub(lambda m: m.group(1) + "<redacted>", msg)
    if rec.get("level") in ("ERROR", "WARN"):
        return "1", "error"
    logger = rec.get("loggerName")
    if logger != ACCESS_LOGGER:
        if logger == COMMIT_LOGGER:
            return "2a", "dropped"
        if logger in APP_ALLOW:
            return "2b", "candidate"
        return "2c", "dropped"
    m = ACCESS_RE.match(rec["message"]) if isinstance(rec.get("message"), str) else None
    if m is None:
        return "3", "error"
    method, path, status = m.group(3), m.group(4), int(m.group(5))
    if status == 404:
        return "3'", "candidate"
    if status >= 400:
        return "3", "error"
    if method in KEEP_METHODS:
        return "4", "candidate"
    if method == "POST":
        return ("5", "candidate") if path.startswith("/api/management/") else ("-", "dropped")
    if method in READ_METHODS:
        return "6", "dropped"
    return "7", "error"


def adr_routing():
    """{rule: route} from the Routing table of ADR-081 (the rows between its Decisions 3 and 4)."""
    [path] = glob.glob(os.path.join(ROOT, "docs", "adr", "081-*.md"))
    with open(path, encoding="utf-8") as f:
        text = f.read()
    section = text.split("### 3. Routing", 1)[1].split("### 4.", 1)[0]
    rules = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 3 and re.fullmatch(r"1|2a|2b|2c|3'|3|4|5|-|6|7",
                                                                   cells[0]):
            rules[cells[0]] = cells[2].split()[0]
    return rules


class Router(unittest.TestCase):
    def test_table_equals_adr_081(self):
        # when an ADR amends the table this fails on purpose: update the contract to follow
        self.assertEqual(adr_routing(), v3("routing.json")["rules"])
        self.assertEqual(v3("routing.json")["rules"], {
            "1": "error", "2a": "dropped", "2b": "candidate", "2c": "dropped", "3'": "candidate",
            "3": "error", "4": "candidate", "5": "candidate", "-": "dropped", "6": "dropped",
            "7": "error"})

    def test_every_rule_has_an_example(self):
        seen = {e["rule"] for e in v3("routing.json")["examples"]}
        self.assertEqual(seen, set(v3("routing.json")["rules"]))

    def test_every_example_routes_as_declared(self):
        for ex in v3("routing.json")["examples"]:
            with self.subTest(ex["name"]):
                self.assertEqual(route(ex["input"]), (ex["rule"], ex["route"]))

    def test_a_failed_change_is_an_error_line_not_a_candidate(self):
        by_name = {e["name"]: e for e in v3("routing.json")["examples"]}
        self.assertEqual(route(by_name["PUT 409"]["input"]), ("3", "error"))

    def test_redaction_runs_before_the_rules(self):
        line = {"level": "ERROR", "loggerName": "x", "message": "clientSecret: abc123"}
        self.assertEqual(route(line), ("1", "error"))
        line = {"level": "INFO", "loggerName": ACCESS_LOGGER, "message": "clientSecret: abc"}
        self.assertEqual(route(line), ("3", "error"))  # no access line parses: rule 3


class Shapes(unittest.TestCase):
    def schema(self, kind):
        return v3(f"{kind}-line.schema.json")

    def kept(self):
        return [e for e in v3("routing.json")["examples"] if e["route"] != "dropped"]

    def test_every_kept_example_holds_its_shape(self):
        for ex in self.kept():
            with self.subTest(ex["name"]):
                out = ex["output"]
                self.assertEqual(contract.problems(out, self.schema(ex["route"])), [])
                self.assertEqual(out["index_class"], ex["route"])
                self.assertEqual(out["policy_rule"], ex["rule"])
                self.assertEqual(out["policy"], "policy-v5")

    def test_a_dropped_example_has_no_output(self):
        for ex in v3("routing.json")["examples"]:
            if ex["route"] == "dropped":
                self.assertIsNone(ex["output"], ex["name"])

    def test_examples_say_where_they_come_from(self):
        for ex in v3("routing.json")["examples"]:
            self.assertRegex(ex["source"], r"^(constructed|measured 2026-10-11)$", ex["name"])

    def test_status_class_follows_the_status(self):
        def want(status):
            return ("ok" if status < 400 else str(status) if status in (401, 403, 404)
                    else "4xx" if status < 500 else "5xx")

        for ex in self.kept():
            out = ex["output"]
            if "http_status" in out:
                self.assertEqual(out["status_class"], want(out["http_status"]), ex["name"])
            elif out.get("access_log_parse_error"):
                self.assertEqual(out["status_class"], "unparsed", ex["name"])
            else:
                self.assertEqual(out["status_class"], "none", ex["name"])

    def example(self, name):
        [ex] = [e for e in v3("routing.json")["examples"] if e["name"] == name]
        return copy.deepcopy(ex["output"])

    def test_candidate_schema_refuses_an_error_line(self):
        out = self.example("GET 404")
        out["index_class"] = "error"
        self.assertIn("index_class: expected 'candidate'",
                      contract.problems(out, self.schema("candidate")))

    def test_error_schema_refuses_a_candidate_rule(self):
        out = self.example("GET 401")
        out["policy_rule"] = "3'"
        [problem] = contract.problems(out, self.schema("error"))
        self.assertTrue(problem.startswith("policy_rule: not one of ["), problem)

    def test_event_id_is_required(self):
        out = self.example("GET 404")
        del out["event_id"]
        self.assertEqual(contract.problems(out, self.schema("candidate")), ["event_id: required"])

    def test_status_class_is_an_enum(self):
        out = self.example("GET 404")
        out["status_class"] = "404x"
        self.assertEqual(len(contract.problems(out, self.schema("candidate"))), 1)

    def test_unknown_fields_are_accepted(self):
        out = self.example("GET 404")
        out["something_new"] = 1
        self.assertEqual(contract.problems(out, self.schema("candidate")), [])

    def test_v2_only_fields_are_not_required(self):
        for kind in ("candidate", "error"):
            required = set(self.schema(kind)["required"])
            self.assertFalse(required & {"log_hour", "held_orphan"}, kind)


class Aggregate(unittest.TestCase):
    def test_example_holds(self):
        self.assertEqual(contract.problems(v3("routing.json")["aggregate_example"],
                                           v3("aggregate.schema.json")), [])

    def test_gone_names_are_refused(self):
        gone = v3("routing.json")["gone"]
        self.assertIn("counted_404", gone)
        for name in gone:
            with self.subTest(name):
                doc = dict(v3("routing.json")["aggregate_example"], **{name: 0})
                self.assertEqual(contract.problems(doc, v3("aggregate.schema.json")),
                                 [f"{name}: not allowed"])

    def test_no_property_is_gone(self):
        props = set(v3("aggregate.schema.json")["properties"])
        self.assertEqual(props & set(v3("routing.json")["gone"]), set())

    def test_group_and_metric_fields(self):
        schema = v3("aggregate.schema.json")
        self.assertEqual(set(schema["properties"]), GROUPS | METRICS)
        self.assertEqual(set(schema["required"]), GROUPS | METRICS)
        self.assertIs(schema["additionalProperties"], False)

    def test_window_start_is_required(self):
        doc = dict(v3("routing.json")["aggregate_example"])
        del doc["window_start"]
        self.assertEqual(contract.problems(doc, v3("aggregate.schema.json")),
                         ["window_start: required"])

    def test_counters_are_sums_over_status_class(self):
        counters = {k: set(v) for k, v in v3("routing.json")["counters"].items()}
        self.assertEqual(counters["errors_4xx"], {"401", "403", "4xx"})
        self.assertNotIn("404", counters["errors_4xx"])
        self.assertEqual(counters["not_found"], {"404"})
        self.assertEqual(counters["auth_denied"], {"401", "403"})
        self.assertEqual(counters["errors_5xx"], {"5xx"})
        classes = set(v3("candidate-line.schema.json")["properties"]["status_class"]["enum"])
        for name, members in counters.items():
            self.assertLessEqual(members, classes, name)


class Indices(unittest.TestCase):
    NAMES = {"candidate": "polaris-logs-candidate-2026.10.30",
             "error": "polaris-logs-error-2026.10.30",
             "aggregate": "polaris-logs-aggregate"}

    def test_every_file_parses(self):
        for name in V3_FILES:
            v3(name)

    def test_templates_map_every_property_and_refuse_the_rest(self):
        for kind in ("candidate", "error", "aggregate"):
            with self.subTest(kind):
                schema = v3("aggregate.schema.json" if kind == "aggregate"
                            else f"{kind}-line.schema.json")
                tpl = v3(f"{kind}-template.json")
                mappings = tpl["template"]["mappings"]
                self.assertIs(mappings["dynamic"], False)
                self.assertEqual(set(schema["properties"]) - set(mappings["properties"]), set())
                self.assertEqual(mappings["_meta"]["contract_version"], 3)

    def test_line_templates_carry_the_alias(self):
        for kind in ("candidate", "error"):
            self.assertIn(SOURCE_ALIAS, v3(f"{kind}-template.json")["template"]["aliases"])
        self.assertNotIn("aliases", v3("aggregate-template.json")["template"])

    def test_patterns_do_not_overlap(self):
        for kind, patterns in ((k, v3(f"{k}-template.json")["index_patterns"])
                               for k in self.NAMES):
            for other, name in self.NAMES.items():
                match = any(fnmatch.fnmatch(name, p) for p in patterns)
                self.assertEqual(match, kind == other, f"{kind} pattern vs {other} index")

    def test_ism_ages_and_patterns(self):
        for kind, age in (("candidate", "3d"), ("error", "30d")):
            with self.subTest(kind):
                policy = v3(f"{kind}-ism.json")["policy"]
                [tpl] = policy["ism_template"]
                self.assertEqual(tpl["index_patterns"],
                                 v3(f"{kind}-template.json")["index_patterns"])
                states = {s["name"]: s for s in policy["states"]}
                self.assertEqual(policy["default_state"], "hot")
                [step] = states["hot"]["transitions"]
                self.assertEqual(step["state_name"], "delete")
                self.assertEqual(step["conditions"]["min_index_age"], age)
                self.assertEqual(states["delete"]["actions"], [{"delete": {}}])

    def test_aggregate_has_no_ism(self):
        self.assertFalse(os.path.exists(os.path.join(CONTRACTS, "polaris-logs-v3-aggregate-ism.json")))

    def test_transform_reads_the_alias_and_writes_the_aggregate(self):
        t = v3("transform.json")["transform"]
        self.assertEqual(t["source_index"], SOURCE_ALIAS)
        self.assertIs(t["continuous"], True)
        self.assertIn(t["target_index"], v3("aggregate-template.json")["index_patterns"])
        for kind in ("candidate", "error"):  # the feedback trap
            for p in v3(f"{kind}-template.json")["index_patterns"]:
                self.assertFalse(fnmatch.fnmatch(t["target_index"], p))
        self.assertFalse(fnmatch.fnmatch(t["target_index"], "polaris-logs-source"))

    def test_transform_groups_and_aggregations_equal_the_aggregate(self):
        t = v3("transform.json")["transform"]
        targets = set()
        for g in t["groups"]:
            [(kind, body)] = g.items()
            targets.add(body["target_field"])
            if kind == "date_histogram":
                self.assertEqual(body["source_field"], "timestamp")
                self.assertEqual(body["fixed_interval"], "1h")
                self.assertEqual(body["target_field"], "window_start")
        self.assertEqual(targets, GROUPS)
        self.assertEqual(set(t["aggregations"]), METRICS)


class Document(unittest.TestCase):
    def md(self):
        with open(os.path.join(CONTRACTS, "polaris-logs.md"), encoding="utf-8") as f:
            return f.read()

    def test_v3_comes_before_the_superseded_v2(self):
        text = self.md().lower()
        self.assertGreaterEqual(text.find("contract v3"), 0)
        self.assertLess(text.find("contract v3"), text.find("contract v2 (superseded"))

    def test_v2_schemas_are_untouched(self):
        for name in ("processed", "summary", "resource", "principal", "app-dropped"):
            self.assertTrue(os.path.exists(
                os.path.join(CONTRACTS, f"polaris-logs-{name}.schema.json")), name)
        self.assertEqual(contract.load("processed")["title"],
                         "Polaris log contract v2: one processed line")

    def test_files_and_the_document_name_each_other(self):
        named = set(re.findall(r"polaris-logs-v3-[a-z-]+(?:\.schema)?\.json", self.md()))
        on_disk = {os.path.basename(p)
                   for p in glob.glob(os.path.join(CONTRACTS, "polaris-logs-v3-*"))}
        self.assertEqual(named, on_disk)
        self.assertEqual(on_disk, {f"polaris-logs-v3-{n}" for n in V3_FILES})

    def test_versions_lists_v3(self):
        self.assertRegex(self.md().split("## Versions")[-1], r"\*\*v3\*\*")


if __name__ == "__main__":
    unittest.main()
