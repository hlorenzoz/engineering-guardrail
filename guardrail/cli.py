"""Command line of the guardrail: one command per hook.

Exit codes: 0 pass, 1 blocked, 2 unknown command or the lock could not look.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys

from . import core, locks, perf

USAGE = (
    "usage: guardrail selfcheck | staged | perturb | commit-gate <message file> | citations"
    " | perf-gate --report <file> --history <file> [--label <name>] [--strict]"
)


def _report(name: str, problems: list[str], notes: list[str] | None = None) -> int:
    """Print what a lock found, on stderr, and turn it into an exit code."""
    for note in notes or []:
        print(f"guardrail {name}: note: {note}", file=sys.stderr)
    for problem in problems:
        print(f"guardrail {name}: BLOCKED: {problem}", file=sys.stderr)
    return 1 if problems else 0


def _perf_gate(args: list[str]) -> int:
    """Record the full suite's timing and compare it with the last run of its label."""
    parser = argparse.ArgumentParser(prog="guardrail perf-gate", allow_abbrev=False)
    parser.add_argument("--report", required=True, help="Jest-shaped JSON report of a full run")
    parser.add_argument("--history", required=True, help="JSON-lines file the run is appended to")
    parser.add_argument("--label", default=perf.DEFAULT_LABEL, help="runs with the same label are compared")
    parser.add_argument("--strict", action="store_true", help="a warning is an exit code 1")
    try:
        options = parser.parse_args(args)
    except SystemExit as stop:  # argparse exits on a usage error (2) and on --help (0)
        return stop.code if isinstance(stop.code, int) else 2
    cwd = os.getcwd()
    root = core.git(cwd, "rev-parse", "--show-toplevel", check=False).strip() or cwd  # a repository is optional
    commit = core.git(root, "rev-parse", "--short", "HEAD", check=False).strip() or None
    date = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    entry = perf.summarise(root, options.report, options.label, commit, date)
    history, notes = perf.read_history(options.history)
    warnings, lines = perf.compare(entry, perf.previous_run(history, options.label))
    perf.append(options.history, entry)  # always: a run that warns is still a run
    for note in notes:
        print(f"guardrail perf-gate: note: {note}", file=sys.stderr)
    for line in lines + warnings:
        print(line, file=sys.stderr)
    return 1 if warnings and options.strict else 0


def _read(path: str) -> str:
    """The text of an input file. One that cannot be read (missing, a folder, no permission) is a lock
    that cannot look, so it does not pass: a reported error, never a traceback."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as error:
        raise core.GuardrailError(f"{path} cannot be read: {error}") from error


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    command, rest = args[0], args[1:]
    try:
        if command == "perf-gate":  # needs no configuration and no repository
            return _perf_gate(rest)
        root = core.repo_root()
        config = core.Config.load(root)

        if command == "selfcheck":
            return _report(command, locks.selfcheck(root, _read(os.path.join(root, ".pre-commit-config.yaml"))))

        if command == "citations":  # manual: every tracked document, not only the staged lines
            problems, unresolved = locks.live_citations(root, config, every_file=True)
            return _report(command, problems, [f"{unresolved} citation(s) point outside this repository"])

        if core.is_merge(root):
            return 0

        if command == "staged":
            problems = locks.no_fake_tests(root, config)
            cited, unresolved = locks.live_citations(root, config)
            shape, stale = locks.switched_on(root, config)
            notes = stale + ([f"{unresolved} citation(s) point outside this repository"] if unresolved else [])
            return _report(command, problems + cited + shape, notes)

        if command == "perturb":
            problems, notes = locks.perturb_expected(root, config)
            return _report(command, problems, notes)

        if command == "commit-gate":
            if len(rest) != 1:
                print(USAGE, file=sys.stderr)
                return 2
            message = _read(rest[0])
            problems, notes = locks.commit_gate(root, config, message)
            return _report(command, problems, notes)
    except core.GuardrailError as error:
        print(f"guardrail {command}: cannot look, so it does not pass: {error}", file=sys.stderr)
        return 2
    print(f"guardrail: unknown command '{command}'. {USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
