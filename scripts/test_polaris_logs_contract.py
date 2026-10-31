"""Tests for contract v3 of the Polaris logs (docs/contracts/polaris-logs.md, ADR-081, ADR-082).
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
# images/polaris-log-batch/polaris_log_batch.py:262-266 and :649-696 (no counters, no hold, no
# redaction: ADR-082 Decision 3). Its patterns are copied, so removing the batch (L7) does not
# break this test.
ACCESS_LOGGER = "io.quarkus.http.access-log"
ACCESS_RE = re.compile(r'^(\S+) \S+ (\S+) \[[^\]]*\] "([A-Z]+) (\S+)[^"]*" (\d+) (\S+)')
APP_ALLOW = frozenset({"org.apache.polaris.service.exception.IcebergExceptionMapper",
                       "org.apache.polaris.service.admin.PolarisServiceImpl"})
COMMIT_LOGGER = "org.apache.polaris.service.catalog.iceberg.LocalIcebergCatalog"
KEEP_METHODS = frozenset({"PUT", "DELETE", "PATCH"})
READ_METHODS = frozenset({"GET", "HEAD"})

V3_FILES = ["routing.json", "candidate-line.schema.json", "error-line.schema.json",
            "aggregate.schema.json", "aggregate-template.json", "candidate-template.json",
            "error-template.json", "candidate-ism.json", "error-ism.json", "transform.json"]
METRICS = {"lines", "response_bytes"}
GROUPS = {"window_start", "hostName", "level", "http_method", "http_status", "user_principal_name",
          "api_path"}
OPTIONAL_GROUPS = GROUPS - {"window_start"}
POLICY_IDS = {"1", "2a", "2b", "2c", "3'", "3", "4", "5", "-", "6", "7"}
GONE_RECORD_FIELDS = {"event_id", "index_class", "policy_rule", "policy", "resource",
                      "resource_kind", "api_kind", "status_class", "secret_redacted",
                      "access_log_parse_error"}
GONE_AGGREGATE = {"min_record_time", "max_record_time", "index_class", "resource", "resource_kind",
                  "api_kind", "status_class", "errors_kept"}
ACCESS_FIELDS = ("client_ip", "user_principal_name", "http_method", "api_path", "http_status",
                 "response_size")
FILTERS = {"parser", "grep", "rewrite_tag"}
INTERMEDIATE_TAGS = {"polaris.access", "polaris.post"}
FINAL_TAG = re.compile(r"^polaris\.(error|candidate)\.[a-z0-9]+$")
SOURCE_ALIAS = "polaris-logs-source"


def v3(name):
    with open(os.path.join(CONTRACTS, f"polaris-logs-v3-{name}"), encoding="utf-8") as f:
        return json.load(f)


def route(line):
    """(rule, route) of a Polaris line: the rules of policy-v5, first match."""
    rec = line
    if rec.get("level") in ("ERROR", "WARN"):
        return "1", "error"
    logger = rec.get("loggerName")
    if logger is None:  # a line whose JSON did not parse has no loggerName
        return "7", "error"
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


def adr_082_rows():
    """The rows of Decision 2's table of ADR-082: (id, filter cell), with `\\|` unescaped."""
    [path] = glob.glob(os.path.join(ROOT, "docs", "adr", "082-*.md"))
    with open(path, encoding="utf-8") as f:
        text = f.read()
    section = text.split("### 2. Policy v5", 1)[1].split("### 3.", 1)[0]
    rows = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip().replace("\\|", "|")
                 for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if len(cells) == 4 and cells[0] != "Rule" and not set(cells[0]) <= set("-"):
            rows.append((cells[0], cells[2]))
    return rows


def stage_text(stage):
    if stage["filter"] == "grep":
        return f"Exclude {stage['key']} {stage['regex']}"
    return f"${stage['key']} {stage['regex']}"


class Router(unittest.TestCase):
    def rows(self):
        return v3("routing.json")["rows"]

    def test_rules_is_gone_and_rows_follow_adr_082(self):
        routing = v3("routing.json")
        self.assertNotIn("rules", routing)
        self.assertEqual([r["id"] for r in self.rows()], [i for i, _ in adr_082_rows()])
        self.assertEqual(len(self.rows()), 12)

    def test_every_stage_is_in_its_adr_row(self):
        for row, (_, cell) in zip(self.rows(), adr_082_rows()):
            with self.subTest(row["id"]):
                self.assertTrue(row["stages"])
                for st in row["stages"]:
                    self.assertIn(st["filter"], FILTERS)
                    self.assertIn(f"`{st['filter']}`", cell)
                    if st["filter"] == "grep" or f"${st['key']} " in cell:
                        self.assertIn(stage_text(st), cell)
                    self.assertNotIn("*", st["match"])
                    self.assertRegex(st["match"], r"^polaris(\.[a-z0-9]+)*$")
                    self.assertIn(st["action"], ("rewrite", "exclude", "parse"))
                    self.assertEqual(st["filter"] == "parser", st["regex"] is None)
                tos = [st["to"] for st in row["stages"]]
                self.assertEqual(any(t and t.startswith("polaris.error.") for t in tos),
                                 "error tag" in cell)
                self.assertEqual(any(t and t.startswith("polaris.candidate.") for t in tos),
                                 "candidate tag" in cell)
                self.assertEqual("dropped" in tos, "Exclude" in cell)

    def test_only_built_in_filters_and_exact_tags(self):
        final = set()
        for row in self.rows():
            for st in row["stages"]:
                self.assertIn(st["filter"], FILTERS)
                to = st["to"]
                if to in (None, "dropped") or to in INTERMEDIATE_TAGS:
                    continue
                self.assertRegex(to, FINAL_TAG)
                final.add(to)
        self.assertEqual(final, set(v3("routing.json")["tags"].values()))

    def test_tags_and_dropped_cover_the_policy_rules(self):
        routing = v3("routing.json")
        self.assertEqual(set(routing["tags"]) | set(routing["dropped"]), POLICY_IDS)
        self.assertEqual(set(routing["tags"]) & set(routing["dropped"]), set())
        self.assertEqual(sorted(routing["tags"].values()), sorted([
            "polaris.candidate.r2b", "polaris.candidate.r3p", "polaris.candidate.r4",
            "polaris.candidate.r5", "polaris.error.r1", "polaris.error.r3", "polaris.error.r7"]))

    def test_every_rule_has_an_example(self):
        seen = {e["rule"] for e in v3("routing.json")["examples"]}
        self.assertEqual(seen, POLICY_IDS)

    def test_every_example_routes_as_declared(self):
        routing = v3("routing.json")
        for ex in routing["examples"]:
            with self.subTest(ex["name"]):
                self.assertEqual(route(ex["input"]), (ex["rule"], ex["route"]))
                if ex["route"] == "dropped":
                    self.assertIsNone(ex["tag"])
                    self.assertIn(ex["rule"], routing["dropped"])
                else:
                    want = ("polaris.error.r7" if ex.get("folded") else routing["tags"][ex["rule"]])
                    self.assertEqual(ex["tag"], want)
                    self.assertEqual(ex["tag"].split(".")[1], ex["route"])

    def test_a_failed_change_is_an_error_line_not_a_candidate(self):
        by_name = {e["name"]: e for e in v3("routing.json")["examples"]}
        self.assertEqual(route(by_name["PUT 409"]["input"]), ("3", "error"))
        self.assertEqual(by_name["PUT 409"]["tag"], "polaris.error.r3")

    def test_an_unparsed_access_line_folds_into_r7(self):
        [ex] = [e for e in v3("routing.json")["examples"] if e["name"] == "unparsable access line"]
        self.assertIs(ex["folded"], True)
        self.assertEqual(ex["tag"], "polaris.error.r7")

    def test_a_line_without_loggername_is_r7(self):
        self.assertEqual(route({"message": '{"timestamp":"2026-10-30T19:15:02.53'}), ("7", "error"))


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
                self.assertFalse(set(out) & GONE_RECORD_FIELDS)
                for name in ("index_class", "policy_rule", "policy"):
                    self.assertNotIn(name, out)

    def test_access_fields_are_strings(self):
        [ex] = [e for e in self.kept() if e["name"] == "GET 404"]
        self.assertEqual(ex["output"]["http_status"], "404")
        self.assertEqual({k: ex["output"][k] for k in ACCESS_FIELDS if k != "http_status"},
                         {"client_ip": "172.17.0.1", "user_principal_name": "root",
                          "http_method": "GET",
                          "api_path": "/api/catalog/v1/cat/namespaces/a/tables/t",
                          "response_size": "120"})

    def test_a_dropped_example_has_no_output(self):
        for ex in v3("routing.json")["examples"]:
            if ex["route"] == "dropped":
                self.assertIsNone(ex["output"], ex["name"])

    def test_examples_say_where_they_come_from(self):
        for ex in v3("routing.json")["examples"]:
            self.assertRegex(ex["source"], r"^(constructed|measured 2026-10-11)$", ex["name"])

    def example(self, name):
        [ex] = [e for e in v3("routing.json")["examples"] if e["name"] == name]
        return copy.deepcopy(ex["output"])

    def test_required_fields(self):
        self.assertEqual(set(self.schema("candidate")["required"]),
                         {"timestamp", "loggerName", "level", "hostName"})
        self.assertEqual(set(self.schema("error")["required"]), {"message"})

    def test_no_gone_name_is_a_property(self):
        for kind in ("candidate", "error"):
            props = set(self.schema(kind)["properties"])
            self.assertEqual(props & GONE_RECORD_FIELDS, set(), kind)
            self.assertEqual(props & {"min_record_time", "max_record_time"}, set(), kind)

    def test_gone_record_fields_are_listed(self):
        self.assertEqual(set(v3("routing.json")["gone_record_fields"]), GONE_RECORD_FIELDS)

    def test_a_candidate_without_loggername_is_refused(self):
        out = self.example("GET 404")
        del out["loggerName"]
        self.assertEqual(contract.problems(out, self.schema("candidate")), ["loggerName: required"])

    def test_http_status_is_a_string_of_three_digits(self):
        out = self.example("GET 404")
        out["http_status"] = 404
        self.assertEqual(contract.problems(out, self.schema("candidate")),
                         ["http_status: expected string"])
        out["http_status"] = "4040"
        self.assertEqual(len(contract.problems(out, self.schema("candidate"))), 1)

    def test_response_size_is_digits_or_a_dash(self):
        out = self.example("GET 404")
        out["response_size"] = "-"
        self.assertEqual(contract.problems(out, self.schema("candidate")), [])
        out["response_size"] = "x"
        self.assertEqual(len(contract.problems(out, self.schema("candidate"))), 1)

    def test_unknown_fields_are_accepted(self):
        out = self.example("GET 404")
        out["something_new"] = 1
        for name, value in (("loggerClassName", "org.jboss.logging.Logger"), ("ndc", ""),
                            ("processName", "/usr/lib/jvm/java/bin/java"), ("processId", 1)):
            out[name] = value
        self.assertEqual(contract.problems(out, self.schema("candidate")), [])

    def test_a_cut_record_is_an_error_record(self):
        [ex] = [e for e in self.kept() if e["tag"] == "polaris.error.r7" and not e.get("folded")
                and "loggerName" not in e["output"]]
        self.assertEqual(ex["output"]["message"], '{"timestamp":"2026-10-30T19:15:02.53')
        self.assertEqual(contract.problems(ex["output"], self.schema("error")), [])
        self.assertTrue(contract.problems(ex["output"], self.schema("candidate")))

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
        self.assertLessEqual(GONE_AGGREGATE, set(gone))
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
        self.assertEqual(set(schema["required"]), {"window_start"} | METRICS)
        self.assertIs(schema["additionalProperties"], False)

    def test_the_optional_groups_may_be_missing_or_null(self):
        schema = v3("aggregate.schema.json")
        doc = {"window_start": "2026-10-30T12:00:00.000Z", "lines": 2, "response_bytes": 0}
        self.assertEqual(contract.problems(doc, schema), [])
        doc.update({name: None for name in OPTIONAL_GROUPS})
        self.assertEqual(contract.problems(doc, schema), [])

    def test_window_start_is_required(self):
        doc = dict(v3("routing.json")["aggregate_example"])
        del doc["window_start"]
        self.assertEqual(contract.problems(doc, v3("aggregate.schema.json")),
                         ["window_start: required"])

    def test_counters_are_ranges_of_http_status(self):
        counters = v3("routing.json")["counters"]
        text = json.dumps(counters)
        self.assertNotIn("status_class", text)
        self.assertNotIn("index_class", text)

        def codes(name):
            return {c for lo, hi in counters[name] for c in range(lo, hi + 1)}

        self.assertEqual(codes("not_found"), {404})
        self.assertNotIn(404, codes("errors_4xx"))
        self.assertEqual(codes("errors_4xx"), set(range(400, 500)) - {404})
        self.assertEqual(codes("auth_denied"), {401, 403})
        self.assertLessEqual(codes("auth_denied"), codes("errors_4xx"))
        self.assertEqual(codes("errors_5xx"), set(range(500, 600)))
        self.assertEqual(len(codes("not_found")) + len(codes("errors_4xx")) + len(codes("errors_5xx")),
                         len(codes("not_found") | codes("errors_4xx") | codes("errors_5xx")))


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

    def test_mapping_types_follow_the_schema_types(self):
        for kind in ("candidate", "error", "aggregate"):
            schema = v3("aggregate.schema.json" if kind == "aggregate"
                        else f"{kind}-line.schema.json")
            props = v3(f"{kind}-template.json")["template"]["mappings"]["properties"]
            for name, spec in schema["properties"].items():
                got = props[name]["type"]
                types = spec["type"] if isinstance(spec.get("type"), list) else [spec.get("type")]
                if name in ("timestamp", "window_start"):
                    want = "date"
                elif kind != "aggregate" and name in ("http_status", "response_size"):
                    want = "long"  # a string in the record, coerced; the dash is not indexed
                elif "integer" in types:
                    want = "long"
                elif spec.get("type") == "object":
                    want = "object"
                elif name == "message":
                    want = "text"
                else:
                    want = "keyword"
                self.assertEqual(got, want, f"{kind}.{name}")

    def test_response_size_ignores_a_malformed_value(self):
        for kind in ("candidate", "error"):
            props = v3(f"{kind}-template.json")["template"]["mappings"]["properties"]
            self.assertIs(props["response_size"]["ignore_malformed"], True, kind)

    def test_no_template_maps_a_gone_name(self):
        for kind in ("candidate", "error", "aggregate"):
            props = set(v3(f"{kind}-template.json")["template"]["mappings"]["properties"])
            self.assertEqual(props & (GONE_RECORD_FIELDS | GONE_AGGREGATE), set(), kind)

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
        targets, kinds = set(), []
        for g in t["groups"]:
            [(kind, body)] = g.items()
            kinds.append(kind)
            targets.add(body["target_field"])
            self.assertEqual(body["source_field"],
                             "timestamp" if kind == "date_histogram" else body["target_field"])
            if kind == "date_histogram":
                self.assertEqual(body["fixed_interval"], "1h")
                self.assertEqual(body["target_field"], "window_start")
        self.assertEqual(targets, GROUPS)
        self.assertEqual(sorted(kinds), ["date_histogram"] + ["terms"] * 6)
        self.assertEqual(set(t["aggregations"]), METRICS)
        for name, agg in t["aggregations"].items():
            [(func, body)] = agg.items()
            self.assertIn(func, ("value_count", "sum"), name)
            self.assertNotEqual(body["field"], "event_id", name)
        self.assertEqual(t["aggregations"]["lines"], {"value_count": {"field": "timestamp"}})
        props = v3("aggregate-template.json")["template"]["mappings"]["properties"]
        for name in METRICS:
            self.assertEqual(props[name]["type"], "long")
        self.assertNotIn("date", {props[n]["type"] for n in METRICS})


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
        self.assertIn("ADR-082", self.md().split("## Versions")[-1])

    def v3_text(self):
        return self.md().split("# Contract v2 (superseded", 1)[0]

    def test_v3_says_what_was_measured(self):
        text = self.v3_text()
        self.assertNotIn("assumed, not measured", text)
        for key in ("hostName", "sequence", "mdc", "loggerName", "loggerClassName", "level",
                    "message", "ndc", "processId", "processName", "threadId", "threadName",
                    "timestamp"):
            self.assertIn(f"`{key}`", text)
        for word in ("nanosecond", "BOM", "max-length", "app-name", "165"):
            self.assertIn(word, text)
        self.assertNotIn("equal to ADR-081", text)
        self.assertNotIn("tasks#", self.md())

    def test_v3_names_every_tag_and_r7(self):
        text = self.v3_text()
        for tag in v3("routing.json")["tags"].values():
            self.assertIn(tag, text)
        self.assertIn("polaris.error.r7", text)

    def test_derived_fields_are_left_to_opensearch(self):
        text = self.v3_text()
        heading = "Derived by OpenSearch, or not done"
        self.assertIn(f"\n## {heading}", text)
        section = text.split(heading, 1)[1].split("\n## ", 1)[0]
        for name in ("resource", "resource_kind", "api_kind", "status_class", "event_id", "L0b"):
            self.assertIn(name, section)

    def test_gone_names_only_in_a_gone_paragraph(self):
        paragraphs = [p for p in re.split(r"\n\s*\n", self.v3_text())]
        rest = "\n\n".join(p for p in paragraphs if not p.startswith("**Gone"))
        for name in ("min_record_time", "max_record_time", "policy_rule"):
            self.assertNotIn(name, rest)
        self.assertTrue(any(p.startswith("**Gone") and "policy_rule" in p for p in paragraphs))

    def test_v3_schema_descriptions_are_not_assumptions(self):
        for name in ("candidate-line.schema.json", "error-line.schema.json",
                     "aggregate.schema.json"):
            description = v3(name)["description"]
            self.assertNotIn("assumed", description, name)
            self.assertNotIn("NOT KNOWN", description, name)


if __name__ == "__main__":
    unittest.main()
