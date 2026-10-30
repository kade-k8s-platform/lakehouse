"""Tests for syslog_load.py. Run: python3 -m unittest discover -s gates"""

import http.server
import io
import json
import os
import sys
import threading
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(__file__))
import syslog_load as load  # noqa: E402


class Ids(unittest.TestCase):
    def test_request_id(self):
        self.assertEqual(load.request_id(42, "a", "stall", 7), "syslog-42-a-stall-7")

    def test_the_long_id_has_exactly_the_length_asked(self):
        for length in (3000, 9000, 19000):
            got = load.long_id(42, "b", length)
            self.assertEqual(len(got), length)
            self.assertTrue(got.startswith(f"syslog-42-b-long-{length}-x"))
        with self.assertRaises(ValueError):
            load.long_id(42, "b", 10)

    def test_the_pad_makes_the_query_the_length_asked(self):
        self.assertEqual(len(load.pad_query(1500)), 1500)
        self.assertEqual(load.pad_query(0), "")


class Summary(unittest.TestCase):
    def test_counts_per_status_and_one_figure_per_request(self):
        results = [(401, 12.5, "ok"), (401, 8.0, "ok"), (200, 30.0, "ok"), (None, 10000.0, "timeout"),
                   (None, 3.0, "error")]
        got = load.summarize("stall-1", results)
        self.assertEqual(got["phase"], "stall-1")
        self.assertEqual(got["sent"], 5)
        self.assertEqual(got["counts"], {"401": 2, "200": 1})
        self.assertEqual((got["timeouts"], got["errors"]), (1, 1))
        self.assertEqual(len(got["ms"]), 5)


class Answering(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        Answering.seen.append((self.path, self.headers["X-Request-ID"],
                               self.headers["Polaris-Realm"], self.headers["Authorization"]))
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class Phases(unittest.TestCase):
    def test_a_phase_sends_its_ids_and_reports_them(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Answering)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        Answering.seen = []
        spec = {"url": f"http://127.0.0.1:{server.server_port}", "realm": "R", "pid": 5,
                "leg": "a", "phases": [{"phase": "base", "count": 6, "workers": 3, "pad": 100},
                                       {"phase": "long", "lengths": [300]}]}
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(load.main(["syslog_load.py", json.dumps(spec)], {}), 0)
        phases = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([p["phase"] for p in phases], ["base", "long"])
        self.assertEqual(phases[0]["counts"], {"401": 6})
        self.assertEqual(phases[1]["ids"], [load.long_id(5, "a", 300)])
        ids = sorted(i for _, i, _, _ in Answering.seen)
        self.assertEqual(ids, sorted([load.request_id(5, "a", "base", i) for i in range(6)]
                                     + [load.long_id(5, "a", 300)]))
        base = [path for path, i, _, _ in Answering.seen if "-base-" in i]
        self.assertTrue(all(len(p.split("?", 1)[1]) == 100 for p in base))
        self.assertTrue(all(realm == "R" and auth is None for _, _, realm, auth in Answering.seen))


if __name__ == "__main__":
    unittest.main()
