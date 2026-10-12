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
       "runners": [
         {
           "files": "test/**",
           "command": "bunx vitest run {file}",
           "batch": { "args": "--reporter=json --outputFile={report}", "format": "jest-json" }
         }
       ],
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

## Perturbation in batch

`guardrail-perturb` replaces the expected value of each assertion a commit adds and
requires the test to fail. Without more, that is one run of the project's test runner per
assertion, and a runner takes seconds to start. A runner that declares `batch` proves all
the assertions of a file together:

```json
{ "files": "test/**", "command": "bunx vitest run {file}",
  "batch": { "args": "--reporter=json --outputFile={report}", "format": "jest-json" } }
```

The lock appends `args` to the command. `{report}` is a temporary file it creates and
removes. `format` is the shape of the report; `jest-json` is Jest's JSON
(`testResults[].assertionResults[]` with `status` and `failureMessages[]`), which
Vitest's `json` reporter also writes. A `batch` that is not an object, names another
format or has no `{report}` is a configuration error (exit 2). A runner without `batch`
behaves as it always did: one baseline run, then one run per assertion.

How it proves, per test file:

1. **Baseline.** The unperturbed file runs once and must pass, as always.
2. **A sentinel of its own for every assertion.** Each expected value is replaced by a
   number (`988654003`, `989654006`, ...) or a string (`ZZ_MUTATED_<n>_ZZ`). A boolean
   also gets a number: it cannot carry an identity, and comparing it with a number fails
   for the same reason. A sentinel that already occurs in the file is never used.
3. **One run, read from the report.** An assertion is proven when ITS sentinel appears in
   a failure message of a test whose status is `failed`. Nothing else counts: not the exit
   code, not a failed test without the sentinel, not a sentinel in a passed or skipped
   test, not a line number (a stack trace names the callers' lines).
4. **Rounds.** The first failure of a test ends it, so an assertion behind a failing one
   is not reached. The lock restores each proven assertion and runs again with the rest,
   until a round proves nothing or one assertion is left.
5. **Whatever is not proven is checked the old way**, one at a time, with the old
   sentinel and the old rule (green means "does not check what it claims"). The batch
   never reports a problem: it can only say "proven", on positive evidence. If the report
   is missing, unreadable, of another shape, the run timed out or exited 0, that round
   proves nothing and everything left goes through the one-by-one check.

A file with one assertion is not batched: a batch of one is the run it replaces.
Cost: the baseline plus one run when every new assertion is in its own test; plus one
for each further assertion in the longest chain inside one test.

### What the batch proves, and when it would not

The claim is: *if sentinel k is in the failure message of a failed test during a batch
round, the run also fails when only assertion k is perturbed*. It rests on two things:

- **The tests are deterministic.** A flaky test is as unreliable here as in any run.
- **Whether an assertion executes does not depend on ANOTHER assertion or test having
  failed.** In a batch, assertions are made to fail at the same time, so code that runs
  only after a failure runs here and would not run alone. The cases: an assertion in a
  `catch` block, or in an `afterEach` that checks the test's result; a test that reads a
  flag another test sets only when it gets past its own assertion; a test that reads its
  own source and prints it in a message. For these the batch can say "proven" where the
  one-by-one check would say "does not check". A scenario pins the first case.

Also checked on real runners (Vitest 4, jsdom and the Cloudflare Workers pool) and not a
problem: `expect.soft` (every message is kept), `it.each` (one sentinel, every row),
an `expect` in a helper or in `beforeEach` (the test fails with the message), values
that are long (the message cuts the received value, never the expected sentinel) and
numbers (printed in plain digits, no separators). The report has stack frames with
`file:line:column` but no code frame. `toBeCloseTo` prints the DIFFERENCE between the
received and the expected value, which is why sentinels are a million apart: sentinels
one apart would make a small received value print another assertion's sentinel.

## Honest limits

- **The size cap and the cross-view rule trust a typed reason.** A hatch is a person's
  word, recorded in history; the hook cannot verify it.
- **Cross-view checks that a test under the configured folder is staged.** It does not
  check that the test really compares two views.
- **A runner with `"command": null`** declares a suite that cannot run at commit time
  (a browser suite): its assertions are not perturbed, and the hook says so.
- **A one-by-one check counts any non-zero exit as "fails".** A crash, a timeout or a
  syntax error in the perturbed file also kills the mutant. The batch is stricter: it
  needs the sentinel in a failed test's message.
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
