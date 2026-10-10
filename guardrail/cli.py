"""Command line of the guardrail: one command per hook.

Exit codes: 0 pass, 1 blocked, 2 unknown command or the lock could not look.
"""

from __future__ import annotations

import os
import sys

from . import core, locks

USAGE = "usage: guardrail selfcheck | staged | perturb | commit-gate <message file> | citations"


def _report(name: str, problems: list[str], notes: list[str] | None = None) -> int:
    """Print what a lock found, on stderr, and turn it into an exit code."""
    for note in notes or []:
        print(f"guardrail {name}: note: {note}", file=sys.stderr)
    for problem in problems:
        print(f"guardrail {name}: BLOCKED: {problem}", file=sys.stderr)
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    command, rest = args[0], args[1:]
    try:
        root = core.repo_root()
        config = core.Config.load(root)

        if command == "selfcheck":
            with open(os.path.join(root, ".pre-commit-config.yaml"), encoding="utf-8") as handle:
                return _report(command, locks.selfcheck(root, handle.read()))

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
            with open(rest[0], encoding="utf-8") as handle:
                message = handle.read()
            problems = [p for lock in locks.COMMIT_GATE for p in lock(root, config, message)]
            return _report(command, problems)
    except core.GuardrailError as error:
        print(f"guardrail {command}: cannot look, so it does not pass: {error}", file=sys.stderr)
        return 2
    print(f"guardrail: unknown command '{command}'. {USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
