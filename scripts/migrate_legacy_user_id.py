"""Safely move one legacy email-keyed workspace to a Clerk user ID.

Run with --dry-run first. The migration only changes tenant identity columns;
campaign, contact, sender, and delivery record IDs stay unchanged.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable
from pathlib import Path

import psycopg2
from psycopg2 import sql
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-user-id", required=True, help="Existing legacy owner, for example an email address")
    parser.add_argument("--to-user-id", required=True, help="Destination immutable Clerk user ID")
    parser.add_argument("--dry-run", action="store_true", help="Show affected records without changing the database")
    parser.add_argument("--apply", action="store_true", help="Commit the migration")
    args = parser.parse_args()
    if args.dry_run == args.apply:
        parser.error("Choose exactly one of --dry-run or --apply")
    if args.from_user_id == args.to_user_id:
        parser.error("--from-user-id and --to-user-id must differ")
    return args


def user_id_tables(cursor) -> list[str]:
    cursor.execute(
        """
        SELECT table_name
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND column_name = 'user_id'
        ORDER BY table_name
        """
    )
    return [row[0] for row in cursor.fetchall()]


def table_count(cursor, table: str, user_id: str) -> int:
    cursor.execute(
        sql.SQL("SELECT COUNT(*) FROM {} WHERE user_id = %s").format(sql.Identifier(table)),
        (user_id,),
    )
    return int(cursor.fetchone()[0])


def table_exists(cursor, table: str) -> bool:
    cursor.execute("SELECT to_regclass(%s)", (f"{table}",))
    return cursor.fetchone()[0] is not None


def user_columns(cursor) -> set[str]:
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'users'
        """
    )
    return {row[0] for row in cursor.fetchall()}


def ensure_destination_user(cursor, source: str, destination: str) -> bool:
    """Create the destination user before updating foreign-keyed records."""

    if not table_exists(cursor, "users"):
        return False

    columns = user_columns(cursor)
    cursor.execute("SELECT 1 FROM users WHERE id = %s", (destination,))
    if cursor.fetchone():
        raise RuntimeError("Destination Clerk user ID already exists; refusing to merge workspaces")

    cursor.execute("SELECT 1 FROM users WHERE id = %s", (source,))
    source_exists = cursor.fetchone() is not None
    if source_exists:
        copy_columns = [column for column in ("email", "created_at", "updated_at") if column in columns]
        cursor.execute(
            sql.SQL("INSERT INTO users ({}) SELECT {} FROM users WHERE id = %s").format(
                sql.SQL(", ").join([sql.Identifier("id"), *map(sql.Identifier, copy_columns)]),
                sql.SQL(", ").join([sql.Placeholder(), *map(sql.Identifier, copy_columns)]),
            ),
            (destination, source),
        )
        return True

    # A compatibility-only deployment may have legacy rows without a matching
    # ORM users record. Make the minimum valid platform user row in that case.
    insert_columns = ["id"]
    values: list[sql.Composable] = [sql.Placeholder()]
    params: list[object] = [destination]
    if "created_at" in columns:
        insert_columns.append("created_at")
        values.append(sql.SQL("NOW()"))
    if "updated_at" in columns:
        insert_columns.append("updated_at")
        values.append(sql.SQL("NOW()"))
    cursor.execute(
        sql.SQL("INSERT INTO users ({}) VALUES ({})").format(
            sql.SQL(", ").join(map(sql.Identifier, insert_columns)),
            sql.SQL(", ").join(values),
        ),
        params,
    )
    return False


def print_counts(label: str, counts: Iterable[tuple[str, int]]) -> None:
    print(label)
    any_rows = False
    for table, count in counts:
        if count:
            any_rows = True
            print(f"  {table}: {count}")
    if not any_rows:
        print("  (no rows)")


def main() -> int:
    args = parse_args()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2

    with psycopg2.connect(dsn) as connection, connection.cursor() as cursor:
        tables = user_id_tables(cursor)
        source_counts = [(table, table_count(cursor, table, args.from_user_id)) for table in tables]
        destination_counts = [(table, table_count(cursor, table, args.to_user_id)) for table in tables]
        if table_exists(cursor, "users"):
            cursor.execute("SELECT COUNT(*) FROM users WHERE id = %s", (args.from_user_id,))
            source_user = int(cursor.fetchone()[0])
            cursor.execute("SELECT COUNT(*) FROM users WHERE id = %s", (args.to_user_id,))
            destination_user = int(cursor.fetchone()[0])
        else:
            source_user = destination_user = 0

        print_counts(f"Records owned by {args.from_user_id!r}:", source_counts)
        print_counts(f"Records already owned by {args.to_user_id!r}:", destination_counts)
        print(f"users table rows: source={source_user}, destination={destination_user}")

        if any(count for _, count in destination_counts) or destination_user:
            raise RuntimeError("Destination Clerk user ID already has records; refusing to merge workspaces")
        if not any(count for _, count in source_counts) and not source_user:
            raise RuntimeError("No source records found; refusing to perform an empty migration")
        if args.dry_run:
            print("Dry run complete. No records changed.")
            return 0

        source_user_exists = ensure_destination_user(cursor, args.from_user_id, args.to_user_id)
        for table, count in source_counts:
            if not count:
                continue
            cursor.execute(
                sql.SQL("UPDATE {} SET user_id = %s WHERE user_id = %s").format(sql.Identifier(table)),
                (args.to_user_id, args.from_user_id),
            )
            if cursor.rowcount != count:
                raise RuntimeError(f"Unexpected update count for {table}")
        if source_user_exists:
            cursor.execute("DELETE FROM users WHERE id = %s", (args.from_user_id,))
        print("Migration applied. All affected records now use the Clerk user ID.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, psycopg2.Error) as exc:
        print(f"Migration aborted: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
