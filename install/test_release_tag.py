"""Tests for release_tag.py. Run: python3 -m unittest discover -s install"""

import contextlib
import inspect
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import release_tag as rt  # noqa: E402

PINS = {
    "node_image": {"image": "kindest/node:v1.36.4@sha256:" + "a" * 64},
    "cnpg_chart": {"version": "0.29.1", "operator_version": "1.30.1"},
}
SUPPORTED = {"cnpg_operator": ["1.30"]}


def check(name, conclusion="success", status="completed", url=None):
    return {"name": name, "status": status, "conclusion": conclusion,
            "html_url": url or f"https://github.com/o/r/actions/runs/1/job/{len(name)}"}


GATES = ("static", "dry-run / dry-run", "smoke upgrade / smoke", "smoke fresh / smoke")


def all_green():
    return [check(name) for name in GATES]


class Previous(unittest.TestCase):
    def test_no_tag_gives_none(self):
        self.assertIsNone(rt.previous([]))

    def test_the_latest_date_wins(self):
        tags = ["platform-2026.10.10", "platform-2026.11.02", "platform-2026.10.31"]
        self.assertEqual(rt.previous(tags), "platform-2026.11.02")

    def test_a_second_release_the_same_day_is_later_and_10_follows_9(self):
        tags = ["platform-2026.10.10", "platform-2026.10.10.2", "platform-2026.10.10.10",
                "platform-2026.10.10.9"]
        self.assertEqual(rt.previous(tags), "platform-2026.10.10.10")

    def test_other_names_are_ignored(self):
        self.assertIsNone(rt.previous(["v1.0.0", "platform-x", "platform-2026.10.10.1a"]))

    def test_a_candidate_tag_is_not_a_release(self):
        tags = ["platform-2026.10.10", "platform-2026.10.23-rc.1", "platform-2026.10.10.2-rc.3"]
        self.assertEqual(rt.previous(tags), "platform-2026.10.10")
        self.assertIsNone(rt.previous(["platform-2026.10.23-rc.1"]))


class NextTag(unittest.TestCase):
    def test_the_first_release_of_a_day_has_no_suffix(self):
        self.assertEqual(rt.next_tag("2026-10-10", ["platform-2026.10.09"]),
                         "platform-2026.10.10")

    def test_the_second_release_of_a_day_is_2(self):
        self.assertEqual(rt.next_tag("2026-10-10", ["platform-2026.10.10"]),
                         "platform-2026.10.10.2")

    def test_the_next_follows_the_highest_suffix(self):
        tags = ["platform-2026.10.10", "platform-2026.10.10.2", "platform-2026.10.10.3"]
        self.assertEqual(rt.next_tag("2026-10-10", tags), "platform-2026.10.10.4")

    def test_a_date_with_only_candidate_tags_is_still_free(self):
        tags = ["platform-2026.10.09", "platform-2026.10.10-rc.1", "platform-2026.10.10-rc.2"]
        self.assertEqual(rt.next_tag("2026-10-10", tags), "platform-2026.10.10")

    def test_candidates_of_a_released_date_do_not_move_the_suffix(self):
        tags = ["platform-2026.10.10", "platform-2026.10.10.2-rc.5"]
        self.assertEqual(rt.next_tag("2026-10-10", tags), "platform-2026.10.10.2")

    def test_a_malformed_date_raises(self):
        for date in ("2026-1-10", "20261010", "2026.10.10"):
            with self.subTest(date=date), self.assertRaises(ValueError):
                rt.next_tag(date, [])


class RcTag(unittest.TestCase):
    def test_the_first_candidate_of_a_version_is_rc_1(self):
        self.assertEqual(rt.rc_tag("platform-2026.10.23", ["platform-2026.10.22"]),
                         "platform-2026.10.23-rc.1")

    def test_the_next_follows_the_highest_candidate_of_that_version_only(self):
        tags = ["platform-2026.10.23-rc.1", "platform-2026.10.23-rc.2", "platform-2026.10.23-rc.10",
                "platform-2026.10.23.2-rc.7", "platform-2026.10.24-rc.4"]
        self.assertEqual(rt.rc_tag("platform-2026.10.23", tags), "platform-2026.10.23-rc.11")
        self.assertEqual(rt.rc_tag("platform-2026.10.23.2", tags), "platform-2026.10.23.2-rc.8")

    def test_a_final_tag_that_is_not_a_release_name_raises(self):
        for final in ("platform-2026.10.23-rc.1", "v1.0.0", ""):
            with self.subTest(final=final), self.assertRaises(ValueError):
                rt.rc_tag(final, [])


class ChecksPassed(unittest.TestCase):
    def run_of(self, name, conclusion="success", status="completed", run="3"):
        return {"name": name, "status": status, "conclusion": conclusion,
                "details_url": f"https://github.com/o/r/actions/runs/{run}/job/5"}

    def runs(self, **kw):
        return [self.run_of("static", run="1")] + [
            self.run_of(n, run="2") for n in ("gate-check", "auto-merge")] + list(kw.values())

    def test_green_checks_pass(self):
        rt.checks_passed(self.runs(), "99")

    def test_a_failed_unfinished_or_cancelled_check_is_named(self):
        for conclusion, status in (("failure", "completed"), ("cancelled", "completed"),
                                   ("timed_out", "completed"), (None, "in_progress")):
            with self.subTest(conclusion=conclusion, status=status):
                runs = self.runs() + [self.run_of("lint", conclusion, status)]
                shown = conclusion if status == "completed" else status
                with self.assertRaisesRegex(ValueError, f"lint: {shown}"):
                    rt.checks_passed(runs, "99")

    def test_skipped_and_neutral_checks_do_not_stop_it(self):
        rt.checks_passed(self.runs() + [self.run_of("a", "skipped"), self.run_of("b", "neutral")],
                         "99")

    def test_this_runs_own_jobs_are_left_out(self):
        runs = self.runs() + [self.run_of("check the candidate", None, "in_progress", run="99"),
                              self.run_of("smoke fresh / smoke", None, "in_progress", run="99")]
        rt.checks_passed(runs, "99")

    def test_the_candidate_the_guard_and_auto_candidate_jobs_from_any_run_are_left_out(self):
        for name in rt.CANDIDATE_JOBS:
            with self.subTest(name=name):
                rt.checks_passed(self.runs() + [self.run_of(name, "failure", run="7")], "99")
        self.assertLessEqual({"decide on a candidate", "push the candidate branch",
                              "candidate branch unchanged", "delete the candidate branch",
                              "rc tag and prerelease"}, set(rt.CANDIDATE_JOBS))

    def test_the_retired_image_job_of_an_earlier_candidate_run_is_still_left_out(self):
        # A commit an earlier Candidate ran on still carries that check run.
        self.assertIn("the log batch's image on GHCR", rt.CANDIDATE_JOBS)
        rt.checks_passed(self.runs() + [self.run_of("the log batch's image on GHCR", "failure",
                                                    run="7")], "99")

    def test_the_gates_called_by_an_earlier_candidate_run_are_left_out(self):
        runs = self.runs() + [self.run_of(n, "failure", run="7") for n in
                              ("dry-run / dry-run", "smoke upgrade / smoke",
                               "smoke fresh / datahub-recovery")]
        rt.checks_passed(runs, "99")

    def test_a_gate_branch_run_is_not_left_out(self):
        with self.assertRaisesRegex(ValueError, "smoke: failure"):
            rt.checks_passed(self.runs() + [self.run_of("smoke", "failure", run="7")], "99")

    def test_static_must_have_succeeded(self):
        runs = [r for r in self.runs() if r["name"] != "static"]
        with self.assertRaisesRegex(ValueError, "static: not reported"):
            rt.checks_passed(runs, "99")

    def test_no_check_run_but_this_runs_stops_it(self):
        with self.assertRaisesRegex(ValueError, "no check run"):
            rt.checks_passed([self.run_of("x", run="99")], "99")


class CheckPromote(unittest.TestCase):
    def test_the_highest_candidate_of_a_new_version_passes_and_gives_the_final_tag(self):
        tags = ["platform-2026.10.22", "platform-2026.10.23-rc.1", "platform-2026.10.23-rc.2"]
        self.assertEqual(rt.check_promote("platform-2026.10.23-rc.2", tags), "platform-2026.10.23")

    def test_an_older_candidate_is_refused(self):
        tags = ["platform-2026.10.23-rc.1", "platform-2026.10.23-rc.2"]
        with self.assertRaisesRegex(ValueError, "platform-2026.10.23-rc.2 is newer"):
            rt.check_promote("platform-2026.10.23-rc.1", tags)

    def test_a_released_version_is_refused(self):
        tags = ["platform-2026.10.23", "platform-2026.10.23-rc.1"]
        with self.assertRaisesRegex(ValueError, "platform-2026.10.23 is already released"):
            rt.check_promote("platform-2026.10.23-rc.1", tags)

    def test_a_version_below_the_latest_release_is_refused(self):
        tags = ["platform-2026.10.24", "platform-2026.10.23-rc.1"]
        with self.assertRaisesRegex(ValueError, "not above the latest release"):
            rt.check_promote("platform-2026.10.23-rc.1", tags)

    def test_a_name_that_is_not_a_candidate_tag_is_refused(self):
        for name in ("platform-2026.10.23", "v1-rc.1", ""):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "not a candidate"):
                rt.check_promote(name, [])


class RcBranches(unittest.TestCase):
    BRANCHES = ["main", "rc", "rc/platform-2026.10.22-rc.1", "rc/platform-2026.10.23-rc.1",
                "rc/platform-2026.10.23-rc.2", "rc/platform-2026.10.23.2-rc.1",
                "rc/junk", "candidate/0123456789ab"]

    def test_the_rc_tag_of_an_rc_branch(self):
        self.assertEqual(rt.rc_of_branch("rc/platform-2026.10.23-rc.2"), "platform-2026.10.23-rc.2")
        for name in ("rc", "rc/junk", "main", "rc/platform-2026.10.23", ""):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "not rc/<rc tag>"):
                rt.rc_of_branch(name)

    def test_superseded_are_the_lower_candidates_of_the_same_version(self):
        self.assertEqual(rt.superseded("platform-2026.10.23-rc.2", self.BRANCHES),
                         ["rc/platform-2026.10.23-rc.1"])
        self.assertEqual(rt.superseded("platform-2026.10.23-rc.1", self.BRANCHES), [])

    def test_released_are_every_candidate_of_that_version_and_older(self):
        self.assertEqual(rt.released("platform-2026.10.23", self.BRANCHES),
                         ["rc/platform-2026.10.22-rc.1", "rc/platform-2026.10.23-rc.1",
                          "rc/platform-2026.10.23-rc.2"])
        self.assertEqual(rt.released("platform-2026.10.23.2", self.BRANCHES),
                         ["rc/platform-2026.10.22-rc.1", "rc/platform-2026.10.23-rc.1",
                          "rc/platform-2026.10.23-rc.2", "rc/platform-2026.10.23.2-rc.1"])

    def test_released_refuses_a_name_that_is_not_a_release(self):
        with self.assertRaises(ValueError):
            rt.released("platform-2026.10.23-rc.1", self.BRANCHES)


class Notes(unittest.TestCase):
    def notes(self, previous="platform-2026.10.09"):
        return rt.notes(tag="platform-2026.10.10-rc.1", commit="c" * 40, previous=previous,
                        branch="main", checks=all_green(),
                        log=["abc1234 A change", "def5678 Another"], pins=PINS,
                        supported=SUPPORTED)

    def test_names_the_tag_commit_and_branch(self):
        text = self.notes()
        self.assertIn("# platform-2026.10.10-rc.1", text)
        self.assertIn("platform-2026.10.10", text)
        self.assertIn("c" * 40, text)
        self.assertIn("from `main`", text)

    def test_names_the_gate_kubernetes_and_its_node_image_digest(self):
        text = self.notes()
        self.assertIn("Kubernetes v1.36.4", text)
        self.assertIn("kindest/node:v1.36.4@sha256:" + "a" * 64, text)

    def test_names_the_cnpg_pins_and_the_supported_operators(self):
        text = self.notes()
        self.assertIn("chart 0.29.1, operator 1.30.1", text)
        self.assertIn("1.30", text)

    def test_links_every_gate_check(self):
        text = self.notes()
        for run in all_green():
            self.assertIn(f"[{run['name']}]({run['html_url']})", text)

    def test_lists_the_changes_since_the_previous_release(self):
        text = self.notes()
        self.assertIn("since platform-2026.10.09", text)
        self.assertIn("- abc1234 A change", text)

    def test_names_no_batch_image_and_has_no_digest_placeholder(self):
        text = self.notes()
        self.assertNotIn("polaris-log-batch", text)
        self.assertNotIn("BATCH_IMAGE_DIGEST", text)
        self.assertFalse(hasattr(rt, "DIGEST_PLACEHOLDER"))
        self.assertNotIn("batch_image", inspect.signature(rt.notes).parameters)

    def test_the_first_release_says_so(self):
        self.assertIn("First release: nothing to upgrade from.", self.notes(previous=None))


class CheckMain(unittest.TestCase):
    def test_main_passes(self):
        rt.check_main("refs/heads/main")

    def test_any_other_ref_is_refused(self):
        for ref in ("refs/heads/candidate/0123456789ab", "refs/heads/rc/platform-2026.10.23-rc.1",
                    "refs/heads/claude/x", "refs/heads/main2", "refs/heads/feature/main",
                    "refs/tags/main", "main", ""):
            with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, "refs/heads/main"):
                rt.check_main(ref)


class ShouldCandidate(unittest.TestCase):
    FINAL = "platform-2026.10.10"

    def test_a_commit_since_the_latest_release_is_a_candidate(self):
        for since in (1, 7):
            with self.subTest(since=since):
                self.assertEqual(rt.should_candidate([], self.FINAL, since), (True, ""))

    def test_head_at_the_latest_releases_commit_is_nothing_to_release(self):
        for tags in ([], ["platform-2026.10.10"]):
            with self.subTest(tags=tags):
                ok, why = rt.should_candidate(tags, self.FINAL, 0)
                self.assertFalse(ok)
                self.assertIn("nothing to release", why)
                self.assertIn(self.FINAL, why)

    def test_no_final_tag_is_the_first_release_of_any_commit(self):
        self.assertEqual(rt.should_candidate([], None, 3), (True, ""))

    def test_a_platform_tag_on_head_final_or_candidate_is_not(self):
        for tag in ("platform-2026.10.10", "platform-2026.10.23-rc.1"):
            with self.subTest(tag=tag):
                ok, why = rt.should_candidate([tag], self.FINAL, 2)
                self.assertFalse(ok)
                self.assertIn(tag, why)

    def test_other_tags_on_head_do_not_stop_it(self):
        self.assertEqual(rt.should_candidate(["v1", "platform-x"], self.FINAL, 1), (True, ""))


class Main(unittest.TestCase):
    def run_main(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = rt.main(["release_tag.py"] + argv)
        return code, out.getvalue()

    def test_previous_prints_the_tag_or_nothing(self):
        self.assertEqual(self.run_main(["previous", "platform-2026.10.10"]),
                         (0, "platform-2026.10.10\n"))
        self.assertEqual(self.run_main(["previous"]), (0, ""))

    def test_next_prints_the_name(self):
        self.assertEqual(self.run_main(["next", "2026-10-10", "platform-2026.10.10"]),
                         (0, "platform-2026.10.10.2\n"))

    def test_rc_prints_the_next_candidate_tag(self):
        self.assertEqual(self.run_main(["rc", "platform-2026.10.23", "platform-2026.10.23-rc.1"]),
                         (0, "platform-2026.10.23-rc.2\n"))

    def test_check_promote_prints_the_final_tag_or_exits_1(self):
        self.assertEqual(self.run_main(["check-promote", "platform-2026.10.23-rc.1"]),
                         (0, "platform-2026.10.23\n"))
        code, _ = self.run_main(["check-promote", "platform-2026.10.23-rc.1",
                                 "platform-2026.10.23"])
        self.assertEqual(code, 1)

    def test_rc_branch_superseded_and_released_print_space_separated(self):
        self.assertEqual(self.run_main(["rc-branch", "rc/platform-2026.10.23-rc.1"]),
                         (0, "platform-2026.10.23-rc.1\n"))
        self.assertEqual(self.run_main(["superseded", "platform-2026.10.23-rc.2",
                                        "rc/platform-2026.10.23-rc.1", "rc/platform-2026.10.23-rc.2"]),
                         (0, "rc/platform-2026.10.23-rc.1\n"))
        self.assertEqual(self.run_main(["released", "platform-2026.10.23",
                                        "rc/platform-2026.10.23-rc.1", "rc/platform-2026.10.24-rc.1"]),
                         (0, "rc/platform-2026.10.23-rc.1\n"))
        self.assertEqual(self.run_main(["superseded", "platform-2026.10.23-rc.1"]), (0, "\n"))

    def test_checks_passed_reads_the_api_json_on_stdin_and_names_the_problems(self):
        own = dict(check("static"), details_url="https://github.com/o/r/actions/runs/99/job/1")
        body = json.dumps({"check_runs": [own]})
        stdin = sys.stdin
        try:
            sys.stdin = io.StringIO(body)
            code, out = self.run_main(["checks-passed", "99"])
        finally:
            sys.stdin = stdin
        self.assertEqual(code, 1)
        self.assertIn("no check run", out)

    def test_notes_reads_the_committed_pins(self):
        with tempfile.TemporaryDirectory() as d:
            checks, log = os.path.join(d, "checks.json"), os.path.join(d, "log.txt")
            with open(checks, "w", encoding="utf-8") as f:
                json.dump(all_green(), f)
            with open(log, "w", encoding="utf-8") as f:
                f.write("abc1234 A change\n")
            code, out = self.run_main(["notes", "platform-2026.10.10", "c" * 40, "-",
                                       "main", checks, log])
        self.assertEqual(code, 0)
        self.assertIn("node image `kindest/node:v1.36.4@sha256:", out)
        self.assertIn("First release", out)
        self.assertNotIn("polaris-log-batch", out)

    def test_check_main_prints_nothing_on_main_and_exits_1_elsewhere(self):
        self.assertEqual(self.run_main(["check-main", "refs/heads/main"]), (0, ""))
        code, out = self.run_main(["check-main", "refs/heads/candidate/0123456789ab"])
        self.assertEqual(code, 1)
        self.assertIn("refs/heads/main", out)

    def test_should_candidate_prints_a_verdict(self):
        code, out = self.run_main(["should-candidate"])
        self.assertEqual(code, 0)
        self.assertIn(out, ("true\n", "false\n"))

    def test_unknown_arguments_exit_2(self):
        self.assertEqual(self.run_main(["release"])[0], 2)


if __name__ == "__main__":
    unittest.main()
