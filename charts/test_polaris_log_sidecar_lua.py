"""Tests for charts/polaris-log-sidecar/files/policy_v5.lua (ADR-081): the sidecar's policy-v5
filter, run in lupa's LuaJIT 2.1, the dialect of Fluent Bit's Lua (not Fluent Bit's own build:
the filter in the pinned image is the gate's to run).

Three things are compared: the contract's routing examples (docs/contracts/), the schemas of
the two indices, and the batch's decision (images/polaris-log-batch), which the filter replaces
(ADR-081). The batch comparison drops out with the batch (L7), through the one skip below.

Run: python3 -m unittest discover -s charts
Needs lupa (`pip install --require-hashes -r charts/requirements.txt`).
"""

import copy
import datetime as dt
import itertools
import json
import os
import re
import sys
import unittest

import lupa
from lupa import luajit21

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LUA = os.path.join(HERE, "polaris-log-sidecar", "files", "policy_v5.lua")
BATCH_DIR = os.path.join(ROOT, "images", "polaris-log-batch")
HAVE_BATCH = os.path.exists(os.path.join(BATCH_DIR, "polaris_log_batch.py"))
sys.path.insert(0, os.path.join(ROOT, "gates"))
import contract  # noqa: E402

if HAVE_BATCH:
    sys.path.insert(0, BATCH_DIR)
    import polaris_log_batch as plb  # noqa: E402

DROP = -1
KEEP = 2
HOST = "polaris-7f4d69c67c-aaaaa"
ACCESS = "io.quarkus.http.access-log"
MAPPER = "org.apache.polaris.service.exception.IcebergExceptionMapper"
ADMIN = "org.apache.polaris.service.admin.PolarisServiceImpl"
COMMIT = "org.apache.polaris.service.catalog.iceberg.LocalIcebergCatalog"
OTHER = "org.apache.polaris.persistence.impl.cache.CaffeineCacheBackend"
CAT = "/api/catalog/v1/cat"
MGMT = "/api/management/v1"
TS = 1793364032.7536


with open(os.path.join(ROOT, "docs", "contracts", "polaris-logs-v3-routing.json")) as _f:
    ROUTING = json.load(_f)


def to_python(value):
    """A Lua value as Python: a table with keys 1..n is a list, any other (an empty one too) a
    dict; a number with no fraction an int, which is how the contract writes http_status."""
    if lupa.lua_type(value) == "table":
        items = list(value.items())
        if items and all(isinstance(k, (int, float)) for k, _ in items):
            keys = sorted(int(k) for k, _ in items)
            if keys == list(range(1, len(keys) + 1)):
                return [to_python(v) for _, v in sorted(items, key=lambda kv: kv[0])]
        return {k: to_python(v) for k, v in items}
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


class Filter:
    """The Lua file loaded into a fresh runtime; `__call__` is Fluent Bit's callback."""

    def __init__(self):
        self.lua = luajit21.LuaRuntime(unpack_returned_tuples=True)
        with open(LUA) as f:
            self.source = f.read()
        self.before = set(self.lua.globals().keys())
        self.lua.execute(self.source)
        self.fn = self.lua.globals().polaris_filter

    def global_names(self):
        return set(self.lua.globals().keys())

    def __call__(self, record, ts=TS):
        """(code, record or None): -1 drops; 2 returns the modified record."""
        result = self.fn("polaris", ts, self.lua.table_from(copy.deepcopy(record), recursive=True))
        if result == DROP:
            return DROP, None
        code, out_ts, out = result
        assert out_ts == ts, "the timestamp is kept"
        return code, to_python(out)


def line(logger, level, message, sequence=1, mdc=None, host=HOST,
         timestamp="2026-10-30T12:40:00.000000000Z"):
    rec = {"timestamp": timestamp, "loggerClassName": "org.jboss.logging.Logger",
           "loggerName": logger, "level": level, "message": message,
           "threadName": "executor-thread-2", "threadId": 31, "mdc": mdc or {}, "ndc": "",
           "hostName": host, "processName": "/usr/lib/jvm/java/bin/java", "processId": 1}
    if sequence is not None:
        rec["sequence"] = sequence
    return rec


def access(method, path, status, size="10", user="root", level="INFO", **kw):
    msg = (f'172.17.0.1 - {user} [30/Oct/2026:12:40:00 +0000] "{method} {path} HTTP/1.1" '
           f"{status} {size}")
    return line(ACCESS, level, msg, **kw)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = Filter()


class Examples(Base):
    def test_the_contracts_routing_examples(self):
        self.assertEqual(len(ROUTING["examples"]), 20)
        for ex in ROUTING["examples"]:
            with self.subTest(ex["name"]):
                code, out = self.f(ex["input"])
                if ex["output"] is None:
                    self.assertEqual(code, DROP)
                else:
                    self.assertEqual(code, KEEP)
                    self.assertEqual(out, ex["output"])

    def test_the_client_secret_is_redacted(self):
        (ex,) = [e for e in ROUTING["examples"] if e["name"] == "ERROR with clientSecret"]
        _, out = self.f(ex["input"])
        self.assertEqual(out["message"], "Bad request, clientSecret: <redacted>")
        self.assertIs(out["secret_redacted"], True)

    def test_polaris_own_mask_is_kept(self):
        for mask in ("*", "<redacted>"):
            with self.subTest(mask):
                _, out = self.f(line(MAPPER, "ERROR", f"clientSecret: {mask}"))
                self.assertEqual(out["message"], f"clientSecret: {mask}")
                self.assertNotIn("secret_redacted", out)


def kept_schema_problems(out):
    schema = contract.load(f"v3-{out['index_class']}-line")
    return contract.problems(out, schema)


class Contract(Base):
    def test_every_kept_example_validates_and_routes_by_the_rule_table(self):
        for ex in ROUTING["examples"]:
            if ex["output"] is None:
                continue
            with self.subTest(ex["name"]):
                _, out = self.f(ex["input"])
                self.assertEqual(kept_schema_problems(out), [])
                self.assertEqual(out["index_class"], ROUTING["rules"][out["policy_rule"]])

    def test_every_kept_corpus_line_validates(self):
        for rec in corpus():
            code, out = self.f(rec)
            if code == DROP:
                continue
            with self.subTest(rec["message"][:60], level=rec["level"]):
                self.assertEqual(kept_schema_problems(out), [])
                self.assertEqual(out["index_class"], ROUTING["rules"][out["policy_rule"]])
                self.assertEqual(out["policy"], "policy-v5")


PATHS = (f"{CAT}/namespaces/a/tables/t", f"{MGMT}/principals", f"{MGMT}/principals/p1/rotate")


def corpus():
    """Loggers x levels x methods x statuses x paths, plus the odd lines."""
    out = []
    for logger, level in itertools.product((MAPPER, ADMIN, COMMIT, OTHER),
                                           ("INFO", "WARN", "ERROR")):
        out.append(line(logger, level, f"{logger[-12:]} says something",
                        mdc={"requestId": "r-1", "realmId": "POLARIS"}))
        out.append(line(logger, level, "no request id"))
    for level, method, status, path in itertools.product(
            ("INFO", "WARN", "ERROR"), ("GET", "HEAD", "PUT", "DELETE", "PATCH", "POST", "OPTIONS"),
            (200, 201, 204, 301, 401, 403, 404, 409, 500, 503), PATHS):
        out.append(access(method, path, status, level=level))
    for level in ("INFO", "WARN", "ERROR"):
        out.append(line(ACCESS, level, "not an access line"))
        out.append(line(MAPPER, level, "Failed, clientSecret: hunter2 and more"))
        out.append(access("PUT", f"{CAT}/namespaces/a/tables/t?x=1&y=2", 200, level=level))
        out.append(access("GET", f"{MGMT}/catalogs?x=1", 200, size="-", level=level))
    return out


@unittest.skipUnless(HAVE_BATCH, "the batch is gone (L7): its half of the parity goes with it")
class Parity(Base):
    # Where the sidecar differs from the batch on purpose (ADR-081 Decision 2, Kade 2026-10-30).
    DELTAS = ("an access line with status 404 at INFO: the batch drops it, the sidecar keeps it "
              "for the candidate index (rule 3')",
              "an application line of rule 2b whose request has a 404 access line: the batch "
              "drops it, the sidecar keeps it")

    @staticmethod
    def batch_keeps(rec, status_idx=None):
        report = plb.HourReport(set())
        ts = dt.datetime(2026, 10, 30, 12, 40, tzinfo=dt.timezone.utc)
        return plb.AuditPolicy()._decide(copy.deepcopy(rec), ts, report, status_idx or {})

    def is_delta_i(self, rec):
        m = plb.ACCESS_RE.match(rec["message"]) if rec["loggerName"] == ACCESS else None
        return bool(m) and m.group(5) == "404" and rec["level"] == "INFO"

    def test_kept_in_the_batch_iff_kept_by_the_sidecar_but_the_two_deltas(self):
        seen = 0
        for rec in corpus():
            code, _ = self.f(rec)
            lua_keeps = code != DROP
            batch_keeps = self.batch_keeps(rec)
            if self.is_delta_i(rec):
                seen += 1
                self.assertFalse(batch_keeps, rec["message"])
                self.assertTrue(lua_keeps, rec["message"])
            else:
                self.assertEqual(lua_keeps, batch_keeps,
                                 f"{rec['level']} {rec['loggerName']} {rec['message']}")
        self.assertGreater(seen, 0, "delta (i) is in the corpus")

    def test_delta_ii_an_application_line_of_a_404_request_is_kept(self):
        for logger in (MAPPER, ADMIN):
            rec = line(logger, "INFO", "Full RuntimeException", mdc={"requestId": "r-9"})
            t = dt.datetime(2026, 10, 30, 12, 40, tzinfo=dt.timezone.utc).timestamp()
            with self.subTest(logger):
                self.assertFalse(self.batch_keeps(rec, {"r-9": [(t, 404)]}))
                code, out = self.f(rec)
                self.assertEqual((code, out["policy_rule"], out["index_class"]),
                                 (KEEP, "2b", "candidate"))

    def test_derived_fields_match_the_batchs_helpers(self):
        paths = [
            f"{MGMT}/principal-roles/r1", f"{MGMT}/principal-roles", f"{MGMT}/catalogs/c/catalog-roles/x",
            f"{MGMT}/catalogs/c/catalog-roles", "/api/catalog/v1/oauth/tokens",
            "/api/catalog/v1/config", f"{CAT}/tables/rename", f"{CAT}/views/rename",
            f"{CAT}/transactions/commit", f"{CAT}/namespaces/a/register",
            f"{CAT}/namespaces/a/properties", f"{CAT}/namespaces/a/tables/t",
            f"{CAT}/namespaces/a/tables/t/metrics", f"{CAT}/namespaces/a/views/v",
            f"{CAT}/namespaces/a/tables", f"{CAT}/namespaces/a/views", f"{CAT}/namespaces/a",
            f"{CAT}/namespaces", f"{CAT}/namespaces/a%1Fb/tables/t",
            f"{CAT}/namespaces/a%1Fb%1Fc", f"{MGMT}/catalogs/c", f"{MGMT}/catalogs",
            f"{MGMT}/principals/p1", f"{MGMT}/principals", f"{MGMT}/principals/p1/rotate",
            f"{MGMT}/principals/p1?a=b", f"{CAT}/namespaces/a/tables/t?snapshots=all",
            f"{CAT}/namespaces?pageToken=x", "/api/other/x", "/q/health", "/",
            "/api/catalog/v1/cat/something/else", f"{CAT}/namespaces//tables",
            f"{CAT}/namespaces/a/tables/", "/api/management/v1/principal-roles/r1/catalog-roles/c",
            "/api/management/v1/principals/p1/principal-roles", "/x/principals",
            "/x/catalogs/", "/api/management/", "/api/catalog/",
        ]
        self.assertGreaterEqual(len(paths), 40)
        for p in paths:
            with self.subTest(p):
                _, out = self.f(access("GET", p, 200, level="WARN"))
                resource, kind = plb.classify_path(plb.path_only(p))
                self.assertEqual((out["resource"], out["resource_kind"]), (resource, kind))
                self.assertEqual(out["api_kind"], plb.api_of(p))
                self.assertEqual(out["api_path"], p)

    def test_status_and_size_match_the_batchs_parse(self):
        for size in ("0", "-", "17", "123456"):
            for status in (200, 404, 503):
                with self.subTest(size=size, status=status):
                    rec = access("PUT", f"{MGMT}/principals/p", status, size=size)
                    ref = copy.deepcopy(rec)
                    plb.parse_access(ref)
                    _, out = self.f(rec)
                    for k in ("client_ip", "user_principal_name", "http_method", "api_path",
                              "http_status", "response_size"):
                        self.assertEqual(out[k], ref[k], k)

    def test_the_redacted_message_matches_the_batchs(self):
        for msg in ("clientSecret: abc", "clientSecret:abc,x", "a clientSecret:   *", "x clientSecret: "
                    "<redacted> y clientSecret: z", "clientSecret", "no secret here"):
            with self.subTest(msg):
                rec = line(MAPPER, "ERROR", msg)
                ref = copy.deepcopy(rec)
                plb.redact_secret(ref)
                _, out = self.f(rec)
                self.assertEqual(out["message"], ref["message"])
                self.assertEqual(out.get("secret_redacted"), ref.get("secret_redacted"))


class WriteFailures(Base):
    def test_a_failed_write_is_an_error_line(self):
        for method, status in (("PUT", 409), ("DELETE", 500), ("PATCH", 403)):
            with self.subTest(method=method, status=status):
                _, out = self.f(access(method, f"{MGMT}/principals/p", status))
                self.assertEqual((out["policy_rule"], out["index_class"]), ("3", "error"))

    def test_a_successful_write_is_a_candidate(self):
        _, out = self.f(access("PUT", f"{MGMT}/principals/p", 200))
        self.assertEqual((out["policy_rule"], out["index_class"]), ("4", "candidate"))

    def test_an_access_line_at_warn_is_rule_1_with_its_fields(self):
        _, out = self.f(access("GET", f"{MGMT}/catalogs", 503, level="WARN"))
        self.assertEqual(out["policy_rule"], "1")
        self.assertEqual((out["http_status"], out["status_class"]), (503, "5xx"))

    def test_a_404_on_any_method_is_a_candidate(self):
        for method in ("GET", "PUT", "DELETE", "POST", "OPTIONS"):
            with self.subTest(method):
                _, out = self.f(access(method, f"{CAT}/namespaces/a", 404))
                self.assertEqual((out["policy_rule"], out["index_class"]), ("3'", "candidate"))


class StatusClass(Base):
    def test_status_class(self):
        for status, want in ((200, "ok"), (399, "ok"), (401, "401"), (403, "403"), (404, "404"),
                             (400, "4xx"), (418, "4xx"), (499, "4xx"), (500, "5xx"), (599, "5xx")):
            with self.subTest(status):
                _, out = self.f(access("PUT", f"{MGMT}/principals/p", status))
                self.assertEqual(out["status_class"], want)

    def test_unparsed_and_not_an_access_line(self):
        _, out = self.f(line(ACCESS, "INFO", "not an access line"))
        self.assertEqual(out["status_class"], "unparsed")
        self.assertIs(out["access_log_parse_error"], True)
        _, out = self.f(line(MAPPER, "ERROR", "boom"))
        self.assertEqual(out["status_class"], "none")
        self.assertNotIn("access_log_parse_error", out)


class Stateless(Base):
    def test_no_global_is_added_but_the_callback(self):
        f = Filter()
        for rec in corpus():
            f(rec)
        self.assertEqual(f.global_names() - f.before, {"polaris_filter"})

    def test_the_order_of_lines_changes_nothing(self):
        recs = corpus()
        forward, backward = Filter(), Filter()
        want = [forward(r) for r in recs]
        got = [backward(r) for r in reversed(recs)][::-1]
        self.assertEqual(got, want)

    def test_no_clock_and_no_io_in_the_source(self):
        for word in ("os.time", "os.clock", "os.date", "io.", "require"):
            self.assertNotIn(word, self.f.source, word)

    def test_a_line_given_twice_gets_the_same_event_id(self):
        rec = access("PUT", f"{MGMT}/principals/p", 200)
        self.assertEqual(self.f(rec)[1]["event_id"], self.f(rec)[1]["event_id"])


class EventId(Base):
    def test_integers_stay_integers(self):
        for seq, tail in ((1772, ":1772"), (10000000, ":10000000"), (0, ":0")):
            with self.subTest(seq):
                _, out = self.f(access("PUT", f"{MGMT}/principals/p", 200, sequence=seq))
                self.assertTrue(out["event_id"].endswith(tail), out["event_id"])
                self.assertIsNone(re.search(r"e\+|\.", out["event_id"].rsplit(":", 1)[1]))

    def test_a_missing_sequence_ends_with_a_dash(self):
        _, out = self.f(access("PUT", f"{MGMT}/principals/p", 200, sequence=None))
        self.assertTrue(out["event_id"].endswith(":-"), out["event_id"])

    def test_composition(self):
        _, out = self.f(access("PUT", f"{MGMT}/principals/p", 200, sequence=7))
        self.assertEqual(out["event_id"], f"{HOST}:2026-10-30T12:40:00.000000000Z:7")


class Trimmed(Base):
    def test_the_trimmed_fields_go_and_the_rest_stays(self):
        rec = access("PUT", f"{MGMT}/principals/p", 200, mdc={"requestId": "r", "realmId": "P"})
        _, out = self.f(rec)
        for k in ("loggerClassName", "ndc", "processName", "processId"):
            self.assertNotIn(k, out)
        for k in ("timestamp", "sequence", "loggerName", "level", "message", "threadName",
                  "threadId", "mdc", "hostName"):
            self.assertEqual(out[k], rec[k], k)


if __name__ == "__main__":
    unittest.main()
