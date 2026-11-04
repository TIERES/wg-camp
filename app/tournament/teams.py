"""Base de times dos torneios (clubes, seleções e times criados pelo admin).

A base inicial vem de teams_seed.json (gerado a partir do football-logos.cc
e do flag-icons - ver "source" no arquivo) e é carregada por seed_teams() no
migrate_db/init_db: só insere o que falta, então nomes e escudos editados
pelo admin não são sobrescritos.

teams.logo:  "base:<caminho>"  -> app/static/teams/<caminho>
             "upload:<arquivo>" -> TEAM_LOGOS_DIR (enviado pelo admin)
teams.flag:  arquivo em app/static/flags (só seleções)
"""
import base64
import hashlib
import json
import unicodedata
from pathlib import Path

from ..db import now

SEED_PATH = Path(__file__).with_name("teams_seed.json")
SEED_SETTING = "teams_seed_version"

TEAM_MODES = {
    "free": "Livre: cada jogador digita o nome do seu time",
    "choose": "Lista: cada jogador escolhe um time da lista",
    "draw": "Lista: os times são sorteados entre os jogadores",
}
KINDS = {"club": "Clube", "national": "Seleção", "custom": "Fictício"}


def seed_teams(db):
    """Carrega a base inicial (idempotente; só roda quando o arquivo muda)."""
    data = SEED_PATH.read_bytes()
    version = hashlib.sha1(data).hexdigest()[:12]
    row = db.execute("SELECT value FROM site_settings WHERE key = ?", (SEED_SETTING,)).fetchone()
    if row and row["value"] == version:
        return False
    seed = json.loads(data.decode("utf-8"))
    stamp = now()
    for team in seed["teams"]:
        db.execute(
            "INSERT OR IGNORE INTO teams (name, kind, country, logo, flag, source_key, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (team["name"], team["kind"], team.get("country") or "", "base:" + team["logo"], team.get("flag"),
             team["key"], stamp, stamp))
    for seq, group in enumerate(seed["groups"]):
        db.execute("INSERT OR IGNORE INTO team_groups (slug, name, seq, created_at) VALUES (?, ?, ?, ?)",
                   (group["slug"], group["name"], seq, stamp))
        group_id = db.execute("SELECT id FROM team_groups WHERE slug = ?", (group["slug"],)).fetchone()[0]
        for key in group["teams"]:
            db.execute("INSERT OR IGNORE INTO team_group_members (group_id, team_id) "
                       "SELECT ?, id FROM teams WHERE source_key = ?", (group_id, key))
    db.execute("INSERT OR REPLACE INTO site_settings (key, value, updated_at) VALUES (?, ?, ?)",
               (SEED_SETTING, version, stamp))
    return True


def plain_name(value):
    """Nome para comparar sem acentos, maiúsculas nem espaços repetidos."""
    return " ".join(unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().casefold().split())


def find_by_name(db, name):
    """Time ativo da base com esse nome (ignorando acentos/maiúsculas), ou None."""
    wanted = plain_name(name)
    if not wanted:
        return None
    return next((row for row in db.execute("SELECT * FROM teams WHERE is_active = 1 ORDER BY id")
                 if plain_name(row["name"]) == wanted), None)


_COLORS = ("#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#be185d", "#ca8a04", "#4f46e5",
           "#059669")


def generated_badge(name):
    """Escudo genérico (SVG em data URI) com as iniciais, para time sem imagem."""
    words = [w for w in (name or "?").replace("-", " ").split() if w[:1].isalnum()]
    initials = ("".join(w[0] for w in words[:3]) if len(words) > 1 else (name or "?")[:3]).upper()
    color = _COLORS[int(hashlib.md5((name or "").encode("utf-8")).hexdigest(), 16) % len(_COLORS)]
    size = 30 if len(initials) <= 2 else 24
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
           f'<path d="M50 4 L90 16 V48 C90 74 72 89 50 97 C28 89 10 74 10 48 V16 Z" fill="{color}" '
           f'stroke="#fff" stroke-width="5"/><text x="50" y="60" font-family="Arial,sans-serif" font-weight="700" '
           f'font-size="{size}" fill="#fff" text-anchor="middle">{_xml(initials)}</text></svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def _xml(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
