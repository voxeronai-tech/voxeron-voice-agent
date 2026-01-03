import json
import logging
from typing import Any, Dict, Optional, Tuple

import asyncpg

logger = logging.getLogger(__name__)


class OrderRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def get_or_create_customer(self, tenant_id: str, session_id: str) -> int:
        """
        customers.phone is NOT NULL, so we use session_id as a stable demo key.
        """
        async with self.pool.acquire() as con:
            await con.execute("SET search_path TO public;")

            row = await con.fetchrow(
                """
                SELECT customer_id
                FROM customers
                WHERE tenant_id = $1 AND phone = $2
                """,
                tenant_id,
                session_id,
            )
            if row:
                return int(row["customer_id"])

            row = await con.fetchrow(
                """
                INSERT INTO customers (tenant_id, name, phone)
                VALUES ($1, $2, $3)
                RETURNING customer_id
                """,
                tenant_id,
                f"Voice Customer ({session_id[:8]})",
                session_id,
            )
            return int(row["customer_id"])

    async def create_order(
        self,
        tenant_id: str,
        customer_id: int,
        session_id: str,
        language: Optional[str] = None,
        order_type: str = "PICKUP",
    ) -> int:
        async with self.pool.acquire() as con:
            await con.execute("SET search_path TO public;")
            row = await con.fetchrow(
                """
                INSERT INTO orders (tenant_id, customer_id, order_type, total_amount, session_id, language, order_status)
                VALUES ($1, $2, $3, 0, $4, $5, 'NEW')
                RETURNING order_id
                """,
                tenant_id,
                customer_id,
                order_type,
                session_id,
                language,
            )
            return int(row["order_id"])

    async def add_order_item(
        self,
        tenant_id: str,
        order_id: int,
        item_id: str,
        quantity: int,
        price_at_order: float,
        customizations: Optional[Dict[str, Any]] = None,
    ) -> int:
        async with self.pool.acquire() as con:
            await con.execute("SET search_path TO public;")

            row = await con.fetchrow(
                """
                INSERT INTO order_items (tenant_id, order_id, item_id, quantity, price_at_order, customizations)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                RETURNING order_item_id
                """,
                tenant_id,
                order_id,
                item_id,
                int(quantity),
                float(price_at_order),
                json.dumps(customizations or {}),
            )

            # Update totals (simple)
            await con.execute(
                """
                UPDATE orders
                SET total_amount = (
                    SELECT COALESCE(SUM(quantity * price_at_order),0)
                    FROM order_items
                    WHERE tenant_id = $1 AND order_id = $2
                )
                WHERE tenant_id = $1 AND order_id = $2
                """,
                tenant_id,
                order_id,
            )

            return int(row["order_item_id"])
