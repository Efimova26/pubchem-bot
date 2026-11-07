"""База SQLite: одно вещество = одна строка, сколько бы названий у него ни было."""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from pubchem import Compound

SCHEMA = """
-- Ключ вещества — InChIKey: у аспирина и ацетилсалициловой кислоты он один и тот же
CREATE TABLE IF NOT EXISTS compounds (
    inchikey    TEXT PRIMARY KEY,
    cid         INTEGER NOT NULL,
    formula     TEXT NOT NULL,
    mol_weight  REAL NOT NULL,
    smiles      TEXT NOT NULL,
    xlogp       REAL,
    iupac_name  TEXT,
    first_seen  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Все названия, под которыми вещество искали. Это и кэш: знакомое имя не идёт в API
CREATE TABLE IF NOT EXISTS aliases (
    alias     TEXT PRIMARY KEY,
    inchikey  TEXT NOT NULL REFERENCES compounds(inchikey)
);

-- Журнал запросов пользователей: откуда взят ответ (api / cache) или почему его нет
CREATE TABLE IF NOT EXISTS queries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    raw_query   TEXT NOT NULL,
    inchikey    TEXT REFERENCES compounds(inchikey),
    status      TEXT NOT NULL CHECK (status IN ('api', 'cache', 'not_found', 'error')),
    created_at  TEXT NOT NULL
);

-- Что поменялось в PubChem при регулярном обновлении
CREATE TABLE IF NOT EXISTS changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    inchikey     TEXT NOT NULL REFERENCES compounds(inchikey),
    field        TEXT NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    detected_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_queries_user ON queries(user_id, created_at);
"""

TRACKED_FIELDS = ["cid", "formula", "mol_weight", "smiles", "xlogp", "iupac_name"]

# Разные виды тире из статей и копипасты (‐ ‑ ‒ – — −) приводим к обычному дефису
DASHES = re.compile(r"[\u2010-\u2015\u2212]")


def normalize_name(name: str) -> str:
    """Один и тот же запрос, написанный по-разному, должен давать один ключ."""
    text = unicodedata.normalize("NFKC", name)
    text = DASHES.sub("-", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Change:
    field: str
    old_value: object
    new_value: object


@dataclass(frozen=True)
class SaveResult:
    is_new: bool              # вещества раньше не было в базе
    known_under_other_name: bool  # вещество было, но искали его под другим названием
    changes: list[Change]


class Database:
    def __init__(self, path: str = "compounds.db") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def get_by_alias(self, alias: str) -> Compound | None:
        row = self.conn.execute(
            """SELECT c.* FROM aliases a JOIN compounds c ON c.inchikey = a.inchikey
               WHERE a.alias = ?""",
            (alias,),
        ).fetchone()
        return _row_to_compound(row) if row else None

    def save(self, compound: Compound, alias: str | None = None) -> SaveResult:
        """Вставляет вещество или обновляет его, записывая, какие поля изменились."""
        with self.conn:  # одна транзакция: либо всё записалось, либо ничего
            existing = self.conn.execute(
                "SELECT * FROM compounds WHERE inchikey = ?", (compound.inchikey,)
            ).fetchone()
            new_values = asdict(compound)
            timestamp = now()

            if existing is None:
                self.conn.execute(
                    """INSERT INTO compounds (inchikey, cid, formula, mol_weight, smiles,
                       xlogp, iupac_name, first_seen, updated_at)
                       VALUES (:inchikey, :cid, :formula, :mol_weight, :smiles,
                       :xlogp, :iupac_name, :ts, :ts)""",
                    {**new_values, "ts": timestamp},
                )
                changes: list[Change] = []
            else:
                changes = [
                    Change(f, existing[f], new_values[f])
                    for f in TRACKED_FIELDS
                    if existing[f] != new_values[f]
                ]
                for change in changes:
                    self.conn.execute(
                        """INSERT INTO changes (inchikey, field, old_value, new_value, detected_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (compound.inchikey, change.field, str(change.old_value),
                         str(change.new_value), timestamp),
                    )
                self.conn.execute(
                    """UPDATE compounds SET cid = :cid, formula = :formula,
                       mol_weight = :mol_weight, smiles = :smiles, xlogp = :xlogp,
                       iupac_name = :iupac_name, updated_at = :ts
                       WHERE inchikey = :inchikey""",
                    {**new_values, "ts": timestamp},
                )

            if alias:
                self.conn.execute(
                    "INSERT OR IGNORE INTO aliases (alias, inchikey) VALUES (?, ?)",
                    (alias, compound.inchikey),
                )

        return SaveResult(
            is_new=existing is None,
            known_under_other_name=existing is not None and alias is not None,
            changes=changes,
        )

    def log_query(self, user_id: int, raw_query: str, status: str,
                  inchikey: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO queries (user_id, raw_query, inchikey, status, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, raw_query, inchikey, status, now()),
            )

    def history(self, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT q.raw_query, q.status, q.created_at, c.formula, c.cid
               FROM queries q LEFT JOIN compounds c ON c.inchikey = q.inchikey
               WHERE q.user_id = ? ORDER BY q.id DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()

    def all_compounds(self) -> list[tuple[str, int]]:
        return [(r["inchikey"], r["cid"]) for r in
                self.conn.execute("SELECT inchikey, cid FROM compounds").fetchall()]

    def stats(self) -> dict[str, int]:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "compounds": one("SELECT COUNT(*) FROM compounds"),
            "aliases": one("SELECT COUNT(*) FROM aliases"),
            "queries": one("SELECT COUNT(*) FROM queries"),
            "cache_hits": one("SELECT COUNT(*) FROM queries WHERE status = 'cache'"),
            "api_calls": one("SELECT COUNT(*) FROM queries WHERE status = 'api'"),
            "not_found": one("SELECT COUNT(*) FROM queries WHERE status = 'not_found'"),
            "changes": one("SELECT COUNT(*) FROM changes"),
        }

    def close(self) -> None:
        self.conn.close()


def _row_to_compound(row: sqlite3.Row) -> Compound:
    return Compound(
        cid=row["cid"], inchikey=row["inchikey"], formula=row["formula"],
        mol_weight=row["mol_weight"], smiles=row["smiles"], xlogp=row["xlogp"],
        iupac_name=row["iupac_name"],
    )
