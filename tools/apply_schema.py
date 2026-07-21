#!/usr/bin/env python3
"""Apply tools/schema.sql to Postgres without requiring the psql CLI."""
import os
import pathlib

import psycopg2

try:
    from asset_stack_config import load_secret
except ModuleNotFoundError:  # Imported as tools.apply_schema in unit tests.
    from tools.asset_stack_config import load_secret

SCHEMA_SQL = pathlib.Path(__file__).with_name("schema.sql")


def main():
    postgres_url, _source = load_secret(
        os.environ, "POSTGRES_URL", "POSTGRES_URL_FILE", required=True
    )
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    conn = psycopg2.connect(
        postgres_url,
        connect_timeout=10,
        application_name="simworld_asset_schema",
    )
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
    finally:
        conn.close()
    print(f"Applied schema: {SCHEMA_SQL}")


if __name__ == "__main__":
    main()
