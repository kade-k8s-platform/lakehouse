"""Tests for the contract v3 loaders of contract.py (ADR-081). Run: python3 -m unittest discover -s gates"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import contract  # noqa: E402


class Loaders(unittest.TestCase):
    def test_the_three_schemas(self):
        for name in ("candidate-line", "error-line", "aggregate"):
            with self.subTest(name=name):
                self.assertIn("properties", contract.load_v3(name))

    def test_the_json_files(self):
        self.assertIn("examples", contract.load_v3("routing"))
        self.assertIn("transform", contract.load_v3("transform"))
        for name in ("candidate-template", "error-template", "aggregate-template"):
            with self.subTest(name=name):
                self.assertIn("index_patterns", contract.load_v3(name))
        for name in ("candidate-ism", "error-ism"):
            with self.subTest(name=name):
                self.assertIn("policy", contract.load_v3(name))

    def test_an_unknown_name_raises(self):
        with self.assertRaises(FileNotFoundError):
            contract.load_v3("nothing")

    def test_templates(self):
        found = contract.templates()
        self.assertEqual(sorted(found), ["aggregate", "candidate", "error"])
        self.assertEqual(found["candidate"]["index_patterns"], ["polaris-logs-candidate-*"])
        self.assertEqual(found["error"]["index_patterns"], ["polaris-logs-error-*"])
        self.assertEqual(found["aggregate"]["index_patterns"], ["polaris-logs-aggregate"])

    def test_ism_policies(self):
        found = contract.ism_policies()
        self.assertEqual(sorted(found), ["candidate", "error"])
        for kind, age in (("candidate", "3d"), ("error", "30d")):
            with self.subTest(kind=kind):
                states = {s["name"]: s for s in found[kind]["policy"]["states"]}
                self.assertIn(age, repr(states["hot"]["transitions"]))

    def test_transform_and_routing(self):
        self.assertEqual(contract.transform()["transform"]["source_index"], "polaris-logs-source")
        self.assertEqual(len(contract.routing_examples()), 20)
        kept = contract.kept_examples()
        self.assertEqual(len(kept), 15)
        self.assertTrue(all(e["output"] is not None for e in kept))


class LineProblems(unittest.TestCase):
    def test_every_kept_output_holds(self):
        for e in contract.kept_examples():
            with self.subTest(name=e["name"]):
                self.assertEqual(contract.line_problems(e["output"]), [])

    def test_the_class_picks_the_schema(self):
        out = copy.deepcopy(contract.kept_examples()[0]["output"])
        out["index_class"] = "other"
        self.assertTrue(any("index_class" in p for p in contract.line_problems(out)))

    def test_a_missing_event_id(self):
        out = copy.deepcopy(contract.kept_examples()[0]["output"])
        del out["event_id"]
        self.assertTrue(any("event_id" in p for p in contract.line_problems(out)))

    def test_a_host_name_that_is_an_integer(self):
        out = copy.deepcopy(contract.kept_examples()[0]["output"])
        out["hostName"] = 5
        self.assertTrue(any("hostName" in p for p in contract.line_problems(out)))


class AggregateProblems(unittest.TestCase):
    def example(self):
        return copy.deepcopy(contract.routing()["aggregate_example"])

    def test_the_example_holds(self):
        self.assertEqual(contract.aggregate_problems(self.example()), [])

    def test_a_whole_float_is_an_integer(self):
        doc = self.example()
        doc["response_bytes"] = 360.0
        self.assertEqual(contract.aggregate_problems(doc), [])

    def test_a_fraction_is_not(self):
        doc = self.example()
        doc["response_bytes"] = 360.5
        self.assertTrue(any("response_bytes" in p for p in contract.aggregate_problems(doc)))

    def test_a_string_count(self):
        doc = self.example()
        doc["lines"] = "3"
        self.assertTrue(any("lines" in p for p in contract.aggregate_problems(doc)))

    def test_an_extra_key(self):
        doc = self.example()
        doc["extra"] = 1
        self.assertTrue(any("not allowed" in p for p in contract.aggregate_problems(doc)))


class V2Untouched(unittest.TestCase):
    def test_v2_is_as_it_was(self):
        self.assertIn("properties", contract.load("processed"))
        self.assertEqual(len(contract.ROW_SCHEMAS), 4)
        self.assertTrue(contract.row_problems({"report_type": "nothing"}))


if __name__ == "__main__":
    unittest.main()
