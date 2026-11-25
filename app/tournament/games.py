"""Jogos dos torneios: os Winning Eleven do PS1, fixos (não há mais cadastro
de jogos no admin). Cada torneio usa um deles, com o patch que o organizador
quiser - a ISO é enviada depois, em Gerenciar (app/tournament/iso.py).

seed_games() roda no migrate_db/init_db: marca os jogos da lista pelo nome
(o "Winning Eleven 2002" que já existia continua com o mesmo id) e desativa
os outros - torneios antigos de um jogo desativado continuam aparecendo.

Só os jogos cujo placar o RetroArch TIERES sabe ler (app/live.py, os mapas
de RAM do fork): um jogo base novo entra aqui junto com o mapa dele.

Capa (200 x 250, gravada em 480 x 600): app/static/images/games/<code>.webp/.jpg/.png;
sem ela, vale a capa provisória <code>.svg. As .webp são fotos da TV com a
tela de título cortadas em 4:5, com 90% da largura e o corte sobretudo
embaixo (o fio do controle). Nas capas pequenas (cover_url mini) vale
a .svg; no e-mail, ela em PNG 400 x 500 (images/games/email, tournament/announce.py).
"""
from pathlib import Path

from flask import current_app, url_for

from ..db import now

# (code, nome, ano) - na ordem de lançamento.
GAMES = (
    ("we3fv", "Winning Eleven 3: Final Version", 1998),
    ("we4", "Winning Eleven 4", 1999),
    ("we2000u23", "Winning Eleven 2000 U-23", 2000),
    ("we2001", "J.League Winning Eleven 2001", 2001),
    ("we2002", "Winning Eleven 2002", 2002),
)
# Jogos base só para o cadastro de ISO (code, nome): a ISO que não é de Winning
# Eleven, para jogar no emulador. Ficam com is_active = 0 (is_active = liberado
# para campeonato e amistoso): não aparecem para criar campeonato nem amistoso,
# nos filtros, no ranking nem no Ao vivo - só na página da ISO, no Enviar/Editar
# ISO (Minhas ISOs) e no cadastro de ISOs da administração (iso_games). As ISOs
# deles também não entram na escolha do convite nem do campeonato
# (iso_catalog.options / resolve).
ISO_ONLY = (
    ("playstation", "Jogo de Playstation"),
)
ISO_ONLY_CODES = tuple(code for code, _name in ISO_ONLY)
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
    for code, name in ISO_ONLY:
        found = db.execute("SELECT id FROM game_titles WHERE code = ?", (code,)).fetchone()
        if found:
            db.execute("UPDATE game_titles SET name = ?, year = NULL, platform = 'PS1', is_active = 0, updated_at = ? "
                       "WHERE id = ?", (name, now(), found["id"]))
        else:
            db.execute("INSERT INTO game_titles (code, name, year, platform, is_active, created_at, updated_at) "
                       "VALUES (?, ?, NULL, 'PS1', 0, ?, ?)", (code, name, now(), now()))


def is_iso_only(code):
    """Jogo base só de ISO (ISO_ONLY): não vale para campeonato nem amistoso."""
    return code in ISO_ONLY_CODES


def iso_games(db):
    """Os jogos base do cadastro de ISO: os de campeonato, na ordem de
    lançamento, e no fim os só de ISO (ISO_ONLY)."""
    marks = ",".join("?" * len(ISO_ONLY_CODES))
    return db.execute(f"SELECT id, name, code FROM game_titles WHERE is_active = 1 OR code IN ({marks}) "
                      f"ORDER BY code IN ({marks}), year, name", (*ISO_ONLY_CODES, *ISO_ONLY_CODES)).fetchall()


def cover_url(code, mini=False):
    """URL da capa do jogo (a provisória .svg quando não há uma de verdade).
    mini: a das capas pequenas (até 96 px de largura: cards do Ao vivo, filtros,
    listas) - a .svg, que a foto da TV não fica legível nesse tamanho."""
    folder = Path(current_app.static_folder) / COVER_DIR
    extensions = ("svg",) + COVER_EXTENSIONS if mini else COVER_EXTENSIONS
    for extension in extensions:
        if code and (folder / f"{code}.{extension}").exists():
            return url_for("static", filename=f"{COVER_DIR}/{code}.{extension}")
    return url_for("static", filename=f"{COVER_DIR}/default.svg")
