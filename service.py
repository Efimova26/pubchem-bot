"""Логика без Telegram: поиск вещества и регулярное обновление базы.

Отделена от bot.py, чтобы её можно было тестировать и переиспользовать
(например, в другом интерфейсе) без запуска бота.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from db import Change, Database, normalize_name
from pubchem import Compound, CompoundNotFound, PubChemClient, PubChemError

log = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 100


@dataclass
class LookupResult:
    compound: Compound | None
    source: str                    # 'api', 'cache', 'not_found', 'error', 'invalid'
    known_under_other_name: bool = False
    message: str = ""


@dataclass
class RefreshReport:
    checked: int = 0
    changed: dict[str, list[Change]] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)


async def lookup(db: Database, client: PubChemClient, raw_query: str, user_id: int) -> LookupResult:
    alias = normalize_name(raw_query)
    if not alias or len(alias) > MAX_QUERY_LENGTH:
        return LookupResult(None, "invalid", message="Пришли название вещества до 100 символов.")

    # 1. Кэш: это название уже искали — в API не ходим
    cached = db.get_by_alias(alias)
    if cached:
        db.log_query(user_id, raw_query, "cache", cached.inchikey)
        return LookupResult(cached, "cache")

    # 2. Запрос в PubChem
    try:
        compound = await client.by_name(alias)
    except CompoundNotFound:
        db.log_query(user_id, raw_query, "not_found")
        return LookupResult(None, "not_found", message="PubChem не знает такого вещества.")
    except PubChemError as exc:
        log.warning("PubChem error for %r: %s", alias, exc)
        db.log_query(user_id, raw_query, "error")
        return LookupResult(None, "error", message="PubChem сейчас не отвечает, попробуй позже.")

    # 3. Сохранение без дублей: если InChIKey уже есть, добавится только новое название
    result = db.save(compound, alias)
    db.log_query(user_id, raw_query, "api", compound.inchikey)
    return LookupResult(compound, "api", known_under_other_name=result.known_under_other_name)


async def refresh_all(db: Database, client: PubChemClient) -> RefreshReport:
    """Перезапрашивает все вещества из базы и записывает, что изменилось в PubChem."""
    report = RefreshReport()
    for inchikey, cid in db.all_compounds():
        report.checked += 1
        try:
            fresh = await client.by_cid(cid)
        except (CompoundNotFound, PubChemError) as exc:
            log.warning("Refresh failed for CID %s: %s", cid, exc)
            report.failed.append(inchikey)
            continue
        if fresh.inchikey != inchikey:
            # Противоречие: CID теперь указывает на другое вещество. Не перезаписываем молча
            log.error("CID %s now maps to %s, expected %s", cid, fresh.inchikey, inchikey)
            report.failed.append(inchikey)
            continue
        result = db.save(fresh)
        if result.changes:
            report.changed[inchikey] = result.changes
    log.info("Refresh: checked=%d changed=%d failed=%d",
             report.checked, len(report.changed), len(report.failed))
    return report
