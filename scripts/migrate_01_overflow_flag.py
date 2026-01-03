import asyncio, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from src.db.neon import NeonDB

async def main():
    print("==[migrate_01_overflow_flag]==")
    db = NeonDB()
    await db.connect()
    try:
        async with db.pool.acquire() as con:
            await con.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='tenants' AND column_name='overflow_enabled'
                ) THEN
                    ALTER TABLE tenants ADD COLUMN overflow_enabled boolean NOT NULL DEFAULT false;
                END IF;
            END$$;
            """)
            row = await con.fetchrow("SELECT tenant_id, name, overflow_enabled FROM tenants LIMIT 1")
            print("tenant:", row["name"], str(row["tenant_id"]), "overflow_enabled:", row["overflow_enabled"])
    finally:
        await db.close()
    print("==[end migrate_01_overflow_flag]==")

asyncio.run(main())
