#!/usr/bin/env python3
"""Apply tools/schema.sql to Postgres without requiring the psql CLI."""
import os
import pathlib

import psycopg2


POSTGRES_URL = os.environ["POSTGRES_URL"]
SCHEMA_SQL = pathlib.Path(__file__).with_name("schema.sql")


def main():
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    conn = psycopg2.connect(POSTGRES_URL)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
    finally:
        conn.close()
    print(f"Applied schema: {SCHEMA_SQL}")


if __name__ == "__main__":
    main()
