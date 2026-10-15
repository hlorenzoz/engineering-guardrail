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
#   guarded(A).toBe(B)               an assertion in a try whose catch block runs and rethrows
#   whenfailed(A).toBe(B)            the catch block: runs only after a guarded or swallowed failure
#   afterfailed(A).toBe(B)           an afterEach that checks the result: runs only if the test failed
#   setflag / unlessflag(A).toBe(B)  a flag a test sets when it gets that far; the second runs only without it
#   twice(A).toBe(B)                 one line that runs twice: swallowed in the normal flow, and again
#                                    (surfacing) after the test failed
# Reaching an assertion prints its marker when the expected value was wrapped by the lock
# (console.log(...), value), as a real runner prints console.log.
# With --report=PATH it also writes a Jest-shaped JSON report; --mode=... breaks things on purpose
# (echo: a passed test carries the message; exit0: the report claims a failure, the exit code says 0;
# silent: console.log prints nothing; quiet: it prints only when --console is also given (a reporter
# that hides the output of passing tests, as Vitest's does when an AI agent runs it);
# nolog: a test that fails if console.log is called;
# echo-source: the runner prints the source it read, as a code frame would).
import json, os, re, sys, time

args = sys.argv[1:]
path = args[0]
opts = {}
for arg in args[1:]:
    if arg.startswith("--"):
        key, _, value = arg[2:].partition("=")
        opts[key] = value or "1"
mode = opts.get("mode", "")
STEP = re.compile(r"^\s*(expect|soft|unchecked|swallowed|guarded|whenfailed|afterfailed|unlessflag|twice)\(([^()]*)\)\.(\w+)\((.*)\)\s*$")
WRAP = re.compile(r"^\(console\.log\((.*?)\), (.*)\)$")


def bare(text):
    return text.strip().strip("'\"")


def unwrap(raw):
    wrapped = WRAP.match(raw.strip())
    if not wrapped:
        return raw.strip(), None
    return wrapped.group(2).strip(), "".join(re.findall(r'"([^"]*)"', wrapped.group(1)))


tests = []
source = open(path, encoding="utf-8").read()
for number, line in enumerate(source.split("\n"), start=1):
    if line.startswith("test "):
        tests.append((line[5:], []))
    elif tests and line.strip() == "setflag":
        tests[-1][1].append((number, "setflag", "", "", "", "", None))
    elif tests and STEP.match(line):
        kind, actual, matcher, expected = STEP.match(line).groups()
        inner, marker = unwrap(expected)
        tests[-1][1].append((number, kind, bare(actual), matcher, bare(inner), inner, marker))


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


perturbed, results, marked, logged = {}, [], False, False
flag = False
for name, steps in tests:
    for number, kind, actual, matcher, expected, raw, marker in steps:
        if kind != "setflag" and actual != expected:
            perturbed[number] = raw
        marked = marked or marker is not None
    messages, swallowed, guarded, retry, late = [], False, False, [], []

    def reach(step):
        global logged
        if step[6] is not None and mode != "silent" and ("quiet" not in opts or "console" in opts):
            print(step[6])
            logged = True

    def check(step, swallow=False):
        number, kind, actual, matcher, expected, raw, marker = step
        if agrees(matcher, actual, expected):
            return True
        if not swallow:
            messages.append(describe(matcher, actual, expected))
        return False

    for step in steps:
        number, kind, actual, matcher, expected, raw, marker = step
        if kind == "setflag":
            if not guarded:
                flag = True
            continue
        if guarded and kind != "whenfailed":
            continue
        if kind == "whenfailed" and not (swallowed or guarded):
            continue
        if kind == "unlessflag" and flag:
            continue
        if kind == "afterfailed":
            late.append(step)
            continue
        reach(step)
        if kind == "unchecked":
            if mode == "echo" and actual != expected:
                results.append(("echoed " + name, "passed", [describe(matcher, actual, expected)]))
            if mode == "exit0" and "report" in opts and actual != expected:  # a runner that lies
                results.append(("claimed " + name, "failed", [describe(matcher, actual, expected)]))
            continue
        if kind == "twice":
            check(step, swallow=True)
            retry.append(step)
            continue
        if kind == "swallowed":
            if not check(step, swallow=True):
                swallowed = True
            continue
        if kind == "guarded":
            if not check(step):
                guarded = True
            continue
        if not check(step):
            if kind != "soft":
                break
    if messages:
        for step in retry + late:
            reach(step)
            check(step)
    results.append((name, "failed" if messages else "passed", messages))

if mode == "nolog" and logged:
    results.append(("console.log must not be called", "failed", ["expected console.log not to be called"]))
if mode == "echo-source":
    print(source)
if "report" in opts and perturbed and mode == "noise":
    results.append(("noise", "failed", ["boom"]))

with open(".runlog", "a", encoding="utf-8") as log:
    log.write(json.dumps({"report": "report" in opts, "perturbed": perturbed, "marked": marked,
                          "git": sorted(k for k in os.environ if k.startswith("GIT_"))}) + "\n")
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
        # Refused for the right reason: the lock found production code and no test, not any other failure.
        self.expect(repo.gate("fix: a, see no-test: rename only, covered by types\n"), 1, "production file(s) staged and no test")
        self.expect(repo.gate("fix: a\n\nno-test: n/a\n"), 1, "production file(s) staged and no test")

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

    def test_restores_the_modification_time_too(self):
        # The time budget compares a test file's modification time with the report the related
        # tests wrote earlier in the same commit: a perturbation that left the file "newer" would
        # make every file it touched look changed after its own report.
        repo = Repo(self)
        repo.stage("test/a.test.py", self.script(True))
        target = os.path.join(repo.root, "test/a.test.py")
        past = time.time() - 3600
        os.utime(target, (past, past))
        before = os.stat(target).st_mtime_ns
        repo.run("perturb")
        self.assertEqual(os.stat(target).st_mtime_ns, before)

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

    def repo(self, tests, args="--report={report}", batch=True, command_args="", baseline_args=None, **perturb):
        # `args` reach the runner only in batch rounds; `command_args` in every run, baseline included.
        runner = {"files": "test/**", "command": ("python3 tools/runner.py {file} " + command_args).strip()}
        if batch:
            runner["batch"] = {"args": args, "format": "jest-json"}
            if baseline_args is not None:
                runner["batch"]["baselineArgs"] = baseline_args
        repo = Repo(self, {**CONFIG, "perturb": {"runners": [runner], "max": 12, **perturb}})
        repo.write("tools/runner.py", RUNNER)
        repo.stage("test/a.test.ts", tests)
        return repo

    def runs(self, repo):
        """What the runner was asked to do: (report requested, {line: expected value}) per run."""
        with open(os.path.join(repo.root, ".runlog"), encoding="utf-8") as handle:
            return [(run["report"], {int(k): v for k, v in run["perturbed"].items()}) for run in map(json.loads, handle)]

    def marked(self, repo):
        """Which runs had the lock's markers in the file (the instrumented baseline)."""
        with open(os.path.join(repo.root, ".runlog"), encoding="utf-8") as handle:
            return [json.loads(line)["marked"] for line in handle]

    def shape(self, repo):
        """The same, reduced to (report requested, [lines]) to compare with what is expected."""
        return [(report, sorted(lines)) for report, lines in self.runs(repo)]

    def leftovers(self, repo):
        """Files the lock left in the system temporary folder (TMPDIR is private to the run)."""
        return os.listdir(os.path.join(repo.root, "tmp"))

    def perturb(self, repo, *args, env=None):
        os.makedirs(os.path.join(repo.root, "tmp"), exist_ok=True)
        return repo.run("perturb", *args, env={"TMPDIR": os.path.join(repo.root, "tmp"), **(env or {})})

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
        # The sentinel is learned from a first run, never written here: the test holds whatever the
        # lock's base and stride become. A second file that already contains it must not get it.
        tests = "test one\n  expect(1).toBe(1)\ntest two\n  expect(2).toBe(2)\n"
        first = self.repo(tests)
        self.expect(self.perturb(first), 0)
        taken = self.runs(first)[1][1][2]  # the sentinel the first assertion (line 2) was given
        second = self.repo("// " + taken + " is in the file\n" + tests)
        self.expect(self.perturb(second), 0)
        values = list(self.runs(second)[1][1].values())
        self.assertNotIn(taken, values)
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
        # The instrumented baseline fails, and so does the plain one that confirms it.
        self.assertEqual(self.shape(repo), [(False, [2]), (False, [2])], "nothing is perturbed over a red baseline")
        self.assertEqual(self.marked(repo), [True, False])

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
        # One assertion per run, replaced by a value that is not the one it expected (the digits are the lock's own).
        alone = self.runs(repo)[1:]
        self.assertEqual([list(values) for _, values in alone], [[2], [4], [6]])
        for (_, values), original in zip(alone, ("1", "2", "3")):
            (replacement,) = values.values()
            self.assertNotEqual(replacement, original)
            self.assertRegex(replacement, r"^\d+$")

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

    # --- an assertion must be reached when nothing is perturbed ------------
    #
    # Each case puts a good assertion in the batch that makes the other one run. The
    # old lock, which perturbs one assertion at a time, reports the other one.

    def test_bites_on_an_assertion_in_a_catch_block(self):
        repo = self.repo("test one\n  guarded(4).toBe(4)\n  whenfailed(5).toBe(5)\ntest two\n  expect(1).toBe(1)\n")
        code, out = self.perturb(repo)
        self.assertEqual(code, 1, out)
        self.assertIn("test/a.test.ts:3: " + self.LIMIT.format(5), out)
        self.assertEqual(out.count("BLOCKED"), 1, out)
        # The catch block was not reached in the baseline, so it never joined the batch.
        self.assertEqual(self.shape(repo), [(False, []), (True, [2, 5]), (False, [3])])
        self.assertEqual(self.marked(repo), [True, False, False])

    def test_bites_on_an_assertion_in_an_after_each_that_checks_the_result(self):
        repo = self.repo("test one\n  expect(4).toBe(4)\n  afterfailed(5).toBe(5)\ntest two\n  expect(1).toBe(1)\n")
        code, out = self.perturb(repo)
        self.assertEqual(code, 1, out)
        self.assertIn("test/a.test.ts:3: " + self.LIMIT.format(5), out)
        self.assertEqual(out.count("BLOCKED"), 1, out)
        self.assertEqual(self.shape(repo), [(False, []), (True, [2, 5]), (False, [3])])

    def test_bites_on_an_assertion_that_runs_only_when_a_flag_was_not_set(self):
        # Test one sets the flag when it gets to its end; the failing assertion of the batch
        # stops it before, and the assertion behind `if (!flag)` would run.
        repo = self.repo("test one\n  expect(4).toBe(4)\n  setflag\ntest two\n  unlessflag(5).toBe(5)\ntest three\n  expect(1).toBe(1)\n")
        code, out = self.perturb(repo)
        self.assertEqual(code, 1, out)
        self.assertIn("test/a.test.ts:5: " + self.LIMIT.format(5), out)
        self.assertEqual(out.count("BLOCKED"), 1, out)
        self.assertEqual(self.shape(repo), [(False, []), (True, [2, 7]), (False, [5])])

    def test_a_marker_in_the_echoed_source_is_not_a_marker_from_running(self):
        # A runner that prints the source it read (a code frame) must not fake a "seen".
        repo = self.repo(
            "test one\n  guarded(4).toBe(4)\n  whenfailed(5).toBe(5)\ntest two\n  expect(1).toBe(1)\n",
            command_args="--mode=echo-source",
        )
        self.expect(self.perturb(repo), 1, "test/a.test.ts:3: " + self.LIMIT.format(5))

    def test_known_limit_one_line_that_runs_twice_swallowed_and_then_surfacing(self):
        # The same assertion line runs in the normal flow, where its failure is swallowed
        # (so it is seen, and alone it would stay green), and once more after the test has
        # failed, where it surfaces. Reaching the line in the baseline does not tell the two
        # contexts apart. The README states this limit; this pins it.
        repo = self.repo("test one\n  twice(5).toBe(5)\n  expect(4).toBe(4)\ntest two\n  expect(1).toBe(1)\n")
        code, out = self.perturb(repo)
        self.assertEqual(code, 0, out)
        self.assertNotIn("test/a.test.ts:2:", out)

    def test_an_assertion_that_is_seen_and_good_is_proven_in_the_batch(self):
        repo = self.repo(self.GOOD_FOUR)
        self.expect(self.perturb(repo), 0)
        self.assertEqual(self.marked(repo), [True, False])

    # --- when the instrumented baseline cannot tell -----------------------

    def test_a_test_that_fails_when_console_log_is_called_gets_todays_verdicts(self):
        repo = self.repo(self.GOOD_BESIDE_BAD, command_args="--mode=nolog")
        code, out = self.perturb(repo)
        self.assertEqual(code, 1, out)
        self.assertIn("test/a.test.ts:4: " + self.LIMIT.format(2), out)
        self.assertEqual(out.count("BLOCKED"), 1, out)
        self.assertIn("note: test/a.test.ts: the batch could not tell which assertions run", out)
        # Instrumented baseline (fails), plain baseline (passes), then each one alone.
        self.assertEqual(self.shape(repo), [(False, []), (False, []), (False, [2]), (False, [4]), (False, [6])])
        self.assertEqual(self.marked(repo), [True, False, False, False, False])

    def test_no_marker_in_the_output_gets_todays_verdicts_at_no_extra_run(self):
        repo = self.repo(self.GOOD_BESIDE_BAD, command_args="--mode=silent")
        code, out = self.perturb(repo)
        self.assertEqual(code, 1, out)
        self.assertIn("test/a.test.ts:4: " + self.LIMIT.format(2), out)
        self.assertEqual(out.count("BLOCKED"), 1, out)
        self.assertIn("note: test/a.test.ts: the batch could not tell which assertions run", out)
        self.assertEqual(self.shape(repo), [(False, []), (False, [2]), (False, [4]), (False, [6])])
        self.assertEqual(self.marked(repo), [True, False, False, False])

    def test_a_reporter_that_hides_the_output_of_passing_tests_needs_baseline_args(self):
        # Vitest 4 swaps in a quiet reporter when an AI agent runs it: the markers of passing
        # tests never reach the output. `baselineArgs` asks for a reporter that shows them.
        quiet = self.repo(self.GOOD_FOUR, command_args="--quiet")
        code, out = self.perturb(quiet)
        self.assertEqual(code, 0, out)
        self.assertIn("the batch could not tell which assertions run", out)
        self.assertEqual(self.shape(quiet), [(False, []), (False, [2]), (False, [4]), (False, [6]), (False, [8])])
        loud = self.repo(self.GOOD_FOUR, command_args="--quiet", baseline_args="--console")
        self.expect(self.perturb(loud), 0)
        self.assertEqual(self.shape(loud), [(False, []), (True, [2, 4, 6, 8])])

    def test_baseline_args_that_are_not_a_string_are_an_error(self):
        repo = Repo(self, {**CONFIG, "perturb": {"runners": [{"files": "test/**", "command": "python3 tools/runner.py {file}", "batch": {"args": "--report={report}", "format": "jest-json", "baselineArgs": 3}}]}})
        repo.write("tools/runner.py", RUNNER)
        repo.stage("test/a.test.ts", self.GOOD_FOUR)
        self.expect(repo.run("perturb"), 2, "cannot look, so it does not pass")

    # --- the runner does not see the hook's git environment -----------------

    def test_the_runner_is_launched_without_the_git_variables_of_a_hook(self):
        repo = self.repo(self.GOOD_FOUR)
        git_dir = os.path.join(repo.root, ".git")
        env = {"GIT_DIR": git_dir, "GIT_WORK_TREE": repo.root, "GIT_INDEX_FILE": os.path.join(git_dir, "index"), "GIT_AUTHOR_NAME": "someone"}
        self.expect(self.perturb(repo, env=env), 0)
        with open(os.path.join(repo.root, ".runlog"), encoding="utf-8") as handle:
            seen = [json.loads(line)["git"] for line in handle]
        self.assertEqual(seen, [[], []], "the runner saw GIT_* variables")

    def test_a_runner_without_batch_is_launched_without_them_too(self):
        repo = self.repo(self.GOOD_BESIDE_BAD, batch=False)
        self.perturb(repo, env={"GIT_DIR": os.path.join(repo.root, ".git"), "GIT_WORK_TREE": repo.root})
        with open(os.path.join(repo.root, ".runlog"), encoding="utf-8") as handle:
            self.assertEqual([json.loads(line)["git"] for line in handle], [[]] * 4)


class TimeBudget(Scenario):
    """`commit-gate` and the durations the related-tests hook wrote into a Jest-shaped report
    during this same commit: a new or changed test that is slow refuses the commit."""

    BUDGET = {"report": "tests/report_related.json", "format": "jest-json", "testMs": 1000, "fileMs": 3000}
    REPORT = "tests/report_related.json"
    FILE = "test/a.test.ts"
    HATCH = "slow-test: {name} | {duration} | {reason}\n"
    REASON = "talks to the real database"

    def repo(self, tests, staged=None, budget=BUDGET, report=True, age=60):
        """A repository with `tests` ({path: [(full name, ms)]}) in the report and `staged`
        (default: all of them) staged. `age` is how many seconds newer than the staged files the
        report is: negative means the report is older than they are."""
        repo = Repo(self, {**CONFIG, "timeBudget": budget} if budget is not None else CONFIG)
        for path in tests if staged is None else staged:
            repo.stage(path, "it('x', () => { expect(f()).toBe(1) })\n")
        if report:
            document = {"testResults": [
                {"name": os.path.join(repo.root, path), "assertionResults": [
                    {"title": name.split(" > ")[-1], "fullName": name, "status": "passed", "failureMessages": [], "duration": ms}
                    for name, ms in results]}
                for path, results in tests.items()]}
            repo.write(self.REPORT, json.dumps(document))
            now = time.time()
            for path in tests:
                if os.path.exists(os.path.join(repo.root, path)):  # a file of the report that is not staged has none
                    os.utime(os.path.join(repo.root, path), (now, now))
            os.utime(os.path.join(repo.root, self.REPORT), (now + age, now + age))
        return repo

    def hatch(self, name, duration, reason=REASON):
        return "test: more\n\n" + self.HATCH.format(name=name, duration=duration, reason=reason)

    # --- what bites ---------------------------------------------------------

    def test_bites_on_a_staged_test_over_the_test_budget(self):
        limit = self.BUDGET["testMs"]
        repo = self.repo({self.FILE: [("adds", 5), ("adds twice", limit + 1)]})
        outcome = repo.gate("test: more\n")
        self.expect(outcome, 1, f'{self.FILE}: "adds twice" took {limit + 1} ms, over the {limit} ms budget for one test')
        self.assertNotIn('"adds" took', outcome[1])
        self.assertIn("slow-test:", outcome[1])

    def test_bites_on_a_staged_file_whose_tests_add_up_over_the_file_budget(self):
        each, limit = self.BUDGET["testMs"], self.BUDGET["fileMs"]
        count = limit // each + 1
        repo = self.repo({self.FILE: [(f"case {n}", each) for n in range(count)]})
        self.expect(repo.gate("test: more\n"), 1, f"{self.FILE}: its tests add up to {each * count} ms, over the {limit} ms budget for one file")

    def test_bites_on_a_hatch_that_names_the_test_but_states_no_duration(self):
        limit = self.BUDGET["testMs"]
        repo = self.repo({self.FILE: [("adds twice", limit + 1)]})
        for duration in ("", "slow", "6200", "-5s", "ms"):
            with self.subTest(duration=duration):
                outcome = repo.gate(self.hatch("adds twice", duration))
                self.expect(outcome, 1, "slow-test: a measured duration is missing")
                self.assertIn('"adds twice" took', outcome[1])  # and the test is still not excused

    # --- what stays quiet ----------------------------------------------------

    def test_stays_quiet_under_the_budget_and_exactly_at_it(self):
        each, limit = self.BUDGET["testMs"], self.BUDGET["fileMs"]
        repo = self.repo({self.FILE: [(f"case {n}", each) for n in range(limit // each)]})  # each at its limit, the file at its limit
        self.expect(repo.gate("test: more\n"), 0)

    def test_stays_quiet_on_a_slow_test_in_a_file_this_commit_does_not_stage(self):
        limit = self.BUDGET["testMs"]
        repo = self.repo({self.FILE: [("adds", 5)], "test/b.test.ts": [("slow", limit * 10)]}, staged=[self.FILE])
        self.expect(repo.gate("test: more\n"), 0)

    def test_stays_quiet_when_the_lock_is_not_configured(self):
        repo = self.repo({self.FILE: [("slow", 10**6)]}, budget=None)
        self.expect(repo.gate("test: more\n"), 0)

    def test_a_test_without_a_duration_costs_nothing(self):
        repo = self.repo({self.FILE: [("skipped", None), ("adds", 5)]})
        self.expect(repo.gate("test: more\n"), 0)

    # --- the hatch -----------------------------------------------------------

    def test_a_hatch_with_the_name_a_duration_and_a_reason_opens_it(self):
        limit = self.BUDGET["testMs"]
        repo = self.repo({self.FILE: [("adds twice", limit + 400)]})
        self.expect(repo.gate(self.hatch("adds twice", f"{limit + 400}ms")), 0)

    def test_a_hatch_may_name_a_test_by_a_substring_and_state_seconds(self):
        limit = self.BUDGET["testMs"]
        repo = self.repo({self.FILE: [("pricing > adds twice", limit * 2)]})
        self.expect(repo.gate(self.hatch("adds tw", f"{limit * 2 / 1000}s")), 0)

    def test_a_substring_that_matches_two_slow_tests_is_not_a_name(self):
        limit = self.BUDGET["testMs"]
        repo = self.repo({self.FILE: [("adds once", limit + 1), ("adds twice", limit + 1)]})
        self.expect(repo.gate(self.hatch("adds", f"{limit + 1}ms")), 1, "matches 2 tests over budget")

    def test_a_full_name_wins_over_a_longer_name_that_contains_it(self):
        limit = self.BUDGET["testMs"]
        repo = self.repo({self.FILE: [("adds", limit + 1), ("adds twice", limit + 1)]})
        outcome = repo.gate(self.hatch("adds", f"{limit + 1}ms"))
        self.expect(outcome, 1, '"adds twice" took')  # only the one named is excused
        self.assertNotIn('"adds" took', outcome[1])

    def test_bites_on_a_hatch_for_a_test_that_is_not_over_budget(self):
        limit = self.BUDGET["testMs"]
        repo = self.repo({self.FILE: [("adds", 5), ("slow", limit + 1)]})
        message = self.hatch("slow", f"{limit + 1}ms") + self.HATCH.format(name="adds", duration=f"{limit + 1}ms", reason=self.REASON)
        self.expect(repo.gate(message), 1, 'slow-test: "adds" names no test over budget')

    def test_bites_on_a_hatch_without_a_real_reason(self):
        limit = self.BUDGET["testMs"]
        repo = self.repo({self.FILE: [("slow", limit + 1)]})
        self.expect(repo.gate(self.hatch("slow", f"{limit + 1}ms", "n/a")), 1, "slow-test: a reason of at least")

    def test_bites_on_a_hatch_that_states_a_duration_inside_the_budget(self):
        limit = self.BUDGET["testMs"]
        repo = self.repo({self.FILE: [("slow", limit + 1)]})
        self.expect(repo.gate(self.hatch("slow", f"{limit}ms")), 1, f"states {limit} ms, which is not over the {limit} ms budget")

    def test_bites_on_a_hatch_that_does_not_name_a_test_or_is_not_in_three_parts(self):
        limit = self.BUDGET["testMs"]
        repo = self.repo({self.FILE: [("slow", limit + 1)]})
        for body in (f" | {limit + 1}ms | {self.REASON}", f"slow {limit + 1}ms {self.REASON}", f"slow | {limit + 1}ms"):
            with self.subTest(body=body):
                outcome = repo.gate(f"test: more\n\nslow-test: {body}\n")
                self.expect(outcome, 1, 'slow-test: expected "<test name> | <measured duration')
                self.assertIn('"slow" took', outcome[1])

    def test_a_hatch_must_start_the_line(self):
        limit = self.BUDGET["testMs"]
        repo = self.repo({self.FILE: [("slow", limit + 1)]})
        self.expect(repo.gate("test: more, see " + self.HATCH.format(name="slow", duration=f"{limit + 1}ms", reason=self.REASON)), 1, '"slow" took')

    def test_a_hatch_opens_a_file_over_its_budget_by_the_path(self):
        each, limit = self.BUDGET["testMs"], self.BUDGET["fileMs"]
        count = limit // each + 1
        repo = self.repo({self.FILE: [(f"case {n}", each) for n in range(count)]})
        self.expect(repo.gate(self.hatch(self.FILE, f"{each * count}ms")), 0)

    def test_a_duration_with_a_fraction_is_shown_with_it(self):
        limit = self.BUDGET["testMs"]
        repo = self.repo({self.FILE: [("slow", limit + 0.5)]})
        self.expect(repo.gate("test: more\n"), 1, f'"slow" took {limit}.5 ms')

    def test_a_test_is_named_by_its_ancestors_and_title_when_the_report_has_no_full_name(self):
        limit = self.BUDGET["testMs"]
        repo = self.repo({self.FILE: [("pricing adds", limit + 1)]})
        with open(os.path.join(repo.root, self.REPORT), encoding="utf-8") as handle:
            document = json.load(handle)
        for test in document["testResults"][0]["assertionResults"]:
            del test["fullName"]
            test.update(title="adds", ancestorTitles=["pricing"])
        repo.write(self.REPORT, json.dumps(document))
        os.utime(os.path.join(repo.root, self.REPORT), (time.time() + 60,) * 2)
        self.expect(repo.gate("test: more\n"), 1, '"pricing adds" took')

    # --- when the report cannot be trusted -----------------------------------

    def test_a_missing_report_is_a_note_not_a_failure(self):
        repo = self.repo({self.FILE: [("slow", 10**6)]}, report=False)
        self.expect(repo.gate("test: more\n"), 0, f"{self.REPORT} does not exist")

    def test_a_report_older_than_the_staged_file_is_not_evidence(self):
        repo = self.repo({self.FILE: [("slow", 10**6)]}, age=-3600)
        self.expect(repo.gate("test: more\n"), 0, f"{self.FILE}: changed after {self.REPORT} was written")

    def test_a_staged_test_the_report_does_not_have_is_a_note_not_a_failure(self):
        limit = self.BUDGET["testMs"]
        repo = self.repo({self.FILE: [("adds", 5)]}, staged=[self.FILE, "e2e/flow.spec.ts"])
        self.expect(repo.gate("test: more\n"), 0, "e2e/flow.spec.ts: not in tests/report_related.json")
        slow = self.repo({self.FILE: [("slow", limit + 1)]}, staged=[self.FILE, "e2e/flow.spec.ts"])
        self.expect(slow.gate("test: more\n"), 1, '"slow" took')  # the one in the report is still judged

    def test_a_report_that_is_not_a_report_cannot_be_looked_at(self):
        repo = self.repo({self.FILE: [("adds", 5)]})
        repo.write(self.REPORT, "{ not json")
        self.expect(repo.gate("test: more\n"), 1, f"{self.REPORT} is not a Jest-shaped JSON report")

    def test_a_commit_that_stages_no_test_is_not_judged_whatever_the_report_is(self):
        repo = self.repo({self.FILE: [("adds", 5)]}, staged=[])
        repo.write(self.REPORT, "{ not json")
        repo.stage("docs/a.md", "text\n")
        self.expect(repo.gate("docs: a\n\n" + self.HATCH.format(name="gone", duration="9s", reason=self.REASON)), 0)

    # --- defaults and configuration -------------------------------------------

    def test_the_budget_defaults_are_five_seconds_a_test_and_twenty_a_file(self):
        block = {"report": self.REPORT, "format": "jest-json"}
        self.expect(self.repo({self.FILE: [("a", 5000)] * 4}, budget=block).gate("test: more\n"), 0)
        self.expect(self.repo({self.FILE: [("a", 5001)]}, budget=block).gate("test: more\n"), 1, "over the 5000 ms budget for one test")
        self.expect(self.repo({self.FILE: [("a", 5000)] * 4 + [("b", 1)]}, budget=block).gate("test: more\n"), 1, "over the 20000 ms budget for one file")

    def test_a_budget_that_cannot_be_used_is_an_error_not_a_pass(self):
        for budget in (
            {"format": "jest-json"},
            {"report": self.REPORT, "format": "xml"},
            {"report": self.REPORT, "format": "jest-json", "testMs": 0},
            {"report": self.REPORT, "format": "jest-json", "fileMs": "20s"},
            {"report": self.REPORT, "format": "jest-json", "testMS": 5},
            "yes",
        ):
            with self.subTest(budget=budget):
                repo = self.repo({self.FILE: [("adds", 5)]}, budget=budget)
                self.expect(repo.gate("test: more\n"), 2, "cannot look, so it does not pass")


class PerfGate(Scenario):
    """`guardrail perf-gate`: the push suite's timing record. It appends one line per full run to a
    history and compares it with the previous line of the same label. Not a lock: it warns."""

    REPORT, HISTORY = "tests/report_full.json", "tests/perf_history.jsonl"

    @staticmethod
    def perf():
        """The module under test, for its thresholds: they are named constants, and the scenarios
        are written relative to them. (A literal check of their values is one test, below.)"""
        sys.path.insert(0, HOME)
        try:
            from guardrail import perf
            return perf
        finally:
            sys.path.remove(HOME)

    def report(self, repo, files, name=REPORT):
        """A Jest-shaped report. `files` is {path: [ms, ...]}, one test per duration."""
        repo.write(name, json.dumps({"testResults": [
            {"name": os.path.join(repo.root, path), "assertionResults": [
                {"title": f"t{n}", "fullName": f"{path} t{n}", "status": "passed", "failureMessages": [], "duration": ms}
                for n, ms in enumerate(durations)]}
            for path, durations in files.items()]}))

    def gate(self, repo, *extra, files=None, label=None):
        if files is not None:
            self.report(repo, files)
        args = ["perf-gate", "--report", self.REPORT, "--history", self.HISTORY, *extra]
        return repo.run(*(args + ["--label", label] if label else args))

    def history(self, repo):
        with open(os.path.join(repo.root, self.HISTORY), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle.read().splitlines()]

    # --- recording ---------------------------------------------------------

    def test_the_first_run_is_recorded_and_has_nothing_to_compare_with(self):
        repo = Repo(self)
        self.expect(self.gate(repo, files={"test/a.test.ts": [100, 200], "test/b.test.ts": [50]}, label="push"), 0, "no previous run")
        (entry,) = self.history(repo)
        self.assertEqual((entry["label"], entry["tests"], entry["totalMs"]), ("push", 3, 350))
        self.assertEqual(entry["files"], {"test/a.test.ts": 300, "test/b.test.ts": 50})  # relative to the repository
        self.assertRegex(entry["date"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertEqual(entry["commit"], repo_head(repo))

    def test_the_default_label_is_named_default_so_a_run_can_name_it(self):
        repo = Repo(self)
        self.gate(repo, files={"test/a.test.ts": [1000]})
        code, out = self.gate(repo, files={"test/a.test.ts": [1000]}, label="default")
        self.assertEqual(code, 0, out)
        self.assertNotIn("no previous run", out)

    def test_a_file_that_took_no_time_before_is_listed_without_a_ratio_and_does_not_warn(self):
        perf = self.perf()
        repo = Repo(self)
        now = perf.FILE_FLOOR_MS * 4
        self.gate(repo, files={"test/a.test.ts": [0], "test/b.test.ts": [10**6]})
        code, out = self.gate(repo, files={"test/a.test.ts": [now], "test/b.test.ts": [10**6]})
        self.assertEqual(code, 0, out)
        self.assertNotIn("test/a.test.ts grew", out)
        (line,) = [row for row in out.splitlines() if row.strip().startswith("test/a.test.ts")]
        self.assertNotRegex(line, r"\dx\)")  # no ratio against a duration of zero

    def test_a_test_without_a_duration_counts_as_zero(self):
        repo = Repo(self)
        repo.write(self.REPORT, json.dumps({"testResults": [{"name": os.path.join(repo.root, "test/a.test.ts"), "assertionResults": [
            {"title": "skipped", "status": "pending", "failureMessages": [], "duration": None},
            {"title": "real", "status": "passed", "failureMessages": [], "duration": 40}]}]}))
        self.expect(repo.run("perf-gate", "--report", self.REPORT, "--history", self.HISTORY), 0)
        (entry,) = self.history(repo)
        self.assertEqual((entry["tests"], entry["totalMs"]), (2, 40))

    def test_it_records_without_a_repository_and_says_no_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "r.json"), "w", encoding="utf-8") as handle:
                json.dump({"testResults": [{"name": "a.test.ts", "assertionResults": [{"title": "t", "status": "passed", "failureMessages": [], "duration": 5}]}]}, handle)
            result = subprocess.run(["python3", BIN, "perf-gate", "--report", "r.json", "--history", "h.jsonl"], cwd=tmp, capture_output=True, text=True, env=CLEAN_ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(os.path.join(tmp, "h.jsonl"), encoding="utf-8") as handle:
                entry = json.loads(handle.read())
            self.assertIsNone(entry["commit"])
            self.assertEqual(entry["files"], {"a.test.ts": 5})

    def test_each_run_appends_a_line_and_is_compared_with_the_last_one_of_its_label(self):
        repo = Repo(self)
        self.gate(repo, files={"test/a.test.ts": [500]}, label="push")
        self.gate(repo, files={"test/a.test.ts": [1000]}, label="push")
        self.gate(repo, files={"test/a.test.ts": [100_000]}, label="other")
        # The last "push" line is 1000: a run at exactly that is no growth, though it would be
        # against the first "push" line (500).
        code, out = self.gate(repo, label="push", files={"test/a.test.ts": [1000]})
        self.assertEqual(code, 0, out)
        self.assertNotIn("WARNING", out)
        self.assertEqual([e["label"] for e in self.history(repo)], ["push", "push", "other", "push"])

    def test_a_different_label_is_not_compared(self):
        repo = Repo(self)
        self.gate(repo, files={"test/a.test.ts": [1000]}, label="push")
        code, out = self.gate(repo, files={"test/a.test.ts": [10**6]}, label="release")
        self.assertEqual(code, 0, out)
        self.assertIn("no previous run", out)
        self.assertNotIn("WARNING", out)

    # --- warning ---------------------------------------------------------

    def test_warns_when_the_total_grows_more_than_the_threshold_and_exits_zero(self):
        perf = self.perf()
        repo = Repo(self)
        self.gate(repo, files={"test/a.test.ts": [10_000]})
        grown = int(10_000 * (1 + perf.TOTAL_GROWTH)) + 1
        code, out = self.gate(repo, files={"test/a.test.ts": [grown]})
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING: the total duration grew", out)
        self.assertIn(f"+{(grown - 10_000) / 10_000 * 100:.1f}%", out)
        self.assertIn(f"from 10000 ms to {grown} ms", out)

    def test_stays_quiet_at_the_total_threshold_and_below(self):
        perf = self.perf()
        repo = Repo(self)
        self.gate(repo, files={"test/a.test.ts": [10_000]})
        at = int(10_000 * (1 + perf.TOTAL_GROWTH))
        for total in (at, at - 1, 5_000):
            with self.subTest(total=total):
                code, out = self.gate(repo, files={"test/a.test.ts": [total]})
                self.assertEqual(code, 0, out)
                self.assertNotIn("WARNING", out)

    def test_warns_when_one_file_grows_more_than_the_factor(self):
        perf = self.perf()
        repo = Repo(self)
        base = perf.FILE_FLOOR_MS * 4
        self.gate(repo, files={"test/a.test.ts": [base], "test/b.test.ts": [100_000]})
        code, out = self.gate(repo, files={"test/a.test.ts": [int(base * perf.FILE_FACTOR) + 1], "test/b.test.ts": [100_000]})
        self.assertEqual(code, 0, out)
        grown = int(base * perf.FILE_FACTOR) + 1
        self.assertIn(f"WARNING: test/a.test.ts grew {grown / base:.1f}x (from {base} ms to {grown} ms)", out)
        self.assertNotIn("test/b.test.ts grew", out)
        self.assertNotIn("the total duration grew", out)  # a small file does not move the total past its threshold

    def test_stays_quiet_when_a_file_is_exactly_at_the_factor(self):
        perf = self.perf()
        repo = Repo(self)
        base = perf.FILE_FLOOR_MS * 4
        self.gate(repo, files={"test/a.test.ts": [base], "test/b.test.ts": [10**6]})
        code, out = self.gate(repo, files={"test/a.test.ts": [int(base * perf.FILE_FACTOR)], "test/b.test.ts": [10**6]})
        self.assertEqual(code, 0, out)
        self.assertNotIn("WARNING", out)

    def test_a_file_below_the_floor_is_noise_even_if_it_triples(self):
        perf = self.perf()
        repo = Repo(self)
        small = perf.FILE_FLOOR_MS // 4
        self.gate(repo, files={"test/a.test.ts": [small], "test/b.test.ts": [10**6]})
        code, out = self.gate(repo, files={"test/a.test.ts": [small * 3], "test/b.test.ts": [10**6]})
        self.assertEqual(code, 0, out)
        self.assertNotIn("WARNING", out)

    def test_a_file_that_ends_exactly_at_the_floor_is_still_noise(self):
        perf = self.perf()
        repo = Repo(self)
        self.gate(repo, files={"test/a.test.ts": [perf.FILE_FLOOR_MS // 4], "test/b.test.ts": [10**6]})
        code, out = self.gate(repo, files={"test/a.test.ts": [perf.FILE_FLOOR_MS], "test/b.test.ts": [10**6]})
        self.assertEqual(code, 0, out)
        self.assertNotIn("WARNING", out)

    def test_a_file_that_goes_from_below_the_floor_to_above_it_by_more_than_the_factor_warns(self):
        perf = self.perf()
        repo = Repo(self)
        self.gate(repo, files={"test/a.test.ts": [perf.FILE_FLOOR_MS // 4], "test/b.test.ts": [10**6]})
        code, out = self.gate(repo, files={"test/a.test.ts": [perf.FILE_FLOOR_MS * 3], "test/b.test.ts": [10**6]})
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING: test/a.test.ts grew", out)

    def test_a_new_file_is_not_a_growth_warning_but_is_listed(self):
        repo = Repo(self)
        self.gate(repo, files={"test/a.test.ts": [1000]})
        code, out = self.gate(repo, files={"test/a.test.ts": [1000], "test/new.test.ts": [50]})
        self.assertEqual(code, 0, out)
        self.assertNotIn("test/new.test.ts grew", out)
        (line,) = [line for line in out.splitlines() if "test/new.test.ts" in line]
        self.assertIn("new", line)
        self.assertNotIn("test/a.test.ts", out)  # unchanged: not a growth

    def test_strict_turns_a_warning_into_a_failure_and_still_records_the_run(self):
        perf = self.perf()
        repo = Repo(self)
        self.gate(repo, files={"test/a.test.ts": [10_000]})
        slow = {"test/a.test.ts": [int(10_000 * (1 + perf.TOTAL_GROWTH)) + 1]}
        self.expect(self.gate(repo, "--strict", files=slow), 1, "WARNING: the total duration grew")
        self.assertEqual(len(self.history(repo)), 2)

    def test_strict_is_quiet_without_a_warning(self):
        repo = Repo(self)
        self.gate(repo, files={"test/a.test.ts": [10_000]})
        self.expect(self.gate(repo, "--strict", files={"test/a.test.ts": [9_000]}), 0)

    def test_a_table_names_the_files_that_grew_most_and_only_the_top_ones(self):
        perf = self.perf()
        repo = Repo(self)
        count = perf.TOP_FILES + 2
        self.gate(repo, files={f"test/f{n:02}.test.ts": [1000] for n in range(count)})
        code, out = self.gate(repo, files={f"test/f{n:02}.test.ts": [1000 + 10 * (n + 1)] for n in range(count)})
        self.assertEqual(code, 0, out)
        listed = [n for n in range(count) if f"test/f{n:02}.test.ts" in out]
        self.assertEqual(listed, list(range(count - perf.TOP_FILES, count)))  # the ones that grew most
        self.assertLess(out.index(f"test/f{count - 1:02}.test.ts"), out.index(f"test/f{count - 2:02}.test.ts"))  # biggest first
        self.assertIn(f"{count} tests (previous {count})", out)

    def test_the_decision_is_ten_percent_on_the_total_and_double_on_a_file(self):
        repo = Repo(self)
        self.gate(repo, files={"test/a.test.ts": [1000], "test/b.test.ts": [9000]})
        self.assertNotIn("WARNING", self.gate(repo, files={"test/a.test.ts": [1000], "test/b.test.ts": [10000]})[1])  # +10.0%: not above ten
        repo2 = Repo(self)
        self.gate(repo2, files={"test/a.test.ts": [1000], "test/b.test.ts": [9000]})
        self.assertIn("WARNING: the total duration grew", self.gate(repo2, files={"test/a.test.ts": [1000], "test/b.test.ts": [10002]})[1])
        files = {f"test/f{n:02}.test.ts": [1000] for n in range(12)}
        repo4 = Repo(self)
        self.gate(repo4, files=files)
        listed = [line for line in self.gate(repo4, files={path: [1000 + 10 * (n + 1)] for n, path in enumerate(files)})[1].splitlines() if line.startswith("  test/")]
        self.assertEqual(len(listed), 10)  # the ten that grew most
        repo5 = Repo(self)
        self.gate(repo5, files={"test/a.test.ts": [40], "test/b.test.ts": [10**6]})
        out = self.gate(repo5, files={"test/a.test.ts": [90], "test/b.test.ts": [10**6]})[1]
        self.assertNotIn("WARNING", out)  # 2.25 times, but under a hundred milliseconds is noise
        out = self.gate(repo5, files={"test/a.test.ts": [210], "test/b.test.ts": [10**6]})[1]
        self.assertIn("WARNING: test/a.test.ts grew", out)  # 2.33 times and over a hundred
        for now, warns in ((2000, False), (2001, True)):  # double is not more than double
            repo3 = Repo(self)
            self.gate(repo3, files={"test/a.test.ts": [1000], "test/b.test.ts": [10**6]})
            self.assertEqual("WARNING: test/a.test.ts grew" in self.gate(repo3, files={"test/a.test.ts": [now], "test/b.test.ts": [10**6]})[1], warns)

    # --- what it refuses ----------------------------------------------------

    def test_a_report_that_is_not_a_report_is_refused_and_records_nothing(self):
        repo = Repo(self)
        for text in ("{ not json", json.dumps({"results": []}), ""):
            with self.subTest(text=text):
                repo.write(self.REPORT, text)
                self.expect(self.gate(repo), 2, f"{self.REPORT} is not a Jest-shaped JSON report")
        self.assertFalse(os.path.exists(os.path.join(repo.root, self.HISTORY)))

    def test_a_missing_report_is_refused(self):
        repo = Repo(self)
        self.expect(self.gate(repo), 2, f"{self.REPORT} is not a Jest-shaped JSON report")

    def test_a_report_with_no_tests_is_refused_rather_than_recorded_as_free(self):
        repo = Repo(self)
        self.report(repo, {})
        self.expect(self.gate(repo), 2, "has no tests")
        self.assertFalse(os.path.exists(os.path.join(repo.root, self.HISTORY)))

    def test_missing_or_unknown_options_are_a_usage_error(self):
        repo = Repo(self)
        self.report(repo, {"test/a.test.ts": [5]})
        for args in (["perf-gate"], ["perf-gate", "--report", self.REPORT], ["perf-gate", "--history", self.HISTORY], ["perf-gate", "--report", self.REPORT, "--history", self.HISTORY, "--loud"], ["perf-gate", "--rep", self.REPORT, "--history", self.HISTORY]):
            with self.subTest(args=args):
                code, out = repo.run(*args)
                self.assertEqual(code, 2, out)
                self.assertIn("usage: guardrail perf-gate", out)

    def test_a_damaged_line_of_the_history_is_skipped_with_a_note(self):
        perf = self.perf()
        repo = Repo(self)
        self.gate(repo, files={"test/a.test.ts": [1000]})
        (good,) = self.history(repo)
        lines = ["{ damaged", "", "[1]"] + [json.dumps({key: value for key, value in good.items() if key != missing}) for missing in ("label", "tests", "totalMs", "files")]
        with open(os.path.join(repo.root, self.HISTORY), "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        code, out = self.gate(repo, files={"test/a.test.ts": [1000 * 3]})
        self.assertEqual(code, 0, out)
        for number in (2, 4, 5, 6, 7, 8):  # every damaged line, and the blank one (3) is no record and no note
            self.assertIn(f"{self.HISTORY}:{number}: not a record of this tool, skipped", out)
        self.assertNotIn(f"{self.HISTORY}:3:", out)
        self.assertIn("WARNING: the total duration grew", out)  # compared with the last good line


def repo_head(repo):
    """The short commit id the scenario's repository is at."""
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo.root, capture_output=True, text=True, env=CLEAN_ENV).stdout.strip()


class FkIndex(Scenario):
    """A foreign key a migration adds needs an index that leads with its columns, in the same
    migration or in any other migration of the repository, and that no migration dropped."""

    FK_INDEX = {"migrations": ["drizzle/*.sql"], "leadingColumnsIgnored": ["organization_id"]}
    BREAK = "--> statement-breakpoint\n"
    CLIENTS = 'CREATE TABLE "clients" (\n  "organization_id" uuid NOT NULL,\n  "id" uuid NOT NULL,\n  CONSTRAINT "clients_pk" PRIMARY KEY("organization_id","id")\n);\n' + BREAK
    INVOICES = 'CREATE TABLE "invoices" (\n  "organization_id" uuid NOT NULL,\n  "id" uuid PRIMARY KEY NOT NULL,\n  "client_id" uuid NOT NULL,\n  "number" text\n);\n' + BREAK
    FK = 'ALTER TABLE "invoices" ADD CONSTRAINT "invoices_client_fk" FOREIGN KEY ("organization_id","client_id") REFERENCES "public"."clients"("organization_id","id") ON DELETE no action ON UPDATE no action;\n' + BREAK
    INDEX = 'CREATE INDEX "invoices_client_idx" ON "invoices" USING btree ("client_id");\n' + BREAK
    HATCH = "no-index: {target} | {reason}\n"
    REASON = "the table never holds more than a few rows"

    def repo(self, files, committed=(), config=FK_INDEX):
        """`committed` files are in history, `files` are staged. Both are {path: sql}."""
        repo = Repo(self, {**CONFIG, "fkIndex": config} if config is not None else CONFIG)
        for path, sql in dict(committed).items():
            repo.stage(path, sql)
        if committed:
            repo.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "feat: earlier migrations")
        for path, sql in dict(files).items():
            repo.stage(path, sql)
        return repo

    def hatch(self, target, reason=REASON):
        return "feat: migration\n\n" + self.HATCH.format(target=target, reason=reason)

    UNINDEXED = "foreign key on invoices (organization_id, client_id)"

    # --- what bites -----------------------------------------------------------

    def test_bites_on_a_foreign_key_without_an_index(self):
        repo = self.repo({"drizzle/0002_invoices.sql": self.INVOICES + self.FK}, committed={"drizzle/0001_clients.sql": self.CLIENTS})
        outcome = repo.gate("feat: migration\n")
        self.expect(outcome, 1, f"drizzle/0002_invoices.sql: {self.UNINDEXED} -> clients has no index that leads with (client_id)")
        self.assertIn("no-index: invoices.client_id | <why it needs none>", outcome[1])

    def test_bites_on_an_index_that_leads_with_another_column(self):
        sql = self.INVOICES + self.FK + 'CREATE INDEX "invoices_number_idx" ON "invoices" ("number","client_id");\n'
        self.expect(self.repo({"drizzle/0002_invoices.sql": sql}).gate("feat: migration\n"), 1, "has no index that leads with (client_id)")

    def test_bites_on_an_index_a_later_migration_dropped(self):
        committed = {
            "drizzle/0001_invoices.sql": self.INVOICES + self.INDEX,
            "drizzle/0002_drop.sql": 'DROP INDEX "invoices_client_idx";\n',
        }
        repo = self.repo({"drizzle/0003_fk.sql": self.FK}, committed=committed)
        self.expect(repo.gate("feat: migration\n"), 1, "has no index that leads with (client_id)")

    def test_bites_on_an_inline_references_and_on_an_added_column_with_one(self):
        inline = 'CREATE TABLE "notes" (\n  "id" uuid PRIMARY KEY,\n  "client_id" uuid REFERENCES "clients"("id") ON DELETE CASCADE\n);\n'
        self.expect(self.repo({"drizzle/0002_notes.sql": inline}).gate("feat: migration\n"), 1, "foreign key on notes (client_id) -> clients")
        added = 'ALTER TABLE "invoices" ADD COLUMN "note" text, ADD COLUMN "client_id" uuid REFERENCES "clients"("id");\n'
        self.expect(self.repo({"drizzle/0002_x.sql": added}).gate("feat: migration\n"), 1, "foreign key on invoices (client_id) -> clients")

    def test_bites_on_a_table_level_foreign_key_inside_create_table(self):
        sql = 'CREATE TABLE "notes" (\n  "id" uuid PRIMARY KEY,\n  "client_id" uuid,\n  CONSTRAINT "n_fk" FOREIGN KEY ("client_id") REFERENCES "clients"("id")\n);\n'
        self.expect(self.repo({"drizzle/0002_notes.sql": sql}).gate("feat: migration\n"), 1, "foreign key on notes (client_id)")

    def test_a_foreign_key_on_the_ignored_column_alone_still_needs_its_index(self):
        sql = 'CREATE TABLE "notes" (\n  "id" uuid PRIMARY KEY,\n  "organization_id" uuid REFERENCES "organizations"("id")\n);\n'
        self.expect(self.repo({"drizzle/0002_notes.sql": sql}).gate("feat: migration\n"), 1, "foreign key on notes (organization_id)")
        indexed = sql + 'CREATE INDEX "notes_org_idx" ON "notes" ("organization_id");\n'
        self.expect(self.repo({"drizzle/0002_notes.sql": indexed}).gate("feat: migration\n"), 0)

    def test_a_partial_index_does_not_cover_a_foreign_key(self):
        sql = self.INVOICES + self.FK + 'CREATE INDEX "i" ON "invoices" ("client_id") WHERE "number" IS NOT NULL;\n'
        self.expect(self.repo({"drizzle/0002_invoices.sql": sql}).gate("feat: migration\n"), 1, "has no index that leads with (client_id)")

    def test_an_expression_index_does_not_cover_a_foreign_key(self):
        sql = self.INVOICES + self.FK + 'CREATE INDEX "i" ON "invoices" (lower("client_id"::text));\n'
        self.expect(self.repo({"drizzle/0002_invoices.sql": sql}).gate("feat: migration\n"), 1, "has no index that leads with (client_id)")

    def test_a_column_that_is_not_ignored_has_to_be_in_the_index(self):
        # No "leadingColumnsIgnored": the foreign key's two columns are both required.
        config = {"migrations": ["drizzle/*.sql"]}
        alone = self.repo({"drizzle/0002_invoices.sql": self.INVOICES + self.FK + self.INDEX}, config=config)
        self.expect(alone.gate("feat: migration\n"), 1, "has no index that leads with (organization_id, client_id)")
        for columns in ('"organization_id","client_id"', '"client_id","organization_id"'):  # in any order
            with self.subTest(columns=columns):
                both = self.INDEX.replace('("client_id")', f"({columns})")
                self.expect(self.repo({"drizzle/0002_invoices.sql": self.INVOICES + self.FK + both}, config=config).gate("feat: migration\n"), 0)

    # --- what stays quiet -------------------------------------------------------

    def test_stays_quiet_with_the_index_in_the_same_migration_before_or_after_the_key(self):
        for sql in (self.INVOICES + self.INDEX + self.FK, self.INVOICES + self.FK + self.INDEX):
            with self.subTest(sql=sql):
                self.expect(self.repo({"drizzle/0002_invoices.sql": sql}).gate("feat: migration\n"), 0)

    def test_stays_quiet_with_the_index_in_an_earlier_migration(self):
        repo = self.repo({"drizzle/0002_fk.sql": self.FK}, committed={"drizzle/0001_invoices.sql": self.INVOICES + self.INDEX})
        self.expect(repo.gate("feat: migration\n"), 0)

    def test_stays_quiet_when_a_composite_key_has_an_index_on_the_other_column_or_on_both(self):
        for index in (self.INDEX, self.INDEX.replace('("client_id")', '("organization_id","client_id")'), self.INDEX.replace('("client_id")', '("client_id","organization_id")')):
            with self.subTest(index=index):
                self.expect(self.repo({"drizzle/0002_invoices.sql": self.INVOICES + self.FK + index}).gate("feat: migration\n"), 0)

    def test_stays_quiet_when_a_primary_key_or_a_unique_constraint_covers_the_key(self):
        lines = (
            'CREATE TABLE "invoice_lines" (\n  "organization_id" uuid NOT NULL,\n  "invoice_id" uuid NOT NULL,\n  "line_no" integer NOT NULL,\n'
            '  CONSTRAINT "lines_pk" PRIMARY KEY("invoice_id","line_no")\n);\n'
            'ALTER TABLE "invoice_lines" ADD CONSTRAINT "lines_fk" FOREIGN KEY ("organization_id","invoice_id") REFERENCES "invoices"("organization_id","id");\n'
        )
        self.expect(self.repo({"drizzle/0002_lines.sql": lines}).gate("feat: migration\n"), 0)
        unique = lines.replace('PRIMARY KEY("invoice_id","line_no")', 'UNIQUE("invoice_id","line_no")')
        self.expect(self.repo({"drizzle/0002_lines.sql": unique}).gate("feat: migration\n"), 0)
        inline = 'CREATE TABLE "profiles" (\n  "id" uuid PRIMARY KEY,\n  "client_id" uuid UNIQUE REFERENCES "clients"("id")\n);\n'
        self.expect(self.repo({"drizzle/0002_profiles.sql": inline}).gate("feat: migration\n"), 0)
        one_to_one = 'CREATE TABLE "profiles" (\n  "client_id" uuid PRIMARY KEY REFERENCES "clients"("id")\n);\n'
        self.expect(self.repo({"drizzle/0002_profiles.sql": one_to_one}).gate("feat: migration\n"), 0)

    def test_stays_quiet_with_a_unique_index_and_with_the_other_forms_of_create_index(self):
        for index in (
            'CREATE UNIQUE INDEX "i" ON "invoices" ("client_id");',
            'CREATE INDEX CONCURRENTLY IF NOT EXISTS "i" ON ONLY "public"."invoices" USING btree ("client_id" DESC NULLS LAST) INCLUDE ("number");',
            'CREATE INDEX ON invoices (client_id text_pattern_ops);',
        ):
            with self.subTest(index=index):
                self.expect(self.repo({"drizzle/0002_invoices.sql": self.INVOICES + self.FK + index + "\n"}).gate("feat: migration\n"), 0)

    def test_stays_quiet_when_an_index_is_dropped_and_made_again_or_the_key_is_dropped(self):
        again = {"drizzle/0001_invoices.sql": self.INVOICES + self.INDEX, "drizzle/0002_drop.sql": 'DROP INDEX IF EXISTS "invoices_client_idx", "other";\n'}
        recreated = self.repo({"drizzle/0003_fk.sql": self.FK + self.INDEX}, committed=again)
        self.expect(recreated.gate("feat: migration\n"), 0)
        gone = self.repo({"drizzle/0003_fk.sql": self.FK, "drizzle/0004_drop_fk.sql": 'ALTER TABLE "invoices" DROP CONSTRAINT "invoices_client_fk";\n'})
        self.expect(gone.gate("feat: migration\n"), 0)
        dropped_column = self.repo({"drizzle/0003_fk.sql": self.FK, "drizzle/0004_drop.sql": 'ALTER TABLE "invoices" DROP COLUMN "client_id";\n'})
        self.expect(dropped_column.gate("feat: migration\n"), 0)

    def test_bites_after_an_index_is_lost_with_its_column_or_its_table_or_by_a_dropped_constraint(self):
        lost = [
            ("column", {"drizzle/0001_invoices.sql": self.INVOICES + self.INDEX, "drizzle/0002_swap.sql": 'ALTER TABLE "invoices" DROP COLUMN "client_id";\n--> statement-breakpoint\nALTER TABLE "invoices" ADD COLUMN "client_id" uuid;\n'}),
            ("table", {"drizzle/0001_invoices.sql": self.INVOICES + self.INDEX, "drizzle/0002_drop.sql": 'DROP TABLE IF EXISTS "invoices" CASCADE;\n'}),
            ("constraint", {"drizzle/0001_invoices.sql": self.INVOICES + 'ALTER TABLE "invoices" ADD CONSTRAINT "by_client" UNIQUE ("client_id");\n', "drizzle/0002_drop.sql": 'ALTER TABLE "invoices" DROP CONSTRAINT "by_client";\n'}),
            ("rename", {"drizzle/0001_invoices.sql": self.INVOICES + self.INDEX, "drizzle/0002_rename.sql": 'ALTER INDEX "invoices_client_idx" RENAME TO "renamed";\n', "drizzle/0003_drop.sql": 'DROP INDEX "renamed";\n'}),
        ]
        for how, committed in lost:
            with self.subTest(how=how):
                repo = self.repo({"drizzle/0009_fk.sql": self.FK}, committed=committed)
                self.expect(repo.gate("feat: migration\n"), 1, "has no index that leads with (client_id)")

    def test_reads_names_whatever_their_quotes_schema_or_case(self):
        sql = 'create table INVOICES (organization_id uuid, client_id uuid);\nALTER TABLE ONLY "public"."Invoices" ADD CONSTRAINT fk FOREIGN KEY (ORGANIZATION_ID, "client_id") REFERENCES public.CLIENTS (organization_id, id);\n'
        self.expect(self.repo({"drizzle/0002_x.sql": sql}).gate("feat: migration\n"), 1, "foreign key on invoices (organization_id, client_id) -> clients")

    def test_a_comment_inside_a_statement_hides_what_it_contains(self):
        sql = 'CREATE TABLE "t" (\n  "id" uuid PRIMARY KEY,\n  "b" uuid /* REFERENCES c(d) */,\n  "n" uuid -- REFERENCES c(d)\n);\n'
        self.expect(self.repo({"drizzle/0002_t.sql": sql}).gate("feat: migration\n"), 0)

    def test_a_statement_inside_a_function_body_is_not_executed_by_the_migration(self):
        sql = 'CREATE FUNCTION f() RETURNS void AS $body$\nBEGIN\n  ALTER TABLE "invoices" ADD COLUMN "z" uuid REFERENCES "clients"("id");\nEND $body$ LANGUAGE plpgsql;\n'
        self.expect(self.repo({"drizzle/0002_f.sql": sql}).gate("feat: migration\n"), 0)

    def test_a_function_body_with_several_statements_is_still_one_quoted_body(self):
        sql = (
            'CREATE FUNCTION f() RETURNS void AS $body$\nBEGIN\n  PERFORM 1;\n'
            '  ALTER TABLE "invoices" ADD COLUMN "z" uuid REFERENCES "clients"("id");\nEND $body$ LANGUAGE plpgsql;\n'
        )
        self.expect(self.repo({"drizzle/0002_f.sql": sql}).gate("feat: migration\n"), 0)

    def test_a_separator_inside_a_quoted_name_does_not_split_the_statement(self):
        sql = self.INVOICES + self.FK + 'CREATE INDEX "invoices_client;idx" ON "invoices" USING btree ("client_id");\n'
        self.expect(self.repo({"drizzle/0002_invoices.sql": sql}).gate("feat: migration\n"), 0)

    def test_an_expression_index_does_not_cover_a_key_on_a_column_named_like_its_function(self):
        sql = (
            'CREATE TABLE "notes" (\n  "id" uuid PRIMARY KEY,\n  "lower" uuid REFERENCES "clients"("id")\n);\n'
            'CREATE INDEX "notes_lower_idx" ON "notes" (lower("id"::text));\n'
        )
        self.expect(self.repo({"drizzle/0002_notes.sql": sql}).gate("feat: migration\n"), 1, "foreign key on notes (lower)")

    def test_judges_only_the_migrations_this_commit_stages(self):
        committed = {"drizzle/0001_invoices.sql": self.INVOICES + self.FK}  # an old, unindexed key
        repo = self.repo({"drizzle/0002_other.sql": 'CREATE TABLE "other" ("id" uuid PRIMARY KEY);\n'}, committed=committed)
        self.expect(repo.gate("feat: migration\n"), 0)

    def test_stays_quiet_outside_the_configured_migrations_and_when_not_configured(self):
        self.expect(self.repo({"db/0002_invoices.sql": self.INVOICES + self.FK, "drizzle/meta/0002_snapshot.json": "{}"}).gate("feat: migration\n"), 0)
        self.expect(self.repo({"drizzle/0002_invoices.sql": self.INVOICES + self.FK}, config=None).gate("feat: migration\n"), 0)

    def test_a_key_in_a_comment_or_a_string_is_not_a_key(self):
        sql = (
            '-- ALTER TABLE a ADD CONSTRAINT x FOREIGN KEY (b) REFERENCES c(d);\n/* FOREIGN KEY (b) REFERENCES c(d) */\n'
            'CREATE TABLE "t" ("id" uuid PRIMARY KEY, "note" text DEFAULT \'FOREIGN KEY (b) REFERENCES c(d); x\');\n'
        )
        self.expect(self.repo({"drizzle/0002_t.sql": sql}).gate("feat: migration\n"), 0)

    def test_known_limit_a_key_built_in_a_do_block_is_not_seen(self):
        sql = 'DO $$ BEGIN ALTER TABLE "invoices" ADD CONSTRAINT "x" FOREIGN KEY ("client_id") REFERENCES "clients"("id"); END $$;\n'
        self.expect(self.repo({"drizzle/0002_do.sql": sql}).gate("feat: migration\n"), 0)

    # --- the hatch ----------------------------------------------------------------

    def test_a_hatch_that_names_the_table_and_the_column_with_a_reason_opens_it(self):
        repo = self.repo({"drizzle/0002_invoices.sql": self.INVOICES + self.FK})
        self.expect(repo.gate(self.hatch("invoices.client_id")), 0)
        self.expect(repo.gate(self.hatch('"Invoices"."CLIENT_ID"')), 0)

    def test_a_hatch_without_a_real_reason_or_a_target_does_not_open_it(self):
        repo = self.repo({"drizzle/0002_invoices.sql": self.INVOICES + self.FK})
        self.expect(repo.gate(self.hatch("invoices.client_id", "n/a")), 1, "no-index: a reason of at least")
        self.expect(repo.gate("feat: x\n\nno-index: invoices | the table never holds more than a few rows\n"), 1, 'no-index: expected "<table>.<column> | <reason>"')
        self.expect(repo.gate("feat: x\n\nno-index: invoices.client_id the table never holds more than a few rows\n"), 1, 'no-index: expected "<table>.<column> | <reason>"')
        self.expect(repo.gate("feat: x\n\nno-index: invoices.client_id\n"), 1, 'no-index: expected "<table>.<column> | <reason>"')
        self.expect(repo.gate(self.hatch("invoices.number")), 1, 'no-index: "invoices.number" names no foreign key without an index')
        self.expect(repo.gate("feat: x, see " + self.HATCH.format(target="invoices.client_id", reason=self.REASON)), 1, self.UNINDEXED)

    def test_a_hatch_for_a_key_that_has_its_index_is_a_stale_line(self):
        repo = self.repo({"drizzle/0002_invoices.sql": self.INVOICES + self.FK + self.INDEX})
        self.expect(repo.gate(self.hatch("invoices.client_id")), 1, 'no-index: "invoices.client_id" names no foreign key without an index')

    def test_a_hatch_opens_only_the_key_it_names(self):
        sql = self.INVOICES + self.FK + 'ALTER TABLE "invoices" ADD COLUMN "owner_id" uuid REFERENCES "clients"("id");\n'
        outcome = self.repo({"drizzle/0002_invoices.sql": sql}).gate(self.hatch("invoices.owner_id"))
        self.expect(outcome, 1, self.UNINDEXED)
        self.assertNotIn("foreign key on invoices (owner_id)", outcome[1])

    def test_a_commit_that_stages_no_migration_is_not_judged_even_with_a_hatch(self):
        repo = self.repo({"docs/a.md": "text\n"})
        self.expect(repo.gate(self.hatch("invoices.client_id")), 0)

    # --- configuration --------------------------------------------------------------

    def test_a_configuration_that_cannot_be_used_is_an_error_not_a_pass(self):
        for config in ({}, {"migrations": []}, {"migrations": "drizzle/*.sql"}, {"migrations": ["a"], "leadingColumnsIgnored": "organization_id"}, {"migrations": ["a"], "ignored": []}, "yes"):
            with self.subTest(config=config):
                repo = self.repo({"drizzle/0002_invoices.sql": self.INVOICES}, config=config)
                self.expect(repo.gate("feat: migration\n"), 2, "cannot look, so it does not pass")


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
