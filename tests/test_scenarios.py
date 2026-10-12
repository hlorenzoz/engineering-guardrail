"""Canaries: each lock must bite on the bad case and stay quiet on the good one.

Every scenario builds a throwaway git repository and runs the REAL command line
(bin/guardrail), never a re-implementation. GUARDRAIL_HOME points the suite at
another copy of the code: test_mutants.py uses it to prove these scenarios turn
red when a lock is broken.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
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


RUNNER = r"""
# A stand-in for a test runner that the scenarios control. It reads a tiny test language:
#   test <name>                      opens a test
#   expect(A).toBe(B)                a hard assertion: the first failure ends the test
#   soft(A).toBe(B)                  records a failure and goes on
#   unchecked(A).toBe(B)             an assertion that never compares (the bug the lock hunts)
#   swallowed(A).toBe(B)             a failure that a try/catch swallows
#   whenfailed(A).toBe(B)            runs only after a swallowed failure (a catch block)
# With --report=PATH it also writes a Jest-shaped JSON report; --mode=... breaks it on purpose
# (echo: a passed test carries the message; exit0: the report claims a failure, the exit code says 0).
import json, os, re, sys, time

args = sys.argv[1:]
path = args[0]
opts = {}
for arg in args[1:]:
    if arg.startswith("--"):
        key, _, value = arg[2:].partition("=")
        opts[key] = value or "1"
mode = opts.get("mode", "")
STEP = re.compile(r"^\s*(expect|soft|unchecked|swallowed|whenfailed)\(([^()]*)\)\.(\w+)\(([^()]*)\)\s*$")


def bare(text):
    return text.strip().strip("'\"")


tests = []
with open(path, encoding="utf-8") as handle:
    for number, line in enumerate(handle.read().split("\n"), start=1):
        if line.startswith("test "):
            tests.append((line[5:], []))
        elif tests and STEP.match(line):
            kind, actual, matcher, expected = STEP.match(line).groups()
            tests[-1][1].append((number, kind, bare(actual), matcher, bare(expected), expected.strip()))


def agrees(matcher, actual, expected):
    if matcher == "toBeCloseTo":
        return abs(float(actual) - float(expected)) < 0.005
    return actual == expected


def describe(matcher, actual, expected):
    if matcher == "toBeCloseTo":
        diff = abs(float(actual) - float(expected))
        return "expected %s to be close to %s, received difference is %s" % (actual, expected, int(diff) if diff == int(diff) else diff)
    if matcher == "toHaveLength":
        return "expected [] to have a length of %s but got %s" % (expected, actual)
    if matcher == "toHaveBeenCalledTimes":
        return 'expected "spy" to be called %s times, but got %s times' % (expected, actual)
    return "expected %s to be %s // Object.is equality" % (actual, expected)


perturbed = {}
results = []
for name, steps in tests:
    messages, swallowed = [], False
    for number, kind, actual, matcher, expected, raw in steps:
        if actual != expected:
            perturbed[number] = raw
    for number, kind, actual, matcher, expected, raw in steps:
        if kind == "unchecked":
            if mode == "echo" and actual != expected:
                results.append(("echoed " + name, "passed", [describe(matcher, actual, expected)]))
            if mode == "exit0" and "report" in opts and actual != expected:  # a runner that lies
                results.append(("claimed " + name, "failed", [describe(matcher, actual, expected)]))
            continue
        if kind == "whenfailed" and not swallowed:
            continue
        if agrees(matcher, actual, expected):
            continue
        if kind == "swallowed":
            swallowed = True
            continue
        messages.append(describe(matcher, actual, expected))
        if kind != "soft":
            break
    results.append((name, "failed" if messages else "passed", messages))

if "report" in opts and perturbed and mode == "noise":
    results.append(("noise", "failed", ["boom"]))

with open(".runlog", "a", encoding="utf-8") as log:
    log.write(json.dumps({"report": "report" in opts, "perturbed": perturbed}) + "\n")
with open(".runlog", encoding="utf-8") as log:
    runs_so_far = len(log.read().splitlines())

if "report" in opts and mode != "missing":
    statuses = "passed" if mode == "passed" else None
    document = {"testResults": [{"name": path, "assertionResults": [
        {"title": name, "status": statuses or status, "failureMessages": messages} for name, status, messages in results]}]}
    with open(opts["report"], "w", encoding="utf-8") as out:
        if mode == "malformed":
            out.write("{ not json")
        elif mode == "wrongshape":
            json.dump({"results": []}, out)
        else:
            json.dump(document, out)

if perturbed and (mode == "hang" and "report" in opts or opts.get("pause-on-run") == str(runs_so_far)):
    open(".running", "w").close()
    time.sleep(3)

failed = any(status == "failed" for _, status, _ in results)
sys.exit(0 if (mode == "exit0" and "report" in opts) else 1 if failed else 0)
"""


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

    def run(self, *args, env=None):
        # A lock broken on purpose (test_mutants.py) can loop forever: the timeout turns that into a failure.
        result = subprocess.run(["python3", BIN, *args], cwd=self.root, capture_output=True, text=True, env={**CLEAN_ENV, **(env or {})}, timeout=90)
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


class PerturbBatch(Scenario):
    """`perturb` with a runner that declares `batch`: the new assertions of a file are
    proven together, and only a sentinel found in a FAILED test's message counts."""

    LIMIT = "still green with the expected value {} replaced. The assertion does not check what it claims."

    def repo(self, tests, args="--report={report}", batch=True, **perturb):
        runner = {"files": "test/**", "command": "python3 tools/runner.py {file}"}
        if batch:
            runner["batch"] = {"args": args, "format": "jest-json"}
        repo = Repo(self, {**CONFIG, "perturb": {"runners": [runner], "max": 12, **perturb}})
        repo.write("tools/runner.py", RUNNER)
        repo.stage("test/a.test.ts", tests)
        return repo

    def runs(self, repo):
        """What the runner was asked to do: (report requested, {line: expected value}) per run."""
        with open(os.path.join(repo.root, ".runlog"), encoding="utf-8") as handle:
            return [(run["report"], {int(k): v for k, v in run["perturbed"].items()}) for run in map(json.loads, handle)]

    def shape(self, repo):
        """The same, reduced to (report requested, [lines]) to compare with what is expected."""
        return [(report, sorted(lines)) for report, lines in self.runs(repo)]

    def leftovers(self, repo):
        """Files the lock left in the system temporary folder (TMPDIR is private to the run)."""
        return os.listdir(os.path.join(repo.root, "tmp"))

    def perturb(self, repo, *args):
        os.makedirs(os.path.join(repo.root, "tmp"), exist_ok=True)
        return repo.run("perturb", *args, env={"TMPDIR": os.path.join(repo.root, "tmp")})

    GOOD_FOUR = "test one\n  expect(1).toBe(1)\ntest two\n  expect(2).toBe(2)\ntest three\n  expect(3).toBe(3)\ntest four\n  expect(4).toBe(4)\n"
    GOOD_BESIDE_BAD = "test one\n  expect(1).toBe(1)\ntest two\n  unchecked(2).toBe(2)\ntest three\n  expect(3).toBe(3)\n"

    # --- must stay quiet ---------------------------------------------------

    def test_good_assertions_in_different_tests_are_proven_in_one_run(self):
        repo = self.repo(self.GOOD_FOUR)
        self.expect(self.perturb(repo), 0)
        self.assertEqual(self.shape(repo), [(False, []), (True, [2, 4, 6, 8])])
        self.assertEqual(self.leftovers(repo), [], "the report was left behind")

    def test_two_good_assertions_in_one_test_cost_one_more_run(self):
        # The first failure ends the test, so the second is only seen once the first is restored.
        repo = self.repo("test one\n  expect(1).toBe(1)\n  expect(2).toBe(2)\n")
        self.expect(self.perturb(repo), 0)
        self.assertEqual(self.shape(repo), [(False, []), (True, [2, 3]), (False, [3])])

    def test_a_chain_of_good_assertions_is_proven_over_rounds(self):
        repo = self.repo("test one\n  expect(1).toBe(1)\n  expect(2).toBe(2)\n  expect(3).toBe(3)\n")
        self.expect(self.perturb(repo), 0)
        # Each round restores what it proved; the last one alone is checked the old way.
        self.assertEqual(self.shape(repo), [(False, []), (True, [2, 3, 4]), (True, [3, 4]), (False, [4])])

    def test_soft_assertions_do_not_need_a_second_round(self):
        repo = self.repo("test one\n  soft(1).toBe(1)\n  soft(2).toBe(2)\n")
        self.expect(self.perturb(repo), 0)
        self.assertEqual(self.shape(repo), [(False, []), (True, [2, 3])])

    def test_numbers_strings_and_booleans_each_get_their_own_sentinel(self):
        repo = self.repo("test one\n  expect('a').toBe('a')\ntest two\n  expect(true).toBe(true)\ntest three\n  expect(7).toHaveLength(7)\n")
        self.expect(self.perturb(repo), 0)
        _, round_one = self.runs(repo)[1]
        self.assertRegex(round_one[2], r"^'ZZ_MUTATED_\d+_ZZ'$")
        self.assertRegex(round_one[4], r"^\d+$")
        self.assertRegex(round_one[6], r"^\d+$")
        self.assertEqual(len(set(round_one.values())), 3, "two assertions share a sentinel")

    def test_a_sentinel_that_already_appears_in_the_file_is_not_used(self):
        repo = self.repo("// 988654003 is in the file\ntest one\n  expect(1).toBe(1)\ntest two\n  expect(2).toBe(2)\n")
        self.expect(self.perturb(repo), 0)
        values = list(self.runs(repo)[1][1].values())
        self.assertNotIn("988654003", values)
        self.assertEqual(len(set(values)), 2)

    def test_the_difference_a_close_to_matcher_prints_is_not_another_assertions_sentinel(self):
        # toBeCloseTo prints |received - expected|. With consecutive sentinels, the second one
        # (received 1) prints the FIRST sentinel, and the unchecked assertion looks proven.
        repo = self.repo("test one\n  unchecked(4).toBe(4)\ntest two\n  expect(1).toBeCloseTo(1)\n")
        self.expect(self.perturb(repo), 1, "test/a.test.ts:2: " + self.LIMIT.format(4))

    def test_the_limit_per_commit_applies_to_the_batch(self):
        repo = self.repo(self.GOOD_FOUR, max=3)
        code, out = self.perturb(repo)
        self.assertEqual(code, 0, out)
        self.assertIn("test/a.test.ts:8: not perturbed (limit of 3 per commit reached)", out)
        self.assertEqual(self.shape(repo), [(False, []), (True, [2, 4, 6])])

    # --- must bite ---------------------------------------------------------

    def test_bites_on_a_lone_assertion_that_checks_nothing(self):
        repo = self.repo("test one\n  unchecked(4).toBe(4)\n")
        self.expect(self.perturb(repo), 1, "test/a.test.ts:2: " + self.LIMIT.format(4))
        self.assertEqual(self.shape(repo), [(False, []), (False, [2])], "one assertion gains nothing from a batch")

    def test_bites_on_a_non_checking_assertion_hidden_behind_a_checking_one(self):
        # Both are in one test: the first failure ends it, so round one cannot see the second.
        repo = self.repo("test one\n  expect(4).toBe(4)\n  unchecked(5).toBe(5)\n")
        code, out = self.perturb(repo)
        self.assertEqual(code, 1, out)
        self.assertIn("test/a.test.ts:3: " + self.LIMIT.format(5), out)
        self.assertNotIn("test/a.test.ts:2:", out)
        self.assertEqual(self.shape(repo), [(False, []), (True, [2, 3]), (False, [3])])

    def test_bites_on_a_non_checking_assertion_beside_good_ones_in_other_tests(self):
        repo = self.repo("test one\n  expect(1).toBe(1)\ntest two\n  expect(2).toBe(2)\ntest three\n  unchecked(3).toBe(3)\ntest four\n  expect(4).toBe(4)\n")
        code, out = self.perturb(repo)
        self.assertEqual(code, 1, out)
        self.assertIn("test/a.test.ts:6: " + self.LIMIT.format(3), out)
        self.assertEqual(out.count("BLOCKED"), 1, out)
        self.assertEqual(self.shape(repo), [(False, []), (True, [2, 4, 6, 8]), (False, [6])])

    def test_a_missing_or_unreadable_report_proves_nothing(self):
        for mode in ("missing", "malformed", "wrongshape"):
            with self.subTest(mode=mode):
                repo = self.repo(self.GOOD_BESIDE_BAD, args="--report={report} --mode=" + mode)
                code, out = self.perturb(repo)
                self.assertEqual(code, 1, out)
                self.assertIn("test/a.test.ts:4: " + self.LIMIT.format(2), out)
                self.assertEqual(out.count("BLOCKED"), 1, out)
                # Nothing was proven, so every assertion went through the one-by-one path.
                self.assertEqual(self.shape(repo), [(False, []), (True, [2, 4, 6]), (False, [2]), (False, [4]), (False, [6])])

    def test_a_sentinel_in_a_passed_test_is_not_proof(self):
        repo = self.repo(self.GOOD_BESIDE_BAD, args="--report={report} --mode=echo")
        self.expect(self.perturb(repo), 1, "test/a.test.ts:4: " + self.LIMIT.format(2))

    def test_a_failure_without_the_sentinel_is_not_proof(self):
        # Some other test fails in the batch run, for its own reasons.
        repo = self.repo(self.GOOD_BESIDE_BAD, args="--report={report} --mode=noise")
        self.expect(self.perturb(repo), 1, "test/a.test.ts:4: " + self.LIMIT.format(2))

    def test_a_report_of_a_run_that_exited_zero_is_not_proof(self):
        repo = self.repo(self.GOOD_BESIDE_BAD, args="--report={report} --mode=exit0")
        self.expect(self.perturb(repo), 1, "test/a.test.ts:4: " + self.LIMIT.format(2))

    def test_a_round_that_times_out_proves_nothing(self):
        # The runner writes a complete report and then hangs: a timed-out run is not read.
        repo = self.repo(self.GOOD_BESIDE_BAD, args="--report={report} --mode=hang", timeoutSeconds=1)
        code, out = self.perturb(repo)
        self.assertEqual(code, 1, out)
        self.assertIn("test/a.test.ts:4: " + self.LIMIT.format(2), out)
        self.assertEqual(self.shape(repo)[2:], [(False, [2]), (False, [4]), (False, [6])])

    def test_bites_when_the_test_is_already_red(self):
        repo = self.repo("test one\n  expect(4).toBe(5)\ntest two\n  expect(1).toBe(1)\n")
        self.expect(self.perturb(repo), 1, "test/a.test.ts: the test fails before any perturbation; fix it first")
        self.assertEqual(len(self.runs(repo)), 1, "nothing is perturbed over a red baseline")

    def test_a_boolean_that_checks_nothing_is_flipped_when_checked_alone(self):
        repo = self.repo("test one\n  expect(true).toBe(true)\ntest two\n  unchecked(false).toBe(false)\n")
        self.expect(self.perturb(repo), 1, "test/a.test.ts:4: " + self.LIMIT.format("false"))
        self.assertEqual(self.runs(repo)[2], (False, {4: "true"}))

    # --- a runner without `batch` is untouched -----------------------------

    def test_a_runner_without_batch_runs_exactly_as_before(self):
        repo = self.repo(self.GOOD_BESIDE_BAD, batch=False)
        code, out = self.perturb(repo)
        self.assertEqual(code, 1, out)
        self.assertIn("test/a.test.ts:4: " + self.LIMIT.format(2), out)
        self.assertEqual(self.shape(repo), [(False, []), (False, [2]), (False, [4]), (False, [6])])
        self.assertEqual([values for _, values in self.runs(repo)[1:]], [{2: "987654"}, {4: "987654"}, {6: "987654"}])

    # --- the working copy always comes back --------------------------------

    def interrupted(self, tests, run_to_stop_at):
        repo = self.repo(tests, args="--report={report} --pause-on-run=" + str(run_to_stop_at))
        os.makedirs(os.path.join(repo.root, "tmp"))
        process = subprocess.Popen(
            ["python3", BIN, "perturb"], cwd=repo.root, env={**CLEAN_ENV, "TMPDIR": os.path.join(repo.root, "tmp")},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.kill)
        marker = os.path.join(repo.root, ".running")
        for _ in range(200):
            if os.path.exists(marker):
                break
            time.sleep(0.05)
        else:
            self.fail("the runner never started")
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), 130)
        return repo

    def test_an_interrupt_during_the_first_round_restores_the_file(self):
        text = "test one\n  expect(1).toBe(1)\ntest two\n  expect(2).toBe(2)\n"
        repo = self.interrupted(text, 2)
        with open(os.path.join(repo.root, "test/a.test.ts"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), text)
        self.assertEqual(self.leftovers(repo), [])

    def test_an_interrupt_between_rounds_restores_the_file(self):
        text = "test one\n  expect(1).toBe(1)\n  expect(2).toBe(2)\n  expect(3).toBe(3)\n"
        repo = self.interrupted(text, 3)  # baseline, round one, then round two is stopped
        with open(os.path.join(repo.root, "test/a.test.ts"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), text)
        self.assertEqual(self.shape(repo), [(False, []), (True, [2, 3, 4]), (True, [3, 4])])
        self.assertEqual(self.leftovers(repo), [])

    # --- configuration -----------------------------------------------------

    def test_a_batch_declaration_that_cannot_be_used_is_an_error_not_a_pass(self):
        for batch in ({"args": "--report={report}", "format": "xml"}, {"args": "--reporter=json", "format": "jest-json"}, "yes"):
            with self.subTest(batch=batch):
                repo = Repo(self, {**CONFIG, "perturb": {"runners": [{"files": "test/**", "command": "python3 tools/runner.py {file}", "batch": batch}]}})
                repo.write("tools/runner.py", RUNNER)
                repo.stage("test/a.test.ts", self.GOOD_FOUR)
                self.expect(repo.run("perturb"), 2, "cannot look, so it does not pass")

    # --- known limit, documented in the README -----------------------------

    def test_known_limit_an_assertion_that_only_runs_after_another_one_failed(self):
        # `whenfailed` is a catch block: it executes only when the `swallowed` assertion
        # fails. In the batch both are perturbed, so it executes and fails: proven. Alone it
        # would never run and the old lock would report it. The README states this limit.
        repo = self.repo("test one\n  swallowed(4).toBe(4)\n  whenfailed(5).toBe(5)\n")
        code, out = self.perturb(repo)
        self.assertEqual(code, 1, out)
        self.assertIn("test/a.test.ts:2: " + self.LIMIT.format(4), out)
        self.assertNotIn("test/a.test.ts:3:", out)


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
