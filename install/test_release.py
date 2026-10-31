"""Tests for release.py. Run: python3 -m unittest discover -s install"""

import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import release  # noqa: E402

# The settle reads after a helm upgrade of polaris-pg do not wait in these tests.
release.SETTLE_INTERVAL = 0

ENV = {"KUBE_CONTEXT": "kind-lakehouse-gate", "NS_CNPG_SYSTEM": "cnpg-system"}


def deployments(*images):
    return json.dumps({"items": [
        {"spec": {"template": {"spec": {"containers": [{"name": "manager", "image": i}]}}}}
        for i in images]})


def plugin_services(*selectors):
    return json.dumps({"items": [{"metadata": {"name": "barman-cloud"},
                                  "spec": {"selector": sel}} for sel in selectors]})


def pods(*ready):
    return json.dumps({"items": [
        {"metadata": {"name": f"barman-cloud-{i}"},
         "status": {"conditions": [{"type": "Ready", "status": "True" if r else "False"}]}}
        for i, r in enumerate(ready)]})


class FakeKubectl:
    """Answers kubectl calls by their arguments; records every call."""

    def __init__(self, crd=True, deploys=deployments("ghcr.io/cloudnative-pg/cloudnative-pg:1.30.1"),
                 plugin_crd=True, services=plugin_services({"app": "barman-cloud"}),
                 plugin_pods=pods(True)):
        self.crd, self.deploys, self.calls = crd, deploys, []
        self.plugin_crd, self.services, self.plugin_pods = plugin_crd, services, plugin_pods

    def __call__(self, argv):
        self.calls.append(argv)
        if "crd" in argv and release.CNPG_CRD in argv:
            return (0, "customresourcedefinition.apiextensions.k8s.io/" + release.CNPG_CRD) \
                if self.crd else (1, "NotFound")
        if "crd" in argv and release.PLUGIN_CRD in argv:
            return (0, "customresourcedefinition.apiextensions.k8s.io/" + release.PLUGIN_CRD) \
                if self.plugin_crd else (1, "NotFound")
        if "deployments" in argv:
            return 0, self.deploys
        if "services" in argv:
            return 0, self.services
        if "pods" in argv:
            return 0, self.plugin_pods
        raise AssertionError(f"unexpected call {argv}")


class OperatorVersions(unittest.TestCase):
    def test_tag_is_read(self):
        self.assertEqual(release.operator_versions(
            deployments("ghcr.io/cloudnative-pg/cloudnative-pg:1.30.1")), ["1.30.1"])

    def test_tag_with_a_digest(self):
        self.assertEqual(release.operator_versions(deployments(
            "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.1@sha256:" + "a" * 64)), ["1.30.1"])

    def test_a_mirror_registry_counts(self):
        self.assertEqual(release.operator_versions(
            deployments("registry.example.invalid/mirror/cloudnative-pg:1.29.3")), ["1.29.3"])

    def test_digest_only_or_other_images_give_nothing(self):
        self.assertEqual(release.operator_versions(deployments(
            "ghcr.io/cloudnative-pg/cloudnative-pg@sha256:" + "a" * 64,
            "ghcr.io/cloudnative-pg/postgresql:16")), [])


class Preconditions(unittest.TestCase):
    def check(self, kubectl, env=ENV, supported=("1.30",)):
        return release.check_preconditions(env, kubectl, list(supported))

    def test_present_and_supported_passes(self):
        problems, notes = self.check(FakeKubectl())
        self.assertEqual(problems, [])
        self.assertIn("1.30.1", "\n".join(notes))

    def test_every_call_names_the_context(self):
        kubectl = FakeKubectl()
        self.check(kubectl)
        for argv in kubectl.calls:
            self.assertEqual(argv[:3], ["kubectl", "--context", "kind-lakehouse-gate"])

    def test_the_deployments_are_read_in_the_operator_namespace(self):
        kubectl = FakeKubectl()
        self.check(kubectl)
        call = next(a for a in kubectl.calls if "deployments" in a)
        self.assertIn("-n", call)
        self.assertEqual(call[call.index("-n") + 1], "cnpg-system")

    def test_an_empty_context_stops_before_any_call(self):
        kubectl = FakeKubectl()
        problems, _ = self.check(kubectl, env={**ENV, "KUBE_CONTEXT": ""})
        self.assertTrue(problems)
        self.assertEqual(kubectl.calls, [])

    def test_no_crd_stops(self):
        problems, _ = self.check(FakeKubectl(crd=False))
        self.assertIn(release.CNPG_CRD, "\n".join(problems))

    def test_no_operator_stops(self):
        problems, _ = self.check(FakeKubectl(deploys=deployments()))
        self.assertTrue(problems)

    def test_an_unsupported_version_stops(self):
        problems, _ = self.check(FakeKubectl(
            deploys=deployments("ghcr.io/cloudnative-pg/cloudnative-pg:1.31.0")))
        self.assertIn("1.31.0", "\n".join(problems))

    def test_a_minor_prefix_does_not_match_a_longer_minor(self):
        problems, _ = self.check(FakeKubectl(
            deploys=deployments("ghcr.io/cloudnative-pg/cloudnative-pg:1.300.0")))
        self.assertTrue(problems)

    # ADR-043 §3: the plugin runs in NS_CNPG_SYSTEM and the ObjectStore CRD is present.
    def test_the_plugin_running_is_named_in_the_notes(self):
        problems, notes = self.check(FakeKubectl())
        self.assertEqual(problems, [])
        self.assertIn("Barman Cloud Plugin", "\n".join(notes))

    def test_no_object_store_crd_stops(self):
        problems, _ = self.check(FakeKubectl(plugin_crd=False))
        self.assertEqual(len(problems), 1)
        self.assertIn(release.PLUGIN_CRD, problems[0])
        self.assertIn("ADR-043", problems[0])

    def test_the_plugin_is_found_as_cnpg_finds_it_in_the_operator_namespace(self):
        kubectl = FakeKubectl()
        self.check(kubectl)
        call = next(a for a in kubectl.calls if "services" in a)
        self.assertEqual(call[call.index("-n") + 1], "cnpg-system")
        self.assertEqual(call[call.index("-l") + 1],
                         "cnpg.io/pluginName=barman-cloud.cloudnative-pg.io")

    def test_its_pods_are_read_by_the_services_selector(self):
        kubectl = FakeKubectl(services=plugin_services({"app": "barman-cloud", "tier": "x"}))
        self.check(kubectl)
        call = next(a for a in kubectl.calls if "pods" in a)
        self.assertEqual(call[call.index("-n") + 1], "cnpg-system")
        self.assertEqual(call[call.index("-l") + 1], "app=barman-cloud,tier=x")

    def test_no_plugin_service_stops(self):
        kubectl = FakeKubectl(services=plugin_services())
        problems, _ = self.check(kubectl)
        self.assertEqual(len(problems), 1)
        self.assertIn("no Barman Cloud Plugin", problems[0])
        self.assertIn("cnpg-system", problems[0])
        self.assertFalse([a for a in kubectl.calls if "pods" in a])

    def test_no_ready_plugin_pod_stops(self):
        for answer in (pods(), pods(False, False)):
            with self.subTest(pods=answer):
                problems, _ = self.check(FakeKubectl(plugin_pods=answer))
                self.assertEqual(len(problems), 1)
                self.assertIn("not running", problems[0])

    def test_an_unsupported_operator_and_a_missing_plugin_are_both_named(self):
        problems, _ = self.check(FakeKubectl(
            deploys=deployments("ghcr.io/cloudnative-pg/cloudnative-pg:1.31.0"),
            plugin_crd=False))
        self.assertEqual(len(problems), 2)


POLARIS_ENV = {**ENV, "NS_CATALOG_POLARIS": "catalog-polaris",
               "NS_CATALOG_DATAHUB": "catalog-datahub",
               "S3_ENDPOINT": "http://s3.example.invalid:8333",
               "POLARIS_BACKUP_BUCKET": "polaris-backup",
               "DATAHUB_BACKUP_BUCKET": "datahub-backup"}


class FakeCalls:
    """Answers step 1's kubectl and helm calls; records every call."""

    def __init__(self, secret=True, helm=(0, ""), wait=(0, ""), clusters=None, pods=(0, ""),
                 label=(0, ""), generations=((1, "NotFound"),), rollout=(0, ""),
                 backup_keys=("accessKeyId", "region", "secretAccessKey"),
                 pg=release.POLARIS_PG, db_secret=release.POLARIS_DB_SECRET,
                 backup_secret=release.POLARIS_BACKUP_SECRET, secret_keys=None):
        self.secret, self.helm, self.wait, self.calls = secret, helm, wait, []
        # `kubectl rollout status`, as Datahub's Kafka step waits for its StatefulSet.
        self.rollout = rollout
        # The Cluster and Secrets of the service whose step this answers: Polaris's by default.
        self.pg, self.db_secret, self.backup_secret = pg, db_secret, backup_secret
        # The key names the Secrets read gives for polaris-backup-s3 (ADR-060), and for any
        # other Secret named in `secret_keys`; every other Secret has the keys init-env writes.
        self.backup_keys = backup_keys
        self.secret_keys = dict(secret_keys or {})
        self.pods, self.label = pods, label
        # The Cluster's generation as each read gives it, before the helm upgrade and after
        # Ready; the last answer repeats. By default there is no Cluster.
        self.generations = list(generations)
        self.values = []
        # The Cluster as each `get` reads it; the last answer repeats.
        self.clusters = list(clusters or [cluster_status()])

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] == "helm":
            files = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
            for path in files:
                if path.endswith(".json"):
                    with open(path, encoding="utf-8") as f:
                        self.values.append(json.load(f))
            return self.helm
        if "secret" in argv:
            if not self.secret:
                return (1, "NotFound")
            names = argv[argv.index("secret") + 1:argv.index("-o")]
            return (0, "".join(f"{name}: {' '.join(self.keys_of(name))}\n" for name in names))
        if "wait" in argv:
            return self.wait
        if "rollout" in argv:
            return self.rollout
        if argv[5:7] == ["get", "pods"]:
            return self.pods
        if "label" in argv:
            return self.label
        if f"cluster/{self.pg}" in argv and "jsonpath={.metadata.generation}" in argv:
            return self.generations.pop(0) if len(self.generations) > 1 else self.generations[0]
        if f"cluster/{self.pg}" in argv and "get" in argv:
            answer = self.clusters.pop(0) if len(self.clusters) > 1 else self.clusters[0]
            return (0, json.dumps(answer)) if isinstance(answer, dict) else answer
        raise AssertionError(f"unexpected call {argv}")

    def keys_of(self, name):
        if name == self.backup_secret:
            return sorted(self.backup_keys)
        if name in self.secret_keys:
            return sorted(self.secret_keys[name])
        (secret,) = [s for s in release.SECRETS if s.name == name]
        return sorted({**secret.from_env, **secret.fixed})


def cluster_status(phase="Cluster in healthy state", ready=3, instances=3):
    return {"spec": {"instances": instances},
            "status": {"phase": phase, "readyInstances": ready}}


class PolarisPostgres(unittest.TestCase):
    def step(self, calls, env=POLARIS_ENV, dry_run=False):
        return release.polaris_postgres(env, calls, dry_run=dry_run)

    def test_installs_the_chart_then_waits_for_the_cluster(self):
        calls = FakeCalls()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual([c[0] for c in calls.calls], ["kubectl", "kubectl", "helm", "kubectl"]
                         + ["kubectl"] * release.SETTLE_READS)
        helm = calls.calls[2]
        self.assertEqual(helm[:3], ["helm", "--kube-context", "kind-lakehouse-gate"])
        self.assertEqual(helm[helm.index("-n") + 1], "catalog-polaris")
        self.assertEqual(helm[helm.index("upgrade"):helm.index("upgrade") + 4],
                         ["upgrade", "--install", "polaris-postgres", release.POLARIS_PG_CHART])
        self.assertNotIn("--dry-run=server", helm)
        wait = calls.calls[3]
        self.assertIn("--for=condition=Ready", wait)
        self.assertIn("cluster/polaris-pg", wait)
        self.assertIn("polaris-pg", "\n".join(notes))

    def test_every_call_names_the_context_and_the_namespace(self):
        calls = FakeCalls()
        self.step(calls)
        for argv in calls.calls:
            flag = "--kube-context" if argv[0] == "helm" else "--context"
            self.assertEqual(argv[1:3], [flag, "kind-lakehouse-gate"])
            self.assertEqual(argv[argv.index("-n") + 1], "catalog-polaris")

    def test_the_chart_is_this_repositorys(self):
        self.assertTrue(os.path.isfile(os.path.join(release.POLARIS_PG_CHART, "Chart.yaml")))

    def test_dry_run_is_server_side_and_does_not_wait(self):
        calls = FakeCalls()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        self.assertIn("--dry-run=server", calls.calls[1])
        self.assertFalse(any("wait" in c for c in calls.calls))
        self.assertIn("dry-run", "\n".join(notes))

    def test_an_empty_namespace_stops_before_any_call(self):
        calls = FakeCalls()
        problems, _ = self.step(calls, env={**POLARIS_ENV, "NS_CATALOG_POLARIS": ""})
        self.assertIn("NS_CATALOG_POLARIS", "\n".join(problems))
        self.assertEqual(calls.calls, [])

    def test_a_missing_secret_stops_before_helm_and_names_init_env(self):
        calls = FakeCalls(secret=False)
        problems, _ = self.step(calls)
        self.assertIn("init_env.py --apply-secrets", "\n".join(problems))
        self.assertFalse(any(c[0] == "helm" for c in calls.calls))

    def test_a_backup_secret_without_a_region_stops_before_helm_and_names_init_env(self):
        """ADR-060: an upgrade that skipped --apply-secrets would archive no WAL."""
        calls = FakeCalls(backup_keys=("accessKeyId", "secretAccessKey"))
        problems, _ = self.step(calls)
        self.assertEqual(problems, [
            f"Secret {release.POLARIS_BACKUP_SECRET} in catalog-polaris has no key region: run "
            "scripts/init_env.py --apply-secrets with this release first (ADR-060)"])
        self.assertFalse(any(c[0] == "helm" for c in calls.calls))

    def test_a_backup_secret_without_a_region_stops_a_dry_run_too(self):
        calls = FakeCalls(backup_keys=("accessKeyId", "secretAccessKey"))
        problems, _ = self.step(calls, dry_run=True)
        self.assertIn("no key region", "\n".join(problems))
        self.assertFalse(any(c[0] == "helm" for c in calls.calls))

    def test_the_secrets_are_read_for_their_key_names_never_their_values(self):
        calls = FakeCalls()
        self.step(calls)
        read = next(c for c in calls.calls if "secret" in c)
        self.assertEqual(read[read.index("get"):read.index("get") + 4],
                         ["get", "secret", release.POLARIS_DB_SECRET, release.POLARIS_BACKUP_SECRET])
        self.assertEqual(read[-2:], ["-o", release.SECRET_KEYS_TEMPLATE])
        self.assertNotIn("$value}}", release.SECRET_KEYS_TEMPLATE)

    def test_a_failed_helm_stops_with_its_error(self):
        problems, _ = self.step(FakeCalls(helm=(1, "admission webhook denied")))
        self.assertIn("admission webhook denied", "\n".join(problems))

    # CNPG sets no observedGeneration on Ready: after an upgrade, `kubectl wait` passed before
    # the operator began restarting the instances (smoke 38215183351, adding the plugin).
    def test_after_ready_the_cluster_must_read_healthy_several_times_in_a_row(self):
        calls = FakeCalls(clusters=[
            cluster_status(), cluster_status("Upgrading cluster", ready=2),
            cluster_status("Waiting for the instances to become active", ready=2),
            cluster_status(), cluster_status(), cluster_status()])
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        reads = self.reads(calls)
        self.assertEqual(len(reads), 6)
        for argv in reads:
            self.assertEqual(argv[:5], ["kubectl", "--context", "kind-lakehouse-gate", "-n",
                                        "catalog-polaris"])
            self.assertEqual(argv[-2:], ["-o", "json"])
        self.assertIn("healthy", "\n".join(notes))

    def test_all_instances_must_be_ready_for_a_healthy_read(self):
        calls = FakeCalls(clusters=[cluster_status(ready=2)] * 2 + [cluster_status()])
        problems, _ = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual(len(self.reads(calls)), 2 + release.SETTLE_READS)

    # #125: settle costs at least 20 s. An upgrade that leaves the Cluster's spec as it was
    # (its generation unchanged) gives CNPG nothing to roll out, so one healthy read is enough.
    def reads(self, calls):
        return [c for c in calls.calls if f"cluster/{release.POLARIS_PG}" in c
                and c[-2:] == ["-o", "json"]]

    def test_the_generation_is_read_before_the_upgrade_and_after_ready(self):
        calls = FakeCalls(generations=[(0, "4")])
        self.step(calls)
        reads = [i for i, c in enumerate(calls.calls) if "jsonpath={.metadata.generation}" in c]
        helm = [i for i, c in enumerate(calls.calls) if c[0] == "helm"][0]
        wait = [i for i, c in enumerate(calls.calls) if "wait" in c][0]
        self.assertEqual(len(reads), 2)
        self.assertLess(reads[0], helm)
        self.assertGreater(reads[1], wait)
        for i in reads:
            self.assertEqual(calls.calls[i][:7], ["kubectl", "--context", "kind-lakehouse-gate",
                                                  "-n", "catalog-polaris", "get",
                                                  "cluster/polaris-pg"])

    def test_an_unchanged_spec_needs_one_healthy_read(self):
        calls = FakeCalls(generations=[(0, "4")])
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual(len(self.reads(calls)), 1)
        self.assertIn("unchanged", "\n".join(notes))

    def test_an_unchanged_spec_still_waits_for_a_healthy_read(self):
        calls = FakeCalls(generations=[(0, "4")], clusters=[
            cluster_status("Waiting for the instances to become active", ready=2),
            cluster_status()])
        problems, _ = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual(len(self.reads(calls)), 2)

    def test_a_changed_spec_needs_every_read(self):
        calls = FakeCalls(generations=[(0, "4"), (0, "5")])
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual(len(self.reads(calls)), release.SETTLE_READS)
        self.assertNotIn("unchanged", "\n".join(notes))

    def test_a_new_cluster_needs_every_read(self):
        calls = FakeCalls(generations=[(1, "NotFound"), (0, "1")])
        problems, _ = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual(len(self.reads(calls)), release.SETTLE_READS)

    def test_a_cluster_that_never_settles_stops_with_its_phase(self):
        with mock.patch.object(release, "SETTLE_TIMEOUT", 0):
            problems, _ = self.step(FakeCalls(clusters=[cluster_status("Upgrading cluster", 2)]))
        self.assertIn("polaris-pg", "\n".join(problems))
        self.assertIn("Upgrading cluster", "\n".join(problems))

    def test_a_dry_run_reads_no_cluster(self):
        calls = FakeCalls()
        self.step(calls, dry_run=True)
        self.assertFalse([c for c in calls.calls if f"cluster/{release.POLARIS_PG}" in c])

    def test_a_cluster_that_never_gets_ready_stops(self):
        problems, _ = self.step(FakeCalls(wait=(1, "timed out")))
        self.assertIn("polaris-pg", "\n".join(problems))
        self.assertIn("timed out", "\n".join(problems))

    # ADR-043 §2: backups to the S3 endpoint from .env, into their own bucket, with their key.
    def test_the_backups_endpoint_and_bucket_come_from_env_in_a_values_file(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                calls = FakeCalls()
                self.step(calls, dry_run=dry_run)
                self.assertEqual(calls.values, [{"backup": {
                    "endpointURL": "http://s3.example.invalid:8333",
                    "bucket": "polaris-backup"}}])
                self.assertFalse(any(os.path.exists(a) for c in calls.calls for a in c
                                     if a.endswith(".json")))

    def test_the_backup_key_secret_is_required_with_the_database_one(self):
        calls = FakeCalls()
        self.step(calls)
        secret = calls.calls[0]
        self.assertEqual(secret[secret.index("secret") + 1:secret.index("-o")],
                         ["polaris-db", "polaris-backup-s3"])
        problems, _ = self.step(FakeCalls(secret=False))
        self.assertIn("polaris-backup-s3", "\n".join(problems))

    def test_an_empty_bucket_or_endpoint_stops_before_any_call(self):
        for var in ("POLARIS_BACKUP_BUCKET", "S3_ENDPOINT"):
            with self.subTest(var=var):
                calls = FakeCalls()
                problems, _ = self.step(calls, env={**POLARIS_ENV, var: ""})
                self.assertIn(var, "\n".join(problems))
                self.assertIn("ADR-043", "\n".join(problems))
                self.assertEqual(calls.calls, [])


NETPOL_ENV = {**POLARIS_ENV,
              "POLARIS_GATEWAY_NAMESPACE_LABEL": "example.org/caller=gateway",
              "POLARIS_QUERY_NAMESPACE_LABEL": "example.org/caller=query",
              "METRICS_NAMESPACE_LABEL": "example.org/caller=metrics",
              "INGRESS_NAMESPACE_LABEL": "example.org/caller=ingress"}


class NetworkPolicies(unittest.TestCase):
    """ADR-048, first in step 1 (ADR-045)."""

    def step(self, calls, env=NETPOL_ENV, dry_run=False):
        return release.network_policies(env, calls, dry_run=dry_run)

    def helm(self, calls):
        (helm,) = [c for c in calls.calls if c[0] == "helm"]
        return helm

    def test_installs_this_repositorys_chart_in_polaris_namespace(self):
        calls = FakeCalls()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        helm = self.helm(calls)
        self.assertEqual(helm[:3], ["helm", "--kube-context", "kind-lakehouse-gate"])
        self.assertEqual(helm[helm.index("-n") + 1], "catalog-polaris")
        self.assertEqual(helm[helm.index("upgrade"):helm.index("upgrade") + 4],
                         ["upgrade", "--install", release.POLARIS_NETPOL_RELEASE,
                          release.POLARIS_NETPOL_CHART])
        self.assertTrue(os.path.isfile(os.path.join(release.POLARIS_NETPOL_CHART, "Chart.yaml")))
        self.assertNotIn("--dry-run=server", helm)
        self.assertIn("network policies", "\n".join(notes))

    def test_every_call_names_the_context_and_the_namespace(self):
        calls = FakeCalls(pods=(0, "pod/polaris-a\npod/polaris-b\n"))
        self.step(calls)
        self.assertEqual(len(calls.calls), 3)
        for argv in calls.calls:
            flag = "--kube-context" if argv[0] == "helm" else "--context"
            self.assertEqual(argv[1:3], [flag, "kind-lakehouse-gate"])
            self.assertEqual(argv[argv.index("-n") + 1], "catalog-polaris")

    def test_the_values_come_from_env(self):
        calls = FakeCalls()
        self.step(calls)
        self.assertEqual(calls.values, [{
            "callers": {"gateway": {"key": "example.org/caller", "value": "gateway"},
                        "query": {"key": "example.org/caller", "value": "query"},
                        "metrics": {"key": "example.org/caller", "value": "metrics"},
                        "ingress": {"key": "example.org/caller", "value": "ingress"}},
            "namespaces": {"datahub": "catalog-datahub", "operator": "cnpg-system"},
            "console": {"enabled": False}}])

    def test_the_console_enabled_opens_its_rules(self):
        calls = FakeCalls()
        self.step(calls, env={**NETPOL_ENV, **CONSOLE_ENV})
        self.assertEqual(calls.values[0]["console"], {"enabled": True})

    def test_an_empty_caller_class_is_left_out_and_named(self):
        calls = FakeCalls()
        env = {**NETPOL_ENV, "POLARIS_QUERY_NAMESPACE_LABEL": "", "METRICS_NAMESPACE_LABEL": ""}
        problems, notes = self.step(calls, env=env)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(calls.values[0]["callers"]), ["gateway", "ingress"])
        text = "\n".join(notes)
        self.assertIn("POLARIS_QUERY_NAMESPACE_LABEL", text)
        self.assertIn("METRICS_NAMESPACE_LABEL", text)

    def test_a_label_without_a_value_stops_before_any_call(self):
        calls = FakeCalls()
        problems, _ = self.step(calls, env={**NETPOL_ENV, "METRICS_NAMESPACE_LABEL": "team"})
        self.assertIn("METRICS_NAMESPACE_LABEL", "\n".join(problems))
        self.assertEqual(calls.calls, [])

    def test_an_empty_namespace_stops_before_any_call(self):
        for var in ("NS_CATALOG_POLARIS", "NS_CATALOG_DATAHUB", "NS_CNPG_SYSTEM"):
            with self.subTest(var=var):
                calls = FakeCalls()
                problems, _ = self.step(calls, env={**NETPOL_ENV, var: ""})
                self.assertIn(var, "\n".join(problems))
                self.assertEqual(calls.calls, [])

    def test_running_polaris_pods_get_the_client_label_before_the_policies(self):
        # The first upgrade to a release with policies: Polaris's pods from the release before
        # keep reaching polaris-pg until their rollout (Kade, 2026-10-13, option a).
        calls = FakeCalls(pods=(0, "pod/polaris-a\npod/polaris-b\n"))
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        get, label, helm = calls.calls
        self.assertEqual(get[5:], ["get", "pods", "-l", release.POLARIS_POD_SELECTOR,
                                   "-o", "name"])
        self.assertEqual(release.POLARIS_POD_SELECTOR,
                         "app.kubernetes.io/name=polaris,app.kubernetes.io/instance=polaris")
        self.assertEqual(label[5:], ["label", "--overwrite", "pod/polaris-a", "pod/polaris-b",
                                     f"{release.PG_CLIENT_LABEL}=true"])
        self.assertEqual(helm[0], "helm")
        self.assertIn("2 running Polaris pods", "\n".join(notes))

    def test_no_running_polaris_pod_labels_nothing(self):
        calls = FakeCalls()
        self.step(calls)
        self.assertFalse(any("label" in c for c in calls.calls))

    def test_a_failed_label_or_listing_stops_before_helm(self):
        for fake in (FakeCalls(pods=(1, "forbidden")),
                     FakeCalls(pods=(0, "pod/polaris-a"), label=(1, "forbidden"))):
            with self.subTest(fake=fake.pods):
                problems, _ = self.step(fake)
                self.assertIn("forbidden", "\n".join(problems))
                self.assertFalse(any(c[0] == "helm" for c in fake.calls))

    def test_dry_run_is_server_side_and_labels_nothing(self):
        calls = FakeCalls(pods=(0, "pod/polaris-a"))
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        self.assertEqual([c[0] for c in calls.calls], ["helm"])
        self.assertIn("--dry-run=server", calls.calls[0])
        self.assertIn("dry-run", "\n".join(notes))

    def test_a_failed_helm_stops_with_its_error(self):
        problems, _ = self.step(FakeCalls(helm=(1, "values don't meet the schema")))
        self.assertIn("values don't meet the schema", "\n".join(problems))


def datahub_calls(**kwargs):
    """FakeCalls answering Datahub's part of step 1 (ADR-066 §4)."""
    return FakeCalls(pg=release.DATAHUB_PG, db_secret=release.DATAHUB_DB_SECRET,
                     backup_secret=release.DATAHUB_BACKUP_SECRET, **kwargs)


class DatahubNetworkPolicies(unittest.TestCase):
    """ADR-066 §4, ADR-048 §4: Datahub's policies first in its part of step 1 (ADR-045)."""

    def step(self, calls, env=POLARIS_ENV, dry_run=False):
        return release.datahub_network_policies(env, calls, dry_run=dry_run)

    def test_installs_this_repositorys_chart_in_datahubs_namespace_with_the_operators(self):
        calls = datahub_calls()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        (helm,) = calls.calls
        self.assertEqual(helm[:3], ["helm", "--kube-context", "kind-lakehouse-gate"])
        self.assertEqual(helm[helm.index("-n") + 1], "catalog-datahub")
        self.assertEqual(helm[helm.index("upgrade"):helm.index("upgrade") + 4],
                         ["upgrade", "--install", release.DATAHUB_NETPOL_RELEASE,
                          release.DATAHUB_NETPOL_CHART])
        self.assertTrue(os.path.isfile(os.path.join(release.DATAHUB_NETPOL_CHART, "Chart.yaml")))
        self.assertNotIn("--dry-run=server", helm)
        self.assertEqual(calls.values, [{"callers": {},
                                         "namespaces": {"operator": "cnpg-system"}}])
        self.assertEqual(notes, [
            "Step 1: Datahub's network policies in catalog-datahub, installed; no namespace "
            "label in INGRESS_NAMESPACE_LABEL, DATAHUB_EMITTER_NAMESPACE_LABEL, so those callers "
            "are refused (ADR-048, ADR-066, ADR-071)."])

    def test_the_ingress_and_emitters_labels_become_caller_classes(self):
        calls = datahub_calls()
        env = {**POLARIS_ENV, "INGRESS_NAMESPACE_LABEL": "team/role=ingress",
               "DATAHUB_EMITTER_NAMESPACE_LABEL": "team/role=lineage"}
        problems, notes = self.step(calls, env=env)
        self.assertEqual(problems, [])
        self.assertEqual(calls.values, [{
            "callers": {"ingress": {"key": "team/role", "value": "ingress"},
                        "emitter": {"key": "team/role", "value": "lineage"}},
            "namespaces": {"operator": "cnpg-system"}}])
        self.assertEqual(notes, ["Step 1: Datahub's network policies in catalog-datahub, "
                                 "installed (ADR-048, ADR-066, ADR-071)."])

    def test_an_emitters_label_that_is_not_key_value_stops_before_any_call(self):
        calls = datahub_calls()
        problems, _ = self.step(calls, env={**POLARIS_ENV,
                                            "DATAHUB_EMITTER_NAMESPACE_LABEL": "lineage"})
        self.assertEqual(problems, ["DATAHUB_EMITTER_NAMESPACE_LABEL must be one namespace "
                                    "label, key=value (ADR-048)"])
        self.assertEqual(calls.calls, [])

    def test_an_empty_namespace_stops_before_any_call(self):
        for var in ("NS_CATALOG_DATAHUB", "NS_CNPG_SYSTEM"):
            with self.subTest(var=var):
                calls = datahub_calls()
                problems, _ = self.step(calls, env={**POLARIS_ENV, var: ""})
                self.assertIn(var, "\n".join(problems))
                self.assertEqual(calls.calls, [])

    def test_dry_run_is_server_side(self):
        calls = datahub_calls()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        self.assertIn("--dry-run=server", calls.calls[0])
        self.assertIn("dry-run", "\n".join(notes))

    def test_a_failed_helm_stops_with_its_error(self):
        problems, _ = self.step(datahub_calls(helm=(1, "values don't meet the schema")))
        self.assertIn("values don't meet the schema", "\n".join(problems))


class DatahubPostgres(unittest.TestCase):
    """ADR-066 §3, §4: datahub-pg as polaris-pg, from Datahub's chart, Secrets and bucket."""

    def step(self, calls, env=POLARIS_ENV, dry_run=False):
        return release.datahub_postgres(env, calls, dry_run=dry_run)

    def test_installs_the_chart_in_datahubs_namespace_then_waits_for_datahub_pg(self):
        calls = datahub_calls()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        helm = next(c for c in calls.calls if c[0] == "helm")
        self.assertEqual(helm[helm.index("-n") + 1], "catalog-datahub")
        self.assertEqual(helm[helm.index("upgrade"):helm.index("upgrade") + 4],
                         ["upgrade", "--install", "datahub-postgres", release.DATAHUB_PG_CHART])
        wait = next(c for c in calls.calls if "wait" in c)
        self.assertIn("cluster/datahub-pg", wait)
        self.assertEqual(wait[wait.index("-n") + 1], "catalog-datahub")
        self.assertIn("Cluster datahub-pg in catalog-datahub is Ready", "\n".join(notes))

    def test_the_backups_go_to_datahubs_bucket(self):
        calls = datahub_calls()
        self.step(calls)
        self.assertEqual(calls.values, [{"backup": {
            "endpointURL": "http://s3.example.invalid:8333", "bucket": "datahub-backup"}}])

    def test_datahubs_secrets_are_read_for_their_key_names(self):
        calls = datahub_calls()
        self.step(calls)
        read = next(c for c in calls.calls if "secret" in c)
        self.assertEqual(read[read.index("get"):read.index("get") + 4],
                         ["get", "secret", "datahub-db", "datahub-backup-s3"])
        self.assertEqual(read[read.index("-n") + 1], "catalog-datahub")

    def test_a_backup_secret_without_a_region_stops_before_helm(self):
        calls = datahub_calls(backup_keys=("accessKeyId", "secretAccessKey"))
        problems, _ = self.step(calls)
        self.assertEqual(problems, [
            "Secret datahub-backup-s3 in catalog-datahub has no key region: run "
            "scripts/init_env.py --apply-secrets with this release first (ADR-066)"])
        self.assertFalse(any(c[0] == "helm" for c in calls.calls))

    def test_an_empty_bucket_namespace_or_endpoint_stops_before_any_call(self):
        cases = {"DATAHUB_BACKUP_BUCKET": "DATAHUB_BACKUP_BUCKET is empty: datahub-pg's backups "
                                          "need it in .env (ADR-066)",
                 "S3_ENDPOINT": "S3_ENDPOINT is empty: datahub-pg's backups need it in .env "
                                "(ADR-066)",
                 "NS_CATALOG_DATAHUB": "NS_CATALOG_DATAHUB is empty: set Datahub's namespace in "
                                       ".env (ADR-007)"}
        for var, problem in cases.items():
            with self.subTest(var=var):
                calls = datahub_calls()
                problems, _ = self.step(calls, env={**POLARIS_ENV, var: ""})
                self.assertEqual(problems, [problem])
                self.assertEqual(calls.calls, [])

    def test_dry_run_is_server_side_and_reads_no_cluster(self):
        calls = datahub_calls()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        self.assertIn("--dry-run=server", next(c for c in calls.calls if c[0] == "helm"))
        self.assertFalse([c for c in calls.calls if "cluster/datahub-pg" in c])
        self.assertIn("Cluster datahub-pg in catalog-datahub, server-side dry-run passed",
                      "\n".join(notes))


class DatahubKafka(unittest.TestCase):
    """ADR-068 §4: Datahub's Kafka after datahub-pg in its part of step 1 (ADR-045)."""

    def step(self, calls, env=POLARIS_ENV, dry_run=False):
        return release.datahub_kafka(env, calls, dry_run=dry_run)

    def test_installs_the_chart_in_datahubs_namespace_then_waits_for_its_rollout(self):
        calls = datahub_calls()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        helm, rollout = calls.calls
        self.assertEqual(helm[:3], ["helm", "--kube-context", "kind-lakehouse-gate"])
        self.assertEqual(helm[helm.index("-n") + 1], "catalog-datahub")
        self.assertEqual(helm[helm.index("upgrade"):], [
            "upgrade", "--install", release.DATAHUB_KAFKA_RELEASE, release.DATAHUB_KAFKA_CHART])
        self.assertTrue(os.path.isfile(os.path.join(release.DATAHUB_KAFKA_CHART, "Chart.yaml")))
        # Nothing from .env but the namespace: the chart's values are its own (ADR-068 §4).
        self.assertEqual(calls.values, [])
        self.assertEqual(rollout, [
            "kubectl", "--context", "kind-lakehouse-gate", "-n", "catalog-datahub", "rollout",
            "status", f"statefulset/{release.DATAHUB_KAFKA}",
            f"--timeout={release.READY_TIMEOUT}"])
        self.assertEqual(notes, ["Step 1: StatefulSet datahub-kafka in catalog-datahub is "
                                 "rolled out and ready (ADR-068)."])

    def test_an_empty_namespace_stops_before_any_call(self):
        calls = datahub_calls()
        problems, _ = self.step(calls, env={**POLARIS_ENV, "NS_CATALOG_DATAHUB": ""})
        self.assertIn("NS_CATALOG_DATAHUB", "\n".join(problems))
        self.assertEqual(calls.calls, [])

    def test_dry_run_is_server_side_and_waits_for_nothing(self):
        calls = datahub_calls()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        (helm,) = calls.calls
        self.assertEqual(helm[-1], "--dry-run=server")
        self.assertIn("dry-run", "\n".join(notes))

    def test_a_failed_helm_stops_with_its_error_before_waiting(self):
        calls = datahub_calls(helm=(1, "values don't meet the schema"))
        problems, _ = self.step(calls)
        self.assertIn("values don't meet the schema", "\n".join(problems))
        self.assertEqual([c[0] for c in calls.calls], ["helm"])

    def test_a_broker_that_never_gets_ready_stops_the_install(self):
        calls = datahub_calls(rollout=(1, "error: timed out waiting for the condition"))
        problems, _ = self.step(calls)
        self.assertEqual(problems, [
            "StatefulSet datahub-kafka in catalog-datahub is not ready: "
            "error: timed out waiting for the condition"])


class DatahubOpensearch(unittest.TestCase):
    """ADR-070 §4: Datahub's OpenSearch after datahub-kafka in its part of step 1 (ADR-045)."""

    def step(self, calls, env=POLARIS_ENV, dry_run=False):
        return release.datahub_opensearch(env, calls, dry_run=dry_run)

    def test_installs_upstreams_chart_with_our_values_then_waits_for_its_rollout(self):
        calls = datahub_calls()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        helm, rollout = calls.calls
        self.assertEqual(helm[:3], ["helm", "--kube-context", "kind-lakehouse-gate"])
        self.assertEqual(helm[helm.index("-n") + 1], "catalog-datahub")
        self.assertEqual(helm[helm.index("upgrade"):], [
            "upgrade", "--install", release.DATAHUB_OPENSEARCH_RELEASE,
            release.DATAHUB_OPENSEARCH_CHART, "-f", release.DATAHUB_OPENSEARCH_VALUES])
        self.assertTrue(os.path.isfile(release.DATAHUB_OPENSEARCH_VALUES))
        # Nothing from .env but the namespace: no Secret, no credential (ADR-070 §2, §4).
        self.assertEqual(calls.values, [])
        self.assertEqual(rollout, [
            "kubectl", "--context", "kind-lakehouse-gate", "-n", "catalog-datahub", "rollout",
            "status", f"statefulset/{release.DATAHUB_OPENSEARCH}",
            f"--timeout={release.READY_TIMEOUT}"])
        self.assertEqual(notes, ["Step 1: StatefulSet datahub-opensearch in catalog-datahub is "
                                 "rolled out and ready (ADR-070)."])

    def test_an_empty_namespace_stops_before_any_call(self):
        calls = datahub_calls()
        problems, _ = self.step(calls, env={**POLARIS_ENV, "NS_CATALOG_DATAHUB": ""})
        self.assertIn("NS_CATALOG_DATAHUB", "\n".join(problems))
        self.assertEqual(calls.calls, [])

    def test_dry_run_is_server_side_and_waits_for_nothing(self):
        calls = datahub_calls()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        (helm,) = calls.calls
        self.assertEqual(helm[-1], "--dry-run=server")
        self.assertIn("dry-run", "\n".join(notes))

    def test_a_failed_helm_stops_with_its_error_before_waiting(self):
        calls = datahub_calls(helm=(1, "UPGRADE FAILED"))
        problems, _ = self.step(calls)
        self.assertIn("UPGRADE FAILED", "\n".join(problems))
        self.assertEqual([c[0] for c in calls.calls], ["helm"])

    def test_a_node_that_never_gets_ready_stops_the_install(self):
        calls = datahub_calls(rollout=(1, "error: timed out waiting for the condition"))
        problems, _ = self.step(calls)
        self.assertEqual(problems, [
            "StatefulSet datahub-opensearch in catalog-datahub is not ready: "
            "error: timed out waiting for the condition"])


class Datahub(unittest.TestCase):
    """ADR-071 §7: Datahub after datahub-opensearch in its part of step 1 (ADR-045)."""

    SECRETS = ["datahub-auth-secrets", "datahub-encryption-secrets", "datahub-frontend-secret",
               "datahub-root-user"]

    def step(self, calls, env=POLARIS_ENV, dry_run=False):
        return release.datahub(env, calls, dry_run=dry_run)

    def test_the_chart_and_its_values_are_the_ones_the_chart_test_renders(self):
        self.assertEqual(release.DATAHUB_RELEASE, "datahub")
        self.assertEqual(release.DATAHUB_CHART, os.path.join(release.ROOT, "charts", "datahub"))
        self.assertEqual(release.DATAHUB_VALUES,
                         os.path.join(release.DATAHUB_CHART, "values-lakehouse.yaml"))
        self.assertTrue(os.path.isfile(release.DATAHUB_VALUES))
        self.assertEqual(list(release.DATAHUB_SECRETS), self.SECRETS)

    def test_checks_the_secrets_key_names_then_installs_and_waits_on_helm(self):
        calls = datahub_calls()
        problems, notes = self.step(calls, env={**POLARIS_ENV,
                                                "DATAHUB_EMITTER_NAMESPACE_LABEL": "a=b"})
        self.assertEqual(problems, [])
        get, helm = calls.calls
        self.assertEqual(get, ["kubectl", "--context", "kind-lakehouse-gate", "-n",
                               "catalog-datahub", "get", "secret", *self.SECRETS, "-o",
                               release.SECRET_KEYS_TEMPLATE])
        values_file = helm[helm.index(release.DATAHUB_VALUES) + 2]
        self.assertEqual(helm, [
            "helm", "--kube-context", "kind-lakehouse-gate", "-n", "catalog-datahub", "upgrade",
            "--install", release.DATAHUB_RELEASE, release.DATAHUB_CHART, "-f",
            release.DATAHUB_VALUES, "-f", values_file, "--wait",
            f"--timeout={release.DATAHUB_TIMEOUT}"])
        self.assertTrue(values_file.endswith(".json"))
        self.assertFalse(os.path.exists(values_file))
        # From .env only the ingestion's Polaris URI, built from Polaris's namespace, and its
        # catalogs (ADR-076 §2): the credentials are in the Secrets.
        self.assertEqual(calls.values, [{"datahub-ingestion-cron": {"crons": {"polaris": {
            "env": {"POLARIS_URI": "http://polaris.catalog-polaris.svc.cluster.local:8181"
                                   "/api/catalog",
                    "DATAHUB_INGEST_POLARIS_CATALOGS": ""},
            "suspend": True}}}}])
        self.assertEqual(notes, [
            "Step 1: Datahub in catalog-datahub is installed: system-update's Jobs completed, "
            "GMS and the frontend ready; DATAHUB_INGEST_POLARIS_CATALOGS is empty, so the "
            "ingestion CronJob is suspended (ADR-071, ADR-076)."])

    def test_the_catalogs_to_ingest_reach_the_cronjob_which_runs(self):
        # ADR-076 §2: names from .env, each checked; spaces around a name are dropped.
        calls = datahub_calls()
        env = {**POLARIS_ENV, "DATAHUB_EMITTER_NAMESPACE_LABEL": "a=b",
               "DATAHUB_INGEST_POLARIS_CATALOGS": "sales, Ops-2,raw_data"}
        problems, notes = self.step(calls, env=env)
        self.assertEqual(problems, [])
        (values,) = calls.values
        cron = values["datahub-ingestion-cron"]["crons"]["polaris"]
        self.assertEqual(cron["env"]["DATAHUB_INGEST_POLARIS_CATALOGS"], "sales,Ops-2,raw_data")
        self.assertIs(cron["suspend"], False)
        self.assertEqual(notes, [
            "Step 1: Datahub in catalog-datahub is installed: system-update's Jobs completed, "
            "GMS and the frontend ready; the ingestion CronJob runs daily for sales, Ops-2, "
            "raw_data (ADR-071, ADR-076)."])

    def test_a_catalog_name_outside_the_pattern_stops_before_any_call(self):
        for value in ("sales;rm", "a,,b", "a,", "caf\u00e9", "a b"):
            with self.subTest(value=value):
                calls = datahub_calls()
                problems, _ = self.step(calls, env={**POLARIS_ENV,
                                                    "DATAHUB_INGEST_POLARIS_CATALOGS": value})
                self.assertEqual(problems, [
                    "DATAHUB_INGEST_POLARIS_CATALOGS must be Polaris catalog names matching "
                    "^[A-Za-z0-9_-]+$, comma-separated (ADR-076)"])
                self.assertEqual(calls.calls, [])

    def test_an_empty_polaris_namespace_stops_before_any_call(self):
        # The ingestion's URI names Polaris's namespace from .env, never one in a chart.
        calls = datahub_calls()
        problems, _ = self.step(calls, env={**POLARIS_ENV, "NS_CATALOG_POLARIS": ""})
        self.assertIn("NS_CATALOG_POLARIS", "\n".join(problems))
        self.assertEqual(calls.calls, [])

    def test_an_empty_emitters_label_is_said_and_the_install_goes_on(self):
        calls = datahub_calls()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual([c[0] for c in calls.calls], ["kubectl", "helm"])
        self.assertEqual(notes, [
            "Step 1: Datahub in catalog-datahub is installed: system-update's Jobs completed, "
            "GMS and the frontend ready; DATAHUB_EMITTER_NAMESPACE_LABEL is empty, so no "
            "lineage emitter's namespace may reach GMS; DATAHUB_INGEST_POLARIS_CATALOGS is "
            "empty, so the ingestion CronJob is suspended (ADR-071, ADR-076)."])

    def test_a_missing_secret_stops_before_helm(self):
        calls = datahub_calls(secret=False)
        problems, _ = self.step(calls)
        self.assertEqual(problems, [
            f"Secrets {', '.join(self.SECRETS)} must exist in catalog-datahub: run "
            "scripts/init_env.py --apply-secrets first (ADR-006, ADR-071)"])
        self.assertEqual([c[0] for c in calls.calls], ["kubectl"])

    def test_a_secret_without_its_keys_stops_before_helm_naming_keys_only(self):
        calls = datahub_calls(secret_keys={"datahub-auth-secrets": ["system_client_secret"],
                                           "datahub-root-user": []})
        problems, _ = self.step(calls)
        self.assertEqual(problems, [
            "Secret datahub-auth-secrets in catalog-datahub lacks token_service_salt, "
            "token_service_signing_key: run scripts/init_env.py --apply-secrets with this "
            "release first (ADR-071)",
            "Secret datahub-root-user in catalog-datahub lacks user.props: run "
            "scripts/init_env.py --apply-secrets with this release first (ADR-071)"])
        self.assertEqual([c[0] for c in calls.calls], ["kubectl"])

    def test_an_empty_namespace_stops_before_any_call(self):
        calls = datahub_calls()
        problems, _ = self.step(calls, env={**POLARIS_ENV, "NS_CATALOG_DATAHUB": ""})
        self.assertIn("NS_CATALOG_DATAHUB", "\n".join(problems))
        self.assertEqual(calls.calls, [])

    def test_dry_run_is_server_side_and_waits_for_nothing(self):
        calls = datahub_calls()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        _, helm = calls.calls
        self.assertEqual(helm[-1], "--dry-run=server")
        self.assertNotIn("--wait", helm)
        self.assertIn("dry-run", "\n".join(notes))

    def test_a_failed_or_timed_out_helm_stops_the_install(self):
        calls = datahub_calls(helm=(1, "Error: failed pre-install: job datahub-system-update "
                                       "failed: BackoffLimitExceeded"))
        problems, _ = self.step(calls)
        self.assertEqual(problems, [
            "helm upgrade datahub in catalog-datahub failed: Error: failed pre-install: job "
            "datahub-system-update failed: BackoffLimitExceeded"])


class Settle(unittest.TestCase):
    """settle reads the Cluster it is given, polaris-pg unless told otherwise."""

    @staticmethod
    def reads(*clusters):
        calls, answers = [], list(clusters)

        def run(argv):
            calls.append(argv)
            return 0, json.dumps(answers.pop(0) if len(answers) > 1 else answers[0])
        return calls, run

    def test_polaris_pg_by_default(self):
        calls, run = self.reads(cluster_status())
        self.assertIsNone(release.settle(run, "kind-lakehouse-gate", "catalog-polaris"))
        self.assertTrue(all(f"cluster/{release.POLARIS_PG}" in argv for argv in calls))

    # Smoke 38245393066: polaris-pg-restore read Ready, then refused psql while CNPG
    # restarted it after the recovery (#83).
    def test_the_cluster_it_is_given(self):
        calls, run = self.reads(cluster_status(instances=1, ready=0),
                                cluster_status(instances=1, ready=1))
        self.assertIsNone(release.settle(run, "kind-lakehouse-gate", "catalog-polaris",
                                         cluster="polaris-pg-restore"))
        self.assertEqual(len(calls), 1 + release.SETTLE_READS)
        for argv in calls:
            self.assertEqual(argv, ["kubectl", "--context", "kind-lakehouse-gate", "-n",
                                    "catalog-polaris", "get", "cluster/polaris-pg-restore",
                                    "-o", "json"])

    def test_a_problem_names_the_cluster_it_is_given(self):
        _, run = self.reads(cluster_status("Setting up primary", ready=0, instances=1))
        with mock.patch.object(release, "SETTLE_TIMEOUT", 0):
            problem = release.settle(run, "kind-lakehouse-gate", "catalog-polaris",
                                     cluster="polaris-pg-restore")
        self.assertIn("Cluster polaris-pg-restore in catalog-polaris", problem)
        self.assertIn("Setting up primary", problem)


class Steps(unittest.TestCase):
    def test_step_0_problems_stop_before_step_1(self):
        calls = []

        def run(argv):
            calls.append(argv)
            return 1, "NotFound"
        problems, _ = release.install(ENDPOINT_ENV, run, ["1.30"], dry_run=False)
        self.assertIn(release.CNPG_CRD, "\n".join(problems))
        self.assertTrue(all(c[0] == "kubectl" and "crd" in c for c in calls))

    def test_every_step_passes_in_order(self):
        step0, pg, endpoints = FakeKubectl(), FakeCalls(), FakeRun()
        boot, pol, claim = FakeBootstrap(), FakePolaris(), FakeClaim()

        def run(argv, stdin=None):
            if "crd" in argv or "cnpg-system" in argv:
                return step0(argv)
            if "endpoint-check" in " ".join(argv):
                return endpoints(argv, stdin)
            if "storageclass" in argv or "apply" in argv:
                return claim(argv, stdin)
            if is_principal(argv):
                return FakePrincipal()(argv, stdin)
            if "polaris-root" in argv or "run" in argv:
                return boot(argv, stdin)
            if release.BATCH_CHART in argv or "cronjob/polaris-log-batch" in argv:
                return batch(argv, stdin)
            if release.SIDECAR_CHART in argv:
                order.append(release.SIDECAR_CHART)
                return sidecar(argv, stdin)
            if "catalog-datahub" in argv:
                return dh(argv)
            if "polaris-s3" in argv or "rollout" in argv or release.POLARIS_CHART in argv:
                if release.POLARIS_CHART in argv:
                    order.append(release.POLARIS_CHART)
                return pol(argv, stdin)
            return pg(argv)
        batch, dh, sidecar, order = FakeBatch(), datahub_calls(), FakeSidecar(), []
        problems, notes = release.install({**BOOTSTRAP_ENV, **LOGS_ENV}, run, ["1.30"],
                                          dry_run=False)
        self.assertEqual(problems, [])
        self.assertEqual([n.split(":")[0] for n in notes],
                         ["Step 0"] * 2 + ["Step 1"] * 13 + ["Step 2"] * 2)
        self.assertIn("Barman Cloud Plugin", notes[1])
        self.assertIn("network policies", notes[2])
        self.assertIn("polaris-pg", notes[3])
        self.assertIn("answer", notes[4])
        self.assertIn("bootstrapped", notes[5])
        self.assertIn("polaris-logs", notes[6])
        # ADR-082: the sidecar's ConfigMap before Polaris, whose pods mount it.
        self.assertIn("log sidecar", notes[7])
        self.assertIn("deployment/polaris", notes[8])
        # ADR-045 step 1, ADR-066 §4, ADR-068 §4, ADR-070 §4: then Datahub, its policies,
        # datahub-pg, its Kafka, then its OpenSearch.
        self.assertIn("Datahub's network policies", notes[9])
        self.assertIn("datahub-pg", notes[10])
        self.assertIn("datahub-kafka", notes[11])
        self.assertIn("datahub-opensearch", notes[12])
        # ADR-071 §7: then Datahub itself.
        self.assertIn("Datahub in catalog-datahub is installed", notes[13])
        # ADR-076 §3: then its Polaris principal, last in step 1.
        self.assertIn("principal datahub_ingest", notes[14])
        self.assertIn("cronjob/polaris-log-batch", notes[15])
        self.assertIn("console is not enabled", notes[16])
        self.assertEqual(order[:2], [release.SIDECAR_CHART, release.POLARIS_CHART])
        # ADR-045 §2: the policies first in step 1, before polaris-pg.
        helms = [c for c in pg.calls if c[0] == "helm"]
        self.assertEqual([h[h.index("--install") + 1] for h in helms],
                         [release.POLARIS_NETPOL_RELEASE, release.POLARIS_PG_RELEASE])
        helms = [c for c in dh.calls if c[0] == "helm"]
        self.assertEqual([h[h.index("--install") + 1] for h in helms],
                         [release.DATAHUB_NETPOL_RELEASE, release.DATAHUB_PG_RELEASE,
                          release.DATAHUB_KAFKA_RELEASE, release.DATAHUB_OPENSEARCH_RELEASE,
                          release.DATAHUB_RELEASE])

    def test_failed_network_policies_stop_before_polaris_pg(self):
        step0, pg = FakeKubectl(), FakeCalls(helm=(1, "values don't meet the schema"))

        def run(argv, stdin=None):
            if "crd" in argv or "cnpg-system" in argv:
                return step0(argv)
            return pg(argv)
        problems, _ = release.install(BOOTSTRAP_ENV, run, ["1.30"], dry_run=False)
        self.assertIn("schema", "\n".join(problems))
        helms = [c for c in pg.calls if c[0] == "helm"]
        self.assertEqual(len(helms), 1)
        self.assertIn(release.POLARIS_NETPOL_CHART, helms[0])

    def test_a_failed_batch_stops_the_install_before_the_console(self):
        step0, pg, endpoints = FakeKubectl(), FakeCalls(), FakeRun()
        boot, pol, claim = FakeBootstrap(), FakePolaris(), FakeClaim()
        batch, console = FakeBatch(helm=(1, "image tag invalid")), FakeConsole()

        def run(argv, stdin=None):
            if "crd" in argv or "cnpg-system" in argv:
                return step0(argv)
            if "endpoint-check" in " ".join(argv):
                return endpoints(argv, stdin)
            if "storageclass" in argv or "apply" in argv:
                return claim(argv, stdin)
            if is_principal(argv):
                return FakePrincipal()(argv, stdin)
            if "polaris-root" in argv or "run" in argv:
                return boot(argv, stdin)
            if release.BATCH_CHART in argv or "cronjob/polaris-log-batch" in argv:
                return batch(argv, stdin)
            if release.CONSOLE_CHART in argv or "deployment/polaris-console" in argv:
                return console(argv, stdin)
            if "catalog-datahub" in argv:
                return dh(argv)
            if release.SIDECAR_CHART in argv:
                return (0, "")
            if "polaris-s3" in argv or "rollout" in argv or release.POLARIS_CHART in argv:
                return pol(argv, stdin)
            return pg(argv)
        dh = datahub_calls()
        problems, notes = release.install({**BOOTSTRAP_ENV, **LOGS_ENV, **CONSOLE_ENV}, run,
                                          ["1.30"], dry_run=False)
        self.assertIn("image tag invalid", "\n".join(problems))
        self.assertEqual(console.calls, [])
        self.assertIn("principal datahub_ingest", notes[-1])

    def test_a_failed_datahub_pg_stops_the_install_before_the_log_batch(self):
        """ADR-045: every service of step 1 before step 2."""
        step0, pg, endpoints = FakeKubectl(), FakeCalls(), FakeRun()
        boot, pol, claim, batch = FakeBootstrap(), FakePolaris(), FakeClaim(), FakeBatch()
        dh = datahub_calls(backup_keys=("accessKeyId", "secretAccessKey"))

        def run(argv, stdin=None):
            if "crd" in argv or "cnpg-system" in argv:
                return step0(argv)
            if "endpoint-check" in " ".join(argv):
                return endpoints(argv, stdin)
            if "storageclass" in argv or "apply" in argv:
                return claim(argv, stdin)
            if is_principal(argv):
                return FakePrincipal()(argv, stdin)
            if "polaris-root" in argv or "run" in argv:
                return boot(argv, stdin)
            if release.BATCH_CHART in argv or "cronjob/polaris-log-batch" in argv:
                return batch(argv, stdin)
            if "catalog-datahub" in argv:
                return dh(argv)
            if release.SIDECAR_CHART in argv:
                return (0, "")
            if "polaris-s3" in argv or "rollout" in argv or release.POLARIS_CHART in argv:
                return pol(argv, stdin)
            return pg(argv)
        problems, notes = release.install({**BOOTSTRAP_ENV, **LOGS_ENV}, run, ["1.30"],
                                          dry_run=False)
        self.assertEqual(problems, [
            "Secret datahub-backup-s3 in catalog-datahub has no key region: run "
            "scripts/init_env.py --apply-secrets with this release first (ADR-066)"])
        self.assertEqual(batch.calls, [])
        self.assertIn("Datahub's network policies", notes[-1])

    def test_a_kafka_that_never_gets_ready_stops_the_install_before_the_log_batch(self):
        """ADR-045, ADR-068 §4: Datahub's Kafka is part of step 1."""
        step0, pg, endpoints = FakeKubectl(), FakeCalls(), FakeRun()
        boot, pol, claim, batch = FakeBootstrap(), FakePolaris(), FakeClaim(), FakeBatch()
        dh = datahub_calls(rollout=(1, "error: timed out waiting for the condition"))

        def run(argv, stdin=None):
            if "crd" in argv or "cnpg-system" in argv:
                return step0(argv)
            if "endpoint-check" in " ".join(argv):
                return endpoints(argv, stdin)
            if "storageclass" in argv or "apply" in argv:
                return claim(argv, stdin)
            if is_principal(argv):
                return FakePrincipal()(argv, stdin)
            if "polaris-root" in argv or "run" in argv:
                return boot(argv, stdin)
            if release.BATCH_CHART in argv or "cronjob/polaris-log-batch" in argv:
                return batch(argv, stdin)
            if "catalog-datahub" in argv:
                return dh(argv)
            if release.SIDECAR_CHART in argv:
                return (0, "")
            if "polaris-s3" in argv or "rollout" in argv or release.POLARIS_CHART in argv:
                return pol(argv, stdin)
            return pg(argv)
        problems, notes = release.install({**BOOTSTRAP_ENV, **LOGS_ENV}, run, ["1.30"],
                                          dry_run=False)
        self.assertIn("StatefulSet datahub-kafka in catalog-datahub is not ready",
                      "\n".join(problems))
        self.assertEqual(batch.calls, [])
        self.assertIn("datahub-pg", notes[-1])

    def test_a_missing_storage_class_stops_before_polaris(self):
        step0, pg, endpoints = FakeKubectl(), FakeCalls(), FakeRun()
        boot, pol = FakeBootstrap(), FakePolaris()
        claim = FakeClaim(storage_class=(1, "NotFound"))

        def run(argv, stdin=None):
            if "crd" in argv or "cnpg-system" in argv:
                return step0(argv)
            if "endpoint-check" in " ".join(argv):
                return endpoints(argv, stdin)
            if "storageclass" in argv or "apply" in argv:
                return claim(argv, stdin)
            if is_principal(argv):
                return FakePrincipal()(argv, stdin)
            if "polaris-root" in argv or "run" in argv:
                return boot(argv, stdin)
            if release.SIDECAR_CHART in argv:
                return (0, "")
            if "polaris-s3" in argv or "rollout" in argv or release.POLARIS_CHART in argv:
                return pol(argv, stdin)
            return pg(argv)
        problems, _ = release.install({**BOOTSTRAP_ENV, **LOGS_ENV}, run, ["1.30"],
                                      dry_run=False)
        self.assertIn("ReadWriteMany", "\n".join(problems))
        self.assertEqual(pol.calls, [])

    def test_a_console_that_fails_is_reported_and_the_install_still_passes(self):
        step0, pg, endpoints = FakeKubectl(), FakeCalls(), FakeRun()
        boot, pol, claim = FakeBootstrap(), FakePolaris(), FakeClaim()
        console = FakeConsole(rollout=(1, "ErrImageNeverPull"))

        def run(argv, stdin=None):
            if "crd" in argv or "cnpg-system" in argv:
                return step0(argv)
            if "endpoint-check" in " ".join(argv):
                return endpoints(argv, stdin)
            if "storageclass" in argv or "apply" in argv:
                return claim(argv, stdin)
            if is_principal(argv):
                return FakePrincipal()(argv, stdin)
            if "polaris-root" in argv or "run" in argv:
                return boot(argv, stdin)
            if release.BATCH_CHART in argv or "cronjob/polaris-log-batch" in argv:
                return FakeBatch()(argv, stdin)
            if release.CONSOLE_CHART in argv or "deployment/polaris-console" in argv:
                return console(argv, stdin)
            if "catalog-datahub" in argv:
                return dh(argv)
            if release.SIDECAR_CHART in argv:
                return (0, "")
            if "polaris-s3" in argv or "rollout" in argv or release.POLARIS_CHART in argv:
                return pol(argv, stdin)
            return pg(argv)
        dh = datahub_calls()
        problems, notes = release.install({**BOOTSTRAP_ENV, **LOGS_ENV, **CONSOLE_ENV}, run,
                                          ["1.30"], dry_run=False)
        self.assertEqual(problems, [])
        self.assertIn("ErrImageNeverPull", notes[-1])
        self.assertIn("not blocking", notes[-1])

    def test_a_failed_bootstrap_stops_before_polaris(self):
        step0, pg, endpoints = FakeKubectl(), FakeCalls(), FakeRun()
        boot, pol = FakeBootstrap(answer=(1, "connection refused")), FakePolaris()
        claim = FakeClaim()

        def run(argv, stdin=None):
            if "crd" in argv or "cnpg-system" in argv:
                return step0(argv)
            if "endpoint-check" in " ".join(argv):
                return endpoints(argv, stdin)
            if "storageclass" in argv or "apply" in argv:
                return claim(argv, stdin)
            if is_principal(argv):
                return FakePrincipal()(argv, stdin)
            if "polaris-root" in argv or "run" in argv:
                return boot(argv, stdin)
            if release.SIDECAR_CHART in argv:
                return (0, "")
            if "polaris-s3" in argv or "rollout" in argv or release.POLARIS_CHART in argv:
                return pol(argv, stdin)
            return pg(argv)
        problems, _ = release.install(BOOTSTRAP_ENV, run, ["1.30"], dry_run=False)
        self.assertIn("connection refused", "\n".join(problems))
        self.assertEqual(pol.calls, [])

    def test_endpoints_that_do_not_answer_stop_the_install(self):
        step0, step1 = FakeKubectl(), FakeCalls()

        def run(argv, stdin=None):
            if "crd" in argv or "cnpg-system" in argv:
                return step0(argv)
            return (1, "http://s3.example:8333 did not answer") if "run" in argv \
                else step1(argv)
        problems, _ = release.install(ENDPOINT_ENV, run, ["1.30"], dry_run=False)
        self.assertIn("did not answer", "\n".join(problems))

    def test_a_dry_run_checks_the_endpoints_too(self):
        step0, step1, endpoints, claim = FakeKubectl(), FakeCalls(), FakeRun(), FakeClaim()

        def run(argv, stdin=None):
            if "crd" in argv or "cnpg-system" in argv:
                return step0(argv)
            if "storageclass" in argv or "apply" in argv:
                return claim(argv, stdin)
            if "catalog-datahub" in argv:
                return dh(argv)
            return endpoints(argv, stdin) if "run" in argv else step1(argv)
        dh = datahub_calls()
        problems, _ = release.install({**ENDPOINT_ENV, **LOGS_ENV}, run, ["1.30"], dry_run=True)
        self.assertEqual(problems, [])
        self.assertEqual(len(endpoints.calls), 1)


ENDPOINT_ENV = {**POLARIS_ENV, "S3_ENDPOINT": "http://s3.example:8333",
                "POLARIS_OPA_URL": "http://opa.example:8181/v1/data/polaris/allow"}
CURL = "curlimages/curl:8.0.0@sha256:" + "b" * 64


class FakeRun:
    """Answers the endpoint check's `kubectl run`; records every call."""

    def __init__(self, answer=(0, "answered")):
        self.answer, self.calls = answer, []

    def __call__(self, argv, stdin=None):
        self.calls.append(argv)
        self.stdin = stdin
        return self.answer


class Endpoints(unittest.TestCase):
    """ADR-029: before Polaris, the S3 and policy endpoints answer, seen from its namespace."""

    def check(self, calls, env=ENDPOINT_ENV):
        return release.endpoints_answer(env, calls, image=CURL, name="endpoint-check-test")

    def test_one_short_lived_pod_in_polaris_namespace_asks_both_endpoints(self):
        calls = FakeRun()
        problems, notes = self.check(calls)
        self.assertEqual(problems, [])
        (argv,) = calls.calls
        self.assertEqual(argv[:5], ["kubectl", "--context", "kind-lakehouse-gate",
                                    "-n", "catalog-polaris"])
        self.assertEqual(argv[5:7], ["run", "endpoint-check-test"])
        for flag in ("--rm", "-i", "--restart=Never", f"--image={CURL}"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[-2:], ["http://s3.example:8333",
                                     "http://opa.example:8181/v1/data/polaris/allow"])
        self.assertIn("answer", "\n".join(notes))

    def test_the_script_goes_on_stdin_and_the_endpoints_are_its_arguments(self):
        calls = FakeRun()
        self.check(calls)
        argv = calls.calls[0]
        self.assertEqual(argv[-5:-2], ["sh", "-s", "--"])
        self.assertEqual(calls.stdin, release.ENDPOINT_CHECK)
        self.assertNotIn("example", calls.stdin)

    def test_an_empty_endpoint_stops_before_any_call(self):
        for name in ("S3_ENDPOINT", "POLARIS_OPA_URL"):
            with self.subTest(name=name):
                calls = FakeRun()
                problems, _ = self.check(calls, env={**ENDPOINT_ENV, name: ""})
                self.assertIn(name, "\n".join(problems))
                self.assertEqual(calls.calls, [])

    def test_no_answer_stops_with_what_the_pod_said(self):
        problems, _ = self.check(FakeRun(answer=(1, "http://s3.example:8333 did not answer")))
        self.assertIn("did not answer", "\n".join(problems))

    def test_the_curl_image_is_pinned_by_digest(self):
        self.assertRegex(release.images()["curl"],
                         r"^curlimages/curl:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")


class EndpointScript(unittest.TestCase):
    """The pod's script, run here with this machine's curl against a local server."""

    def run_script(self, *urls):
        return subprocess.run(["sh", "-s", "--", *urls], input=release.ENDPOINT_CHECK,
                              capture_output=True, text=True, check=False)

    def test_any_http_status_is_an_answer_and_no_connection_is_not(self):
        import http.server
        import threading

        class Forbidden(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(403)
                self.end_headers()

            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Forbidden)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            up = f"http://127.0.0.1:{server.server_port}/"
            result = self.run_script(up)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("HTTP 403", result.stdout)
            closed = "http://127.0.0.1:9/"
            result = self.run_script(up, closed)
            self.assertEqual(result.returncode, 1)
            self.assertIn(f"{closed} did not answer", result.stdout)
        finally:
            server.shutdown()
            server.server_close()


class Call(unittest.TestCase):
    def test_a_failed_command_returns_its_stdout_and_its_stderr(self):
        code, out = release.call(["sh", "-c",
                                  "echo said-on-stdout; echo said-on-stderr >&2; exit 3"])
        self.assertEqual(code, 3)
        self.assertIn("said-on-stdout", out)
        self.assertIn("said-on-stderr", out)

    def test_a_command_that_succeeds_returns_its_stdout_only(self):
        self.assertEqual(release.call(["sh", "-c", "echo out; echo err >&2"]), (0, "out\n"))


class EndpointsThroughCall(unittest.TestCase):
    """#37: the URL that did not answer reaches the install's problem. A stand-in kubectl runs
    the check's script here, its output on stdout as `kubectl run -i` relays the pod's."""

    def test_the_problem_names_the_url_that_did_not_answer(self):
        with tempfile.TemporaryDirectory() as d:
            kubectl = os.path.join(d, "kubectl")
            with open(kubectl, "w", encoding="utf-8") as f:
                f.write('#!/bin/sh\nwhile [ "$1" != "--command" ]; do shift; done\n'
                        'shift 2\nexec "$@"\n')
            os.chmod(kubectl, 0o755)
            closed = "http://127.0.0.1:9/"
            env = {**ENDPOINT_ENV, "S3_ENDPOINT": closed, "POLARIS_OPA_URL": closed + "opa"}
            with mock.patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ["PATH"]}):
                problems, _ = release.endpoints_answer(env, release.call, image=CURL,
                                                       name="endpoint-check-test")
        self.assertIn(f"{closed} did not answer", "\n".join(problems))
        self.assertIn(f"{closed}opa did not answer", "\n".join(problems))


BOOTSTRAP_ENV = {**ENDPOINT_ENV, "POLARIS_DB_HOST": "",
                 "POLARIS_ROOT_CLIENT_ID": "rootid0123456789",
                 "POLARIS_ROOT_CLIENT_SECRET": "RootSecretValue0123456789abcdefg",
                 "POLARIS_DB_PASSWORD": "DbPasswordValue0123456789abcdefgh"}
ADMIN = "apache/polaris-admin-tool:1.6.0@sha256:" + "c" * 64


class FakeBootstrap:
    """Answers the bootstrap's Secret check and its `kubectl run`; records every call."""

    def __init__(self, secret=True, answer=(0, "Realm 'POLARIS' successfully bootstrapped.")):
        self.secret, self.answer, self.calls, self.stdin = secret, answer, [], None

    def __call__(self, argv, stdin=None):
        self.calls.append(argv)
        if "secret" in argv:
            return (0, "secret/polaris-root") if self.secret else (1, "NotFound")
        if "run" in argv:
            self.stdin = stdin
            return self.answer
        raise AssertionError(f"unexpected call {argv}")


class Bootstrap(unittest.TestCase):
    """ADR-026: the realm and root principal, by Polaris's admin tool, once."""

    def step(self, calls, env=BOOTSTRAP_ENV, dry_run=False):
        return release.bootstrap(env, calls, image=ADMIN, name="polaris-bootstrap-test",
                                 dry_run=dry_run)

    def run_call(self, calls):
        (argv,) = [c for c in calls.calls if "run" in c]
        return argv

    def overrides(self, argv):
        flag = next(a for a in argv if a.startswith("--overrides="))
        return json.loads(flag.split("=", 1)[1])

    def test_one_short_lived_admin_tool_pod_in_polaris_namespace(self):
        calls = FakeBootstrap()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        argv = self.run_call(calls)
        self.assertEqual(argv[:5], ["kubectl", "--context", "kind-lakehouse-gate",
                                    "-n", "catalog-polaris"])
        self.assertEqual(argv[5:7], ["run", "polaris-bootstrap-test"])
        for flag in ("--rm", "-i", "--restart=Never", f"--image={ADMIN}",
                     "--override-type=strategic"):
            self.assertIn(flag, argv)
        self.assertIn("bootstrapped", "\n".join(notes))

    def test_it_bootstraps_the_realm_with_the_root_credential_from_its_secret(self):
        calls = FakeBootstrap()
        self.step(calls)
        argv = self.run_call(calls)
        tail = argv[argv.index("--") + 1:]
        self.assertEqual(tail[-5:], ["bootstrap", "-r", "POLARIS", "-c",
                                     "POLARIS,$(POLARIS_ROOT_CLIENT_ID),"
                                     "$(POLARIS_ROOT_CLIENT_SECRET)"])
        (container,) = self.overrides(argv)["spec"]["containers"]
        self.assertEqual((container["name"], container["image"], container["stdin"],
                          container["stdinOnce"]), ("polaris-bootstrap-test", ADMIN, True, True))
        env = {e["name"]: e for e in container["env"]}
        self.assertEqual(env["POLARIS_ROOT_CLIENT_ID"]["valueFrom"]["secretKeyRef"],
                         {"name": "polaris-root", "key": "clientId"})
        self.assertEqual(env["POLARIS_ROOT_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"],
                         {"name": "polaris-root", "key": "clientSecret"})

    def test_the_pod_carries_the_label_polaris_pgs_policy_allows(self):
        # ADR-048 §3: only pods labelled as its clients reach polaris-pg.
        calls = FakeBootstrap()
        self.step(calls)
        self.assertIn(f"--labels={release.PG_CLIENT_LABEL}=true", self.run_call(calls))

    def test_it_reaches_the_database_as_polaris_does(self):
        calls = FakeBootstrap()
        self.step(calls, env={**BOOTSTRAP_ENV, "POLARIS_DB_HOST": "recorder.standins.svc"})
        (container,) = self.overrides(self.run_call(calls))["spec"]["containers"]
        env = {e["name"]: e for e in container["env"]}
        self.assertEqual(env["QUARKUS_DATASOURCE_JDBC_URL"]["value"],
                         "jdbc:postgresql://recorder.standins.svc:5432/polaris?sslmode=require")
        self.assertEqual(env["QUARKUS_DATASOURCE_USERNAME"]["valueFrom"]["secretKeyRef"],
                         {"name": "polaris-db", "key": "username"})
        self.assertEqual(env["QUARKUS_DATASOURCE_PASSWORD"]["valueFrom"]["secretKeyRef"],
                         {"name": "polaris-db", "key": "password"})

    def test_no_credential_is_in_the_command_line(self):
        calls = FakeBootstrap()
        self.step(calls)
        text = " ".join(self.run_call(calls))
        for var in ("POLARIS_ROOT_CLIENT_ID", "POLARIS_ROOT_CLIENT_SECRET", "POLARIS_DB_PASSWORD"):
            self.assertNotIn(BOOTSTRAP_ENV[var], text)

    def test_the_tool_starts_only_once_kubectl_has_attached(self):
        calls = FakeBootstrap()
        self.step(calls)
        self.assertIsNotNone(calls.stdin)
        argv = self.run_call(calls)
        tail = argv[argv.index("--") + 1:]
        self.assertEqual(tail[:3], ["sh", "-c", release.BOOTSTRAP_WRAPPER])

    def test_an_already_bootstrapped_realm_is_kept(self):
        answer = (3, "java.lang.IllegalArgumentException: It appears this metastore manager "
                     "has already been bootstrapped. To continue bootstrapping, please first "
                     "purge the metastore with the `purge` command.")
        problems, notes = self.step(FakeBootstrap(answer=answer))
        self.assertEqual(problems, [])
        self.assertIn("already bootstrapped", "\n".join(notes))

    def test_any_other_failure_stops_and_never_prints_a_credential(self):
        answer = (3, "Invalid credentials format: POLARIS,rootid0123456789,"
                     "RootSecretValue0123456789abcdefg\nBootstrap encountered errors")
        problems, _ = self.step(FakeBootstrap(answer=answer))
        text = "\n".join(problems)
        self.assertIn("Bootstrap encountered errors", text)
        self.assertNotIn("RootSecretValue0123456789abcdefg", text)
        self.assertNotIn("rootid0123456789", text)

    def test_a_missing_root_secret_stops_before_the_pod_and_names_init_env(self):
        calls = FakeBootstrap(secret=False)
        problems, _ = self.step(calls)
        self.assertIn("polaris-root", "\n".join(problems))
        self.assertIn("init_env.py --apply-secrets", "\n".join(problems))
        self.assertFalse(any("run" in c for c in calls.calls))

    def test_a_dry_run_makes_no_call(self):
        calls = FakeBootstrap()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual((problems, calls.calls), ([], []))
        self.assertIn("dry-run", "\n".join(notes))

    def test_the_admin_tool_is_pinned_by_digest(self):
        self.assertRegex(release.images()["polaris_admin_tool"],
                         r"^apache/polaris-admin-tool:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")


class BootstrapWrapper(unittest.TestCase):
    """The wrapper, run here with sh: the tool starts after stdin ends, with its arguments, and
    all its output goes to stderr, which is what a failed `kubectl run` returns."""

    def test_the_entrypoint_gets_the_arguments_after_stdin_ends(self):
        with tempfile.TemporaryDirectory() as d:
            entrypoint = os.path.join(d, "run-java.sh")
            with open(entrypoint, "w", encoding="utf-8") as f:
                f.write('#!/bin/sh\necho "args: $*"\nexit 3\n')
            os.chmod(entrypoint, 0o755)
            wrapper = release.BOOTSTRAP_WRAPPER.replace(release.ADMIN_TOOL_ENTRYPOINT, entrypoint)
            result = subprocess.run(["sh", "-c", wrapper, "admin-tool", "bootstrap", "-r", "R"],
                                    input="\n", capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual((result.stdout, result.stderr), ("", "args: bootstrap -r R\n"))


PRINCIPAL_ENV = {**BOOTSTRAP_ENV, "DATAHUB_POLARIS_CLIENT_ID": "ingestid012345678",
                 "DATAHUB_POLARIS_CLIENT_SECRET": "IngestSecretValue0123456789abcde"}
INGESTION = "docker.io/acryldata/datahub-ingestion:v1.7.0.1@sha256:" + "d" * 64


def is_principal(argv):
    """The principal step's calls (ADR-076 §3), as the install's routers tell them apart."""
    return release.DATAHUB_PRINCIPAL_SECRET in argv or "datahub-principal-" in " ".join(argv)


class FakePrincipal:
    """Answers the principal step's Secret read and its `kubectl run`; records every call."""

    def __init__(self, keys="clientId clientSecret", answer=(0, "ok   done\n")):
        self.keys, self.answer, self.calls, self.stdin = keys, answer, [], None

    def __call__(self, argv, stdin=None):
        self.calls.append(argv)
        if "secret" in argv:
            return (1, "NotFound") if self.keys is None else (0, self.keys)
        if "run" in argv:
            self.stdin = stdin
            return self.answer
        raise AssertionError(f"unexpected call {argv}")


class DatahubPrincipal(unittest.TestCase):
    """ADR-076 §3: step 1's last part, after Datahub: datahub_ingest created by root and given
    init-env's credential, from a pod in Datahub's namespace."""

    def step(self, calls, env=PRINCIPAL_ENV, dry_run=False):
        return release.datahub_principal(env, calls, image=INGESTION,
                                         name="datahub-principal-test", dry_run=dry_run)

    def run_call(self, calls):
        (argv,) = [c for c in calls.calls if "run" in c]
        return argv

    def test_the_name_and_the_secret(self):
        self.assertEqual(release.DATAHUB_PRINCIPAL, "datahub_ingest")
        self.assertEqual(release.DATAHUB_PRINCIPAL_SECRET, "datahub-polaris-principal")

    def test_the_image_is_the_ingestion_cronjobs_from_the_values_file(self):
        image = release.ingestion_image()
        self.assertRegex(image, r"^docker\.io/acryldata/datahub-ingestion:v1\.7\.0\.1"
                                r"@sha256:[0-9a-f]{64}$")
        with open(release.DATAHUB_VALUES, encoding="utf-8") as f:
            self.assertIn(image.split(":", 1)[1], f.read())

    def test_reads_the_secrets_key_names_then_runs_one_pod_in_datahubs_namespace(self):
        calls = FakePrincipal()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        get, argv = calls.calls
        self.assertEqual(get, ["kubectl", "--context", "kind-lakehouse-gate", "-n",
                               "catalog-datahub", "get", "secret", "datahub-polaris-principal",
                               "-o", release.SECRET_ONE_KEYS_TEMPLATE])
        self.assertEqual(argv[:7], ["kubectl", "--context", "kind-lakehouse-gate", "-n",
                                    "catalog-datahub", "run", "datahub-principal-test"])
        for flag in ("--rm", "-i", "--restart=Never", f"--image={INGESTION}",
                     "--override-type=strategic"):
            self.assertIn(flag, argv)
        tail = argv[argv.index("--command") + 1:]
        self.assertEqual(tail[:3], ["--", "python3", "-c"])
        self.assertEqual(tail[3], release.PRINCIPAL_SCRIPT)
        self.assertEqual(tail[4:], ["http://polaris.catalog-polaris.svc.cluster.local:8181",
                                    "POLARIS", "datahub_ingest"])
        self.assertEqual(notes, [
            "Step 1: Polaris principal datahub_ingest has datahub-polaris-principal's "
            "credential and gets a token with it (ADR-076)."])

    def test_the_pod_mounts_the_secret_and_gets_roots_credential_on_stdin(self):
        calls = FakePrincipal()
        self.step(calls)
        argv = self.run_call(calls)
        flag = next(a for a in argv if a.startswith("--overrides="))
        (container,) = json.loads(flag.split("=", 1)[1])["spec"]["containers"]
        self.assertEqual((container["name"], container["image"], container["stdin"],
                          container["stdinOnce"]), ("datahub-principal-test", INGESTION, True,
                                                    True))
        env = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]}
        self.assertEqual(env, {
            "CLIENT_ID": {"name": "datahub-polaris-principal", "key": "clientId"},
            "CLIENT_SECRET": {"name": "datahub-polaris-principal", "key": "clientSecret"}})
        self.assertEqual(calls.stdin, "rootid0123456789\nRootSecretValue0123456789abcdefg\n")
        text = " ".join(argv)
        for var in ("POLARIS_ROOT_CLIENT_ID", "POLARIS_ROOT_CLIENT_SECRET",
                    "DATAHUB_POLARIS_CLIENT_ID", "DATAHUB_POLARIS_CLIENT_SECRET"):
            self.assertNotIn(PRINCIPAL_ENV[var], text)

    def test_a_missing_secret_or_key_stops_before_the_pod(self):
        for keys, lacking in ((None, None), ("clientId", "clientSecret")):
            with self.subTest(keys=keys):
                calls = FakePrincipal(keys=keys)
                problems, _ = self.step(calls)
                if lacking:
                    self.assertEqual(problems, [
                        "Secret datahub-polaris-principal in catalog-datahub lacks clientSecret: "
                        "run scripts/init_env.py --apply-secrets with this release first "
                        "(ADR-076)"])
                else:
                    self.assertEqual(problems, [
                        "Secret datahub-polaris-principal must exist in catalog-datahub: run "
                        "scripts/init_env.py --apply-secrets first (ADR-006, ADR-076)"])
                self.assertEqual([c for c in calls.calls if "run" in c], [])

    def test_a_failed_pod_stops_the_install_with_its_lines_masked(self):
        said = ("ok   a token as root\nFAIL resetting datahub_ingest's credentials: HTTP 403, "
                "ingestid012345678 refused\n")
        problems, _ = self.step(FakePrincipal(answer=(1, said)))
        self.assertEqual(problems, [
            "Polaris principal datahub_ingest in catalog-datahub was not set (exit 1):\n"
            "ok   a token as root\nFAIL resetting datahub_ingest's credentials: HTTP 403, "
            "<redacted> refused"])

    def test_dry_run_makes_no_call_and_says_so(self):
        calls = FakePrincipal()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual((problems, calls.calls), ([], []))
        self.assertEqual(notes, ["Step 1: Polaris principal datahub_ingest is not set in a "
                                 "dry-run (ADR-076)."])


class FakePolarisApi(http.server.BaseHTTPRequestHandler):
    """Polaris's token and principal endpoints, as PRINCIPAL_SCRIPT calls them."""

    credentials = {}
    existing = set()
    requests = []
    reset_status = 200

    def log_message(self, *args):
        pass

    def answer(self, status, body=None):
        raw = json.dumps(body or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode()
        cls = type(self)
        cls.requests.append((self.path, self.headers.get("Polaris-Realm"),
                             self.headers.get("Authorization")))
        if self.path == "/api/catalog/v1/oauth/tokens":
            form = urllib.parse.parse_qs(raw)
            cid, secret = form["client_id"][0], form["client_secret"][0]
            if cls.credentials.get(cid) != secret or form["scope"] != ["PRINCIPAL_ROLE:ALL"]:
                return self.answer(401, {"error": "unauthorized_client"})
            return self.answer(200, {"access_token": f"token-{cid}"})
        body = json.loads(raw)
        if self.path == "/api/management/v1/principals":
            name = body["principal"]["name"]
            if name in cls.existing:
                return self.answer(409, {"error": {"message": "already exists"}})
            cls.existing.add(name)
            return self.answer(201, {"credentials": {"clientId": "x", "clientSecret": "y"}})
        if self.path.startswith("/api/management/v1/principals/") and self.path.endswith(
                "/reset"):
            if cls.reset_status != 200:
                return self.answer(cls.reset_status, {"error": {"message": "refused"}})
            cls.credentials[body["clientId"]] = body["clientSecret"]
            return self.answer(200, {})
        return self.answer(404)


class PrincipalScript(unittest.TestCase):
    """PRINCIPAL_SCRIPT run here against a stand-in Polaris (ADR-076 §3)."""

    def setUp(self):
        FakePolarisApi.credentials = {"rootid": "rootsecret"}
        FakePolarisApi.existing = set()
        FakePolarisApi.requests = []
        FakePolarisApi.reset_status = 200
        self.server = http.server.HTTPServer(("127.0.0.1", 0), FakePolarisApi)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def run_script(self, stdin="rootid\nrootsecret\n"):
        return subprocess.run([sys.executable, "-c", release.PRINCIPAL_SCRIPT, self.url,
                               "POLARIS", "datahub_ingest"], input=stdin, capture_output=True,
                              text=True, check=False, timeout=60,
                              env={**os.environ, "CLIENT_ID": "ingestid",
                                   "CLIENT_SECRET": "ingestsecret"})

    def test_creates_the_principal_sets_its_credential_and_gets_its_token(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "ok   a token as root",
            "ok   principal datahub_ingest created",
            "ok   datahub_ingest's credentials reset to datahub-polaris-principal's",
            "ok   a token as datahub_ingest"])
        self.assertEqual([r[0] for r in FakePolarisApi.requests], [
            "/api/catalog/v1/oauth/tokens", "/api/management/v1/principals",
            "/api/management/v1/principals/datahub_ingest/reset",
            "/api/catalog/v1/oauth/tokens"])
        self.assertTrue(all(r[1] == "POLARIS" for r in FakePolarisApi.requests))
        self.assertEqual(FakePolarisApi.requests[1][2], "Bearer token-rootid")
        for secret in ("rootsecret", "ingestsecret"):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_a_principal_already_there_is_kept_and_reset(self):
        FakePolarisApi.existing = {"datahub_ingest"}
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok   principal datahub_ingest already there", result.stdout)
        self.assertIn("ok   a token as datahub_ingest", result.stdout)

    def test_a_wrong_root_credential_fails_first(self):
        result = self.run_script("rootid\nwrong\n")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.splitlines(), ["FAIL a token as root: HTTP 401"])

    def test_a_refused_reset_fails_with_polariss_message(self):
        FakePolarisApi.reset_status = 403
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.splitlines()[-1],
                         "FAIL resetting datahub_ingest's credentials: HTTP 403, refused")


class FakePolaris:
    """Answers the Polaris step's Secret check, helm and the rollout wait; records every call."""

    def __init__(self, secrets_found=True, helm=(0, ""), rollout=(0, "")):
        self.secrets_found, self.helm, self.rollout = secrets_found, helm, rollout
        self.calls, self.values = [], []

    def __call__(self, argv, stdin=None):
        self.calls.append(argv)
        if argv[0] == "helm":
            files = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
            with open(files[-1], encoding="utf-8") as f:
                self.values.append(json.load(f))
            return self.helm
        if "secret" in argv:
            return (0, "") if self.secrets_found else (1, "NotFound")
        if "rollout" in argv:
            return self.rollout
        raise AssertionError(f"unexpected call {argv}")


class JdbcProperties(unittest.TestCase):
    """pgjdbc's client timeouts, read from values-lakehouse.yaml without a YAML parser
    (ADR-047 §8); charts/test_polaris.py checks the reading against PyYAML's."""

    def test_the_timeouts_polaris_passes_to_every_connection(self):
        self.assertEqual(release.polaris_jdbc_properties(), {
            "connectTimeout": "2", "loginTimeout": "3", "socketTimeout": "10",
            "tcpKeepAlive": "true"})

    def test_only_the_driver_properties_of_advanced_config_are_read(self):
        text = ('advancedConfig:\n'
                '  quarkus.log.file.enabled: "true"\n'
                '  # quarkus.datasource.jdbc.additional-jdbc-properties.commented: "1"\n'
                '  quarkus.datasource.jdbc.additional-jdbc-properties.socketTimeout: "7"\n')
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
            f.write(text)
            f.flush()
            self.assertEqual(release.polaris_jdbc_properties(f.name), {"socketTimeout": "7"})


class FakeSidecar:
    """Answers the sidecar step's helm call; records every call and the generated values."""

    def __init__(self, helm=(0, "")):
        self.helm, self.calls, self.values = helm, [], []

    def __call__(self, argv, stdin=None):
        self.calls.append(argv)
        if argv[0] == "helm":
            files = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
            with open(files[-1], encoding="utf-8") as f:
                self.values.append(json.load(f))
            return self.helm
        raise AssertionError(f"unexpected call {argv}")


class LogSidecar(unittest.TestCase):
    """ADR-082, ADR-081: step 1 installs the sidecar's ConfigMap before Polaris."""

    ENV = {**BOOTSTRAP_ENV, "POLARIS_LOG_OUTPUT_HOST": "fluent.example.invalid",
           "POLARIS_LOG_OUTPUT_PORT": "24225"}

    def step(self, calls, env=None, dry_run=False):
        return release.log_sidecar(env or self.ENV, calls, dry_run=dry_run)

    def test_installs_the_chart_in_polaris_namespace_with_the_env_values(self):
        calls = FakeSidecar()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual(len(calls.calls), 1)
        helm = calls.calls[0]
        self.assertEqual(helm[:3], ["helm", "--kube-context", "kind-lakehouse-gate"])
        self.assertEqual(helm[helm.index("-n") + 1], "catalog-polaris")
        self.assertEqual(helm[helm.index("upgrade"):helm.index("upgrade") + 4],
                         ["upgrade", "--install", "polaris-log-sidecar", release.SIDECAR_CHART])
        self.assertEqual(calls.values,
                         [{"output": {"host": "fluent.example.invalid", "port": 24225}}])
        self.assertIsInstance(calls.values[0]["output"]["port"], int)
        files = [helm[i + 1] for i, a in enumerate(helm) if a == "-f"]
        self.assertFalse(os.path.exists(files[0]), "the generated values file is left behind")
        self.assertNotIn("--wait", helm)
        self.assertIn("fluent.example.invalid:24225", "\n".join(notes))

    def test_empty_variables_pass_no_values_and_the_note_says_so(self):
        calls = FakeSidecar()
        problems, notes = self.step(calls, BOOTSTRAP_ENV)
        self.assertEqual(problems, [])
        self.assertEqual(calls.values, [{}])
        text = "\n".join(notes)
        self.assertIn("POLARIS_LOG_OUTPUT_HOST is empty", text)
        self.assertIn("main-fluent-bit-standin", text)

    def test_a_port_without_a_host_is_named_with_the_placeholder(self):
        calls = FakeSidecar()
        _, notes = self.step(calls, {**BOOTSTRAP_ENV, "POLARIS_LOG_OUTPUT_PORT": "24225"})
        self.assertEqual(calls.values, [{"output": {"port": 24225}}])
        self.assertIn("port 24225", "\n".join(notes))

    def test_a_port_that_is_not_a_port_stops_before_helm(self):
        for port in ("abc", "0", "70000", "\u0665\u0661\u0664\u0660"):
            with self.subTest(port=port):
                calls = FakeSidecar()
                problems, _ = self.step(calls, {**self.ENV, "POLARIS_LOG_OUTPUT_PORT": port})
                self.assertIn("POLARIS_LOG_OUTPUT_PORT", "\n".join(problems))
                self.assertEqual(calls.calls, [])

    def test_dry_run_is_server_side(self):
        calls = FakeSidecar()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        self.assertIn("--dry-run=server", calls.calls[0])
        self.assertIn("dry-run", "\n".join(notes))

    def test_a_failed_helm_stops_with_its_error(self):
        problems, _ = self.step(FakeSidecar(helm=(1, "invalid ConfigMap")))
        self.assertIn("invalid ConfigMap", "\n".join(problems))


class SidecarSteps(unittest.TestCase):
    def test_a_failed_sidecar_chart_stops_before_polaris_and_everything_after(self):
        step0, pg, endpoints = FakeKubectl(), FakeCalls(), FakeRun()
        boot, claim, calls = FakeBootstrap(), FakeClaim(), []

        def run(argv, stdin=None):
            calls.append(argv)
            if "crd" in argv or "cnpg-system" in argv:
                return step0(argv)
            if "endpoint-check" in " ".join(argv):
                return endpoints(argv, stdin)
            if "storageclass" in argv or "apply" in argv:
                return claim(argv, stdin)
            if "polaris-root" in argv or "run" in argv:
                return boot(argv, stdin)
            if release.SIDECAR_CHART in argv:
                return 1, "ConfigMap refused"
            return pg(argv)
        problems, _ = release.install({**BOOTSTRAP_ENV, **LOGS_ENV}, run, ["1.30"],
                                      dry_run=False)
        self.assertIn("ConfigMap refused", "\n".join(problems))
        self.assertFalse(any(release.POLARIS_CHART in c or release.BATCH_CHART in c
                             for c in calls))


class SidecarChecksum(unittest.TestCase):
    def annotation(self, env=BOOTSTRAP_ENV, **kwargs):
        return release.polaris_values(env)["podAnnotations"]["checksum/log-sidecar"] \
            if not kwargs else release.sidecar_checksum(env, **kwargs)

    def test_the_pod_annotation_is_a_sha256_of_the_sidecar_chart_and_its_values(self):
        first = self.annotation()
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(first, self.annotation())
        self.assertNotEqual(first, self.annotation({**BOOTSTRAP_ENV,
                                                    "POLARIS_LOG_OUTPUT_HOST": "other.invalid"}))
        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, "chart")
            shutil.copytree(release.SIDECAR_CHART, copy)
            base = self.annotation(directory=copy)
            self.assertEqual(base, first)
            with open(os.path.join(copy, "polaris-values.yaml"), "a") as f:
                f.write("# comment\n")
            self.assertEqual(self.annotation(directory=copy), base)
            template = os.path.join(copy, "templates", "configmap.yaml")
            with open(template, "a") as f:
                f.write("# changed\n")
            self.assertNotEqual(self.annotation(directory=copy), base)

    def test_the_minimal_environment_of_the_chart_tests_works(self):
        release.polaris_values({"POLARIS_DB_HOST": "", "POLARIS_OPA_URL": "http://opa:8181/x"})

    def test_the_jdbc_and_policy_keys_are_unchanged(self):
        keys = release.polaris_values(BOOTSTRAP_ENV)["advancedConfig"]
        self.assertEqual(sorted(keys), ["polaris.authorization.opa.policy-uri",
                                        "quarkus.datasource.jdbc.url"])


class EnvTemplate(unittest.TestCase):
    def test_the_output_variables_are_in_the_template_and_empty(self):
        path = os.path.join(release.ROOT, ".env.template")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        values = release.parse(text)
        for name in ("POLARIS_LOG_OUTPUT_HOST", "POLARIS_LOG_OUTPUT_PORT"):
            self.assertEqual(values.get(name), "")
        for line in text.splitlines():
            if line.startswith("POLARIS_LOG_OUTPUT_"):
                self.assertNotIn("://", line)


class Polaris(unittest.TestCase):
    def step(self, calls, env=BOOTSTRAP_ENV, dry_run=False):
        return release.polaris(env, calls, dry_run=dry_run)

    def test_installs_the_chart_with_the_three_values_files_then_waits_for_the_rollout(self):
        calls = FakePolaris()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual([c[0] for c in calls.calls], ["kubectl", "helm", "kubectl"])
        helm = calls.calls[1]
        self.assertEqual(helm[:3], ["helm", "--kube-context", "kind-lakehouse-gate"])
        self.assertEqual(helm[helm.index("-n") + 1], "catalog-polaris")
        self.assertEqual(helm[helm.index("upgrade"):helm.index("upgrade") + 4],
                         ["upgrade", "--install", "polaris", release.POLARIS_CHART])
        files = [helm[i + 1] for i, a in enumerate(helm) if a == "-f"]
        self.assertEqual(files[0], release.POLARIS_VALUES)
        self.assertEqual(files[1], release.SIDECAR_FRAGMENT)
        self.assertEqual(len(files), 3)
        self.assertEqual(calls.values, [release.polaris_values(BOOTSTRAP_ENV)])
        self.assertFalse(os.path.exists(files[2]), "the generated values file is left behind")
        rollout = calls.calls[2]
        self.assertEqual(rollout[rollout.index("rollout"):rollout.index("rollout") + 3],
                         ["rollout", "status", "deployment/polaris"])
        self.assertIn("polaris", "\n".join(notes))

    def test_every_call_names_the_context_and_the_namespace(self):
        calls = FakePolaris()
        self.step(calls)
        for argv in calls.calls:
            flag = "--kube-context" if argv[0] == "helm" else "--context"
            self.assertEqual(argv[1:3], [flag, "kind-lakehouse-gate"])
            self.assertEqual(argv[argv.index("-n") + 1], "catalog-polaris")

    def test_the_secrets_the_chart_reads_are_checked_first(self):
        calls = FakePolaris()
        self.step(calls)
        check = calls.calls[0]
        for name in ("polaris-db", "polaris-s3", "polaris-token-signing"):
            self.assertIn(name, check)

    def test_a_missing_secret_stops_before_helm_and_names_init_env(self):
        calls = FakePolaris(secrets_found=False)
        problems, _ = self.step(calls)
        self.assertIn("init_env.py --apply-secrets", "\n".join(problems))
        self.assertFalse(any(c[0] == "helm" for c in calls.calls))

    def test_dry_run_is_server_side_and_does_not_wait(self):
        calls = FakePolaris()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        self.assertIn("--dry-run=server", calls.calls[1])
        self.assertFalse(any("rollout" in c for c in calls.calls))
        self.assertIn("dry-run", "\n".join(notes))

    def test_a_failed_helm_stops_with_its_error(self):
        problems, _ = self.step(FakePolaris(helm=(1, "admission webhook denied")))
        self.assertIn("admission webhook denied", "\n".join(problems))

    def test_a_rollout_that_never_finishes_stops(self):
        problems, _ = self.step(FakePolaris(rollout=(1, "timed out waiting")))
        self.assertIn("timed out waiting", "\n".join(problems))


LOGS_ENV = {"POLARIS_LOGS_STORAGE_CLASS": "shared", "POLARIS_LOGS_SIZE": "1Gi"}


class FakeClaim:
    """Answers the claim step's storage class check and its apply; records every call."""

    def __init__(self, storage_class=(0, "storageclass.storage.k8s.io/shared"),
                 apply=(0, "persistentvolumeclaim/polaris-logs created")):
        self.storage_class, self.apply, self.calls, self.stdin = storage_class, apply, [], []

    def __call__(self, argv, stdin=None):
        self.calls.append(argv)
        self.stdin.append(stdin)
        if "storageclass" in argv:
            return self.storage_class
        if "apply" in argv:
            return self.apply
        raise AssertionError(f"unexpected call {argv}")


class LogsClaim(unittest.TestCase):
    """Step 1, before Polaris: the claim polaris-logs that Polaris and the batch share
    (ADR-036)."""

    def step(self, calls, env=None, dry_run=False):
        return release.logs_claim({**BOOTSTRAP_ENV, **LOGS_ENV, **(env or {})}, calls,
                                  dry_run=dry_run)

    def test_the_storage_class_is_checked_then_the_claim_applied(self):
        calls = FakeClaim()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual(calls.calls[0][calls.calls[0].index("get"):],
                         ["get", "storageclass", "shared", "-o", "name"])
        apply = calls.calls[1]
        self.assertEqual(apply[apply.index("apply"):], ["apply", "-f", "-"])
        self.assertIn("polaris-logs", notes[0])
        self.assertIn("shared", notes[0])

    def test_the_claim_is_read_write_many_with_the_class_and_size_from_env(self):
        calls = FakeClaim()
        self.step(calls)
        claim = json.loads(calls.stdin[1])
        self.assertEqual((claim["kind"], claim["metadata"]["name"]),
                         ("PersistentVolumeClaim", release.POLARIS_LOGS))
        self.assertEqual(claim["metadata"]["namespace"], "catalog-polaris")
        self.assertEqual(claim["spec"]["accessModes"], ["ReadWriteMany"])
        self.assertEqual(claim["spec"]["storageClassName"], "shared")
        self.assertEqual(claim["spec"]["resources"], {"requests": {"storage": "1Gi"}})

    def test_the_claim_belongs_to_no_helm_release(self):
        calls = FakeClaim()
        self.step(calls)
        claim = json.loads(calls.stdin[1])
        self.assertFalse(any(k.startswith("meta.helm.sh")
                             for k in claim["metadata"].get("annotations", {})))
        self.assertFalse(any(c[0] == "helm" for c in calls.calls))

    def test_every_call_names_the_context_and_the_namespace(self):
        calls = FakeClaim()
        self.step(calls)
        for argv in calls.calls:
            self.assertEqual(argv[1:3], ["--context", "kind-lakehouse-gate"])
            self.assertEqual(argv[argv.index("-n") + 1], "catalog-polaris")

    def test_an_empty_class_or_size_stops_before_any_call(self):
        for var in LOGS_ENV:
            calls = FakeClaim()
            problems, _ = self.step(calls, env={var: ""})
            self.assertIn(var, "\n".join(problems))
            self.assertIn("ADR-036", "\n".join(problems))
            self.assertEqual(calls.calls, [])

    def test_a_missing_storage_class_stops_before_the_claim(self):
        calls = FakeClaim(storage_class=(1, 'storageclasses.storage.k8s.io "shared" not found'))
        problems, _ = self.step(calls)
        self.assertIn("shared", "\n".join(problems))
        self.assertIn("ReadWriteMany", "\n".join(problems))
        self.assertEqual(len(calls.calls), 1)

    def test_dry_run_applies_server_side(self):
        calls = FakeClaim()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        self.assertIn("--dry-run=server", calls.calls[1])
        self.assertIn("dry-run", "\n".join(notes))

    def test_a_refused_apply_stops_with_its_error(self):
        problems, _ = self.step(FakeClaim(apply=(1, "spec is immutable after creation")))
        self.assertIn("spec is immutable", "\n".join(problems))


CONSOLE_ENV = {"POLARIS_CONSOLE_ENABLED": "true",
               "POLARIS_CONSOLE_ORIGIN": "http://console.example:8080",
               "POLARIS_CONSOLE_API_URL": "http://polaris.example:8181"}


class FakeConsole(FakePolaris):
    """Answers the console step's helm and rollout wait; it checks no Secret."""


class Console(unittest.TestCase):
    def step(self, calls, env=None, dry_run=False):
        return release.console({**BOOTSTRAP_ENV, **CONSOLE_ENV} if env is None else env,
                               calls, dry_run=dry_run)

    def test_off_unless_enabled_and_then_nothing_is_called(self):
        for value in (None, "false", ""):
            with self.subTest(value=value):
                env = dict(BOOTSTRAP_ENV)
                if value is not None:
                    env["POLARIS_CONSOLE_ENABLED"] = value
                calls = FakeConsole()
                problems, notes = self.step(calls, env)
                self.assertEqual((problems, calls.calls), ([], []))
                self.assertIn("Step 2: the console is not enabled", "\n".join(notes))

    def test_installs_the_chart_with_the_three_values_files_then_waits_for_the_rollout(self):
        calls = FakeConsole()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual([c[0] for c in calls.calls], ["helm", "kubectl"])
        helm = calls.calls[0]
        self.assertEqual(helm[:3], ["helm", "--kube-context", "kind-lakehouse-gate"])
        self.assertEqual(helm[helm.index("-n") + 1], "catalog-polaris")
        self.assertEqual(helm[helm.index("upgrade"):helm.index("upgrade") + 4],
                         ["upgrade", "--install", "polaris-console", release.CONSOLE_CHART])
        files = [helm[i + 1] for i, a in enumerate(helm) if a == "-f"]
        self.assertEqual(files[0], release.CONSOLE_VALUES)
        self.assertFalse(os.path.exists(files[1]), "the generated values file is left behind")
        rollout = calls.calls[1]
        self.assertEqual(rollout[1:3], ["--context", "kind-lakehouse-gate"])
        self.assertEqual(rollout[rollout.index("-n") + 1], "catalog-polaris")
        self.assertEqual(rollout[rollout.index("rollout"):rollout.index("rollout") + 3],
                         ["rollout", "status", "deployment/polaris-console"])
        self.assertIn("Step 2: deployment/polaris-console", "\n".join(notes))

    def test_the_browser_reaches_polaris_at_the_api_url_from_env(self):
        calls = FakeConsole()
        self.step(calls)
        self.assertEqual(calls.values, [{"config": {"api": {
            "polarisApiUrl": "http://polaris.example:8181",
            "oauthTokenUrl": "http://polaris.example:8181/api/catalog/v1/oauth/tokens",
            "polarisRealm": "POLARIS"}}}])

    def test_enabled_without_its_origin_or_api_url_stops_before_helm(self):
        for var in ("POLARIS_CONSOLE_ORIGIN", "POLARIS_CONSOLE_API_URL"):
            with self.subTest(var=var):
                calls = FakeConsole()
                problems, _ = self.step(calls, {**BOOTSTRAP_ENV, **CONSOLE_ENV, var: ""})
                self.assertIn(var, "\n".join(problems))
                self.assertEqual(calls.calls, [])

    def test_anything_but_true_or_false_is_refused(self):
        calls = FakeConsole()
        problems, _ = self.step(calls, {**BOOTSTRAP_ENV, **CONSOLE_ENV,
                                        "POLARIS_CONSOLE_ENABLED": "yes"})
        self.assertIn("POLARIS_CONSOLE_ENABLED", "\n".join(problems))
        self.assertEqual(calls.calls, [])

    def test_dry_run_is_server_side_and_does_not_wait(self):
        calls = FakeConsole()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        self.assertIn("--dry-run=server", calls.calls[0])
        self.assertEqual(len(calls.calls), 1)
        self.assertIn("dry-run", "\n".join(notes))

    def test_a_failed_helm_or_rollout_is_a_problem_with_its_error(self):
        problems, _ = self.step(FakeConsole(helm=(1, "schema invalid")))
        self.assertIn("schema invalid", "\n".join(problems))
        problems, _ = self.step(FakeConsole(rollout=(1, "timed out waiting")))
        self.assertIn("timed out waiting", "\n".join(problems))


class FakeBatch:
    """Answers the batch step's helm and its read of the CronJob's image; records every call."""

    def __init__(self, helm=(0, ""), image=None):
        self.helm, self.calls, self.values = helm, [], []
        self.image = release.batch_image() if image is None else image

    def __call__(self, argv, stdin=None):
        self.calls.append(argv)
        if argv[0] == "helm":
            files = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
            with open(files[-1], encoding="utf-8") as f:
                self.values.append(json.load(f))
            return self.helm
        if "cronjob/polaris-log-batch" in argv:
            return (0, self.image) if self.image else (1, "NotFound")
        raise AssertionError(f"unexpected call {argv}")


class LogBatch(unittest.TestCase):
    """ADR-036, ADR-037: step 2 installs the batch's CronJob; a failure stops the install."""

    def step(self, calls, dry_run=False):
        return release.log_batch(BOOTSTRAP_ENV, calls, dry_run=dry_run)

    def test_installs_the_chart_with_the_image_built_from_this_checkout(self):
        calls = FakeBatch()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual([c[0] for c in calls.calls], ["helm", "kubectl"])
        helm = calls.calls[0]
        self.assertEqual(helm[:3], ["helm", "--kube-context", "kind-lakehouse-gate"])
        self.assertEqual(helm[helm.index("-n") + 1], "catalog-polaris")
        self.assertEqual(helm[helm.index("upgrade"):helm.index("upgrade") + 4],
                         ["upgrade", "--install", "polaris-log-batch", release.BATCH_CHART])
        self.assertEqual(calls.values, [release.batch_values(BOOTSTRAP_ENV)])
        files = [helm[i + 1] for i, a in enumerate(helm) if a == "-f"]
        self.assertFalse(os.path.exists(files[0]), "the generated values file is left behind")
        self.assertIn(f"Step 2: cronjob/polaris-log-batch in catalog-polaris runs "
                      f"{release.batch_image()}", "\n".join(notes))

    def test_the_cronjob_is_read_back_in_its_namespace(self):
        calls = FakeBatch()
        self.step(calls)
        read = calls.calls[1]
        self.assertEqual(read[1:3], ["--context", "kind-lakehouse-gate"])
        self.assertEqual(read[read.index("-n") + 1], "catalog-polaris")
        self.assertIn("cronjob/polaris-log-batch", read)

    def test_a_cronjob_with_another_image_stops(self):
        problems, _ = self.step(FakeBatch(image="lakehouse/polaris-log-batch:000000000000"))
        self.assertIn("000000000000", "\n".join(problems))

    def test_a_failed_helm_or_a_missing_cronjob_stops_with_its_error(self):
        problems, _ = self.step(FakeBatch(helm=(1, "schema invalid")))
        self.assertIn("schema invalid", "\n".join(problems))
        problems, _ = self.step(FakeBatch(image=""))
        self.assertIn("NotFound", "\n".join(problems))

    def test_dry_run_is_server_side_and_reads_nothing_back(self):
        calls = FakeBatch()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        self.assertIn("--dry-run=server", calls.calls[0])
        self.assertEqual(len(calls.calls), 1)
        self.assertIn("dry-run", "\n".join(notes))

    def test_a_registry_in_env_names_the_image_installed_and_read_back(self):
        env = {**BOOTSTRAP_ENV, "IMAGE_REGISTRY": MIRROR}
        image = release.batch_image(registry=MIRROR)
        calls = FakeBatch(image=image)
        problems, notes = release.log_batch(env, calls)
        self.assertEqual(problems, [])
        self.assertEqual(calls.values[0]["image"]["repository"], f"{MIRROR}/polaris-log-batch")
        self.assertIn(f"runs {image}", "\n".join(notes))
        problems, _ = release.log_batch(env, FakeBatch())
        self.assertIn(f"expected {image}", "\n".join(problems))


MIRROR = "mirror.example.invalid:5000/platform"


class BatchImage(unittest.TestCase):
    """ADR-042: the release pushes the batch's image to GHCR; IMAGE_REGISTRY names a mirror.
    ADR-074: under the repository's organization, kade-k8s-platform."""

    def test_the_default_is_the_github_container_registry(self):
        self.assertEqual(release.DEFAULT_IMAGE_REGISTRY, "ghcr.io/kade-k8s-platform/lakehouse")
        self.assertTrue(release.batch_image().startswith(
            "ghcr.io/kade-k8s-platform/lakehouse/polaris-log-batch:"))

    def test_the_charts_default_repository_is_the_releases(self):
        values = os.path.join(os.path.dirname(__file__), "..", "charts", "polaris-log-batch", "values.yaml")
        with open(values, encoding="utf-8") as f:
            repositories = [line.split(":", 1)[1].strip() for line in f
                            if line.startswith("  repository:")]
        self.assertEqual(repositories, [release.DEFAULT_IMAGE_REGISTRY + "/polaris-log-batch"])

    def test_an_empty_or_missing_registry_is_the_default(self):
        for env in ({}, {"IMAGE_REGISTRY": ""}):
            with self.subTest(env=env):
                self.assertEqual(release.image_registry(env), release.DEFAULT_IMAGE_REGISTRY)

    def test_a_registry_in_env_is_used_without_a_trailing_slash(self):
        self.assertEqual(release.image_registry({"IMAGE_REGISTRY": MIRROR}), MIRROR)
        self.assertEqual(release.image_registry({"IMAGE_REGISTRY": MIRROR + "/"}), MIRROR)

    def test_a_mirror_keeps_the_name_and_the_tag(self):
        default, mirrored = release.batch_image(), release.batch_image(registry=MIRROR)
        self.assertEqual(mirrored, f"{MIRROR}/polaris-log-batch:{default.rsplit(':', 1)[1]}")

    def test_the_values_split_the_image_at_its_tag(self):
        self.assertEqual(release.batch_values({"IMAGE_REGISTRY": MIRROR}),
                         {"image": {"repository": f"{MIRROR}/polaris-log-batch",
                                    "tag": release.batch_image().rsplit(":", 1)[1]}})


class Cors(unittest.TestCase):
    def test_polaris_allows_the_consoles_origin_only_while_it_is_enabled(self):
        enabled = release.polaris_values({**BOOTSTRAP_ENV, **CONSOLE_ENV})
        self.assertEqual(enabled["cors"], {"enabled": True,
                                           "allowedOrigins": ["http://console.example:8080"]})
        for env in (BOOTSTRAP_ENV, {**BOOTSTRAP_ENV, **CONSOLE_ENV,
                                    "POLARIS_CONSOLE_ENABLED": "false"},
                    {**BOOTSTRAP_ENV, **CONSOLE_ENV, "POLARIS_CONSOLE_ORIGIN": ""}):
            with self.subTest(env=env.get("POLARIS_CONSOLE_ENABLED")):
                self.assertNotIn("cors", release.polaris_values(env))


class LoadEnv(unittest.TestCase):
    def test_env_file_then_environment_wins(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".env"), "w", encoding="utf-8") as f:
                f.write("KUBE_CONTEXT=from-file\nNS_CNPG_SYSTEM=cnpg-system\n")
            env = release.load_env(d, {"KUBE_CONTEXT": "from-environ"})
        self.assertEqual(env["KUBE_CONTEXT"], "from-environ")
        self.assertEqual(env["NS_CNPG_SYSTEM"], "cnpg-system")

    def test_no_env_file_uses_the_environment(self):
        with tempfile.TemporaryDirectory() as d:
            env = release.load_env(d, dict(ENV))
        self.assertEqual(env, ENV)


class Main(unittest.TestCase):
    def test_stops_with_exit_1_when_kube_context_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            result = subprocess.run(
                [sys.executable, release.__file__], cwd=d, capture_output=True, text=True,
                env={"PATH": os.environ.get("PATH", ""), "KUBE_CONTEXT": ""}, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("KUBE_CONTEXT", result.stdout + result.stderr)


    def test_batch_image_prints_the_image_the_release_pushes_whatever_env_says(self):
        result = subprocess.run([sys.executable, release.__file__, "batch-image"],
                                capture_output=True, text=True, check=False,
                                env={"PATH": os.environ.get("PATH", ""),
                                     "IMAGE_REGISTRY": MIRROR})
        self.assertEqual((result.returncode, result.stdout), (0, release.batch_image() + "\n"))

    def test_an_unknown_option_exits_2(self):
        result = subprocess.run([sys.executable, release.__file__, "--upgrade"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
