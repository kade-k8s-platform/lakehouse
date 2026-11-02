"""Tests for the removed v2 checks and the batch-image command (ADR-081, ADR-083).
Run: python3 -m unittest discover -s gates"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import gate  # noqa: E402


class V2IsGone(unittest.TestCase):
    REMOVED = (
        "check_logs logs_problems logs_request_ids logs_table logs_traffic_command batch_now "
        "batch_job job_outcome json_lines batch_events fluent_bit_records logs_listing_command "
        "parse_listing fluent_bit_pod LOGS_REQUESTS LOGS_CATALOG LOGS_NAMESPACE "
        "LOGS_POLICY_KINDS APP_LINE_LOGGERS LOGS_MOUNT OUTPUT_DIRS READER_UID LOGS_LISTING "
        "batch_image_commands"
    ).split()
    KEPT = "wait_for LOGS_TIMEOUT SHARED_CLASS GATE_LOGS_SIZE gate_env".split()

    def test_the_v2_checks_and_helpers_are_gone(self):
        for name in self.REMOVED:
            self.assertFalse(hasattr(gate, name), name)

    def test_what_the_install_and_other_parts_still_need_is_kept(self):
        for name in self.KEPT:
            self.assertTrue(hasattr(gate, name), name)

    def test_the_batch_image_command_is_gone(self):
        self.assertNotIn("batch-image", gate.__doc__)

    def test_the_traffic_script_and_the_v2_schemas_are_gone(self):
        self.assertFalse(os.path.exists(os.path.join(HERE, "logs_traffic.sh")))
        for name in ("app-dropped", "principal", "processed", "resource", "summary"):
            path = os.path.join(ROOT, "docs", "contracts", f"polaris-logs-{name}.schema.json")
            self.assertFalse(os.path.exists(path), name)
