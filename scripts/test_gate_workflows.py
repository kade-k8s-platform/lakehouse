"""Every gate workflow deletes its own gate branch when its run ends (ADR-049). Run:
python3 -m unittest discover -s scripts

A gate workflow is any workflow that a push to `gate/**` triggers (ADR-049 §1). The test keeps
no list of them: it reads every file in .github/workflows/. The scripts/ tests run on the
runner's own Python, without PyYAML, so `read_workflow` reads the subset of YAML these files
use: block mappings, `- ` sequences, flow lists, `{}`, and `|` and `>` blocks.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
GATES_DIR = os.path.join(ROOT, "gates")
DELETE = "delete-gate-branch.yml"
DELETE_USES = f"./.github/workflows/{DELETE}"
GUARD = "always() && startsWith(github.ref, 'refs/heads/gate/')"
MAIN_STATUS = "main-status.yml"
MAIN_STATUS_USES = f"./.github/workflows/{MAIN_STATUS}"
MAIN_GUARD = ("always() && github.ref == 'refs/heads/main' && "
              "(github.event_name == 'schedule' || github.event_name == 'workflow_run')")


def _scalar(text):
    text = text.strip()
    if text == "{}":
        return {}
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(x) for x in text[1:-1].split(",") if x.strip()]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _lines(text):
    """(indent, content, block) for each line that is not blank or a comment; a `|` or `>`
    line carries its block's text, and the block's own lines are consumed."""
    out, raw = [], text.splitlines()
    i = 0
    while i < len(raw):
        line = raw[i]
        stripped = line.strip()
        i += 1
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        block = None
        style = re.search(r":\s+([|>])-?$", stripped)
        if style:
            stripped, body = stripped[:style.start() + 1], []
            while i < len(raw) and (not raw[i].strip()
                                    or len(raw[i]) - len(raw[i].lstrip(" ")) > indent):
                body.append(raw[i].rstrip())
                i += 1
            margin = min((len(x) - len(x.lstrip(" ")) for x in body if x), default=0)
            body = [x[margin:] for x in body]
            if style.group(1) == ">":
                body = [x.strip() for x in body]
            block = ("\n" if style.group(1) == "|" else " ").join(body).strip()
        out.append((indent, re.sub(r"\s+#.*$", "", stripped), block))
    return out


def _parse(lines, start, indent):
    """The node at `indent` from lines[start:]; returns (node, next index)."""
    if lines[start][1].startswith("- "):
        items, i = [], start
        while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
            rest = lines[i][1][2:]
            if ":" in rest:
                sub = [(indent + 2, rest, lines[i][2])] + lines[i + 1:]
                node, used = _parse(sub, 0, indent + 2)
            else:
                node, used = _scalar(rest), 1
            items.append(node)
            i += used
        return items, i
    mapping, i = {}, start
    while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- "):
        key, _, value = lines[i][1].partition(":")
        key, value = _scalar(key), value.strip()
        if lines[i][2] is not None:
            mapping[key], i = lines[i][2], i + 1
        elif value:
            mapping[key], i = _scalar(value), i + 1
        elif i + 1 < len(lines) and lines[i + 1][0] > indent:
            mapping[key], i = _parse(lines, i + 1, lines[i + 1][0])
        elif i + 1 < len(lines) and lines[i + 1][0] == indent and lines[i + 1][1].startswith("- "):
            mapping[key], i = _parse(lines, i + 1, indent)
        else:
            mapping[key], i = None, i + 1
    return mapping, i


def read_workflow(path):
    with open(path, encoding="utf-8") as f:
        lines = _lines(f.read())
    node, _ = _parse(lines, 0, lines[0][0])
    return node


def workflows():
    return {name: read_workflow(os.path.join(WORKFLOWS, name))
            for name in sorted(os.listdir(WORKFLOWS)) if name.endswith(".yml")}


def push_branches(workflow):
    on = workflow.get("on") or {}
    push = on.get("push") if isinstance(on, dict) else None
    return (push or {}).get("branches") or [] if isinstance(push, dict) else []


def gate_workflows():
    return {name: w for name, w in workflows().items()
            if any(str(b).startswith("gate/") for b in push_branches(w))}


class Reader(unittest.TestCase):
    def test_reads_mappings_lists_and_blocks(self):
        text = ("on:\n  push:\n    branches: [\"gate/x/**\"]  # comment\n"
                "jobs:\n  a:\n    needs: [b, c]\n    steps:\n      - name: one\n"
                "        run: |\n          echo 1\n          echo 2\n      - uses: x@v1\n")
        lines = _lines(text)
        node, _ = _parse(lines, 0, 0)
        self.assertEqual(node["on"]["push"]["branches"], ["gate/x/**"])
        self.assertEqual(node["jobs"]["a"]["needs"], ["b", "c"])
        self.assertEqual(node["jobs"]["a"]["steps"][0]["run"], "echo 1\necho 2")
        self.assertEqual(node["jobs"]["a"]["steps"][1], {"uses": "x@v1"})

    def test_reads_folded_blocks(self):
        lines = _lines("a:\n  if: >-\n    always()\n    && x\n  b: c\n")
        node, _ = _parse(lines, 0, 0)
        self.assertEqual(node, {"a": {"if": "always() && x", "b": "c"}})


class GateWorkflows(unittest.TestCase):
    def test_the_known_gate_workflows_are_found(self):
        # A reader that finds nothing would pass every check below.
        self.assertLessEqual({"dry-run.yml", "smoke.yml"}, set(gate_workflows()))

    def test_every_gate_workflow_ends_with_the_delete_job(self):
        for name, workflow in gate_workflows().items():
            with self.subTest(workflow=name):
                jobs = workflow["jobs"]
                deleting = [j for j, job in jobs.items() if job.get("uses") == DELETE_USES]
                self.assertEqual(len(deleting), 1, "one job calls delete-gate-branch.yml")
                job = jobs[deleting[0]]
                self.assertEqual(job.get("if"), GUARD)
                self.assertEqual(job.get("permissions"), {"contents": "write"})
                others = sorted(j for j in jobs if j != deleting[0])
                self.assertEqual(sorted(job.get("needs") or []), others,
                                 "it needs every other job, so it runs last")

    def test_no_other_gate_job_can_write(self):
        for name, workflow in gate_workflows().items():
            for job_name, job in workflow["jobs"].items():
                if job.get("uses") in (DELETE_USES, MAIN_STATUS_USES):
                    continue
                with self.subTest(workflow=name, job=job_name):
                    self.assertNotIn("write", (job.get("permissions") or {}).values())

    def test_a_workflow_calling_a_gate_workflow_grants_it_contents_write(self):
        # A called workflow's job gets at most what its caller grants; the release gate calls
        # dry-run.yml and smoke.yml, whose delete job asks for contents: write.
        gates = {f"./.github/workflows/{n}" for n in gate_workflows()}
        for name, workflow in workflows().items():
            for job_name, job in (workflow.get("jobs") or {}).items():
                if job.get("uses") in gates:
                    with self.subTest(workflow=name, job=job_name):
                        self.assertEqual((job.get("permissions") or {}).get("contents"),
                                         "write")


class MainStatus(unittest.TestCase):
    """main is re-tested after Claude's merges and weekly, and a red run on main is an issue
    (ADR-055)."""

    def setUp(self):
        self.static = read_workflow(os.path.join(WORKFLOWS, "static.yml"))

    def test_static_also_runs_after_auto_merge_and_weekly(self):
        on = self.static["on"]
        self.assertEqual(on["workflow_run"], {"workflows": ["Auto-merge"],
                                              "types": ["completed"]})
        self.assertEqual(len(on["schedule"]), 1)
        self.assertTrue(on["schedule"][0]["cron"].endswith(" * * 1"), "weekly, on Mondays")
        self.assertEqual(on["push"]["branches"], ["main", "claude/**"])

    def test_static_skips_a_head_of_main_that_already_passed(self):
        jobs = self.static["jobs"]
        (check,) = [n for n, j in jobs.items()
                    if "head_sha=" in "\n".join(s.get("run", "") for s in j.get("steps") or [])]
        self.assertEqual(jobs[check]["permissions"], {"actions": "read"})
        self.assertEqual(jobs["static"]["needs"], check)
        self.assertEqual(jobs["static"]["if"], f"needs.{check}.outputs.skip != 'true'")

    def test_static_and_smoke_report_on_main_last(self):
        for name in ("static.yml", "smoke.yml"):
            with self.subTest(workflow=name):
                jobs = read_workflow(os.path.join(WORKFLOWS, name))["jobs"]
                calling = [j for j, job in jobs.items() if job.get("uses") == MAIN_STATUS_USES]
                self.assertEqual(len(calling), 1, "one job calls main-status.yml")
                job = jobs[calling[0]]
                self.assertEqual(job.get("if"), MAIN_GUARD)
                self.assertEqual(job.get("permissions"), {"issues": "write"})
                others = sorted(j for j, o in jobs.items()
                                if j != calling[0] and o.get("uses") != DELETE_USES)
                self.assertEqual(sorted(job.get("needs") or []), others)
                self.assertIn("result", job.get("with") or {})

    def test_a_workflow_calling_one_that_reports_grants_issues_write(self):
        reporting = {f"./.github/workflows/{n}" for n, w in workflows().items()
                     if any(j.get("uses") == MAIN_STATUS_USES
                            for j in (w.get("jobs") or {}).values())}
        self.assertLessEqual({"./.github/workflows/smoke.yml"}, reporting)
        for name, workflow in workflows().items():
            for job_name, job in (workflow.get("jobs") or {}).items():
                if job.get("uses") in reporting:
                    with self.subTest(workflow=name, job=job_name):
                        self.assertEqual((job.get("permissions") or {}).get("issues"), "write")

    def test_main_status_is_only_called_and_writes_issues_only(self):
        workflow = read_workflow(os.path.join(WORKFLOWS, MAIN_STATUS))
        self.assertEqual(list(workflow["on"]), ["workflow_call"])
        self.assertEqual(workflow["permissions"], {})
        (job,) = workflow["jobs"].values()
        self.assertEqual(job["permissions"], {"issues": "write"})
        self.assertFalse(any("actions/checkout" in str(s.get("uses", "")) for s in job["steps"]))

    def test_main_status_opens_comments_on_and_closes_one_issue(self):
        (job,) = read_workflow(os.path.join(WORKFLOWS, MAIN_STATUS))["jobs"].values()
        script = "\n".join(s.get("run", "") for s in job["steps"])
        self.assertIn('title="main is red: $WORKFLOW"', script)
        for command in ("gh issue create", "gh issue comment", "gh issue close"):
            self.assertIn(command, script)
        self.assertIn("failure)", script)
        self.assertIn("success)", script)


class SmokeFresh(unittest.TestCase):
    """smoke-fresh is smoke.yml on a push to gate/smoke-fresh/<task>, with `fresh` (ADR-050 §2)."""

    def setUp(self):
        self.workflow = read_workflow(os.path.join(WORKFLOWS, "smoke.yml"))

    def test_a_push_to_either_smoke_branch_runs_it(self):
        self.assertLessEqual({"gate/smoke/**", "gate/smoke-fresh/**"},
                             set(push_branches(self.workflow)))

    def test_fresh_comes_from_the_input_or_the_ref(self):
        steps = self.workflow["jobs"]["smoke"]["steps"]
        (install,) = [s for s in steps if "gate.py install" in s.get("run", "")]
        self.assertEqual(install["env"]["FRESH"], "${{ inputs.fresh || "
                         "startsWith(github.ref, 'refs/heads/gate/smoke-fresh/') }}")


class CheckMigration(unittest.TestCase):
    """Smoke reads the seed back right after the install, before any other step (ADR-051 §3)."""

    def test_the_step_comes_right_after_the_install(self):
        steps = read_workflow(os.path.join(WORKFLOWS, "smoke.yml"))["jobs"]["smoke"]["steps"]
        runs = [s.get("run", "") for s in steps]
        install = next(i for i, r in enumerate(runs) if "gate.py install" in r)
        check = next(i for i, r in enumerate(runs) if "gate.py check-migration" in r)
        self.assertEqual(check, install + 1)

    def test_only_smoke_runs_it(self):
        for name, workflow in workflows().items():
            if name != "smoke.yml":
                for job in (workflow.get("jobs") or {}).values():
                    for step in job.get("steps") or []:
                        self.assertNotIn("check-migration", step.get("run", ""), name)


class RebuildReplica(unittest.TestCase):
    """Smoke rebuilds a replica after the install and check-migration, before check-polaris-pg
    (ADR-048 §7, ADR-051 §3)."""

    def test_the_step_sits_between_check_migration_and_check_polaris_pg(self):
        steps = read_workflow(os.path.join(WORKFLOWS, "smoke.yml"))["jobs"]["smoke"]["steps"]
        runs = [s.get("run", "") for s in steps]
        migration = next(i for i, r in enumerate(runs) if "gate.py check-migration" in r)
        rebuild = next(i for i, r in enumerate(runs) if "gate.py rebuild-replica" in r)
        check = next(i for i, r in enumerate(runs) if "gate.py check-polaris-pg" in r)
        self.assertEqual((rebuild, check), (migration + 1, migration + 2))

    def test_only_smoke_runs_it(self):
        for name, workflow in workflows().items():
            if name != "smoke.yml":
                for job in (workflow.get("jobs") or {}).values():
                    for step in job.get("steps") or []:
                        self.assertNotIn("rebuild-replica", step.get("run", ""), name)


class DatahubRecovery(unittest.TestCase):
    """check-datahub-recovery runs in a smoke job of its own, on its own kind cluster, after
    check-datahub, on every smoke run (ADR-075), over Datahub installed alone, fresh, after
    smoke's set-up without the console's and the batch's images (ADR-077)."""

    def setUp(self):
        self.jobs = read_workflow(os.path.join(WORKFLOWS, "smoke.yml"))["jobs"]
        self.smoke = self.jobs["smoke"]
        self.job = self.jobs.get("datahub-recovery") or {"steps": []}

    @staticmethod
    def _install(steps):
        return next((i for i, s in enumerate(steps) if "gate.py install" in s.get("run", "")),
                    len(steps))

    def test_it_runs_beside_smoke_on_a_runner_of_its_own_for_40_minutes(self):
        self.assertIn("datahub-recovery", self.jobs)
        for key in ("runs-on", "env", "permissions"):
            with self.subTest(key=key):
                self.assertEqual(self.job.get(key), self.smoke.get(key))
        self.assertEqual(self.job.get("permissions"), {"contents": "read"})
        self.assertEqual(self.job.get("timeout-minutes"), "40")
        self.assertNotIn("needs", self.job)

    def test_both_jobs_run_on_every_trigger(self):
        for name in ("smoke", "datahub-recovery"):
            with self.subTest(job=name):
                self.assertIn(name, self.jobs)
                self.assertNotIn("if", self.jobs[name])

    # ADR-077 §2: the job's steps, each found by what it runs.
    STEPS = ("actions/checkout@", "python3 gates/gate.py tools ", "python3 gates/gate.py up",
             "python3 gates/gate.py secrets", "python3 gates/gate.py standins",
             "python3 gates/gate.py install --datahub-only |",
             "python3 gates/gate.py check-datahub |",
             "python3 gates/gate.py check-datahub-recovery |", "kubectl ")

    def test_its_steps_are_the_set_up_then_datahub_alone_then_its_checks(self):
        steps = self.job["steps"]
        self.assertEqual(len(steps), len(self.STEPS))
        for step, want in zip(steps, self.STEPS):
            with self.subTest(step=want):
                self.assertIn(want, step.get("uses") or step.get("run", ""))

    def test_its_set_up_steps_are_smokes(self):
        set_up = self.job["steps"][:self._install(self.job["steps"])]
        self.assertEqual(len(set_up), 5)
        for step in set_up:
            with self.subTest(step=step.get("name") or step.get("uses")):
                self.assertIn(step, self.smoke["steps"])

    def test_no_console_or_batch_image_and_no_polaris_part(self):
        # Nothing in this job runs them (ADR-077 §2).
        for step in self.job["steps"]:
            for absent in ("console-image", "batch-image", "gate-policies", "seed",
                           "check-migration"):
                with self.subTest(step=step.get("name"), absent=absent):
                    self.assertNotIn(absent, step.get("run", ""))

    def test_it_installs_datahub_alone_fresh_on_every_trigger(self):
        steps = self.job["steps"]
        install = steps[self._install(steps)]
        self.assertIn("ADR-077", install["name"])
        self.assertNotIn("env", install)
        self.assertNotIn("if", install)
        self.assertEqual([line.strip() for line in install["run"].strip().splitlines()],
                         ["set -euo pipefail",
                          'python3 gates/gate.py install --datahub-only | tee -a '
                          '"$GITHUB_STEP_SUMMARY"'])

    def test_smoke_keeps_the_upgrade_the_seed_and_both_images(self):
        runs = "\n".join(s.get("run", "") for s in self.smoke["steps"])
        for want in ("python3 gates/gate.py install --fresh |", "python3 gates/gate.py install |",
                     "python3 gates/gate.py check-migration |", "console-image --load",
                     "python3 gates/gate.py batch-image"):
            with self.subTest(want=want):
                self.assertIn(want, runs)
        self.assertNotIn("--datahub-only", runs)

    def test_after_the_install_check_datahub_then_the_recovery_then_the_state_on_failure(self):
        steps = self.job["steps"]
        after = steps[self._install(steps) + 1:]
        self.assertEqual(len(after), 3)
        check, recovery, state = after
        self.assertIn("python3 gates/gate.py check-datahub |", check["run"])
        self.assertIn("python3 gates/gate.py check-datahub-recovery |", recovery["run"])
        self.assertEqual(state.get("if"), "failure()")
        for line in state["run"].splitlines():
            if " -n " in line:
                with self.subTest(line=line):
                    self.assertIn('-n "$NS_CATALOG_DATAHUB"', line)

    def test_only_this_job_runs_the_recovery(self):
        for name, workflow in workflows().items():
            for job_name, job in (workflow.get("jobs") or {}).items():
                if (name, job_name) == ("smoke.yml", "datahub-recovery"):
                    continue
                for step in job.get("steps") or []:
                    with self.subTest(workflow=name, job=job_name):
                        self.assertNotIn("check-datahub-recovery", step.get("run", ""))

    def test_smoke_still_runs_check_datahub(self):
        self.assertTrue(any("python3 gates/gate.py check-datahub |" in s.get("run", "")
                            for s in self.smoke["steps"]))

    def test_main_status_is_given_success_only_when_both_jobs_succeeded(self):
        self.assertEqual(self.jobs["main-status"]["with"]["result"],
                         "${{ (needs.smoke.result == 'success' && "
                         "needs.datahub-recovery.result == 'success') && 'success' || "
                         "(contains(needs.*.result, 'failure') && 'failure' || 'cancelled') }}")


class DatahubIngestion(unittest.TestCase):
    """check-datahub-ingestion runs in smoke's main job right after check-datahub, and the
    recorder's check reads its expectations (ADR-076 §6)."""

    def setUp(self):
        self.jobs = read_workflow(os.path.join(WORKFLOWS, "smoke.yml"))["jobs"]
        self.runs = [s.get("run", "") for s in self.jobs["smoke"]["steps"]]

    def index(self, text):
        return next(i for i, run in enumerate(self.runs) if text in run)

    def test_it_runs_right_after_check_datahub(self):
        step = self.jobs["smoke"]["steps"][self.index("python3 gates/gate.py check-datahub |") + 1]
        self.assertIn("ADR-076", step["name"])
        self.assertEqual([line.strip() for line in step["run"].strip().splitlines()],
                         ["set -euo pipefail",
                          'python3 gates/gate.py check-datahub-ingestion | tee -a '
                          '"$GITHUB_STEP_SUMMARY"'])

    def test_check_traffic_reads_its_expectations(self):
        self.assertIn("gates/expectations/datahub-ingestion.json",
                      self.runs[self.index("python3 gates/gate.py check-traffic ")])
        self.assertLess(self.index("check-datahub-ingestion |"),
                        self.index("python3 gates/gate.py check-traffic "))

    def test_the_recovery_job_does_not_run_it(self):
        for step in self.jobs["datahub-recovery"]["steps"]:
            self.assertNotIn("check-datahub-ingestion", step.get("run", ""))


class Stress(unittest.TestCase):
    """stress.yml runs only when Kade starts it, never on a push or in the release gate
    (ADR-054 §1); it builds the gate as smoke does, in direct mode (ADR-053 §4)."""

    def setUp(self):
        self.workflow = read_workflow(os.path.join(WORKFLOWS, "stress.yml"))
        self.job = self.workflow["jobs"]["stress"]

    def test_its_only_trigger_is_workflow_dispatch_with_profiles_and_a_baseline_run(self):
        self.assertEqual(list(self.workflow["on"]), ["workflow_dispatch"])
        inputs = self.workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs), {"profiles", "baseline_run"})
        self.assertEqual(inputs["profiles"]["default"],
                         "commits queries-conditional queries-full mixed")
        self.assertEqual(inputs["baseline_run"]["default"], "")
        self.assertNotIn("stress.yml", gate_workflows())

    def test_no_workflow_calls_it(self):
        for name, workflow in workflows().items():
            for job in (workflow.get("jobs") or {}).values():
                self.assertNotEqual(job.get("uses"), "./.github/workflows/stress.yml", name)

    def test_each_job_gets_only_the_read_it_needs(self):
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.job["permissions"], {"contents": "read"})
        self.assertEqual(self.workflow["jobs"]["baseline"]["permissions"], {"actions": "read"})
        self.assertEqual(sorted(self.workflow["jobs"]), ["baseline", "stress"])

    def test_it_stops_after_60_minutes_and_never_keeps_credentials(self):
        self.assertEqual(self.job["timeout-minutes"], "60")
        (checkout,) = [s for s in self.job["steps"]
                       if str(s.get("uses", "")).startswith("actions/checkout@")]
        self.assertEqual(checkout["with"]["persist-credentials"], "false")

    def test_the_steps_build_the_gate_in_direct_mode_then_seed_ramp_hold_verify_report(self):
        runs = [s.get("run", "") for s in self.job["steps"]]
        order = ["gate.py up", "gate.py secrets --direct", "gate.py batch-image",
                 "gate.py standins --direct", "gate.py install --fresh",
                 "stress/driver.py seed", "stress/driver.py ramp", "stress/driver.py hold",
                 "stress/driver.py verify", "stress/driver.py report"]
        found = [next(i for i, r in enumerate(runs) if command in r) for command in order]
        self.assertEqual(found, sorted(found))
        self.assertFalse(any(re.search(r"gate\.py (secrets|standins)\s*($|\|)", r)
                             for r in runs), "both run in direct mode")

    def test_inputs_reach_the_scripts_through_the_environment_only(self):
        for job in self.workflow["jobs"].values():
            for step in job["steps"]:
                self.assertNotIn("${{ inputs.", step.get("run", ""))

    def test_the_summary_is_uploaded_whatever_the_outcome(self):
        (upload,) = [s for s in self.job["steps"]
                     if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
        self.assertEqual(upload["if"], "${{ !cancelled() }}")
        self.assertEqual(upload["with"]["name"], "stress")


class DeleteWorkflow(unittest.TestCase):
    def setUp(self):
        self.workflow = read_workflow(os.path.join(WORKFLOWS, DELETE))

    def test_it_is_only_called(self):
        self.assertEqual(list(self.workflow["on"]), ["workflow_call"])
        self.assertEqual(self.workflow["permissions"], {})

    def test_one_job_writes_contents_without_a_checkout(self):
        (job,) = self.workflow["jobs"].values()
        self.assertEqual(job["permissions"], {"contents": "write"})
        self.assertFalse(any("actions/checkout" in str(s.get("uses", "")) for s in job["steps"]))

    def test_it_deletes_only_its_own_commits_gate_branch_and_never_fails(self):
        (job,) = self.workflow["jobs"].values()
        script = "\n".join(s.get("run", "") for s in job["steps"])
        self.assertIn('refs/heads/gate/*)', script)
        self.assertIn('--force-with-lease="$REF:$SHA"', script)
        self.assertNotIn("exit 1", script)
        self.assertIn("::warning::", script)


if __name__ == "__main__":
    unittest.main()


class LogBatchGateIsGone(unittest.TestCase):
    """The old log-batch path's gate is removed (ADR-081, L7b)."""

    def test_the_two_workflows_are_gone(self):
        for name in ("log-batch.yml", "create-fixtures.yml"):
            with self.subTest(workflow=name):
                self.assertFalse(os.path.exists(os.path.join(WORKFLOWS, name)), name)
        self.assertNotIn("log-batch.yml", gate_workflows())

    def test_the_gate_modules_and_their_tests_are_gone(self):
        for name in ("log_batch_gate.py", "fixtures.py",
                     "test_log_batch_gate.py", "test_fixtures.py"):
            with self.subTest(file=name):
                self.assertFalse(os.path.exists(os.path.join(GATES_DIR, name)), name)

    def test_no_workflow_names_a_deleted_file(self):
        names = ("create-fixtures.yml", "log-batch.yml", "gates/fixtures.py",
                 "log_batch_gate", "gate/log-batch")
        for name in sorted(os.listdir(WORKFLOWS)):
            with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as f:
                text = f.read()
            for needle in names:
                with self.subTest(workflow=name, needle=needle):
                    self.assertNotIn(needle, text)

    def test_no_gate_module_imports_a_deleted_one(self):
        pattern = re.compile(r"^\s*(import|from)\s+(fixtures|log_batch_gate)\b", re.M)
        for name in sorted(os.listdir(GATES_DIR)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(GATES_DIR, name), encoding="utf-8") as f:
                text = f.read()
            with self.subTest(module=name):
                self.assertIsNone(pattern.search(text))
