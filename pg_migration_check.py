#!/usr/bin/env python3
"""pg-migration-check - a CI linter for PostgreSQL migration files.

Reads .sql migration files and flags statements that are destructive,
fail on non-empty tables, or take heavy locks / full-table rewrites on
production databases. Rules are grounded in the current PostgreSQL
documentation (ALTER TABLE / CREATE INDEX reference pages); anything that
is a heuristic rather than a documented fact is labeled as one.

Pure standard library. No database connection required.

Sources:
- https://www.postgresql.org/docs/current/sql-altertable.html
- https://www.postgresql.org/docs/current/sql-createindex.html
"""

import argparse
import json
import re
import sys

VOLATILE_DEFAULT = re.compile(
    r"\b(random|clock_timestamp|uuid_generate_v4|gen_random_uuid|"
    r"txid_current|nextval|currval)\s*\(", re.IGNORECASE)
RE_INDEX = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\b", re.IGNORECASE)
RE_CONCURRENTLY = re.compile(r"\bCONCURRENTLY\b", re.IGNORECASE)
RE_ALTER_TABLE = re.compile(r"^\s*ALTER\s+TABLE\b", re.IGNORECASE)
RE_ADD_COLUMN = re.compile(r"\bADD\s+(?:COLUMN\s+)?", re.IGNORECASE)
RE_NOT_NULL = re.compile(r"\bNOT\s+NULL\b", re.IGNORECASE)
RE_DEFAULT = re.compile(r"\bDEFAULT\b", re.IGNORECASE)
RE_ALTER_TYPE = re.compile(
    r"\b(?:ALTER\s+(?:COLUMN\s+)?\w+\s+(?:SET\s+DATA\s+)?TYPE|"
    r"SET\s+DATA\s+TYPE)\b", re.IGNORECASE)
RE_SET_NOT_NULL = re.compile(
    r"\bALTER\s+(?:COLUMN\s+)?\w+\s+SET\s+NOT\s+NULL\b", re.IGNORECASE)
RE_ADD_FK_OR_CHECK = re.compile(
    r"\bADD\s+(?:CONSTRAINT\s+\w+\s+)?(?:FOREIGN\s+KEY|CHECK)\b",
    re.IGNORECASE)
RE_NOT_VALID = re.compile(r"\bNOT\s+VALID\b", re.IGNORECASE)
RE_DROP_COLUMN = re.compile(
    r"\bDROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?"
    r"(?!NOT\b|CONSTRAINT\b|DEFAULT\b|EXPRESSION\b|IDENTITY\b)"
    r"[A-Za-z_]", re.IGNORECASE)
RE_DROP_TABLE = re.compile(r"^\s*DROP\s+TABLE\b", re.IGNORECASE)
RE_BEGIN = re.compile(r"^\s*(?:BEGIN|START\s+TRANSACTION)\b", re.IGNORECASE)
RE_COMMIT = re.compile(r"^\s*(?:COMMIT|ROLLBACK|END)\b", re.IGNORECASE)


class Finding:
    def __init__(self, path, line, rule, severity, message):
        self.path = path
        self.line = line
        self.rule = rule
        self.severity = severity  # "error" or "warning"
        self.message = message

    def as_dict(self):
        return {
            "file": self.path,
            "line": self.line,
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
        }


def split_statements(text):
    """Split SQL into (start_line, statement) pairs.

    A small state machine - not a full SQL parser: line comments (--),
    block comments, single-quoted strings ('' escape), and dollar-quoted
    bodies ($tag$...$tag$) are handled; semicolons inside them do not
    split statements. Comments are stripped from returned statements.
    """
    statements = []
    buf = []
    start_line = None
    i = 0
    line = 1
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                if text[i] == "\n":
                    line += 1
                i += 1
            i += 2
            continue
        if ch == "'":
            if start_line is None:
                start_line = line
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(text[i])
                if text[i] == "\n":
                    line += 1
                if text[i] == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        buf.append(text[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", text[i:])
        if m:
            tag = m.group(0)
            if start_line is None:
                start_line = line
            end = text.find(tag, i + len(tag))
            if end == -1:
                buf.append(text[i:])
                line += text[i:].count("\n")
                i = n
                continue
            body = text[i:end + len(tag)]
            buf.append(body)
            line += body.count("\n")
            i = end + len(tag)
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append((start_line or line, stmt))
            buf = []
            start_line = None
            i += 1
            continue
        if start_line is None and not ch.isspace():
            start_line = line
        buf.append(ch)
        if ch == "\n":
            line += 1
        i += 1
    stmt = "".join(buf).strip()
    if stmt:
        statements.append((start_line or line, stmt))
    return statements


def _add_column_parts(stmt):
    """Return the substring after ADD [COLUMN] in an ALTER TABLE statement,
    or None. Only inspects the first subcommand."""
    m = re.search(r"\bADD\s+(?:COLUMN\s+)?", stmt, re.IGNORECASE)
    if not m:
        return None
    tail = stmt[m.end():]
    if re.match(r"CONSTRAINT\b", tail, re.IGNORECASE):
        return None  # table constraint, not a column definition
    return tail


def check_migration(text, path):
    findings = []

    def emit(line, rule, severity, message):
        findings.append(Finding(path, line, rule, severity, message))

    statements = split_statements(text)
    in_tx = False

    for lineno, stmt in statements:
        if RE_BEGIN.match(stmt):
            in_tx = True
            continue
        if RE_COMMIT.match(stmt):
            in_tx = False
            continue
        if RE_INDEX.match(stmt):
            if RE_CONCURRENTLY.search(stmt):
                if in_tx:
                    emit(lineno, "concurrently-in-transaction", "error",
                         "CREATE INDEX CONCURRENTLY cannot run inside a "
                         "transaction block (PostgreSQL CREATE INDEX docs); "
                         "this file opens a transaction.")
            else:
                emit(lineno, "create-index-no-concurrently", "warning",
                     "CREATE INDEX without CONCURRENTLY locks out writes "
                     "(not reads) on the table until the build finishes; "
                     "use CONCURRENTLY for live tables.")
            continue

        if RE_DROP_TABLE.match(stmt):
            emit(lineno, "drop-table", "warning",
                 "DROP TABLE is destructive and irreversible outside a "
                 "restored backup (heuristic review rule).")
            continue

        if not RE_ALTER_TABLE.match(stmt):
            continue

        tail = _add_column_parts(stmt)
        if tail is not None and not RE_ADD_FK_OR_CHECK.search(stmt):
            if RE_NOT_NULL.search(tail) and not RE_DEFAULT.search(tail):
                emit(lineno, "add-column-not-null-no-default", "error",
                     "ADD COLUMN ... NOT NULL without DEFAULT fails on a "
                     "table that already has rows (existing rows would "
                     "violate the constraint).")
            m = RE_DEFAULT.search(tail)
            if m and VOLATILE_DEFAULT.search(tail[m.end():]):
                emit(lineno, "add-column-volatile-default", "warning",
                     "ADD COLUMN with a volatile DEFAULT rewrites the "
                     "entire table and its indexes (PostgreSQL ALTER TABLE "
                     "docs); a non-volatile DEFAULT is metadata-only.")
        if RE_ALTER_TYPE.search(stmt):
            emit(lineno, "alter-column-type", "warning",
                 "changing a column's type normally rewrites the entire "
                 "table and its indexes (PostgreSQL ALTER TABLE docs).")
        if RE_SET_NOT_NULL.search(stmt):
            emit(lineno, "set-not-null", "warning",
                 "SET NOT NULL ordinarily scans the entire table to verify "
                 "existing rows (PostgreSQL ALTER TABLE docs); a valid "
                 "CHECK constraint that proves no NULL can exist skips the "
                 "scan.")
        if RE_ADD_FK_OR_CHECK.search(stmt) and not RE_NOT_VALID.search(stmt):
            emit(lineno, "add-constraint-without-not-valid", "warning",
                 "adding a FOREIGN KEY or CHECK constraint without NOT "
                 "VALID scans the table to verify existing rows; adding it "
                 "NOT VALID and running VALIDATE CONSTRAINT separately "
                 "skips the lengthy scan.")
        if RE_DROP_COLUMN.search(stmt):
            emit(lineno, "drop-column", "warning",
                 "DROP COLUMN is destructive; indexes and table constraints "
                 "involving the column are dropped automatically "
                 "(PostgreSQL ALTER TABLE docs).")
    return findings


def format_text(results):
    lines = []
    errors = warnings = 0
    for path, findings in results:
        for f in findings:
            lines.append("%s:%d [%s] %s: %s" % (
                f.path, f.line, f.severity, f.rule, f.message))
            if f.severity == "error":
                errors += 1
            else:
                warnings += 1
    if errors or warnings:
        lines.append("pg-migration-check: %d error(s), %d warning(s)" % (
            errors, warnings))
    else:
        lines.append("pg-migration-check: clean")
    return "\n".join(lines), errors, warnings


def format_json(results, errors, warnings):
    return json.dumps({
        "findings": [f.as_dict() for _, fs in results for f in fs],
        "summary": {"files": len(results), "errors": errors,
                    "warnings": warnings},
    }, indent=2)


def build_parser():
    p = argparse.ArgumentParser(
        prog="pg-migration-check",
        description="Lint PostgreSQL migration files for destructive, "
                    "failing, or heavy-locking statements. No database "
                    "required.")
    p.add_argument("migrations", nargs="+", help="path(s) to .sql files")
    p.add_argument("--ignore", action="append", default=[], metavar="RULE",
                   help="suppress a rule by name (repeatable)")
    p.add_argument("--fail-on-warning", action="store_true",
                   help="exit 1 when only warnings are present")
    p.add_argument("--format", choices=["text", "json"], default="text",
                   help="output format (default: text)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    ignore = set(args.ignore)
    results = []
    for path in args.migrations:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            print("pg-migration-check: cannot read %s: %s" % (path, exc),
                  file=sys.stderr)
            return 2
        findings = [f for f in check_migration(text, path)
                    if f.rule not in ignore]
        results.append((path, findings))

    text_out, errors, warnings = format_text(results)
    if args.format == "json":
        print(format_json(results, errors, warnings))
    else:
        print(text_out)
    if errors:
        return 1
    if args.fail_on_warning and warnings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
