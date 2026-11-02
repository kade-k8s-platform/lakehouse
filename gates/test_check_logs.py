"""Tests for the log batch on the gate (ADR-036): `gate.py batch-image` and `check-logs`, and
logs_traffic.sh run here with this machine's curl against local fakes of Polaris replicas.
Run: python3 -m unittest discover -s gates"""

import datetime as dt
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "images", "polaris-log-batch"))
import gate  # noqa: E402
import polaris_log_batch  # noqa: E402
from test_check_polaris import CREDS, TOKEN  # noqa: E402

ENV = {"NS_CATALOG_POLARIS": "catalog-polaris"}
PODS = ["polaris-6d9f-aaaaa", "polaris-6d9f-bbbbb"]
PREFIX = "gate-logs-42"
NOW = "2026-10-11T13:01:01Z"
ACCESS = "io.quarkus.http.access-log"
ADMIN = "org.apache.polaris.service.admin.PolarisServiceImpl"
MAPPER = "org.apache.polaris.service.exception.IcebergExceptionMapper"
COMMIT = "org.apache.polaris.service.catalog.iceberg.LocalIcebergCatalog"


def polaris_record(pod, second, request_id, logger, message, level="INFO", **extra):
    """One line as Polaris 1.6.0 writes it (docs/contracts/polaris-logs.md)."""
    return json.dumps({
        "timestamp": f"2026-10-11T12:40:{second:02d}.753600155Z", "sequence": second,
        "loggerClassName": "org.jboss.logging.Logger", "loggerName": logger, "level": level,
        "message": message, "threadName": "executor-thread-2", "threadId": 31,
        "mdc": {"requestId": request_id or f"generated-{pod}-{second}", "realmId": "POLARIS"},
        "ndc": "", "hostName": pod, "processName": "/usr/lib/jvm/java-21/bin/java",
        "processId": 1, **extra}) + "\n"


def access_message(who, request, status):
    return f"10.0.0.9 - {who} [11/Oct/2026:12:40:47 +0000] \"{request} HTTP/1.1\" {status}"


def polaris_line(pod, second, request_id=None, kind="read"):
    """One access line: root listing the catalogs, or (`denied`) a request without a token."""
    who, status = ("root", "200 15") if kind == "read" else ("-", "401 -")
    return polaris_record(pod, second, request_id, ACCESS,
                          access_message(who, "GET /api/management/v1/catalogs", status))


# The lines Polaris 1.6.0 wrote for each of check-logs' other requests, measured on 2026-10-11:
# its application lines, then its access line.
SPACE = f"/api/catalog/v1/{gate.LOGS_CATALOG}/namespaces/{gate.LOGS_NAMESPACE}"
POLICY_LINES = {
    "catalog": [(ADMIN, "Created new catalog class PolarisCatalog {\n    class Catalog {"),
                (ACCESS, access_message("root", "POST /api/management/v1/catalogs", "201 261"))],
    "invalid": [(MAPPER, "Handling runtimeException Invalid schema: null"),
                (ACCESS, access_message("root", f"POST {SPACE}/tables", "400 89"))],
    "missing": [(MAPPER, "Handling runtimeException Table does not exist: logs.absent"),
                (ACCESS, access_message("root", f"GET {SPACE}/tables/absent", "404 98"))],
    "commit": [(COMMIT, "Successfully committed to table {catalog}.logs.{table} in 311 ms"),
               (ACCESS, access_message("root", f"POST {SPACE}/tables", "200 836"))],
}
# A table made in a catalog whose S3 endpoint does not answer, as Polaris 1.6.0 wrote it
# (2026-10-11, claude/batch-roll-fixture at aa38b9d): the mapper's line, its ERROR line with
# the exception, and the access line of the 500.
EXCEPTION = {"exceptionType": "software.amazon.awssdk.core.exception.SdkClientException",
             "message": "Unable to execute HTTP request: Connect to s3 failed",
             "frames": [{"class": "software.amazon.awssdk.core.exception.SdkClientException",
                         "method": "create", "line": 111}],
             "causedBy": {"exception": {
                 "exceptionType": "org.apache.http.conn.HttpHostConnectException",
                 "message": "Connection refused", "frames": [], "refId": 2}},
             "suppressed": [{"exceptionType":
                             "software.amazon.awssdk.core.exception.SdkClientException",
                             "message": "Request attempt 1 failure", "frames": [],
                             "refId": 3}],
             "refId": 1}
DOWN_SPACE = f"/api/catalog/v1/{gate.LOGS_CATALOG}_down/namespaces/{gate.LOGS_NAMESPACE}"
FAILED_LINES = [
    (MAPPER, "INFO", "Handling runtimeException Unable to execute HTTP request", {}),
    (MAPPER, "ERROR", "Unhandled exception returning INTERNAL_SERVER_ERROR",
     {"exception": EXCEPTION}),
    (ACCESS, "INFO", access_message("root", f"POST {DOWN_SPACE}/tables", "500 120"), {})]


def polaris_lines(pod, second, request_id, kind):
    """Every line Polaris writes for one of check-logs' requests, from `second` on."""
    if kind in ("read", "denied"):
        return [polaris_line(pod, second, request_id, kind)]
    if kind == "failed":
        return [polaris_record(pod, second + i, request_id, logger, message, level, **extra)
                for i, (logger, level, message, extra) in enumerate(FAILED_LINES)]
    return [polaris_record(pod, second + i, request_id, logger, message.replace(
        "{catalog}", gate.LOGS_CATALOG).replace("{table}", gate.logs_table(request_id)))
        for i, (logger, message) in enumerate(POLICY_LINES[kind])]


def batch_outputs(log_dir, lines_by_pod, now=NOW):
    """Polaris's files written, then the real batch run over them as the gate runs it."""
    for pod, lines in lines_by_pod.items():
        path = os.path.join(log_dir, f"polaris-{pod}.log")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.utime(path, (1791722460, 1791722460))  # 2026-10-11T12:41:00Z
    out = subprocess.run([sys.executable, os.path.join(ROOT, "images", "polaris-log-batch",
                                                       "polaris_log_batch.py"),
                          "--log-dir", log_dir, "--no-pod-list", "--now", now],
                         capture_output=True, text=True, check=True).stdout
    return gate.batch_events(out)


def read_as_fluent_bit(log_dir):
    """What the stand-in prints: every line of both directories with its path added."""
    records = []
    for sub in ("processed-logs", "aggregated-logs"):
        for name in sorted(os.listdir(os.path.join(log_dir, sub))):
            with open(os.path.join(log_dir, sub, name), encoding="utf-8") as f:
                for line in f:
                    records.append({**json.loads(line),
                                    "source_file": f"/logs/{sub}/{name}"})
    return records


def listing(log_dir):
    out = subprocess.run(["sh", "-s", "--", log_dir], input=gate.LOGS_LISTING,
                         capture_output=True, text=True, check=True).stdout
    return gate.parse_listing(out)


class Fixture:
    """The batch run twice over both pods' lines, and what the reader saw."""

    def __init__(self, sent=None):
        self.sent = sent or gate.logs_request_ids(PREFIX, 2, PODS)
        self.dir = tempfile.TemporaryDirectory()
        lines = {pod: [polaris_line(pod, 1)] for pod in PODS}
        second = 2
        for rid, (pod, kind) in sorted(self.sent.items()):
            written = polaris_lines(pod or PODS[0], second, rid, kind)
            lines[pod or PODS[0]] += written
            second += len(written)
        self.events = [batch_outputs(self.dir.name, lines)]
        self.before = listing(self.dir.name)
        out = subprocess.run([sys.executable, os.path.join(
            ROOT, "images", "polaris-log-batch", "polaris_log_batch.py"),
            "--log-dir", self.dir.name, "--no-pod-list", "--now", NOW],
            capture_output=True, text=True, check=True).stdout
        self.events.append(gate.batch_events(out))
        self.after = listing(self.dir.name)
        self.records = read_as_fluent_bit(self.dir.name)

    def problems(self, **change):
        args = {"sent": self.sent, "pods": PODS, "events": self.events,
                "before": self.before, "after": self.after, "records": self.records}
        args.update(change)
        return gate.logs_problems(**args)


class Pins(unittest.TestCase):
    def test_the_stand_in_fluent_bit_is_pinned_by_digest(self):
        pins = gate.load_pins()
        self.assertRegex(pins["standin_images"]["fluent_bit"]["image"],
                         r"^fluent/fluent-bit:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")


class BatchImage(unittest.TestCase):
    def test_builds_the_image_release_py_names_then_loads_it_into_kind(self):
        build, load = gate.batch_image_commands()
        image = gate.release.batch_image()
        self.assertEqual(build[:2], ["docker", "build"])
        self.assertEqual(build[build.index("--tag") + 1], image)
        self.assertEqual(build[build.index("--file") + 1],
                         os.path.join(ROOT, "images", "polaris-log-batch", "Dockerfile"))
        self.assertEqual(build[-1], os.path.join(ROOT, "images", "polaris-log-batch"))
        self.assertEqual(load, ["kind", "load", "docker-image", image, "--name", gate.CLUSTER])


class Traffic(unittest.TestCase):
    def test_each_pod_gets_requests_for_each_rule_and_the_catalog_is_made_once(self):
        sent = gate.logs_request_ids(PREFIX, 3, PODS)
        self.assertEqual(len(sent), 2 * (3 * 2 + 3) + 1)
        self.assertEqual(sent[f"{PREFIX}-setup-catalog"], (None, "catalog"))
        for pod in PODS:
            kinds = sorted(kind for p, kind in sent.values() if p == pod)
            self.assertEqual(kinds, ["commit"] + ["denied"] * 3 + ["invalid", "missing"]
                             + ["read"] * 3)
        for rid, (pod, kind) in sent.items():
            if pod:
                self.assertTrue(rid.startswith(f"{PREFIX}-{pod}-{kind}-"), rid)

    def test_with_failures_each_pod_also_gets_a_500_and_the_down_catalog_is_made_once(self):
        plain = gate.logs_request_ids(PREFIX, 3, PODS)
        sent = gate.logs_request_ids(PREFIX, 3, PODS, failing=True)
        self.assertEqual(set(sent) - set(plain),
                         {f"{PREFIX}-{pod}-failed-1" for pod in PODS}
                         | {f"{PREFIX}-setup-down-catalog"})
        self.assertEqual(sent[f"{PREFIX}-setup-down-catalog"], (None, "catalog"))
        for pod in PODS:
            self.assertEqual(sent[f"{PREFIX}-{pod}-failed-1"], (pod, "failed"))

    def test_a_commits_table_is_named_for_its_request(self):
        self.assertEqual(gate.logs_table(f"{PREFIX}-{PODS[0]}-commit-1"),
                         "gate_logs_42_polaris_6d9f_aaaaa_commit_1")

    def test_the_traffic_pod_gets_roots_credentials_and_asks_each_pod_by_its_address(self):
        env = {**ENV, "S3_ENDPOINT": "http://s3.test:8333", "S3_REGION": "us-east-1",
               "POLARIS_WAREHOUSE_BUCKET": "warehouse"}
        argv = gate.logs_traffic_command(env, "curl:1@sha256:" + "e" * 64, "logs-traffic",
                                         PREFIX, 3, [(PODS[0], "http://10.0.0.1:8181"),
                                                     (PODS[1], "http://10.0.0.2:8181")])
        self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "catalog-polaris", "run", "logs-traffic"])
        overrides = json.loads(argv[argv.index("--overrides") + 1])
        (container,) = overrides["spec"]["containers"]
        env = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]}
        self.assertEqual(env, {"ROOT_CLIENT_ID": {"name": "polaris-root", "key": "clientId"},
                               "ROOT_CLIENT_SECRET": {"name": "polaris-root",
                                                      "key": "clientSecret"}})
        self.assertEqual(argv[argv.index("--") + 1:],
                         ["sh", "-s", "--", "http://polaris:8181", "POLARIS", "3", PREFIX,
                          "gate_logs", "http://s3.test:8333", "us-east-1", "warehouse",
                          PODS[0], "http://10.0.0.1:8181", PODS[1], "http://10.0.0.2:8181"])
        self.assertNotIn("LOGS_DOWN_ENDPOINT", json.dumps(overrides))

    def test_with_a_down_endpoint_the_traffic_pod_gets_it(self):
        env = {**ENV, "S3_ENDPOINT": "http://s3.test:8333", "S3_REGION": "us-east-1",
               "POLARIS_WAREHOUSE_BUCKET": "warehouse"}
        argv = gate.logs_traffic_command(env, "curl:1@sha256:" + "e" * 64, "logs-traffic",
                                         PREFIX, 3, [(PODS[0], "http://10.0.0.1:8181")],
                                         down=DOWN)
        (container,) = json.loads(argv[argv.index("--overrides") + 1])["spec"]["containers"]
        self.assertIn({"name": "LOGS_DOWN_ENDPOINT", "value": DOWN}, container["env"])


DOWN = "http://127.0.0.1:9"


def fake_catalog(signing_key=TOKEN, fail=None, catalogs=None, down=DOWN):
    """A Polaris replica for logs_traffic.sh: a token for root's client credentials, the
    catalogs listed with that token (401 without), and one catalog's API as Polaris 1.6.0
    answers it. `catalogs` is shared between replicas, as their database is; `fail` names a
    request kind to answer 500; a table made in a catalog on the S3 endpoint `down` is answered
    500, as Polaris answers when that endpoint does not answer."""
    state = catalogs if catalogs is not None else {}
    seen = []

    class Replica(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def reply(self, code, body=None):
            data = json.dumps(body).encode() if body is not None else b""
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def handle_one(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else ""
            seen.append((method, self.path, dict(self.headers), body))
            if self.path == "/api/catalog/v1/oauth/tokens":
                form = urllib.parse.parse_qs(body)
                if form.get("client_secret") == [CREDS["ROOT_CLIENT_SECRET"]]:
                    return self.reply(200, {"access_token": signing_key})
                return self.reply(401)
            if self.headers.get("Authorization") != f"Bearer {signing_key}":
                return self.reply(401)
            kind = self.headers.get("X-Request-ID", "").split("-")[-2]
            if kind == fail:
                return self.reply(500)
            if (method, self.path) == ("GET", "/api/management/v1/catalogs"):
                return self.reply(200, {"catalogs": []})
            if (method, self.path) == ("POST", "/api/management/v1/catalogs"):
                catalog = json.loads(body)["catalog"]
                if catalog["name"] in state:
                    return self.reply(409)
                state[catalog["name"]] = {"endpoint": catalog["storageConfigInfo"]["endpoint"],
                                          "namespaces": set()}
                return self.reply(201)
            parts = self.path.split("/")
            name = parts[4] if len(parts) > 5 and parts[5] == "namespaces" else None
            if name not in state:
                return self.reply(404)
            space = f"/api/catalog/v1/{name}/namespaces"
            if (method, self.path) == ("POST", space):
                ns = json.loads(body)["namespace"][0]
                if ns in state[name]["namespaces"]:
                    return self.reply(409)
                state[name]["namespaces"].add(ns)
                return self.reply(200)
            if (method, self.path) == ("GET", f"{space}/{gate.LOGS_NAMESPACE}/tables/absent"):
                return self.reply(404)
            if (method, self.path) == ("POST", f"{space}/{gate.LOGS_NAMESPACE}/tables"):
                if "schema" not in json.loads(body):
                    return self.reply(400)
                if state[name]["endpoint"] == down:
                    return self.reply(500)
                return self.reply(200, {"metadata-location": "s3://x"})
            self.reply(404)

        def do_GET(self):
            self.handle_one("GET")

        def do_POST(self):
            self.handle_one("POST")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Replica)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


class TrafficScript(unittest.TestCase):
    def setUp(self):
        self.servers = []
        self.catalogs = {}

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def replica(self, **kwargs):
        server, seen = fake_catalog(catalogs=self.catalogs, **kwargs)
        self.servers.append(server)
        return f"http://127.0.0.1:{server.server_port}", seen

    def run_script(self, api, pods, prefix=PREFIX, down=None):
        with open(os.path.join(HERE, "logs_traffic.sh"), encoding="utf-8") as f:
            text = f.read()
        args = [a for name, url in pods for a in (name, url)]
        env = {k: v for k, v in os.environ.items() if k != "LOGS_DOWN_ENDPOINT"}
        env.update(CREDS)
        if down:
            env["LOGS_DOWN_ENDPOINT"] = down
        return subprocess.run(["sh", "-s", "--", api, "POLARIS", "2", prefix, gate.LOGS_CATALOG,
                               "http://s3.test:8333", "us-east-1", "warehouse", *args],
                              input=text, capture_output=True, text=True, env=env, timeout=60)

    def test_each_pod_is_read_with_roots_token_and_asked_without_one(self):
        (a, seen_a), (b, seen_b) = self.replica(), self.replica()
        done = self.run_script(a, [(PODS[0], a), (PODS[1], b)])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for pod, seen in ((PODS[0], seen_a), (PODS[1], seen_b)):
            asked = [(h.get("X-Request-ID"), h.get("Authorization"))
                     for method, path, h, _ in seen
                     if (method, path) == ("GET", "/api/management/v1/catalogs")]
            self.assertEqual(asked, [(f"{PREFIX}-{pod}-read-1", f"Bearer {TOKEN}"),
                                     (f"{PREFIX}-{pod}-denied-1", None),
                                     (f"{PREFIX}-{pod}-read-2", f"Bearer {TOKEN}"),
                                     (f"{PREFIX}-{pod}-denied-2", None)])
        self.assertNotIn(CREDS["ROOT_CLIENT_SECRET"], done.stdout + done.stderr)
        self.assertNotIn(TOKEN, done.stdout + done.stderr)

    def test_the_catalog_is_made_on_s3_then_each_pod_gets_a_400_a_404_and_a_commit(self):
        (a, seen_a), (b, seen_b) = self.replica(), self.replica()
        done = self.run_script(a, [(PODS[0], a), (PODS[1], b)])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        made = [(h["X-Request-ID"], json.loads(body)) for method, path, h, body in seen_a
                if (method, path) == ("POST", "/api/management/v1/catalogs")]
        self.assertEqual(made, [(f"{PREFIX}-setup-catalog", {"catalog": {
            "type": "INTERNAL", "name": "gate_logs",
            "properties": {"default-base-location": "s3://warehouse/gate_logs"},
            "storageConfigInfo": {"storageType": "S3",
                                  "allowedLocations": ["s3://warehouse/gate_logs"],
                                  "endpoint": "http://s3.test:8333", "region": "us-east-1",
                                  "pathStyleAccess": True, "stsUnavailable": True}}})])
        space = "/api/catalog/v1/gate_logs/namespaces"
        for pod, seen in ((PODS[0], seen_a), (PODS[1], seen_b)):
            policy = [(method, path, h, body) for method, path, h, body in seen
                      if any(h.get("X-Request-ID") == f"{PREFIX}-{pod}-{kind}-1"
                             for kind in gate.LOGS_POLICY_KINDS)]
            asked = [(h["X-Request-ID"], method, path, body and json.loads(body))
                     for method, path, h, body in policy]
            rid = f"{PREFIX}-{pod}-commit-1"
            self.assertEqual(asked, [
                (f"{PREFIX}-{pod}-invalid-1", "POST", f"{space}/logs/tables",
                 {"name": gate.logs_table(f"{PREFIX}-{pod}-invalid-1")}),
                (f"{PREFIX}-{pod}-missing-1", "GET", f"{space}/logs/tables/absent", ""),
                (rid, "POST", f"{space}/logs/tables", {"name": gate.logs_table(rid), "schema": {
                    "type": "struct", "schema-id": 0, "fields": [
                        {"id": 1, "name": "id", "required": False, "type": "long"}]}})])
            self.assertTrue(all(h.get("Authorization") == f"Bearer {TOKEN}"
                                for _, _, h, _ in policy))
        for kind, code in (("invalid", 400), ("missing", 404), ("commit", 200)):
            self.assertIn(f"ok   {PREFIX}-{PODS[1]}-{kind}-1: {PODS[1]} answered HTTP {code}",
                          done.stdout)

    def test_a_catalog_already_there_is_kept(self):
        a, _ = self.replica()
        self.assertEqual(self.run_script(a, [(PODS[0], a)]).returncode, 0)
        done = self.run_script(a, [(PODS[0], a)], prefix="gate-logs-43")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("gate-logs-43-setup-catalog: answered HTTP 409", done.stdout)

    def test_a_commit_that_fails_fails_the_script(self):
        (a, _), (b, _) = self.replica(), self.replica(fail="commit")
        done = self.run_script(a, [(PODS[0], a), (PODS[1], b)])
        self.assertEqual(done.returncode, 1)
        self.assertIn(f"FAIL {PREFIX}-{PODS[1]}-commit-1: {PODS[1]} answered HTTP 500, "
                      "expected 200", done.stdout)
        self.assertIn(f"ok   {PREFIX}-{PODS[0]}-commit-1", done.stdout)

    def test_without_a_down_endpoint_nothing_fails_on_purpose(self):
        a, seen = self.replica()
        done = self.run_script(a, [(PODS[0], a)])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse([path for _, path, _, _ in seen if "_down" in path])
        self.assertNotIn("failed-1", done.stdout)

    def test_with_a_down_endpoint_each_pod_gets_a_500_from_the_down_catalog(self):
        (a, seen_a), (b, seen_b) = self.replica(), self.replica()
        done = self.run_script(a, [(PODS[0], a), (PODS[1], b)], down=DOWN)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        made = {json.loads(body)["catalog"]["name"]: json.loads(body)["catalog"]
                for method, path, h, body in seen_a
                if (method, path) == ("POST", "/api/management/v1/catalogs")}
        down = made[f"{gate.LOGS_CATALOG}_down"]
        self.assertEqual(down["storageConfigInfo"]["endpoint"], DOWN)
        self.assertEqual(down["properties"]["default-base-location"],
                         f"s3://warehouse/{gate.LOGS_CATALOG}_down")
        for pod, seen in ((PODS[0], seen_a), (PODS[1], seen_b)):
            rid = f"{PREFIX}-{pod}-failed-1"
            [(path, body)] = [(path, json.loads(body)) for method, path, h, body in seen
                              if h.get("X-Request-ID") == rid]
            self.assertEqual(path, f"{DOWN_SPACE}/tables")
            self.assertEqual(body["name"], gate.logs_table(rid))
            self.assertIn("schema", body)
            self.assertIn(f"ok   {rid}: {pod} answered HTTP 500", done.stdout)

    def test_a_down_catalog_that_answers_fails_the_script(self):
        (a, _), (b, _) = self.replica(down="http://nothing.test"), self.replica()
        done = self.run_script(a, [(PODS[0], a), (PODS[1], b)], down=DOWN)
        self.assertEqual(done.returncode, 1)
        self.assertIn(f"FAIL {PREFIX}-{PODS[0]}-failed-1: {PODS[0]} answered HTTP 200, "
                      "expected 500", done.stdout)

    def test_a_pod_that_refuses_fails_the_script(self):
        (a, _), (b, _) = self.replica(), self.replica(signing_key="other-key")
        done = self.run_script(a, [(PODS[0], a), (PODS[1], b)])
        self.assertEqual(done.returncode, 1)
        self.assertIn(f"FAIL {PREFIX}-{PODS[1]}-read-1", done.stdout)


class BatchRun(unittest.TestCase):
    def test_now_is_past_the_hour_the_traffic_ended_in(self):
        for t, now in (("2026-10-11T13:00:00+00:00", "2026-10-11T14:01:01Z"),
                       ("2026-10-11T13:59:59+00:00", "2026-10-11T14:01:01Z"),
                       ("2026-10-11T23:30:00+00:00", "2026-10-12T00:01:01Z")):
            with self.subTest(t=t):
                self.assertEqual(gate.batch_now(dt.datetime.fromisoformat(t)), now)

    def test_the_batch_publishes_the_hour_of_the_traffic_at_that_now(self):
        with tempfile.TemporaryDirectory() as d:
            t = dt.datetime(2026, 10, 11, 12, 59, 59, tzinfo=dt.timezone.utc)
            events = batch_outputs(d, {PODS[0]: [polaris_line(PODS[0], 7)]},
                                   now=gate.batch_now(t))
            published = [e["hour"] for e in events if e["event"] == "published"]
            self.assertEqual(published, ["20261011-12"])

    def test_the_job_is_the_cronjobs_own_template_with_now_added(self):
        cronjob = {"apiVersion": "batch/v1", "kind": "CronJob",
                   "metadata": {"name": "polaris-log-batch", "namespace": "catalog-polaris",
                                "uid": "u-1", "resourceVersion": "9"},
                   "spec": {"schedule": "3 * * * *", "jobTemplate": {
                       "metadata": {"labels": {"app.kubernetes.io/name": "polaris-log-batch"}},
                       "spec": {"backoffLimit": 0, "template": {"spec": {
                           "serviceAccountName": "polaris-log-batch",
                           "containers": [{"name": "batch", "image": "lakehouse/b:1"}],
                           "volumes": [{"name": "polaris-logs"}]}}}}}}
        job = gate.batch_job(cronjob, "polaris-log-batch-gate-1", NOW)
        self.assertEqual((job["apiVersion"], job["kind"]), ("batch/v1", "Job"))
        self.assertEqual(job["metadata"], {
            "name": "polaris-log-batch-gate-1", "namespace": "catalog-polaris",
            "labels": {"app.kubernetes.io/name": "polaris-log-batch"}})
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["containers"][0]["args"], ["--now", NOW])
        self.assertEqual(pod["serviceAccountName"], "polaris-log-batch")
        self.assertEqual(pod["volumes"], [{"name": "polaris-logs"}])
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertNotIn("args", cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                         ["containers"][0], "the CronJob read back is changed")

    def test_a_job_is_done_once_it_succeeded_or_failed(self):
        self.assertIsNone(gate.job_outcome({"status": {"active": 1}}))
        self.assertEqual(gate.job_outcome({"status": {"succeeded": 1}}), "succeeded")
        self.assertEqual(gate.job_outcome({"status": {"failed": 1}}), "failed")

    def test_the_batchs_events_are_its_json_lines(self):
        self.assertEqual(gate.batch_events('{"event":"idle"}\nTraceback\n\n'),
                         [{"event": "idle"}])


class Listing(unittest.TestCase):
    def test_each_output_file_with_its_hash_and_lines_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as d:
            for sub in ("processed-logs", "aggregated-logs", "done", ".state"):
                os.makedirs(os.path.join(d, sub))
            for rel, text in (("processed-logs/20261011-12.jsonl", "{}\n{}\n"),
                              ("processed-logs/20261011-11.jsonl", ""),
                              ("aggregated-logs/20261011-12.jsonl", "{}\n"),
                              ("done/x.log", "{}\n"), (".state/checkpoint.json", "{}"),
                              ("polaris-a.log", "{}\n")):
                with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                    f.write(text)
            found = listing(d)
        self.assertEqual(sorted(found), ["aggregated-logs/20261011-12.jsonl",
                                         "processed-logs/20261011-11.jsonl",
                                         "processed-logs/20261011-12.jsonl"])
        self.assertEqual(found["processed-logs/20261011-12.jsonl"]["lines"], 2)
        self.assertEqual(found["processed-logs/20261011-11.jsonl"]["lines"], 0)
        self.assertRegex(found["aggregated-logs/20261011-12.jsonl"]["sha256"], r"^[0-9a-f]{64}$")

    def test_no_output_yet_is_an_empty_listing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(listing(d), {})

    def test_the_listing_pod_mounts_the_claim_read_only(self):
        argv = gate.logs_listing_command(ENV, "curl:1@sha256:" + "e" * 64, "logs-list")
        self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "catalog-polaris", "run", "logs-list"])
        spec = json.loads(argv[argv.index("--overrides") + 1])["spec"]
        self.assertEqual(spec["volumes"], [{"name": "polaris-logs", "persistentVolumeClaim": {
            "claimName": "polaris-logs", "readOnly": True}}])
        self.assertEqual(spec["containers"][0]["volumeMounts"], [
            {"name": "polaris-logs", "mountPath": "/logs", "readOnly": True}])
        self.assertEqual(argv[argv.index("--") + 1:], ["sh", "-s", "--", "/logs"])


class FluentBit(unittest.TestCase):
    def test_the_stand_in_reads_both_directories_from_the_start_read_only(self):
        pod = gate.fluent_bit_pod(ENV, "fluent/fluent-bit:1@sha256:" + "f" * 64, "fb-1")
        self.assertEqual(pod["metadata"]["namespace"], "catalog-polaris")
        spec = pod["spec"]
        self.assertEqual(spec["restartPolicy"], "Never")
        self.assertEqual(spec["volumes"], [{"name": "polaris-logs", "persistentVolumeClaim": {
            "claimName": "polaris-logs", "readOnly": True}}])
        (container,) = spec["containers"]
        self.assertEqual(container["volumeMounts"], [
            {"name": "polaris-logs", "mountPath": "/logs", "readOnly": True}])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertNotIn(spec["securityContext"]["runAsUser"], (0, 10000),
                         "the reader is neither root nor the user Polaris and the batch run as")
        args = " ".join(container["command"] + container["args"])
        for part in ("-i tail", "path=/logs/processed-logs/*.jsonl,/logs/aggregated-logs/*.jsonl",
                     "read_from_head=true", "exit_on_eof=true", "parser=json",
                     "path_key=source_file", "-o stdout", "format=json_lines",
                     "json_date_key=false"):
            self.assertIn(part, args)

    def test_its_records_are_the_json_lines_of_its_output(self):
        text = ("Fluent Bit v5.1.2\n[info] [engine] started\n"
                '{"a":1,"source_file":"/logs/processed-logs/x.jsonl"}\n')
        self.assertEqual(gate.fluent_bit_records(text),
                         [{"a": 1, "source_file": "/logs/processed-logs/x.jsonl"}])


class Problems(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = Fixture()

    @classmethod
    def tearDownClass(cls):
        cls.f.dir.cleanup()

    def test_the_real_batchs_output_read_as_the_reader_does_passes(self):
        problems, notes = self.f.problems()
        self.assertEqual(problems, [])
        text = "\n".join(notes)
        self.assertIn("4 kept request IDs", text)
        self.assertIn("4 counted", text)
        self.assertIn("2 404s dropped with their application lines", text)
        self.assertIn("2 400s kept with theirs", text)
        self.assertIn("2 commits on their tables' rows", text)
        for pod in PODS:
            self.assertIn(pod, text)
        self.assertIn("contract v2", text)
        self.assertIn("second run", text)

    def kept(self):
        return sorted(r for r, (_, kind) in self.f.sent.items() if kind == "denied")

    def test_without_500s_the_notes_name_none(self):
        self.assertNotIn("500", "\n".join(self.f.problems()[1]))


    def test_a_request_id_missing_or_read_twice_fails(self):
        rid = self.kept()[0]
        records = [r for r in self.f.records if r.get("mdc", {}).get("requestId") != rid]
        problems, _ = self.f.problems(records=records)
        self.assertIn(rid, "\n".join(problems))
        twice = [r for r in self.f.records if r.get("mdc", {}).get("requestId") == rid]
        problems, _ = self.f.problems(records=self.f.records + twice)
        self.assertIn(rid, "\n".join(problems))

    def test_a_request_id_from_another_pod_fails(self):
        rid = self.kept()[0]
        pod = self.f.sent[rid][0]
        records = [dict(r, hostName=PODS[1] if pod == PODS[0] else PODS[0])
                   if r.get("mdc", {}).get("requestId") == rid else r for r in self.f.records]
        problems, _ = self.f.problems(records=records)
        self.assertIn(rid, "\n".join(problems))

    def test_a_counted_request_in_the_processed_lines_fails(self):
        rid = next(r for r, (_, kind) in sorted(self.f.sent.items()) if kind == "read")
        [doc] = [r for r in self.f.records if r.get("mdc", {}).get("requestId") == self.kept()[0]]
        leaked = dict(doc, mdc={"requestId": rid, "realmId": "POLARIS"})
        problems, _ = self.f.problems(records=self.f.records + [leaked])
        self.assertIn(rid, "\n".join(problems))

    def test_rows_that_do_not_count_the_requests_sent_fail(self):
        records = [dict(r, reads=0) if r.get("user_principal_name") == "root"
                   and r.get("report_type") == "principal" else r for r in self.f.records]
        problems, _ = self.f.problems(records=records)
        self.assertIn("root", "\n".join(problems))
        records = [dict(r, auth_denied=0) if r.get("resource") == "/api/management/v1/catalogs"
                   else r for r in self.f.records]
        problems, _ = self.f.problems(records=records)
        self.assertIn("/api/management/v1/catalogs", "\n".join(problems))

    def test_a_summary_whose_books_do_not_balance_fails(self):
        records = [dict(r, dropped=r["dropped"] + 1) if r.get("report_type") == "summary"
                   else r for r in self.f.records]
        problems, _ = self.f.problems(records=records)
        self.assertIn("dropped", "\n".join(problems))

    def test_a_line_the_contract_refuses_fails(self):
        records = [dict(self.f.records[0], event_id="not-a-sha1")] + self.f.records[1:]
        problems, _ = self.f.problems(records=records)
        self.assertIn("event_id", "\n".join(problems))

    def test_fewer_records_than_lines_fails(self):
        summary = [r for r in self.f.records if r.get("report_type") == "summary"]
        problems, _ = self.f.problems(records=[r for r in self.f.records if r not in summary])
        self.assertIn("aggregated-logs/", "\n".join(problems))

    def test_a_pod_whose_file_was_not_read_fails(self):
        problems, _ = self.f.problems(pods=PODS + ["polaris-6d9f-ccccc"])
        self.assertIn("polaris-6d9f-ccccc", "\n".join(problems))

    def test_a_second_run_that_changes_a_file_or_publishes_fails(self):
        path = sorted(self.f.after)[0]
        after = {**self.f.after, path: {**self.f.after[path], "sha256": "0" * 64}}
        problems, _ = self.f.problems(after=after)
        self.assertIn(path, "\n".join(problems))
        problems, _ = self.f.problems(events=[self.f.events[0], self.f.events[0]])
        self.assertIn("second run", "\n".join(problems))

    def test_a_first_run_that_published_nothing_fails(self):
        skipped = [{"event": "skipped", "reason": "another run holds the lock"}]
        problems, _ = self.f.problems(events=[skipped, self.f.events[1]])
        self.assertIn("another run holds the lock", "\n".join(problems))

    def rid(self, kind, pod=PODS[1]):
        return f"{PREFIX}-{pod}-{kind}-1"

    def test_a_404_or_its_application_line_kept_fails(self):
        rid = self.rid("missing")
        [doc] = [r for r in self.f.records if r.get("mdc", {}).get("requestId")
                 == self.rid("invalid") and r.get("http_status") == 400]
        for logger in (ACCESS, MAPPER):
            leaked = dict(doc, loggerName=logger, mdc={"requestId": rid, "realmId": "POLARIS"})
            problems, _ = self.f.problems(records=self.f.records + [leaked])
            self.assertIn(rid, "\n".join(problems))

    def test_404s_the_summaries_do_not_count_fail(self):
        for field in ("counted_404", "app_dropped_404"):
            records = [dict(r, **{field: 0}) if r.get("report_type") == "summary" else r
                       for r in self.f.records]
            problems, _ = self.f.problems(records=records)
            self.assertIn(field, "\n".join(problems))

    def test_a_400_without_its_kept_application_line_fails(self):
        rid = self.rid("invalid")
        records = [r for r in self.f.records if not (
            r.get("mdc", {}).get("requestId") == rid and r.get("loggerName") == MAPPER)]
        problems, _ = self.f.problems(records=records)
        self.assertIn(rid, "\n".join(problems))

    def test_a_commit_its_tables_row_does_not_count_fails(self):
        rid = self.rid("commit")
        table = f"{SPACE}/tables/{gate.logs_table(rid)}"
        records = [dict(r, commit_count=0) if r.get("resource") == table else r
                   for r in self.f.records]
        problems, _ = self.f.problems(records=records)
        self.assertIn(table, "\n".join(problems))
        records = [r for r in self.f.records if r.get("resource") != table]
        problems, _ = self.f.problems(records=records)
        self.assertIn(table, "\n".join(problems))

    def test_the_catalog_made_without_its_kept_application_line_fails(self):
        rid = f"{PREFIX}-setup-catalog"
        records = [r for r in self.f.records if not (
            r.get("mdc", {}).get("requestId") == rid and r.get("loggerName") == ADMIN)]
        problems, _ = self.f.problems(records=records)
        self.assertIn(rid, "\n".join(problems))

    def test_an_hour_without_its_summary_fails(self):
        after = {k: v for k, v in self.f.after.items() if not k.startswith("aggregated-logs/")}
        problems, _ = self.f.problems(before=after, after=after, records=[
            r for r in self.f.records if "aggregated-logs" not in r["source_file"]])
        self.assertIn("aggregated-logs/", "\n".join(problems))


class ProblemsWithFailures(unittest.TestCase):
    """The traffic Create Fixtures sends (ADR-040): each pod also gets a 500."""

    @classmethod
    def setUpClass(cls):
        cls.f = Fixture(gate.logs_request_ids(PREFIX, 2, PODS, failing=True))

    @classmethod
    def tearDownClass(cls):
        cls.f.dir.cleanup()

    def failed(self):
        return sorted(r for r, (_, kind) in self.f.sent.items() if kind == "failed")

    def test_each_500_kept_with_its_error_line_and_exception_passes(self):
        problems, notes = self.f.problems()
        self.assertEqual(problems, [])
        self.assertIn("2 500s kept with their ERROR line and its exception", notes[-1])

    def test_a_500_without_its_error_line_fails(self):
        rid = self.failed()[0]
        records = [r for r in self.f.records if not (
            r.get("mdc", {}).get("requestId") == rid and r.get("level") == "ERROR")]
        problems, _ = self.f.problems(records=records)
        self.assertIn(f"request {rid}", "\n".join(problems))
        self.assertIn("ERROR line", "\n".join(problems))

    def test_an_error_line_without_the_exception_fails(self):
        rid = self.failed()[1]
        records = [({k: v for k, v in r.items() if k != "exception"}
                    if r.get("mdc", {}).get("requestId") == rid else r)
                   for r in self.f.records]
        problems, _ = self.f.problems(records=records)
        self.assertIn(f"request {rid}", "\n".join(problems))

    def test_a_500_without_its_access_line_fails(self):
        rid = self.failed()[0]
        records = [r for r in self.f.records if not (
            r.get("mdc", {}).get("requestId") == rid and "http_status" in r)]
        problems, _ = self.f.problems(records=records)
        self.assertIn(f"request {rid}", "\n".join(problems))


if __name__ == "__main__":
    unittest.main()


class V2IsGone(unittest.TestCase):
    REMOVED = (
        "check_logs logs_problems logs_request_ids logs_table logs_traffic_command batch_now "
        "batch_job job_outcome json_lines batch_events fluent_bit_records logs_listing_command "
        "parse_listing fluent_bit_pod LOGS_REQUESTS LOGS_CATALOG LOGS_NAMESPACE "
        "LOGS_POLICY_KINDS APP_LINE_LOGGERS LOGS_MOUNT OUTPUT_DIRS READER_UID LOGS_LISTING"
    ).split()
    KEPT = "wait_for LOGS_TIMEOUT SHARED_CLASS GATE_LOGS_SIZE batch_image_commands gate_env".split()

    def test_the_v2_checks_and_helpers_are_gone(self):
        for name in self.REMOVED:
            self.assertFalse(hasattr(gate, name), name)

    def test_what_the_install_and_other_parts_still_need_is_kept(self):
        for name in self.KEPT:
            self.assertTrue(hasattr(gate, name), name)

    def test_the_traffic_script_and_the_v2_schemas_are_gone(self):
        self.assertFalse(os.path.exists(os.path.join(HERE, "logs_traffic.sh")))
        for name in ("app-dropped", "principal", "processed", "resource", "summary"):
            path = os.path.join(ROOT, "docs", "contracts", f"polaris-logs-{name}.schema.json")
            self.assertFalse(os.path.exists(path), name)
