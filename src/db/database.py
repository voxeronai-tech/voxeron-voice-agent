import os
import asyncpg
from typing import Optional, List, Dict
import logging
import json

logger = logging.getLogger(__name__)

class Database:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

        # Prefer env var; fallback only if not set.
        # NOTE: .env is loaded by src/api/server.py at process start (load_dotenv()).
        self.db_url = os.getenv("DATABASE_URL") or ""
        if not self.db_url:
            # Keep a safe error message (do NOT print secrets)
            logger.warning("DATABASE_URL is not set in environment; Database.connect() will fail.")

    async def connect(self):
        """Initialize connection pool"""
        if self.pool:
            return

        if not self.db_url:
            raise RuntimeError(
                "DATABASE_URL is empty. "
                "Load .env (e.g. via load_dotenv()) or export DATABASE_URL in your shell."
            )

        self.pool = await asyncpg.create_pool(
            self.db_url,
            min_size=2,
            max_size=10
        )
        logger.info("✅ Database connected to Neon")

    async def close(self):
        if self.pool:
            await self.pool.close()
            self.pool = None

    # MENU QUERIES
    async def get_menu_items(self, category_id: Optional[int] = None) -> List[Dict]:
        """Get menu items, optionally filtered by category"""
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            if category_id:
                query = """
                    SELECT item_id, name_en, description_en, price_delivery,
                           category_id, customizable_spice
                    FROM menu_items
                    WHERE is_available = true AND category_id = $1
                    ORDER BY display_order
                """
                rows = await conn.fetch(query, category_id)
            else:
                query = """
                    SELECT item_id, name_en, description_en, price_delivery,
                           category_id, customizable_spice
                    FROM menu_items
                    WHERE is_available = true
                    ORDER BY category_id, display_order
                """
                rows = await conn.fetch(query)

            return [dict(row) for row in rows]

    async def search_menu_item(self, query: str) -> Optional[Dict]:
        """Fuzzy search for menu item by name"""
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT item_id, name_en, description_en, price_delivery,
                       category_id, customizable_spice,
                       is_vegetarian, is_vegan, contains_nuts, contains_dairy
                FROM menu_items
                WHERE is_available = true
                  AND (LOWER(name_en) LIKE LOWER($1)
                       OR LOWER(search_keywords_en) LIKE LOWER($1))
                ORDER BY
                    CASE
                        WHEN LOWER(name_en) = LOWER($2) THEN 1
                        WHEN LOWER(name_en) LIKE LOWER($1) THEN 2
                        ELSE 3
                    END
                LIMIT 1
            """, f"%{query}%", query)

            return dict(row) if row else None

    async def get_categories(self) -> List[Dict]:
        """Get all active categories"""
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT category_id, name_en, description_en
                FROM menu_categories
                WHERE is_active = true
                ORDER BY display_order
            """)
            return [dict(row) for row in rows]

    # CUSTOMER MANAGEMENT
    async def get_or_create_customer(self, phone: str, name: str = None) -> int:
        """Get existing customer or create new one"""
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT customer_id FROM customers WHERE phone = $1
            """, phone)

            if row:
                return row['customer_id']

            row = await conn.fetchrow("""
                INSERT INTO customers (name, phone)
                VALUES ($1, $2)
                RETURNING customer_id
            """, name or "Guest", phone)

            return row['customer_id']

    # ORDER MANAGEMENT
    async def create_order(self, customer_id: int, order_type: str = "delivery") -> int:
        """Create new order"""
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO orders (customer_id, order_type, order_status, total_amount)
                VALUES ($1, $2, 'NEW', 0.00)
                RETURNING order_id
            """, customer_id, order_type)

            return row['order_id']

    async def add_order_item(
        self,
        order_id: int,
        item_id: str,
        quantity: int = 1,
        spice_level: Optional[str] = None,
        special_notes: Optional[str] = None
    ):
        """Add item to order"""
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            item = await conn.fetchrow("""
                SELECT price_delivery FROM menu_items WHERE item_id = $1
            """, item_id)

            if not item:
                raise ValueError(f"Item {item_id} not found")

            price = float(item['price_delivery'])

            customizations = {}
            if spice_level:
                customizations['spice_level'] = spice_level
            if special_notes:
                customizations['notes'] = special_notes

            await conn.execute("""
                INSERT INTO order_items (order_id, item_id, quantity, price_at_order, customizations)
                VALUES ($1, $2, $3, $4, $5)
            """, order_id, item_id, quantity, price * quantity, json.dumps(customizations) if customizations else None)

            await conn.execute("""
                UPDATE orders
                SET total_amount = (
                    SELECT COALESCE(SUM(price_at_order), 0)
                    FROM order_items
                    WHERE order_id = $1
                )
                WHERE order_id = $1
            """, order_id)

    async def get_order(self, order_id: int) -> Dict:
        """Get order with all items"""
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            order = await conn.fetchrow("""
                SELECT o.*, c.name as customer_name, c.phone as customer_phone
                FROM orders o
                JOIN customers c ON o.customer_id = c.customer_id
                WHERE o.order_id = $1
            """, order_id)

            if not order:
                return None

            items = await conn.fetch("""
                SELECT oi.*, m.name_en as item_name
                FROM order_items oi
                JOIN menu_items m ON oi.item_id = m.item_id
                WHERE oi.order_id = $1
            """, order_id)

            return {
                **dict(order),
                'items': [dict(item) for item in items]
            }

    async def finalize_order(self, order_id: int):
        """Mark order as confirmed"""
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE orders
                SET order_status = 'CONFIRMED'
                WHERE order_id = $1
            """, order_id)

# Global instance
db = Database()
