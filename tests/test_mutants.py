"""Locks on the locks: break each lock on purpose and require the canaries to turn red.

A scenario suite that stays green when a lock is broken is not guarding that lock.
Each mutant replaces one literal that must occur EXACTLY once in its file; a
mutant that cannot be applied is a failure, because it proves nothing.

The run works on a snapshot of the repository taken when it starts, so the code can be
edited while it goes. A mutant stops at the first scenario that fails (that is a kill).
GUARDRAIL_MUTANTS=<text> runs only the mutants whose label or file contains the text
(comma-separated for several); it is for the work in progress, never for a release.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (file, original, replacement, what the mutant breaks)
MUTANTS = [
    ("guardrail/locks.py", "if not production or any(config.is_test(p) for p in staged):", "if True:", "fix-needs-test never bites"),
    ("guardrail/locks.py", "if len(production) <= cap[\"files\"] and lines <= cap[\"lines\"]:", "if len(production) <= cap[\"files\"]:", "size cap ignores lines"),
    ("guardrail/locks.py", "len(production) <= cap[\"files\"] and", "len(production) < cap[\"files\"] and", "size cap off by one"),
    ("guardrail/locks.py", "if not touched or any(core.matches(p, rule[\"tests\"]) for p in staged):", "if True:", "cross-view never bites"),
    ("guardrail/locks.py", "if not touched or settings[\"registry\"] in staged:", "if True:", "switch-on trigger never bites"),
    ("guardrail/locks.py", "if only_lines is not None and number not in only_lines:\n            continue\n        if _is_comment(line) or ALLOW_SKIP in line:", "if _is_comment(line) or ALLOW_SKIP in line:", "old skipped tests block new work"),
    ("guardrail/locks.py", "if _is_comment(line) or ALLOW_SKIP in line:", "if ALLOW_SKIP in line:", "a skip inside a comment bites"),
    ("guardrail/locks.py", "\"focused or skipped test\"),\n    (re.compile(r\"\\b(?:xit", "\"focused\"),\n    (re.compile(r\"\\b(?:xit", "wrong message for a skipped test"),
    ("guardrail/locks.py", "if code == 0:\n        return [", "if code == 99:\n        return [", "perturbation survivors are not reported"),
    ("guardrail/locks.py", "            if code != 0:\n                problems.append(", "            if False:\n                problems.append(", "a red test counts as a killed perturbation"),
    ("guardrail/locks.py", "        finally:\n            restore()", "        finally:\n            pass", "the file is left perturbed"),
    ("guardrail/locks.py", "if cited_line < 1 or cited_line > len(lines):", "if cited_line < 1:", "citation past the end is accepted"),
    ("guardrail/locks.py", "elif not lines[cited_line - 1].strip():", "elif False:", "citation of a blank line is accepted"),
    ("guardrail/locks.py", "if age > stale_days:\n                stale.append(", "if age > stale_days:\n                problems.append(", "an old check fails the commit"),
    ("guardrail/locks.py", "if state and state not in _STATES:", "if False:", "any registry state is accepted"),
    ("guardrail/core.py", "if line.startswith(key + \":\")]", "if (key + \":\") in line]", "a hatch mentioned inside a sentence opens the lock"),
    ("guardrail/core.py", "len(body) >= min_reason", "len(body) >= 0", "a hatch without a reason opens the lock"),
    ("guardrail/core.py", "\"--diff-filter=ACMR\"", "\"--diff-filter=ACMRD\"", "a deleted test counts as a staged test"),
    ("guardrail/core.py", "if matches(path, self[\"ignore\"]) or self.is_test(path):\n            return False", "if matches(path, self[\"ignore\"]):\n            return False", "tests count as production"),
    ("guardrail/locks.py", "    return _NO_RUNNER\n", "    return {\"command\": None}\n", "a test with no runner is skipped in silence"),
    ("guardrail/locks.py", "notes.append(f\"{path}: {len(sites)} assertion(s) not perturbed (declared", "problems.append(f\"{path}: {len(sites)} assertion(s) not perturbed (declared", "a suite declared not runnable blocks"),
    ("guardrail/core.py", "encoding=\"utf-8\", errors=\"replace\"", "encoding=\"utf-8\", errors=\"strict\"", "a binary file among the tests stops the lock"),
    ("guardrail/cli.py", "if core.is_merge(root):\n            return 0", "if False:\n            return 0", "merges are blocked by content locks"),
    ("guardrail/cli.py", "return 1 if problems else 0", "return 0", "a blocked lock exits 0"),
    ("guardrail/cli.py", "cannot look, so it does not pass: {error}\", file=sys.stderr)\n        return 2", "cannot look, so it does not pass: {error}\", file=sys.stderr)\n        return 0", "a lock that cannot look passes"),
    # --- perturbation in batch ---
    ("guardrail/locks.py", "if outcome.status == \"failed\"", "if True", "a sentinel found in a passed test counts as proof"),
    ("guardrail/locks.py", "proven = [i for i in remaining if messages and _mentions(messages, marks[i][1])]", "proven = [i for i in remaining if messages]", "any failed test counts as proof, sentinel or not"),
    ("guardrail/locks.py", "proven = [i for i in remaining if messages and _mentions(messages, marks[i][1])]", "proven = [i for i in remaining if messages is None or (messages and _mentions(messages, marks[i][1]))]", "a missing report proves everything"),
    ("guardrail/locks.py", "            token = text_token if is_string else number", "            token = \"ZZ_MUTATED_ZZ\" if is_string else \"987654\"", "every assertion shares one sentinel"),
    ("guardrail/locks.py", "            if token not in text:", "            if True:", "a sentinel that is already in the file is used"),
    ("guardrail/locks.py", "_BATCH_BASE, _BATCH_STRIDE = 987654000, 1000003", "_BATCH_BASE, _BATCH_STRIDE = 987654000, 1", "sentinels one apart collide with a printed difference"),
    ("guardrail/locks.py", "        for index in remaining:\n            changed[", "        for index in range(len(sites)):\n            changed[", "a proven assertion is not restored before the next round"),
    ("guardrail/locks.py", "            for site in chosen:\n                problems += _prove_alone(", "            for site in []:\n                problems += _prove_alone(", "what the batch cannot prove is never checked alone"),
    ("guardrail/locks.py", "messages = None if timed_out or code == 0 else _failed_messages(report)", "messages = None if code == 0 else _failed_messages(report)", "a round that timed out is read"),
    ("guardrail/locks.py", "messages = None if timed_out or code == 0 else _failed_messages(report)", "messages = None if timed_out else _failed_messages(report)", "a round that reported success is read"),
    ("guardrail/locks.py", "    while len(remaining) > 1:", "    while remaining:", "a batch of one is run"),
    ("guardrail/locks.py", "not isinstance(batch, dict) or batch.get(\"format\") not in REPORT_FORMATS or", "not isinstance(batch, dict) or", "an unknown report format is accepted"),
    ("guardrail/locks.py", "if \"{report}\" not in batch[\"args\"]:", "if False:", "a batch declaration without {report} is accepted"),
    ("guardrail/locks.py", "lambda *_: (restore(), os._exit(130))", "lambda *_: os._exit(130)", "an interrupt leaves the file perturbed"),
    ("guardrail/locks.py", "            while scratch:\n", "            while False:\n", "the report folder is left behind"),
    # --- the unperturbed run must reach an assertion before the batch may count for it ---
    ("guardrail/locks.py", "return 0, {index for index in range(len(sites)) if _seen_token(nonce, index) in output}", "return 0, set(range(len(sites)))", "every assertion counts as reached"),
    ("guardrail/locks.py", "if _seen_token(nonce, index) in output}", "if _seen_token(nonce, 0) in output}", "the marker of one assertion is read for another"),
    ("guardrail/locks.py", "\"ZZ_SEEN_\" + \"{nonce}_{index}\" + \"_ZZ\"", "\"ZZ_SEEN_{nonce}_{index}_ZZ\"", "a marker that is in the file's own text counts as seen"),
    ("guardrail/locks.py", "return _run(command, path, root, timeout), set()", "return 1, set()", "a baseline broken by the markers is reported as red"),
    ("guardrail/locks.py", "unreached = [site for index, site in enumerate(chosen) if index not in seen]", "unreached = []", "an assertion that was not reached is never checked"),
    ("guardrail/locks.py", "reached = [site for index, site in enumerate(chosen) if index in seen]", "reached = list(chosen)", "an assertion that was not reached joins the batch"),
    ("guardrail/locks.py", "_execute((command + \" \" + extra).strip(), path, root, timeout)", "_execute(command, path, root, timeout)", "the baseline args are not passed to the instrumented run"),
    ("guardrail/locks.py", "if not isinstance(batch.get(\"baselineArgs\", \"\"), str):", "if False:", "baseline args of the wrong type are accepted"),
    ("guardrail/locks.py", "if not key.startswith(\"GIT_\")}", "if True}", "the runner sees the git variables of the hook"),
    # --- time budget for new or changed tests ---
    ("guardrail/locks.py", "if (test.duration or 0) > budget[\"testMs\"]:", "if (test.duration or 0) >= budget[\"testMs\"]:", "a test exactly at its budget is refused"),
    ("guardrail/locks.py", "if total > budget[\"fileMs\"]:", "if total >= budget[\"fileMs\"]:", "a file exactly at its budget is refused"),
    ("guardrail/locks.py", "if (test.duration or 0) > budget[\"testMs\"]:", "if test.duration > budget[\"testMs\"]:", "a test without a duration stops the lock"),
    ("guardrail/locks.py", "total = sum(test.duration or 0 for test in tests)", "total = sum(test.duration for test in tests)", "a file with a test without a duration stops the lock"),
    ("guardrail/locks.py", "if changed is None or changed > written:", "if changed is None:", "a report older than the staged file is trusted"),
    ("guardrail/locks.py", "notes.append(f\"{path}: not in {report}", "problems.append(f\"{path}: not in {report}", "a staged test the report does not have blocks the commit"),
    ("guardrail/locks.py", "return [], [f\"{report} does not exist: the time budget was not checked (the related-tests hook writes it)\"]", "return [f\"{report} does not exist\"], []", "a missing report blocks the commit"),
    ("guardrail/locks.py", "return [f\"{report} is not a Jest-shaped JSON report, so the time budget cannot be checked\"], []", "return [], []", "a report that is not a report passes in silence"),
    ("guardrail/locks.py", "if settings is None or not staged:", "if settings is None:", "a commit that stages no test is judged by its report"),
    ("guardrail/locks.py", "if settings is None or not staged:", "if not staged:", "an unconfigured time budget is judged"),
    ("guardrail/locks.py", "    if raw is None:\n        return None\n", "    if False:\n        return None\n", "an unconfigured time budget is an error"),
    ("guardrail/locks.py", "if raw.get(\"format\") not in REPORT_FORMATS:", "if False:", "an unknown format of time budget report is accepted"),
    ("guardrail/locks.py", "if set(raw) - _TIME_BUDGET_KEYS:", "if False:", "a misspelled time budget key is ignored"),
    ("guardrail/locks.py", "or value <= 0:", "or value < 0:", "a time budget of zero is accepted"),
    ("guardrail/locks.py", "TIME_BUDGET_DEFAULTS = {\"testMs\": 5000, \"fileMs\": 20000}", "TIME_BUDGET_DEFAULTS = {\"testMs\": 5001, \"fileMs\": 20001}", "the default time budget changed"),
    ("guardrail/locks.py", "if os.path.isabs(name):", "if False:", "an absolute path of the report is not read relative to the repository"),
    ("guardrail/locks.py", "exact = [item for item in slow if name in (item.name, item.label)]", "exact = []", "a full name does not win over a longer name that contains it"),
    ("guardrail/locks.py", "    if len(found) > 1:\n", "    if False:\n", "a slow-test name that matches two tests excuses the first"),
    ("guardrail/locks.py", "    if not found:\n        return None, [f'slow-test:", "    if False:\n        return None, [f'slow-test:", "a stale slow-test line is accepted"),
    ("guardrail/locks.py", "if stated_ms <= item.limit:", "if stated_ms < item.limit:", "a stated duration equal to the budget excuses the test"),
    ("guardrail/locks.py", "if stated_ms <= item.limit:", "if False:", "any stated duration excuses the test"),
    ("guardrail/locks.py", "* (1000 if stated.group(2) == \"s\" else 1)", "* 1", "seconds are read as milliseconds"),
    ("guardrail/locks.py", "if len(reason) < core.MIN_REASON:", "if False:", "a slow-test line without a reason excuses the test"),
    ("guardrail/locks.py", "    if not stated:\n", "    if False:\n", "a slow-test line without a duration excuses the test"),
    ("guardrail/locks.py", "if len(fields) != 3 or not fields[0]:", "if len(fields) != 3:", "a slow-test line without a name excuses the only slow test"),
    ("guardrail/locks.py", "if len(fields) != 3 or not fields[0]:", "if not fields[0]:", "a slow-test line that is not in three parts is read"),
    ("guardrail/locks.py", "        if item in excused:\n            continue\n", "        if False:\n            continue\n", "a slow-test line excuses nothing"),
    ("guardrail/locks.py", "    return problems + found, notes\n", "    return problems, notes\n", "the time budget problems are dropped from the commit gate"),
    ("guardrail/locks.py", "return str(int(value)) if value == int(value) else f\"{value:.1f}\"", "return str(int(value))", "a fraction of a millisecond is cut"),
    ("guardrail/locks.py", "\" \".join([*test.get(\"ancestorTitles\", []), test.get(\"title\", \"\")]).strip()", "test.get(\"title\", \"\")", "a test without a full name loses its ancestors"),
    ("guardrail/cli.py", "problems, notes = locks.commit_gate(root, config, message)", "problems, notes = [p for lock in locks.COMMIT_GATE for p in lock(root, config, message)], []", "the commit gate does not run the time budget"),
    ("guardrail/cli.py", "locks.commit_gate(root, config, message)\n            return _report(command, problems, notes)", "locks.commit_gate(root, config, message)\n            return _report(command, problems)", "the notes of the commit gate are not shown"),
]


def selected(mutants):
    """The mutants to run: all of them, or those matching GUARDRAIL_MUTANTS."""
    wanted = [text for text in os.environ.get("GUARDRAIL_MUTANTS", "").split(",") if text]
    return [m for m in mutants if not wanted or any(text in m[3] or text in m[0] for text in wanted)]


class LocksAreGuarded(unittest.TestCase):
    def test_every_mutant_is_killed_by_the_scenarios(self):
        survivors, not_applied = [], []
        with tempfile.TemporaryDirectory() as snapshot:
            for name in ("guardrail", "bin", "tests"):
                shutil.copytree(os.path.join(HERE, name), os.path.join(snapshot, name), ignore=shutil.ignore_patterns("__pycache__", "tests_*.txt"))
            self.run_mutants(snapshot, survivors, not_applied)
        self.assertEqual(not_applied, [], "mutants that could not be applied prove nothing")
        self.assertEqual(survivors, [], "the scenarios stayed green with these locks broken")

    def run_mutants(self, snapshot, survivors, not_applied):
        for file, original, replacement, label in selected(MUTANTS):
            with tempfile.TemporaryDirectory() as tmp:
                for name in ("guardrail", "bin"):
                    shutil.copytree(os.path.join(snapshot, name), os.path.join(tmp, name))
                target = os.path.join(tmp, file)
                with open(target, encoding="utf-8") as handle:
                    source = handle.read()
                if source.count(original) != 1:
                    not_applied.append(f"{label} (found {source.count(original)} times)")
                    print(f"NOT APPLIED {file}: {label}", file=sys.stderr, flush=True)
                    continue
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write(source.replace(original, replacement))
                result = subprocess.run(
                    [sys.executable, "-m", "unittest", "-q", "-f", "tests.test_scenarios"],
                    cwd=snapshot,
                    env={**os.environ, "GUARDRAIL_HOME": tmp},
                    capture_output=True,
                )
                killed = result.returncode != 0
                # One line per mutant, so the log of a run says which lock each one broke.
                print(f"{'killed  ' if killed else 'SURVIVED'} {file}: {label}", file=sys.stderr, flush=True)
                if not killed:
                    survivors.append(label)


if __name__ == "__main__":
    unittest.main()
