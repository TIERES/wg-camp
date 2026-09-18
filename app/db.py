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
        """Aplica a pequena migração necessária para bancos de testes iniciais."""
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
                player_names TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'live' CHECK (status IN ('live', 'finished')),
                stored_name TEXT NOT NULL,
                bytes_received INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                ended_at TEXT
            )
        """)
        database.commit()
        click.echo("Banco de dados atualizado.")
