"""gates/contract.py: the schema checker for contract v3's schemas (ADR-081, ADR-082): nested
objects, nullable fields, `minimum` and the refusal of keywords it does not cover."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import contract  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
