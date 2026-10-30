"""Tests for syslog_receiver.py. Run: python3 -m unittest discover -s gates"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import syslog_receiver as receiver  # noqa: E402


def feed_all(framer, data, step):
    frames, errors = [], []
    for i in range(0, len(data), step):
        got, bad = framer.feed(data[i:i + step])
        frames += got
        errors += bad
    return frames, errors


class SplitFrames(unittest.TestCase):
    def test_two_frames(self):
        self.assertEqual(receiver.split_frames(b"5 hello6 world!"),
                         ([b"hello", b"world!"], b"", []))

    def test_a_partial_frame_waits(self):
        self.assertEqual(receiver.split_frames(b"5 hel"), ([], b"5 hel", []))
        self.assertEqual(receiver.split_frames(b"12"), ([], b"12", []))

    def test_a_frame_not_starting_with_a_digit_is_an_error_naming_its_offset(self):
        frames, rest, errors = receiver.split_frames(b"x 5 hello")
        self.assertEqual((frames, rest), ([], b""))
        self.assertEqual(len(errors), 1)
        self.assertIn("offset 0", errors[0])

    def test_the_offset_counts_the_frames_before_it(self):
        frames, _, errors = receiver.split_frames(b"2 okzz")
        self.assertEqual(frames, [b"ok"])
        self.assertIn("offset 4", errors[0])

    def test_a_length_over_a_mebibyte_is_an_error(self):
        _, _, errors = receiver.split_frames(b"99999999 x")
        self.assertEqual(len(errors), 1)
        self.assertIn("99999999", errors[0])

    def test_lengths_count_octets_not_characters(self):
        self.assertEqual(receiver.split_frames("3 é!".encode())[0], ["é!".encode()])

    def test_every_byte_boundary_gives_the_same_frames(self):
        data = "5 hello3 é!12 {\"a\":\"b\"} x".encode()
        want = receiver.split_frames(data)[0]
        for step in range(1, len(data) + 1):
            frames, errors = feed_all(receiver.Framer(), data, step)
            self.assertEqual((frames, errors), (want, []), step)

    def test_a_stream_that_ends_mid_frame_is_an_error(self):
        framer = receiver.Framer()
        framer.feed(b"5 hel")
        errors = framer.close()
        self.assertEqual(len(errors), 1)
        self.assertIn("offset 0", errors[0])
        self.assertEqual(receiver.Framer().close(), [])


class ParseSyslog(unittest.TestCase):
    def test_the_envelope(self):
        got = receiver.parse_syslog(
            b'<14>1 2026-10-30T10:00:00.123Z pod-1 polaris 1 - - {"a":1}')
        self.assertEqual((got["pri"], got["version"], got["host"], got["app"]),
                         (14, 1, "pod-1", "polaris"))
        self.assertEqual(got["msg"], '{"a":1}')
        self.assertEqual(got["timestamp"], "2026-10-30T10:00:00.123Z")
        self.assertIsNone(got["sd"])

    def test_structured_data_is_kept(self):
        got = receiver.parse_syslog(b'<14>1 t h a 1 id [x a="b"] {"a":1}')
        self.assertEqual(got["sd"], '[x a="b"]')
        self.assertEqual(got["msg"], '{"a":1}')
        self.assertEqual(got["msgid"], "id")

    def test_a_byte_order_mark_is_not_part_of_the_message(self):
        got = receiver.parse_syslog(b'<14>1 t h a 1 - - \xef\xbb\xbf{"a":1}')
        self.assertEqual(got["msg"], '{"a":1}')

    def test_a_frame_not_starting_with_a_bracket_is_an_error(self):
        with self.assertRaises(ValueError):
            receiver.parse_syslog(b"hello")


def frame_of(body, host="pod-1"):
    return f"<14>1 2026-10-30T10:00:00Z {host} polaris 1 - - {body}".encode()


class RecordOf(unittest.TestCase):
    def test_a_json_line_gives_its_fields(self):
        body = json.dumps({"timestamp": "t", "sequence": 3, "loggerName": "l", "level": "INFO",
                           "mdc": {"requestId": "syslog-1-a-normal-4"}, "hostName": "pod-1",
                           "extra": 1})
        record = receiver.record_of(7, frame_of(body))
        self.assertTrue(record["json_ok"])
        self.assertEqual(record["n"], 7)
        self.assertEqual(record["request_id"], "syslog-1-a-normal-4")
        self.assertEqual(record["fields"]["sequence"], 3)
        self.assertEqual(record["keys"]["extra"], "int")
        self.assertEqual(record["host"], "pod-1")
        self.assertEqual((record["pri"], record["version"]), (14, 1))
        self.assertEqual(record["declared"], record["actual"])
        self.assertNotIn("msg", record)

    def test_a_cut_body_is_not_json_and_keeps_its_tail(self):
        body = '{"loggerName":"l","mdc":{"requestId":"syslog-1-a-long-3000-' + "x" * 3000
        cut = body[:2048 - 40]
        record = receiver.record_of(1, frame_of(cut))
        self.assertFalse(record["json_ok"])
        self.assertEqual(record["tail"], cut[-120:])
        self.assertEqual(len(record["head"]), 160)
        self.assertTrue(record["request_id"].startswith("syslog-1-a-long-3000-xx"))
        self.assertEqual(record["msg"], cut)

    def test_the_full_message_is_kept_only_for_the_long_marker(self):
        body = json.dumps({"mdc": {"requestId": "syslog-1-a-normal-1"}, "pad": "x" * 500})
        self.assertNotIn("msg", receiver.record_of(1, frame_of(body)))
        long_body = json.dumps({"mdc": {"requestId": "syslog-1-a-long-9-xxx"}})
        self.assertEqual(receiver.record_of(1, frame_of(long_body))["msg"], long_body)

    def test_msg_len_counts_octets(self):
        record = receiver.record_of(1, frame_of('{"a":"é"}'))
        self.assertEqual(record["msg_len"], len('{"a":"é"}'.encode()))
        self.assertGreater(record["declared"], record["msg_len"])

    def test_a_body_with_two_objects_is_not_json(self):
        self.assertFalse(receiver.record_of(1, frame_of('{"a":1}{"b":2}'))["json_ok"])

    def test_a_frame_that_is_no_syslog_message_is_a_record_with_an_error(self):
        record = receiver.record_of(2, b"hello")
        self.assertFalse(record["json_ok"])
        self.assertIn("error", record)

    def test_the_first_records_carry_a_sample_of_each_key(self):
        record = receiver.record_of(1, frame_of('{"a":"' + "y" * 300 + '","b":2}'))
        self.assertEqual(record["sample"]["b"], "2")
        self.assertLessEqual(len(record["sample"]["a"]), 80)
        self.assertNotIn("sample", receiver.record_of(500, frame_of('{"a":1}')))


class Pump(unittest.TestCase):
    def pump(self, stall, received, ready=None):
        a, b = socket.socketpair()
        self.addCleanup(b.close)
        self.addCleanup(a.close)
        a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        thread = threading.Thread(
            target=receiver.pump, args=(b, stall, received.extend), kwargs={"poll": 0.01})
        thread.start()
        return a, thread

    def test_with_the_stall_file_nothing_is_read_and_afterwards_everything_in_order(self):
        with tempfile.TemporaryDirectory() as d:
            stall = os.path.join(d, "stall")
            open(stall, "w").close()
            received = []
            a, thread = self.pump(stall, received)
            a.setblocking(False)
            sent = bytearray()
            chunk, n = b"0123456789" * 100, 0
            while True:
                data = chunk + str(n).encode()
                try:
                    a.send(data)
                except BlockingIOError:
                    break
                sent += data
                n += 1
                self.assertLess(n, 10000, "the writer never blocked")
            time.sleep(0.1)
            self.assertEqual(received, [])
            self.assertGreater(len(sent), 0)
            os.remove(stall)
            a.setblocking(True)
            tail = b"the end"
            a.sendall(tail)
            a.close()
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(bytes(received), bytes(sent) + tail)


class Serve(unittest.TestCase):
    def test_ready_after_the_bind_and_records_are_written(self):
        with tempfile.TemporaryDirectory() as d:
            paths = {k: os.path.join(d, k) for k in ("ready", "stall", "results")}
            stop = threading.Event()
            listening = socket.socket()
            listening.bind(("127.0.0.1", 0))
            port = listening.getsockname()[1]
            listening.close()
            thread = threading.Thread(target=receiver.serve, kwargs={
                "host": "127.0.0.1", "port": port, "ready": paths["ready"],
                "stall": paths["stall"], "results": paths["results"], "stop": stop,
                "poll": 0.01})
            thread.start()
            end = time.monotonic() + 5
            while not os.path.exists(paths["ready"]) and time.monotonic() < end:
                time.sleep(0.01)
            self.assertTrue(os.path.exists(paths["ready"]))
            body = '{"mdc":{"requestId":"syslog-1-a-normal-1"},"sequence":1}'
            frame = frame_of(body)
            with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
                c.sendall(f"{len(frame)} ".encode() + frame)
                c.sendall(f"{len(frame)} ".encode() + frame)
            end = time.monotonic() + 5
            lines = []
            while len(lines) < 2 and time.monotonic() < end:
                time.sleep(0.05)
                with open(paths["results"], encoding="utf-8") as f:
                    lines = f.read().splitlines()
            stop.set()
            thread.join(5)
            records = [json.loads(line) for line in lines]
            self.assertEqual([r["n"] for r in records], [1, 2])
            self.assertTrue(all(r["framing_ok"] and r["json_ok"] for r in records))

    def test_the_ready_file_is_not_there_before_the_bind(self):
        with tempfile.TemporaryDirectory() as d:
            ready = os.path.join(d, "ready")
            busy = socket.socket()
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            with self.assertRaises(OSError):
                receiver.serve(host="127.0.0.1", port=port, ready=ready,
                               stall=os.path.join(d, "stall"),
                               results=os.path.join(d, "results"), reuse=False)
            busy.close()
            self.assertFalse(os.path.exists(ready))


if __name__ == "__main__":
    unittest.main()
