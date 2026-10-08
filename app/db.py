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


# Contas de jogadores e Memory Card online - separadas da tabela `users`, que
# é só de administradores. Idempotente: roda no init_db() e no migrate_db().
#  - players.username é o nick do Kaillera e nunca muda depois do cadastro
#    (o Memory Card de cada um é identificado por ele);
#  - player_tokens guarda só o SHA-256 dos tokens (confirmação de e-mail,
#    redefinição de senha e o token de API que o kailleraclient.dll usa);
#  - games é identificado pelo mesmo "CRC32:TAMANHO" (hex) que o anti-desync
#    do retroarch-k3-ffw calcula do conteúdo (.cue/.m3u resolvidos);
#  - cada versão de um cartão aponta para o arquivo pelo SHA-256 do conteúdo
#    (MEMCARDS_DIR/<sha256>.mcd), então versões iguais compartilham o arquivo;
#  - room_tickets são os "ingressos" de uso único que provam, ao host de uma
#    sala "Só logados", que quem entrou é mesmo o dono da conta.
PLAYERS_DDL = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    email_verified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS player_tokens (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('verify', 'reset', 'api')),
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    used_at TEXT,
    last_used_at TEXT
);

CREATE INDEX IF NOT EXISTS player_tokens_player_idx ON player_tokens(player_id, kind);

CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY,
    content_id TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memcards (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE RESTRICT,
    current_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (player_id, game_id)
);

CREATE TABLE IF NOT EXISTS memcard_versions (
    id INTEGER PRIMARY KEY,
    memcard_id INTEGER NOT NULL REFERENCES memcards(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('blank', 'upload', 'match', 'restore')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (memcard_id, version)
);

CREATE TABLE IF NOT EXISTS room_tickets (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    room TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    verified_by INTEGER REFERENCES players(id) ON DELETE SET NULL
);
"""


def init_db():
    db = get_db()
    with current_app.open_resource("schema.sql") as schema:
        db.executescript(schema.read().decode("utf-8"))
    db.executescript(PLAYERS_DDL)
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

    @app.cli.command("register-game")
    @click.argument("path")
    @click.argument("name", required=False)
    def register_game(path, name):
        """Cadastra um jogo do Memory Card online a partir do arquivo (.bin/.cue/.iso)."""
        from pathlib import Path
        from .memcards import content_id_for_path, get_or_create_game
        content_id = content_id_for_path(path)
        game = get_or_create_game(get_db(), content_id, name or Path(path).name)
        get_db().commit()
        click.echo(f"{game['name']}: {game['content_id']}")

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
    if "state_requested_at" not in live_session_columns:
        database.execute("ALTER TABLE live_sessions ADD COLUMN state_requested_at TEXT")
    file_columns = {column["name"] for column in database.execute("PRAGMA table_info(files)")}
    for column in ("md5", "archive_url", "archive_status", "archive_error", "archive_updated_at", "local_removed_at"):
        if column not in file_columns:
            database.execute(f"ALTER TABLE files ADD COLUMN {column} TEXT")
    database.executescript(PLAYERS_DDL)
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
