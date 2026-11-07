"""Тесты без интернета: PubChem подменяется фейковым сервером (httpx.MockTransport)."""
import asyncio
import sys
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database, normalize_name  # noqa: E402
from pubchem import (BASE_URL, CompoundNotFound, PubChemClient, PubChemError,  # noqa: E402
                     parse_properties)
from service import lookup, refresh_all  # noqa: E402

ASPIRIN = {
    "CID": 2244, "MolecularFormula": "C9H8O4", "MolecularWeight": "180.16",
    "SMILES": "CC(=O)OC1=CC=CC=C1C(=O)O", "InChIKey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
    "XLogP": 1.2, "IUPACName": "2-acetyloxybenzoic acid",
}
# Синонимы, под которыми PubChem отдаёт то же вещество
ASPIRIN_NAMES = {"aspirin", "acetylsalicylic acid"}


def payload(props):
    return {"PropertyTable": {"Properties": [props]}}


def make_client(handler):
    """Клиент PubChem, который вместо интернета вызывает handler и запоминает URL запросов."""
    calls = []

    def recording(request):
        calls.append(str(request.url))
        return handler(request)

    http = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(recording))
    return PubChemClient(http, max_rps=1000, backoff_base=0), calls


def pubchem_fake(request):
    """Мини-PubChem: знает только аспирин по двум названиям и по CID 2244."""
    path = request.url.path
    if "/compound/name/" in path:
        name = unquote(path.split("/compound/name/")[1].split("/property")[0])
        if name in ASPIRIN_NAMES:
            return httpx.Response(200, json=payload(ASPIRIN))
        return httpx.Response(404, json={"Fault": {"Code": "PUGREST.NotFound"}})
    if "/compound/cid/2244/" in path:
        return httpx.Response(200, json=payload(ASPIRIN))
    return httpx.Response(404)


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


# --- Разбор ответа API ---

def test_parse_converts_types():
    c = parse_properties(payload(ASPIRIN))
    assert c.cid == 2244 and c.mol_weight == pytest.approx(180.16)


def test_parse_fails_loudly_on_missing_smiles():
    # Именно так выглядел ответ после переименования полей в 2025: SMILES просто нет
    broken = {k: v for k, v in ASPIRIN.items() if k != "SMILES"}
    with pytest.raises(PubChemError, match="SMILES"):
        parse_properties(payload(broken))


# --- Нормализация названий ---

@pytest.mark.parametrize("raw", ["Aspirin", "  aspirin ", "ASPIRIN", "aspirin\t"])
def test_normalize_same_key(raw):
    assert normalize_name(raw) == "aspirin"


def test_normalize_unicode_dash():
    assert normalize_name("N–acetylcysteine") == "n-acetylcysteine"


# --- Поиск, кэш, дедупликация ---

def test_lookup_api_then_cache(db):
    client, calls = make_client(pubchem_fake)
    first = asyncio.run(lookup(db, client, "Aspirin", user_id=1))
    second = asyncio.run(lookup(db, client, "  ASPIRIN ", user_id=1))
    assert first.source == "api" and second.source == "cache"
    assert len(calls) == 1  # второй раз в API не ходили


def test_synonym_does_not_create_duplicate(db):
    client, _ = make_client(pubchem_fake)
    asyncio.run(lookup(db, client, "aspirin", user_id=1))
    result = asyncio.run(lookup(db, client, "acetylsalicylic acid", user_id=2))
    assert result.known_under_other_name
    stats = db.stats()
    assert stats["compounds"] == 1 and stats["aliases"] == 2


def test_not_found_is_logged(db):
    client, _ = make_client(pubchem_fake)
    result = asyncio.run(lookup(db, client, "unobtainium", user_id=1))
    assert result.compound is None and result.source == "not_found"
    assert db.stats()["not_found"] == 1


def test_invalid_query(db):
    client, calls = make_client(pubchem_fake)
    assert asyncio.run(lookup(db, client, "   ", user_id=1)).source == "invalid"
    assert asyncio.run(lookup(db, client, "x" * 101, user_id=1)).source == "invalid"
    assert calls == []


def test_history_newest_first(db):
    client, _ = make_client(pubchem_fake)
    asyncio.run(lookup(db, client, "aspirin", user_id=7))
    asyncio.run(lookup(db, client, "unobtainium", user_id=7))
    rows = db.history(7)
    assert [r["raw_query"] for r in rows] == ["unobtainium", "aspirin"]
    assert db.history(8) == []


# --- Повторы при сбоях ---

def test_retry_after_overload():
    responses = iter([httpx.Response(503), httpx.Response(200, json=payload(ASPIRIN))])
    client, calls = make_client(lambda request: next(responses))
    assert asyncio.run(client.by_name("aspirin")).cid == 2244
    assert len(calls) == 2


def test_gives_up_after_three_failures():
    client, calls = make_client(lambda request: httpx.Response(503))
    with pytest.raises(PubChemError):
        asyncio.run(client.by_name("aspirin"))
    assert len(calls) == 3


def test_network_error_becomes_friendly_message(db):
    def broken(request):
        raise httpx.ConnectError("no network")
    client, _ = make_client(broken)
    result = asyncio.run(lookup(db, client, "aspirin", user_id=1))
    assert result.source == "error" and "позже" in result.message


def test_not_found_raises():
    client, _ = make_client(pubchem_fake)
    with pytest.raises(CompoundNotFound):
        asyncio.run(client.by_name("unobtainium"))


# --- Регулярное обновление ---

def test_refresh_records_changes(db):
    client, _ = make_client(pubchem_fake)
    asyncio.run(lookup(db, client, "aspirin", user_id=1))

    updated = {**ASPIRIN, "XLogP": 1.3}
    client2, _ = make_client(lambda request: httpx.Response(200, json=payload(updated)))
    report = asyncio.run(refresh_all(db, client2))

    assert report.checked == 1 and not report.failed
    [change] = report.changed["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"]
    assert (change.field, change.old_value, change.new_value) == ("xlogp", 1.2, 1.3)
    assert db.stats()["changes"] == 1


def test_refresh_without_changes(db):
    client, _ = make_client(pubchem_fake)
    asyncio.run(lookup(db, client, "aspirin", user_id=1))
    report = asyncio.run(refresh_all(db, client))
    assert report.changed == {} and db.stats()["changes"] == 0


def test_refresh_refuses_cid_pointing_to_other_compound(db):
    client, _ = make_client(pubchem_fake)
    asyncio.run(lookup(db, client, "aspirin", user_id=1))
    other = {**ASPIRIN, "InChIKey": "OTHERKEYXXXXXX-UHFFFAOYSA-N"}
    client2, _ = make_client(lambda request: httpx.Response(200, json=payload(other)))
    report = asyncio.run(refresh_all(db, client2))
    assert report.failed == ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"]
    assert db.get_by_alias("aspirin").inchikey == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
