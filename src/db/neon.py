import os
import asyncpg
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class NeonDB:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self.dsn = dsn or os.getenv("DATABASE_URL", "")
        if not self.dsn:
            raise ValueError("DATABASE_URL missing")
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        if self.pool:
            return
        self.pool = await asyncpg.create_pool(
            dsn=self.dsn,
            min_size=1,
            max_size=5,
            command_timeout=30,
        )
        logger.info("✅ Neon pool connected")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None
            logger.info("✅ Neon pool closed")

    async def get_first_tenant_id(self) -> str:
        if not self.pool:
            raise RuntimeError("DB not connected")
        async with self.pool.acquire() as con:
            await con.execute("SET search_path TO public;")
            row = await con.fetchrow("SELECT tenant_id::text AS tenant_id FROM tenants ORDER BY created_at ASC LIMIT 1;")
            if not row:
                raise RuntimeError("No tenant found in tenants table")
            return row["tenant_id"]
