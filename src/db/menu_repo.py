import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import asyncpg

logger = logging.getLogger(__name__)


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


@dataclass
class ResolvedItem:
    item_id: str
    name_en: Optional[str]
    name_nl: Optional[str]
    price_pickup: float
    price_delivery: float
    customizable_spice: bool
    default_spice_level: Optional[str]
    confidence: float
    matched_alias: str


class MenuRepo:
    """
    Loads tenant-scoped aliases and resolves transcripts deterministically.
    Cache is in-memory, per process.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool
        self._alias_cache: Dict[str, Dict[str, List[Tuple[str, str, float]]]] = {}
        # cache shape:
        # tenant_id -> { alias_text -> [(item_id, lang, weight), ...] }

    async def warm_alias_cache(self, tenant_id: str) -> None:
        tenant_id = str(tenant_id)
        async with self.pool.acquire() as con:
            await con.execute("SET search_path TO public;")
            rows = await con.fetch(
                """
                SELECT item_id, lang, alias_text, weight
                FROM menu_item_aliases
                WHERE tenant_id = $1
                """,
                tenant_id,
            )

        alias_map: Dict[str, List[Tuple[str, str, float]]] = {}
        for r in rows:
            alias = _norm(r["alias_text"])
            if not alias:
                continue
            alias_map.setdefault(alias, []).append((r["item_id"], (r["lang"] or "").lower(), float(r["weight"] or 1.0)))

        self._alias_cache[tenant_id] = alias_map
        logger.info("✅ Alias cache warmed, tenant=%s, aliases=%d", tenant_id, len(alias_map))

    def _get_alias_map(self, tenant_id: str) -> Dict[str, List[Tuple[str, str, float]]]:
        return self._alias_cache.get(str(tenant_id), {})

    async def resolve_items(self, tenant_id: str, transcript: str, max_results: int = 3) -> List[ResolvedItem]:
        """
        Strategy:
        - Normalize transcript
        - Check for alias substring matches (longer aliases score higher)
        - If multiple items match, rank by score
        """
        t = _norm(transcript)
        if not t:
            return []

        alias_map = self._get_alias_map(tenant_id)
        if not alias_map:
            # cache miss, but don't block the call; caller should warm during startup
            logger.warning("Alias cache empty for tenant=%s", tenant_id)
            return []

        # Find all alias matches
        hits: Dict[str, Tuple[str, float, str]] = {}
        # item_id -> (matched_alias, score, lang)

        for alias, targets in alias_map.items():
            if len(alias) < 3:
                continue
            if alias in t:
                # base score favors longer aliases and explicit weights
                base = min(1.0, 0.15 + (len(alias) / 30.0))
                for (item_id, lang, weight) in targets:
                    score = base * float(weight or 1.0)
                    prev = hits.get(item_id)
                    if not prev or score > prev[1]:
                        hits[item_id] = (alias, score, lang)

        if not hits:
            return []

        # Fetch item details for top-N candidates
        ranked = sorted(hits.items(), key=lambda kv: kv[1][1], reverse=True)[: max_results * 2]
        item_ids = [iid for iid, _ in ranked]

        async with self.pool.acquire() as con:
            await con.execute("SET search_path TO public;")
            rows = await con.fetch(
                """
                SELECT item_id, name_en, name_nl, price_pickup, price_delivery,
                       COALESCE(customizable_spice,false) AS customizable_spice,
                       default_spice_level
                FROM menu_items
                WHERE tenant_id = $1 AND item_id = ANY($2::varchar[])
                """,
                str(tenant_id),
                item_ids,
            )

        by_id = {r["item_id"]: r for r in rows}

        results: List[ResolvedItem] = []
        for item_id, (alias, score, lang) in ranked:
            r = by_id.get(item_id)
            if not r:
                continue
            results.append(
                ResolvedItem(
                    item_id=item_id,
                    name_en=r["name_en"],
                    name_nl=r["name_nl"],
                    price_pickup=float(r["price_pickup"]),
                    price_delivery=float(r["price_delivery"]),
                    customizable_spice=bool(r["customizable_spice"]),
                    default_spice_level=r["default_spice_level"],
                    confidence=float(score),
                    matched_alias=alias,
                )
            )

        # Final rank + trim
        results.sort(key=lambda x: x.confidence, reverse=True)
        return results[:max_results]
