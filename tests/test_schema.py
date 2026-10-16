"""Direct tests of the reader of migration SQL (guardrail/schema.py): SQL text in, the foreign keys
and the indexes it leaves behind out. No repository, no git, no command line.

The scenarios of the foreign key index lock (test_scenarios.py, class FkIndex) prove it through
git and the command line, one throwaway repository per case: about half a second each. HOW a
statement is read (an inline REFERENCES, a partial or an expression index, quotes, schema, case,
comments, function bodies, drop and rename) needs no repository, so it is proven here, one line
per case, in milliseconds. The scenarios keep what needs git and the command line: the lock
bites, stays quiet, judges only the staged migrations, is off when not configured, the hatch in
all its forms, the configuration errors and the two known limits.

Every expected value below is worked out by hand from the SQL on its line. GUARDRAIL_HOME points
these at another copy of the code, as it does for the scenarios: test_mutants.py breaks the
reader in a copy and requires something here or in the scenarios to turn red.
"""

import os
import sys
import unittest

HOME = os.environ.get("GUARDRAIL_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HOME)  # the code under test is the copy HOME names, never one found by accident

from guardrail import schema  # noqa: E402

BREAK = "--> statement-breakpoint\n"
ORG = frozenset({"organization_id"})  # the column every row of a multi-tenant table is filtered by
NONE = frozenset()

INVOICES = 'CREATE TABLE "invoices" (\n  "organization_id" uuid NOT NULL,\n  "id" uuid PRIMARY KEY NOT NULL,\n  "client_id" uuid NOT NULL,\n  "number" text\n);\n' + BREAK
FK = 'ALTER TABLE "invoices" ADD CONSTRAINT "invoices_client_fk" FOREIGN KEY ("organization_id","client_id") REFERENCES "public"."clients"("organization_id","id") ON DELETE no action ON UPDATE no action;\n' + BREAK
INDEX = 'CREATE INDEX "invoices_client_idx" ON "invoices" USING btree ("client_id");\n' + BREAK
PRIMARY_KEY = (None, ("id",), False)  # what the inline `"id" uuid PRIMARY KEY` of INVOICES leaves
INVOICES_KEY = ("invoices", ("organization_id", "client_id"), "clients", "invoices_client_fk")  # what FK leaves


def replay(*migrations):
    """The schema after applying each migration, in order, as the lock replays the repository's files."""
    state = schema.Schema()
    for number, sql in enumerate(migrations):
        state.apply(sql, f"drizzle/{number:04}.sql")
    return state


def keys(state):
    """{table: [(table, columns, target, constraint)]} of the tables that have a foreign key."""
    return {name: [(k.table, k.columns, k.target, k.constraint) for k in table.foreign_keys] for name, table in state.tables.items() if table.foreign_keys}


def indexes(state):
    """{table: [(name, columns, partial)]} of the tables that have an index or a key."""
    return {name: [tuple(i) for i in table.indexes] for name, table in state.tables.items() if table.indexes}


def by_table(keys_list):
    """A list of foreign keys grouped by its table, as `keys` does."""
    grouped = {}
    for key in keys_list:
        grouped.setdefault(key[0], []).append(key)
    return grouped


def index_on(columns):
    """An index of `invoices` on `columns`, written as they appear between the parentheses."""
    return f'CREATE INDEX "i" ON "invoices" ({columns});\n'


def lacking(state, files, ignored):
    """[(table, columns)] of the foreign keys `files` added that no index serves."""
    return [(k.table, k.columns) for k in schema.unindexed(state, files, ignored)]


class Reading(unittest.TestCase):
    """One migration in; the foreign keys and indexes it declares out. (label, sql, foreign keys, indexes)."""

    CASES = [
        (
            "an inline REFERENCES on a column",
            'CREATE TABLE "notes" (\n  "id" uuid PRIMARY KEY,\n  "client_id" uuid REFERENCES "clients"("id") ON DELETE CASCADE\n);\n',
            [("notes", ("client_id",), "clients", None)],
            {"notes": [PRIMARY_KEY]},
        ),
        (
            "a column added with REFERENCES beside one added without",
            'ALTER TABLE "invoices" ADD COLUMN "note" text, ADD COLUMN "client_id" uuid REFERENCES "clients"("id");\n',
            [("invoices", ("client_id",), "clients", None)],
            {},
        ),
        (
            "a named table-level FOREIGN KEY inside CREATE TABLE",
            'CREATE TABLE "notes" (\n  "id" uuid PRIMARY KEY,\n  "client_id" uuid,\n  CONSTRAINT "n_fk" FOREIGN KEY ("client_id") REFERENCES "clients"("id")\n);\n',
            [("notes", ("client_id",), "clients", "n_fk")],
            {"notes": [PRIMARY_KEY]},
        ),
        (
            "a FOREIGN KEY the migration left unnamed",
            'CREATE TABLE "notes" ("client_id" uuid, FOREIGN KEY ("client_id") REFERENCES "clients"("id"));\n',
            [("notes", ("client_id",), "clients", None)],
            {},
        ),
        (
            "ADD CONSTRAINT with a schema-qualified target, as Drizzle writes it",
            FK,
            [INVOICES_KEY],
            {},
        ),
        (
            "names whatever their quotes, schema or case",
            'create table INVOICES (organization_id uuid, client_id uuid);\n'
            'ALTER TABLE ONLY "public"."Invoices" ADD CONSTRAINT fk FOREIGN KEY (ORGANIZATION_ID, "client_id") REFERENCES public.CLIENTS (organization_id, id);\n',
            [("invoices", ("organization_id", "client_id"), "clients", "fk")],
            {},
        ),
        (
            "a doubled quote inside a quoted name and inside a string is a quote, not the end of it",
            'CREATE TABLE "we""ird" ("a" uuid REFERENCES "c"("id"), "n" text DEFAULT \'it\'\'s; -- not a comment\');\n',
            [('we"ird', ("a",), "c", None)],
            {},
        ),
        (
            "a string that is never closed ends the text, and what came before it is still read",
            "CREATE TABLE t (a uuid REFERENCES c(id), b text DEFAULT 'oops",
            [("t", ("a",), "c", None)],
            {},
        ),
        (
            "a parenthesis that is never closed ends the text, and what came before it is still read",
            "CREATE TABLE t (a uuid REFERENCES c(id)",
            [("t", ("a",), "c", None)],
            {},
        ),
        (
            "an element that has no name is skipped and the next one is read",
            "CREATE TABLE t (a uuid, (b), 5, c uuid REFERENCES d(id))",
            [("t", ("c",), "d", None)],
            {},
        ),
        (
            "a comment inside a statement hides what it contains",
            'CREATE TABLE "t" (\n  "id" uuid PRIMARY KEY,\n  "b" uuid /* REFERENCES c(d) */,\n  "n" uuid -- REFERENCES c(d)\n);\n',
            [],
            {"t": [PRIMARY_KEY]},
        ),
        (
            "a key in a comment or in a string is not a key",
            '-- ALTER TABLE a ADD CONSTRAINT x FOREIGN KEY (b) REFERENCES c(d);\n/* FOREIGN KEY (b) REFERENCES c(d) */\n'
            'CREATE TABLE "t" ("id" uuid PRIMARY KEY, "note" text DEFAULT \'FOREIGN KEY (b) REFERENCES c(d); x\');\n',
            [],
            {"t": [PRIMARY_KEY]},
        ),
        (
            "a keyword in a default value is not a PRIMARY KEY, a UNIQUE or a REFERENCES",
            "CREATE TABLE t (n text DEFAULT 'UNIQUE PRIMARY KEY REFERENCES c(d)');\n",
            [],
            {},
        ),
        (
            "a statement inside a function body is not executed by the migration",
            'CREATE FUNCTION f() RETURNS void AS $body$\nBEGIN\n  ALTER TABLE "invoices" ADD COLUMN "z" uuid REFERENCES "clients"("id");\nEND $body$ LANGUAGE plpgsql;\n',
            [],
            {},
        ),
        (
            "a function body with several statements is still one quoted body",
            'CREATE FUNCTION f() RETURNS void AS $body$\nBEGIN\n  PERFORM 1;\n'
            '  ALTER TABLE "invoices" ADD COLUMN "z" uuid REFERENCES "clients"("id");\nEND $body$ LANGUAGE plpgsql;\n',
            [],
            {},
        ),
        (
            "an inline UNIQUE and REFERENCES on one column are an index and a key",
            'CREATE TABLE "profiles" (\n  "id" uuid PRIMARY KEY,\n  "client_id" uuid UNIQUE REFERENCES "clients"("id")\n);\n',
            [("profiles", ("client_id",), "clients", None)],
            {"profiles": [PRIMARY_KEY, (None, ("client_id",), False)]},
        ),
        (
            "table-level PRIMARY KEY and UNIQUE: named, unnamed, NULLS NOT DISTINCT",
            'CREATE TABLE "lines" ("a" uuid, "b" integer, "c" integer, CONSTRAINT "lines_pk" PRIMARY KEY("a","b"), UNIQUE ("c"), UNIQUE NULLS NOT DISTINCT ("b","c"));\n',
            [],
            {"lines": [("lines_pk", ("a", "b"), False), (None, ("c",), False), (None, ("b", "c"), False)]},
        ),
        (
            "a UNIQUE constraint added to a table",
            'ALTER TABLE "invoices" ADD CONSTRAINT "by_client" UNIQUE ("client_id");\n',
            [],
            {"invoices": [("by_client", ("client_id",), False)]},
        ),
        (
            "an index on two columns, named",
            'CREATE INDEX "i" ON "t" ("a","b");\n',
            [],
            {"t": [("i", ("a", "b"), False)]},
        ),
        (
            "a unique index",
            'CREATE UNIQUE INDEX "i" ON "t" ("a");\n',
            [],
            {"t": [("i", ("a",), False)]},
        ),
        (
            "every option of CREATE INDEX: concurrently, if not exists, only, schema, method, order, INCLUDE",
            'CREATE INDEX CONCURRENTLY IF NOT EXISTS "i" ON ONLY "public"."t" USING btree ("a" DESC NULLS LAST) INCLUDE ("b");\n',
            [],
            {"t": [("i", ("a",), False)]},
        ),
        (
            "an index with no name and an operator class",
            "CREATE INDEX ON t (a text_pattern_ops);\n",
            [],
            {"t": [(None, ("a",), False)]},
        ),
        (
            "a partial index",
            'CREATE INDEX "i" ON "t" ("a") WHERE "b" IS NOT NULL;\n',
            [],
            {"t": [("i", ("a",), True)]},
        ),
        (
            "an expression in an index is a key that is no column",
            'CREATE INDEX "i" ON "t" ("a", lower("b"::text));\n',
            [],
            {"t": [("i", ("a", None), False)]},
        ),
        (
            "an expression index is not an index on the function it calls, even when a column is named like it",
            'CREATE INDEX "notes_lower_idx" ON "notes" (lower("id"::text));\n',
            [],
            {"notes": [("notes_lower_idx", (None,), False)]},
        ),
        (
            "a separator inside a quoted name does not split the statement",
            'CREATE INDEX "t_a;idx" ON "t" USING btree ("a");\n',
            [],
            {"t": [("t_a;idx", ("a",), False)]},
        ),
    ]

    def test_each_form_leaves_the_keys_and_indexes_it_declares(self):
        for label, sql, expected_keys, expected_indexes in self.CASES:
            with self.subTest(label):
                state = replay(sql)
                self.assertEqual(keys(state), by_table(expected_keys))
                self.assertEqual(indexes(state), expected_indexes)

    def test_a_key_remembers_the_file_that_added_it(self):
        state = replay(INVOICES, FK)
        self.assertEqual([k.file for t in state.tables.values() for k in t.foreign_keys], ["drizzle/0001.sql"])


class Replaying(unittest.TestCase):
    """Migrations in order; what is left after a later one drops, renames or makes again. (label, migrations, keys, indexes)."""

    PK_ONLY = {"invoices": [PRIMARY_KEY]}
    CASES = [
        ("an index a later migration dropped", [INVOICES + INDEX, 'DROP INDEX "invoices_client_idx";\n'], [], PK_ONLY),
        ("DROP INDEX with IF EXISTS, several names and CASCADE", [INVOICES + INDEX, 'DROP INDEX IF EXISTS "invoices_client_idx", "other" CASCADE;\n'], [], PK_ONLY),
        (
            "an index dropped and made again",
            [INVOICES + INDEX, 'DROP INDEX "invoices_client_idx";\n', INDEX],
            [],
            {"invoices": [PRIMARY_KEY, ("invoices_client_idx", ("client_id",), False)]},
        ),
        (
            "an index renamed keeps serving under its new name",
            [INVOICES + INDEX, 'ALTER INDEX "invoices_client_idx" RENAME TO "renamed";\n'],
            [],
            {"invoices": [PRIMARY_KEY, ("renamed", ("client_id",), False)]},
        ),
        ("an index renamed and then dropped by its new name", [INVOICES + INDEX, 'ALTER INDEX "invoices_client_idx" RENAME TO "renamed";\n', 'DROP INDEX "renamed";\n'], [], PK_ONLY),
        ("a dropped constraint takes its foreign key", [INVOICES + FK, 'ALTER TABLE "invoices" DROP CONSTRAINT "invoices_client_fk";\n'], [], PK_ONLY),
        (
            "a dropped constraint takes its index and leaves the foreign key it is not",
            [INVOICES + FK + 'ALTER TABLE "invoices" ADD CONSTRAINT "by_client" UNIQUE ("client_id");\n', 'ALTER TABLE "invoices" DROP CONSTRAINT IF EXISTS "by_client";\n'],
            [INVOICES_KEY],
            PK_ONLY,
        ),
        (
            "a dropped column takes the keys and the indexes that name it",
            [INVOICES + FK + INDEX, 'ALTER TABLE "invoices" DROP COLUMN "client_id";\n'],
            [],
            PK_ONLY,
        ),
        (
            "a column dropped and added again is not indexed",
            [INVOICES + INDEX, 'ALTER TABLE "invoices" DROP COLUMN IF EXISTS "client_id";\n--> statement-breakpoint\nALTER TABLE "invoices" ADD COLUMN "client_id" uuid;\n'],
            [],
            PK_ONLY,
        ),
        ("a table dropped takes everything with it", [INVOICES + INDEX + FK, 'DROP TABLE IF EXISTS "invoices" CASCADE;\n'], [], {}),
        ("a table made again starts empty", [INVOICES + INDEX + FK, INVOICES], [], PK_ONLY),
    ]

    def test_each_later_migration_leaves_what_it_should(self):
        for label, migrations, expected_keys, expected_indexes in self.CASES:
            with self.subTest(label):
                state = replay(*migrations)
                self.assertEqual(keys(state), by_table(expected_keys))
                self.assertEqual(indexes(state), expected_indexes)


class Covering(unittest.TestCase):
    """`unindexed`: which foreign keys have no index that leads with their columns.
    (label, migrations, columns ignored, the (table, columns) that lack an index)."""

    LINES = (
        'CREATE TABLE "invoice_lines" (\n  "organization_id" uuid NOT NULL,\n  "invoice_id" uuid NOT NULL,\n  "line_no" integer NOT NULL,\n'
        '  CONSTRAINT "lines_pk" PRIMARY KEY("invoice_id","line_no")\n);\n'
        'ALTER TABLE "invoice_lines" ADD CONSTRAINT "lines_fk" FOREIGN KEY ("organization_id","invoice_id") REFERENCES "invoices"("organization_id","id");\n'
    )
    NOTES = 'CREATE TABLE "notes" (\n  "id" uuid PRIMARY KEY,\n  "organization_id" uuid REFERENCES "organizations"("id")\n);\n'
    CLIENT = ("invoices", ("organization_id", "client_id"))

    CASES = [
        ("no index at all", [INVOICES + FK], ORG, [CLIENT]),
        ("an index that leads with another column", [INVOICES + FK + 'CREATE INDEX "i" ON "invoices" ("number","client_id");\n'], ORG, [CLIENT]),
        ("a partial index", [INVOICES + FK + 'CREATE INDEX "i" ON "invoices" ("client_id") WHERE "number" IS NOT NULL;\n'], ORG, [CLIENT]),
        ("an expression index", [INVOICES + FK + 'CREATE INDEX "i" ON "invoices" (lower("client_id"::text));\n'], ORG, [CLIENT]),
        ("a key on the ignored column alone, with no index", [NOTES], ORG, [("notes", ("organization_id",))]),
        ("a key on the ignored column alone, with its index", [NOTES + 'CREATE INDEX "notes_org_idx" ON "notes" ("organization_id");\n'], ORG, []),
        ("nothing ignored and an index on the second column only", [INVOICES + FK + INDEX], NONE, [CLIENT]),
        ("nothing ignored and an index on both columns", [INVOICES + FK + index_on('"organization_id","client_id"')], NONE, []),
        ("nothing ignored and an index on both columns in the other order", [INVOICES + FK + index_on('"client_id","organization_id"')], NONE, []),
        ("an index on the other column of a composite key", [INVOICES + FK + INDEX], ORG, []),
        ("an index on both columns of a composite key", [INVOICES + FK + index_on('"organization_id","client_id"')], ORG, []),
        ("an index on both columns, ignored one last", [INVOICES + FK + index_on('"client_id","organization_id"')], ORG, []),
        ("the index before the key, in the same migration", [INVOICES + INDEX + FK], ORG, []),
        ("the index after the key, in the same migration", [INVOICES + FK + INDEX], ORG, []),
        ("the index in an earlier migration", [INVOICES + INDEX, FK], ORG, []),
        ("a table-level PRIMARY KEY", [LINES], ORG, []),
        ("a table-level UNIQUE", [LINES.replace('PRIMARY KEY("invoice_id","line_no")', 'UNIQUE("invoice_id","line_no")')], ORG, []),
        (
            "an inline UNIQUE",
            ['CREATE TABLE "profiles" (\n  "id" uuid PRIMARY KEY,\n  "client_id" uuid UNIQUE REFERENCES "clients"("id")\n);\n'],
            ORG,
            [],
        ),
        ("an inline PRIMARY KEY (one to one)", ['CREATE TABLE "profiles" (\n  "client_id" uuid PRIMARY KEY REFERENCES "clients"("id")\n);\n'], ORG, []),
        ("a unique index", [INVOICES + FK + 'CREATE UNIQUE INDEX "i" ON "invoices" ("client_id");\n'], ORG, []),
        (
            "an index with every option of CREATE INDEX",
            [INVOICES + FK + 'CREATE INDEX CONCURRENTLY IF NOT EXISTS "i" ON ONLY "public"."invoices" USING btree ("client_id" DESC NULLS LAST) INCLUDE ("number");\n'],
            ORG,
            [],
        ),
        ("an index with no name", [INVOICES + FK + "CREATE INDEX ON invoices (client_id text_pattern_ops);\n"], ORG, []),
        ("an index whose name holds a separator", [INVOICES + FK + 'CREATE INDEX "invoices_client;idx" ON "invoices" USING btree ("client_id");\n'], ORG, []),
        (
            "an expression index does not serve a key on a column named like its function",
            ['CREATE TABLE "notes" (\n  "id" uuid PRIMARY KEY,\n  "lower" uuid REFERENCES "clients"("id")\n);\nCREATE INDEX "notes_lower_idx" ON "notes" (lower("id"::text));\n'],
            ORG,
            [("notes", ("lower",))],
        ),
        ("an index a later migration dropped", [INVOICES + INDEX, 'DROP INDEX "invoices_client_idx";\n', FK], ORG, [CLIENT]),
        ("an index dropped and made again", [INVOICES + INDEX, 'DROP INDEX IF EXISTS "invoices_client_idx", "other";\n', FK + INDEX], ORG, []),
        ("a key a later migration dropped", [INVOICES + FK, 'ALTER TABLE "invoices" DROP CONSTRAINT "invoices_client_fk";\n'], ORG, []),
        ("a column a later migration dropped", [INVOICES + FK, 'ALTER TABLE "invoices" DROP COLUMN "client_id";\n'], ORG, []),
        (
            "an index lost with its column, which came back unindexed",
            [INVOICES + INDEX, 'ALTER TABLE "invoices" DROP COLUMN "client_id";\n--> statement-breakpoint\nALTER TABLE "invoices" ADD COLUMN "client_id" uuid;\n', FK],
            ORG,
            [CLIENT],
        ),
        ("an index lost with its table", [INVOICES + INDEX, 'DROP TABLE IF EXISTS "invoices" CASCADE;\n', INVOICES + FK], ORG, [CLIENT]),
        (
            "an index that is a constraint, lost with the constraint",
            [INVOICES + 'ALTER TABLE "invoices" ADD CONSTRAINT "by_client" UNIQUE ("client_id");\n', 'ALTER TABLE "invoices" DROP CONSTRAINT "by_client";\n', FK],
            ORG,
            [CLIENT],
        ),
        (
            "an index renamed and then dropped by the new name",
            [INVOICES + INDEX, 'ALTER INDEX "invoices_client_idx" RENAME TO "renamed";\n', 'DROP INDEX "renamed";\n', FK],
            ORG,
            [CLIENT],
        ),
    ]

    def test_each_form_is_served_or_not(self):
        for label, migrations, ignored, expected in self.CASES:
            with self.subTest(label):
                state = replay(*migrations)
                files = {f"drizzle/{n:04}.sql" for n in range(len(migrations))}
                self.assertEqual(lacking(state, files, ignored), expected)

    def test_only_the_keys_the_judged_files_added_are_reported(self):
        state = replay(INVOICES + FK, 'CREATE TABLE "other" ("id" uuid PRIMARY KEY);\n')
        self.assertEqual(lacking(state, {"drizzle/0001.sql"}, ORG), [], "an old key is not judged by a commit that did not add it")
        self.assertEqual(lacking(state, {"drizzle/0000.sql"}, ORG), [self.CLIENT])
        self.assertEqual(lacking(state, set(), ORG), [])

    def test_the_columns_an_index_has_to_lead_with_leave_out_the_ignored_ones(self):
        key = schema.ForeignKey("invoices", ("organization_id", "client_id"), "clients", None, "f.sql")
        alone = schema.ForeignKey("notes", ("organization_id",), "organizations", None, "f.sql")
        self.assertEqual(schema.required_columns(key, ORG), ("client_id",))
        self.assertEqual(schema.required_columns(key, NONE), ("organization_id", "client_id"))
        self.assertEqual(schema.required_columns(alone, ORG), ("organization_id",), "a key on the ignored column alone needs an index on it")


if __name__ == "__main__":
    unittest.main()
