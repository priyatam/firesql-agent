from src.eval import extract_sql, rows_match
from src.golden import DraftQuestion, dedupe, sql_tier, unique_names


def test_rows_match_ignores_names_order_and_float_noise():
    assert rows_match([(554.0,)], [(554,)], ordered=False)
    assert rows_match([(1, "a"), (2, "b")], [(2, "b"), (1, "a")], ordered=False)
    assert not rows_match([(1,)], [(1,), (2,)], ordered=False)


def test_rows_match_allows_ties_in_any_order_but_not_real_reorders():
    gold = [("A", 128), ("B", 53), ("C", 53), ("D", 10)]
    assert rows_match([("A", 128), ("C", 53), ("B", 53), ("D", 10)], gold, ordered=True)
    assert not rows_match([("B", 53), ("A", 128), ("C", 53), ("D", 10)], gold, ordered=True)


def test_extract_sql_from_free_text():
    assert extract_sql("Here:\n```sql\nSELECT 1\n```") == "SELECT 1"
    assert extract_sql("Use SELECT 2 FROM t\n\nThat works.") == "SELECT 2 FROM t"


def test_sql_tier_rule():
    assert sql_tier("SELECT COUNT(*) FROM loan") == 1
    assert sql_tier("SELECT COUNT(*) FROM a JOIN b ON a.x=b.x") == 2
    assert sql_tier("SELECT d.A2, COUNT(*) FROM a JOIN d ON a.x=d.x GROUP BY 1") == 3


def test_dedupe_drops_near_duplicates_and_dev_questions():
    drafts = [
        DraftQuestion(question="How many bank accounts are located in the Prague region?", tier=1),  # dev q_001
        DraftQuestion(question="What is the average loan amount?", tier=1),
        DraftQuestion(question="What is the average loan amount ?", tier=1),
    ]
    assert [d.question for d in dedupe(drafts)] == ["What is the average loan amount?"]


def test_unique_names_keeps_every_column():
    assert unique_names(("A2", "n", "A2")) == ["A2", "n", "A2_2"]


def test_verify_keeps_a_question_only_when_two_samples_agree(monkeypatch):
    from src import golden
    from src.fireworks import Usage

    one = Usage(prompt_tokens=10, completion_tokens=5)
    a = golden.SqlSample(sql="SELECT 1", columns=("x",), rows=((1,),))
    b = golden.SqlSample(sql="SELECT 2", columns=("x",), rows=((2,),))
    draft = DraftQuestion(question="q?", tier=1)

    samples = iter([(a, one), (b, one), (a, one)])
    monkeypatch.setattr(golden, "sample_sql", lambda *args: next(samples))
    kept, used = golden.verify(draft, "briefing", "key")
    assert kept.sql == "SELECT 1" and kept.agreement == "2/3" and used == Usage(prompt_tokens=30, completion_tokens=15)

    samples = iter([(a, one), (b, one), (None, one)])
    kept, used = golden.verify(draft, "briefing", "key")
    assert kept is None and used.completion_tokens == 15


def test_followup_set_gold_queries_run_and_return_rows():
    from contextlib import closing

    from src.chat import connect_readonly
    from src.eval import gold_rows, load_conversations

    conversations = load_conversations()
    assert len(conversations) >= 15 and all(len(c.turns) >= 3 for c in conversations)
    with closing(connect_readonly()) as conn:
        assert all(gold_rows(conn, t.gold_sql) for c in conversations for t in c.turns)


def test_summary_scores_turns_and_whole_conversations():
    from src.eval import Record, summarize

    def rec(qid, turn, correct):
        return Record(mode="agent", id=qid, tier=3, turn=turn, sql="", columns=[], answer="", error=None, correct=correct,
                      attempts=1, tool_calls=0, prompt_tokens=0, cached_tokens=0, completion_tokens=0, cost_usd=0.0,
                      llm_s=0.0, sql_s=0.0, total_s=1.0)

    s = summarize([rec("f01.1", 1, True), rec("f01.2", 2, True), rec("f02.1", 1, True), rec("f02.2", 2, False)], "m")
    assert s.by_turn == {"1": "2/2", "2": "1/2"} and s.conversations == "1/2"


def test_only_a_top_level_order_by_makes_order_matter():
    from src.eval import sorts_result

    assert sorts_result("SELECT a FROM t ORDER BY a DESC LIMIT 5")
    assert not sorts_result("SELECT a FROM t WHERE id IN (SELECT id FROM u ORDER BY n DESC LIMIT 5)")
    assert sorts_result("SELECT a FROM (SELECT a FROM t ORDER BY a) ORDER BY a")
    assert not sorts_result("SELECT COUNT(*) FROM (SELECT x FROM (SELECT x FROM t ORDER BY x) LIMIT 3)")
