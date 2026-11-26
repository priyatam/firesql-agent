"""Build the model's briefing about the database: schema, column docs, and real values.

This is BIRD's "database + external knowledge" input. The column-description CSVs explain
cryptic names and codes; values read from the data itself correct places where the docs
are wrong or incomplete (for example, dates documented as YYMMDD are stored as YYYY-MM-DD).

`inspect_database` reads from SQLite (about 3.5s: it counts values on every column, including the 1M-row
`trans` table). `load_tables` caches its result in .cache/ until the database, the docs or this file change,
so later starts take milliseconds. `render_briefing` is pure.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from src.utils import DB_PATH, DOCS_DIR, ROOT, get_schema, load_column_descriptions

MAX_LISTED_VALUES = 100  # list every distinct value of text columns with at most this many
NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
CACHE_DIR = ROOT / ".cache"


class ColumnDoc(BaseModel):
    """One row of a column-description CSV."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    original_column_name: str = ""
    column_name: str = ""
    column_description: str = ""
    data_format: str = ""
    value_description: str = ""


class SchemaColumn(BaseModel):
    """One row of SQLite's PRAGMA table_info."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    name: str
    type: str | None = ""
    pk: int = 0


class ForeignKey(BaseModel):
    model_config = ConfigDict(frozen=True)
    column: str
    ref_table: str
    ref_column: str


class Column(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    type: str
    is_pk: bool
    label: str  # human-readable name from the docs, e.g. A3 -> "region"
    description: str
    notes: str  # value descriptions from the docs, e.g. code meanings
    values: tuple[str, ...] = ()  # every distinct value, for low-cardinality text columns
    example: str = ""  # one stored value, to show formats such as dates
    numeric_text: bool = False  # numbers stored as TEXT; needs CAST to compare


class Table(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    row_count: int
    columns: tuple[Column, ...]
    foreign_keys: tuple[ForeignKey, ...] = Field(default=())


def cache_path(db_path: Path = DB_PATH, cache_dir: Path = CACHE_DIR) -> Path:
    return cache_dir / f"schema-{schema_key(Path(db_path))}.json"


def load_tables(conn: sqlite3.Connection, db_path: Path = DB_PATH, cache_dir: Path = CACHE_DIR) -> tuple[Table, ...]:
    """The inspected schema, from the cache when the database, the docs and this module are unchanged."""
    adapter = TypeAdapter(tuple[Table, ...])
    path = cache_path(db_path, cache_dir)
    if path.exists():
        try:
            return adapter.validate_json(path.read_bytes())
        except ValidationError:
            pass  # stale or damaged cache: rebuild it below
    tables = inspect_database(conn, load_docs())
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(adapter.dump_json(tables))
    return tables


def schema_key(db_path: Path) -> str:
    """Changes when the database, a column-doc CSV, or the inspection code changes."""
    files = [db_path, *sorted(DOCS_DIR.glob("*.csv"))]
    stamp = "|".join(f"{p.name}:{p.stat().st_size}:{p.stat().st_mtime_ns}" for p in files)
    code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return hashlib.sha256(f"{db_path.resolve()}|{stamp}|{code}".encode()).hexdigest()[:16]


def build_briefing(conn: sqlite3.Connection) -> str:
    return render_briefing(inspect_database(conn, load_docs()))


def load_docs() -> dict[str, tuple[ColumnDoc, ...]]:
    """Column docs per table, parsed from the CSVs."""
    rows_by_table = load_column_descriptions(str(DOCS_DIR))
    return {table: tuple(ColumnDoc.model_validate(row) for row in rows) for table, rows in rows_by_table.items()}


def inspect_database(conn: sqlite3.Connection, docs: dict[str, tuple[ColumnDoc, ...]]) -> tuple[Table, ...]:
    return tuple(
        _inspect_table(conn, table, tuple(SchemaColumn.model_validate(c) for c in columns), _docs_by_column(docs.get(table, ())))
        for table, columns in get_schema(conn).items()
    )


def render_briefing(tables: tuple[Table, ...]) -> str:
    return "\n\n".join(render_table(table) for table in tables)


def render_outline(tables: tuple[Table, ...]) -> str:
    """Compact schema: table names, row counts, joins and column names with types. No notes or values."""
    lines = []
    for t in tables:
        cols = ", ".join(f"{c.name} {c.type}".strip() for c in t.columns)
        joins = "; joins " + ", ".join(f"{fk.column} -> {fk.ref_table}.{fk.ref_column}" for fk in t.foreign_keys) if t.foreign_keys else ""
        lines.append(f'"{t.name}" ({t.row_count:,} rows): {cols}{joins}')
    return "\n".join(lines)


# --- inspection (reads the database) ---------------------------------------------------


def _inspect_table(conn, table: str, columns: tuple[SchemaColumn, ...], docs: dict[str, ColumnDoc]) -> Table:
    row_count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    foreign_keys = tuple(
        ForeignKey(column=fk[3], ref_table=fk[2], ref_column=fk[4]) for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    )
    return Table(
        name=table,
        row_count=row_count,
        columns=tuple(_inspect_column(conn, table, col, docs.get(col.name, ColumnDoc())) for col in columns),
        foreign_keys=foreign_keys,
    )


def _inspect_column(conn, table: str, col: SchemaColumn, doc: ColumnDoc) -> Column:
    name, col_type = col.name, (col.type or "").upper()
    base = {
        "name": name,
        "type": col_type,
        "is_pk": bool(col.pk),
        "label": _clean(doc.column_name),
        "description": _clean(doc.column_description),
        "notes": _clean(doc.value_description),
    }
    if col_type == "DATE":
        # Show the real stored format; the docs' "YYMMDD" note is wrong for this data.
        return Column(**{**base, "notes": ""}, example=_first_value(conn, table, name))
    if col_type != "TEXT":
        return Column(**base)

    sample = [r[0] for r in conn.execute(f'SELECT "{name}" FROM "{table}" WHERE "{name}" IS NOT NULL LIMIT 50')]
    if sample and all(NUMBER_RE.match(str(v).strip()) for v in sample):
        return Column(**base, numeric_text=True, example=str(sample[0]))

    distinct = conn.execute(f'SELECT COUNT(DISTINCT "{name}") FROM "{table}"').fetchone()[0]
    if distinct <= MAX_LISTED_VALUES:
        values = tuple(
            r[0]
            for r in conn.execute(f'SELECT DISTINCT "{name}" FROM "{table}" WHERE "{name}" IS NOT NULL ORDER BY 1')
        )
        return Column(**base, values=values)
    return Column(**base, example=_first_value(conn, table, name))


def _first_value(conn, table: str, column: str) -> str:
    row = conn.execute(f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT 1').fetchone()
    return "" if row is None else str(row[0])


def _docs_by_column(rows: tuple[ColumnDoc, ...]) -> dict[str, ColumnDoc]:
    return {row.original_column_name.strip(): row for row in rows}


def _clean(text: str) -> str:
    """Collapse whitespace and line breaks in a docs cell into one line."""
    lines = (line.strip().rstrip(";").strip() for line in (text or "").splitlines())
    return "; ".join(line for line in lines if line).replace("：", ": ")


# --- rendering (pure) ------------------------------------------------------------------


def render_table(table: Table) -> str:
    lines = [f'Table "{table.name}" ({table.row_count:,} rows)']
    if table.foreign_keys:
        joins = ", ".join(f"{fk.column} -> {fk.ref_table}.{fk.ref_column}" for fk in table.foreign_keys)
        lines.append(f"  joins: {joins}")
    lines.extend(f"  - {_render_column(col)}" for col in table.columns)
    return "\n".join(lines)


def _render_column(col: Column) -> str:
    head = f"{col.name} {col.type}{' PRIMARY KEY' if col.is_pk else ''}"
    meaning = "; ".join(_unique([col.label, col.description]))
    parts = [head + (f": {meaning}" if meaning else "")]
    if col.notes:
        parts.append(f"notes: {col.notes}")
    if col.values:
        listed = ", ".join(repr(v) for v in col.values)
        parts.append(f"all values: {listed}")
    if col.numeric_text:
        parts.append(f"numbers stored as TEXT (e.g. {col.example!r}); CAST before comparing or doing math")
    elif col.example:
        parts.append(f"stored like {col.example!r}")
    return " | ".join(parts)


def _unique(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item and item.lower() not in (s.lower() for s in seen):
            seen.append(item)
    return seen
