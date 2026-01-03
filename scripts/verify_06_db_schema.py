import asyncio
import os
import sys
from pathlib import Path

# Ensure repo root is on sys.path so `import src...` works when run from /scripts
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load .env (no secrets printed)
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from src.db.database import db

TARGET_TABLES = ["orders", "order_items", "customers", "menu_items"]

async def main():
    print("==[verify_06_db_schema]==")
    print("ROOT:", ROOT)
    print("DATABASE_URL set:", bool(os.getenv("DATABASE_URL")))

    await db.connect()
    try:
        async with db.pool.acquire() as con:
            rows = await con.fetch("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name
            """)
            tables = [r["table_name"] for r in rows]
            print("\n-- tables (public) --")
            print(", ".join(tables))

            for t in TARGET_TABLES:
                print(f"\n-- columns: {t} --")
                if t not in tables:
                    print("MISSING TABLE")
                    continue
                cols = await con.fetch("""
                    SELECT column_name, data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema='public' AND table_name=$1
                    ORDER BY ordinal_position
                """, t)
                for c in cols:
                    print(f"{c['column_name']:24} {c['data_type']:18} nullable={c['is_nullable']}")

            if "menu_items" in tables:
                sample = await con.fetchrow("SELECT * FROM menu_items LIMIT 1")
                print("\n-- sample menu_items keys --")
                if sample:
                    print(list(sample.keys()))
                    print("\n-- sample menu_items row (first ~20 fields) --")
                    d = dict(sample)
                    for k in list(d.keys())[:20]:
                        print(f"{k:24} = {d[k]}")
                else:
                    print("menu_items empty")
    finally:
        await db.close()

    print("\n==[end verify_06_db_schema]==")

if __name__ == "__main__":
    asyncio.run(main())
