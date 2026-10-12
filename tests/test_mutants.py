"""Locks on the locks: break each lock on purpose and require the canaries to turn red.

A scenario suite that stays green when a lock is broken is not guarding that lock.
Each mutant replaces one literal that must occur EXACTLY once in its file; a
mutant that cannot be applied is a failure, because it proves nothing.
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
    ("guardrail/locks.py", "if _run(command, path, root, settings[\"timeoutSeconds\"]) != 0:", "if False:", "a red test counts as a killed perturbation"),
    ("guardrail/locks.py", "        finally:\n            restore()", "        finally:\n            pass", "the file is left perturbed"),
    ("guardrail/locks.py", "if cited_line < 1 or cited_line > len(lines):", "if cited_line < 1:", "citation past the end is accepted"),
    ("guardrail/locks.py", "elif not lines[cited_line - 1].strip():", "elif False:", "citation of a blank line is accepted"),
    ("guardrail/locks.py", "if age > stale_days:\n                stale.append(", "if age > stale_days:\n                problems.append(", "an old check fails the commit"),
    ("guardrail/locks.py", "if state and state not in _STATES:", "if False:", "any registry state is accepted"),
    ("guardrail/core.py", "if line.startswith(key + \":\") and len(line[len(key) + 1 :].strip()) >= min_reason:", "if (key + \":\") in line and len(line.split(key + \":\")[1].strip()) >= min_reason:", "a hatch mentioned inside a sentence opens the lock"),
    ("guardrail/core.py", "len(line[len(key) + 1 :].strip()) >= min_reason", "len(line[len(key) + 1 :].strip()) >= 0", "a hatch without a reason opens the lock"),
    ("guardrail/core.py", "\"--diff-filter=ACMR\"", "\"--diff-filter=ACMRD\"", "a deleted test counts as a staged test"),
    ("guardrail/core.py", "if matches(path, self[\"ignore\"]) or self.is_test(path):\n            return False", "if matches(path, self[\"ignore\"]):\n            return False", "tests count as production"),
    ("guardrail/locks.py", "    return _NO_RUNNER\n", "    return {\"command\": None}\n", "a test with no runner is skipped in silence"),
    ("guardrail/locks.py", "notes.append(f\"{path}: {len(sites)} assertion(s) not perturbed (declared", "problems.append(f\"{path}: {len(sites)} assertion(s) not perturbed (declared", "a suite declared not runnable blocks"),
    ("guardrail/core.py", "encoding=\"utf-8\", errors=\"replace\"", "encoding=\"utf-8\", errors=\"strict\"", "a binary file among the tests stops the lock"),
    ("guardrail/cli.py", "if core.is_merge(root):\n            return 0", "if False:\n            return 0", "merges are blocked by content locks"),
    ("guardrail/cli.py", "return 1 if problems else 0", "return 0", "a blocked lock exits 0"),
    ("guardrail/cli.py", "cannot look, so it does not pass: {error}\", file=sys.stderr)\n        return 2", "cannot look, so it does not pass: {error}\", file=sys.stderr)\n        return 0", "a lock that cannot look passes"),
    # --- perturbation in batch ---
    ("guardrail/locks.py", "if test[\"status\"] == \"failed\":", "if True:", "a sentinel found in a passed test counts as proof"),
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
    ("guardrail/locks.py", "not isinstance(batch, dict) or batch.get(\"format\") not in _BATCH_FORMATS or", "not isinstance(batch, dict) or", "an unknown report format is accepted"),
    ("guardrail/locks.py", "if \"{report}\" not in batch[\"args\"]:", "if False:", "a batch declaration without {report} is accepted"),
    ("guardrail/locks.py", "lambda *_: (restore(), os._exit(130))", "lambda *_: os._exit(130)", "an interrupt leaves the file perturbed"),
    ("guardrail/locks.py", "            while scratch:\n", "            while False:\n", "the report folder is left behind"),
]


class LocksAreGuarded(unittest.TestCase):
    def test_every_mutant_is_killed_by_the_scenarios(self):
        survivors, not_applied = [], []
        for file, original, replacement, label in MUTANTS:
            with tempfile.TemporaryDirectory() as tmp:
                for name in ("guardrail", "bin"):
                    shutil.copytree(os.path.join(HERE, name), os.path.join(tmp, name))
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
                    [sys.executable, "-m", "unittest", "-q", "tests.test_scenarios"],
                    cwd=HERE,
                    env={**os.environ, "GUARDRAIL_HOME": tmp},
                    capture_output=True,
                )
                killed = result.returncode != 0
                # One line per mutant, so the log of a run says which lock each one broke.
                print(f"{'killed  ' if killed else 'SURVIVED'} {file}: {label}", file=sys.stderr, flush=True)
                if not killed:
                    survivors.append(label)
        self.assertEqual(not_applied, [], "mutants that could not be applied prove nothing")
        self.assertEqual(survivors, [], "the scenarios stayed green with these locks broken")


if __name__ == "__main__":
    unittest.main()
