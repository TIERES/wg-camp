"""Aviso de campeonato liberado: quando o organizador faz o sorteio
(tournaments.start -> store.start), cada inscrito recebe por e-mail um card
com o campeonato (notify.tournament_started, templates/email/tournament_started.html)
e vê a mesma mensagem numa janela em qualquer página do site
(templates/tournaments/_notice.html, aberta pelo base.html) até clicar em
"Entendi" (tabela tournament_notices_seen). Vale para todo campeonato em
andamento em que o jogador está inscrito."""
from flask import Blueprint, redirect, request, session, url_for

from ..accounts import external_url
from ..db import get_db, now
from ..images import url as image_url
from ..players import player_required
from ..security import validate_csrf
from ..timeutil import format_local
from ..wallet import format_cents, setting
from . import engine, formats, store

bp = Blueprint("announce", __name__, url_prefix="/torneios")

COVER_DIR = "images/games"
EMAIL_COVER_DIR = "images/games/email"  # as capas em PNG: e-mail não mostra SVG


def _date(value):
    return format_local(value + "T12:00:00+00:00", "%d/%m/%Y") if value else ""


def view(db, tournament, external=False):
    """O que o aviso mostra (janela e e-mail): nome, imagem, capa do jogo base,
    resumo do formato, datas, prêmio, se cada jogo paga W$ e o prazo de confirmação.
    external: links e imagens com o endereço completo do site (e-mail)."""
    full = (lambda endpoint, **values: external_url(endpoint, **values)) if external else url_for
    game = db.execute("SELECT * FROM game_titles WHERE id = ?", (tournament["game_title_id"],)).fetchone()
    organizer = db.execute("SELECT username FROM players WHERE id = ?", (tournament["organizer_id"],)).fetchone()
    # A imagem de compartilhamento (JPEG 1200 x 630) abre em qualquer leitor de e-mail; a do
    # campeonato é WebP (o Outlook não mostra) - só sem a outra.
    image_name = tournament["share_image"] or tournament["image"]
    image = image_url("torneio", image_name) if image_name else None
    if image and external:
        image = external_url("images.serve", kind="torneio", name=image_name)
    code = game["code"] if game else ""
    pool = store.prize_pool(db, tournament["id"])
    shares = engine.split_prize(pool, engine.parse_split(tournament["prize_split"]), 3) if pool else []
    return {
        "id": tournament["id"], "name": tournament["name"], "slug": tournament["slug"],
        "url": full("tournaments.show", slug=tournament["slug"]),
        "games_url": full("tournaments.show", slug=tournament["slug"], aba="jogos"),
        "my_matches_url": full("tournaments.my_matches"),
        "image": image,
        "game_name": game["name"] if game else "",
        "cover": full("static", filename=f"{EMAIL_COVER_DIR}/{code or 'default'}.png"),
        "summary": formats.describe(tournament["format"], store.settings_of(tournament)),
        "organizer": organizer["username"] if organizer else "",
        "starts_on": _date(tournament["starts_on"]), "ends_on": _date(tournament["ends_on"]),
        "prize": format_cents(pool) if pool else "",
        "shares": [format_cents(value) for value in shares],
        "pays_per_game": setting(db, "torneios_vitoria") > 0,
        "confirm_hours": setting(db, "torneios_confirmacao_horas"),
    }


EMAIL_IMAGES = "images/email"  # bandeiras e escudos da base em PNG (o e-mail não mostra SVG nem WebP)


def email_team_image(team):
    """Endereço completo da bandeira/escudo do time para o e-mail: o PNG gerado das
    bandeiras (static/flags) e dos escudos da base (static/teams); o escudo enviado
    pelo admin, como está; sem imagem (escudo gerado), None."""
    from pathlib import Path

    from flask import current_app
    if team is None:
        return None
    logo = team["logo"] or ""
    if team["flag"]:
        path = f"{EMAIL_IMAGES}/flags/{Path(team['flag']).stem}.png"
    elif logo.startswith("base:"):
        path = f"{EMAIL_IMAGES}/teams/{Path(logo[5:]).with_suffix('.png').as_posix()}"
    elif logo.startswith("upload:"):
        return external_url("tournaments.team_logo", filename=logo[7:])
    else:
        return None
    if not (Path(current_app.static_folder) / path).exists():
        return None
    return external_url("static", filename=path)


def group_view(db, tournament, stage, entry_id):
    """O grupo do inscrito na fase de grupos, para o e-mail: nome, os times com a
    bandeira/escudo e o jogador de cada um (na ordem do sorteio) e quantos se classificam."""
    group = db.execute("SELECT g.* FROM tournament_groups g JOIN tournament_group_members gm ON gm.group_id = g.id "
                       "WHERE g.stage_id = ? AND gm.entry_id = ?", (stage["id"], entry_id)).fetchone()
    if group is None:
        return None
    members = []
    for row in db.execute(
            "SELECT e.id, e.team_name, e.team_id, p.username FROM tournament_group_members gm "
            "JOIN tournament_entries e ON e.id = gm.entry_id JOIN players p ON p.id = e.player_id "
            "WHERE gm.group_id = ? ORDER BY gm.draw_order", (group["id"],)):
        team = db.execute("SELECT * FROM teams WHERE id = ?", (row["team_id"],)).fetchone() if row["team_id"] else None
        members.append({"team": row["team_name"] or row["username"], "username": row["username"] if row["team_name"] else "",
                        "image": email_team_image(team), "me": row["id"] == entry_id})
    qualifiers = store.settings_of(tournament).get("qualifiers") or 0
    note = ("" if not qualifiers else "O primeiro do grupo vai para o mata-mata." if qualifiers == 1
            else f"Os {qualifiers} primeiros do grupo vão para o mata-mata.")
    return {"name": group["name"], "members": members, "note": note}


def pending(db, player_id):
    """O campeonato em andamento em que o jogador está inscrito e ainda não clicou
    em "Entendi" (o mais antigo primeiro), ou None."""
    row = db.execute(
        "SELECT t.* FROM tournaments t JOIN tournament_entries e ON e.tournament_id = t.id AND e.player_id = ? "
        "WHERE t.status = 'running' AND NOT EXISTS (SELECT 1 FROM tournament_notices_seen s "
        "WHERE s.tournament_id = t.id AND s.player_id = ?) ORDER BY t.started_at, t.id LIMIT 1",
        (player_id, player_id)).fetchone()
    return view(db, row) if row else None


def tournament_notice():
    """Para o base.html: o aviso a abrir agora para quem está logado, ou None."""
    if "player_id" not in session:
        return None
    try:
        return pending(get_db(), session["player_id"])
    except Exception:  # banco sem a tabela ainda (antes da migração)
        return None


@bp.app_context_processor
def inject_notice():
    return {"tournament_notice": tournament_notice}


@bp.post("/<slug>/aviso-entendi")
@player_required
def seen(slug):
    """ "Entendi": o aviso deste campeonato não aparece mais para o jogador."""
    validate_csrf()
    db = get_db()
    tournament = db.execute("SELECT id FROM tournaments WHERE slug = ?", (slug,)).fetchone()
    if tournament:
        db.execute("INSERT OR IGNORE INTO tournament_notices_seen (player_id, tournament_id, seen_at) VALUES (?, ?, ?)",
                   (session["player_id"], tournament["id"], now()))
        db.commit()
    target = request.form.get("voltar") or url_for("tournaments.show", slug=slug)
    if not target.startswith("/") or target.startswith("//"):
        target = url_for("tournaments.show", slug=slug)
    return redirect(target)
