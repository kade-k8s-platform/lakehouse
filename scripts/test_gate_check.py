"""Tests for gate_check.py. Run: python3 -m unittest discover -s scripts"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gate_check as gate  # noqa: E402

DRY_RUN = ".github/workflows/dry-run.yml"
SMOKE = ".github/workflows/smoke.yml"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def dry_run(sha, branch="gate/dry-run/task", conclusion="success", path=DRY_RUN,
            created="2026-10-10T12:00:00Z", run_id=1):
    return {"id": run_id, "head_sha": sha, "head_branch": branch, "path": path,
            "conclusion": conclusion, "status": "completed", "created_at": created,
            "html_url": f"https://github.com/o/r/actions/runs/{run_id}"}


def smoke(sha, branch="gate/smoke/task", run_id=101, **kw):
    return dry_run(sha, branch=branch, path=SMOKE, run_id=run_id, **kw)


class FakeGit:
    """Ancestry and changed files as a dict: sha -> files changed from sha to the head."""

    def __init__(self, since=None):
        self.since = since or {}

    def is_ancestor(self, sha):
        return sha in self.since

    def changed_since(self, sha):
        return self.since[sha]


class TaskBranch(unittest.TestCase):
    def test_claude_branch_maps_to_the_two_gate_branches(self):
        self.assertEqual(gate.gate_branches("claude/p1-gate-check"),
                         ["gate/dry-run/p1-gate-check", "gate/smoke/p1-gate-check"])

    def test_other_prefixes_drop_the_first_segment(self):
        self.assertEqual(gate.gate_branches("kade/x"), ["gate/dry-run/x", "gate/smoke/x"])
        self.assertEqual(gate.gate_branches("kade/a/b"),
                         ["gate/dry-run/a/b", "gate/smoke/a/b"])

    def test_a_branch_without_a_slash_is_its_own_task(self):
        self.assertEqual(gate.gate_branches("feature"),
                         ["gate/dry-run/feature", "gate/smoke/feature"])


class PlatformFiles(unittest.TestCase):
    def test_platform_paths(self):
        for path in ("charts/polaris/values.yaml", "images/console/Dockerfile",
                     "install/install.sh", "gates/kind.yaml", DRY_RUN,
                     ".github/workflows/smoke.yml"):
            with self.subTest(path=path):
                self.assertTrue(gate.is_platform(path))

    def test_everything_else_is_not(self):
        for path in ("README.md", "docs/adr/019-x.md", "scripts/gate_check.py",
                     ".github/workflows/static.yml", "chartsx/a", "charts.md"):
            with self.subTest(path=path):
                self.assertFalse(gate.is_platform(path))


class NoLogBatch(unittest.TestCase):
    """ADR-081: the log-batch gate lapsed with the batch's removal."""

    def test_the_log_batch_names_are_gone(self):
        for name in ("LOG_BATCH_WORKFLOW", "BATCH_DIRS", "SPEC_FILES", "LOG_BATCH_GATES",
                     "is_log_batch"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(gate, name))
        self.assertEqual([g[0] for g in gate.GATES], ["dry-run", "smoke"])

    def test_the_rule_file_has_no_log_batch_line(self):
        with open(os.path.join(ROOT, ".claude", "rules", "gates.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("log-batch", text)
        self.assertNotIn("BATCH_DIRS", text)
        self.assertNotIn("docs/contracts/**", text.split("---")[1])

    def test_the_workflow_comments_name_two_gate_branches(self):
        with open(os.path.join(ROOT, ".github", "workflows", "gate-check.yml"),
                  encoding="utf-8") as f:
            self.assertNotIn("gate/log-batch", f.read())


class Decide(unittest.TestCase):
    def decide(self, changed, runs=(), git=None, branch="claude/task"):
        return gate.decide(branch, changed, list(runs), git or FakeGit())

    def test_a_gate_branch_as_head_fails(self):
        ok, lines = self.decide(["README.md"], branch="gate/dry-run/task")
        self.assertFalse(ok)
        self.assertIn("gate/", lines[0])

    def test_no_platform_file_passes_without_a_run(self):
        ok, lines = self.decide(["README.md", "scripts/x.py", "docs/contractsx/a.md"])
        self.assertTrue(ok)
        self.assertIn("no gate run needed", lines[0])

    def test_platform_change_without_a_dry_run_fails(self):
        ok, lines = self.decide(["charts/a/values.yaml"])
        self.assertFalse(ok)
        self.assertIn("gate/dry-run/task", "\n".join(lines))

    def test_dry_run_and_smoke_on_the_head_pass_and_name_commits_and_runs(self):
        git = FakeGit({"aaa": []})
        ok, lines = self.decide(["charts/a/values.yaml"],
                                [dry_run("aaa", run_id=7), smoke("aaa", run_id=8)], git)
        self.assertTrue(ok)
        text = "\n".join(lines)
        self.assertIn("aaa", text)
        self.assertIn("actions/runs/7", text)
        self.assertIn("actions/runs/8", text)

    def test_docs_only_since_the_gated_commit_passes(self):
        git = FakeGit({"aaa": ["docs/measurements/x.md", "README.md"]})
        ok, _ = self.decide(["charts/a/values.yaml"], [dry_run("aaa"), smoke("aaa")], git)
        self.assertTrue(ok)

    def test_code_since_the_gated_commit_fails(self):
        git = FakeGit({"aaa": ["README.md", "charts/a/values.yaml"]})
        ok, lines = self.decide(["charts/a/values.yaml"], [dry_run("aaa"), smoke("aaa")], git)
        self.assertFalse(ok)
        self.assertIn("charts/a/values.yaml", "\n".join(lines))

    def test_any_non_markdown_file_since_counts(self):
        git = FakeGit({"aaa": ["scripts/x.py"]})
        ok, _ = self.decide(["charts/a/values.yaml"], [dry_run("aaa"), smoke("aaa")], git)
        self.assertFalse(ok)

    def test_gated_commit_not_an_ancestor_fails(self):
        ok, lines = self.decide(["charts/a/values.yaml"], [dry_run("zzz"), smoke("aaa")],
                                FakeGit({"aaa": []}))
        self.assertFalse(ok)
        self.assertIn("zzz", "\n".join(lines))

    def test_only_the_latest_successful_run_counts(self):
        # The older run covers the head; the newer one is on a commit that is not an
        # ancestor. ADR-039 relies on the latest, so this fails.
        runs = [dry_run("aaa", created="2026-10-10T10:00:00Z", run_id=1),
                dry_run("zzz", created="2026-10-10T11:00:00Z", run_id=2), smoke("aaa")]
        ok, _ = self.decide(["charts/a/values.yaml"], runs, FakeGit({"aaa": []}))
        self.assertFalse(ok)

    def test_failed_runs_other_branches_and_other_workflows_are_ignored(self):
        runs = [dry_run("bbb", conclusion="failure"),
                dry_run("ccc", branch="gate/dry-run/other"),
                dry_run("ddd", path=SMOKE), smoke("aaa")]
        git = FakeGit({"aaa": [], "bbb": [], "ccc": [], "ddd": []})
        ok, _ = self.decide(["charts/a/values.yaml"], runs, git)
        self.assertFalse(ok)

    def test_a_dry_run_without_a_smoke_fails_and_names_the_smoke_branch(self):
        ok, lines = self.decide(["charts/a/values.yaml"], [dry_run("aaa")],
                                FakeGit({"aaa": []}))
        self.assertFalse(ok)
        self.assertIn("git push origin HEAD:refs/heads/gate/smoke/task", "\n".join(lines))

    def test_a_smoke_without_a_dry_run_fails(self):
        ok, lines = self.decide(["charts/a/values.yaml"], [smoke("aaa")], FakeGit({"aaa": []}))
        self.assertFalse(ok)
        self.assertIn("gate/dry-run/task", "\n".join(lines))

    def test_the_two_may_cover_the_head_from_different_commits(self):
        git = FakeGit({"aaa": ["README.md"], "bbb": []})
        ok, lines = self.decide(["charts/a/values.yaml"], [dry_run("aaa"), smoke("bbb")], git)
        self.assertTrue(ok)
        text = "\n".join(lines)
        self.assertIn("aaa", text)
        self.assertIn("bbb", text)

    def test_a_smoke_on_a_commit_that_is_not_an_ancestor_fails(self):
        ok, lines = self.decide(["charts/a/values.yaml"], [dry_run("aaa"), smoke("zzz")],
                                FakeGit({"aaa": []}))
        self.assertFalse(ok)
        self.assertIn("zzz", "\n".join(lines))

    def test_code_since_the_smoke_commit_fails(self):
        git = FakeGit({"aaa": [], "bbb": ["gates/gate.py"]})
        ok, lines = self.decide(["charts/a/values.yaml"], [dry_run("aaa"), smoke("bbb")], git)
        self.assertFalse(ok)
        self.assertIn("gates/gate.py", "\n".join(lines))

    def test_failed_smokes_smokes_of_other_tasks_and_dry_runs_on_the_smoke_branch_are_ignored(
            self):
        runs = [dry_run("aaa"), smoke("bbb", conclusion="failure"),
                smoke("ccc", branch="gate/smoke/other"),
                dry_run("ddd", branch="gate/smoke/task")]
        git = FakeGit({"aaa": [], "bbb": [], "ccc": [], "ddd": []})
        ok, _ = self.decide(["charts/a/values.yaml"], runs, git)
        self.assertFalse(ok)

    def test_a_platform_file_needs_dry_run_and_smoke(self):
        ok, lines = self.decide(["install/release.py"], [dry_run("aaa"), smoke("aaa")],
                                FakeGit({"aaa": []}))
        self.assertTrue(ok)
        self.assertNotIn("gate/log-batch/", "\n".join(lines))

    def test_a_contract_or_batch_only_change_needs_no_run(self):
        for path in ("docs/contracts/polaris-logs.md",
                     "docs/contracts/polaris-logs-summary.schema.json",
                     ".github/workflows/create-fixtures.yml",
                     ".github/workflows/log-batch.yml", ".claude/rules/gates.md",
                     "scripts/gate_check.py"):
            with self.subTest(path=path):
                ok, lines = self.decide([path])
                self.assertTrue(ok)
                self.assertIn("no gate run needed", lines[0])

    def test_a_platform_path_that_was_a_spec_file_needs_dry_run_and_smoke_only(self):
        for path in ("gates/logs_traffic.sh", "gates/fixtures.py",
                     "charts/polaris/values-lakehouse.yaml",
                     "images/polaris-log-batch/polaris_log_batch.py",
                     "charts/polaris-log-batch/values.yaml"):
            with self.subTest(path=path):
                ok, lines = self.decide([path], branch="claude/task")
                self.assertFalse(ok)
                text = "\n".join(lines)
                self.assertIn("gate/dry-run/task", text)
                self.assertIn("gate/smoke/task", text)
                self.assertNotIn("gate/log-batch/", text)
                ok, lines = self.decide([path], [dry_run("aaa"), smoke("aaa")],
                                        FakeGit({"aaa": []}))
                self.assertTrue(ok)
                self.assertNotIn("log-batch", "\n".join(lines))

    def test_a_log_batch_run_is_neither_needed_nor_read(self):
        old = dry_run("bbb", branch="gate/log-batch/task",
                      path=".github/workflows/log-batch.yml", run_id=201)
        ok, lines = self.decide(["install/release.py"], [dry_run("aaa"), smoke("aaa"), old],
                                FakeGit({"aaa": []}))
        self.assertTrue(ok)
        self.assertNotIn("log-batch", "\n".join(lines))


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class GitHelpers(unittest.TestCase):
    """The git calls, on a throwaway repository."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "user.email", "t@example.invalid")
        self.base = self.commit({"README.md": "a\n", "charts/a/values.yaml": "x: 1\n"})

    def tearDown(self):
        self._tmp.cleanup()

    def commit(self, files=None, remove=()):
        for name, text in (files or {}).items():
            path = os.path.join(self.repo, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        for name in remove:
            git(self.repo, "rm", "-q", name)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "c")
        return git(self.repo, "rev-parse", "HEAD")

    def test_changed_files_since_the_merge_base(self):
        git(self.repo, "checkout", "-q", "-b", "claude/task")
        head = self.commit({"docs/x.md": "d\n"})
        git(self.repo, "checkout", "-q", "main")
        base = self.commit({"other.txt": "o\n"})
        self.assertEqual(gate.changed_files(self.repo, base, head), ["docs/x.md"])

    def test_a_rename_lists_both_names(self):
        head = self.commit({"docs/values.yaml": "x: 1\n"}, remove=["charts/a/values.yaml"])
        self.assertEqual(sorted(gate.changed_files(self.repo, self.base, head)),
                         ["charts/a/values.yaml", "docs/values.yaml"])

    def test_ancestry_and_changes_since(self):
        gated = self.commit({"charts/a/values.yaml": "x: 2\n"})
        head = self.commit({"docs/proof.md": "p\n"})
        repo = gate.Git(self.repo, head)
        self.assertTrue(repo.is_ancestor(gated))
        self.assertEqual(repo.changed_since(gated), ["docs/proof.md"])

    def test_unknown_or_unrelated_commit_is_not_an_ancestor(self):
        head = self.commit({"docs/x.md": "x\n"})
        repo = gate.Git(self.repo, head)
        self.assertFalse(repo.is_ancestor("0" * 40))
        git(self.repo, "checkout", "-q", "-b", "side", self.base)
        side = self.commit({"side.txt": "s\n"})
        self.assertFalse(repo.is_ancestor(side))


class Main(unittest.TestCase):
    def test_gate_branch_mode_prints_the_two_branches(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            with open(out, "w", encoding="utf-8") as f:
                code = subprocess.run(
                    [sys.executable, gate.__file__, "--gate-branch", "claude/t"],
                    stdout=f, check=False).returncode
            with open(out, encoding="utf-8") as f:
                self.assertEqual((code, f.read().split()),
                                 (0, ["gate/dry-run/t", "gate/smoke/t"]))

    def test_runs_file_may_be_the_api_object_or_a_list(self):
        runs = [dry_run("aaa")]
        self.assertEqual(gate.load_runs(json.dumps({"workflow_runs": runs})), runs)
        self.assertEqual(gate.load_runs(json.dumps(runs)), runs)


if __name__ == "__main__":
    unittest.main()
