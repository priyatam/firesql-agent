from rich.console import Console

from src.chat import Turn
from src.cli import format_table, render_turn


def text(renderable) -> str:
    console = Console(width=100, record=True, color_system=None)
    console.print(renderable)
    return console.export_text()


def test_table_caps_rows_and_shows_nulls():
    out = text(format_table(("id", "name"), tuple((i, None if i == 0 else f"n{i}") for i in range(30))))
    assert "NULL" in out and "n19" in out and "n20" not in out and "30 rows · 10 more not shown" in out


def test_turn_shows_sql_error_and_stats():
    out = text(render_turn(Turn(question="q", sql="SELECT nope FROM loan", error="no such column: nope", total_s=1.2), "a/b/glm"))
    assert "SELECT nope FROM loan" in out and "Error: no such column: nope" in out and "1.20s total" in out and "glm" in out


def test_declined_turn_renders_as_a_note_without_sql():
    from rich.console import Console

    from src.chat import NOT_A_QUESTION, Turn
    from src.cli import render_turn

    console = Console(record=True, width=100)
    console.print(render_turn(Turn(question="zz", sql="", error=f"{NOT_A_QUESTION}: gibberish", declined=True), "m"))
    out = console.export_text()
    assert NOT_A_QUESTION in out and "SQL" not in out.split("\n")[1] and "Error:" not in out
