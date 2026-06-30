import sqlite3
from contextlib import closing
from pathlib import Path


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(db_path: str | Path) -> None:
    schema_path = Path(__file__).with_name("schema.sql")
    schema_sql = schema_path.read_text(encoding="utf-8")

    with closing(get_connection(db_path)) as connection:
        connection.executescript(schema_sql)
        connection.commit()
