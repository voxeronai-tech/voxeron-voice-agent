# src/api/menu_store.py
from __future__ import annotations

import logging
import os
import time
import asyncpg
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any

from ..db.database import db  # ✅ reuse your existing global Database() instance
from .text import norm_text

logger = logging.getLogger(__name__)

_GENERIC_NAAN_ALIASES = {"naan", "nan", "naam"}


def _is_flavored_naan_item_name(name_norm: str) -> bool:
    return any(x in name_norm for x in ("garlic", "cheese", "keema", "peshawari", "butter", "boter", "knoflook"))


def _prefer_new_generic_naan_mapping(existing_name: str, new_name: str) -> bool:
    # Prefer mapping generic "naan/nan/naam" to the plain/regular naan (if present)
    if not _is_flavored_naan_item_name(existing_name):
        return False
    if not _is_flavored_naan_item_name(new_name):
        return True
    return False


@dataclass
class MenuItem:
    item_id: str
    name: str
    description: str
    price_pickup: float
    price_delivery: float
    category_id: Optional[int]
    is_available: bool
    tags: Dict[str, Any]
    customizable_spice: Optional[bool] = None
    default_spice_level: Optional[str] = None


@dataclass
class MenuSnapshot:
    tenant_id: str
    tenant_name: str
    default_language: str = "english"

    items_by_id: Dict[str, MenuItem] = field(default_factory=dict)
    name_choices: List[Tuple[str, str]] = field(default_factory=list)  # (norm_name, item_id)
    alias_map: Dict[str, str] = field(default_factory=dict)           # norm_alias -> item_id

    def display_name(self, item_id: str) -> str:
        it = self.items_by_id.get(item_id)
        return it.name if it else item_id


class MenuStore:
    """
    MenuStore loads a per-tenant snapshot from Neon and builds alias_map.

    IMPORTANT: This implementation matches YOUR DB schema:
      - tenants: tenant_id, tenant_ref, name, default_language
      - menu_items: name_en/name_nl, description_en/description_nl, tags, etc.
      - menu_item_aliases: item_id, alias_text, tenant_id (lang may exist, but not required)
    """

    def __init__(self, database_url: str = "", ttl_seconds: int = 180, schema: str = "public"):
        self.schema = schema or "public"
        self.ttl_seconds = int(ttl_seconds or 180)
        self._cache: Dict[Tuple[str, str], Tuple[float, MenuSnapshot]] = {}

        # If a DATABASE_URL is passed, ensure env matches what src/db/database.py expects.
        if database_url and not os.getenv("DATABASE_URL"):
            os.environ["DATABASE_URL"] = database_url

    async def start(self) -> None:
        # Ensure pool is up
        await db.connect()

    async def close(self) -> None:
        await db.close()

    async def get_snapshot(self, tenant_ref: str, lang: str = "en") -> Optional[MenuSnapshot]:
        now = time.time()
        cache_key = (tenant_ref, lang)
        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]) < self.ttl_seconds:
            return cached[1]

        await db.connect()
        assert db.pool is not None

        async def _load_with_conn(conn) -> Optional[MenuSnapshot]:

            # --- Retry-once acquire wrapper (Neon/serverless can drop idle connections) ---
        last_exc: Optional[BaseException] = None
        for attempt in (1, 2):
            try:
                async with db.pool.acquire() as conn:
                    snap = await _load_with_conn(conn)

                    # If tenant not found, cache negative result briefly
                    if snap is None:
                        return None

                    # Continue building snapshot using the SAME conn below (existing code)
                    break
            except (
                asyncpg.exceptions.ConnectionDoesNotExistError,
                asyncpg.PostgresConnectionError,
                ConnectionResetError,
                OSError,
            ) as e:
                last_exc = e
                logger.warning("MenuStore.get_snapshot transient DB error (attempt %s/2): %s", attempt, e)
                try:
                    await db.reconnect()
                except Exception as e2:
                    logger.warning("DB reconnect failed: %s", e2)
                continue

        if last_exc and attempt == 2:
            logger.error("MenuStore.get_snapshot failed after retry: %s", last_exc)
            return None

            rows = await conn.fetch(
                f"""
                SELECT
                    item_id,
                    {name_col} AS name,
                    {desc_col} AS description,
                    price_pickup,
                    price_delivery,
                    category_id,
                    is_available,
                    tags,
                    customizable_spice,
                    default_spice_level
                FROM {self.schema}.menu_items
                WHERE tenant_id = $1 AND is_available = TRUE
                """,
                snap.tenant_id,
            )
            tenant = await conn.fetchrow(
                f"""
                SELECT tenant_id, name, default_language
                FROM {self.schema}.tenants
                WHERE tenant_ref = $1
                """,
                tenant_ref,
            )
            if not tenant:
                return None

            snap = MenuSnapshot(
                tenant_id=str(tenant["tenant_id"]),
                tenant_name=str(tenant["name"]),
                default_language=str(tenant.get("default_language") or "english"),
            )

            # pick language columns
            lang_n = (lang or "en").lower()
            if lang_n == "nl":
                name_col = "name_nl"
                desc_col = "description_nl"
            else:
                name_col = "name_en"
                desc_col = "description_en"

            rows = await conn.fetch(
                f"""
                SELECT
                    item_id,
                    {name_col} AS name,
                    {desc_col} AS description,
                    price_pickup,
                    price_delivery,
                    category_id,
                    is_available,
                    tags,
                    customizable_spice,
                    default_spice_level
                FROM {self.schema}.menu_items
                WHERE tenant_id = $1 AND is_available = TRUE
                """,
                snap.tenant_id,
            )

            def _set_alias(alias_norm: str, item_id: str, item_name_norm: str) -> None:
                if not alias_norm or len(alias_norm) < 3:
                    return

                if alias_norm in _GENERIC_NAAN_ALIASES:
                    # avoid mapping "naan" -> garlic naan if plain exists
                    if _is_flavored_naan_item_name(item_name_norm):
                        return
                    existing = snap.alias_map.get(alias_norm)
                    if existing and existing in snap.items_by_id:
                        existing_name = norm_text(snap.items_by_id[existing].name)
                        if not _prefer_new_generic_naan_mapping(existing_name, item_name_norm):
                            return

                snap.alias_map[alias_norm] = item_id

            for r in rows:
                item_id = str(r["item_id"])
                name = (r.get("name") or "").strip() or item_id
                description = (r.get("description") or "").strip()

                tags = r.get("tags") or {}
                if not isinstance(tags, dict):
                    tags = {}

                item = MenuItem(
                    item_id=item_id,
                    name=name,
                    description=description,
                    price_pickup=float(r.get("price_pickup") or 0),
                    price_delivery=float(r.get("price_delivery") or 0),
                    category_id=int(r["category_id"]) if r.get("category_id") is not None else None,
                    is_available=bool(r.get("is_available", True)),
                    tags=tags,
                    customizable_spice=bool(r.get("customizable_spice")) if r.get("customizable_spice") is not None else None,
                    default_spice_level=(str(r.get("default_spice_level")).strip() if r.get("default_spice_level") else None),
                )

                snap.items_by_id[item_id] = item
                name_norm = norm_text(name)
                snap.name_choices.append((name_norm, item_id))

                # name itself is an alias
                _set_alias(name_norm, item_id, name_norm)

                # optional search keywords (DB columns exist)
                kw = ""
                if lang_n == "nl":
                    kw = (r.get("search_keywords_nl") or "")
                else:
                    kw = (r.get("search_keywords_en") or "")

                # BUT: your SELECT above doesn't include search_keywords_* (kept light).
                # If you want them, add them to SELECT and this will work.
                # We'll still support tags.keywords for now:

                kws = (tags.get("keywords") or "")
                if isinstance(kws, str) and kws.strip():
                    for part in kws.replace("\n", ",").split(","):
                        _set_alias(norm_text(part), item_id, name_norm)

                aliases = tags.get("aliases")
                if isinstance(aliases, list):
                    for a in aliases[:30]:
                        _set_alias(norm_text(str(a)), item_id, name_norm)

            # explicit alias table
            alias_rows = await conn.fetch(
                f"""
                SELECT item_id, alias_text
                FROM {self.schema}.menu_item_aliases
                WHERE tenant_id = $1
                """,
                snap.tenant_id,
            )

            for ar in alias_rows:
                iid = str(ar["item_id"])
                if iid not in snap.items_by_id:
                    continue
                alias = norm_text(ar.get("alias_text") or "")
                if not alias:
                    continue
                name_norm = norm_text(snap.items_by_id[iid].name)
                _set_alias(alias, iid, name_norm)

        self._cache[cache_key] = (now, snap)
        return snap
