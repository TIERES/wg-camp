import sqlite3
from datetime import datetime, timezone

import click
from flask import current_app, g
from werkzeug.security import generate_password_hash


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    with current_app.open_resource("schema.sql") as schema:
        db.executescript(schema.read().decode("utf-8"))
    db.commit()


def init_app(app):
    app.teardown_appcontext(close_db)

    @app.cli.command("init-db")
    def init_db_command():
        init_db()
        click.echo("Banco de dados inicializado.")

    @app.cli.command("create-admin")
    @click.argument("username")
    @click.password_option()
    def create_admin(username, password):
        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (username, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (username, generate_password_hash(password), now(), now()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            raise click.ClickException("Este usuário já existe.")
        click.echo("Administrador criado.")

    @app.cli.command("migrate-db")
    def migrate_db_command():
        migrate_db()
        click.echo("Banco de dados atualizado.")


def migrate_db():
    """Aplica a pequena migração necessária para bancos de testes iniciais.

    Chamada tanto pelo comando `flask --app wsgi migrate-db` (uso local) quanto
    diretamente via script Python em produção, onde a CLI do Flask não
    funciona sob Python 3.9 com as dependências vendorizadas (falta
    `importlib_metadata`) - mesmo motivo pelo qual o bootstrap chama `init_db()`
    direto em vez de `flask --app wsgi init-db`.
    """
    database = get_db()
    columns = {column["name"] for column in database.execute("PRAGMA table_info(championships)")}
    if "league_name" not in columns:
        database.execute("ALTER TABLE championships ADD COLUMN league_name TEXT")
    database.execute("DROP INDEX IF EXISTS one_current_championship")
    database.execute("""
        CREATE TABLE IF NOT EXISTS live_sessions (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL UNIQUE,
            app_name TEXT NOT NULL DEFAULT '',
            game_name TEXT NOT NULL DEFAULT '',
            owner_name TEXT NOT NULL DEFAULT '',
            player_names TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'live' CHECK (status IN ('live', 'finished')),
            stored_name TEXT NOT NULL,
            bytes_received INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            ended_at TEXT
        )
    """)
    live_session_columns = {column["name"] for column in database.execute("PRAGMA table_info(live_sessions)")}
    if "owner_name" not in live_session_columns:
        database.execute("ALTER TABLE live_sessions ADD COLUMN owner_name TEXT NOT NULL DEFAULT ''")
    if "duration_seconds" not in live_session_columns:
        database.execute("ALTER TABLE live_sessions ADD COLUMN duration_seconds INTEGER")
    database.execute("CREATE INDEX IF NOT EXISTS live_sessions_game_name_idx ON live_sessions(game_name)")
    database.execute("CREATE INDEX IF NOT EXISTS live_sessions_replay_idx ON live_sessions(status, duration_seconds)")
    rows = database.execute(
        "SELECT session_id, started_at, ended_at FROM live_sessions WHERE status = 'finished' AND duration_seconds IS NULL AND ended_at IS NOT NULL"
    ).fetchall()
    for row in rows:
        try:
            started = datetime.fromisoformat(row["started_at"])
            ended = datetime.fromisoformat(row["ended_at"])
        except ValueError:
            continue
        duration = max(0, int((ended - started).total_seconds()))
        database.execute(
            "UPDATE live_sessions SET duration_seconds = ? WHERE session_id = ?",
            (duration, row["session_id"]),
        )
    database.commit()
