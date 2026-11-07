"""Клиент PubChem PUG REST: запросы к API, ограничение частоты, повторы, разбор ответа."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import quote

import httpx

BASE_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"

# В 2025 году PubChem переименовал свойства: IsomericSMILES -> SMILES,
# CanonicalSMILES -> ConnectivitySMILES. Со старыми именами API не падает,
# а молча возвращает ответ без этих полей. Поэтому ниже есть явная проверка
# обязательных полей: лучше громкая ошибка, чем тихие пустые данные.
PROPERTIES = ["MolecularFormula", "MolecularWeight", "SMILES", "InChIKey", "XLogP", "IUPACName"]
REQUIRED = ["CID", "InChIKey", "MolecularFormula", "MolecularWeight", "SMILES"]

MAX_ATTEMPTS = 3


class CompoundNotFound(Exception):
    """PubChem не знает такого вещества."""


class PubChemError(Exception):
    """Сбой API, сети или неожиданный формат ответа."""


@dataclass(frozen=True)
class Compound:
    cid: int
    inchikey: str
    formula: str
    mol_weight: float
    smiles: str
    xlogp: float | None
    iupac_name: str | None


def parse_properties(payload: dict) -> Compound:
    """Превращает JSON-ответ PubChem в Compound или явно сообщает, чего не хватает."""
    try:
        props = payload["PropertyTable"]["Properties"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise PubChemError("Неожиданная структура ответа PubChem") from exc

    missing = [field for field in REQUIRED if props.get(field) in (None, "")]
    if missing:
        raise PubChemError(f"В ответе PubChem нет полей: {', '.join(missing)}")

    xlogp = props.get("XLogP")
    return Compound(
        cid=int(props["CID"]),
        inchikey=props["InChIKey"],
        formula=props["MolecularFormula"],
        # PubChem отдаёт молярную массу строкой ("180.16"), приводим к числу
        mol_weight=float(props["MolecularWeight"]),
        smiles=props["SMILES"],
        xlogp=float(xlogp) if xlogp is not None else None,
        iupac_name=props.get("IUPACName"),
    )


class PubChemClient:
    """Асинхронный клиент. PubChem разрешает до 5 запросов в секунду, держим 4 с запасом."""

    def __init__(self, client: httpx.AsyncClient | None = None, max_rps: float = 4.0,
                 backoff_base: float = 1.0) -> None:
        self._client = client or httpx.AsyncClient(base_url=BASE_URL, timeout=15.0)
        self._min_interval = 1.0 / max_rps
        self._backoff_base = backoff_base
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def _throttle(self) -> None:
        # Лок нужен, чтобы два одновременных запроса не проскочили лимит вместе
        async with self._lock:
            loop = asyncio.get_running_loop()
            wait = self._last_call + self._min_interval - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = loop.time()

    async def _fetch(self, path: str) -> Compound:
        url = f"{path}/property/{','.join(PROPERTIES)}/JSON"
        for attempt in range(MAX_ATTEMPTS):
            await self._throttle()
            try:
                response = await self._client.get(url)
            except httpx.TransportError as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise PubChemError("PubChem недоступен") from exc
                await asyncio.sleep(self._backoff_base * 2 ** attempt)
                continue

            # 404 — нет такого вещества, 400 — PubChem не смог разобрать имя
            if response.status_code in (400, 404):
                raise CompoundNotFound(path)
            # 429/503 — сервер перегружен: ждём и пробуем снова (экспоненциальная пауза)
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == MAX_ATTEMPTS - 1:
                    raise PubChemError(f"PubChem перегружен (HTTP {response.status_code})")
                await asyncio.sleep(self._backoff_base * 2 ** attempt)
                continue
            if response.status_code != 200:
                raise PubChemError(f"Неожиданный ответ PubChem: HTTP {response.status_code}")
            return parse_properties(response.json())
        raise PubChemError("PubChem не ответил")

    async def by_name(self, name: str) -> Compound:
        return await self._fetch(f"/compound/name/{quote(name, safe='')}")

    async def by_cid(self, cid: int) -> Compound:
        return await self._fetch(f"/compound/cid/{int(cid)}")

    async def aclose(self) -> None:
        await self._client.aclose()
