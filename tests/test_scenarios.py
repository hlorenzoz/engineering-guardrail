"""Canaries: each lock must bite on the bad case and stay quiet on the good one.

Every scenario builds a throwaway git repository and runs the REAL command line
(bin/guardrail), never a re-implementation. GUARDRAIL_HOME points the suite at
another copy of the code: test_mutants.py uses it to prove these scenarios turn
red when a lock is broken.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HOME = os.environ.get("GUARDRAIL_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(HOME, "bin", "guardrail")
# A hook runs with GIT_DIR, GIT_INDEX_FILE and friends set for the repository being
# committed. A throwaway repository must never see them: its git would read and write
# the REAL index instead of its own.
CLEAN_ENV = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
CONFIG = {
    "production": ["src/**"],
    "crossView": [{"name": "billing", "paths": ["src/billing/**"], "tests": ["test/consistency/**"]}],
    "perturb": {"runners": [{"files": "test/**", "command": "python3 {file}"}], "max": 2},
    "switchedOn": {"triggers": ["migrations/**"]},
}


class Repo:
    """A throwaway repository with one commit, removed when the test ends."""

    def __init__(self, case: unittest.TestCase, config=CONFIG):
        self._tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        self.git("init", "-q")
        self.write(".guardrail.json", json.dumps(config))
        self.write("README.md", "start\n")
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "chore: start")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, env=CLEAN_ENV)

    def write(self, path, text):
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    def stage(self, path, text):
        self.write(path, text)
        self.git("add", path)

    def run(self, *args):
        result = subprocess.run(["python3", BIN, *args], cwd=self.root, capture_output=True, text=True, env=CLEAN_ENV)
        return result.returncode, result.stderr

    def gate(self, message):
        self.write(".git/MSG", message)
        return self.run("commit-gate", os.path.join(self.root, ".git", "MSG"))


class Scenario(unittest.TestCase):
    def expect(self, outcome, code, text=None):
        """Check the exit code AND the message: a lock that blocks for the wrong reason is broken."""
        self.assertEqual(outcome[0], code, outcome[1])
        if text is not None:
            self.assertIn(text, outcome[1])


class NoFakeTests(Scenario):
    def test_bites_on_a_skipped_test(self):
        repo = Repo(self)
        repo.stage("test/a.test.ts", "it.skip('adds', () => { expect(add(1, 1)).toBe(2) })\n")
        self.expect(repo.run("staged"), 1, "test/a.test.ts:1: focused or skipped test")

    def test_bites_on_a_literal_compared_with_itself(self):
        repo = Repo(self)
        repo.stage("test/a.test.ts", "it('x', () => { expect(2).toBe(2) })\n")
        self.expect(repo.run("staged"), 1, "compares a literal with itself")

    def test_bites_on_an_empty_body(self):
        repo = Repo(self)
        repo.stage("test/a.test.ts", "it('does it', () => {})\n")
        self.expect(repo.run("staged"), 1, "empty body")

    def test_stays_quiet_on_a_real_test(self):
        repo = Repo(self)
        repo.stage("test/a.test.ts", "it('adds', () => { expect(add(1, 1)).toBe(2) })\n")
        self.expect(repo.run("staged"), 0)

    def test_stays_quiet_on_an_old_skip_this_commit_did_not_add(self):
        repo = Repo(self)
        repo.stage("test/a.test.ts", "it.skip('old', () => { expect(f()).toBe(1) })\n")
        repo.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "test: old")
        repo.stage("test/a.test.ts", "it.skip('old', () => { expect(f()).toBe(1) })\nit('new', () => { expect(g()).toBe(3) })\n")
        self.expect(repo.run("staged"), 0)

    def test_stays_quiet_on_a_skip_inside_a_comment_or_with_a_reason(self):
        repo = Repo(self)
        repo.stage(
            "test/a.test.ts",
            "// it.skip('x') was removed\nconst suite = DB ? describe : describe.skip // guardrail: allow-skip no database\n",
        )
        self.expect(repo.run("staged"), 0)

    def test_a_binary_file_among_the_tests_does_not_break_the_lock(self):
        # A browser suite keeps its screenshots next to the specs.
        repo = Repo(self)
        full = os.path.join(repo.root, "e2e/shot.png")
        os.makedirs(os.path.dirname(full))
        with open(full, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n\x00\xff\xfe it.skip(")
        repo.git("add", "e2e/shot.png")
        repo.stage("test/a.test.ts", "it.skip('x', () => { expect(f()).toBe(1) })\n")
        self.expect(repo.run("staged"), 1, "test/a.test.ts:1: focused or skipped test")

    def test_ignores_files_that_are_not_tests(self):
        repo = Repo(self)
        repo.stage("src/a.ts", "export const skipIt = 'it.skip('\n")
        self.expect(repo.run("staged"), 0)


class FixNeedsTest(Scenario):
    def test_bites_on_production_without_a_test(self):
        repo = Repo(self)
        repo.stage("src/a.ts", "export const a = 1\n")
        self.expect(repo.gate("fix: a\n"), 1, "1 production file(s) staged and no test")

    def test_stays_quiet_with_a_test_staged(self):
        repo = Repo(self)
        repo.stage("src/a.ts", "export const a = 1\n")
        repo.stage("test/a.test.ts", "it('a', () => { expect(a).toBe(1) })\n")
        self.expect(repo.gate("fix: a\n"), 0)

    def test_hatch_needs_its_own_line_and_a_reason(self):
        repo = Repo(self)
        repo.stage("src/a.ts", "export const a = 1\n")
        self.expect(repo.gate("fix: a\n\nno-test: rename only, covered by the type check\n"), 0)
        self.expect(repo.gate("fix: a, see no-test: rename only, covered by types\n"), 1)
        self.expect(repo.gate("fix: a\n\nno-test: n/a\n"), 1)

    def test_stays_quiet_on_documentation_only(self):
        repo = Repo(self)
        repo.stage("docs/a.md", "text\n")
        self.expect(repo.gate("docs: a\n"), 0)

    def test_a_deleted_test_does_not_count_as_a_staged_test(self):
        repo = Repo(self)
        repo.stage("test/a.test.ts", "it('a', () => { expect(a).toBe(1) })\n")
        repo.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "test: a")
        repo.git("rm", "-q", "test/a.test.ts")
        repo.stage("src/a.ts", "export const a = 2\n")
        self.expect(repo.gate("fix: a\n"), 1, "no test")


class SizeCap(Scenario):
    def big(self, repo, files):
        for n in range(files):
            repo.stage(f"src/f{n}.ts", "export const v = 1\n")
        repo.stage("test/a.test.ts", "it('a', () => { expect(v).toBe(1) })\n")

    def test_bites_over_the_file_cap(self):
        repo = Repo(self)
        self.big(repo, 9)
        self.expect(repo.gate("feat: many\n"), 1, "9 production files and 9 changed lines (cap: 8 files, 400 lines)")

    def test_stays_quiet_at_the_cap(self):
        repo = Repo(self)
        self.big(repo, 8)
        self.expect(repo.gate("feat: many\n"), 0)

    def test_bites_over_the_line_cap(self):
        repo = Repo(self)
        repo.stage("src/a.ts", "x\n" * 401)
        repo.stage("test/a.test.ts", "it('a', () => { expect(v).toBe(1) })\n")
        self.expect(repo.gate("feat: long\n"), 1, "401 changed lines")

    def test_tests_and_documents_do_not_count(self):
        repo = Repo(self)
        repo.stage("src/a.ts", "x\n")
        repo.stage("test/a.test.ts", "y\n" * 900)
        repo.stage("docs/a.md", "z\n" * 900)
        self.expect(repo.gate("feat: a\n"), 0)

    def test_hatch_opens_it(self):
        repo = Repo(self)
        self.big(repo, 9)
        self.expect(repo.gate("feat: many\n\nsize-ok: baseline of work done before the guardrail\n"), 0)


class CrossView(Scenario):
    def test_bites_when_a_figure_changes_without_a_consistency_test(self):
        repo = Repo(self)
        repo.stage("src/billing/total.ts", "export const t = 1\n")
        repo.stage("test/total.test.ts", "it('t', () => { expect(t).toBe(1) })\n")
        self.expect(repo.gate("fix: total\n"), 1, "[billing] src/billing/total.ts changes a figure")

    def test_stays_quiet_with_the_consistency_test(self):
        repo = Repo(self)
        repo.stage("src/billing/total.ts", "export const t = 1\n")
        repo.stage("test/consistency/total.test.ts", "it('t', () => { expect(a()).toBe(b()) })\n")
        self.expect(repo.gate("fix: total\n"), 0)

    def test_stays_quiet_outside_the_governed_paths(self):
        repo = Repo(self)
        repo.stage("src/other.ts", "export const t = 1\n")
        repo.stage("test/other.test.ts", "it('t', () => { expect(t).toBe(1) })\n")
        self.expect(repo.gate("fix: other\n"), 0)


class SwitchedOn(Scenario):
    ENTRY = "### nightly job\n- **State**: ON\n- **Who switches it on**: person (ops)\n- **Check**: `crontab -l`\n- **Last check**: 2026-10-01 → listed\n"

    def test_bites_on_a_trigger_without_a_registry_entry(self):
        repo = Repo(self)
        repo.stage("migrations/0001.sql", "select 1;\n")
        self.expect(repo.gate("feat: migration\n"), 1, "has to be switched on")

    def test_stays_quiet_when_the_registry_is_staged(self):
        repo = Repo(self)
        repo.stage("migrations/0001.sql", "select 1;\n")
        repo.stage(".agents/SWITCHED_ON.md", self.ENTRY)
        self.expect(repo.gate("feat: migration\n"), 0)

    def test_bites_on_a_broken_entry(self):
        repo = Repo(self)
        repo.stage(".agents/SWITCHED_ON.md", "### nightly job\n- **State**: MAYBE\n- **Check**: look at it\n")
        code, out = repo.run("staged")
        self.assertEqual(code, 1, out)
        for text in ("State must be one of", "missing 'Who switches it on'", "missing 'Last check'", "in backticks"):
            self.assertIn(text, out)

    def test_an_old_check_is_a_note_never_a_failure(self):
        repo = Repo(self)
        repo.stage(".agents/SWITCHED_ON.md", self.ENTRY.replace("2026-10-01", "2020-01-01"))
        self.expect(repo.run("staged"), 0, "note: nightly job: last checked")


class LiveCitations(Scenario):
    def test_bites_on_a_missing_file_a_line_past_the_end_and_a_blank_line(self):
        repo = Repo(self)
        repo.stage("src/a.ts", "one\n\nthree\n")
        repo.stage("docs/a.md", "see `src/gone.ts:1` and `src/a.ts:9` and `src/a.ts:2`\n")
        code, out = repo.run("staged")
        self.assertEqual(code, 1, out)
        for text in ("src/gone.ts does not exist", "src/a.ts:9 is past the end (3 lines)", "src/a.ts:2 is a blank line"):
            self.assertIn(text, out)

    def test_stays_quiet_on_a_live_citation(self):
        repo = Repo(self)
        repo.stage("src/a.ts", "one\n\nthree\n")
        repo.stage("docs/a.md", "see `src/a.ts:3`\n")
        self.expect(repo.run("staged"), 0)

    def test_a_path_in_another_repository_is_counted_not_failed(self):
        repo = Repo(self)
        repo.stage("docs/a.md", "see `other-repo/src/a.ts:3`\n")
        self.expect(repo.run("staged"), 0, "1 citation(s) point outside this repository")


class Perturb(Scenario):
    def script(self, checked):
        # A stand-in test file: it passes only when the asserted value is 4.
        tail = "assert v == EXPECTED" if checked else "pass"
        return f"import re, sys\nv = 2 + 2\nline = \"expect(v).toBe(4)\"\nEXPECTED = int(re.search(r'toBe\\((\\d+)\\)', open(__file__).read().split('\\n')[2]).group(1))\n{tail}\n"

    def test_stays_quiet_when_the_assertion_really_checks(self):
        repo = Repo(self)
        repo.stage("test/a.test.py", self.script(True))
        self.expect(repo.run("perturb"), 0)

    def test_bites_when_the_test_stays_green_with_a_wrong_expected_value(self):
        repo = Repo(self)
        repo.stage("test/a.test.py", self.script(False))
        self.expect(repo.run("perturb"), 1, "test/a.test.py:3: still green with the expected value 4 replaced")

    def test_restores_the_file_afterwards(self):
        repo = Repo(self)
        repo.stage("test/a.test.py", self.script(True))
        repo.run("perturb")
        with open(os.path.join(repo.root, "test/a.test.py"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), self.script(True))

    def test_bites_when_no_runner_is_configured(self):
        repo = Repo(self, {**CONFIG, "perturb": {"runners": []}})
        repo.stage("test/a.test.py", self.script(True))
        self.expect(repo.run("perturb"), 1, "no runner configured")

    def test_a_suite_declared_not_runnable_is_a_note_not_a_pass_in_silence(self):
        repo = Repo(self, {**CONFIG, "perturb": {"runners": [{"files": "test/**", "command": None}]}})
        repo.stage("test/a.test.py", self.script(False))
        self.expect(repo.run("perturb"), 0, "1 assertion(s) not perturbed (declared not runnable at commit time)")

    def test_bites_when_the_test_is_already_red(self):
        repo = Repo(self)
        repo.stage("test/a.test.py", self.script(True).replace("v = 2 + 2", "v = 5"))
        self.expect(repo.run("perturb"), 1, "fails before any perturbation")

    def test_says_so_when_the_limit_is_reached(self):
        repo = Repo(self)
        body = self.script(True) + "x = \"expect(v).toHaveLength(7)\"\ny = \"expect(v).toEqual(8)\"\nz = \"expect(v).toEqual(9)\"\n"
        repo.stage("test/a.test.py", body)
        _, out = repo.run("perturb")
        self.assertIn("not perturbed (limit of 2 per commit reached)", out)


class HookEnvironment(Scenario):
    def test_the_scenarios_run_under_the_git_environment_of_a_hook(self):
        # pre-commit and git export GIT_DIR / GIT_INDEX_FILE to a hook. If the throwaway
        # repositories inherited them, the suite would act on the repository being committed.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "GIT_DIR": os.path.join(tmp, "elsewhere", ".git"), "GIT_INDEX_FILE": os.path.join(tmp, "index")}
            probes = ["tests.test_scenarios.NoFakeTests.test_bites_on_a_skipped_test", "tests.test_scenarios.Perturb.test_stays_quiet_when_the_assertion_really_checks"]
            result = subprocess.run([sys.executable, "-m", "unittest", "-q", *probes], cwd=here, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(os.path.exists(env["GIT_INDEX_FILE"]), "the suite touched the index of the hook's repository")


class Wiring(Scenario):
    def test_selfcheck_bites_when_hooks_are_not_installed(self):
        repo = Repo(self)
        repo.write(".pre-commit-config.yaml", "repos: []\n")
        code, out = repo.run("selfcheck")
        self.assertEqual(code, 1, out)
        self.assertIn("the pre-push hook is not installed", out)
        self.assertIn("guardrail-commit-gate is not in .pre-commit-config.yaml", out)

    def test_selfcheck_passes_when_everything_is_wired(self):
        repo = Repo(self)
        repo.write(".pre-commit-config.yaml", "".join(f"  - id: {i}\n" for i in ("guardrail-selfcheck", "guardrail-staged", "guardrail-perturb", "guardrail-commit-gate")))
        for name in ("pre-commit", "commit-msg", "pre-push"):
            repo.write(f".git/hooks/{name}", "#!/bin/sh\n# generated by pre-commit\n")
            os.chmod(os.path.join(repo.root, ".git/hooks", name), 0o755)
        self.expect(repo.run("selfcheck"), 0)

    def test_a_merge_skips_the_content_locks(self):
        repo = Repo(self)
        repo.stage("src/a.ts", "export const a = 1\n")
        repo.write(".git/MERGE_HEAD", "0" * 40 + "\n")
        self.expect(repo.gate("Merge branch 'x'\n"), 0)

    def test_a_broken_configuration_never_passes(self):
        repo = Repo(self)
        repo.write(".guardrail.json", "{ not json")
        self.expect(repo.run("staged"), 2, "cannot look, so it does not pass")

    def test_an_unknown_command_is_refused(self):
        repo = Repo(self)
        self.expect(repo.run("nonsense"), 2, "unknown command")


if __name__ == "__main__":
    unittest.main()
