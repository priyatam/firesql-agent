from types import SimpleNamespace

import pytest

from src import db
from src.agent import answer_with_agent, build_agent
from src.chat import Config, SQLReply
from src.cli import parse_args
from src.context import inspect_database, load_docs, render_outline


@pytest.fixture(scope="module")
def schema():
    engine = db.connect()
    yield engine, inspect_database(engine, load_docs())


def fake_call(sql: str):
    return lambda agent, question: SimpleNamespace(structured_output=SQLReply(reasoning="", sql=sql))


def test_agent_turn_runs_the_final_sql_itself(schema):
    engine, tables = schema
    agent = build_agent(tables, engine, Config(mode="agent"), "test-key")
    turn = answer_with_agent(agent, "How many loans?", engine=engine, cfg=Config(), call=fake_call("SELECT COUNT(*) FROM loan"))
    assert turn.ok and turn.rows == ((682,),)


def test_agent_turn_reports_unsafe_or_missing_answers(schema):
    engine, tables = schema
    agent = build_agent(tables, engine, Config(mode="agent"), "test-key")
    unsafe = answer_with_agent(agent, "q", engine=engine, cfg=Config(), call=fake_call("DELETE FROM loan"))
    missing = answer_with_agent(agent, "q", engine=engine, cfg=Config(),
                                call=lambda a, q: SimpleNamespace(structured_output=None))
    assert not unsafe.ok and not missing.ok


def test_outline_names_every_table_without_notes(schema):
    _, tables = schema
    outline = render_outline(tables)
    assert all(f'"{t.name}"' in outline for t in tables) and "all values" not in outline


def test_cli_mode_flag():
    assert parse_args(["--agent"]).mode == "agent"
    assert parse_args(["--chat"]).mode == "chat"
    assert parse_args([]).mode in ("chat", "agent")
    with pytest.raises(SystemExit):
        parse_args(["--chat", "--agent"])  # one mode at a time
    with pytest.raises(SystemExit):
        parse_args(["--mode", "agent"])  # the old flag is gone


def test_followup_prompt_shows_previous_rows_only_when_useful():
    from src.agent import followup_prompt
    from src.chat import Turn

    prev = Turn(question="top districts", sql="SELECT 1", columns=("A2", "n"), rows=(("Praha", 663), ("Ostrava", 180), ("Karvina", 169)))
    prompt = followup_prompt("How many accounts are in the third one?", (prev,), max_rows=2)
    assert "A2 | n\nPraha | 663\nOstrava | 180" in prompt and "Karvina" not in prompt
    assert "first 2 of 3 rows" in prompt and prompt.endswith("Question: How many accounts are in the third one?")
    assert followup_prompt("q", (prev,), max_rows=0) == "q"
    assert followup_prompt("q", (prev.model_copy(update={"error": "boom"}),), max_rows=5) == "q"
    assert followup_prompt("q", (), max_rows=5) == "q"


def test_agent_follow_up_receives_previous_result(schema):
    from src.chat import Turn

    engine, tables = schema
    seen = []
    call = lambda agent, prompt: (seen.append(prompt), fake_call("SELECT 1")(agent, prompt))[1]
    agent = build_agent(tables, engine, Config(mode="agent"), "test-key")
    prev = Turn(question="q1", sql="SELECT 1", columns=("x",), rows=((7,),))
    turn = answer_with_agent(agent, "and the next?", history=(prev,), engine=engine, cfg=Config(), call=call)
    assert turn.ok and turn.question == "and the next?" and "x\n7" in seen[0]


def test_schema_cache_returns_the_same_tables(schema, tmp_path):
    from src.context import load_tables

    engine, tables = schema
    first = load_tables(engine, cache_dir=tmp_path)
    assert first == tables and len(list(tmp_path.glob("schema-*.json"))) == 1
    (tmp_path / next(tmp_path.glob("schema-*.json")).name).write_text("not json")
    assert load_tables(engine, cache_dir=tmp_path) == tables  # a damaged cache is rebuilt


def test_small_schema_puts_full_briefing_in_the_prompt(schema):
    from src import agent

    _, tables = schema
    assert "all values" in agent.schema_for_prompt(tables)
    big = agent.MAX_BRIEFING_CHARS
    try:
        agent.MAX_BRIEFING_CHARS = 10
        assert "all values" not in agent.schema_for_prompt(tables)
    finally:
        agent.MAX_BRIEFING_CHARS = big


def test_agent_declines_a_message_that_is_not_a_question(schema):
    engine, tables = schema
    agent = build_agent(tables, engine, Config(mode="agent"), "test-key")
    call = lambda a, q: SimpleNamespace(structured_output=SQLReply(reasoning="Not a question.", sql=""))
    turn = answer_with_agent(agent, "asdkj qwpeoi", engine=engine, cfg=Config(), call=call)
    assert turn.declined and not turn.ok and turn.sql == "" and turn.rows == ()
    assert "Not a question" in turn.error
