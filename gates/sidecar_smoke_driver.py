"""The in-pod side of gates/sidecar_smoke.py (ADR-082). Standard library only: the gate reads
this file into a ConfigMap and runs it on the pinned Python image.

  python3 sidecar_smoke_driver.py send     waits for the sidecar's syslog input on 127.0.0.1:5140,
                                           sends LINES and the burst on one connection, waits until
                                           /out has been quiet, prints `collect`'s line
  python3 sidecar_smoke_driver.py collect  prints ONE JSON line: for each file in /out, the tag (its
                                           name) and the records in it

The lines are syslog frames as Quarkus writes them (RFC 5424, octet counting, a UTF-8 BOM before
the JSON body, as lakehouse#243 measured on 89 of 89 records). Every ID is `smoke-<n>`, in the
body's `mdc.requestId` (the non-JSON body holds it in its text). The hosts, paths and IDs are
invented.
"""

import json
import os
import re
import socket
import sys
import time
from collections import namedtuple

OUT = "/out"
HOST, PORT = "127.0.0.1", 5140
# Placeholders (L2c): how long /out must be quiet, and the cap.
QUIET, CAP = 5, 60
BURST_COUNT, BURST_PAD = 2000, 1000
JAVA_APP = "/usr/lib/jvm/java-21-openjdk-21.0.11.0.10-2.el9."  # L3b's first frame, cut at 48
APP_LOGGER = "org.apache.polaris.service.catalog.IcebergRestCatalogApiService"
ACCESS_LOGGER = "io.quarkus.http.access-log"
MAPPER_LOGGER = "org.apache.polaris.service.exception.IcebergExceptionMapper"
LOCAL_LOGGER = "org.apache.polaris.service.catalog.iceberg.LocalIcebergCatalog"
NOSQL_LOGGER = "org.apache.polaris.persistence.nosql.x"
ID_RE = re.compile(r"smoke-[A-Za-z0-9-]+")

# expect: the tag below the prefix a line must arrive under (None: dropped). access: the string
# values its parsed record must hold.
Line = namedtuple("Line", "id body bom expect access")


def access_message(method, path, status, size):
    return (f'172.17.0.1 - - [30/Oct/2026:12:40:00 +0000] "{method} {path} HTTP/1.1" '
            f"{status} {size}")


def json_body(line_id, level, logger, message, sequence):
    return json.dumps({
        "timestamp": "2026-10-30T19:15:02.536706745Z", "sequence": sequence,
        "loggerClassName": "org.slf4j.impl.Slf4jLogger", "loggerName": logger, "level": level,
        "message": message, "threadName": "executor-thread-1", "threadId": 120,
        "mdc": {"requestId": line_id}, "ndc": "", "hostName": os.environ.get("HOSTNAME", "pod"),
        "processName": "quarkus-run.jar", "processId": 1}, ensure_ascii=False)


def _access(n, method, path, status, size, level="INFO", expect=None, **extra):
    line_id = f"smoke-s{n:02d}"
    keys = {"client_ip": "172.17.0.1", "user_principal_name": "-", "http_method": method,
            "api_path": path, "http_status": str(status), "response_size": str(size)}
    body = json_body(line_id, level, ACCESS_LOGGER, access_message(method, path, status, size),
                     n)
    return Line(line_id, body, True, expect, keys)


def _app(n, logger, level, expect, bom=True):
    line_id = f"smoke-s{n:02d}"
    return Line(line_id, json_body(line_id, level, logger, "an application line", n), bom,
                expect, None)


def lines():
    mgmt, cat = "/api/management/v1/catalogs", "/api/catalog/v1/x/namespaces"
    garbage = json_body("smoke-s14", "INFO", ACCESS_LOGGER, "garbage", 14)
    s03 = _access(3, "GET", cat, 500, 0, level="ERROR", expect="error.r1")
    return [
        Line("smoke-s01", json_body("smoke-s01", "INFO", NOSQL_LOGGER, "committed", 1779), True,
             None, None),
        _app(2, APP_LOGGER, "ERROR", "error.r1"),
        Line(s03.id, s03.body, True, "error.r1", None),
        _access(4, "GET", cat, 404, 0, expect="candidate.r3p"),
        _access(5, "GET", mgmt, 401, "-", expect="error.r3"),
        _access(6, "PUT", cat + "/n/tables/t", 200, 512, expect="candidate.r4"),
        _access(7, "GET", cat, 200, 512),
        _access(8, "POST", mgmt, 201, 64, expect="candidate.r5"),
        _access(9, "POST", cat, 200, 64),
        _app(10, MAPPER_LOGGER, "INFO", "candidate.r2b"),
        _app(11, LOCAL_LOGGER, "INFO", None),
        Line("smoke-s12", "this is not json (smoke-s12)", True, "error.r7", None),
        _app(13, APP_LOGGER, "ERROR", "error.r1", bom=False),
        Line("smoke-s14", garbage, True, "error.r7a", None),
        _access(15, "OPTIONS", "/api/catalog/v1/config", 200, 0, expect="error.r7a"),
        Line("smoke-s16", '{"foo":"bar"}', True, None, None),
    ]


def burst(count=BURST_COUNT, pad=BURST_PAD):
    for i in range(count):
        line_id = f"smoke-burst-{i}"
        yield Line(line_id, json_body(line_id, "ERROR", APP_LOGGER, "x" * pad, i), True,
                   "error.r1", None)


def build_frame(body, bom=True, host="pod", app=JAVA_APP):
    """`<n> <frame>`, n the octets of the frame (the BOM is three); the prefix is not counted."""
    frame = f"<14>1 2026-10-30T19:15:02.536+00:00 {host} {app} 1 - - " \
            + ("﻿" if bom else "") + body
    return f"{len(frame.encode('utf-8'))} {frame}"


def record_id(record):
    mdc = record.get("mdc")
    if isinstance(mdc, dict) and isinstance(mdc.get("requestId"), str):
        return mdc["requestId"]
    found = ID_RE.search(json.dumps(record, ensure_ascii=False))
    return found.group(0) if found else None


def collect(out=OUT):
    files = {}
    for name in sorted(os.listdir(out)):
        records = []
        with open(os.path.join(out, name), encoding="utf-8") as f:
            for text in f.read().splitlines():
                try:
                    record = json.loads(text)
                except ValueError:
                    record = {"_raw": text}
                records.append(record if isinstance(record, dict) else {"_raw": text})
        files[name] = records
    return files


def _size(out=OUT):
    return sum(os.path.getsize(os.path.join(out, n)) for n in os.listdir(out))


def wait_quiet(out=OUT, quiet=QUIET, cap=CAP):
    end, last, since = time.monotonic() + cap, -1, time.monotonic()
    while time.monotonic() < end:
        size = _size(out)
        if size != last:
            last, since = size, time.monotonic()
        elif time.monotonic() - since >= quiet:
            return
        time.sleep(1)


def send():
    host = os.environ.get("HOSTNAME", "pod")
    end = time.monotonic() + 120
    while True:
        try:
            sock = socket.create_connection((HOST, PORT), timeout=5)
            break
        except OSError:
            if time.monotonic() > end:
                raise
            time.sleep(1)
    with sock:
        for line in lines() + list(burst()):
            sock.sendall(build_frame(line.body, line.bom, host).encode("utf-8"))
    wait_quiet()


def main(argv):
    if argv[1:] == ["send"]:
        send()
    elif argv[1:] != ["collect"]:
        sys.exit("usage: sidecar_smoke_driver.py send|collect")
    print(json.dumps(collect(), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
