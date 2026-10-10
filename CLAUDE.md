# CLAUDE.md

Operating guide for AI agents working in this repository: the shared git hooks
("locks") of the Lorenzoz Agency projects. Read `README.md` for what each lock does.

## Rules

- **Standard library only.** No dependency, ever: a hook must run wherever `pre-commit`
  and `python3` exist. Python 3.9 syntax at most.
- **A lock that cannot look does not pass.** Bad configuration, a git error or a missing
  runner is exit 2 or a reported problem, never a silent 0.
- **One definition of each primitive**, in `guardrail/core.py`: what production code is,
  what is staged, what a hatch is. A lock never re-derives them.
- **A lock returns problems; it never prints or exits.** `guardrail/cli.py` does that.
- **Judge only what the commit adds**, so code that predates the guardrail does not
  block unrelated work.
- **Every lock change ships with three things:** a must-bite scenario, a
  must-stay-quiet scenario (`tests/test_scenarios.py`, through the real command line on
  a throwaway repository), and a mutant in `tests/test_mutants.py` that breaks the lock
  and is killed. A surviving mutant, or one that cannot be applied, fails the suite.
- **Check the message, not only the exit code.** A lock that blocks for the wrong reason
  is broken.
- **A hatch is matched as a whole line prefix and needs a reason.** Never as a substring.
- **No lock may fail because time passed.** Stale items are notes.
- **Honest limits are documented.** When a lock trusts a person's word or misses a case,
  the README says so.
- Conventional commits, no AI attribution, never `--no-verify`. English only.

## Layout

| Path | Holds |
| :--- | :--- |
| `guardrail/core.py` | Configuration, path classification, git access, hatches |
| `guardrail/locks.py` | The locks and the built-in canaries |
| `guardrail/cli.py` | Commands, messages and exit codes |
| `bin/guardrail` | The entry point the hooks call |
| `.pre-commit-hooks.yaml` | The hooks offered to projects |
| `tests/` | Scenarios and mutants |

## Releasing

Run `just check`, tag `vX.Y.Z`, push the tag. A project adopts it by changing `rev` in
its `.pre-commit-config.yaml`. A change that makes a lock stricter is a minor version
and is announced in the tag message, because it can block commits that passed before.
