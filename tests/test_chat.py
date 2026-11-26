import pytest

from src.chat import ReplyError, SQLReply, Turn, build_messages, guard_sql, parse_reply
from src.fireworks import Completion, FireworksError, Usage, _parse_completion


def test_guard_accepts_one_select_and_with():
    assert guard_sql("SELECT 1;") == "SELECT 1"
    assert guard_sql("```sql\nWITH t AS (SELECT 1) SELECT * FROM t\n```").startswith("WITH")


@pytest.mark.parametrize("sql", ["DELETE FROM account", "SELECT 1; DROP TABLE account", "", "PRAGMA table_info(x)"])
def test_guard_rejects_writes_and_multiple_statements(sql):
    with pytest.raises(ReplyError):
        guard_sql(sql)


def test_guard_ignores_keywords_and_semicolons_inside_strings():
    assert guard_sql("SELECT * FROM t WHERE note = 'update needed'")
    assert guard_sql("SELECT 'a;b', 'it''s'") == "SELECT 'a;b', 'it''s'"
    with pytest.raises(ReplyError):
        guard_sql("SELECT 'x'; DELETE FROM t")


def test_parse_reply_rejects_cut_off_and_bad_json():
    ok = Completion(content='{"reasoning": "", "sql": "SELECT 1"}', finish_reason="stop", usage=Usage(), latency_s=0)
    assert parse_reply(ok).sql == "SELECT 1"
    with pytest.raises(ReplyError):
        parse_reply(ok.model_copy(update={"finish_reason": "length"}))
    with pytest.raises(ReplyError):
        parse_reply(ok.model_copy(update={"content": "not json"}))


def test_history_keeps_only_successful_turns_after_the_briefing():
    good, bad = Turn(question="q1", sql="SELECT 1"), Turn(question="q2", sql="", error="boom")
    messages = build_messages("BRIEF", (good, bad), "q3")
    assert [m.role for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[0].content.endswith("BRIEF") and messages[-1].content == "q3"
    assert SQLReply.model_validate_json(messages[2].content).sql == "SELECT 1"


def test_parse_completion_reads_usage_and_rejects_bad_shapes():
    c = _parse_completion({"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 3}}}, 0.1)
    assert c.usage == Usage(prompt_tokens=5, completion_tokens=2, cached_tokens=3)
    with pytest.raises(FireworksError):
        _parse_completion({"error": "x"}, 0)


def test_empty_sql_reply_is_declined_without_running_or_repairing(monkeypatch):
    from src import chat, fireworks

    calls = []

    def fake_chat(messages, **kw):
        calls.append(messages)
        return Completion(content='{"reasoning": "That is gibberish.", "sql": ""}', finish_reason="stop", usage=Usage(), latency_s=0)

    monkeypatch.setattr(fireworks, "chat", fake_chat)
    monkeypatch.setattr(chat.db, "query", lambda *a, **k: (_ for _ in ()).throw(AssertionError("SQL must not run")))
    prev = chat.Turn(question="How many accounts?", sql="SELECT COUNT(*) FROM account", columns=("n",), rows=((4500,),))
    turn = chat.answer("asdkj qwpeoi", history=(prev,), engine=None, briefing="b", cfg=chat.Config(), api_key="k")
    assert turn.declined and not turn.ok and turn.sql == "" and turn.rows == ()
    assert "gibberish" in turn.error and len(calls) == 1
