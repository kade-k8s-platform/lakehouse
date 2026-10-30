"""A plain syslog receiver for the Polaris syslog proofs (ADR-081, `gates/syslog_probe.py`).

Run in the probe pod as a container beside Polaris (`python3 /probe/syslog_receiver.py`). It
listens on 127.0.0.1:5140, reads RFC 6587 octet-counted frames, parses each as RFC 5424 and
appends one JSON line per frame to /tmp/syslog-received.jsonl for the gate to read with
`kubectl exec`. Standard library only: the gate puts this file in a ConfigMap and runs it on a
stock Python image.

  - The listener's SO_RCVBUF is small (4096), set before `listen`, so a reader that stops
    closes the TCP window quickly.
  - /tmp/syslog-receiver-ready is written once the port is bound.
  - While /tmp/syslog-stall exists the loop does not call `recv` at all: the window closes and
    the sender's writes block, a stalled receiver and not a refused one.
"""

import json
import os
import re
import socket
import sys
import threading
import time

PORT = 5140
READY = "/tmp/syslog-receiver-ready"
STALL = "/tmp/syslog-stall"
RESULTS = "/tmp/syslog-received.jsonl"
RCVBUF = 4096
MAX_FRAME = 1 << 20
# The fields of Polaris's JSON line the gate checks; the record keeps their values.
FIELDS = ("hostName", "sequence", "loggerName", "level", "mdc", "threadName", "timestamp")
# Request IDs the gate sends (gates/syslog_load.py); a cut line still shows the start of one.
REQUEST_ID = re.compile(r"syslog-\d+-[ab]-[a-z]+\d*-\d+(?:-x+)?")
SAMPLED = 50
ENVELOPE = re.compile(
    r"<(\d{1,3})>(\d+) (\S+) (\S+) (\S+) (\S+) (\S+) (-|(?:\[(?:[^\]\\]|\\.)*\])+)(?: (.*))?",
    re.DOTALL)


def _scan(buffer, base):
    """(frames, rest, errors) of `buffer`, which starts at stream offset `base`."""
    frames, errors, pos = [], [], 0
    while pos < len(buffer):
        end = pos
        while end < len(buffer) and 48 <= buffer[end] <= 57:
            end += 1
        if end == pos:
            errors.append(f"offset {base + pos}: a frame starts with {buffer[pos:pos + 1]!r}, "
                          "not a digit")
            return frames, b"", errors
        if end == len(buffer):
            if end - pos > 8:
                errors.append(f"offset {base + pos}: a length of more than 8 digits")
                return frames, b"", errors
            break
        if buffer[end] != 32:
            errors.append(f"offset {base + end}: the length is not followed by a space")
            return frames, b"", errors
        length = int(buffer[pos:end])
        if length > MAX_FRAME:
            errors.append(f"offset {base + pos}: a length of {length}, over {MAX_FRAME}")
            return frames, b"", errors
        start = end + 1
        if start + length > len(buffer):
            break
        frames.append(buffer[start:start + length])
        pos = start + length
    return frames, buffer[pos:], errors


def split_frames(buffer):
    """RFC 6587 octet counting: decimal digits, one space, then that many octets. Returns
    (frames, rest, errors); `rest` is a frame not yet complete, empty after an error (the
    stream cannot be resynchronised)."""
    return _scan(buffer, 0)


class Framer:
    """split_frames over a stream fed in pieces."""

    def __init__(self):
        self.buffer, self.offset, self.dead = b"", 0, False

    def feed(self, data):
        if self.dead:
            return [], []
        self.buffer += data
        frames, rest, errors = _scan(self.buffer, self.offset)
        self.offset += len(self.buffer) - len(rest)
        self.buffer, self.dead = rest, bool(errors)
        return frames, errors

    def close(self):
        """The errors of a stream that ends now: one when it ends inside a frame."""
        if self.buffer and not self.dead:
            return [f"offset {self.offset}: the stream ended inside a frame "
                    f"({len(self.buffer)} octets held)"]
        return []


def parse_syslog(frame):
    """RFC 5424's <PRI>VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID STRUCTURED-DATA MSG as
    a dict; '-' is None. Raises ValueError when the frame is no such message."""
    text = frame.decode("utf-8", errors="replace") if isinstance(frame, bytes) else frame
    match = ENVELOPE.fullmatch(text)
    if not match:
        raise ValueError(f"not an RFC 5424 message: {text[:60]!r}")
    pri, version, timestamp, host, app, procid, msgid, sd, msg = match.groups()
    nil = lambda value: None if value == "-" else value  # noqa: E731
    return {"pri": int(pri), "version": int(version), "timestamp": nil(timestamp),
            "host": nil(host), "app": nil(app), "procid": nil(procid), "msgid": nil(msgid),
            "sd": nil(sd), "msg": (msg or "").removeprefix("﻿")}


def _type(value):
    return "null" if value is None else type(value).__name__


def record_of(n, frame, framing_ok=True):
    """The line the receiver keeps for frame number `n`."""
    # The octets a frame holds are the octets its prefix declared; a prefix that disagrees with
    # the content shows as a framing error on the next frame, not here.
    record = {"n": n, "framing_ok": framing_ok, "declared": len(frame), "actual": len(frame)}
    try:
        envelope = parse_syslog(frame)
    except ValueError as error:
        text = frame.decode("utf-8", errors="replace")
        record.update(json_ok=False, error=str(error), head=text[:160], tail=text[-120:])
        return record
    msg = envelope.pop("msg")
    for key in ("pri", "version", "host", "app", "procid"):
        record[key] = envelope[key]
    record.update(msg_len=len(msg.encode("utf-8")), head=msg[:160], tail=msg[-120:])
    try:
        body = json.loads(msg)
    except ValueError:
        body = None
    record["json_ok"] = isinstance(body, dict)
    request_id = None
    if record["json_ok"]:
        record["keys"] = {k: _type(v) for k, v in body.items()}
        record["fields"] = {k: body[k] for k in FIELDS if k in body}
        mdc = body.get("mdc")
        request_id = mdc.get("requestId") if isinstance(mdc, dict) else None
        if n <= SAMPLED:
            record["sample"] = {k: json.dumps(v, ensure_ascii=False)[:80]
                                for k, v in body.items()}
    else:
        found = REQUEST_ID.search(msg)
        request_id = found.group(0) if found else None
    record["request_id"] = request_id if isinstance(request_id, str) else None
    if "-long-" in msg:
        record["msg"] = msg
    return record


def pump(conn, stall, handle, poll=0.05, size=65536):
    """Reads `conn` to its end and gives each piece to `handle`; reads nothing while the
    `stall` file exists."""
    conn.settimeout(poll)
    while True:
        if os.path.exists(stall):
            time.sleep(poll)
            continue
        try:
            data = conn.recv(size)
        except socket.timeout:
            continue
        if not data:
            return
        handle(data)


def serve(host="127.0.0.1", port=PORT, ready=READY, stall=STALL, results=RESULTS,
          rcvbuf=RCVBUF, stop=None, poll=0.05, reuse=True):
    """Listens until `stop` (an Event) is set; the ready file appears after the bind."""
    stop = stop or threading.Event()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if reuse:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    try:
        listener.bind((host, port))
    except OSError:
        listener.close()
        raise
    listener.listen(8)
    listener.settimeout(poll)
    out, lock, count = open(results, "w", encoding="utf-8"), threading.Lock(), [0]
    with open(ready, "w", encoding="utf-8") as f:
        f.write("ready\n")

    def write(make):
        with lock:
            count[0] += 1
            out.write(json.dumps(make(count[0]), ensure_ascii=False) + "\n")
            out.flush()

    def connection(conn):
        framer = Framer()

        def handle(data):
            frames, errors = framer.feed(data)
            for frame in frames:
                write(lambda n, frame=frame: record_of(n, frame))
            for error in errors:
                write(lambda n, error=error: {"n": n, "framing_ok": False, "json_ok": False,
                                              "error": error})

        with conn:
            pump(conn, stall, handle, poll)
        for error in framer.close():
            write(lambda n, error=error: {"n": n, "framing_ok": False, "json_ok": False,
                                          "error": error})

    try:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            threading.Thread(target=connection, args=(conn,), daemon=True).start()
    finally:
        listener.close()
        out.close()


if __name__ == "__main__":
    sys.exit(serve())
