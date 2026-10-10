"""Shared primitives: configuration, path classification and git access.

Every lock uses these, so "what is production code" and "what is staged" have one
definition. Standard library only: a hook must run wherever pre-commit runs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

CONFIG_FILE = ".guardrail.json"

# Defaults for a TypeScript project. A project overrides any key in .guardrail.json.
DEFAULTS = {
    "production": ["src/**"],
    "tests": ["**/*.test.ts", "**/*.test.tsx", "**/*.spec.ts", "**/*.spec.tsx", "test/**", "e2e/**"],
    "ignore": [],
    "sizeCap": {"files": 8, "lines": 400},
    # Each rule: {"name", "paths": [globs], "tests": [globs]}. Touching `paths`
    # requires a staged test under `tests` (a test that compares the figure across views).
    "crossView": [],
    # Each runner: {"files": glob, "command": "... {file} ..."}. First match wins.
    "perturb": {"runners": [], "max": 12, "timeoutSeconds": 180},
    "citations": {"files": ["**/*.md"]},
    "switchedOn": {"registry": ".agents/SWITCHED_ON.md", "triggers": [], "staleDays": 30},
}


class GuardrailError(Exception):
    """A lock cannot do its job (bad configuration, git failure). Never a pass."""


def glob_to_regex(pattern: str) -> re.Pattern:
    """Translate a path glob: `**` crosses directories, `*` and `?` do not."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(path: str, patterns) -> bool:
    """True when the path matches at least one glob."""
    return any(glob_to_regex(p).match(path) for p in patterns)


class Config:
    """The project's guardrail settings, with defaults for whatever it leaves out."""

    def __init__(self, data: dict | None = None):
        merged = json.loads(json.dumps(DEFAULTS))
        for key, value in (data or {}).items():
            if key not in DEFAULTS:
                raise GuardrailError(f"{CONFIG_FILE}: unknown key '{key}'")
            if isinstance(merged[key], dict):
                merged[key].update(value)
            else:
                merged[key] = value
        self.data = merged

    @classmethod
    def load(cls, root: str) -> "Config":
        """Read .guardrail.json. A missing file means defaults; a broken one is an error."""
        path = os.path.join(root, CONFIG_FILE)
        if not os.path.exists(path):
            return cls()
        try:
            with open(path, encoding="utf-8") as handle:
                return cls(json.load(handle))
        except (OSError, ValueError) as error:
            raise GuardrailError(f"{CONFIG_FILE} cannot be read: {error}") from error

    def __getitem__(self, key: str):
        return self.data[key]

    def is_test(self, path: str) -> bool:
        return matches(path, self["tests"]) and not matches(path, self["ignore"])

    def is_production(self, path: str) -> bool:
        """Production code: under a production glob, and neither a test nor ignored."""
        if matches(path, self["ignore"]) or self.is_test(path):
            return False
        return matches(path, self["production"])


def git(root: str, *args: str, check: bool = True) -> str:
    """Run git and return its output. A failure is an error, never an empty answer."""
    # Staged content can be binary (a screenshot next to a browser test): undecodable
    # bytes become a replacement character instead of stopping the lock.
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if check and result.returncode != 0:
        raise GuardrailError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def repo_root(cwd: str | None = None) -> str:
    return git(cwd or os.getcwd(), "rev-parse", "--show-toplevel").strip()


def is_merge(root: str) -> bool:
    """A merge commit carries other people's reviewed work: content locks skip it."""
    git_dir = git(root, "rev-parse", "--git-dir").strip()
    return os.path.exists(os.path.join(root, git_dir, "MERGE_HEAD"))


def staged_files(root: str) -> list[str]:
    """Added, copied, modified or renamed paths in the index. Deletions are excluded."""
    out = git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return [p for p in out.split("\0") if p]


def staged_text(root: str, path: str) -> str:
    """The content that will be committed, not the working copy."""
    return git(root, "show", f":{path}")


def staged_changed_lines(root: str, paths) -> int:
    """Lines added plus lines removed in the index, over the given paths."""
    total = 0
    for line in git(root, "diff", "--cached", "--numstat", "--", *paths).splitlines():
        added, removed, _ = line.split("\t", 2)
        if added != "-":  # binary files report "-"
            total += int(added) + int(removed)
    return total


def staged_added_lines(root: str, path: str) -> set[int]:
    """Line numbers (in the staged file) that this commit adds or changes."""
    numbers: set[int] = set()
    diff = git(root, "diff", "--cached", "-U0", "--", path)
    for match in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", diff, re.MULTILINE):
        start, count = int(match.group(1)), int(match.group(2) or "1")
        numbers.update(range(start, start + count))
    return numbers


def hatch(message: str, key: str, min_reason: int = 10) -> bool:
    """True when the commit message has a line `key: <reason>` with a real reason.

    The line must START with the key (exact line prefix, not a mention inside a
    sentence) and the reason must be long enough to say something.
    """
    for line in message.splitlines():
        if line.startswith(key + ":") and len(line[len(key) + 1 :].strip()) >= min_reason:
            return True
    return False
