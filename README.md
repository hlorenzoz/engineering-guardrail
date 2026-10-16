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
| `guardrail-commit-gate` | commit message | production code has no test staged; the commit is over the size cap; a shared figure changes without its cross-view test; something that must be switched on has no registry entry; a staged test is over the time budget (`timeBudget`, off until configured); a staged migration adds a foreign key with no index (`fkIndex`, off until configured) |

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
           "batch": { "args": "--reporter=json --outputFile={report}", "format": "jest-json", "baselineArgs": "--reporter=default" }
         }
       ],
       "max": 12,
       "timeoutSeconds": 180
     },
     "switchedOn": { "registry": ".agents/SWITCHED_ON.md", "triggers": ["drizzle/*.sql"], "staleDays": 30 },
     "timeBudget": { "report": "tests/report_related.json", "format": "jest-json", "testMs": 5000, "fileMs": 20000 },
     "fkIndex": { "migrations": ["drizzle/*.sql"], "leadingColumnsIgnored": ["organization_id"] }
   }
   ```

   **The type of each key is checked (v0.4.0).** A key of the wrong type is a configuration error: exit 2,
   `guardrail <command>: cannot look, so it does not pass: .guardrail.json: <key path> must be <type>`, for
   example `production must be a list of strings`, `sizeCap.lines must be a whole number` (a boolean or a
   fraction is not one), `crossView[0].tests must be a list of strings` or
   `perturb.runners[0].command must be a string or null`. A file that is not an object is refused the same
   way. Before, `"production": "src/**"` was read one letter at a time and the no-test lock never bit, and
   `"sizeCap": 5` ended in a traceback. A rule of `crossView` needs `paths` and `tests`, a runner needs
   `files` and `command` (`null` declares its suite not runnable). `timeBudget`, `fkIndex` and a runner's
   `batch` keep their own validation where they are used. This is stricter than v0.3.0, so a project moving
   from it may meet a refusal for a configuration that used to pass in silence.

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
| `slow-test: <test name> \| <measured duration> \| <why it has to be this slow>` | one test (or file) over the time budget; one line per item |
| `no-index: <table>.<column> \| <why it needs none>` | a foreign key added without an index |

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
  "batch": { "args": "--reporter=json --outputFile={report}", "format": "jest-json", "baselineArgs": "--reporter=default" } }
```

The lock appends `args` to the command. `{report}` is a temporary file it creates and
removes. `format` is the shape of the report; `jest-json` is Jest's JSON
(`testResults[].assertionResults[]` with `status` and `failureMessages[]`), which
Vitest's `json` reporter also writes. `baselineArgs` (optional) is appended to the
command of the instrumented baseline only. It must make the runner show the output of
passing tests: **Vitest 4 swaps in a quiet reporter when an AI agent runs it** (it reads
`AI_AGENT` or `CLAUDECODE`), which prints nothing for a passing test, so without
`"--reporter=default"` the markers never come back in an agent's commit. A `batch` that is not an object, names another
format or has no `{report}` is a configuration error (exit 2). A runner without `batch`
behaves as it always did: one baseline run, then one run per assertion.

How it proves, per test file:

1. **Baseline, instrumented.** The unperturbed file runs once and must pass, as always.
   For this run only, each expected value is wrapped so that reaching the matcher call
   prints a marker: `.toBe(5)` becomes `.toBe((console.log("ZZ_SEEN_" + "<nonce>_<k>" + "_ZZ"), 5))`.
   Same value, same line. The marker is built at run time from pieces, so only executing
   the call prints it; the file's own text (echoed in a code frame, say) never contains it.
   After the run, an assertion is *reached* when its marker is in the runner's output
   (stdout or stderr). It costs no run: it is the baseline.
2. **Only a reached assertion may count on the batch.** One whose marker did not come back
   (it sits in a `catch` block, in an `afterEach` that checks the test's result, behind
   `if (!flag)`) is checked one by one, like today, and gets today's verdict.
3. **A sentinel of its own for every reached assertion.** Each expected value is replaced
   by a number (`988654003`, `989654006`, ...) or a string (`ZZ_MUTATED_<n>_ZZ`). A boolean
   also gets a number: it cannot carry an identity, and comparing it with a number fails
   for the same reason. A sentinel that already occurs in the file is never used.
4. **One run, read from the report.** An assertion is proven when ITS sentinel appears in
   a failure message of a test whose status is `failed`. Nothing else counts: not the exit
   code, not a failed test without the sentinel, not a sentinel in a passed or skipped
   test, not a line number (a stack trace names the callers' lines).
5. **Rounds.** The first failure of a test ends it, so an assertion behind a failing one
   is not reached. The lock restores each proven assertion and runs again with the rest,
   until a round proves nothing or one assertion is left.
6. **Whatever is not proven is checked the old way**, one at a time, with the old
   sentinel and the old rule (green means "does not check what it claims"). The batch
   never reports a problem: it can only say "proven", on positive evidence. If the report
   is missing, unreadable, of another shape, the run timed out or exited 0, that round
   proves nothing and everything left goes through the one-by-one check.

If the instrumented baseline fails, the plain baseline runs: if that fails too, the test
is red ("fails before any perturbation"); if it passes, the markers broke the test (one
that counts `console.log` calls, say) and nothing is reached. If the run passes and no
marker comes back (a silent reporter, a spy that swallows the output), nothing is reached
either. In both cases every assertion is checked one by one, the verdicts are today's, and
the hook prints `note: <file>: the batch could not tell which assertions run`. A project
that sees that note on every commit gets no gain from `batch`; the worst case is today's
cost plus one run (the plain baseline).

A file with one assertion is not batched: a batch of one is the run it replaces.
Cost: the baseline plus one run when every new assertion is in its own test; plus one
for each further assertion in the longest chain inside one test.

### What the batch proves, and when it would not

The claim is: *if the assertion's matcher call executes in the unperturbed run, and its
sentinel is in the failure message of a failed test during a batch round, the run also
fails when only that assertion is perturbed.* It rests on two things:

- **The tests are deterministic.** A flaky test is as unreliable here as in any run.
- **An assertion that executes in the unperturbed run executes the same way when other
  assertions fail.** The reached check closes the cases where an assertion does not
  execute at all without a failure (a catch block, an `afterEach` on failure,
  `if (!flag)`). What it cannot close is one line that executes in TWO dynamic contexts:
  in the normal flow, where its failure is swallowed (so it is reached, and alone it
  stays green), and again only after another assertion failed, where it surfaces. The
  smallest example, built and run on Vitest 4: `const check = () => expect(1).toBe(1)`,
  and a test that calls it inside `try { check() } catch {}`, registers
  `onTestFailed(() => check())`, then has another assertion that is perturbed and fails.
  The one-by-one check reports the assertion inside `check` ("still green"); the batch proves it.
  It needs a swallowed assertion AND the same line re-run by a failure hook; no other
  case was found. The scenario
  `test_known_limit_one_line_that_runs_twice_swallowed_and_then_surfacing` pins it.
- Also not covered: a test that reads its own source and prints it in a message.

Also checked on real runners (Vitest 4, jsdom and the Cloudflare Workers pool) and not a
problem: `expect.soft` (every message is kept), `it.each` (one sentinel, every row),
an `expect` in a helper or in `beforeEach` (the test fails with the message), values
that are long (the message cuts the received value, never the expected sentinel) and
numbers (printed in plain digits, no separators). The report has stack frames with
`file:line:column` but no code frame. `toBeCloseTo` prints the DIFFERENCE between the
received and the expected value, which is why sentinels are a million apart: sentinels
one apart would make a small received value print another assertion's sentinel.

### The runner does not see the hook's git variables

The lock launches the project's test command without any `GIT_*` variable (`GIT_DIR`,
`GIT_INDEX_FILE`, ...). Under a commit hook git sets them for the repository being
committed, and a test that runs `git init` or `git config` in a scratch folder would
write into that repository instead. The cost: a test that needs a `GIT_*` variable
(`GIT_SSH_COMMAND`, `GIT_CONFIG_GLOBAL`) must set it itself; none of the projects' do.

## Time budget for new or changed tests

A slow test is cheapest to fix when it is written. `timeBudget` makes the commit read the
durations of the tests it adds or changes, and refuse one that is over budget. It is off
until a project writes the block:

```json
"timeBudget": { "report": "tests/report_related.json", "format": "jest-json", "testMs": 5000, "fileMs": 20000 }
```

- `report` is a Jest-shaped JSON report (the shape of `perturb`'s `jest-json`; Vitest's
  `json` reporter writes it) that the project's related-tests hook produces **during the
  same commit**, beside its normal output. For Vitest that is adding
  `--reporter=default --reporter=json --outputFile=tests/report_related.json` to the hook
  command. The report is not versioned.
- `testMs` and `fileMs` default to 5000 and 20000 when the block leaves them out: a
  starting point for a project to replace with figures measured on its own suite. They must
  be whole numbers above zero; an unknown key or format is a configuration error (exit 2).
- It judges **only the tests of STAGED test files** (added or modified): one test over
  `testMs`, or a staged file whose tests add up over `fileMs`. Exactly at the budget passes.
  A test without a duration (skipped) counts as zero.
- It runs in `commit-gate` (the commit-message stage), the only place that sees the
  message, and so the hatch.

### The hatch

One line per item over budget, written as three parts separated by `|`:

```
slow-test: pricing > adds twice | 6200ms | talks to the real database on purpose
```

The content is checked, not only the prefix. The first part names the item: a test's full
name, or a substring that matches exactly one item over budget (the path of the file for a
file over `fileMs`); a full name wins over a longer name that contains it. The second is a
measured duration, `1400ms` or `1.4s`, and must exceed that item's budget (a test that was
over budget cannot honestly have taken less). The third is a reason of at least ten
characters. A line that names nothing over budget is an error too (a stale line, or a
typo), so the hatch does not outlive the problem. The line must start with `slow-test:`.

### How the lock knows the report belongs to this commit

It does not know it, it checks the cheap evidence it has, per staged test file:

1. The file must be **in the report**. Not being in it is a note, not a failure: an
   end-to-end suite the related run never executes would otherwise block every commit.
2. The file must **not be newer than the report** (modification time of the working
   copy against the time the report was written). A report older than the file measured
   a previous version of it, so that file is a note and is not judged.
3. A **missing report** is a note. A report that exists and is not a Jest-shaped report is
   a problem: the lock cannot look and does not pass.

A commit that stages no test file is not judged at all, whatever the report says.

`perturb` rewrites the staged test files while it proves them and puts them back with the same content **and the same modification time**, so running it after the related tests does not make every file look newer than its report.

Notes appear on stderr, which `pre-commit` shows only when a hook fails. So a project whose
hook never writes the report gets no complaint: check it once by hand
(`python3 bin/guardrail commit-gate .git/COMMIT_EDITMSG` after a commit that stages a test).

## Every foreign key a migration adds has an index

A foreign key with no index makes each delete or update of the referenced row scan the whole
referencing table, and each join through it too. Nothing shows while the table is small. The
lock refuses it when the migration is written. Off until a project writes the block:

```json
"fkIndex": { "migrations": ["drizzle/*.sql"], "leadingColumnsIgnored": ["organization_id"] }
```

- `migrations` are the globs of the migration files (it reads these paths itself, whatever
  `ignore` says for production code). `leadingColumnsIgnored` are columns that may be left out
  of the index, or sit at its front: a multi-tenant schema whose keys are
  `(organization_id, client_id)` and whose indexes are often just `(client_id)`.
- For each **staged** migration, every foreign key it adds must be served by an index of the
  same table whose leading keys are the key's columns (minus the ignored ones; a key on the
  ignored column alone needs an index on it), in any order. A primary key or a unique
  constraint counts, and so does a unique index. A partial index (`WHERE`) and an expression
  index do not.
- It replays **all** the migration files in the index (what will be committed), in file-name
  order, and judges the final state: an index made in an earlier file counts; one a later
  file dropped (`DROP INDEX`, `DROP CONSTRAINT`, `DROP COLUMN`, `DROP TABLE`) does not; a key
  dropped later needs no index. Only the keys that a staged file adds are judged: an old
  unindexed key never blocks new work.
- Hatch: `no-index: <table>.<column> | <why it needs none>`, one line per key, naming the
  table and one column of the key; the reason needs ten characters. A line that names no
  unindexed key of this commit is an error (a stale line, or a typo).

What it reads (regular expressions over the SQL, not a SQL parser, in `guardrail/schema.py`):
`CREATE TABLE` (column definitions with an inline `REFERENCES`, `PRIMARY KEY` or `UNIQUE`; table
constraints `FOREIGN KEY`, `PRIMARY KEY`, `UNIQUE`), `ALTER TABLE ... ADD [COLUMN | CONSTRAINT]`,
`DROP CONSTRAINT`, `DROP COLUMN`, `CREATE [UNIQUE] INDEX [CONCURRENTLY] [IF NOT EXISTS]`,
`DROP INDEX`, `ALTER INDEX ... RENAME TO`, `DROP TABLE`. Comments, strings, quoted identifiers
and dollar-quoted bodies are skipped. Names are compared in lower case, without the schema.
What it does **not** see is in "Honest limits".

## The push suite's timing record: `perf-gate`

A command, not a lock: it never blocks a commit. After a full run it records how long the
suite took, so a suite that slowly gets slower is noticed when it grows and not when it
has become a problem.

```
guardrail perf-gate --report tests/report_full.json --history tests/perf_history.jsonl [--label push] [--strict]
```

- `--report` is the Jest-shaped JSON report (`jest-json`, the shape `perturb` and
  `timeBudget` read; Vitest's `json` reporter writes it) of a **full** run.
  A missing report, one that is not that shape, or one with no tests is refused (exit 2) and
  records nothing: a run that could not be read must not become the line the next run is
  compared with.
- `--history` is a JSON-lines file. Each run appends one line: `date` (UTC), `commit` (the
  short id, `null` outside a repository), `label`, `tests`, `totalMs` and `files` (the
  sum of each test file's tests, paths relative to the repository). The run is appended
  even when it warns. The file is the project's to keep or not in git; a line the tool did
  not write is skipped with a note.
- It compares with the **last line of the same label** (`--label`, default `default`). A
  different label is never compared: the push suite and a release run are different curves.
- It **warns** (stderr, exit 0) when the total grew more than 10% (`TOTAL_GROWTH`), or a file
  grew more than 2x (`FILE_FACTOR`) and now takes more than 100 ms (`FILE_FLOOR_MS`: below
  it a duration is noise, so a 5 ms file that triples says nothing). A file that is new has
  no previous duration and cannot warn. `--strict` turns a warning into exit 1.
- It prints what a person reads: tests and total against the previous run, and the ten files
  (`TOP_FILES`) that grew most, biggest first, with the new ones marked.

The thresholds are named constants in `guardrail/perf.py`, not configuration: a project does
not tune how much slower is too slow.

## Honest limits

- **The size cap and the cross-view rule trust a typed reason.** A hatch is a person's
  word, recorded in history; the hook cannot verify it.
- **Cross-view checks that a test under the configured folder is staged.** It does not
  check that the test really compares two views.
- **A runner with `"command": null`** declares a suite that cannot run at commit time
  (a browser suite): its assertions are not perturbed, and the hook says so.
- **The time budget trusts a clock and a file.** Freshness is the report's modification
  time against the staged file's; it does not prove the report came from this commit's run.
  Durations of a loaded machine are noisy, so a budget is set with room (a test that is
  borderline fails on a busy day). A staged file that `pre-commit` rewrote while restoring
  unstaged changes looks newer than the report and is skipped with a note. A test the
  related run never executes is never timed here; the push suite is its backstop.
- **`fkIndex` reads SQL with regular expressions.** A foreign key built where text cannot
  show it is invisible, and nothing is guessed: a `DO $$ ... $$` block or any dynamic SQL
  (`EXECUTE`), a function body, `ALTER TABLE ... RENAME` and `ALTER TABLE ... RENAME COLUMN`
  (a renamed table or column keeps its old name in the replay), `ADD CONSTRAINT ... UNIQUE USING
  INDEX`, inherited or partitioned tables, and a migration tool that does not write SQL files. An
  index that exists only in the database, applied by hand, is not in the files and so does not
  count: record it in a migration. A foreign key the lock does not see is not reported, so a
  quiet lock is "no unindexed key in the forms it reads", not a proof about the schema. Matching
  an index to a key checks its leading columns, not that its operator class suits the lookup.
- **`perf-gate` compares durations measured on whatever machine ran the suite.** A busy day
  looks like growth. It is a trend to look at, which is why it warns and why `--strict` is
  opt-in. The history is only as good as the runs that feed it: run it from the same place
  (the push) every time, under one label.
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
| `just test` | The direct tests of the pure readers (`tests/test_units.py`: fake-test patterns, replaceable expected values, globs, the report reader, the 3.9 and standard-library rules), the reader of migration SQL (`tests/test_schema.py`), then the canary scenarios: every lock on a throwaway repository, through the real command line |
| `just mutants` | Breaks each lock on purpose and requires the scenarios to turn red. It first runs the unmutated suite as a control (a red suite stops the run: every mutant would look killed), then tries on each mutant the test that killed it last time (`tests/mutant_hints.py`) and runs the whole suite only when that one stays green, so a survivor is still judged by everything. Measured on 2026-10-16, 3 workers, no other test run on the machine: 234 mutants in 80.6 s (`3667b94`, control included), against 1,834.5 s (30.6 min) for the same 234 in the old order at `9737e65` (alphabetical, a mutant killed late ran most of the suite first); the same 234 killed, none survived. A new mutant has no hint and runs the whole suite; its line says `[no hint]` and which test killed it, to add to `tests/mutant_hints.py` |
| `just check` | Both |

A change to a lock ships with its scenario (must bite, must stay quiet) and its mutant. A rule that
is a table or a pure function (which lines are fake tests, which expected values are replaced, what
a glob matches, how migration SQL is read) is also tested directly in `tests/test_units.py` and
`tests/test_schema.py`, one line per case; the scenario stays, because the lock is the command line.

The Python 3.9 rule is checked by parsing every source with `ast.parse(feature_version=(3, 9))` and
by running the scenarios with a 3.9 `python3` first in the `PATH`. The parse covers syntax only: a
`X | Y` evaluated at run time, or a library function newer than 3.9, passes it and fails on 3.9. The
scenarios start the guardrail as `python3`, so they test the interpreter that comes first in the
`PATH`, not the one that runs them: to test 3.9, put a directory whose `python3` is 3.9 first.
Release by tagging; projects move to the new version by changing `rev`.

A command handed a file it cannot read (`selfcheck` without `.pre-commit-config.yaml`, `commit-gate` with a
message file that is missing or is a folder) says `cannot look, so it does not pass` and names the file: exit 2,
no traceback. It blocked before too, with exit 1 and a traceback; only the code and the message changed (a patch
change, v0.4.0 carries it).
