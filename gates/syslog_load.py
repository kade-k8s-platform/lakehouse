"""The load for the Polaris syslog proofs (gates/syslog_probe.py). Run in a short-lived pod on
the pinned Python image, fed on stdin: `python3 - SPEC`, SPEC a JSON object

  {"url": "http://<pod ip>:8181", "realm": "POLARIS", "pid": N, "leg": "a",
   "phases": [{"phase": "base", "count": 1000, "workers": 8, "pad": 500, "token": 0},
              {"phase": "long", "lengths": [3000]}]}

Each phase sends `count` GETs of /api/management/v1/catalogs (the first `token` of them with
root's token from ROOT_CLIENT_ID and ROOT_CLIENT_SECRET, which exercises mdc.realmId; the rest
without one, which Polaris answers 401 and logs without touching the database), `workers` at a
time, X-Request-ID `syslog-<pid>-<leg>-<phase>-<i>`, the query padded to `pad` characters; a
phase with `lengths` sends one request per length with an ID of exactly that many characters
(for a line over max-length). It prints one JSON line per phase: counts per status, client
timeouts and errors, one millisecond figure per request. Standard library only; the request
IDs are built here so that the gate and the pod agree.
"""

import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PATH = "/api/management/v1/catalogs"
TIMEOUT = 10


def request_id(pid, leg, phase, i):
    return f"syslog-{pid}-{leg}-{phase}-{i}"


def long_id(pid, leg, length):
    """An ID of exactly `length` characters, starting `syslog-<pid>-<leg>-long-<length>-`."""
    prefix = f"syslog-{pid}-{leg}-long-{length}-"
    if length < len(prefix):
        raise ValueError(f"{length} characters cannot hold {prefix!r}")
    return prefix + "x" * (length - len(prefix))


def pad_query(length):
    """A query string of exactly `length` characters ('' for 0)."""
    return "pad=" + "x" * (length - 4) if length else ""


def summarize(phase, results):
    """The line a phase prints; `results` are (status or None, ms, kind) per request."""
    counts = {}
    for status, _, kind in results:
        if kind == "ok":
            counts[str(status)] = counts.get(str(status), 0) + 1
    return {"phase": phase, "sent": len(results), "counts": counts,
            "timeouts": sum(1 for _, _, kind in results if kind == "timeout"),
            "errors": sum(1 for _, _, kind in results if kind == "error"),
            "ms": [round(ms, 1) for _, ms, _ in results]}


def token(url, realm, client_id, client_secret):
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": client_id,
        "client_secret": client_secret, "scope": "PRINCIPAL_ROLE:ALL"}).encode()
    request = urllib.request.Request(f"{url}/api/catalog/v1/oauth/tokens", data=body,
                                     headers={"Polaris-Realm": realm})
    with urllib.request.urlopen(request, timeout=20) as answer:
        return json.load(answer)["access_token"]


def send(url, realm, rid, pad, bearer):
    headers = {"Polaris-Realm": realm, "X-Request-ID": rid}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    query = f"?{pad_query(pad)}" if pad else ""
    request = urllib.request.Request(f"{url}{PATH}{query}", headers=headers)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            status, kind = answer.status, "ok"
            answer.read()
    except urllib.error.HTTPError as error:
        status, kind = error.code, "ok"
    except TimeoutError:
        status, kind = None, "timeout"
    except OSError as error:
        status, kind = None, "timeout" if "timed out" in str(error) else "error"
    return status, (time.monotonic() - start) * 1000, kind


def run_phase(spec, phase, bearer):
    """Returns (summary line, ids sent in order)."""
    url, realm, pid, leg = spec["url"], spec["realm"], spec["pid"], spec["leg"]
    name = phase["phase"]
    if "lengths" in phase:
        ids = [long_id(pid, leg, n) for n in phase["lengths"]]
    else:
        ids = [request_id(pid, leg, name, i) for i in range(phase["count"])]
    tokens = phase.get("token", 0)
    with concurrent.futures.ThreadPoolExecutor(max_workers=phase.get("workers", 1)) as pool:
        results = list(pool.map(
            lambda item: send(url, realm, item[1], phase.get("pad", 0),
                              bearer if item[0] < tokens else None),
            enumerate(ids)))
    line = summarize(name, results)
    if "lengths" in phase:
        line["ids"] = ids
    return line


def main(argv, environ):
    spec = json.loads(argv[1])
    bearer = None
    if any(p.get("token") for p in spec["phases"]):
        bearer = token(spec["url"], spec["realm"], environ["ROOT_CLIENT_ID"],
                       environ["ROOT_CLIENT_SECRET"])
    for phase in spec["phases"]:
        print(json.dumps(run_phase(spec, phase, bearer)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, os.environ))
