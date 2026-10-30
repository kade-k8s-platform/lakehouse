"""Tests for the contract v3 check of `gate.py check-logs` (gates/logs_v3.py, ADR-081): its pure
parts, the pod and requests gate.py builds for it, and its orchestration against a scripted
OpenSearch. No cluster. Run: python3 -m unittest discover -s gates"""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import contract  # noqa: E402
import gate  # noqa: E402
import logs_v3  # noqa: E402

ENV = {"NS_STANDINS": "standins", "NS_CATALOG_POLARIS": "catalog-polaris"}
IMAGE = "fluent/fluent-bit:0@sha256:" + "0" * 64
ROUTING = contract.routing()
TRANSFORM = contract.transform()


def kept():
    return [copy.deepcopy(e["output"]) for e in contract.kept_examples()]


def by_name(name):
    return next(copy.deepcopy(e["output"]) for e in contract.kept_examples() if e["name"] == name)


def found_from(records):
    out = {"candidate": [], "error": []}
    for r in records:
        out[r["index_class"]].append({**r, "@timestamp": "2026-10-30T13:00:00.000Z"})
    return out


class Builders(unittest.TestCase):
    def test_records_jsonl(self):
        records = kept()
        lines = logs_v3.records_jsonl(records).splitlines()
        self.assertEqual([json.loads(line) for line in lines], records)
        self.assertEqual(len(lines), 15)
        self.assertIsInstance(json.loads(lines[0])["sequence"], int)

    def test_pass2_records(self):
        first = kept()
        second = logs_v3.pass2_records(first)
        self.assertEqual(len(second), 17)
        self.assertEqual([json.dumps(r) for r in second[:15]], [json.dumps(r) for r in first])
        extra, probe = second[15:]
        base = by_name("GET 404")
        for key in ("resource", "status_class", "index_class", "hostName"):
            self.assertEqual(extra[key], base[key])
        self.assertNotEqual(extra["event_id"], base["event_id"])
        self.assertNotEqual(extra["sequence"], base["sequence"])
        self.assertNotIn("resource", probe)
        self.assertEqual(probe["hostName"], "probe-missing-group-field")
        self.assertTrue(probe["event_id"].startswith("probe:"))
        self.assertEqual(probe["index_class"], "candidate")

    def test_forwarder_pod(self):
        pod = gate.forwarder_pod(ENV, IMAGE, "forwarder-1", "records-1")
        self.assertEqual(pod["metadata"]["namespace"], "catalog-polaris")
        self.assertEqual(pod["metadata"]["labels"]["lakehouse-gate/check"], "true")
        spec = pod["spec"]
        self.assertEqual(spec["restartPolicy"], "Never")
        (c,) = spec["containers"]
        self.assertEqual(c["image"], IMAGE)
        self.assertEqual(c["command"], ["/fluent-bit/bin/fluent-bit"])
        args = c["args"]
        self.assertIn("tail", args)
        for p in ("exit_on_eof=true", "read_from_head=true", "parser=json",
                  "host=main-fluent-bit-standin.standins.svc.cluster.local",
                  "port=24224", "require_ack_response=true"):
            self.assertIn(p, args)
        self.assertEqual(args[args.index("-o") + 1], "forward")
        (mount,) = c["volumeMounts"]
        self.assertTrue(mount["readOnly"])
        self.assertIn("subPath", mount)
        self.assertIn(mount["mountPath"], args[args.index("-p") + 1:][0:20] + [
            a.split("=", 1)[1] for a in args if a.startswith("path=")])
        (volume,) = spec["volumes"]
        self.assertEqual(volume["configMap"]["name"], "records-1")

    def test_records_configmap(self):
        cm = gate.logs_records_configmap(ENV, "records-1", "a\nb\n")
        self.assertEqual(cm["kind"], "ConfigMap")
        self.assertEqual(cm["metadata"]["namespace"], "catalog-polaris")
        self.assertEqual(cm["data"], {"records.jsonl": "a\nb\n"})

    def test_opensearch_argv_and_answer(self):
        argv = gate.logs_opensearch_argv(ENV, "PUT", "/_x")
        self.assertEqual(argv[:8], ["kubectl", "--context", gate.CONTEXT, "-n", "standins",
                                    "exec", "-i", "deploy/logs-opensearch"])
        self.assertEqual(argv[8:10], ["--", "curl"])
        self.assertIn("http://127.0.0.1:9200/_x", argv)
        self.assertIn("PUT", argv)
        self.assertEqual(gate.parse_http_answer('{"a":1}\n200'), (200, {"a": 1}))
        self.assertEqual(gate.parse_http_answer("\n404"), (404, {}))
        status, body = gate.parse_http_answer("Unauthorized\n401")
        self.assertEqual((status, body["raw"]), (401, "Unauthorized"))


class Routing(unittest.TestCase):
    def test_right_routing(self):
        self.assertEqual(logs_v3.routing_problems(kept(), found_from(kept())), [])

    def mutate(self, name):
        records = kept()
        return records, found_from(records)

    def test_the_404_in_error(self):
        records, found = self.mutate("")
        doc = next(d for d in found["candidate"] if d["status_class"] == "404")
        found["candidate"].remove(doc)
        found["error"].append(doc)
        problems = logs_v3.routing_problems(records, found)
        self.assertTrue(any(doc["event_id"] in p for p in problems), problems)

    def test_a_409_in_candidate(self):
        records, found = self.mutate("")
        doc = next(d for d in found["error"] if d["status_class"] == "4xx")
        found["error"].remove(doc)
        found["candidate"].append(doc)
        problems = logs_v3.routing_problems(records, found)
        self.assertTrue(any(doc["event_id"] in p for p in problems), problems)

    def test_a_record_missing(self):
        records, found = self.mutate("")
        doc = found["candidate"].pop()
        self.assertTrue(any(doc["event_id"] in p
                            for p in logs_v3.routing_problems(records, found)))

    def test_a_record_twice(self):
        records, found = self.mutate("")
        doc = found["candidate"][0]
        found["candidate"].append(dict(doc))
        self.assertTrue(any(doc["event_id"] in p
                            for p in logs_v3.routing_problems(records, found)))

    def test_an_error_line_in_candidate(self):
        records, found = self.mutate("")
        doc = next(d for d in found["error"] if d["level"] == "ERROR")
        found["error"].remove(doc)
        found["candidate"].append(doc)
        self.assertTrue(any(doc["event_id"] in p
                            for p in logs_v3.routing_problems(records, found)))

    def test_a_candidate_with_a_failing_status(self):
        records, found = self.mutate("")
        doc = found["candidate"][0]
        doc["http_status"] = 500
        self.assertTrue(any(doc["event_id"] in p
                            for p in logs_v3.routing_problems(records, found)))


class Documents(unittest.TestCase):
    def test_the_fifteen_hold(self):
        self.assertEqual(logs_v3.document_problems(found_from(kept())), [])

    def test_an_exception_string(self):
        found = found_from(kept())
        found["error"][0]["exception"] = "boom"
        problems = logs_v3.document_problems(found)
        self.assertTrue(any("exception" in p for p in problems), problems)

    def test_a_probe_is_skipped(self):
        found = found_from(logs_v3.pass2_records(kept()))
        self.assertEqual(logs_v3.document_problems(found), [])


class Buckets(unittest.TestCase):
    def test_fourteen_for_fifteen(self):
        buckets = logs_v3.expected_buckets(kept(), TRANSFORM)
        self.assertEqual(len(buckets), 14)
        self.assertEqual(sorted(b["lines"] for b in buckets.values()).count(2), 1)
        self.assertEqual(sum(b["lines"] for b in buckets.values()), 15)
        self.assertEqual(sum(b["response_bytes"] for b in buckets.values()),
                         sum(r.get("response_size", 0) for r in kept()))

    def test_the_groups_come_from_the_transform(self):
        changed = copy.deepcopy(TRANSFORM)
        changed["transform"]["groups"] = [
            g for g in changed["transform"]["groups"]
            if "date_histogram" in g or g["terms"]["source_field"] == "hostName"]
        self.assertEqual(len(logs_v3.expected_buckets(kept(), changed)), 1)

    def test_the_extra_404_joins_its_bucket(self):
        buckets = logs_v3.expected_buckets(logs_v3.pass2_records(kept()), TRANSFORM)
        self.assertEqual(len(buckets), 14)
        (bucket,) = [b for b in buckets.values()
                     if b["groups"]["status_class"] == "404"]
        self.assertEqual(bucket["lines"], 2)

    def test_times(self):
        buckets = logs_v3.expected_buckets(kept(), TRANSFORM)
        for b in buckets.values():
            self.assertLessEqual(b["min_record_time"], b["max_record_time"])
        (b,) = [b for b in buckets.values() if b["lines"] == 2]
        self.assertLess(b["min_record_time"], b["max_record_time"])


class AggregateRun(unittest.TestCase):
    def setUp(self):
        self.expected = logs_v3.expected_buckets(kept(), TRANSFORM)
        self.docs = logs_v3.bucket_docs(self.expected)

    def test_equal(self):
        self.assertEqual(logs_v3.aggregate_run_problems(self.expected, self.docs), [])

    def test_the_documents_pass_the_schema(self):
        for doc in self.docs:
            self.assertEqual(contract.aggregate_problems(doc), [])

    def test_a_missing_bucket(self):
        problems = logs_v3.aggregate_run_problems(self.expected, self.docs[1:])
        self.assertTrue(problems)

    def test_an_extra_bucket(self):
        extra = dict(self.docs[0], resource="/other")
        self.assertTrue(logs_v3.aggregate_run_problems(self.expected, self.docs + [extra]))

    def test_a_wrong_count(self):
        docs = copy.deepcopy(self.docs)
        docs[0]["lines"] += 1
        problems = logs_v3.aggregate_run_problems(self.expected, docs)
        self.assertTrue(any("lines" in p for p in problems), problems)

    def test_a_schema_failure(self):
        docs = copy.deepcopy(self.docs)
        docs[0]["extra"] = 1
        self.assertTrue(logs_v3.aggregate_run_problems(self.expected, docs))

    def test_times_as_epoch_milliseconds(self):
        docs = copy.deepcopy(self.docs)
        for doc in docs:
            for key in ("window_start", "min_record_time", "max_record_time"):
                doc[key] = logs_v3.instant(doc[key])
            doc["response_bytes"] = float(doc["response_bytes"])
        self.assertEqual(logs_v3.aggregate_run_problems(self.expected, docs), [])

    def test_a_probe_bucket_is_left_to_probe_outcome(self):
        probe = dict(self.docs[0], hostName="probe-missing-group-field")
        self.assertEqual(logs_v3.aggregate_run_problems(self.expected, self.docs + [probe]), [])


class Counters(unittest.TestCase):
    def test_the_fifteen(self):
        docs = logs_v3.bucket_docs(logs_v3.expected_buckets(kept(), TRANSFORM))
        found = logs_v3.counters(docs, ROUTING)
        records = kept()
        classes = ROUTING["counters"]
        self.assertEqual(found["not_found"], 1)
        self.assertEqual(found["errors_4xx"], sum(r["status_class"] in classes["errors_4xx"]
                                                  for r in records))
        self.assertEqual(found["auth_denied"], sum(r["status_class"] in classes["auth_denied"]
                                                   for r in records))
        self.assertEqual(found["errors_5xx"], 1)
        self.assertEqual(found["errors_kept"], sum(r["index_class"] == "error" for r in records))
        self.assertEqual(sum(found["by_pod"].values()), 15)

    def test_the_404_is_not_a_4xx(self):
        docs = [d for d in logs_v3.bucket_docs(logs_v3.expected_buckets(kept(), TRANSFORM))
                if d["status_class"] == "404"]
        found = logs_v3.counters(docs, ROUTING)
        self.assertEqual((found["not_found"], found["errors_4xx"]), (1, 0))


class Ism(unittest.TestCase):
    IDS = {"candidate": "polaris-logs-candidate", "error": "polaris-logs-error"}
    KEY = "index.plugins.index_state_management.policy_id"

    def explain(self):
        return {"polaris-logs-candidate-2026.10.30": {self.KEY: self.IDS["candidate"]},
                "polaris-logs-error-2026.10.30": {self.KEY: self.IDS["error"]},
                "total_managed_indices": 2}

    def test_attached(self):
        self.assertEqual(logs_v3.ism_problems(self.explain(), self.IDS), [])

    def test_a_missing_id(self):
        found = self.explain()
        found["polaris-logs-error-2026.10.30"] = {self.KEY: None}
        self.assertTrue(logs_v3.ism_problems(found, self.IDS))

    def test_the_wrong_kind(self):
        found = self.explain()
        found["polaris-logs-error-2026.10.30"][self.KEY] = self.IDS["candidate"]
        self.assertTrue(logs_v3.ism_problems(found, self.IDS))


class Probe(unittest.TestCase):
    def test_outcomes(self):
        self.assertEqual(logs_v3.probe_outcome([]), "no bucket")
        doc = {"hostName": "probe-missing-group-field", "resource": None}
        self.assertEqual(logs_v3.probe_outcome([doc]), "a bucket without resource")
        doc = {"hostName": "probe-missing-group-field"}
        self.assertEqual(logs_v3.probe_outcome([doc]), "a bucket without resource")


class FakeOpenSearch:
    """What OpenSearch answers, for the orchestration: documents by index, the aggregate
    computed from them as the transform would; records every call."""

    def __init__(self, transform_status=200, never_aggregates=False):
        self.calls, self.docs, self.agg = [], {}, []
        self.transform_status, self.never_aggregates = transform_status, never_aggregates
        self.forwards = 0

    def forward(self, text):
        self.calls.append(("FORWARD", ""))
        self.forwards += 1
        for line in text.splitlines():
            doc = json.loads(line)
            index = f"polaris-logs-{doc['index_class']}-2026.10.30"
            self.docs.setdefault(index, {})[doc["event_id"]] = {
                **doc, "@timestamp": "2026-10-30T13:00:00.000Z"}
        return "Succeeded"

    def sources(self):
        return [d for index in self.docs.values() for d in index.values()]

    def http(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "DELETE":
            return 404, {}
        if path.startswith("/_plugins/_ism/policies/") or path.startswith("/_index_template/"):
            return 200, {"acknowledged": True}
        if path == "/_refresh":
            return 200, {}
        if path.startswith("/_alias/polaris-logs-source"):
            return 200, {name: {"aliases": {"polaris-logs-source": {}}} for name in self.docs}
        if path.startswith("/_plugins/_ism/explain"):
            key = "index.plugins.index_state_management.policy_id"
            out = {name: {key: "polaris-logs-" + name.split("-")[2]} for name in self.docs}
            return 200, {**out, "total_managed_indices": len(out)}
        if path.startswith("/_plugins/_transform/") and method == "PUT":
            if self.transform_status != 200:
                return self.transform_status, {"error": {"reason": "interval too short"}}
            return 200, {"_id": "polaris-logs-aggregate-v3"}
        if path.startswith("/_plugins/_transform/"):
            return 200, {}
        if path.startswith("/polaris-logs-aggregate/_search"):
            if self.never_aggregates:
                return 200, {"hits": {"hits": []}}
            buckets = logs_v3.expected_buckets(self.sources(), TRANSFORM)
            hits = [{"_id": "id-%d" % i, "_source": d}
                    for i, d in enumerate(logs_v3.bucket_docs(buckets, as_float=True))]
            return 200, {"hits": {"hits": hits}}
        if "/_search" in path:
            kind = "candidate" if "candidate" in path else "error"
            hits = [{"_id": d["event_id"], "_source": d}
                    for name, index in self.docs.items() if f"-{kind}-" in name
                    for d in index.values()]
            return 200, {"hits": {"hits": hits}}
        return 404, {}


class Check(unittest.TestCase):
    def run_check(self, fake, clock=None):
        ticks = iter(range(0, 10 ** 6, 1))
        return logs_v3.check(fake.http, fake.forward, lambda name: "",
                             sleep=lambda s: None, clock=clock or (lambda: next(ticks)))

    def first(self, fake, prefix):
        return next(i for i, (m, p) in enumerate(fake.calls) if (m + " " + p).startswith(prefix))

    def test_everything_right(self):
        fake = FakeOpenSearch()
        problems, notes = self.run_check(fake)
        self.assertEqual(problems, [])
        text = "\n".join(notes)
        for word in ("1 Minutes", "polaris-logs-source", "window_start", "response_bytes",
                     "probe"):
            self.assertIn(word, text)

    def test_the_order(self):
        fake = FakeOpenSearch()
        self.run_check(fake)
        order = [self.first(fake, "DELETE"),
                 self.first(fake, "PUT /_plugins/_ism/policies/"),
                 self.first(fake, "PUT /_index_template/"),
                 self.first(fake, "FORWARD"),
                 self.first(fake, "POST /_refresh"),
                 self.first(fake, "GET /polaris-logs-candidate-*/_search"),
                 self.first(fake, "GET /_plugins/_ism/explain"),
                 self.first(fake, "PUT /_plugins/_transform/"),
                 self.first(fake, "GET /polaris-logs-aggregate/_search")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(fake.forwards, 2)
        self.assertGreater([m for m, _ in fake.calls].index("FORWARD", order[3] + 1),
                           order[-1])

    def test_a_refused_transform_stops_with_the_reason(self):
        fake = FakeOpenSearch(transform_status=400)
        problems, _ = self.run_check(fake)
        self.assertTrue(any("interval too short" in p for p in problems), problems)
        self.assertEqual(sum(1 for m, p in fake.calls
                             if m == "PUT" and p.startswith("/_plugins/_transform/")), 1)
        self.assertEqual(fake.forwards, 1)

    def test_a_clock_past_the_timeout_is_a_problem(self):
        fake = FakeOpenSearch(never_aggregates=True)
        clock = iter(range(0, 10 ** 6, 100))
        problems, _ = self.run_check(fake, clock=lambda: next(clock))
        self.assertTrue(any("aggregate" in p and "not done" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
