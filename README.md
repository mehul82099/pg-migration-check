# pg-migration-check

A CI linter for PostgreSQL migration files. It reads plain `.sql` files -
no database connection, no ORM - and flags statements that are
destructive, fail on non-empty tables, or lock / rewrite production
tables. Every rule cites the PostgreSQL documentation it implements;
anything that is a heuristic is labeled as one.

Pure Python standard library.

## The problem

Migrations that pass review on an empty dev database fail or stall in
production: `ADD COLUMN ... NOT NULL` errors out against existing rows, a
plain `CREATE INDEX` blocks every write until the build finishes, an
`ALTER COLUMN ... TYPE` silently rewrites a 200 GB table. PostgreSQL
documents all of this; the gap is that nothing checks it before the
migration runs. pg-migration-check is that check, small enough to audit
by reading the source.

## Usage

```bash
python3 pg_migration_check.py migrations/*.sql
python3 pg_migration_check.py --fail-on-warning --format json migrations/
```

(For a directory, pass the files: `migrations/*.sql`.)

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Clean (warnings only when `--fail-on-warning` is off) |
| 1 | Findings present (errors, or warnings with `--fail-on-warning`) |
| 2 | Input problem: an unreadable file |

Example output:

```
migrations/014.sql:3 [warning] create-index-no-concurrently: CREATE INDEX without CONCURRENTLY locks out writes (not reads) on the table until the build finishes; use CONCURRENTLY for live tables.
migrations/014.sql:5 [error] add-column-not-null-no-default: ADD COLUMN ... NOT NULL without DEFAULT fails on a table that already has rows (existing rows would violate the constraint).
pg-migration-check: 1 error(s), 1 warning(s)
```

## Rules

| Rule | Severity | Trigger | Grounding (PostgreSQL docs) |
|---|---|---|---|
| `create-index-no-concurrently` | warning | `CREATE INDEX` without `CONCURRENTLY` | a standard index build "locks out writes (but not reads) on the table until it's done" |
| `concurrently-in-transaction` | error | `CREATE INDEX CONCURRENTLY` while a transaction is open in the same file | "a regular `CREATE INDEX` command can be performed within a transaction block, but `CREATE INDEX CONCURRENTLY` cannot" |
| `add-column-not-null-no-default` | error | `ADD COLUMN ... NOT NULL` without `DEFAULT` | fails when the table already has rows |
| `add-column-volatile-default` | warning | `ADD COLUMN ... DEFAULT` calling a volatile function | "Adding a column with a volatile `DEFAULT` ... will cause the entire table and its indexes to be rewritten"; a non-volatile `DEFAULT` is metadata-only |
| `alter-column-type` | warning | `ALTER COLUMN ... TYPE` / `SET DATA TYPE` | "Changing the type of an existing column will normally cause the entire table and its indexes to be rewritten" |
| `set-not-null` | warning | `ALTER COLUMN ... SET NOT NULL` | "Ordinarily this is checked during the `ALTER TABLE` by scanning the entire table", unless a valid CHECK constraint proves no NULL can exist |
| `add-constraint-without-not-valid` | warning | `ADD` `FOREIGN KEY`/`CHECK` without `NOT VALID` | the `NOT VALID` option skips the "potentially-lengthy scan" of existing rows; validate later with `VALIDATE CONSTRAINT` |
| `drop-table` | warning | `DROP TABLE` | heuristic review rule: destructive and irreversible outside a restored backup |
| `drop-column` | warning | `ALTER TABLE ... DROP COLUMN` | destructive; "Indexes and table constraints involving the column will be automatically dropped as well" |

The volatile-function list (`random`, `clock_timestamp`, `gen_random_uuid`,
`uuid_generate_v4`, `txid_current`, `nextval`, `currval`) is a heuristic
approximation of volatility. Stable functions like `now()` are deliberately
NOT flagged: the docs' fast metadata-only path covers non-volatile
defaults.

## Options

```
--ignore RULE            suppress a rule by name (repeatable)
--fail-on-warning        exit 1 when only warnings are present
--format text|json       output format (default: text)
```

## Caveats

- PostgreSQL only, by design; other dialects have different locking rules.
- The statement splitter is a small state machine (comments, quoted
  strings, dollar-quoted bodies handled), not a full SQL parser. It is
  enough for ordinary migration files; exotic syntax may parse loosely,
  and findings are line-accurate, not execution-accurate.
- An `ALTER TABLE` combining several subcommands is analyzed as one
  statement, so a finding names the statement's first line.
- The transaction check for `concurrently-in-transaction` tracks
  `BEGIN`/`COMMIT`/`ROLLBACK` in file order; it cannot see transactions
  opened by the migration runner wrapping the file.

## Sources

- PostgreSQL ALTER TABLE reference:
  https://www.postgresql.org/docs/current/sql-altertable.html
- PostgreSQL CREATE INDEX reference (incl. Building Indexes Concurrently):
  https://www.postgresql.org/docs/current/sql-createindex.html

## Development

56 tests, all fabricated SQL, no database required:

```bash
python3 -m unittest -v
```

CI runs the suite on Ubuntu and Windows with Python 3.9 and 3.13.

## License

MIT - see LICENSE.
