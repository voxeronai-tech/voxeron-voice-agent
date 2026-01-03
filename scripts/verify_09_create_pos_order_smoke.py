import asyncio
import os
import sys
from pathlib import Path
from pprint import pprint
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from src.db.database import db

async def main():
    print("==[verify_09_create_pos_order_smoke]==")
    await db.connect()
    try:
        async with db.pool.acquire() as con:
            # 1) Get tenant UUID (as string)
            t = await con.fetchrow("SELECT tenant_id, name FROM tenants LIMIT 1")
            tenant_id = str(t["tenant_id"])
            print("tenant:", t["name"], tenant_id)

            # 2) Pick a menu item that exists
            mi = await con.fetchrow("""
                SELECT item_id, name_en, price_delivery
                FROM menu_items
                ORDER BY item_id
                LIMIT 1
            """)
            print("menu_item:", mi["item_id"], mi["name_en"], float(mi["price_delivery"]))

            # 3) Build a minimal OrderDraft that matches schema intent
            order_draft = {
                "tenant_id": tenant_id,
                "location_id": None,
                "session_id": "sess_smoke_001",
                "fulfillment_type": "pickup",
                "items": [
                    {
                        "item_id": mi["item_id"],
                        "name": mi["name_en"] or mi["item_id"],
                        "quantity": 1,
                        "unit_price": {"amount": float(mi["price_delivery"]), "currency": "EUR"},
                        "modifiers": [],
                        "notes": None,
                    }
                ],
                "idempotency_key": "smoke-1",
            }

            print("\n-- order_draft preview --")
            pprint(order_draft)

            # 4) Just verify DB schema can accept tenant_id + session_id conventions
            #    We'll insert a bare-min order directly (not via agent) as a DB smoke test
            customer_id = await con.fetchval(
                "INSERT INTO customers (tenant_id, name, phone) VALUES ($1, $2, $3) RETURNING customer_id",
                tenant_id, "Smoke Customer", "sess_smoke_001"
            )
            order_id = await con.fetchval("""
                INSERT INTO orders (tenant_id, customer_id, order_type, total_amount, session_id, order_status, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING order_id
            """, tenant_id, customer_id, "PICKUP", 0, "sess_smoke_001", "NEW", datetime.now(timezone.utc))

            await con.execute("""
                INSERT INTO order_items (tenant_id, order_id, item_id, quantity, price_at_order, customizations)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """, tenant_id, order_id, mi["item_id"], 1, float(mi["price_delivery"]), None)

            await con.execute("UPDATE orders SET total_amount=$1 WHERE order_id=$2", float(mi["price_delivery"]), order_id)

            print("\n✅ DB insert smoke OK. order_id:", order_id)

    finally:
        await db.close()

    print("\n==[end verify_09_create_pos_order_smoke]==")

asyncio.run(main())
