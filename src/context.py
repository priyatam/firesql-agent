"""Build the model's briefing about the database: schema, column docs, and real values.

This is BIRD's "database + external knowledge" input. The column-description CSVs explain
cryptic names and codes; values read from the data itself correct places where the docs
are wrong or incomplete (for example, dates documented as YYMMDD are stored as YYYY-MM-DD).

`inspect_database` works on any database SQLAlchemy can reflect (see db.py): tables and views in the chosen
schemas (default: the database's default schema), with keys, row counts (estimates on PostgreSQL and MySQL),
and every distinct value of text columns with at most 100 of them. Tables are read in parallel, and each
inspection query stops after INSPECT_TIMEOUT_S, so a huge table costs its values, not the startup. `load_tables`
caches the result in .cache/ until the database file, the docs or this module change (server databases: until
--refresh-schema), so later starts take milliseconds.

Column notes come from a directory of CSVs, one per table, in the format of data/database_description/
(--docs-dir), and otherwise from comments stored in the database. `render_briefing` is pure.
"""

from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy.engine import Engine

from src import db
from src.utils import DOCS_DIR, ROOT, load_column_descriptions

MAX_LISTED_VALUES = 100  # list every distinct value of text columns with at most this many
NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
CACHE_DIR = ROOT / ".cache"
INSPECT_TIMEOUT_S = 10.0  # per inspection query; past it, a column is shown without its values
INSPECT_WORKERS = 8


class ColumnDoc(BaseModel):
    """One row of a column-description CSV."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    original_column_name: str = ""
    column_name: str = ""
    column_description: str = ""
    data_format: str = ""
    value_description: str = ""


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
    row_count: int | None  # None: a view, or a count that timed out
    columns: tuple[Column, ...]
    foreign_keys: tuple[ForeignKey, ...] = Field(default=())
    schema_name: str | None = None  # None: the database's default schema
    is_view: bool = False
    estimated: bool = False  # row_count is the database's estimate, not a COUNT(*)
    description: str = ""  # the table's comment in the database


def qualified(table: Table) -> str:
    """The name the model writes: orders, or sales.orders outside the default schema."""
    return f"{table.schema_name}.{table.name}" if table.schema_name else table.name


def cache_path(engine: Engine, cache_dir: Path = CACHE_DIR, docs_dir: Path = DOCS_DIR, schemas: tuple[str, ...] = ()) -> Path:
    return cache_dir / f"schema-{schema_key(engine, docs_dir, schemas)}.json"


def load_tables(
    engine: Engine, cache_dir: Path = CACHE_DIR, refresh: bool = False, docs_dir: Path = DOCS_DIR, schemas: tuple[str, ...] = ()
) -> tuple[Table, ...]:
    """The inspected schema, from the cache when the database, the docs and this module are unchanged."""
    adapter = TypeAdapter(tuple[Table, ...])
    path = cache_path(engine, cache_dir, docs_dir, schemas)
    if path.exists() and not refresh:
        try:
            return adapter.validate_json(path.read_bytes())
        except ValidationError:
            pass  # stale or damaged cache: rebuild it below
    tables = inspect_database(engine, load_docs(docs_dir), schemas)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(adapter.dump_json(tables))
    return tables


def schema_key(engine: Engine, docs_dir: Path = DOCS_DIR, schemas: tuple[str, ...] = ()) -> str:
    """Changes with the database (its URL and schemas, and for SQLite the file), a column-doc CSV, or this module."""
    files = sorted(docs_dir.glob("*.csv"))
    if db.dialect(engine) == "sqlite":
        files = [Path(engine.url.database or ""), *files]
    stamp = "|".join(f"{p.name}:{p.stat().st_size}:{p.stat().st_mtime_ns}" for p in files)
    code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return hashlib.sha256(f"{db.describe(engine)}|{','.join(schemas)}|{docs_dir.resolve()}|{stamp}|{code}".encode()).hexdigest()[:16]


def build_briefing(engine: Engine) -> str:
    return render_briefing(inspect_database(engine, load_docs()))


def load_docs(docs_dir: Path = DOCS_DIR) -> dict[str, tuple[ColumnDoc, ...]]:
    """Column docs per table (file stem, e.g. loan.csv or sales.orders.csv), parsed from the CSVs."""
    if not docs_dir.is_dir():
        return {}
    rows_by_table = load_column_descriptions(str(docs_dir))
    return {table: tuple(ColumnDoc.model_validate(row) for row in rows) for table, rows in rows_by_table.items()}


def inspect_database(engine: Engine, docs: dict[str, tuple[ColumnDoc, ...]], schemas: tuple[str, ...] = ()) -> tuple[Table, ...]:
    """Every table and view in the given schemas (default: the default schema), read in parallel."""
    inspector = sa.inspect(engine)
    default = inspector.default_schema_name
    targets = [
        (schema, name, is_view)
        for schema in dict.fromkeys(None if s == default else s for s in schemas or (None,))
        for is_view, names in ((False, inspector.get_table_names(schema=schema)), (True, inspector.get_view_names(schema=schema)))
        for name in sorted(names)
    ]

    def one(target: tuple[str | None, str, bool]) -> Table:
        schema, name, is_view = target
        docs_for = docs.get(f"{schema}.{name}", ()) or docs.get(name, ()) if schema else docs.get(name, ())
        return _inspect_table(engine, schema, name, is_view, _docs_by_column(docs_for))

    with ThreadPoolExecutor(INSPECT_WORKERS) as pool:
        return tuple(pool.map(one, targets))


def render_briefing(tables: tuple[Table, ...]) -> str:
    return "\n\n".join(render_table(table) for table in tables)


def render_outline(tables: tuple[Table, ...]) -> str:
    """Compact schema: table names, row counts, joins and column names with types. No notes or values."""
    lines = []
    for t in tables:
        cols = ", ".join(f"{c.name} {c.type}".strip() for c in t.columns)
        joins = "; joins " + ", ".join(f"{fk.column} -> {fk.ref_table}.{fk.ref_column}" for fk in t.foreign_keys) if t.foreign_keys else ""
        lines.append(f'"{qualified(t)}" ({_rows(t)}): {cols}{joins}')
    return "\n".join(lines)


# --- inspection (reads the database) ---------------------------------------------------


def _inspect_table(engine: Engine, schema: str | None, name: str, is_view: bool, docs: dict[str, ColumnDoc]) -> Table:
    inspector = sa.inspect(engine)  # one per thread
    columns = inspector.get_columns(name, schema=schema)
    table = sa.table(name, *(sa.column(c["name"]) for c in columns), schema=schema)
    pk = set() if is_view else set(inspector.get_pk_constraint(name, schema=schema).get("constrained_columns") or ())
    foreign_keys = () if is_view else tuple(
        ForeignKey(column=col, ref_table=_name(fk.get("referred_schema"), fk["referred_table"], inspector.default_schema_name), ref_column=ref)
        for fk in inspector.get_foreign_keys(name, schema=schema)
        for col, ref in zip(fk["constrained_columns"], fk["referred_columns"])
    )
    row_count, estimated = (None, False) if is_view else _row_count(engine, table)
    return Table(
        name=name,
        schema_name=schema,
        is_view=is_view,
        row_count=row_count,
        estimated=estimated,
        description=_clean(_table_comment(inspector, name, schema)),
        columns=tuple(
            _inspect_column(engine, table, c, c["name"] in pk, docs.get(c["name"], ColumnDoc())) for c in columns
        ),
        foreign_keys=foreign_keys,
    )


def _name(schema: str | None, table: str, default: str | None) -> str:
    return f"{schema}.{table}" if schema and schema != default else table


def _row_count(engine: Engine, table: Any) -> tuple[int | None, bool]:
    estimate = db.row_estimate(engine, table.name, table.schema)
    if estimate is not None and estimate > 0:
        return estimate, True
    try:
        return db.query(engine, sa.select(sa.func.count()).select_from(table), timeout_s=INSPECT_TIMEOUT_S, max_rows=1)[1][0][0], False
    except db.QueryError:
        return None, False


def _table_comment(inspector: Any, name: str, schema: str | None) -> str:
    try:
        return inspector.get_table_comment(name, schema=schema).get("text") or ""
    except NotImplementedError:  # SQLite keeps no comments
        return ""


def _inspect_column(engine: Engine, table: Any, info: dict, is_pk: bool, doc: ColumnDoc) -> Column:
    sa_type, col = info["type"], table.c[info["name"]]
    base = {
        "name": info["name"],
        "type": str(sa_type).upper(),
        "is_pk": is_pk,
        "label": _clean(doc.column_name),
        "description": _clean(doc.column_description or info.get("comment") or ""),
        "notes": _clean(doc.value_description),
    }
    values = lambda stmt, n: _values(engine, stmt.where(col.is_not(None)).limit(n), n)
    if isinstance(sa_type, (sa.Date, sa.DateTime)):
        # Show the real stored format; the docs' "YYMMDD" note is wrong for this data.
        return Column(**{**base, "notes": ""}, example=_first(values(sa.select(col), 1)))
    if not isinstance(sa_type, sa.String):
        return Column(**base)

    sample = values(sa.select(col), 50)
    if sample and all(NUMBER_RE.match(str(v).strip()) for v in sample):
        return Column(**base, numeric_text=True, example=str(sample[0]))

    distinct = values(sa.select(col).distinct(), MAX_LISTED_VALUES + 1)  # stops at 101 on high-cardinality columns
    if 0 < len(distinct) <= MAX_LISTED_VALUES:
        return Column(**base, values=tuple(sorted(distinct, key=str)))
    return Column(**base, example=_first(sample))


def _values(engine: Engine, stmt: Any, n: int) -> list:
    try:
        return [r[0] for r in db.query(engine, stmt, timeout_s=INSPECT_TIMEOUT_S, max_rows=n)[1]]
    except db.QueryError:
        return []  # too slow on a huge table: show the column without values


def _first(values: list) -> str:
    return str(values[0]) if values else ""


def _rows(table: Table) -> str:
    if table.is_view:
        return "view"
    if table.row_count is None:
        return "rows not counted"
    return f"{'about ' if table.estimated else ''}{table.row_count:,} rows"


def _docs_by_column(rows: tuple[ColumnDoc, ...]) -> dict[str, ColumnDoc]:
    return {row.original_column_name.strip(): row for row in rows}


def _clean(text: str) -> str:
    """Collapse whitespace and line breaks in a docs cell into one line."""
    lines = (line.strip().rstrip(";").strip() for line in (text or "").splitlines())
    return "; ".join(line for line in lines if line).replace("：", ": ")


# --- rendering (pure) ------------------------------------------------------------------


def render_table(table: Table) -> str:
    name = ".".join(f'"{part}"' for part in qualified(table).split("."))
    lines = [f"{'View' if table.is_view else 'Table'} {name} ({_rows(table)})" + (f": {table.description}" if table.description else "")]
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
