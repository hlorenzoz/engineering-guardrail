"""Direct tests of the readers and tables that need no repository.

The scenarios (test_scenarios.py) prove each lock through the real command line, one
throwaway repository per case: about half a second each. A rule that is a pure function or a
table (which lines count as a fake test, which expected values can be replaced, what a glob
matches, which syntax and imports the code may use) is proven here, in milliseconds, one line
per case, and the scenarios keep one case per lock behaviour. GUARDRAIL_HOME points these at
another copy of the code, as it does for the scenarios: test_mutants.py breaks the code in a
copy and requires something in this file or in the scenarios to turn red.
"""

import ast
import importlib.util
import json
import os
import re
import sys
import sysconfig
import tempfile
import unittest

HOME = os.environ.get("GUARDRAIL_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HOME)  # the code under test is the copy HOME names, never one found by accident

from guardrail import core, locks  # noqa: E402


class TheCodeUnderTest(unittest.TestCase):
    def test_the_units_read_the_code_they_are_pointed_at(self):
        # If these tests imported another copy than the one a mutant broke, every mutant would
        # survive them for the wrong reason, and so would every real change.
        self.assertEqual(os.path.dirname(os.path.realpath(locks.__file__)), os.path.realpath(os.path.join(HOME, "guardrail")))


class FakeTests(unittest.TestCase):
    """`fake_tests_in`: which line is a test that does not run or cannot fail."""

    # A kind of finding -> lines that must bite with it. The words of the reason are the lock's
    # message, proven through the command line; here each kind must give ONE reason, and the
    # kinds must differ from each other.
    BITES = {
        "focused or skipped": [
            "describe.only('a', () => { f() })",
            "it.only('a', () => { expect(f()).toBe(1) })",
            "test.only('a', () => { expect(f()).toBe(1) })",
            "it.skip('a', () => { expect(f()).toBe(1) })",
            "describe.skip('a', () => { f() })",
            "suite.skip('a', () => { f() })",
            "bench.skip('a', () => { f() })",
            "it.todo('a')",
            "test.todo('a')",
            "it.skipIf(isCi)('a', () => { expect(f()).toBe(1) })",
            "test.runIf(hasDb)('a', () => { expect(f()).toBe(1) })",
            "xit('a', () => { expect(f()).toBe(1) })",
            "fit('a', () => { expect(f()).toBe(1) })",
            "xdescribe('a', () => { f() })",
            "xtest('a', () => { f() })",
            "fdescribe('a', () => { f() })",
            "  xit ('a', () => { f() })",
        ],
        "a literal compared with itself": [
            "expect(true).toBe(true)",
            "expect(false).toBe(false)",
            "expect(null).toBe(null)",
            "expect(undefined).toBe(undefined)",
            "expect(2).toBe(2)",
            "expect(2).toEqual(2)",
            "expect(-1.5).toStrictEqual(-1.5)",
            "expect('a').toBe('a')",
            'expect("a b").toBe("a b")',
            "expect( 3 ) . toBe( 3 )",
        ],
        "an assertion that cannot fail": [
            "expect(true).toBeTruthy()",
            "expect( true ) . toBeTruthy( )",
        ],
        "an empty body": [
            "it('does it', () => {})",
            'test("does it", async () => {})',
            "it(`does it`, () => { })",
            "it('does it', async () => {  })",
        ],
    }

    QUIET = [
        # The same literal on both sides is the rule: different values are a real assertion.
        "expect(2).toBe(3)",
        "expect('a').toBe('b')",
        "expect(true).toBe(false)",
        "expect(null).toEqual(undefined)",
        "expect(total(2)).toBe(4)",
        # A name that merely contains the name of a skip.
        "benefit(x)",
        "exit(1)",
        "process.exit(1)",
        "const outfit = fit",
        "mainit('a')",
        # A test with a body.
        "it('adds', () => { expect(add(1, 1)).toBe(2) })",
        "it('adds', async () => { await run() })",
        # A line that only talks about it.
        "// it.skip('a') was removed",
        "    // xit('a', () => { f() })",
        " * it.only('a')",
        " * expect(true).toBeTruthy()",
        "/* xit('a') */",
        "it.skip('a', () => { f() }) // guardrail: allow-skip needs a database",
    ]

    def test_each_form_bites_on_its_line_and_each_kind_has_one_reason(self):
        reasons = {}
        for kind, lines in self.BITES.items():
            for line in lines:
                with self.subTest(line=line):
                    found = locks.fake_tests_in(line)
                    self.assertEqual([number for number, _ in found], [1], "one finding for the line")
                    reasons.setdefault(kind, set()).add(found[0][1])
        self.assertEqual({kind: len(found) for kind, found in reasons.items()}, {kind: 1 for kind in self.BITES}, "a kind has one reason")
        self.assertEqual(len({reason for found in reasons.values() for reason in found}), len(self.BITES), "two kinds share a reason")

    def test_an_assertion_that_cannot_fail_says_so(self):
        # The other three reasons are proven through the command line; this one has no scenario.
        ((_, reason),) = locks.fake_tests_in("expect(true).toBeTruthy()")
        self.assertIn("cannot fail", reason)

    def test_each_quiet_line_stays_quiet(self):
        for line in self.QUIET:
            with self.subTest(line=line):
                self.assertEqual(locks.fake_tests_in(line), [])

    def test_a_line_that_two_patterns_match_gives_one_finding_with_the_first_reason(self):
        both = "it.only('a', () => { expect(2).toBe(2) })"  # a focused test, and a tautology in it
        (found,) = locks.fake_tests_in(both)
        self.assertEqual(found, locks.fake_tests_in("it.only('a', () => { f() })")[0])

    def test_findings_carry_their_line_numbers_and_only_the_asked_lines_are_judged(self):
        text = "it('ok', () => { expect(f()).toBe(1) })\nit.only('a', () => { f() })\n\nxit('b', () => { f() })\n"
        self.assertEqual([number for number, _ in locks.fake_tests_in(text)], [2, 4])
        self.assertEqual([number for number, _ in locks.fake_tests_in(text, {4})], [4])
        self.assertEqual(locks.fake_tests_in(text, {1, 3}), [])


class Perturbations(unittest.TestCase):
    """`perturbations`: which expected value can be replaced, and by what kind of value."""

    # (text before the literal, the literal, text after it, its kind). Every line is replaced in
    # its literal only; the digits of the replacement are the lock's own decision.
    PERTURBED = [
        ("expect(sum).toBe(", "42", ")", "number"),
        ("expect(a).toEqual(", "0", ")", "number"),
        ("expect(a).toStrictEqual(", "7", ")", "number"),
        ("expect(a).toHaveLength(", "3", ")", "number"),
        ("expect(a).toBeCloseTo(", "1.5", ", 2)", "number"),
        ("expect(spy).toHaveBeenCalledTimes(", "2", ")", "number"),
        ("expect(a).toBe(", "-5", ")", "number"),
        ("expect(a).toBe(", "1_000", ")", "number"),
        ("expect(a).toBe(", "'a'", ")", "single-quoted string"),
        ("expect(a).toBe(", '"b c"', ")", "double-quoted string"),
        ("expect(a).toBe(", "'it\\'s'", ")", "single-quoted string"),
        ("expect(a).toBe(", "true", ")", "boolean"),
        ("expect(a).toBe(", "false", ")", "boolean"),
        ("    await expect(page.locator('x')).toHaveLength(", "4", ");", "number"),
        # One per line: the first.
        ("expect(a).toBe(", "1", "); expect(b).toBe(2)", "number"),
    ]

    LEFT_OUT = [
        # Comments.
        "// expect(a).toBe(1)",
        "    * expect(a).toBe(1)",
        "/* expect(a).toBe(1) */",
        # A negated assertion: the wrong value would not make it fail.
        "expect(a).not.toBe(1)",
        "expect(a).resolves.not.toEqual('x')",
        # A computed expected value is not a literal.
        "expect(a).toBe(1 + 2)",
        "expect(a).toBe(3 * rate)",
        "expect(a).toBe(compute(3))",
        "expect(a).toBe(-1 * y)",
        "expect(a).toBe('a' + b)",
        # A value that is not a simple literal.
        "expect(a).toBe(y)",
        "expect(a).toBe(null)",
        "expect(a).toBe(undefined)",
        "expect(a).toBe(`a`)",
        # A matcher outside the list.
        "expect(a).toBeGreaterThan(3)",
        "expect(a).toContain('a')",
        # Nothing to replace.
        "const total = 3",
    ]

    def test_each_expected_value_is_replaced_in_its_literal_only(self):
        for before, literal, after, kind in self.PERTURBED:
            line = before + literal + after
            with self.subTest(line=line):
                ((number, found, mutated),) = locks.perturbations(line)
                self.assertEqual(number, 1)
                self.assertEqual(found, literal)
                self.assertTrue(mutated.startswith(before) and mutated.endswith(after), mutated)
                middle = mutated[len(before) : len(mutated) - len(after)]
                self.assertNotEqual(middle, literal, "the value was not replaced")
                if kind == "number":
                    self.assertRegex(middle, r"^\d+$")
                elif kind == "boolean":
                    self.assertIn(middle, ("true", "false"))
                else:
                    self.assertEqual((middle[0], middle[-1]), (literal[0], literal[-1]), "the quotes were lost")
                    self.assertNotEqual(middle[1:-1], literal[1:-1])

    def test_each_line_that_cannot_be_replaced_is_left_out(self):
        for line in self.LEFT_OUT:
            with self.subTest(line=line):
                self.assertEqual(locks.perturbations(line), [])

    def test_the_hatch_leaves_a_line_out_and_it_is_the_hatch_that_does(self):
        line = "expect(a).toBe(1.5)"
        self.assertEqual(len(locks.perturbations(line)), 1)
        self.assertEqual(locks.perturbations(f"{line}  // {locks.NO_PERTURB} float noise"), [])

    def test_a_value_equal_to_the_replacement_is_replaced_by_another(self):
        # The replacement must be a different NUMBER from what was there (987_654 is 987654), or the
        # assertion would stay green for a reason that is not its own. The replacement is learned, not written here.
        ((_, _, mutated),) = locks.perturbations("expect(a).toBe(1)")
        learned = re.search(r"\((\d+)\)", mutated).group(1)
        for literal in (learned, learned[:3] + "_" + learned[3:]):
            with self.subTest(literal=literal):
                ((_, _, replaced),) = locks.perturbations(f"expect(a).toBe({literal})")
                value = re.search(r"\(([\d_]+)\)", replaced).group(1)
                self.assertNotEqual(int(value.replace("_", "")), int(literal.replace("_", "")))

    def test_lines_are_numbered_from_one_and_only_the_asked_ones_are_judged(self):
        text = "expect(a).toBe(1)\n// note\n\nexpect(b).toBe(2)\nexpect(c).toBe(3)\n"
        self.assertEqual([number for number, _, _ in locks.perturbations(text)], [1, 4, 5])
        self.assertEqual([number for number, _, _ in locks.perturbations(text, {4})], [4])
        self.assertEqual(locks.perturbations(text, {2, 3}), [])


class Globs(unittest.TestCase):
    """`glob_to_regex` and `matches`: `**` crosses directories, `*` and `?` do not."""

    # (pattern, paths that match, paths that do not)
    TABLE = [
        ("src/**", ["src/a.ts", "src/x/y/a.ts"], ["a.ts", "lib/src/a.ts", "srcx/a.ts"]),
        ("**/*.test.ts", ["a.test.ts", "x/a.test.ts", "x/y/a.test.ts"], ["a.test.tsx", "a.test.ts.bak", "a.ts"]),
        ("src/*.ts", ["src/a.ts"], ["src/x/a.ts", "src/a.tsx", "lib/src/a.ts"]),
        ("src/?.ts", ["src/a.ts"], ["src/ab.ts", "src/.ts", "src//.ts"]),
        ("a/**/b", ["a/b", "a/x/b", "a/x/y/b"], ["a/xb", "x/a/b"]),
        ("test/**", ["test/a.ts", "test/x/a.ts"], ["test", "tests/a.ts"]),
        # Every other character is itself, not a regular expression.
        ("src/a.ts", ["src/a.ts"], ["src/aXts", "xsrc/a.ts", "src/a.tsx"]),
        ("a+b.ts", ["a+b.ts"], ["aab.ts", "ab.ts"]),
        ("src/[id]/page.tsx", ["src/[id]/page.tsx"], ["src/i/page.tsx", "src/d/page.tsx"]),
        ("src/(x)/a.ts", ["src/(x)/a.ts"], ["src/x/a.ts"]),
    ]

    def test_a_glob_matches_its_paths_and_only_them(self):
        for pattern, yes, no in self.TABLE:
            for path in yes:
                with self.subTest(pattern=pattern, path=path, expect="match"):
                    self.assertTrue(core.glob_to_regex(pattern).match(path))
            for path in no:
                with self.subTest(pattern=pattern, path=path, expect="no match"):
                    self.assertFalse(core.glob_to_regex(pattern).match(path))

    def test_a_path_matches_when_any_of_the_patterns_does(self):
        patterns = ["docs/**", "src/*.ts"]
        self.assertTrue(core.matches("src/a.ts", patterns))
        self.assertTrue(core.matches("docs/x/a.md", patterns))
        self.assertFalse(core.matches("lib/a.ts", patterns))
        self.assertFalse(core.matches("src/a.ts", []))


class ConfigShape(unittest.TestCase):
    """`Config`: a key of the wrong type is refused, naming the key and the type it expects.

    Without it, `"production": "src/**"` is read one letter at a time and the lock never bites, and
    `"sizeCap": 5` ends in a traceback. `timeBudget` and `fkIndex` keep their own validation in
    `guardrail/locks.py`; the configuration leaves them to it."""

    # (what the file holds, the key it is refused for, what that key must be)
    WRONG = [
        ({"production": "src/**"}, "production", "a list of strings"),
        ({"production": ["src/**", 3]}, "production", "a list of strings"),
        ({"production": None}, "production", "a list of strings"),
        ({"tests": "test/**"}, "tests", "a list of strings"),
        ({"tests": {"a": "test/**"}}, "tests", "a list of strings"),
        ({"ignore": "dist/**"}, "ignore", "a list of strings"),
        ({"ignore": [["dist/**"]]}, "ignore", "a list of strings"),
        ({"sizeCap": 5}, "sizeCap", "an object"),
        ({"sizeCap": "5"}, "sizeCap", "an object"),
        ({"sizeCap": [8, 400]}, "sizeCap", "an object"),
        ({"sizeCap": {"files": "8"}}, "sizeCap.files", "a whole number"),
        ({"sizeCap": {"files": 8.5}}, "sizeCap.files", "a whole number"),
        ({"sizeCap": {"files": True}}, "sizeCap.files", "a whole number"),
        ({"sizeCap": {"lines": None}}, "sizeCap.lines", "a whole number"),
        ({"crossView": {"paths": ["a/**"], "tests": ["t/**"]}}, "crossView", "a list of objects"),
        ({"crossView": ["a/**"]}, "crossView", "a list of objects"),
        ({"crossView": [{"paths": "a/**", "tests": ["t/**"]}]}, "crossView[0].paths", "a list of strings"),
        ({"crossView": [{"paths": ["a/**"]}]}, "crossView[0].tests", "a list of strings"),
        ({"crossView": [{"paths": ["a/**"], "tests": ["t/**"], "name": 5}]}, "crossView[0].name", "a string"),
        ({"crossView": [{"paths": ["a/**"], "tests": ["t/**"]}, {"paths": 1, "tests": []}]}, "crossView[1].paths", "a list of strings"),
        ({"perturb": []}, "perturb", "an object"),
        ({"perturb": {"runners": "python3 {file}"}}, "perturb.runners", "a list of objects"),
        ({"perturb": {"runners": ["python3 {file}"]}}, "perturb.runners", "a list of objects"),
        ({"perturb": {"runners": [{"files": 1, "command": "python3 {file}"}]}}, "perturb.runners[0].files", "a string"),
        ({"perturb": {"runners": [{"command": "python3 {file}"}]}}, "perturb.runners[0].files", "a string"),
        ({"perturb": {"runners": [{"files": "t/**", "command": 5}]}}, "perturb.runners[0].command", "a string or null"),
        ({"perturb": {"runners": [{"files": "t/**"}]}}, "perturb.runners[0].command", "a string or null"),
        ({"perturb": {"runners": [{"files": "t/**", "command": None}, {"files": "e/**", "command": []}]}}, "perturb.runners[1].command", "a string or null"),
        ({"perturb": {"max": "12"}}, "perturb.max", "a whole number"),
        ({"perturb": {"timeoutSeconds": True}}, "perturb.timeoutSeconds", "a whole number"),
        ({"citations": "**/*.md"}, "citations", "an object"),
        ({"citations": {"files": "**/*.md"}}, "citations.files", "a list of strings"),
        ({"switchedOn": 1}, "switchedOn", "an object"),
        ({"switchedOn": {"registry": 3}}, "switchedOn.registry", "a string"),
        ({"switchedOn": {"triggers": "drizzle/*.sql"}}, "switchedOn.triggers", "a list of strings"),
        ({"switchedOn": {"staleDays": "30"}}, "switchedOn.staleDays", "a whole number"),
    ]

    # Every key with the shape it must have, including the ones that look odd but are allowed: a runner
    # that declares its suite not runnable (`command: null`), a batch block (judged where it is used),
    # an empty list, a rule without a name, a count of zero.
    VALID = {
        "production": ["lib/**"],
        "tests": [],
        "ignore": ["lib/generated/**"],
        "sizeCap": {"files": 3, "lines": 12},
        "crossView": [{"name": "billing", "paths": ["a/**"], "tests": ["t/**"]}, {"paths": [], "tests": []}],
        "perturb": {
            "runners": [{"files": "t/**", "command": "run {file}", "batch": {"args": 3}}, {"files": "e/**", "command": None}],
            "max": 1,
            "timeoutSeconds": 9,
        },
        "citations": {"files": []},
        "switchedOn": {"registry": "registry.md", "triggers": [], "staleDays": 0},
        "timeBudget": {"report": "report.json", "format": "jest-json"},
        "fkIndex": {"migrations": ["migrations/*.sql"]},
    }

    def refusal(self, data):
        with self.assertRaises(core.GuardrailError) as raised:
            core.Config(data)
        return str(raised.exception)

    def test_each_wrong_type_is_refused_naming_the_key_and_the_type_it_expects(self):
        for data, key, expected in self.WRONG:
            with self.subTest(data=data):
                message = self.refusal(data)
                self.assertIn(f"{key} must be {expected}", message)
                self.assertIn(core.CONFIG_FILE, message)

    def test_every_key_of_the_right_type_is_accepted_and_kept_as_given(self):
        config = core.Config(self.VALID)
        for key, value in self.VALID.items():
            with self.subTest(key=key):
                self.assertEqual(config[key], value)

    def test_a_partial_object_keeps_the_defaults_of_the_keys_it_leaves_out(self):
        config = core.Config({"sizeCap": {"files": 3}, "perturb": {"max": 2}, "switchedOn": {"triggers": ["x/**"]}})
        self.assertEqual(config["sizeCap"], {"files": 3, "lines": core.DEFAULTS["sizeCap"]["lines"]})
        self.assertEqual(config["perturb"]["timeoutSeconds"], core.DEFAULTS["perturb"]["timeoutSeconds"])
        self.assertEqual(config["switchedOn"]["staleDays"], core.DEFAULTS["switchedOn"]["staleDays"])

    def test_no_configuration_and_an_empty_one_are_the_defaults(self):
        for data in (None, {}):
            with self.subTest(data=data):
                self.assertEqual(core.Config(data).data, core.DEFAULTS)

    def test_an_unknown_key_is_still_refused_by_name(self):
        self.assertIn("unknown key 'productoin'", self.refusal({"productoin": ["src/**"]}))


class ReportReader(unittest.TestCase):
    """`read_jest_report`: a report that is not of the shape it reads proves nothing (None)."""

    def read(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "report.json")
            if text is not None:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(text)
            return locks.read_jest_report(path)

    @staticmethod
    def report(**fields):
        test = {"title": "adds", "ancestorTitles": ["maths"], "status": "passed", "failureMessages": [], **fields}
        return json.dumps({"testResults": [{"name": "test/a.test.ts", "assertionResults": [test]}]})

    def test_a_report_of_its_shape_is_read(self):
        (outcome,) = self.read(self.report(status="failed", failureMessages=["boom"], duration=12.5))
        self.assertEqual((outcome.file, outcome.name, outcome.status, outcome.messages, outcome.duration), ("test/a.test.ts", "maths adds", "failed", ["boom"], 12.5))

    def test_what_is_not_a_report_of_that_shape_reads_as_nothing(self):
        cases = {
            "missing file": None,
            "not json": "{ not json",
            "no testResults": "{}",
            "a status that is not a string": self.report(status=3),
            "failure messages that are not a list": self.report(failureMessages="boom"),
            "a failure message that is not a string": self.report(status="failed", failureMessages=[3]),
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertIsNone(self.read(text))


class CodeRules(unittest.TestCase):
    """The two rules of CLAUDE.md that nothing else checks: Python 3.9 syntax, standard library only."""

    SOURCES = [os.path.join(HOME, "guardrail", name) for name in sorted(os.listdir(os.path.join(HOME, "guardrail"))) if name.endswith(".py")] + [os.path.join(HOME, "bin", "guardrail")]

    @staticmethod
    def parses_as_python_39(source):
        try:
            ast.parse(source, feature_version=(3, 9))
        except SyntaxError:
            return False
        return True

    @staticmethod
    def is_standard(module):
        """A module of the standard library: built in, or a file of its folder that is not an installed package."""
        if module in sys.builtin_module_names:
            return True
        spec = importlib.util.find_spec(module)
        if spec is None or not spec.origin or spec.origin in ("built-in", "frozen"):
            return spec is not None
        origin = os.path.realpath(spec.origin)
        in_stdlib = origin.startswith(os.path.realpath(sysconfig.get_paths()["stdlib"]) + os.sep)
        return in_stdlib and not {"site-packages", "dist-packages"} & set(origin.split(os.sep))

    @classmethod
    def foreign_imports(cls, source):
        """Top-level names the source imports that are neither the standard library nor this package."""
        found = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                found.add(node.module.split(".")[0])
        return sorted(name for name in found if name != "guardrail" and not cls.is_standard(name))

    def read(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_the_checks_can_fail(self):
        # Newer syntax is refused; a library that is not the standard one is named; the standard one and our own package are not.
        self.assertFalse(self.parses_as_python_39("match 1:\n    case 1:\n        pass\n"))
        self.assertTrue(self.parses_as_python_39("x: list[int] = []\n"))
        self.assertEqual(self.foreign_imports("import json, os.path\nfrom guardrail import core\nfrom . import core\nfrom __future__ import annotations\n"), [])
        self.assertEqual(self.foreign_imports("def f():\n    import not_a_standard_module_at_all\n"), ["not_a_standard_module_at_all"])
        self.assertEqual(self.foreign_imports("from not_a_standard_module_at_all.sub import x\n"), ["not_a_standard_module_at_all"])

    def test_every_source_is_python_39_syntax(self):
        # Syntax only: `X | Y` evaluated at run time and a newer library function pass this check. The
        # scenarios run on 3.9 (see the README) are the evidence for those.
        self.assertIn(os.path.join(HOME, "guardrail", "locks.py"), self.SOURCES)
        for path in self.SOURCES:
            with self.subTest(path=os.path.relpath(path, HOME)):
                self.assertTrue(self.parses_as_python_39(self.read(path)))

    def test_every_source_imports_only_the_standard_library_and_this_package(self):
        self.assertIn(os.path.join(HOME, "guardrail", "locks.py"), self.SOURCES)
        for path in self.SOURCES:
            with self.subTest(path=os.path.relpath(path, HOME)):
                self.assertEqual(self.foreign_imports(self.read(path)), [])


if __name__ == "__main__":
    unittest.main()
