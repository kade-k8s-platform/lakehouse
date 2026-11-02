"""The release flow's workflows keep their guarantees (ADR-080). Run:
python3 -m unittest discover -s scripts

Candidate (on main), Promote and the rc guard have never run on GitHub when they merge; these
tests pin what their comments promise: who can write, where they run, what they do not do.
"""

import os
import unittest

from test_gate_workflows import WORKFLOWS, read_workflow, workflows

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GUARD = "candidate-guard.yml"


def run_text(job):
    return "\n".join(s.get("run", "") for s in job.get("steps") or [])


def uses_text(job):
    return "\n".join(str(s.get("uses", "")) for s in job.get("steps") or [])


class CandidateGuard(unittest.TestCase):
    def setUp(self):
        self.workflow = read_workflow(os.path.join(WORKFLOWS, GUARD))
        (self.job,) = self.workflow["jobs"].values()

    def test_it_runs_on_a_push_to_rc_branches_only(self):
        self.assertEqual(list(self.workflow["on"]), ["push"])
        self.assertEqual(self.workflow["on"]["push"]["branches"], ["rc/**"])

    def test_it_can_only_delete_a_branch_and_checks_nothing_out(self):
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.job["permissions"], {"contents": "write"})
        self.assertNotIn("actions/checkout", uses_text(self.job))

    def test_a_deletion_is_ignored(self):
        self.assertEqual(self.job["if"], "${{ !github.event.deleted }}")

    def test_it_deletes_any_pushed_rc_branch_and_knows_no_candidate_branch(self):
        script = run_text(self.job)
        for part in ("== rc/*", "made only by the Candidate workflow", "git/refs/heads/$GITHUB_REF_NAME"):
            self.assertIn(part, script)
        self.assertNotIn("candidate/", script)
        self.assertNotIn("compare/main", script)

CANDIDATE = "candidate.yml"
GATE_CALLS = {"dry-run": "./.github/workflows/dry-run.yml",
              "smoke-upgrade": "./.github/workflows/smoke.yml",
              "smoke-fresh": "./.github/workflows/smoke.yml"}


class Candidate(unittest.TestCase):
    def setUp(self):
        self.workflow = read_workflow(os.path.join(WORKFLOWS, CANDIDATE))
        self.jobs = self.workflow["jobs"]

    def job_named(self, name):
        (key,) = [k for k, j in self.jobs.items() if j.get("name") == name]
        return self.jobs[key]

    def test_kade_starts_it_by_hand_and_it_shares_the_release_lock_with_promote(self):
        self.assertEqual(list(self.workflow["on"]), ["workflow_dispatch"])
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.workflow["concurrency"],
                         {"group": "release", "cancel-in-progress": "false"})

    def test_it_runs_on_main_only_before_anything_else_in_the_check(self):
        script = run_text(self.job_named("check the candidate"))
        self.assertIn('release_tag.py check-main "$GITHUB_REF"', script)
        for later in ("release_tag.py should-candidate", "release_tag.py checks-passed"):
            self.assertLess(script.index("check-main"), script.index(later))
        self.assertNotIn("candidate/", script)

    def test_the_gates_delete_jobs_skip_every_ref_but_a_gate_branch_and_never_main(self):
        for name in ("dry-run.yml", "smoke.yml", "delete-gate-branch.yml"):
            text = open(os.path.join(WORKFLOWS, name), encoding="utf-8").read()
            with self.subTest(workflow=name):
                self.assertIn("refs/heads/gate/", text)
        for name in ("dry-run.yml", "smoke.yml"):
            wf = read_workflow(os.path.join(WORKFLOWS, name))
            deletes = [j for j in wf["jobs"].values() if "delete-gate-branch" in str(j.get("uses", ""))]
            with self.subTest(workflow=name):
                self.assertEqual(len(deletes), 1)
                self.assertIn("startsWith(github.ref, 'refs/heads/gate/')", deletes[0]["if"])
                self.assertNotIn("refs/heads/main", deletes[0]["if"])

    def test_the_job_names_are_the_ones_the_check_read_leaves_out(self):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(WORKFLOWS), "..", "install"))
        from release_tag import CANDIDATE_JOBS, CANDIDATE_GATE_PREFIXES
        own = {j["name"] for j in self.jobs.values() if "uses" not in j}
        self.assertEqual(own - CANDIDATE_JOBS, set())
        for key, called in GATE_CALLS.items():
            self.assertTrue(self.jobs[key]["name"] + " / " in CANDIDATE_GATE_PREFIXES, key)

    def test_the_check_reads_something_to_release_and_the_commits_checks_before_any_gate(self):
        job = self.job_named("check the candidate")
        self.assertEqual(job["permissions"], {"contents": "read", "checks": "read"})
        script = run_text(job)
        for part in ("release_tag.py should-candidate", "release_tag.py checks-passed"):
            self.assertIn(part, script)
        self.assertNotIn("moved from", script, "the run keeps its commit; main may move on")

    def test_it_names_the_final_and_candidate_tags_and_refuses_a_tagged_commit(self):
        script = run_text(self.job_named("check the candidate"))
        for part in ("release_tag.py next", "release_tag.py rc ", "release_tag.py superseded",
                     "tag --points-at"):
            self.assertIn(part, script)

    def test_the_gates_are_the_called_dry_run_and_two_smokes_after_the_check(self):
        for key, uses in GATE_CALLS.items():
            with self.subTest(job=key):
                job = self.jobs[key]
                self.assertEqual(job["uses"], uses)
                self.assertEqual(job["needs"], "check")
                self.assertEqual(job["permissions"]["contents"], "write")
                self.assertEqual(job["permissions"].get("issues"),
                                 None if key == "dry-run" else "write")
        self.assertEqual(self.jobs["smoke-upgrade"]["with"], {"fresh": "false"})
        self.assertEqual(self.jobs["smoke-fresh"]["with"], {"fresh": "true"})

    def test_only_the_gates_and_tag_can_write(self):
        writers = {k for k, j in self.jobs.items()
                   if "write" in (j.get("permissions") or {}).values()}
        self.assertEqual(writers, set(GATE_CALLS) | {"tag"})

    def test_the_tag_job_runs_none_of_the_repositorys_code(self):
        tag = self.job_named("rc tag and prerelease")
        self.assertEqual(tag["permissions"], {"contents": "write"})
        self.assertNotIn("actions/checkout", uses_text(tag))
        self.assertNotIn("python3", run_text(tag))

    def test_the_tag_job_needs_the_check_and_the_notes(self):
        tag = self.job_named("rc tag and prerelease")
        keys = {k for k, j in self.jobs.items() if j["name"] in ("check the candidate",
                "notes of the candidate")}
        self.assertEqual(set(tag["needs"]), keys)

    def test_the_tag_job_tags_the_runs_commit_without_a_stamp(self):
        script = run_text(self.job_named("rc tag and prerelease"))
        for part in ('--target "$GITHUB_SHA"', "--prerelease", 'refs/heads/rc/$RC',
                     "$SUPERSEDED"):
            self.assertIn(part, script)
        for part in ("git/commits", "git/trees", "git/blobs"):
            self.assertNotIn(part, script)
    def test_there_is_no_cleanup_job_any_more(self):
        self.assertNotIn("cleanup", self.jobs)
        self.assertNotIn("delete the candidate branch", {j.get("name") for j in self.jobs.values()})


class BatchImageIsGone(unittest.TestCase):
    """ADR-083: the log batch's chart, image and tests are deleted, and the release builds no
    image."""

    GONE = ("polaris-log-batch", "batch-image", "BATCH_IMAGE", "IMAGE_REGISTRY")

    def test_the_chart_the_image_and_their_tests_are_deleted(self):
        for path in (("charts", "polaris-log-batch"), ("images", "polaris-log-batch"),
                     ("charts", "test_polaris_log_batch.py")):
            with self.subTest(path=path):
                self.assertFalse(os.path.exists(os.path.join(ROOT, *path)))

    def test_no_workflow_template_or_runbook_names_it(self):
        paths = [os.path.join(WORKFLOWS, n) for n in sorted(os.listdir(WORKFLOWS))
                 if n.endswith(".yml")]
        paths += [os.path.join(ROOT, ".env.template"),
                  os.path.join(ROOT, "docs", "runbooks", "stress-capacity.md")]
        for path in paths:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for word in self.GONE:
                with self.subTest(path=os.path.basename(path), word=word):
                    self.assertNotIn(word, text)

    def test_candidate_has_no_image_job_and_no_job_writes_packages(self):
        jobs = read_workflow(os.path.join(WORKFLOWS, CANDIDATE))["jobs"]
        self.assertEqual(set(jobs), {"check", "dry-run", "smoke-upgrade", "smoke-fresh",
                                     "notes", "tag"})
        for key, job in jobs.items():
            with self.subTest(job=key):
                self.assertNotIn("packages", job.get("permissions") or {})
                for step in job.get("steps") or []:
                    self.assertNotIn("docker", step.get("run", ""))
                    self.assertNotIn("ghcr.io", step.get("run", ""))

    def test_the_tag_job_needs_the_check_and_the_notes_and_swaps_nothing(self):
        tag = read_workflow(os.path.join(WORKFLOWS, CANDIDATE))["jobs"]["tag"]
        self.assertEqual(set(tag["needs"]), {"check", "notes"})
        script = run_text(tag)
        for word in ("IMAGE", "DIGEST", "grep -cF", "sed -i"):
            self.assertNotIn(word, script)

    def test_promote_names_neither_a_batch_nor_a_digest_and_writes_no_packages(self):
        workflow = read_workflow(os.path.join(WORKFLOWS, "promote.yml"))
        with open(os.path.join(WORKFLOWS, "promote.yml"), encoding="utf-8") as f:
            text = f.read().lower()
        self.assertNotIn("batch", text)
        self.assertNotIn("digest", text)
        for job in workflow["jobs"].values():
            self.assertNotIn("packages", job.get("permissions") or {})


class Promote(unittest.TestCase):
    def setUp(self):
        self.workflow = read_workflow(os.path.join(WORKFLOWS, "promote.yml"))
        self.jobs = self.workflow["jobs"]

    def test_kade_starts_it_by_hand_under_the_release_lock(self):
        self.assertEqual(list(self.workflow["on"]), ["workflow_dispatch"])
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.workflow["concurrency"],
                         {"group": "release", "cancel-in-progress": "false"})

    def test_it_runs_on_an_rc_branch_only(self):
        self.assertIn("refs/heads/rc/platform-*-rc.*)", run_text(self.jobs["check"]))

    def test_only_the_tag_job_writes_and_it_runs_none_of_the_repositorys_code(self):
        self.assertEqual({k for k, j in self.jobs.items()
                          if "write" in (j.get("permissions") or {}).values()}, {"tag"})
        self.assertEqual(self.jobs["tag"]["permissions"], {"contents": "write"})
        self.assertEqual(self.jobs["tag"]["needs"], "check")
        self.assertNotIn("actions/checkout", uses_text(self.jobs["tag"]))
        self.assertNotIn("python3", run_text(self.jobs["tag"]))

    def test_it_builds_nothing_and_reads_no_packages(self):
        text = run_text(self.jobs["check"]) + run_text(self.jobs["tag"])
        for part in ("docker", "packages", "buildx"):
            self.assertNotIn(part, text)
        self.assertFalse(any("packages" in (j.get("permissions") or {})
                             for j in self.jobs.values()))

    def test_the_check_refuses_unless_the_branch_is_at_its_tag_and_the_prerelease_exists(self):
        script = run_text(self.jobs["check"])
        for part in ("release_tag.py rc-branch", "release_tag.py check-promote",
                     'refs/tags/$rc^{commit}', "isPrerelease", "release_tag.py released"):
            self.assertIn(part, script)

    def test_the_tag_job_reads_the_branch_again_then_tags_the_same_commit_with_the_rcs_notes(self):
        script = run_text(self.jobs["tag"])
        self.assertLess(script.index("moved from"), script.index("gh release create"))
        for part in ('--target "$COMMIT"', "--latest", "$RELEASED", "base64 -d"):
            self.assertIn(part, script)


class RetiredFlow(unittest.TestCase):
    def test_the_rc_gate_and_release_workflows_are_gone(self):
        names = set(workflows())
        self.assertFalse(names & {"release.yml", "release-gate.yml", "auto-candidate.yml"})
        self.assertLessEqual({"candidate.yml", "promote.yml", GUARD}, names)

    def test_no_workflow_runs_on_a_pull_request_into_rc(self):
        for name, workflow in workflows().items():
            on = workflow.get("on") or {}
            pr = on.get("pull_request") if isinstance(on, dict) else None
            with self.subTest(workflow=name):
                self.assertNotIn("rc", (pr or {}).get("branches") or [])
                self.assertNotIn("rc", (pr or {}).get("branches-ignore") or [])


if __name__ == "__main__":
    unittest.main()
