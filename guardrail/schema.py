"""A small reader of the SQL that migrations are made of: which foreign keys and indexes exist.

Not a SQL parser. It reads the statements migration tools write (Drizzle's among them) with
regular expressions over text whose comments and quoted parts have been dealt with, and it
keeps just enough state to answer one question: after these migrations, which foreign keys has
each table, and which indexes could serve them. What it does not read is listed in the README
("Honest limits") and is never guessed: a form it does not recognise is a form it does not see.
"""

from __future__ import annotations

import re
from typing import NamedTuple

IDENT = r'(?:"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*)'
_QNAME = rf"{IDENT}(?:\s*\.\s*{IDENT})*"
_FLAGS = re.IGNORECASE | re.DOTALL
_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


class ForeignKey(NamedTuple):
    """A foreign key a migration added: the table and columns it constrains, what it points at,
    the name of its constraint (None when the migration left it to the database) and the file."""

    table: str
    columns: tuple
    target: str
    constraint: str | None
    file: str


class Index(NamedTuple):
    """Something a lookup can use: an index, a primary key or a unique constraint. `columns` are
    its keys in order, None for a key that is an expression. A partial index serves only some rows."""

    name: str | None
    columns: tuple
    partial: bool


class Table:
    """What is known of one table: its foreign keys and its indexes, in the order they appeared."""

    def __init__(self):
        self.foreign_keys: list[ForeignKey] = []
        self.indexes: list[Index] = []


def name_of(qualified: str) -> str:
    """The last part of a possibly schema-qualified name, unquoted and lower-case."""
    last = re.findall(IDENT, qualified)[-1]
    return (last[1:-1].replace('""', '"') if last.startswith('"') else last).lower()


# --- text handling: comments, quotes, parentheses --------------------------


def _skip(text: str, i: int) -> int:
    """If a quoted part (string, quoted identifier, dollar-quoted body) starts at `i`, the index
    just after it; otherwise `i`."""
    quote = text[i]
    if quote in "'\"":
        j = i + 1
        while j < len(text):
            if text[j] == quote:
                if text[j + 1 : j + 2] == quote:  # a doubled quote is a quote
                    j += 2
                    continue
                return j + 1
            j += 1
        return len(text)
    if quote == "$":
        opening = _DOLLAR.match(text, i)
        if opening:
            end = text.find(opening.group(0), opening.end())
            return len(text) if end < 0 else end + len(opening.group(0))
    return i


def strip_comments(text: str) -> str:
    """The text without `-- ...` and `/* ... */` comments, outside quoted parts."""
    out, i = [], 0
    while i < len(text):
        skipped = _skip(text, i)
        if skipped != i:
            out.append(text[i:skipped])
            i = skipped
        elif text.startswith("--", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            out.append(" ")
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def split_top(text: str, separator: str) -> list[str]:
    """Split at a separator that is outside quoted parts and outside parentheses."""
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        skipped = _skip(text, i)
        if skipped != i:
            i = skipped
            continue
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        elif text[i] == separator and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def _group(text: str, start: int) -> tuple[str, int]:
    """The text inside the parentheses that open at `start`, and the index after the closing one."""
    depth, i = 0, start
    while i < len(text):
        skipped = _skip(text, i)
        if skipped != i:
            i = skipped
            continue
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i], i + 1
        i += 1
    return text[start + 1 :], len(text)


def _blank_strings(text: str) -> str:
    """The text with string literals emptied, so a keyword inside a default value is not read as one."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "'":
            i = _skip(text, i)
            out.append("''")
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _columns(listing: str) -> tuple:
    """The column names of `a, "b"` (a list inside parentheses)."""
    return tuple(name_of(part) for part in split_top(listing, ",") if part.strip())


# --- statements -------------------------------------------------------------

_CREATE_TABLE = re.compile(rf"^CREATE\s+(?:(?:GLOBAL|LOCAL)\s+)?(?:(?:TEMP|TEMPORARY|UNLOGGED)\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({_QNAME})\s*\(", _FLAGS)
_ALTER_TABLE = re.compile(rf"^ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?({_QNAME})\s+(.*)$", _FLAGS)
_CREATE_INDEX = re.compile(rf"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(?:({IDENT})\s+)?ON\s+(?:ONLY\s+)?({_QNAME})\s*(?:USING\s+{IDENT}\s*)?\(", _FLAGS)
_DROP_INDEX = re.compile(r"^DROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?(.*?)(?:\s+(?:CASCADE|RESTRICT))?\s*$", _FLAGS)
_RENAME_INDEX = re.compile(rf"^ALTER\s+INDEX\s+(?:IF\s+EXISTS\s+)?({_QNAME})\s+RENAME\s+TO\s+({IDENT})\s*$", _FLAGS)
_DROP_TABLE = re.compile(r"^DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(.*?)(?:\s+(?:CASCADE|RESTRICT))?\s*$", _FLAGS)
_FOREIGN_KEY = re.compile(rf"^(?:CONSTRAINT\s+({IDENT})\s+)?FOREIGN\s+KEY\s*\(", _FLAGS)
_KEY_CONSTRAINT = re.compile(rf"^(?:CONSTRAINT\s+({IDENT})\s+)?(?:PRIMARY\s+KEY|UNIQUE)\s*(?:NULLS\s+(?:NOT\s+)?DISTINCT\s*)?\(", _FLAGS)
_REFERENCES = re.compile(rf"\bREFERENCES\s+({_QNAME})", _FLAGS)


def _constraint(table: Table, name: str, element: str, file: str) -> bool:
    """Read a table constraint (`FOREIGN KEY (...) REFERENCES t`, `PRIMARY KEY (...)`,
    `UNIQUE (...)`, with an optional `CONSTRAINT name`). True when the element was one."""
    foreign = _FOREIGN_KEY.match(element)
    if foreign:
        listing, after = _group(element, foreign.end() - 1)
        target = _REFERENCES.search(element, after)
        if target:
            constraint = name_of(foreign.group(1)) if foreign.group(1) else None
            table.foreign_keys.append(ForeignKey(name, _columns(listing), name_of(target.group(1)), constraint, file))
        return True
    key = _KEY_CONSTRAINT.match(element)
    if key:
        listing, _ = _group(element, key.end() - 1)
        table.indexes.append(Index(name_of(key.group(1)) if key.group(1) else None, _columns(listing), False))
        return True
    return False


def _column(table: Table, name: str, element: str, file: str) -> None:
    """Read a column definition: an inline REFERENCES is a foreign key on that column, an inline
    PRIMARY KEY or UNIQUE is an index on it."""
    head = re.match(IDENT, element.strip())
    if not head:
        return
    column = name_of(head.group(0))
    rest = _blank_strings(element.strip()[head.end() :])
    target = _REFERENCES.search(rest)
    if target:
        table.foreign_keys.append(ForeignKey(name, (column,), name_of(target.group(1)), None, file))
    if re.search(r"\b(?:PRIMARY\s+KEY|UNIQUE)\b", rest, _FLAGS):
        table.indexes.append(Index(None, (column,), False))


class Schema:
    """The tables a run of migrations leaves behind, as far as foreign keys and indexes go."""

    def __init__(self):
        self.tables: dict[str, Table] = {}

    def table(self, name: str) -> Table:
        return self.tables.setdefault(name, Table())

    def apply(self, sql: str, file: str) -> None:
        """Apply one migration file, statement by statement, in order."""
        for statement in split_top(strip_comments(sql), ";"):
            statement = statement.strip()
            if statement:
                self._statement(statement, file)

    def _statement(self, statement: str, file: str) -> None:
        created = _CREATE_TABLE.match(statement)
        if created:
            name = name_of(created.group(1))
            self.tables[name] = table = Table()  # a table made again starts empty
            body, _ = _group(statement, created.end() - 1)
            for element in split_top(body, ","):
                element = element.strip()
                if element and not _constraint(table, name, element, file):
                    _column(table, name, element, file)
            return
        altered = _ALTER_TABLE.match(statement)
        if altered:
            self._alter(name_of(altered.group(1)), altered.group(2), file)
            return
        index = _CREATE_INDEX.match(statement)
        if index:
            keys, after = _group(statement, index.end() - 1)
            columns = []
            for element in split_top(keys, ","):
                head = re.match(IDENT, element.strip())
                is_column = head and not element.strip()[head.end() :].lstrip().startswith("(")
                columns.append(name_of(head.group(0)) if is_column else None)
            partial = bool(re.search(r"\bWHERE\b", statement[after:], _FLAGS))
            self.table(name_of(index.group(2))).indexes.append(Index(name_of(index.group(1)) if index.group(1) else None, tuple(columns), partial))
            return
        dropped = _DROP_INDEX.match(statement)
        if dropped:
            for name in {name_of(part) for part in split_top(dropped.group(1), ",") if part.strip()}:
                for table in self.tables.values():
                    table.indexes = [i for i in table.indexes if i.name != name]
            return
        renamed = _RENAME_INDEX.match(statement)
        if renamed:
            old, new = name_of(renamed.group(1)), name_of(renamed.group(2))
            for table in self.tables.values():
                table.indexes = [i._replace(name=new) if i.name == old else i for i in table.indexes]
            return
        gone = _DROP_TABLE.match(statement)
        if gone:
            for part in split_top(gone.group(1), ","):
                if part.strip():
                    self.tables.pop(name_of(part), None)

    def _alter(self, name: str, actions: str, file: str) -> None:
        table = self.table(name)
        for action in split_top(actions, ","):
            action = action.strip()
            add = re.match(r"^ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(.*)$", action, _FLAGS)
            drop_constraint = re.match(rf"^DROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?({IDENT})", action, _FLAGS)
            drop_column = re.match(rf"^DROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?({IDENT})", action, _FLAGS)
            if drop_constraint:
                gone = name_of(drop_constraint.group(1))
                table.foreign_keys = [k for k in table.foreign_keys if k.constraint != gone]
                table.indexes = [i for i in table.indexes if i.name != gone]
            elif drop_column:
                gone = name_of(drop_column.group(1))
                table.foreign_keys = [k for k in table.foreign_keys if gone not in k.columns]
                table.indexes = [i for i in table.indexes if gone not in i.columns]
            elif add and not _constraint(table, name, add.group(1), file):
                _column(table, name, add.group(1), file)


def covers(index: Index, required: tuple, ignored: frozenset) -> bool:
    """Does this index serve a lookup on `required` columns? Its leading keys must be exactly
    those columns, in any order, optionally after leading keys that are in `ignored` (the column
    every row of a multi-tenant table is filtered by). A partial index serves only some rows."""
    if index.partial:
        return False
    keys = list(index.columns)
    candidates = [keys]
    while keys and keys[0] in ignored:
        keys = keys[1:]
        candidates.append(keys)
    return any(set(c[: len(required)]) == set(required) for c in candidates)


def required_columns(key: ForeignKey, ignored: frozenset) -> tuple:
    """The columns an index has to lead with to serve this key: its own, minus the ignored ones
    (unless that leaves none: a key on the ignored column alone needs an index on it)."""
    return tuple(c for c in key.columns if c not in ignored) or key.columns


def unindexed(schema: Schema, files: set, ignored: frozenset) -> list[ForeignKey]:
    """The foreign keys added by `files` that still exist and have no index that serves them."""
    return [
        key
        for table in schema.tables.values()
        for key in table.foreign_keys
        if key.file in files and not any(covers(index, required_columns(key, ignored), ignored) for index in table.indexes)
    ]
