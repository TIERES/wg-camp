"""Jogos dos torneios: os Winning Eleven do PS1, fixos (não há mais cadastro
de jogos no admin). Cada torneio usa um deles, com o patch que o organizador
quiser - a ISO é enviada depois, em Gerenciar (app/tournament/iso.py).

seed_games() roda no migrate_db/init_db: marca os jogos da lista pelo nome
(o "Winning Eleven 2002" que já existia continua com o mesmo id) e desativa
os outros - torneios antigos de um jogo desativado continuam aparecendo.

Só os jogos cujo placar o RetroArch TIERES sabe ler (app/live.py, os mapas
de RAM do fork): um jogo base novo entra aqui junto com o mapa dele.

Capa (200 x 250): app/static/images/games/<code>.webp/.jpg/.png; sem ela,
vale a capa provisória <code>.svg.
"""
from pathlib import Path

from flask import current_app, url_for

from ..db import now

# (code, nome, ano) - na ordem de lançamento.
GAMES = (
    ("we3fv", "Winning Eleven 3: Final Version", 1998),
    ("we4", "Winning Eleven 4", 1999),
    ("we2002", "Winning Eleven 2002", 2002),
)
COVER_DIR = "images/games"
COVER_EXTENSIONS = ("webp", "jpg", "png", "svg")


def seed_games(db):
    columns = {column["name"] for column in db.execute("PRAGMA table_info(game_titles)")}
    if "code" not in columns:
        db.execute("ALTER TABLE game_titles ADD COLUMN code TEXT")
    if "year" not in columns:
        db.execute("ALTER TABLE game_titles ADD COLUMN year INTEGER")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS game_titles_code_idx ON game_titles(code)")
    for code, name, year in GAMES:
        found = db.execute("SELECT id FROM game_titles WHERE code = ? OR name = ? COLLATE NOCASE ORDER BY code IS NULL",
                           (code, name)).fetchone()
        if found:
            db.execute("UPDATE game_titles SET code = ?, name = ?, year = ?, platform = 'PS1', is_active = 1, "
                       "updated_at = ? WHERE id = ?", (code, name, year, now(), found["id"]))
        else:
            db.execute("INSERT INTO game_titles (code, name, year, platform, is_active, created_at, updated_at) "
                       "VALUES (?, ?, ?, 'PS1', 1, ?, ?)", (code, name, year, now(), now()))
    codes = [code for code, _name, _year in GAMES]
    db.execute(f"UPDATE game_titles SET is_active = 0, updated_at = ? WHERE is_active = 1 "
               f"AND (code IS NULL OR code NOT IN ({','.join('?' * len(codes))}))", (now(), *codes))


def cover_url(code):
    """URL da capa do jogo (a provisória .svg quando não há uma de verdade)."""
    folder = Path(current_app.static_folder) / COVER_DIR
    for extension in COVER_EXTENSIONS:
        if code and (folder / f"{code}.{extension}").exists():
            return url_for("static", filename=f"{COVER_DIR}/{code}.{extension}")
    return url_for("static", filename=f"{COVER_DIR}/default.svg")
