"""Build the model's briefing about the database: schema, column docs, and real values.

This is BIRD's "database + external knowledge" input. The column-description CSVs explain
cryptic names and codes; values read from the data itself correct places where the docs
are wrong or incomplete (for example, dates documented as YYMMDD are stored as YYYY-MM-DD).

`inspect_database` reads from SQLite once at startup; `render_briefing` is pure.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from src.utils import get_schema, load_column_descriptions

MAX_LISTED_VALUES = 100  # list every distinct value of text columns with at most this many
NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    is_pk: bool
    label: str  # human-readable name from the docs, e.g. A3 -> "region"
    description: str
    notes: str  # value descriptions from the docs, e.g. code meanings
    values: tuple[str, ...] = ()  # every distinct value, for low-cardinality text columns
    example: str = ""  # one stored value, to show formats such as dates
    numeric_text: bool = False  # numbers stored as TEXT; needs CAST to compare


@dataclass(frozen=True)
class Table:
    name: str
    row_count: int
    columns: tuple[Column, ...]
    foreign_keys: tuple[tuple[str, str, str], ...]  # (column, ref_table, ref_column)


def build_briefing(conn: sqlite3.Connection) -> str:
    return render_briefing(inspect_database(conn, load_column_descriptions()))


def inspect_database(conn: sqlite3.Connection, docs: dict[str, list[dict[str, str]]]) -> tuple[Table, ...]:
    return tuple(
        _inspect_table(conn, table, columns, _docs_by_column(docs.get(table, [])))
        for table, columns in get_schema(conn).items()
    )


def render_briefing(tables: tuple[Table, ...]) -> str:
    return "\n\n".join(_render_table(table) for table in tables)


# --- inspection (reads the database) ---------------------------------------------------


def _inspect_table(conn, table: str, columns: list[dict], docs: dict[str, dict[str, str]]) -> Table:
    row_count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    foreign_keys = tuple(
        (fk[3], fk[2], fk[4]) for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    )
    return Table(
        name=table,
        row_count=row_count,
        columns=tuple(_inspect_column(conn, table, col, docs.get(col["name"], {})) for col in columns),
        foreign_keys=foreign_keys,
    )


def _inspect_column(conn, table: str, col: dict, doc: dict[str, str]) -> Column:
    name, col_type = col["name"], (col["type"] or "").upper()
    base = dict(
        name=name,
        type=col_type,
        is_pk=bool(col["pk"]),
        label=_clean(doc.get("column_name", "")),
        description=_clean(doc.get("column_description", "")),
        notes=_clean(doc.get("value_description", "")),
    )
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


def _docs_by_column(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {row.get("original_column_name", "").strip(): row for row in rows}


def _clean(text: str) -> str:
    """Collapse whitespace and line breaks in a docs cell into one line."""
    lines = (line.strip().rstrip(";").strip() for line in (text or "").splitlines())
    return "; ".join(line for line in lines if line).replace("：", ": ")


# --- rendering (pure) ------------------------------------------------------------------


def _render_table(table: Table) -> str:
    lines = [f'Table "{table.name}" ({table.row_count:,} rows)']
    if table.foreign_keys:
        joins = ", ".join(f"{col} -> {ref_t}.{ref_c}" for col, ref_t, ref_c in table.foreign_keys)
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
