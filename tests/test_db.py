import pytest

from src import db
from src.agent import SYSTEM_PROMPT as AGENT_PROMPT
from src.chat import SYSTEM_PROMPT, ReplyError, for_dialect, guard_sql
from src.cli import parse_args


def test_sqlite_is_read_only_and_time_limited():
    engine = db.connect()
    assert db.query(engine, "SELECT COUNT(*) FROM \"order\"", timeout_s=5, max_rows=1)[1] == ((6471,),)
    assert db.query(engine, "SELECT strftime('%Y', date) FROM loan LIMIT 1", timeout_s=5, max_rows=1)[1] == (("1994",),)
    with pytest.raises(db.QueryError, match="readonly"):
        db.query(engine, "DELETE FROM loan", timeout_s=5, max_rows=1)
    with pytest.raises(db.QueryError, match="stopped"):
        db.query(engine, "SELECT COUNT(*) FROM trans a, trans b", timeout_s=0.5, max_rows=1)


def test_connect_fails_loudly(tmp_path):
    with pytest.raises(SystemExit, match="No SQLite database"):
        db.connect(f"sqlite:///{tmp_path}/missing.sqlite")
    with pytest.raises(SystemExit, match="No SQLAlchemy driver"):
        db.connect("oracle://u@h/d")  # other databases connect once their driver is installed
    with pytest.raises(SystemExit, match="Could not connect"):
        db.ping(db.connect("postgresql+psycopg://reader@127.0.0.1:1/bank?connect_timeout=1"))


def test_server_urls_hide_passwords_and_build_without_connecting():
    engine = db.connect("postgresql+psycopg://reader:secret@localhost:5432/bank")
    assert db.dialect(engine) == "postgresql" and "secret" not in db.describe(engine)
    assert db.quote(engine, "order") == '"order"'
    assert db.quote(db.connect("mysql+pymysql://reader@localhost/bank"), "order") == "`order`"
    assert db.dialect(db.connect("mariadb+pymysql://reader@localhost/bank")) == "mariadb"


@pytest.mark.parametrize("dialect", ["postgresql", "mysql", "mariadb"])
def test_prompts_name_the_connected_database(dialect):
    for prompt in (SYSTEM_PROMPT, AGENT_PROMPT):
        adapted = for_dialect(prompt, dialect)
        assert "SQLite" not in adapted and "strftime" not in adapted


def test_sqlite_prompts_are_unchanged_and_other_databases_get_generic_rules():
    assert for_dialect(SYSTEM_PROMPT, "sqlite") == SYSTEM_PROMPT
    other = for_dialect(SYSTEM_PROMPT, "mssql")
    assert "SQLite" not in other and "strftime" not in other and "mssql dialect" in other
    import sqlalchemy as sa
    assert db.is_guarded(db.connect()) and not db.is_guarded(sa.create_mock_engine("mssql://", executor=None))


def test_guard_blocks_select_into():
    with pytest.raises(ReplyError):
        guard_sql("SELECT * INTO backup FROM loan")


def test_views_docs_dir_and_comments(tmp_path):
    import sqlite3

    from src.context import inspect_database, load_docs, render_briefing

    path = tmp_path / "shop.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT);
            INSERT INTO orders (status) VALUES ('A'), ('B'), ('A');
            CREATE VIEW open_orders AS SELECT id FROM orders WHERE status = 'A';
        """)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "orders.csv").write_text("original_column_name,column_name,column_description,data_format,value_description\n"
                                     "status,order status,,text,A: open; B: shipped\n")
    briefing = render_briefing(inspect_database(db.connect(f"sqlite:///{path}"), load_docs(docs)))
    assert 'Table "orders" (3 rows)' in briefing and "A: open; B: shipped" in briefing and "all values: 'A', 'B'" in briefing
    assert 'View "open_orders" (view)' in briefing


def test_cli_db_url(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert parse_args([]).db_url == db.DEFAULT_URL
    args = parse_args(["--schema", "sales", "--schema", "hr", "--docs-dir", str(tmp_path)])
    assert args.schema == ["sales", "hr"] and args.docs_dir == str(tmp_path)
    with pytest.raises(SystemExit):
        parse_args(["--docs-dir", str(tmp_path / "missing")])
    assert parse_args(["--db-url", "postgresql+psycopg://u@h/d"]).db_url == "postgresql+psycopg://u@h/d"
