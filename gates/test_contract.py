"""gates/contract.py: checks lines against the log contract's JSON schemas (ADR-024, ADR-036).

The lines below are lines Polaris 1.6.0 wrote (apache/polaris:1.6.0 at the digest in
charts/polaris/values-lakehouse.yaml, run locally with JSON file logging on 2026-10-11); the 401
is the access line a request without a token got, measured the same way. The batch's audit
policy decides which are kept (contract v2).
"""

import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "images", "polaris-log-batch"))
import contract  # noqa: E402
import polaris_log_batch as plb  # noqa: E402

ACCESS = ('{"timestamp":"2026-10-11T12:40:47.753600155Z","sequence":1770,'
          '"loggerClassName":"org.jboss.logging.Logger","loggerName":"io.quarkus.http.access-log",'
          '"level":"INFO","message":"172.17.0.1 - root [11/Oct/2026:12:40:47 +0000] '
          '\\"GET /api/management/v1/catalogs HTTP/1.1\\" 200 15","threadName":"executor-thread-2",'
          '"threadId":31,"mdc":{"requestId":"gate-req-0001","realmId":"POLARIS"},"ndc":"",'
          '"hostName":"polaris-7f4d69c67c-aaaaa","processName":"/usr/lib/jvm/java/bin/java",'
          '"processId":1}')
STARTUP = ('{"timestamp":"2026-10-11T12:40:38.429922859Z","sequence":1754,'
           '"loggerClassName":"org.slf4j.impl.Slf4jLogger",'
           '"loggerName":"org.apache.polaris.persistence.nosql.impl.cache.CaffeineCacheBackend",'
           '"level":"INFO","message":"Initialized persistence cache","threadName":"main",'
           '"threadId":1,"mdc":{},"ndc":"","hostName":"polaris-7f4d69c67c-aaaaa",'
           '"processName":"/usr/lib/jvm/java/bin/java","processId":1}')
DENIED = ('{"timestamp":"2026-10-11T12:40:48.283104402Z","sequence":1771,'
          '"loggerClassName":"org.jboss.logging.Logger","loggerName":"io.quarkus.http.access-log",'
          '"level":"INFO","message":"172.17.0.1 - - [11/Oct/2026:12:40:48 +0000] '
          '\\"GET /api/management/v1/catalogs HTTP/1.1\\" 401 -",'
          '"threadName":"vert.x-eventloop-thread-3","threadId":27,'
          '"mdc":{"requestId":"gate-req-0002","realmId":"POLARIS"},"ndc":"",'
          '"hostName":"polaris-7f4d69c67c-aaaaa","processName":"/usr/lib/jvm/java/bin/java",'
          '"processId":1}')
FAILED = ('{"timestamp":"2026-10-11T12:40:49.100000000Z","sequence":1772,'
          '"loggerClassName":"org.jboss.logging.Logger",'
          '"loggerName":"org.apache.polaris.service.exception.IcebergExceptionMapper",'
          '"level":"ERROR","message":"Unhandled exception","threadName":"executor-thread-2",'
          '"threadId":31,"mdc":{"requestId":"gate-req-0003","realmId":"POLARIS"},"ndc":"",'
          '"hostName":"polaris-7f4d69c67c-aaaaa","processName":"/usr/lib/jvm/java/bin/java",'
          '"processId":1}')
POD = "polaris-7f4d69c67c-aaaaa"


def processed(raw, hour="20261011-12"):
    """The document the audit policy publishes for `raw`, which it must keep."""
    rec, ts = plb.parse_line(raw.encode())
    entry = {"rec": rec, "ts": ts, "raw": raw.encode(), "pod": POD, "file": "f", "line_no": 1}
    [doc], _, _, _ = plb.AuditPolicy().apply([entry], [entry], plb.hour_from_key(hour), hour)
    return doc


def batch_hour(*raws):
    """Every row and processed line the batch publishes for one hour of `raws`."""
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)
        (d / f"polaris-{POD}.log").write_text("".join(r + "\n" for r in raws))
        when = plb.parse_ts("2026-10-11T12:41:00Z").timestamp()
        os.utime(d / f"polaris-{POD}.log", (when, when))
        plb.run(plb.Config(tmp, pod_lister=lambda: {POD}),
                now=plb.parse_ts("2026-10-11T13:03:00Z"), out=io.StringIO())
        rows = [json.loads(x) for x in
                (d / "aggregated-logs/20261011-12.jsonl").read_text().splitlines()]
        docs = [json.loads(x) for x in
                (d / "processed-logs/20261011-12.jsonl").read_text().splitlines()]
    return rows, docs


class Processed(unittest.TestCase):
    def setUp(self):
        self.schema = contract.load("processed")

    def test_kept_lines_polaris_wrote_pass(self):
        for raw in (DENIED, FAILED):
            self.assertEqual(contract.problems(processed(raw), self.schema), [])

    def test_a_kept_access_line_carries_its_parsed_fields(self):
        doc = processed(DENIED)
        self.assertEqual((doc["http_status"], doc["response_size"], doc["user_principal_name"],
                          doc["api_path"]), (401, 0, "-", "/api/management/v1/catalogs"))
        doc["http_status"] = "401"
        self.assertEqual(contract.problems(doc, self.schema), ["http_status: expected integer"])

    def test_a_field_polaris_writes_must_be_there(self):
        doc = processed(DENIED)
        del doc["mdc"]
        self.assertEqual(contract.problems(doc, self.schema), ["mdc: required"])

    def test_types_are_checked(self):
        doc = processed(DENIED)
        doc["sequence"] = "1770"
        doc["mdc"] = []
        self.assertEqual(sorted(contract.problems(doc, self.schema)),
                         ["mdc: expected object", "sequence: expected integer"])

    def test_event_id_and_log_hour_have_their_shape(self):
        doc = processed(DENIED)
        doc["event_id"] = "not-a-sha1"
        doc["log_hour"] = "2026-10-11-12"
        self.assertEqual(sorted(contract.problems(doc, self.schema)),
                         ["event_id: does not match ^[0-9a-f]{40}$",
                          "log_hour: does not match ^[0-9]{8}-[0-9]{2}$"])

    def test_the_timestamp_is_utc_with_a_z(self):
        doc = processed(DENIED)
        doc["timestamp"] = "2026-10-11T21:40:47.753600155+09:00"
        self.assertEqual(len(contract.problems(doc, self.schema)), 1)

    def test_fields_polaris_adds_later_are_allowed(self):
        doc = processed(FAILED)
        doc["exception"] = {"exceptionType": "java.lang.RuntimeException"}
        self.assertEqual(contract.problems(doc, self.schema), [])

    def test_a_boolean_is_not_an_integer(self):
        doc = processed(DENIED)
        doc["threadId"] = True
        self.assertEqual(contract.problems(doc, self.schema), ["threadId: expected integer"])


class Rows(unittest.TestCase):
    """The aggregated file: a summary row, then resource, principal and app_dropped rows, each
    checked against the schema its report_type names."""

    @classmethod
    def setUpClass(cls):
        cls.rows, cls.docs = batch_hour(ACCESS, DENIED, STARTUP, FAILED, "garbage")

    def test_every_row_and_kept_line_of_a_real_hour_passes(self):
        self.assertEqual([r["report_type"] for r in self.rows],
                         ["summary", "resource", "principal", "principal", "app_dropped"])
        for row in self.rows:
            self.assertEqual(contract.row_problems(row), [], row["report_type"])
        self.assertEqual([d["message"][:20] for d in self.docs],
                         ["172.17.0.1 - - [11/O", "Unhandled exception"])
        for doc in self.docs:
            self.assertEqual(contract.problems(doc, contract.load("processed")), [])

    def test_a_field_no_schema_names_is_refused_in_every_row(self):
        for row in self.rows:
            self.assertEqual(contract.row_problems({**row, "extra": 1}), ["extra: not allowed"],
                             row["report_type"])

    def test_an_unknown_row_type_is_a_problem_not_an_error(self):
        self.assertEqual(contract.row_problems({"report_type": "audit"}),
                         ["report_type: 'audit' is no row type of the contract"])

    def test_every_row_schema_fixes_the_contract_version(self):
        for name in contract.ROW_SCHEMAS.values():
            self.assertEqual(contract.load(name)["properties"]["contract_version"],
                             {"const": 2}, name)


class Validator(unittest.TestCase):
    def test_the_v2_loaders_are_gone(self):
        for name in ("load", "row_problems", "ROW_SCHEMAS", "check_lines"):
            self.assertFalse(hasattr(contract, name), name)
        self.assertTrue(hasattr(contract, "problems"))

    def test_nested_objects_arrays_and_nullable_fields(self):
        schema = {"type": "object", "required": ["a"], "additionalProperties": False,
                  "properties": {"a": {"type": "array",
                                       "items": {"type": "object", "required": ["n"],
                                                 "properties": {"n": {"type": "integer",
                                                                      "minimum": 0}}}},
                                 "b": {"type": ["string", "null"]}}}
        self.assertEqual(contract.problems({"a": [{"n": 1}], "b": None}, schema), [])
        self.assertEqual(contract.problems({"a": [{"n": -1}, {}], "b": 2}, schema),
                         ["a[0].n: below 0", "a[1].n: required", "b: expected string or null"])

    def test_an_unknown_keyword_is_refused_rather_than_ignored(self):
        with self.assertRaises(ValueError):
            contract.problems("x", {"type": "string", "maxLength": 3})

    def test_lines_reports_the_line_number_and_bad_json(self):
        schema = contract.load("processed")
        text = json.dumps(processed(DENIED)) + "\nnot json\n"
        self.assertEqual(contract.check_lines(text, schema),
                         (1, ["line 2: not JSON"]))


if __name__ == "__main__":
    unittest.main()
