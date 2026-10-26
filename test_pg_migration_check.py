import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pg_migration_check as mc


def lint(sql):
    return mc.check_migration(sql, "m.sql")


def rules(findings):
    return [f.rule for f in findings]


class SplitterTests(unittest.TestCase):
    def test_basic_split(self):
        stmts = mc.split_statements("SELECT 1; SELECT 2;")
        self.assertEqual(len(stmts), 2)

    def test_line_numbers(self):
        stmts = mc.split_statements("\n\nSELECT 1;\nSELECT 2;")
        self.assertEqual(stmts[0][0], 3)
        self.assertEqual(stmts[1][0], 4)

    def test_semicolon_in_string(self):
        stmts = mc.split_statements("INSERT INTO t VALUES ('a;b'); SELECT 1;")
        self.assertEqual(len(stmts), 2)
        self.assertIn("'a;b'", stmts[0][1])

    def test_escaped_quote_in_string(self):
        stmts = mc.split_statements("SELECT 'it''s'; SELECT 2;")
        self.assertEqual(len(stmts), 2)

    def test_line_comment_stripped(self):
        stmts = mc.split_statements("SELECT 1; -- comment; here\nSELECT 2;")
        self.assertEqual(len(stmts), 2)
        self.assertNotIn("comment", stmts[1][1])

    def test_block_comment_stripped(self):
        stmts = mc.split_statements("/* drop; everything */ SELECT 1;")
        self.assertEqual(len(stmts), 1)
        self.assertNotIn("drop", stmts[0][1])

    def test_block_comment_line_tracking(self):
        stmts = mc.split_statements("/*\n\n*/ SELECT 1;")
        self.assertEqual(stmts[0][0], 3)

    def test_dollar_quoted_body(self):
        sql = ("CREATE FUNCTION f() RETURNS void AS $$\n"
               "BEGIN\n RAISE NOTICE 'x;y';\nEND;\n$$ LANGUAGE plpgsql;\n"
               "SELECT 1;")
        stmts = mc.split_statements(sql)
        self.assertEqual(len(stmts), 2)
        self.assertIn("RAISE NOTICE", stmts[0][1])

    def test_dollar_quoted_tag(self):
        sql = "CREATE FUNCTION f() RETURNS void AS $body$ SELECT 1; $body$ LANGUAGE sql; SELECT 2;"
        stmts = mc.split_statements(sql)
        self.assertEqual(len(stmts), 2)

    def test_no_trailing_semicolon(self):
        self.assertEqual(len(mc.split_statements("SELECT 1")), 1)

    def test_empty(self):
        self.assertEqual(mc.split_statements("-- nothing\n"), [])


class IndexRuleTests(unittest.TestCase):
    def test_plain_index_flagged(self):
        f = lint("CREATE INDEX idx ON users(email);")
        self.assertEqual(f[0].rule, "create-index-no-concurrently")
        self.assertEqual(f[0].severity, "warning")

    def test_unique_index_flagged(self):
        self.assertIn("create-index-no-concurrently",
                      rules(lint("CREATE UNIQUE INDEX idx ON t(c);")))

    def test_concurrently_passes(self):
        self.assertEqual(lint("CREATE INDEX CONCURRENTLY idx ON t(c);"), [])

    def test_concurrently_in_transaction_is_error(self):
        f = lint("BEGIN;\nCREATE INDEX CONCURRENTLY idx ON t(c);\nCOMMIT;")
        self.assertEqual(f[0].rule, "concurrently-in-transaction")
        self.assertEqual(f[0].severity, "error")

    def test_start_transaction_variant(self):
        f = lint("START TRANSACTION;\nCREATE INDEX CONCURRENTLY i ON t(c);")
        self.assertEqual(rules(f), ["concurrently-in-transaction"])

    def test_index_after_commit_not_flagged(self):
        sql = "BEGIN;\nALTER TABLE t ADD COLUMN a int;\nCOMMIT;\nCREATE INDEX CONCURRENTLY i ON t(a);"
        self.assertNotIn("concurrently-in-transaction", rules(lint(sql)))

    def test_index_before_begin_not_flagged(self):
        sql = "CREATE INDEX CONCURRENTLY i ON t(c);\nBEGIN;\nSELECT 1;\nCOMMIT;"
        self.assertEqual(lint(sql), [])

    def test_drop_index_not_flagged(self):
        self.assertEqual(lint("DROP INDEX idx;"), [])


class AlterTableRuleTests(unittest.TestCase):
    def test_add_not_null_no_default_error(self):
        f = lint("ALTER TABLE users ADD COLUMN age int NOT NULL;")
        self.assertEqual(f[0].rule, "add-column-not-null-no-default")
        self.assertEqual(f[0].severity, "error")

    def test_add_not_null_with_default_passes(self):
        self.assertEqual(
            lint("ALTER TABLE users ADD COLUMN age int NOT NULL DEFAULT 0;"),
            [])

    def test_add_nullable_column_passes(self):
        self.assertEqual(lint("ALTER TABLE t ADD COLUMN nick text;"), [])

    def test_add_column_keyword_optional(self):
        f = lint("ALTER TABLE t ADD age int NOT NULL;")
        self.assertIn("add-column-not-null-no-default", rules(f))

    def test_volatile_default_random(self):
        f = lint("ALTER TABLE t ADD COLUMN r float DEFAULT random();")
        self.assertIn("add-column-volatile-default", rules(f))

    def test_volatile_default_gen_random_uuid(self):
        f = lint("ALTER TABLE t ADD COLUMN id uuid DEFAULT gen_random_uuid();")
        self.assertIn("add-column-volatile-default", rules(f))

    def test_volatile_default_clock_timestamp(self):
        f = lint("ALTER TABLE t ADD COLUMN c timestamptz DEFAULT clock_timestamp();")
        self.assertIn("add-column-volatile-default", rules(f))

    def test_volatile_default_nextval(self):
        f = lint("ALTER TABLE t ADD COLUMN id int DEFAULT nextval('s');")
        self.assertIn("add-column-volatile-default", rules(f))

    def test_stable_now_default_not_flagged(self):
        # now() is STABLE, not volatile: docs' fast metadata-only path.
        self.assertNotIn("add-column-volatile-default",
                         rules(lint("ALTER TABLE t ADD COLUMN c timestamptz DEFAULT now();")))

    def test_static_default_not_flagged(self):
        self.assertEqual(
            lint("ALTER TABLE t ADD COLUMN role text DEFAULT 'user';"), [])

    def test_alter_type_flagged(self):
        f = lint("ALTER TABLE t ALTER COLUMN age TYPE bigint;")
        self.assertEqual(f[0].rule, "alter-column-type")

    def test_alter_type_set_data_type_form(self):
        self.assertIn("alter-column-type",
                      rules(lint("ALTER TABLE t ALTER age SET DATA TYPE bigint;")))

    def test_set_not_null_flagged(self):
        f = lint("ALTER TABLE t ALTER COLUMN age SET NOT NULL;")
        self.assertEqual(f[0].rule, "set-not-null")

    def test_drop_not_null_not_flagged(self):
        self.assertEqual(
            lint("ALTER TABLE t ALTER COLUMN age DROP NOT NULL;"), [])

    def test_add_fk_without_not_valid(self):
        f = lint("ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY (uid) REFERENCES users(id);")
        self.assertEqual(f[0].rule, "add-constraint-without-not-valid")

    def test_add_fk_not_valid_passes(self):
        sql = ("ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY (uid) "
               "REFERENCES users(id) NOT VALID;")
        self.assertEqual(lint(sql), [])

    def test_add_check_without_not_valid(self):
        f = lint("ALTER TABLE t ADD CHECK (age > 0);")
        self.assertIn("add-constraint-without-not-valid", rules(f))

    def test_validate_constraint_passes(self):
        self.assertEqual(lint("ALTER TABLE t VALIDATE CONSTRAINT fk;"), [])

    def test_drop_table_flagged(self):
        f = lint("DROP TABLE users;")
        self.assertEqual(f[0].rule, "drop-table")

    def test_drop_table_if_exists_flagged(self):
        self.assertIn("drop-table", rules(lint("DROP TABLE IF EXISTS users;")))

    def test_drop_column_flagged(self):
        f = lint("ALTER TABLE users DROP COLUMN age;")
        self.assertEqual(f[0].rule, "drop-column")

    def test_drop_column_if_exists(self):
        self.assertIn("drop-column",
                      rules(lint("ALTER TABLE t DROP COLUMN IF EXISTS age;")))

    def test_drop_column_keyword_optional(self):
        self.assertIn("drop-column", rules(lint("ALTER TABLE t DROP age;")))

    def test_drop_constraint_not_flagged_as_column(self):
        self.assertNotIn("drop-column",
                         rules(lint("ALTER TABLE t DROP CONSTRAINT c1;")))

    def test_add_constraint_not_null_not_a_column(self):
        # PostgreSQL not-null constraint form: not an ADD COLUMN.
        self.assertNotIn("add-column-not-null-no-default",
                         rules(lint("ALTER TABLE t ADD CONSTRAINT nn NOT NULL (c);")))

    def test_finding_line_numbers(self):
        sql = "-- header\n\nCREATE INDEX i ON t(c);\nALTER TABLE t DROP COLUMN c;"
        by_rule = {f.rule: f for f in lint(sql)}
        self.assertEqual(by_rule["create-index-no-concurrently"].line, 3)
        self.assertEqual(by_rule["drop-column"].line, 4)

    def test_multiple_findings_one_file(self):
        sql = ("CREATE INDEX i ON t(c);\n"
               "ALTER TABLE t ADD COLUMN a int NOT NULL;\n"
               "DROP TABLE old;")
        self.assertEqual(sorted(rules(lint(sql))), [
            "add-column-not-null-no-default",
            "create-index-no-concurrently",
            "drop-table",
        ])

    def test_clean_realistic_migration(self):
        sql = (
            "BEGIN;\n"
            "ALTER TABLE orders ADD COLUMN note text;\n"
            "ALTER TABLE orders ADD COLUMN priority int NOT NULL DEFAULT 0;\n"
            "ALTER TABLE orders ADD CONSTRAINT fk_cust FOREIGN KEY (cid) "
            "REFERENCES customers(id) NOT VALID;\n"
            "COMMIT;\n"
            "CREATE INDEX CONCURRENTLY idx_orders_cid ON orders(cid);\n"
            "ALTER TABLE orders VALIDATE CONSTRAINT fk_cust;\n")
        self.assertEqual(lint(sql), [])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mc.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_clean_exit_0(self):
        path = self.write("m.sql", "ALTER TABLE t ADD COLUMN a int;\n")
        code, out, _ = self.run_cli([path])
        self.assertEqual(code, 0)
        self.assertIn("clean", out)

    def test_warning_only_exit_0(self):
        path = self.write("m.sql", "CREATE INDEX i ON t(c);\n")
        code, _, _ = self.run_cli([path])
        self.assertEqual(code, 0)

    def test_fail_on_warning_exit_1(self):
        path = self.write("m.sql", "CREATE INDEX i ON t(c);\n")
        code, _, _ = self.run_cli([path, "--fail-on-warning"])
        self.assertEqual(code, 1)

    def test_error_exit_1(self):
        path = self.write("m.sql", "ALTER TABLE t ADD COLUMN a int NOT NULL;\n")
        code, out, _ = self.run_cli([path])
        self.assertEqual(code, 1)
        self.assertIn("1 error(s)", out)

    def test_missing_file_exit_2(self):
        code, _, err = self.run_cli([os.path.join(self.dir, "nope.sql")])
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_ignore_flag(self):
        path = self.write("m.sql", "CREATE INDEX i ON t(c);\n")
        code, out, _ = self.run_cli(
            [path, "--ignore", "create-index-no-concurrently"])
        self.assertEqual(code, 0)
        self.assertIn("clean", out)

    def test_multiple_files(self):
        a = self.write("001.sql", "DROP TABLE old;\n")
        b = self.write("002.sql", "ALTER TABLE t ADD COLUMN a int;\n")
        code, out, _ = self.run_cli([a, b])
        self.assertEqual(code, 0)
        self.assertIn(a + ":1", out)

    def test_json_output(self):
        path = self.write("m.sql", "CREATE INDEX i ON t(c);\n")
        code, out, _ = self.run_cli([path, "--format", "json"])
        parsed = json.loads(out)
        self.assertEqual(parsed["summary"]["warnings"], 1)
        self.assertEqual(parsed["findings"][0]["rule"],
                         "create-index-no-concurrently")
        self.assertEqual(parsed["findings"][0]["file"], path)
        self.assertEqual(parsed["findings"][0]["line"], 1)

    def test_output_shape(self):
        path = self.write("m.sql", "DROP TABLE old;\n")
        _, out, _ = self.run_cli([path])
        self.assertIn("%s:1 [warning] drop-table" % path, out)


if __name__ == "__main__":
    unittest.main()
