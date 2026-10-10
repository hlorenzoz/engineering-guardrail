"""The locks. Each returns a list of problems; an empty list means the lock passes.

A lock never prints and never exits: the command line decides that. A lock that
cannot look (no runner, unreadable file) reports a problem. It never passes blind.
"""

from __future__ import annotations

import datetime
import os
import re
import shlex
import signal
import subprocess

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


def _sentinel(literal: str) -> str:
    """A value the code under test cannot produce by accident."""
    if literal in ("true", "false"):
        return "false" if literal == "true" else "true"
    if literal[0] in "'\"":
        return literal[0] + _STRING_SENTINEL + literal[0]
    return "123456" if literal.replace("_", "") == _NUMBER_SENTINEL else _NUMBER_SENTINEL


def perturbations(text: str, only_lines: set[int] | None = None) -> list[tuple[int, str, str]]:
    """Expected values that can be replaced. Returns (line number, literal, mutated line)."""
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        if only_lines is not None and number not in only_lines:
            continue
        if _is_comment(line) or NO_PERTURB in line or ".not." in line:
            continue
        match = _EXPECTED.search(line)
        if match:
            literal = match.group(2)
            mutated = line[: match.start(2)] + _sentinel(literal) + line[match.end(2) :]
            found.append((number, literal, mutated))
    return found


def _runner_for(config: core.Config, path: str) -> str | None:
    for runner in config["perturb"]["runners"]:
        if core.matches(path, [runner["files"]]):
            return runner["command"]
    return None


def _run(command: str, path: str, root: str, timeout: int) -> int:
    """Run the project's test command for one file. A timeout counts as a failure."""
    try:
        completed = subprocess.run(
            command.replace("{file}", shlex.quote(path)),
            shell=True,
            cwd=root,
            capture_output=True,
            timeout=timeout,
        )
        return completed.returncode
    except subprocess.TimeoutExpired:
        return 124


def perturb_expected(root: str, config: core.Config) -> tuple[list[str], list[str]]:
    """Replace each expected value this commit adds and require its test to fail.

    A test that stays green with a wrong expected value proves nothing. Returns
    (problems, notes). The working copy is always restored.
    """
    problems, notes = [], []
    settings = config["perturb"]
    budget = settings["max"]
    for path in core.staged_files(root):
        if not config.is_test(path):
            continue
        candidates = perturbations(core.staged_text(root, path), core.staged_added_lines(root, path))
        if not candidates:
            continue
        command = _runner_for(config, path)
        if command is None:
            problems.append(f"{path}: no runner configured in .guardrail.json (perturb.runners); not observed")
            continue
        target = os.path.join(root, path)
        with open(target, encoding="utf-8") as handle:
            original = handle.read()

        def restore(*_args):
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(original)

        previous = {s: signal.signal(s, lambda *_: (restore(), os._exit(130))) for s in (signal.SIGINT, signal.SIGTERM)}
        try:
            if _run(command, path, root, settings["timeoutSeconds"]) != 0:
                problems.append(f"{path}: the test fails before any perturbation; fix it first")
                continue
            lines = original.split("\n")
            for number, literal, mutated in candidates:
                if budget == 0:
                    notes.append(f"{path}:{number}: not perturbed (limit of {settings['max']} per commit reached)")
                    continue
                budget -= 1
                changed = list(lines)
                changed[number - 1] = mutated
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(changed))
                code = _run(command, path, root, settings["timeoutSeconds"])
                if code == 0:
                    problems.append(
                        f"{path}:{number}: still green with the expected value {literal} replaced. "
                        "The assertion does not check what it claims."
                    )
        finally:
            restore()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    return problems, notes


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
