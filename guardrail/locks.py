"""The locks. Each returns a list of problems; an empty list means the lock passes.

A lock never prints and never exits: the command line decides that. A lock that
cannot look (no runner, unreadable file) reports a problem. It never passes blind.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import tempfile
from typing import NamedTuple

from . import core

# --- no fake tests ---------------------------------------------------------

ALLOW_SKIP = "guardrail: allow-skip"

_FAKE_PATTERNS = [
    (re.compile(r"\b(?:describe|it|test|suite|bench)\.(?:only|skip|todo|skipIf|runIf)\b"), "focused or skipped test"),
    (re.compile(r"\b(?:xit|xdescribe|xtest|fit|fdescribe)\s*\("), "focused or skipped test"),
    (
        re.compile(
            r"expect\(\s*(true|false|null|undefined|-?\d+(?:\.\d+)?|'[^']*'|\"[^\"]*\")\s*\)"
            r"\s*\.\s*(?:toBe|toEqual|toStrictEqual)\(\s*\1\s*\)"
        ),
        "assertion that compares a literal with itself",
    ),
    (re.compile(r"expect\(\s*true\s*\)\s*\.\s*toBeTruthy\(\s*\)"), "assertion that cannot fail"),
    (
        re.compile(r"\b(?:it|test)\(\s*(['\"`]).*?\1\s*,\s*(?:async\s*)?\(\s*\)\s*=>\s*\{\s*\}\s*\)"),
        "test with an empty body",
    ),
]


def _is_comment(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*")


def fake_tests_in(text: str, only_lines: set[int] | None = None) -> list[tuple[int, str]]:
    """Find tests that cannot fail or do not run. Returns (line number, reason)."""
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        if only_lines is not None and number not in only_lines:
            continue
        if _is_comment(line) or ALLOW_SKIP in line:
            continue
        for pattern, reason in _FAKE_PATTERNS:
            if pattern.search(line):
                found.append((number, reason))
                break
    return found


def no_fake_tests(root: str, config: core.Config) -> list[str]:
    """Staged test files must not add a skipped, focused, empty or tautological test.

    Only the lines this commit adds are judged, so old code does not block new work.
    """
    problems = []
    for path in core.staged_files(root):
        if not config.is_test(path):
            continue
        added = core.staged_added_lines(root, path)
        for number, reason in fake_tests_in(core.staged_text(root, path), added):
            problems.append(f"{path}:{number}: {reason}")
    return problems


# --- commit gate: locks that read the commit message -----------------------


def fix_needs_test(root: str, config: core.Config, message: str) -> list[str]:
    """Production code staged without any test staged is refused."""
    staged = core.staged_files(root)
    production = [p for p in staged if config.is_production(p)]
    if not production or any(config.is_test(p) for p in staged):
        return []
    if core.hatch(message, "no-test"):
        return []
    return [
        f"{len(production)} production file(s) staged and no test "
        f"(first: {production[0]}). Stage the test that fails without this change, "
        "or add a line `no-test: <why no test can cover it>` to the commit message."
    ]


def size_cap(root: str, config: core.Config, message: str) -> list[str]:
    """A commit that touches too much production code must be split."""
    production = [p for p in core.staged_files(root) if config.is_production(p)]
    if not production:
        return []
    lines = core.staged_changed_lines(root, production)
    cap = config["sizeCap"]
    if len(production) <= cap["files"] and lines <= cap["lines"]:
        return []
    if core.hatch(message, "size-ok"):
        return []
    return [
        f"{len(production)} production files and {lines} changed lines "
        f"(cap: {cap['files']} files, {cap['lines']} lines). Split the commit, "
        "or add a line `size-ok: <why it cannot be split>` to the commit message."
    ]


def cross_view(root: str, config: core.Config, message: str) -> list[str]:
    """Code that computes a shared figure needs a staged test comparing its views."""
    staged = core.staged_files(root)
    problems = []
    for rule in config["crossView"]:
        touched = [p for p in staged if config.is_production(p) and core.matches(p, rule["paths"])]
        if not touched or any(core.matches(p, rule["tests"]) for p in staged):
            continue
        if core.hatch(message, "cross-view-na"):
            continue
        problems.append(
            f"[{rule.get('name', 'cross-view')}] {touched[0]} changes a figure shown in more "
            f"than one place, and no test under {rule['tests']} is staged. Stage the test that "
            "reads the figure through every view and compares them, or add a line "
            "`cross-view-na: <why no shared figure changes>` to the commit message."
        )
    return problems


def switch_on_trigger(root: str, config: core.Config, message: str) -> list[str]:
    """Something that must be turned on outside the code needs its registry entry."""
    settings = config["switchedOn"]
    staged = core.staged_files(root)
    touched = [p for p in staged if core.matches(p, settings["triggers"])]
    if not touched or settings["registry"] in staged:
        return []
    if core.hatch(message, "no-switch-on"):
        return []
    return [
        f"{touched[0]} is something that has to be switched on (applied, installed, deployed) "
        f"and {settings['registry']} is not staged. Record who switches it on and the command "
        "that proves it, or add a line `no-switch-on: <why nothing has to be switched on>`."
    ]


COMMIT_GATE = [fix_needs_test, size_cap, cross_view, switch_on_trigger]

# --- switched-on registry shape --------------------------------------------

_STATES = {"ON", "OFF", "PARTIAL"}
_FIELDS = ["State", "Who switches it on", "Check", "Last check"]


def switched_on_shape(text: str, today: datetime.date, stale_days: int) -> tuple[list[str], list[str]]:
    """Validate the registry. Returns (problems, stale notes). Age is never a problem."""
    problems, stale = [], []
    blocks = re.split(r"^### ", text, flags=re.MULTILINE)[1:]
    for block in blocks:
        name = block.splitlines()[0].strip()
        fields = dict(re.findall(r"^- \*\*([^*]+)\*\*:\s*(.*)$", block, re.MULTILINE))
        for field in _FIELDS:
            if not fields.get(field, "").strip():
                problems.append(f"{name}: missing '{field}'")
        state = fields.get("State", "").strip()
        if state and state not in _STATES:
            problems.append(f"{name}: State must be one of {sorted(_STATES)}, got '{state}'")
        if fields.get("Check", "").strip() and "`" not in fields["Check"]:
            problems.append(f"{name}: Check must be an executable command in backticks")
        last = re.match(r"(\d{4}-\d{2}-\d{2})\s*(?:→|->)\s*\S", fields.get("Last check", ""))
        if fields.get("Last check", "").strip() and not last:
            problems.append(f"{name}: Last check must be 'YYYY-MM-DD → <real output>'")
        elif last:
            age = (today - datetime.date.fromisoformat(last.group(1))).days
            if age > stale_days:
                stale.append(f"{name}: last checked {age} days ago")
    return problems, stale


def switched_on(root: str, config: core.Config) -> tuple[list[str], list[str]]:
    """Check the registry file when the project has one."""
    settings = config["switchedOn"]
    path = os.path.join(root, settings["registry"])
    if not os.path.exists(path):
        return [], []
    with open(path, encoding="utf-8") as handle:
        return switched_on_shape(handle.read(), datetime.date.today(), settings["staleDays"])


# --- live citations --------------------------------------------------------

_CITATION = re.compile(r"`([A-Za-z0-9_@$\[\]().+-][A-Za-z0-9_@$\[\]().+/-]*\.[A-Za-z0-9]+):(\d+)(?:-\d+)?`")


def citations_in(root: str, text: str, only_lines: set[int] | None = None) -> tuple[list[str], int]:
    """Check each `path:line` citation. Returns (problems, citations that could not be resolved).

    A path whose first folder does not exist in this repository belongs to another
    one and cannot be judged from here: it is counted, not failed.
    """
    problems, unresolved = [], 0
    for number, line in enumerate(text.splitlines(), start=1):
        if only_lines is not None and number not in only_lines:
            continue
        for match in _CITATION.finditer(line):
            cited, cited_line = match.group(1), int(match.group(2))
            target = os.path.join(root, cited)
            if not os.path.isfile(target):
                first = cited.split("/", 1)[0]
                if "/" in cited and os.path.exists(os.path.join(root, first)):
                    problems.append(f"line {number}: {cited} does not exist")
                else:
                    unresolved += 1
                continue
            with open(target, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
            if cited_line < 1 or cited_line > len(lines):
                problems.append(f"line {number}: {cited}:{cited_line} is past the end ({len(lines)} lines)")
            elif not lines[cited_line - 1].strip():
                problems.append(f"line {number}: {cited}:{cited_line} is a blank line")
    return problems, unresolved


def live_citations(root: str, config: core.Config, every_file: bool = False) -> tuple[list[str], int]:
    """Citations added by this commit must point at a real, non-blank line."""
    problems, unresolved = [], 0
    if every_file:
        paths = [p for p in core.git(root, "ls-files", "-z").split("\0") if p]
    else:
        paths = core.staged_files(root)
    for path in paths:
        if not core.matches(path, config["citations"]["files"]):
            continue
        if every_file:
            with open(os.path.join(root, path), encoding="utf-8", errors="replace") as handle:
                text, lines = handle.read(), None
        else:
            text, lines = core.staged_text(root, path), core.staged_added_lines(root, path)
        found, missing = citations_in(root, text, lines)
        problems += [f"{path}: {p}" for p in found]
        unresolved += missing
    return problems, unresolved


# --- expected-value perturbation -------------------------------------------

NO_PERTURB = "guardrail: no-perturb"
_NUMBER_SENTINEL, _STRING_SENTINEL = "987654", "ZZ_MUTATED_ZZ"
_EXPECTED = re.compile(
    r"\.(toBe|toEqual|toStrictEqual|toHaveLength|toBeCloseTo|toHaveBeenCalledTimes)\(\s*"
    r"(-?\d[\d_]*(?:\.\d+)?|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|true|false)(?=\s*[,)])"
)


class _Site(NamedTuple):
    """An expected value that can be replaced: its line, and where the literal sits in it."""

    number: int
    literal: str
    line: str
    start: int
    end: int

    def with_value(self, value: str) -> str:
        """The line with the literal replaced by `value`."""
        return self.line[: self.start] + value + self.line[self.end :]


def _sentinel(literal: str) -> str:
    """A value the code under test cannot produce by accident (one assertion at a time)."""
    if literal in ("true", "false"):
        return "false" if literal == "true" else "true"
    if literal[0] in "'\"":
        return literal[0] + _STRING_SENTINEL + literal[0]
    return "123456" if literal.replace("_", "") == _NUMBER_SENTINEL else _NUMBER_SENTINEL


def _sites(text: str, only_lines: set[int] | None = None) -> list[_Site]:
    """Expected values that can be replaced, one per line."""
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        if only_lines is not None and number not in only_lines:
            continue
        if _is_comment(line) or NO_PERTURB in line or ".not." in line:
            continue
        match = _EXPECTED.search(line)
        if match:
            found.append(_Site(number, match.group(2), line, match.start(2), match.end(2)))
    return found


def perturbations(text: str, only_lines: set[int] | None = None) -> list[tuple[int, str, str]]:
    """Expected values that can be replaced. Returns (line number, literal, mutated line)."""
    return [(site.number, site.literal, site.with_value(_sentinel(site.literal))) for site in _sites(text, only_lines)]


_NO_RUNNER = object()


def _runner_for(config: core.Config, path: str):
    """The runner entry for this test file; _NO_RUNNER when nothing covers it. Its
    `command` is None when the project declares the file not runnable at commit time
    (a browser suite, say)."""
    for runner in config["perturb"]["runners"]:
        if core.matches(path, [runner["files"]]):
            return runner
    return _NO_RUNNER


def _execute(command: str, path: str, root: str, timeout: int) -> tuple[int, bool, str]:
    """Run the project's test command for one file. Returns (exit code, timed out, output).

    The runner never sees the GIT_* variables a hook sets (GIT_DIR, GIT_INDEX_FILE, ...): a
    project test that runs git in a scratch folder would act on the repository being
    committed instead (a `git init` or `git config` under a hook wrote into the real one).
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        completed = subprocess.run(
            command.replace("{file}", shlex.quote(path)),
            shell=True,
            cwd=root,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        return completed.returncode, False, completed.stdout.decode("utf-8", "replace") + completed.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, True, ""


def _run(command: str, path: str, root: str, timeout: int) -> int:
    """The exit code of the test command. A timeout counts as a failure."""
    return _execute(command, path, root, timeout)[0]


# --- perturbation in batch -------------------------------------------------
#
# Running the whole file once per assertion costs a process of the project's test
# runner each. In batch mode all the assertions of a file are replaced at once, each by
# a sentinel of its own, and a structured report says which assertions failed WITH
# their own sentinel in the message. Only that counts as proof: not the exit code, not a
# line number, not a failed test without the sentinel. Whatever the batch cannot prove
# goes through the one-by-one check, so the batch never produces a problem and never
# hides one it cannot see.

# The shapes of test report the guardrail can read, for perturbation in batch and for the
# time budget alike.
REPORT_FORMATS = ("jest-json",)
# Sentinels are 988654003, 989654006, ... A matcher such as toBeCloseTo prints the
# DIFFERENCE between the received and the expected value; with sentinels one apart, a
# small received value would print another assertion's sentinel. A stride of a million
# keeps every such difference away from them.
_BATCH_BASE, _BATCH_STRIDE = 987654000, 1000003


def _batch_of(runner: dict):
    """The runner's `batch` declaration, validated; None when it has none."""
    batch = runner.get("batch")
    if batch is None:
        return None
    if not isinstance(batch, dict) or batch.get("format") not in REPORT_FORMATS or not isinstance(batch.get("args"), str):
        raise core.GuardrailError(
            "perturb.runners batch must be an object with \"args\" (a string) and \"format\" (one of "
            + ", ".join(REPORT_FORMATS) + ")"
        )
    if not isinstance(batch.get("baselineArgs", ""), str):
        raise core.GuardrailError("perturb.runners batch.baselineArgs must be a string")
    if "{report}" not in batch["args"]:
        raise core.GuardrailError("perturb.runners batch.args must contain {report}, the file the runner writes its report to")
    return batch


def batch_sentinels(sites: list[_Site], text: str) -> list[tuple[str, str]]:
    """One distinct sentinel per site: (replacement for the literal, token to look for).

    A number for numbers and booleans (a boolean cannot carry an identity; comparing it
    with a unique number fails for the same reason and the message names the number), a
    string for strings. A token that already occurs in the file is never used, so
    nothing but the replacement can put it in a message.
    """
    marks, index = [], 0
    for site in sites:
        while True:
            index += 1
            number = str(_BATCH_BASE + _BATCH_STRIDE * index)
            text_token = f"ZZ_MUTATED_{index}_ZZ"
            is_string = site.literal[0] in "'\""
            token = text_token if is_string else number
            if token not in text:
                marks.append((site.literal[0] + token + site.literal[0] if is_string else token, token))
                break
    return marks


class Outcome(NamedTuple):
    """One test of a Jest-shaped report: where it ran, its full name, how it ended, the
    messages of its failure and how long it took in milliseconds (None when the report has none)."""

    file: str
    name: str
    status: str
    messages: list
    duration: float | None


def read_jest_report(report_path: str) -> list[Outcome] | None:
    """The tests of a Jest-shaped JSON report (`testResults[].assertionResults[]` with
    `status` and `failureMessages`), which is also what Vitest's `json` reporter writes.
    The ONE reader of that format: perturbation reads failures from it, the time budget and
    the timing record read durations. None when the report is missing, unreadable or not of
    that shape: a report that cannot be read proves nothing."""
    try:
        with open(report_path, encoding="utf-8") as handle:
            document = json.load(handle)
        outcomes = []
        for file_result in document["testResults"]:
            for test in file_result["assertionResults"]:
                if not isinstance(test["status"], str) or not isinstance(test["failureMessages"], list):
                    return None
                if not all(isinstance(m, str) for m in test["failureMessages"]):
                    return None
                duration = test.get("duration")
                duration = float(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None
                title = test.get("fullName") or " ".join([*test.get("ancestorTitles", []), test.get("title", "")]).strip()
                outcomes.append(Outcome(file_result.get("name"), title, test["status"], test["failureMessages"], duration))
        return outcomes
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _failed_messages(report_path: str) -> list[str] | None:
    """The failure messages of the FAILED tests in a report; None when it cannot be read."""
    outcomes = read_jest_report(report_path)
    if outcomes is None:
        return None
    return [message for outcome in outcomes if outcome.status == "failed" for message in outcome.messages]


def _mentions(messages: list[str], token: str) -> bool:
    """Does a failure message name this sentinel? A number must not be part of a longer one."""
    pattern = re.escape(token) if not token.isdigit() else rf"(?<!\d)(?<!\d\.){token}(?!\d)(?!\.\d)"
    return any(re.search(pattern, message) for message in messages)


def _seen_token(nonce: str, index: int) -> str:
    """What a reached assertion prints. The nonce makes it unique to this run."""
    return f"ZZ_SEEN_{nonce}_{index}_ZZ"


def _seen_expression(nonce: str, index: int, literal: str) -> str:
    """The literal, wrapped so that evaluating it prints the marker and still yields the literal.

    A comma expression is valid wherever an argument is, adds nothing to the line count and
    keeps the value. The token is built at run time from three pieces, so the file's own text
    (echoed in a code frame, say) never contains it: only executing the call prints it.
    """
    return f'(console.log("ZZ_SEEN_" + "{nonce}_{index}" + "_ZZ"), {literal})'


def _baseline(root, command, extra, path, target, lines, sites, timeout, nonce) -> tuple[int, set[int]]:
    """The unperturbed run, instrumented to learn which assertions it reaches, at no extra run.

    Returns (exit code of the baseline, indexes of the sites whose marker came back). If the
    instrumented run fails the plain baseline decides: it fails too, the test is red; it
    passes, the instrumentation broke it (a test that counts console.log calls) and nothing
    is seen. Nothing seen is never an error: those assertions are checked one by one.
    `extra` is the project's `baselineArgs`: some reporters hide the output of passing tests
    (Vitest's does when an AI agent runs it), and the markers need one that shows it.
    """
    changed = list(lines)
    for index, site in enumerate(sites):
        changed[site.number - 1] = site.with_value(_seen_expression(nonce, index, site.literal))
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("\n".join(changed))
    code, _timed_out, output = _execute((command + " " + extra).strip(), path, root, timeout)
    if code == 0:
        return 0, {index for index in range(len(sites)) if _seen_token(nonce, index) in output}
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return _run(command, path, root, timeout), set()


def _prove_together(root, runner, batch, path, target, lines, sites, scratch, timeout) -> list[_Site]:
    """Round after round, replace every assertion not yet proven, each by its own
    sentinel, and keep those whose sentinel is in the message of a failed test. A proven
    assertion is restored before the next round, which is what uncovers one that was
    hidden behind it in the same test (the first failure ends a test). Returns the
    assertions the batch could not prove, in line order, for the one-by-one check."""
    marks = batch_sentinels(sites, "\n".join(lines))
    remaining = list(range(len(sites)))
    while len(remaining) > 1:  # one left is checked alone: a batch of one gains nothing
        changed = list(lines)
        for index in remaining:
            changed[sites[index].number - 1] = sites[index].with_value(marks[index][0])
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("\n".join(changed))
        # The report of an earlier round may still be there if this run writes none. It
        # cannot prove anything now: every assertion it caught is already out of `remaining`.
        if not scratch:
            scratch.append(tempfile.mkdtemp(prefix="guardrail-perturb-"))
        report = os.path.join(scratch[0], "report.json")
        command = runner["command"] + " " + batch["args"].replace("{report}", shlex.quote(report))
        code, timed_out, _output = _execute(command, path, root, timeout)
        # A run that timed out, or that reported success, cannot have caught anything.
        messages = None if timed_out or code == 0 else _failed_messages(report)
        proven = [i for i in remaining if messages and _mentions(messages, marks[i][1])]
        if not proven:
            break
        remaining = [i for i in remaining if i not in proven]
    return [sites[i] for i in remaining]


def _prove_alone(root, command, path, target, lines, site, timeout) -> list[str]:
    """Replace only this assertion's expected value, run the file once: it must fail."""
    changed = list(lines)
    changed[site.number - 1] = site.with_value(_sentinel(site.literal))
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("\n".join(changed))
    code = _run(command, path, root, timeout)
    if code == 0:
        return [
            f"{path}:{site.number}: still green with the expected value {site.literal} replaced. "
            "The assertion does not check what it claims."
        ]
    return []


def perturb_expected(root: str, config: core.Config) -> tuple[list[str], list[str]]:
    """Replace each expected value this commit adds and require its test to fail.

    A test that stays green with a wrong expected value proves nothing. Returns
    (problems, notes). The working copy is always restored.
    """
    problems, notes = [], []
    settings = config["perturb"]
    budget = settings["max"]
    nonce = secrets.token_hex(6)
    for path in core.staged_files(root):
        if not config.is_test(path):
            continue
        sites = _sites(core.staged_text(root, path), core.staged_added_lines(root, path))
        if not sites:
            continue
        runner = _runner_for(config, path)
        if runner is _NO_RUNNER:
            problems.append(f"{path}: no runner configured in .guardrail.json (perturb.runners); not observed")
            continue
        command = runner["command"]
        if command is None:
            notes.append(f"{path}: {len(sites)} assertion(s) not perturbed (declared not runnable at commit time)")
            continue
        batch = _batch_of(runner)
        target = os.path.join(root, path)
        with open(target, encoding="utf-8") as handle:
            original = handle.read()
        scratch = []  # the folder the batch reports go in, removed with the file restored

        def restore(*_args):
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(original)
            while scratch:
                shutil.rmtree(scratch.pop(), ignore_errors=True)

        previous = {s: signal.signal(s, lambda *_: (restore(), os._exit(130))) for s in (signal.SIGINT, signal.SIGTERM)}
        try:
            lines = original.split("\n")
            seen = None  # which assertions the baseline reached; only learned for a batch
            if batch and min(len(sites), budget) > 1:
                code, seen = _baseline(root, command, batch.get("baselineArgs", ""), path, target, lines, sites[:budget], settings["timeoutSeconds"], nonce)
            else:
                code = _run(command, path, root, settings["timeoutSeconds"])
            if code != 0:
                problems.append(f"{path}: the test fails before any perturbation; fix it first")
                continue
            chosen = []
            for site in sites:
                if budget == 0:
                    notes.append(f"{path}:{site.number}: not perturbed (limit of {settings['max']} per commit reached)")
                    continue
                budget -= 1
                chosen.append(site)
            if seen is not None:
                if not seen:
                    notes.append(f"{path}: the batch could not tell which assertions run (no marker came back from the runner); each is checked on its own")
                # Only an assertion the unperturbed run reached may count on the batch: one that
                # runs only after another failed (a catch block, an afterEach, `if (!flag)`) would
                # fail here and never fail alone.
                reached = [site for index, site in enumerate(chosen) if index in seen]
                unreached = [site for index, site in enumerate(chosen) if index not in seen]
                chosen = sorted(
                    _prove_together(root, runner, batch, path, target, lines, reached, scratch, settings["timeoutSeconds"]) + unreached,
                    key=lambda site: site.number,
                )
            for site in chosen:
                problems += _prove_alone(root, command, path, target, lines, site, settings["timeoutSeconds"])
        finally:
            restore()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    return problems, notes


# --- time budget for new or changed tests ----------------------------------
#
# The related-tests hook of a project runs, in the same commit, the tests related to the staged
# files, and can write a Jest-shaped report beside its normal output. This lock reads the
# durations in that report and refuses a staged test file with a test, or a total, over the
# project's budget: a slow test is caught when it is written, not months later when the suite
# takes minutes. It judges only what it can see (README, "Honest limits").

SLOW_TEST = "slow-test"
TIME_BUDGET_DEFAULTS = {"testMs": 5000, "fileMs": 20000}
_TIME_BUDGET_KEYS = {"report", "format", *TIME_BUDGET_DEFAULTS}
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*(ms|s)$")


class _Slow(NamedTuple):
    """Something over budget: a test (kind "test") or a whole staged file (kind "file")."""

    kind: str
    path: str
    name: str
    ms: float
    limit: int

    @property
    def label(self) -> str:
        return self.path if self.kind == "file" else f"{self.path} > {self.name}"


def _time_budget(config: core.Config):
    """The validated `timeBudget` block as (report path, {testMs, fileMs}); None when off."""
    raw = config["timeBudget"]
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("report"), str) or not raw["report"]:
        raise core.GuardrailError('timeBudget must be an object with "report", the file the related-tests hook writes its report to')
    if raw.get("format") not in REPORT_FORMATS:
        raise core.GuardrailError("timeBudget.format must be one of " + ", ".join(REPORT_FORMATS))
    if set(raw) - _TIME_BUDGET_KEYS:
        raise core.GuardrailError("timeBudget has unknown key(s): " + ", ".join(sorted(set(raw) - _TIME_BUDGET_KEYS)))
    budget = {**TIME_BUDGET_DEFAULTS, **{key: raw[key] for key in TIME_BUDGET_DEFAULTS if key in raw}}
    for key, value in budget.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise core.GuardrailError(f"timeBudget.{key} must be a whole number of milliseconds above zero")
    return raw["report"], budget


def _judge_slow_test_line(body: str, slow: list[_Slow]):
    """Judge one `slow-test: <name> | <duration> | <reason>` line against what is over budget.
    Returns (the item it excuses or None, problems). Its content is checked, not only its
    presence: the name must single out one item over budget, the duration must be written the
    way the report measures it and exceed that item's limit, and the reason must say something."""
    fields = [field.strip() for field in body.split("|", 2)]
    shown = f'"slow-test: {body}"'
    if len(fields) != 3 or not fields[0]:
        return None, [f'slow-test: expected "<test name> | <measured duration, e.g. 1400ms or 1.4s> | <reason>", got {shown}']
    name, duration, reason = fields
    stated = _DURATION.match(duration)
    if not stated:
        return None, [f"slow-test: a measured duration is missing in {shown}. Write it as 1400ms or 1.4s, the way the report measured it."]
    if len(reason) < core.MIN_REASON:
        return None, [f"slow-test: a reason of at least {core.MIN_REASON} characters is missing in {shown}"]
    exact = [item for item in slow if name in (item.name, item.label)]
    found = exact or [item for item in slow if name in item.label]
    if not found:
        return None, [f'slow-test: "{name}" names no test over budget (a stale line, or a typo)']
    if len(found) > 1:
        return None, [f'slow-test: "{name}" matches {len(found)} tests over budget ({", ".join(i.label for i in found)}); name one of them in full']
    item = found[0]
    stated_ms = round(float(stated.group(1)) * (1000 if stated.group(2) == "s" else 1), 3)
    if stated_ms <= item.limit:
        return None, [f'slow-test: "{name}" states {core.format_ms(stated_ms)} ms, which is not over the {item.limit} ms budget it is excused from']
    return item, []


def time_budget(root: str, config: core.Config, message: str) -> tuple[list[str], list[str]]:
    """A staged test file with a slow test, or a slow total, is refused. Returns (problems, notes).

    Only the tests of STAGED test files are judged, with the durations the related-tests hook
    wrote during this same commit. What it cannot see is a note, never a pass in silence and
    never a failure: a missing report, a report older than the staged file, a staged file the
    report does not have (an end-to-end suite the related run never executes). A report that
    exists and is not a report cannot be looked at, and that is a problem."""
    settings = _time_budget(config)
    staged = [p for p in core.staged_files(root) if config.is_test(p)]
    if settings is None or not staged:
        return [], []
    report, budget = settings
    report_path = os.path.join(root, report)
    try:
        written = os.path.getmtime(report_path)
    except OSError:
        return [], [f"{report} does not exist: the time budget was not checked (the related-tests hook writes it)"]
    outcomes = read_jest_report(report_path)
    if outcomes is None:
        return [f"{report} is not a Jest-shaped JSON report, so the time budget cannot be checked"], []
    in_report: dict[str, list[Outcome]] = {}
    for outcome in outcomes:
        in_report.setdefault(core.repo_path(root, outcome.file), []).append(outcome)
    notes, slow = [], []
    for path in staged:
        tests = in_report.get(path)
        if tests is None:
            notes.append(f"{path}: not in {report}, so its time was not checked (a suite the related run does not execute, say)")
            continue
        try:
            changed = os.path.getmtime(os.path.join(root, path))
        except OSError:
            changed = None
        if changed is None or changed > written:
            notes.append(f"{path}: changed after {report} was written, so its time was not checked")
            continue
        for test in tests:
            if (test.duration or 0) > budget["testMs"]:
                slow.append(_Slow("test", path, test.name, test.duration, budget["testMs"]))
        total = sum(test.duration or 0 for test in tests)
        if total > budget["fileMs"]:
            slow.append(_Slow("file", path, path, total, budget["fileMs"]))
    problems, excused = [], []
    for body in core.hatch_lines(message, SLOW_TEST):
        item, found = _judge_slow_test_line(body, slow)
        problems += found
        excused.append(item)
    for item in slow:
        if item in excused:
            continue
        hatch = f"`slow-test: {item.name} | <measured duration, e.g. {core.format_ms(item.ms)}ms> | <why it has to be this slow>`"
        if item.kind == "test":
            problems.append(f'{item.path}: "{item.name}" took {core.format_ms(item.ms)} ms, over the {item.limit} ms budget for one test. Make it faster, or add a line {hatch} to the commit message.')
        else:
            problems.append(f"{item.path}: its tests add up to {core.format_ms(item.ms)} ms, over the {item.limit} ms budget for one file. Split it or make it faster, or add a line {hatch} to the commit message.")
    return problems, notes


def commit_gate(root: str, config: core.Config, message: str) -> tuple[list[str], list[str]]:
    """Every lock that reads the commit message. Returns (problems, notes)."""
    problems = [p for lock in COMMIT_GATE for p in lock(root, config, message)]
    found, notes = time_budget(root, config, message)
    return problems + found, notes


# --- self-check ------------------------------------------------------------

HOOK_IDS = ["guardrail-selfcheck", "guardrail-staged", "guardrail-perturb", "guardrail-commit-gate"]
HOOK_TYPES = ["pre-commit", "commit-msg", "pre-push"]


def canaries() -> list[str]:
    """Prove, on built-in samples, that the detectors still bite and still stay quiet."""
    failures = []

    def expect(condition: bool, label: str):
        if not condition:
            failures.append(f"canary failed: {label}")

    expect(len(fake_tests_in("it.skip('x', () => {})")) == 1, "skipped test must bite")
    expect(len(fake_tests_in("expect(1).toBe(1)")) == 1, "tautology must bite")
    expect(fake_tests_in("expect(total(2)).toBe(4)") == [], "real assertion must stay quiet")
    expect(len(perturbations("expect(sum).toBe(42)")) == 1, "expected value must be perturbable")
    expect(perturbations("expect(a).not.toBe(1)") == [], "negated assertion must stay quiet")
    expect(core.hatch("fix: x\n\nno-test: generated file only", "no-test"), "hatch with a reason must open")
    expect(not core.hatch("fix: mentions no-test: here", "no-test"), "hatch inside a sentence must stay shut")
    expect(not core.hatch("fix: x\n\nno-test: n/a", "no-test"), "hatch without a reason must stay shut")
    expect(_judge_slow_test_line("adds | 6200 | talks to the real database", [])[1] != [], "a slow-test line without a unit must bite")
    slow = [_Slow("test", "test/a.test.ts", "adds", 6200.0, 5000)]
    expect(_judge_slow_test_line("adds | 6200ms | talks to the real database", slow) == (slow[0], []), "a complete slow-test line must open")
    config = core.Config({"production": ["src/**"]})
    expect(config.is_production("src/a.ts"), "production path must be recognised")
    expect(not config.is_production("src/a.test.ts"), "a test is not production")
    shape, _ = switched_on_shape("### job\n- **State**: MAYBE\n", datetime.date(2026, 1, 1), 30)
    expect(len(shape) >= 2, "a broken registry entry must bite")
    return failures


def selfcheck(root: str, config_text: str) -> list[str]:
    """The locks are installed, wired and still working in this repository."""
    problems = canaries()
    git_dir = core.git(root, "rev-parse", "--git-common-dir").strip()
    hooks_dir = os.path.join(root, git_dir, "hooks")
    for hook_type in HOOK_TYPES:
        path = os.path.join(hooks_dir, hook_type)
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                installed = "pre-commit" in handle.read() and os.access(path, os.X_OK)
        except OSError:
            installed = False
        if not installed:
            problems.append(f"the {hook_type} hook is not installed: run `pre-commit install`")
    for hook_id in HOOK_IDS:
        if not re.search(rf"^\s*-\s*id:\s*{re.escape(hook_id)}\s*$", config_text, re.MULTILINE):
            problems.append(f"{hook_id} is not in .pre-commit-config.yaml")
    return problems
