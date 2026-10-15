"""Locks on the locks: break each lock on purpose and require the canaries to turn red.

A scenario suite that stays green when a lock is broken is not guarding that lock.
Each mutant replaces one literal that must occur EXACTLY once in its file; a
mutant that cannot be applied is a failure, because it proves nothing.

The run works on a snapshot of the repository taken when it starts, so the code can be
edited while it goes. A mutant stops at the first test that fails (that is a kill): the direct
tests of tests/test_units.py first, which take milliseconds, then the scenarios.
GUARDRAIL_MUTANTS=<text> runs only the mutants whose label or file contains the text
(comma-separated for several); it is for the work in progress, never for a release.
GUARDRAIL_MUTANT_WORKERS (default 3) is how many mutants run at once.
"""

import concurrent.futures
import os
import re
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
    ("guardrail/locks.py", "len(production) <= cap[\"files\"] and", "len(production) < cap[\"files\"] and", "size cap off by one on files"),
    ("guardrail/locks.py", "and lines <= cap[\"lines\"]:", "and lines < cap[\"lines\"]:", "size cap off by one on lines"),
    ("guardrail/core.py", "\"sizeCap\": {\"files\": 8, \"lines\": 400},", "\"sizeCap\": {\"files\": 9, \"lines\": 400},", "the default size cap in files changed"),
    ("guardrail/core.py", "\"sizeCap\": {\"files\": 8, \"lines\": 400},", "\"sizeCap\": {\"files\": 8, \"lines\": 401},", "the default size cap in lines changed"),
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
    ("guardrail/locks.py", "    if raw is None:\n        return None\n    if not isinstance(raw, dict) or not isinstance(raw.get(\"report\"), str)", "    if False:\n        return None\n    if not isinstance(raw, dict) or not isinstance(raw.get(\"report\"), str)", "an unconfigured time budget is an error"),
    ("guardrail/locks.py", "if raw.get(\"format\") not in REPORT_FORMATS:", "if False:", "an unknown format of time budget report is accepted"),
    ("guardrail/locks.py", "if set(raw) - _TIME_BUDGET_KEYS:", "if False:", "a misspelled time budget key is ignored"),
    ("guardrail/locks.py", "or value <= 0:", "or value < 0:", "a time budget of zero is accepted"),
    ("guardrail/locks.py", "TIME_BUDGET_DEFAULTS = {\"testMs\": 5000, \"fileMs\": 20000}", "TIME_BUDGET_DEFAULTS = {\"testMs\": 5001, \"fileMs\": 20001}", "the default time budget changed"),
    ("guardrail/core.py", "if os.path.isabs(name):", "if False:", "an absolute path of the report is not read relative to the repository"),
    ("guardrail/locks.py", "exact = [item for item in slow if name in (item.name, item.label)]", "exact = []", "a full name does not win over a longer name that contains it"),
    ("guardrail/locks.py", "    if len(found) > 1:\n", "    if False:\n", "a slow-test name that matches two tests excuses the first"),
    ("guardrail/locks.py", "    if not found:\n        return None, [f'slow-test:", "    if False:\n        return None, [f'slow-test:", "a stale slow-test line is accepted"),
    ("guardrail/locks.py", "if stated_ms <= item.limit:", "if stated_ms < item.limit:", "a stated duration equal to the budget excuses the test"),
    ("guardrail/locks.py", "if stated_ms <= item.limit:", "if False:", "any stated duration excuses the test"),
    ("guardrail/locks.py", "* (1000 if stated.group(2) == \"s\" else 1)", "* 1", "seconds are read as milliseconds"),
    ("guardrail/locks.py", "    if len(reason) < core.MIN_REASON:\n        return None", "    if False:\n        return None", "a slow-test line without a reason excuses the test"),
    ("guardrail/locks.py", "    if not stated:\n", "    if False:\n", "a slow-test line without a duration excuses the test"),
    ("guardrail/locks.py", "if len(fields) != 3 or not fields[0]:", "if len(fields) != 3:", "a slow-test line without a name excuses the only slow test"),
    ("guardrail/locks.py", "if len(fields) != 3 or not fields[0]:", "if not fields[0]:", "a slow-test line that is not in three parts is read"),
    ("guardrail/locks.py", "        if item in excused:\n            continue\n", "        if False:\n            continue\n", "a slow-test line excuses nothing"),
    ("guardrail/locks.py", "    return problems + found, notes\n", "    return problems, notes\n", "the time budget problems are dropped from the commit gate"),
    ("guardrail/core.py", "return str(int(value)) if value == int(value) else f\"{value:.1f}\"", "return str(int(value))", "a fraction of a millisecond is cut"),
    ("guardrail/locks.py", "\" \".join([*test.get(\"ancestorTitles\", []), test.get(\"title\", \"\")]).strip()", "test.get(\"title\", \"\")", "a test without a full name loses its ancestors"),
    ("guardrail/cli.py", "problems, notes = locks.commit_gate(root, config, message)", "problems, notes = [p for lock in locks.COMMIT_GATE for p in lock(root, config, message)], []", "the commit gate does not run the time budget"),
    ("guardrail/cli.py", "locks.commit_gate(root, config, message)\n            return _report(command, problems, notes)", "locks.commit_gate(root, config, message)\n            return _report(command, problems)", "the notes of the commit gate are not shown"),
    # --- the push suite's timing record ---
    ("guardrail/perf.py", "if before and now > before * (1 + TOTAL_GROWTH):", "if before and now >= before * (1 + TOTAL_GROWTH):", "a total exactly at the threshold warns"),
    ("guardrail/perf.py", "TOTAL_GROWTH = 0.10", "TOTAL_GROWTH = 0.20", "the total threshold changed"),
    ("guardrail/perf.py", "FILE_FACTOR = 2.0", "FILE_FACTOR = 3.0", "the file factor changed"),
    ("guardrail/perf.py", "FILE_FLOOR_MS = 100", "FILE_FLOOR_MS = 500", "the file floor changed upwards"),
    ("guardrail/perf.py", "FILE_FLOOR_MS = 100", "FILE_FLOOR_MS = 50", "the file floor changed downwards"),
    ("guardrail/perf.py", "TOP_FILES = 10", "TOP_FILES = 9", "the table shows fewer files"),
    ("guardrail/perf.py", "ms > old * FILE_FACTOR:", "ms >= old * FILE_FACTOR:", "a file exactly at the factor warns"),
    ("guardrail/perf.py", "and ms > FILE_FLOOR_MS and", "and", "a file below the floor warns"),
    ("guardrail/perf.py", "and ms > FILE_FLOOR_MS and", "and ms >= FILE_FLOOR_MS and", "a file exactly at the floor warns"),
    ("guardrail/perf.py", "if old > 0 and", "if", "a file that was free before has a ratio"),
    ("guardrail/perf.py", "grown.sort(key=lambda row: (-row[0], row[1]))", "grown.sort(key=lambda row: (row[0], row[1]))", "the table lists the smallest growth first"),
    ("guardrail/perf.py", "for growth, path, old, ms in grown[:TOP_FILES]:", "for growth, path, old, ms in grown:", "the table lists every file"),
    ("guardrail/perf.py", "        if ms > old:\n", "        if ms >= old:\n", "a file that did not change is listed as grown"),
    ("guardrail/perf.py", "for record in reversed(history):", "for record in history:", "the first run of a label is the one compared with"),
    ("guardrail/perf.py", "if record[\"label\"] == label:", "if True:", "a run is compared with a run of another label"),
    ("guardrail/perf.py", "isinstance(value, dict)\n        and ", "", "a line that is not an object is read as a record"),
    ("guardrail/perf.py", "and isinstance(value.get(\"label\"), str)\n", "\n", "a record without a label is accepted"),
    ("guardrail/perf.py", "and isinstance(value.get(\"tests\"), int)\n", "\n", "a record without a test count is accepted"),
    ("guardrail/perf.py", "and isinstance(value.get(\"totalMs\"), (int, float))\n", "\n", "a record without a total is accepted"),
    ("guardrail/perf.py", "and isinstance(value.get(\"files\"), dict)\n", "\n", "a record without its files is accepted"),
    ("guardrail/perf.py", "            if not line.strip():\n                continue\n", "", "a blank line of the history is a damaged record"),
    ("guardrail/perf.py", "    if not outcomes:\n", "    if False:\n", "a report with no tests is recorded as a free run"),
    ("guardrail/perf.py", "(outcome.duration or 0.0)", "outcome.duration", "a test without a duration stops the record"),
    ("guardrail/perf.py", "\"tests\": len(outcomes)", "\"tests\": len(files)", "tests are counted as files"),
    ("guardrail/cli.py", "return 1 if warnings and options.strict else 0", "return 1 if warnings else 0", "a warning fails without --strict"),
    ("guardrail/cli.py", "return 1 if warnings and options.strict else 0", "return 1 if options.strict else 0", "--strict fails without a warning"),
    ("guardrail/cli.py", "    perf.append(options.history, entry)  # always: a run that warns is still a run\n", "", "the run is not recorded"),
    ("guardrail/cli.py", "    warnings, lines = perf.compare(entry, perf.previous_run(history, options.label))\n    perf.append(", "    perf.append(options.history, entry)\n    history, notes = perf.read_history(options.history)\n    warnings, lines = perf.compare(entry, perf.previous_run(history, options.label))\n    perf.append(", "the run is compared with itself"),
    ("guardrail/cli.py", "commit = core.git(root, \"rev-parse\", \"--short\", \"HEAD\", check=False).strip() or None", "commit = core.git(root, \"rev-parse\", \"--short\", \"HEAD\", check=False).strip()", "a missing commit is an empty string"),
    ("guardrail/cli.py", "allow_abbrev=False", "allow_abbrev=True", "an abbreviated option is accepted"),
    ("guardrail/cli.py", "parser.add_argument(\"--report\", required=True,", "parser.add_argument(\"--report\", required=False,", "perf-gate runs without a report"),
    ("guardrail/cli.py", "parser.add_argument(\"--history\", required=True,", "parser.add_argument(\"--history\", required=False,", "perf-gate runs without a history"),
    ("guardrail/cli.py", "return stop.code if isinstance(stop.code, int) else 2", "return 0", "a usage error exits zero"),
    ("guardrail/perf.py", "DEFAULT_LABEL = \"default\"", "DEFAULT_LABEL = \"\"", "the default label is empty"),
    # --- every foreign key a migration adds has an index ---
    ("guardrail/schema.py", "    if index.partial:\n        return False\n", "", "a partial index covers a foreign key"),
    ("guardrail/schema.py", "any(set(c[: len(required)]) == set(required) for c in candidates)", "any(c[: len(required)] == list(required) for c in candidates)", "an index in another order of the same columns does not cover"),
    ("guardrail/schema.py", "any(set(c[: len(required)]) == set(required) for c in candidates)", "any(set(c) >= set(required) for c in candidates)", "an index that merely contains the columns covers"),
    ("guardrail/schema.py", "    while keys and keys[0] in ignored:", "    while False:", "an ignored leading column of the index is not skipped"),
    ("guardrail/schema.py", "return tuple(c for c in key.columns if c not in ignored) or key.columns", "return tuple(c for c in key.columns if c not in ignored)", "a key on the ignored column alone needs no index"),
    ("guardrail/schema.py", "return tuple(c for c in key.columns if c not in ignored) or key.columns", "return key.columns", "the ignored columns are required in the index"),
    ("guardrail/schema.py", "if key.file in files and not any(", "if not any(", "a key an earlier migration added is judged"),
    ("guardrail/schema.py", "and not any(covers(index, required_columns(key, ignored), ignored) for index in table.indexes)", "and any(covers(index, required_columns(key, ignored), ignored) for index in table.indexes)", "a key with an index is reported"),
    ("guardrail/schema.py", "table.indexes.append(Index(name_of(key.group(1)) if key.group(1) else None, _columns(listing), False))", "pass", "a primary key or a unique constraint of a table is not an index"),
    ("guardrail/schema.py", "if re.search(r\"\\b(?:PRIMARY\\s+KEY|UNIQUE)\\b\", rest, _FLAGS):", "if False:", "an inline primary key or unique is not an index"),
    ("guardrail/schema.py", "r\"\\b(?:PRIMARY\\s+KEY|UNIQUE)\\b\"", "r\"\\bPRIMARY\\s+KEY\\b\"", "an inline unique is not an index"),
    ("guardrail/schema.py", "r\"\\b(?:PRIMARY\\s+KEY|UNIQUE)\\b\"", "r\"\\bUNIQUE\\b\"", "an inline primary key is not an index"),
    ("guardrail/schema.py", "    rest = _blank_strings(element.strip()[head.end() :])", "    rest = element.strip()[head.end() :]", "a keyword in a default value is read as one"),
    ("guardrail/schema.py", "        elif text.startswith(\"--\", i):", "        elif False:", "a line comment is read as SQL"),
    ("guardrail/schema.py", "        elif text.startswith(\"/*\", i):", "        elif False:", "a block comment is read as SQL"),
    ("guardrail/schema.py", "    if quote == \"$\":", "    if False:", "a dollar-quoted body is read as SQL"),
    ("guardrail/schema.py", "    if quote in \"'\\\"\":", "    if quote == \"'\":", "a quoted identifier may hide a separator"),
    ("guardrail/schema.py", "        if text[i] == \"(\":\n            depth += 1\n        elif text[i] == \")\":\n            depth -= 1\n        elif text[i] == separator", "        if text[i] == separator", "a separator inside parentheses splits"),
    ("guardrail/schema.py", "                table.foreign_keys = [k for k in table.foreign_keys if k.constraint != gone]\n                table.indexes = [i for i in table.indexes if i.name != gone]", "                table.foreign_keys = [k for k in table.foreign_keys if k.constraint != gone]", "a dropped constraint keeps its index"),
    ("guardrail/schema.py", "                table.foreign_keys = [k for k in table.foreign_keys if k.constraint != gone]\n                table.indexes", "                table.indexes", "a dropped constraint keeps its foreign key"),
    ("guardrail/schema.py", "                table.foreign_keys = [k for k in table.foreign_keys if gone not in k.columns]\n                table.indexes = [i for i in table.indexes if gone not in i.columns]", "                table.foreign_keys = [k for k in table.foreign_keys if gone not in k.columns]", "a dropped column keeps its indexes"),
    ("guardrail/schema.py", "                table.foreign_keys = [k for k in table.foreign_keys if gone not in k.columns]\n", "", "a dropped column keeps its foreign keys"),
    ("guardrail/schema.py", "                    self.tables.pop(name_of(part), None)", "                    pass", "a dropped table keeps its indexes"),
    ("guardrail/schema.py", "table.indexes = [i._replace(name=new) if i.name == old else i for i in table.indexes]", "pass", "a renamed index keeps its old name"),
    ("guardrail/schema.py", "                    table.indexes = [i for i in table.indexes if i.name != name]\n            return\n        renamed", "                    pass\n            return\n        renamed", "a dropped index still counts"),
    ("guardrail/schema.py", "partial = bool(re.search(r\"\\bWHERE\\b\", statement[after:], _FLAGS))", "partial = False", "a partial index is not recognised as partial"),
    ("guardrail/schema.py", "columns.append(name_of(head.group(0)) if is_column else None)", "columns.append(name_of(head.group(0)) if head else None)", "an expression index is read as an index on its function"),
    ("guardrail/schema.py", "    last = re.findall(IDENT, qualified)[-1]", "    last = re.findall(IDENT, qualified)[0]", "a schema is read as the name of the table"),
    ("guardrail/schema.py", "return (last[1:-1].replace('\"\"', '\"') if last.startswith('\"') else last).lower()", "return (last[1:-1].replace('\"\"', '\"') if last.startswith('\"') else last)", "names are compared with their case"),
    ("guardrail/locks.py", "    if not staged:\n        return []\n    replay", "    replay", "a commit that stages no migration is judged"),
    ("guardrail/locks.py", "        if key in excused:\n            continue\n", "", "a no-index line excuses nothing"),
    ("guardrail/locks.py", "        if not bar or not named:", "        if not named:", "a no-index line without a separator is read"),
    ("guardrail/locks.py", "        elif len(reason) < core.MIN_REASON:\n            problems.append(f\"no-index:", "        elif False:\n            problems.append(f\"no-index:", "a no-index line without a reason excuses the key"),
    ("guardrail/locks.py", "            if not matched:\n                problems.append(f'no-index:", "            if False:\n                problems.append(f'no-index:", "a stale no-index line is accepted"),
    ("guardrail/locks.py", "matched = [key for key in missing if key.table == table and column in key.columns]", "matched = [key for key in missing if key.table == table]", "a no-index line for a table excuses all its keys"),
    ("guardrail/locks.py", "    if raw is None:\n        return None\n    if not isinstance(raw, dict) or not isinstance(raw.get(\"migrations\"), list)", "    if False:\n        return None\n    if not isinstance(raw, dict) or not isinstance(raw.get(\"migrations\"), list)", "an unconfigured foreign key lock is an error"),
    ("guardrail/locks.py", "or not raw[\"migrations\"] or not all(isinstance(g, str) for g in raw[\"migrations\"]):", "or not all(isinstance(g, str) for g in raw[\"migrations\"]):", "an empty list of migrations is accepted"),
    ("guardrail/locks.py", "if not isinstance(ignored, list) or not all(isinstance(c, str) for c in ignored):", "if False:", "ignored columns of the wrong type are accepted"),
    ("guardrail/locks.py", "    if set(raw) - _FK_INDEX_KEYS:", "    if False:", "a misspelled foreign key lock key is ignored"),
    ("guardrail/locks.py", "COMMIT_GATE = [fix_needs_test, size_cap, cross_view, switch_on_trigger, fk_index]", "COMMIT_GATE = [fix_needs_test, size_cap, cross_view, switch_on_trigger]", "the foreign key lock is not in the commit gate"),
    ("guardrail/locks.py", "            os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))\n", "", "a perturbed file comes back with a new modification time"),
    # --- fake tests: every pattern and every kind of comment ---
    ("guardrail/locks.py", "\\(\\s*\\1\\s*\\)", "\\(\\s*[^)]*\\)", "any assertion on a literal is a tautology"),
    ("guardrail/locks.py", "    (re.compile(r\"\\b(?:xit|xdescribe|xtest|fit|fdescribe)\\s*\\(\"), \"focused or skipped test\"),\n", "", "xit, fit and their family are not seen"),
    ("guardrail/locks.py", "r\"\\b(?:xit|", "r\"(?:xit|", "a name that ends in xit or fit is a skip"),
    ("guardrail/locks.py", "    (re.compile(r\"expect\\(\\s*true\\s*\\)\\s*\\.\\s*toBeTruthy\\(\\s*\\)\"), \"assertion that cannot fail\"),\n", "", "expect(true).toBeTruthy() is not seen"),
    ("guardrail/locks.py", "(?:only|skip|todo|skipIf|runIf)", "(?:skip|todo|skipIf|runIf)", "it.only is not seen"),
    ("guardrail/locks.py", "(?:only|skip|todo|skipIf|runIf)", "(?:only|skip|skipIf|runIf)", "it.todo is not seen"),
    ("guardrail/locks.py", "(?:only|skip|todo|skipIf|runIf)", "(?:only|skip|todo|runIf)", "it.skipIf is not seen"),
    ("guardrail/locks.py", "(?:only|skip|todo|skipIf|runIf)", "(?:only|skip|todo|skipIf)", "it.runIf is not seen"),
    ("guardrail/locks.py", "(?:describe|it|test|suite|bench)", "(?:describe|it|test)", "suite.skip and bench.skip are not seen"),
    ("guardrail/locks.py", "return stripped.startswith(\"//\") or stripped.startswith(\"*\") or stripped.startswith(\"/*\")", "return stripped.startswith(\"*\") or stripped.startswith(\"/*\")", "a line comment is read as code"),
    ("guardrail/locks.py", "return stripped.startswith(\"//\") or stripped.startswith(\"*\") or stripped.startswith(\"/*\")", "return stripped.startswith(\"//\") or stripped.startswith(\"/*\")", "the middle of a block comment is read as code"),
    ("guardrail/locks.py", "return stripped.startswith(\"//\") or stripped.startswith(\"*\") or stripped.startswith(\"/*\")", "return stripped.startswith(\"//\") or stripped.startswith(\"*\")", "the start of a block comment is read as code"),
    ("guardrail/locks.py", "found.append((number, reason))\n                break", "found.append((number, reason))", "a line that two patterns match is found twice"),
    # --- perturbation: which values and which matchers ---
    ("guardrail/locks.py", "\\.(toBe|toEqual|toStrictEqual|toHaveLength|toBeCloseTo|toHaveBeenCalledTimes)\\(", "\\.(toBe|toStrictEqual|toHaveLength|toBeCloseTo|toHaveBeenCalledTimes)\\(", "toEqual is not perturbed"),
    ("guardrail/locks.py", "\\.(toBe|toEqual|toStrictEqual|toHaveLength|toBeCloseTo|toHaveBeenCalledTimes)\\(", "\\.(toBe|toEqual|toHaveLength|toBeCloseTo|toHaveBeenCalledTimes)\\(", "toStrictEqual is not perturbed"),
    ("guardrail/locks.py", "\\.(toBe|toEqual|toStrictEqual|toHaveLength|toBeCloseTo|toHaveBeenCalledTimes)\\(", "\\.(toBe|toEqual|toStrictEqual|toBeCloseTo|toHaveBeenCalledTimes)\\(", "toHaveLength is not perturbed"),
    ("guardrail/locks.py", "\\.(toBe|toEqual|toStrictEqual|toHaveLength|toBeCloseTo|toHaveBeenCalledTimes)\\(", "\\.(toBe|toEqual|toStrictEqual|toHaveLength|toHaveBeenCalledTimes)\\(", "toBeCloseTo is not perturbed"),
    ("guardrail/locks.py", "\\.(toBe|toEqual|toStrictEqual|toHaveLength|toBeCloseTo|toHaveBeenCalledTimes)\\(", "\\.(toBe|toEqual|toStrictEqual|toHaveLength|toBeCloseTo)\\(", "toHaveBeenCalledTimes is not perturbed"),
    ("guardrail/locks.py", "(?=\\s*[,)])", "", "a computed expected value is perturbed in its first literal"),
    ("guardrail/locks.py", "(-?\\d[\\d_]*(?:\\.\\d+)?|", "(\\d[\\d_]*(?:\\.\\d+)?|", "a negative number is not perturbed"),
    ("guardrail/locks.py", "\\d[\\d_]*", "\\d*", "a number with an underscore is not perturbed"),
    ("guardrail/locks.py", "(?:\\.\\d+)?|'(?:", "|'(?:", "a decimal number is not perturbed"),
    ("guardrail/locks.py", "'(?:[^'\\\\]|\\\\.)*'|", "'[^']*'|", "a string with an escaped quote is not perturbed"),
    ("guardrail/locks.py", "|true|false)(?=", "|true)(?=", "false is not perturbed"),
    ("guardrail/locks.py", "\"123456\" if literal.replace(\"_\", \"\") == _NUMBER_SENTINEL else _NUMBER_SENTINEL", "_NUMBER_SENTINEL", "the sentinel replaces itself"),
    ("guardrail/locks.py", "\"123456\" if literal.replace(\"_\", \"\") == _NUMBER_SENTINEL else _NUMBER_SENTINEL", "\"123456\" if literal == _NUMBER_SENTINEL else _NUMBER_SENTINEL", "the sentinel written with an underscore replaces itself"),
    ("guardrail/locks.py", "\"false\" if literal == \"true\" else \"true\"", "\"true\"", "true is replaced by true"),
    ("guardrail/locks.py", "literal[0] + _STRING_SENTINEL + literal[0]", "_STRING_SENTINEL", "a string loses its quotes when replaced"),
    # --- globs ---
    ("guardrail/core.py", "out.append(\"[^/]\")", "out.append(\".\")", "? crosses directories"),
    ("guardrail/core.py", "out.append(\"[^/]*\")", "out.append(\".*\")", "* crosses directories"),
    ("guardrail/core.py", "out.append(\".*\")", "out.append(\"[^/]*\")", "** does not cross directories"),
    ("guardrail/core.py", "out.append(\"(?:.*/)?\")", "out.append(\".*/\")", "**/ needs a directory"),
    ("guardrail/core.py", "out.append(re.escape(pattern[i]))", "out.append(pattern[i])", "a character of a glob is a regular expression"),
    ("guardrail/core.py", "re.compile(\"^\" + \"\".join(out) + \"$\")", "re.compile(\"^\" + \"\".join(out))", "a glob matches the start of a path"),
    ("guardrail/core.py", "            i += 3\n", "            i += 2\n", "**/ leaves its slash behind"),
    ("guardrail/core.py", "return any(glob_to_regex(p).match(path) for p in patterns)", "return all(glob_to_regex(p).match(path) for p in patterns)", "a path must match every pattern"),
    # --- the reader of a test report ---
    ("guardrail/locks.py", "if not isinstance(test[\"status\"], str) or not isinstance(test[\"failureMessages\"], list):", "if not isinstance(test[\"failureMessages\"], list):", "a report whose status is not a string is read"),
    ("guardrail/locks.py", "if not isinstance(test[\"status\"], str) or not isinstance(test[\"failureMessages\"], list):", "if not isinstance(test[\"status\"], str):", "a report whose failure messages are not a list is read"),
    ("guardrail/locks.py", "if not all(isinstance(m, str) for m in test[\"failureMessages\"]):", "if False:", "a report whose failure message is not a string is read"),
    # --- the rules of CLAUDE.md: Python 3.9 syntax and the standard library ---
    ("guardrail/cli.py", "def main(argv: list[str] | None = None) -> int:\n    args", "def main(argv: list[str] | None = None) -> int:\n    match 1:\n        case 1:\n            pass\n    args", "syntax newer than Python 3.9"),
    ("guardrail/locks.py", "import secrets\n", "import secrets\nimport yaml\n", "an import that is not the standard library"),
]


def selected(mutants):
    """The mutants to run: all of them, or those matching GUARDRAIL_MUTANTS."""
    wanted = [text for text in os.environ.get("GUARDRAIL_MUTANTS", "").split(",") if text]
    return [m for m in mutants if not wanted or any(text in m[3] or text in m[0] for text in wanted)]


def killer(result):
    """` <- <first failing scenario>` from the output of a failed run (the first FAIL or ERROR header)."""
    first = re.search(r"^(?:FAIL|ERROR): (.+)$", result.stderr.decode("utf-8", "replace"), re.M)
    return f" <- {first.group(1)}" if first else " <- (no failing scenario named in the output)"


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
        # Mutants are independent (each has its own copy of the code and the scenarios build their own
        # repositories), so a few run at once. GUARDRAIL_MUTANT_WORKERS=1 runs them one by one.
        workers = int(os.environ.get("GUARDRAIL_MUTANT_WORKERS", "3"))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for label, outcome in pool.map(lambda m: self.run_mutant(snapshot, m), selected(MUTANTS)):
                if outcome == "survived":
                    survivors.append(label)
                elif outcome == "not applied":
                    not_applied.append(label)

    def run_mutant(self, snapshot, mutant):
        """Break the code in a copy, run the scenarios on it. Returns (label, killed | survived | not applied)."""
        file, original, replacement, label = mutant
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("guardrail", "bin"):
                shutil.copytree(os.path.join(snapshot, name), os.path.join(tmp, name))
            target = os.path.join(tmp, file)
            with open(target, encoding="utf-8") as handle:
                source = handle.read()
            if source.count(original) != 1:
                print(f"NOT APPLIED {file}: {label}", file=sys.stderr, flush=True)
                return f"{label} (found {source.count(original)} times)", "not applied"
            mutated = source.replace(original, replacement)
            try:
                compile(mutated, target, "exec")
            except SyntaxError as error:
                # Code that does not compile is "killed" by any scenario at all: that proves nothing about the lock.
                print(f"NOT APPLIED {file}: {label} (does not compile: {error.msg}, line {error.lineno})", file=sys.stderr, flush=True)
                return f"{label} (does not compile: {error.msg})", "not applied"
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(mutated)
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "-q", "-f", "tests.test_units", "tests.test_scenarios"],
                cwd=snapshot,
                env={**os.environ, "GUARDRAIL_HOME": tmp},
                capture_output=True,
            )
            killed = result.returncode != 0
            # One line per mutant, so the log of a run says which lock each one broke and WHICH scenario
            # saw it: a mutant killed by an incidental crash looks different from one killed by its lock's scenario.
            print(f"{'killed  ' if killed else 'SURVIVED'} {file}: {label}{killer(result) if killed else ''}", file=sys.stderr, flush=True)
            return label, "killed" if killed else "survived"


if __name__ == "__main__":
    unittest.main()
