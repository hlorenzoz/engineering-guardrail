# engineering-guardrail

Git hooks that stop a commit when it looks right and is not: a test that cannot fail, a
fix with no test, a figure changed without the test that compares its views, something
built and never switched on. Shared by every Lorenzoz Agency project through
[pre-commit](https://pre-commit.com), pinned to a version.

It runs on the developer's machine. Standard-library Python only: there is nothing to
install beyond `pre-commit` and `python3`.

## What it checks

| Hook | Stage | Blocks when |
| :--- | :--- | :--- |
| `guardrail-selfcheck` | commit, first | a hook type is not installed, a guardrail hook is missing from the config, or a built-in canary no longer bites |
| `guardrail-staged` | commit | a staged test adds a skipped, focused, empty or self-comparing test; a document adds a `path:line` citation that is dead; the switched-on registry is malformed |
| `guardrail-perturb` | commit | a new assertion stays green after its expected value is replaced with a wrong one |
| `guardrail-commit-gate` | commit message | production code has no test staged; the commit is over the size cap; a shared figure changes without its cross-view test; something that must be switched on has no registry entry |

Only the lines a commit adds are judged, so code written before the guardrail does not
block new work.

## Use it in a project

1. Add to `.pre-commit-config.yaml`, as the first repository:

   ```yaml
   default_install_hook_types: [pre-commit, commit-msg, pre-push]
   repos:
     - repo: https://github.com/hlorenzoz/engineering-guardrail
       rev: v0.1.2
       hooks:
         - id: guardrail-selfcheck
         - id: guardrail-staged
         - id: guardrail-perturb
         - id: guardrail-commit-gate
   ```

2. Add `.guardrail.json` at the root of the project. Every key is optional:

   ```json
   {
     "production": ["src/**"],
     "tests": ["**/*.test.ts", "test/**"],
     "ignore": ["**/*.gen.ts"],
     "sizeCap": { "files": 8, "lines": 400 },
     "crossView": [
       { "name": "billing", "paths": ["src/modules/accounting/**"], "tests": ["test/consistency/**"] }
     ],
     "perturb": {
       "runners": [{ "files": "test/**", "command": "bunx vitest run {file}" }],
       "max": 12,
       "timeoutSeconds": 180
     },
     "switchedOn": { "registry": ".agents/SWITCHED_ON.md", "triggers": ["drizzle/*.sql"], "staleDays": 30 }
   }
   ```

3. Run `pre-commit install`.

## Escape hatches

A hatch is a line in the commit message that starts with the key and gives a reason of
at least ten characters. A mention inside a sentence does not count.

| Line | Opens |
| :--- | :--- |
| `no-test: <why no test can cover it>` | production code without a staged test |
| `size-ok: <why it cannot be split>` | the size cap |
| `cross-view-na: <why no shared figure changes>` | the cross-view rule |
| `no-switch-on: <why nothing has to be switched on>` | the switched-on trigger |

In a test file, `// guardrail: allow-skip <reason>` on the line allows a conditional
skip, and `// guardrail: no-perturb <reason>` leaves an assertion out of the perturbation.

## The switched-on registry

`.agents/SWITCHED_ON.md` lists what exists only when someone turns it on: a migration
applied, a scheduled job, an installed hook, a deployed service.

```markdown
### migration 0018
- **State**: ON
- **Who switches it on**: person (ops)
- **Check**: `just db-status`
- **Last check**: 2026-10-10 → 19 migrations applied
```

`State` is `ON`, `OFF` or `PARTIAL`. An old check is listed as a note and never fails a
commit: nothing may fail only because time passed.

## Honest limits

- **The size cap and the cross-view rule trust a typed reason.** A hatch is a person's
  word, recorded in history; the hook cannot verify it.
- **Cross-view checks that a test under the configured folder is staged.** It does not
  check that the test really compares two views.
- **A runner with `"command": null`** declares a suite that cannot run at commit time
  (a browser suite): its assertions are not perturbed, and the hook says so.
- **Perturbation covers simple literals** in `toBe`, `toEqual`, `toStrictEqual`,
  `toHaveLength`, `toBeCloseTo` and `toHaveBeenCalledTimes`, one per line, up to `max`
  per commit (it says so when it stops). Computed expected values are not perturbed.
- **Fake-test detection is by line.** A skip spread over several lines is not seen.
- **A citation into another repository cannot be judged** and is counted, not failed.
- **Hooks can be bypassed** with `--no-verify`. The projects forbid it; nothing here
  can prevent it.

## Develop

| Command | What it does |
| :--- | :--- |
| `just test` | The canary scenarios: every lock on a throwaway repository, through the real command line |
| `just mutants` | Breaks each lock on purpose and requires the scenarios to turn red (about 4 minutes) |
| `just check` | Both |

A change to a lock ships with its scenario (must bite, must stay quiet) and its mutant.
Release by tagging; projects move to the new version by changing `rev`.
