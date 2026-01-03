import asyncio
import os
import sys
from pathlib import Path
from pprint import pprint

# Ensure repo root is on sys.path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load .env (no secrets printed)
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from src.db.database import db

async def main():
    print("==[verify_08_tenants]==")
    print("ROOT:", ROOT)
    print("DATABASE_URL set:", bool(os.getenv("DATABASE_URL")))

    await db.connect()
    try:
        async with db.pool.acquire() as con:
            # List columns
            cols = await con.fetch("""
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name='tenants'
                ORDER BY ordinal_position
            """)
            print("\n-- tenants columns --")
            for c in cols:
                print(f"{c['column_name']:<20} {c['data_type']:<18} nullable={c['is_nullable']}")

            # Show a few sample rows
            rows = await con.fetch("""
                SELECT *
                FROM tenants
                ORDER BY created_at NULLS LAST
                LIMIT 5
            """)
            print("\n-- tenants sample rows (up to 5) --")
            for r in rows:
                pprint(dict(r))

    finally:
        await db.close()

    print("\n==[end verify_08_tenants]==")

asyncio.run(main())
