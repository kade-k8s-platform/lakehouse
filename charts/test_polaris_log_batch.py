"""Tests for charts/polaris-log-batch (ADR-014, ADR-036): the CronJob, its Role, how it meets
Polaris's files on the shared claim, and the values schema.

The rendered kinds are core Kubernetes kinds, for which kubeconform has no schema here; the
dry-run gate validates them server-side (ADR-016).

Run: python3 -m unittest discover -s charts
Needs helm on PATH (`python3 gates/gate.py tools BIN_DIR helm kubeconform`) and PyYAML
(`pip install --require-hashes -r charts/requirements.txt`).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "gates"))
sys.path.insert(0, os.path.join(ROOT, "install"))
import gate  # noqa: E402
import release  # noqa: E402

NAMESPACE = "catalog-polaris"


def setUpModule():
    if not shutil.which("helm"):
        raise RuntimeError("helm not on PATH: run "
                           "`python3 gates/gate.py tools BIN_DIR helm kubeconform` first")


def helm_template(*sets, values=None):
    argv = ["helm", "template", release.BATCH, release.BATCH_CHART, "--namespace", NAMESPACE]
    path = None
    if values is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(values, f)
        path = f.name
        argv += ["-f", path]
    for s in sets:
        argv += ["--set", s]
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False)
    finally:
        if path:
            os.unlink(path)


def batch_values():
    """The chart's image values from this checkout (the file's own copy: release.py no longer
    installs the batch; L7e removes this file with the chart)."""
    repository, tag = release.batch_image().rsplit(":", 1)
    return {"image": {"repository": repository, "tag": tag}}


def render(*sets):
    result = helm_template(*sets, values=batch_values())
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def polaris_rendered():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(release.polaris_values({"POLARIS_DB_HOST": "",
                                          "POLARIS_OPA_URL": "http://opa:8181/x"}), f)
    try:
        out = subprocess.run(["helm", "template", release.POLARIS, release.POLARIS_CHART,
                              "--namespace", NAMESPACE, "-f", release.POLARIS_VALUES,
                              "-f", f.name], capture_output=True, text=True, check=True).stdout
    finally:
        os.unlink(f.name)
    return [d for d in yaml.safe_load_all(out) if d]


def one(docs, kind):
    (doc,) = [d for d in docs if d["kind"] == kind]
    return doc


class Rendered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = render()
        cls.cronjob = one(cls.docs, "CronJob")
        cls.pod = cls.cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        (cls.container,) = cls.pod["containers"]
        cls.env = {v["name"]: v["value"] for v in cls.container.get("env", [])}

    def test_the_kinds_and_names(self):
        self.assertEqual(sorted((d["kind"], d["metadata"]["name"]) for d in self.docs), [
            ("CronJob", release.BATCH), ("Role", release.BATCH),
            ("RoleBinding", release.BATCH), ("ServiceAccount", release.BATCH)])

    def test_minute_3_of_every_hour_in_utc_one_run_at_a_time(self):
        spec = self.cronjob["spec"]
        self.assertEqual(spec["schedule"], "3 * * * *")
        self.assertEqual(spec["timeZone"], "Etc/UTC")
        self.assertEqual(spec["concurrencyPolicy"], "Forbid")

    def test_the_image_is_the_one_built_from_this_repository(self):
        self.assertEqual(self.container["image"], release.batch_image())

    def test_it_reads_the_files_polaris_pods_name_and_lists_those_pods(self):
        polaris = polaris_rendered()
        labels = one(polaris, "Deployment")["spec"]["selector"]["matchLabels"]
        selector = set(self.env["POD_SELECTOR"].split(","))
        self.assertEqual(selector, {f"{k}={v}" for k, v in labels.items()})
        self.assertEqual(selector, set(gate.POLARIS_POD_LABELS.split(",")))
        # A pod's HOSTNAME is its name, and Polaris writes polaris-${HOSTNAME}.log; the
        # Deployment's pods are named after it.
        self.assertTrue(f"{release.POLARIS}-".startswith(self.env["POD_PREFIX"]))

    def test_the_role_may_only_get_and_list_pods(self):
        role = one(self.docs, "Role")
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["pods"],
                                          "verbs": ["get", "list"]}])
        binding = one(self.docs, "RoleBinding")
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount",
                                                "name": release.BATCH,
                                                "namespace": NAMESPACE}])
        self.assertEqual(self.pod["serviceAccountName"], release.BATCH)

    def test_it_runs_as_polaris_files_owner_group(self):
        # Polaris 1.6.0 writes its files as uid 10000, gid 10001 (measured, docs/contracts/
        # polaris-logs.md); the batch moves them and writes next to them.
        security = self.pod["securityContext"]
        self.assertEqual((security["runAsUser"], security["runAsGroup"], security["fsGroup"]),
                         (10000, 10001, 10001))
        self.assertIs(security["runAsNonRoot"], True)
        container = self.container["securityContext"]
        self.assertIs(container["allowPrivilegeEscalation"], False)
        self.assertIs(container["readOnlyRootFilesystem"], True)
        self.assertEqual(container["capabilities"], {"drop": ["ALL"]})

    def test_a_failed_run_is_not_retried_within_the_hour(self):
        job = self.cronjob["spec"]["jobTemplate"]["spec"]
        self.assertEqual(job["backoffLimit"], 0)
        self.assertEqual(self.pod["restartPolicy"], "Never")


class ValuesSchema(unittest.TestCase):
    def test_the_default_values_render_and_name_the_image_built_from_this_checkout(self):
        # The static gate lints every chart with its defaults, so values.yaml carries the tag;
        # it must change with the Dockerfile or the script (release.batch_image).
        result = helm_template()
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(release.BATCH_CHART, "values.yaml"), encoding="utf-8") as f:
            image = yaml.safe_load(f)["image"]
        self.assertEqual(f"{image['repository']}:{image['tag']}", release.batch_image(),
                         "images/polaris-log-batch changed: set image.tag in "
                         "charts/polaris-log-batch/values.yaml to the new hash")

    def test_an_empty_image_tag_is_refused(self):
        result = helm_template("image.tag=")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tag", result.stderr)

    def test_an_unknown_value_is_refused(self):
        result = helm_template("nosuch=1", values=batch_values())
        self.assertNotEqual(result.returncode, 0)

    def test_a_schedule_other_than_a_cron_line_is_refused(self):
        result = helm_template("schedule=hourly", values=batch_values())
        self.assertNotEqual(result.returncode, 0)


class Image(unittest.TestCase):
    def test_the_tag_follows_the_dockerfile_and_the_script(self):
        tag = release.batch_image().rsplit(":", 1)[1]
        self.assertRegex(tag, r"^[0-9a-f]{12}$")
        with tempfile.TemporaryDirectory() as d:
            for name in release.BATCH_IMAGE_FILES:
                shutil.copy(os.path.join(release.BATCH_IMAGE_DIR, name), d)
            self.assertEqual(release.batch_image(d), release.batch_image())
            with open(os.path.join(d, "polaris_log_batch.py"), "a", encoding="utf-8") as f:
                f.write("\n")
            self.assertNotEqual(release.batch_image(d), release.batch_image())

    def test_the_base_image_is_the_python_the_gate_pins_by_digest(self):
        with open(os.path.join(release.BATCH_IMAGE_DIR, "Dockerfile"), encoding="utf-8") as f:
            (base,) = [line.split()[1] for line in f if line.startswith("FROM ")]
        self.assertEqual(base, gate.load_pins()["standin_images"]["recorder"]["image"])

    def test_the_image_runs_as_the_charts_user(self):
        with open(os.path.join(release.BATCH_IMAGE_DIR, "Dockerfile"), encoding="utf-8") as f:
            (user,) = [line.split()[1] for line in f if line.startswith("USER ")]
        self.assertEqual(user, "10000:10001")

    def test_only_the_script_goes_into_the_image(self):
        with open(os.path.join(release.BATCH_IMAGE_DIR, "Dockerfile"), encoding="utf-8") as f:
            copies = [line.split()[1:] for line in f if line.startswith("COPY ")]
        self.assertEqual(copies, [["polaris_log_batch.py", "/app/polaris_log_batch.py"]])


if __name__ == "__main__":
    unittest.main()
