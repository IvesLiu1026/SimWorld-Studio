#!/usr/bin/env python3
"""Create the target Postgres database if it does not already exist."""

from __future__ import annotations

import argparse
import os
from urllib.parse import urlsplit, urlunsplit

import psycopg2
from psycopg2 import sql


def database_name(url: str) -> str:
    parsed = urlsplit(url)
    name = parsed.path.lstrip("/")
    if not name:
        raise ValueError(f"POSTGRES_URL has no database name: {url}")
    return name


def url_with_database(url: str, database: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/" + database, parsed.query, parsed.fragment))


def connect_maintenance(target_url: str, maintenance_dbs: list[str]):
    last_error: Exception | None = None
    for db in maintenance_dbs:
        try:
            return psycopg2.connect(url_with_database(target_url, db), connect_timeout=10)
        except Exception as e:  # noqa: PERF203 - keep all connection attempts visible.
            last_error = e
    raise RuntimeError(f"could not connect to maintenance database candidates {maintenance_dbs}: {last_error}")


def ensure_database(target_url: str, maintenance_dbs: list[str]) -> str:
    target_db = database_name(target_url)
    conn = connect_maintenance(target_url, maintenance_dbs)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,))
            if cur.fetchone():
                return "exists"
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_db)))
            return "created"
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-url", default=os.environ.get("POSTGRES_URL", ""))
    parser.add_argument(
        "--maintenance-db",
        action="append",
        default=[],
        help="Maintenance DB to connect to before creating the target DB. Can be repeated.",
    )
    args = parser.parse_args()
    if not args.postgres_url:
        raise SystemExit("ERROR: --postgres-url or POSTGRES_URL is required")
    maintenance = args.maintenance_db or [os.environ.get("POSTGRES_MAINT_DB", "postgres"), "asset_db"]
    status = ensure_database(args.postgres_url, maintenance)
    print(f"postgres_database={database_name(args.postgres_url)} status={status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
