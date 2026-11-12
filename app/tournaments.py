"""Portal de torneios (/torneios) e a administração dele (/admin/torneios).

Qualquer jogador logado cria um torneio e vira o organizador dele: abre as
inscrições, sorteia, lança e corrige resultados, gera as fases e encerra. O
Admin WE Camp (tabela `users`) pode tudo em qualquer torneio e mantém os
valores do W$ (app/wallet.py). Os jogos são fixos (app/tournament/games.py).

Regras dos formatos em app/tournament/ (engine/formats/store).
"""
import hashlib
import json
import re
import secrets
import sqlite3
import time
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from unicodedata import normalize
from urllib.parse import urlsplit

from flask import (abort, Blueprint, current_app, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from markupsafe import Markup

from . import images
from .accounts import absolute_url
from .db import get_db, now
from .players import player_required
from .security import login_required, validate_csrf
from .storage import attachment_header
from .timeutil import format_local
from .tournament import (engine, formats, friendly, friends, games as game_catalog, iso, iso_catalog, notify, reports,
                         stats, store, xp)
from .tournament.store import TournamentError
from .tournament.teams import KINDS as TEAM_KINDS, TEAM_MODES, generated_badge
from .wallet import (KINDS, MONEY_SETTINGS, SETTING_LABELS, SIGNED_SETTINGS, InsufficientFunds, ensure_wallet,
                     format_cents, parse_cents, post, setting)

bp = Blueprint("tournaments", __name__, url_prefix="/torneios")
admin_bp = Blueprint("tournaments_admin", __name__, url_prefix="/admin/torneios")

STATUS_LABELS = {"draft": "Rascunho", "registration": "Inscrições abertas", "running": "Em andamento",
                 "finished": "Encerrado", "cancelled": "Cancelado"}
PRIZE_SPLITS = ("60,30,10", "50,30,20", "70,20,10", "100,0,0")


def slugify(value):
    value = normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")[:60] or "torneio"


def _int_or_none(value):
    value = (value or "").strip()
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        raise TournamentError("Use só números no placar.")


def is_admin():
    return "user_id" in session


def _dates(form):
    """Datas prováveis de início e fim (AAAA-MM-DD do <input type=date>), opcionais."""
    values = []
    for key, label in (("starts_on", "início"), ("ends_on", "fim")):
        raw = (form.get(key) or "").strip()
        try:
            values.append(date.fromisoformat(raw).isoformat() if raw else None)
        except ValueError:
            raise TournamentError(f"Data provável de {label} inválida.")
    if values[0] and values[1] and values[1] < values[0]:
        raise TournamentError("O fim provável não pode ser antes do início.")
    return values


@bp.app_template_filter("date_br")
def date_br(value):
    """AAAA-MM-DD -> DD/MM/AAAA."""
    return f"{value[8:10]}/{value[5:7]}/{value[:4]}" if value else ""


SCORE_TIPS = {
    "scheduled": "Ainda sem resultado",
    "reported": "Placar reportado por um jogador: aguardando a confirmação do adversário",
    "confirmed": "Resultado confirmado",
    "disputed": "Placar contestado: aguardando quem reportou aceitar a correção ou o organizador decidir",
    "partial": "Agregado parcial: os 2 jogos ainda não foram confirmados",
    "live": "Ao vivo: placar lido do jogo pelo RetroArch TIERES, ainda precisa ser confirmado entre os jogadores",
}


# Global (e não context processor): os macros importados com {% from %} só
# enxergam os globais do Jinja.
bp.record_once(lambda state: state.app.jinja_env.globals.update(score_tips=SCORE_TIPS,
                                                                game_cover=game_catalog.cover_url))


@bp.app_template_global()
def player_rank(player_id):
    """Rank (F a A), XP e barra até o próximo rank do jogador (tournament/xp.py)."""
    return xp.info(player_id)


@bp.app_template_global()
def match_xp(match_id, player_id):
    """XP que o jogador ganhou (+) ou perdeu (-) numa partida confirmada, ou None."""
    return xp.match_deltas(match_id).get(player_id)


@bp.app_template_global()
def match_tripled(match_id):
    """Na partida, quem saiu no meio perdeu o triplo de XP?"""
    return xp.match_tripled(match_id)


@bp.app_template_global()
def friendly_xp(friendly_id):
    """XP do amistoso que valeu (a soma dos jogos - xp.friendly_deltas), ou None."""
    return xp.friendly_deltas(friendly_id)


@bp.app_template_global()
def history_url(row):
    """Link de uma linha do histórico (stats.history): o jogo do torneio ou o amistoso."""
    if row["kind"] == "a":
        return url_for("tournaments.friendly_show", friendly_id=row["match_id"])
    return url_for("tournaments.match", slug=row["slug"], match_id=row["match_id"])


@bp.app_template_global()
def friendly_deadline(row):
    """Até quando o placar de amistoso lido do jogo espera os jogadores."""
    return friendly.deadline(get_db(), row)


@bp.app_template_global()
def reported_score(match):
    """Último placar reportado de um jogo ainda não confirmado (para exibir
    antes da confirmação), com cache por requisição."""
    if match["status"] not in ("reported", "disputed"):
        return None
    cache = g.setdefault("reported_scores", {})
    if match["id"] not in cache:
        cache[match["id"]] = reports.last_report(get_db(), match["id"])
    return cache[match["id"]]


def can_manage(tournament):
    return is_admin() or session.get("player_id") == tournament["organizer_id"]


def _load(slug, private_ok=False):
    """O campeonato da URL, ou 404. "Somente para amigos" só existe para quem pode
    vê-lo (can_view); com private_ok ele vem assim mesmo, e quem chama cuida do
    que mostra a quem não é amigo (a página do campeonato - show)."""
    db = get_db()
    tournament = db.execute(
        "SELECT t.*, g.name AS game_name, g.platform, g.code AS game_code, p.username AS organizer_name "
        "FROM tournaments t "
        "JOIN game_titles g ON g.id = t.game_title_id JOIN players p ON p.id = t.organizer_id WHERE t.slug = ?",
        (slug,)).fetchone()
    if tournament is None or (tournament["status"] == "draft" and not can_manage(tournament)):
        abort(404)
    if tournament["friends_only"] and not private_ok and not can_view(db, tournament):
        abort(404)
    return tournament


def can_view(db, tournament):
    """Torneio "Somente para amigos": só o organizador (e o admin), os amigos dele e quem já
    está inscrito entram nele. Os outros não o veem nas listas; com o link, veem só o aviso
    de campeonato privado e os dados da Visão geral (show)."""
    me = session.get("player_id")
    if not tournament["friends_only"] or can_manage(tournament):
        return True
    return bool(me) and (store.are_friends(db, me, tournament["organizer_id"]) or db.execute(
        "SELECT 1 FROM tournament_entries WHERE tournament_id = ? AND player_id = ?",
        (tournament["id"], me)).fetchone() is not None)


_FRIEND_OF_ORGANIZER = ("EXISTS (SELECT 1 FROM friendships f WHERE f.status = 'accepted' AND "
                        "((f.requester_id = t.organizer_id AND f.addressee_id = ?) "
                        "OR (f.addressee_id = t.organizer_id AND f.requester_id = ?)))")
_ENROLLED = "t.id IN (SELECT tournament_id FROM tournament_entries WHERE player_id = ?)"


def manager_required(view):
    @wraps(view)
    def wrapped(slug, *args, **kwargs):
        tournament = _load(slug)
        if not can_manage(tournament):
            abort(403)
        if request.method == "POST":
            validate_csrf()
        return view(tournament, *args, **kwargs)
    return wrapped


def _run(action, tournament, success, *args):
    """Executa uma ação do store com commit/rollback e mensagem."""
    db = get_db()
    try:
        action(db, store.get_tournament(db, tournament["id"]), *args)
        db.commit()
        if success:
            flash(success, "success")
        return True
    except (TournamentError, InsufficientFunds, ValueError) as error:
        db.rollback()
        flash(str(error), "error")
        return False


# --- Telas de leitura ---------------------------------------------------------

def team_image(team):
    """URL da imagem de um time (linha de `teams`): seleção com a bandeira, clube
    com o escudo (da base ou enviado pelo admin), ou o escudo gerado."""
    if team is None:
        return generated_badge("?")
    if team["flag"]:
        return url_for("static", filename="flags/" + team["flag"])
    logo = team["logo"] or ""
    if logo.startswith("base:"):
        return url_for("static", filename="teams/" + logo[5:])
    if logo.startswith("upload:"):
        return url_for("tournaments.team_logo", filename=logo[7:])
    if team["flag"]:
        return url_for("static", filename="flags/" + team["flag"])
    return generated_badge(team["name"])


def _crests(names):
    """entry_id -> só o escudo (para as linhas curtas da chave)."""
    crests = {}
    for entry_id, label in names.items():
        match = re.search(r'<img class="crest" src="([^"]+)"', str(label))
        crests[entry_id] = Markup('<img class="crest" src="{}" alt="" loading="lazy">').format(match.group(1)) \
            if match else label
    return crests


def _names(db, tournament):
    """entry_id -> HTML com escudo, time e jogador (ou só o jogador)."""
    rows = db.execute(
        "SELECT e.id, e.team_name, p.username, t.name, t.logo, t.flag FROM tournament_entries e "
        "JOIN players p ON p.id = e.player_id LEFT JOIN teams t ON t.id = e.team_id WHERE e.tournament_id = ?",
        (tournament["id"],)).fetchall()
    names = {}
    for row in rows:
        if not row["team_name"]:
            names[row["id"]] = Markup('<span class="entry"><span class="entry-name">{}</span></span>').format(
                row["username"])
            continue
        image = team_image(row) if row["name"] else generated_badge(row["team_name"])
        names[row["id"]] = Markup(
            '<span class="entry"><img class="crest" src="{}" alt="" loading="lazy">'
            '<span class="entry-name">{}<small>{}</small></span></span>').format(image, row["team_name"],
                                                                               row["username"])
    return names


def _provisional(db, tournament, live_table):
    """Placares ainda não confirmados que a tabela soma (marcados como tal):
    jogos rolando agora (Ao vivo) e placares reportados esperando confirmação
    (pelos jogadores ou lidos do jogo). {jogo: (gols do mandante, do visitante)}"""
    scores = {}
    for row in db.execute(
            "SELECT m.id FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
            "WHERE s.tournament_id = ? AND s.status = 'running' AND m.status = 'reported'", (tournament["id"],)):
        reported = reports.last_report(db, row["id"])
        if reported and reported["home_goals"] is not None:
            scores[row["id"]] = (reported["home_goals"], reported["away_goals"])
    scores.update(live_table["matches"])
    return scores


def _live_table_view(db, tournament):
    """A aba Tabela de campeonato em andamento: os jogos ao vivo (app/live.py)
    e as fases com os placares não confirmados somados."""
    from . import live
    live_table = live.tournament_live(db, tournament["id"])
    return live_table, _stage_views(db, tournament, _provisional(db, tournament, live_table))


def _stage_views(db, tournament, provisional=None):
    """Fases com tabelas, jogos por rodada e chaves, prontas para o template.
    provisional: placares não confirmados que as tabelas somam (_provisional)."""
    views = []
    for stage in store.stages_of(db, tournament["id"]):
        matches = db.execute("SELECT * FROM tournament_matches WHERE stage_id = ? ORDER BY round, tie_id, leg, id",
                             (stage["id"],)).fetchall()
        view = {"stage": stage, "settings": store.settings_of(stage), "rounds": [], "groups": [], "bracket": [],
                "highlight": 0, "playoff": 0, "match_ids": [m["id"] for m in matches],
                "pending": any(m["status"] in ("reported", "disputed") for m in matches)}
        rounds = {}
        for match in matches:
            rounds.setdefault(match["round"], []).append(match)
        if stage["kind"] == "knockout":
            ties = db.execute("SELECT * FROM tournament_ties WHERE stage_id = ? ORDER BY round, is_third_place, "
                              "position", (stage["id"],)).fetchall()
            last = max(t["round"] for t in ties)
            # Repescagem: confronto que recebe o perdedor de outro (fora a disputa de 3º).
            feeders = {t["loser_next_tie_id"]: t for t in ties if t["loser_next_tie_id"]}
            per_round = {}
            for tie in ties:
                per_round.setdefault(tie["round"], []).append(tie)
            for round_no in sorted(per_round):
                main = [t for t in per_round[round_no] if not t["is_third_place"]]
                name = engine.round_name(len(main), view["settings"].get("first_round_name"), round_no == 1)
                items = []
                for tie in per_round[round_no]:
                    legs = [m for m in matches if m["tie_id"] == tie["id"]]
                    aggregate = None
                    if tie["legs"] == 2 and legs and all(m["status"] == "confirmed" for m in legs):
                        aggregate = {tie["entry_a"]: 0, tie["entry_b"]: 0}
                        for m in legs:
                            aggregate[m["home_entry_id"]] += m["home_goals"]
                            aggregate[m["away_entry_id"]] += m["away_goals"]
                    feeder = None if tie["is_third_place"] else feeders.get(tie["id"])
                    items.append({"tie": tie, "legs": legs, "aggregate": aggregate,
                                  "label": "Disputa de 3º lugar" if tie["is_third_place"] else
                                  "Repescagem" if feeder else None,
                                  "waits_for": None if not feeder else "perdedor do confronto " + (
                                      "abaixo" if feeder["position"] > tie["position"] else "acima")})
                view["bracket"].append({"name": name, "round": round_no, "items": items, "is_last": round_no == last})
            names = {r["round"]: r["name"] for r in view["bracket"]}
            # Jogos da repescagem num bloco próprio, logo depois da rodada deles.
            repechage = {tid for tid in feeders if not any(t["id"] == tid and t["is_third_place"] for t in ties)}
            for round_no in list(rounds):
                waiting = [m for m in rounds[round_no] if m["tie_id"] in repechage]
                if waiting:
                    rounds[round_no] = [m for m in rounds[round_no] if m["tie_id"] not in repechage]
                    rounds[round_no + 0.5] = waiting
                    names[round_no + 0.5] = "Repescagem"
                    if not rounds[round_no]:
                        del rounds[round_no]
        else:
            for group, table in store.group_tables(db, stage["id"], provisional):
                view["groups"].append({"group": group, "table": table})
            names = {r: f"Rodada {r}" for r in rounds}
            if view["settings"].get("legs") == 2 and stage["kind"] in ("league", "groups"):
                # Ida e volta: um bloco por turno em vez de um por rodada. O
                # returno ocupa a segunda metade das rodadas de cada grupo.
                half = {}
                for match in matches:
                    half[match["group_id"]] = max(half.get(match["group_id"], 0), match["round"])
                rounds = {1: [], 2: []}
                for match in matches:
                    rounds[1 if match["round"] <= half[match["group_id"]] // 2 else 2].append(match)
                names = {1: "Ida", 2: "Volta"}
            config = store.settings_of(tournament)
            if stage["kind"] == "groups":
                view["highlight"] = config.get("qualifiers", 0)
            elif stage["kind"] == "league" and tournament["format"] == "league_knockout":
                view["highlight"] = config.get("playoff_qualifiers", 0)
            elif stage["kind"] == "swiss":
                view["highlight"], view["playoff"] = config.get("direct", 0), config.get("direct", 0) * 2
        view["rounds"] = [{"name": names.get(r, f"Rodada {r}"), "matches": rounds[r]} for r in sorted(rounds)]
        # Ida e volta: "legs" = [jogos de ida, jogos de volta], para os botões
        # Ida/Volta das telas girarem entre os dois (macro leg_flip).
        if stage["kind"] == "knockout":
            tie_legs = {t["id"]: t["legs"] for t in ties}
            for rnd in view["rounds"]:
                ms = rnd["matches"]
                if ms and all(tie_legs.get(m["tie_id"]) == 2 for m in ms):
                    rnd["legs"] = [[m for m in ms if m["leg"] == 1], [m for m in ms if m["leg"] == 2]]
        elif names.get(1) == "Ida" and len(view["rounds"]) == 2:
            ida, volta = view["rounds"]
            view["rounds"] = [{"name": "Turno e returno", "matches": ida["matches"] + volta["matches"],
                               "legs": [ida["matches"], volta["matches"]]}]
        views.append(view)
    return views


def _tournament_list(template, player_id=None, **extra):
    """Cards de torneios com os filtros de status, jogo, nome e organizador.
    Com `player_id` (Meus campeonatos), só os torneios em que esse jogador está
    inscrito, que ele criou ou, se forem "Somente para amigos", de um amigo dele;
    sem escolher o status, só os com inscrições abertas e em andamento.
    Sem `player_id` (lista pública), os "Somente para amigos" só aparecem para
    quem pode vê-los (can_view)."""
    db = get_db()
    status = request.args.get("status", "")
    game = request.args.get("jogo", type=int)
    sql = ("SELECT t.*, g.name AS game_name, g.code AS game_code, p.username AS organizer_name, "
           "(SELECT COUNT(*) FROM tournament_entries e WHERE e.tournament_id = t.id) AS entries, "
           "i.original_filename AS iso_name, i.archive_url AS iso_archive_url, i.local_removed_at AS iso_removed "
           "FROM tournaments t JOIN game_titles g ON g.id = t.game_title_id JOIN players p ON p.id = t.organizer_id "
           "LEFT JOIN tournament_isos i ON i.tournament_id = t.id WHERE t.status != 'draft'")
    if player_id:
        scope = f" AND (t.organizer_id = ? OR {_ENROLLED} OR (t.friends_only = 1 AND {_FRIEND_OF_ORGANIZER}))"
        scope_params = [player_id] * 4
    elif is_admin():
        scope, scope_params = "", []
    else:
        me = session.get("player_id") or 0
        scope = f" AND (t.friends_only = 0 OR t.organizer_id = ? OR {_ENROLLED} OR {_FRIEND_OF_ORGANIZER})"
        scope_params = [me] * 4
    sql += scope
    params = list(scope_params)
    if status in STATUS_LABELS:
        sql += " AND t.status = ?"
        params.append(status)
    elif player_id and status != "todos":
        # Meus campeonatos: ao abrir, só inscrições abertas e em andamento; "Todos os status" traz tudo.
        sql += " AND t.status IN ('registration', 'running')"
    if game:
        sql += " AND t.game_title_id = ?"
        params.append(game)
    sql += (" ORDER BY CASE t.status WHEN 'registration' THEN 0 WHEN 'running' THEN 1 WHEN 'finished' THEN 2 "
            "ELSE 3 END, t.updated_at DESC")
    tournaments = db.execute(sql, params).fetchall()
    # Nome e organizador: trecho do texto, sem diferenciar acentos nem maiúsculas
    # (o LIKE do SQLite só ignora maiúsculas sem acento), por isso o filtro é aqui.
    name = request.args.get("nome", "").strip()
    organizer = request.args.get("organizador", "").strip()
    suggestions = db.execute(
        "SELECT DISTINCT t.name, p.id AS player_id, p.username FROM tournaments t JOIN players p ON p.id = t.organizer_id "
        "WHERE t.status != 'draft'" + scope, scope_params).fetchall()
    if name or organizer:
        # Organizador escolhido na lista (nome exato) não traz quem só começa igual (TIERES x TIERES1).
        exact = any(_plain(r["username"]) == _plain(organizer) for r in suggestions)
        tournaments = [t for t in tournaments if _plain(name) in _plain(t["name"]) and (
            _plain(organizer) == _plain(t["organizer_name"]) if exact else _plain(organizer) in _plain(t["organizer_name"]))]
    tournaments = tournaments[:200]
    # Rodapé do card: "Download ISO" só quando a ISO já pode ser baixada (a mesma regra da página do torneio).
    iso_ready = {t["id"] for t in tournaments if t["iso_name"] and iso.downloadable(
        {"archive_url": t["iso_archive_url"], "local_removed_at": t["iso_removed"]}, current_app)}
    games = db.execute("SELECT id, name, code FROM game_titles WHERE is_active = 1 ORDER BY year, name").fetchall()
    return render_template(template, tournaments=tournaments, games=games, status=status,
                           game=game, name=name, organizer=organizer,
                           name_options=sorted({r["name"] for r in suggestions}, key=_plain),
                           organizer_options=sorted({r["player_id"]: {"id": r["player_id"], "username": r["username"]}
                                                     for r in suggestions}.values(), key=lambda p: _plain(p["username"])),
                           status_labels=STATUS_LABELS, format_labels=formats.FORMATS, iso_ready=iso_ready, **extra)


@bp.get("/")
def index():
    return _tournament_list("tournaments/index.html", list_endpoint="tournaments.index")


@bp.get("/meus-campeonatos")
@player_required
def my_tournaments():
    return _tournament_list("tournaments/mine.html", session["player_id"], list_endpoint="tournaments.my_tournaments",
                            invites=friends.my_invites(get_db(), session["player_id"]))


# --- Amigos e convites (app/tournament/friends.py) ----------------------------

def _back(default):
    """Volta para a página de onde veio o formulário (só caminhos do próprio site)."""
    target = request.form.get("voltar", "")
    return redirect(target if target.startswith("/torneios") else default)


@bp.get("/amigos")
@player_required
def friends_page():
    db = get_db()
    me = session["player_id"]
    my_friends = friends.friends(db, me)
    # Sugestões do "Adicionar amigo": quem tem conta confirmada e ainda não é amigo.
    taken = {me} | {p["id"] for p in my_friends}
    others = [p for p in db.execute("SELECT id, username FROM players WHERE email_verified_at IS NOT NULL "
                                    "ORDER BY username COLLATE NOCASE").fetchall() if p["id"] not in taken]
    from .listing import cap
    search = request.args.get("busca", "").strip()
    shown = [p for p in my_friends if search.lower() in p["username"].lower()] if search else my_friends
    shown, shown_total = cap(shown)
    return render_template("tournaments/friends.html", friends=my_friends, others=others, shown=shown,
                           shown_total=shown_total, search=search,
                           received=friends.pending_received(db, me), sent=friends.pending_sent(db, me))


@bp.post("/amigos/adicionar")
@player_required
def friend_add():
    validate_csrf()
    db = get_db()
    me = session["player_id"]
    other = friends.find_player(db, request.form.get("nome"))
    try:
        if other is None:
            raise TournamentError("Jogador não encontrado.")
        result = friends.request(db, me, other["id"])
        db.commit()
    except TournamentError as error:
        db.rollback()
        flash(str(error), "error")
    else:
        if result == "accepted":
            flash(f"Agora você e {other['username']} são amigos!", "success")
        else:
            flash(f"Pedido de amizade enviado para {other['username']}.", "success")
        notify.friend_request(db, me, other["id"], accepted=result == "accepted")
    return _back(url_for("tournaments.friends_page"))


@bp.post("/amigos/<int:player_id>/<acao>")
@player_required
def friend_action(player_id, acao):
    validate_csrf()
    db = get_db()
    me = session["player_id"]
    try:
        if acao in ("aceitar", "recusar"):
            friends.respond(db, me, player_id, acao == "aceitar")
        elif acao == "remover":
            friends.remove(db, me, player_id)
        else:
            abort(404)
        db.commit()
    except TournamentError as error:
        db.rollback()
        flash(str(error), "error")
    else:
        flash({"aceitar": "Pedido de amizade aceito!", "recusar": "Pedido de amizade recusado.",
               "remover": "Pronto."}[acao], "success")
        if acao == "aceitar":
            notify.friend_request(db, me, player_id, accepted=True)
    return _back(url_for("tournaments.friends_page"))


# --- Amistosos (app/tournament/friendly.py) -----------------------------------

# Amistosos anteriores na lista: os mais recentes; os outros, pelo filtro.
FRIENDLIES_HISTORY_LIMIT = 20


def _friendly_in_filters(row, me, filters):
    """O amistoso passa no filtro da lista (nome do adversário, data e hora
    do jogo - o placar informado - entre "de" e "até", no horário local)?"""
    from .timeutil import to_local
    opponent = row["away_name"] if row["home_id"] == me else row["home_name"]
    if filters["opponent"] and filters["opponent"].casefold() not in (opponent or "").casefold():
        return False
    when = to_local(row["reported_at"] or row["confirmed_at"] or row["updated_at"])
    when = when.replace(tzinfo=None) if when else None
    for key, after in (("date_from", True), ("date_to", False)):
        try:
            limit = datetime.fromisoformat(filters[key]) if filters[key] else None
        except ValueError:
            limit = None
        if limit and (when is None or (when < limit if after else when > limit)):
            return False
    return True


@bp.get("/amistosos")
@player_required
def friendlies():  # noqa: C901 - a lista, os filtros e as partidas de cada amistoso
    db = get_db()
    me = session["player_id"]
    rows = friendly.for_player(db, me)
    # No topo, em destaque: placar para eu confirmar e convite para eu aceitar.
    respond = [r for r in rows if (r["status"] == "reported" and friendly.needs_me(r, me))
               or (r["status"] == "invited" and r["away_id"] == me)]
    # Em aberto (também no topo) e Anteriores: filtro por adversário e data/hora,
    # cada lista com o seu (aberto_* e adversario/de/ate).
    def read_filters(prefix):
        return {"opponent": request.args.get(prefix + "adversario", "").strip()[:32],
                "date_from": request.args.get(prefix + "de", "")[:16],
                "date_to": request.args.get(prefix + "ate", "")[:16]}
    open_filters, filters = read_filters("aberto_"), read_filters("")
    open_all = [r for r in rows if r["status"] in friendly.OPEN and r not in respond]
    open_all.sort(key=lambda r: not friendly.needs_me(r, me))  # aguardando o meu placar primeiro
    pending = [r for r in open_all if _friendly_in_filters(r, me, open_filters)]
    # Anteriores: no máximo os 20 mais recentes; os outros, pelo filtro.
    history = [r for r in rows if r["status"] not in friendly.OPEN]
    done = [r for r in history if _friendly_in_filters(r, me, filters)]
    from . import live
    shown = respond + pending + done[:FRIENDLIES_HISTORY_LIMIT]
    games = {r["id"]: _friendly_card_games(db, r) for r in shown
             if r["status"] in ("reported", "suspended", "confirmed")}
    # Convite: o amigo vem já escolhido com ?com=<nick> (link "Chamar para amistoso").
    my_friends = friends.friends(db, me)
    chosen = request.args.get("com", "").strip()
    chosen_friend = next((p for p in my_friends if chosen and p["username"].casefold() == chosen.casefold()), None)
    # Sugestões do filtro "Adversário" de cada lista: quem aparece nela.
    def opponents(items):
        found = {}
        for r in items:
            other = (r["away_id"], r["away_name"]) if r["home_id"] == me else (r["home_id"], r["home_name"])
            found.setdefault(other[0], {"id": other[0], "username": other[1]})
        return sorted(found.values(), key=lambda p: (p["username"] or "").casefold())
    return render_template("tournaments/friendlies.html", pending=pending, respond=respond, friendly_games=games,
                           open_total=len(open_all), open_filters=open_filters,
                           done=done[:FRIENDLIES_HISTORY_LIMIT], done_total=len(done), has_history=bool(history),
                           filters=filters,
                           friends=my_friends, chosen=chosen, chosen_friend=chosen_friend,
                           open_opponents=opponents(open_all), history_opponents=opponents(history),
                           iso_options=iso_catalog.options(db, me), my_isos=iso_catalog.requested_by(db, me),
                           games=db.execute("SELECT id, name FROM game_titles WHERE is_active = 1 "
                                            "ORDER BY year, name").fetchall(), **_iso_limits(),
                           rules={"min_xp": friendly.MIN_XP, "limit": friendly.PAIR_DAILY_LIMIT,
                                  "rank": xp.RANKS[xp.level(friendly.MIN_XP)],
                                  "my_peak": xp.tournament_peaks(db).get(me, 0)},
                           labels=friendly.STATUS_LABELS, needs_me=friendly.needs_me)


@bp.post("/amistosos/novo")
@player_required
def friendly_new():
    validate_csrf()
    db = get_db()
    me = session["player_id"]
    try:
        friendly_id = friendly.invite(db, me, request.form.get("amigo", type=int), request.form.get("iso"),
                                      request.form.get("mensagem"), aggregate=request.form.get("agregado") == "1")
        db.commit()
    except TournamentError as error:
        db.rollback()
        flash(str(error), "error")
        return redirect(url_for("tournaments.friendlies", com=request.form.get("com", "")))
    row = friendly.get(db, friendly_id)
    notify.friendly_event(db, row, row["away_id"], f"{row['home_name']} chamou você para um amistoso",
                          f"{row['home_name']} chamou você para um amistoso"
                          + (f" de {row['game_name']}" if row["game_name"] else "") + "."
                          + (f"\nISO: {row['iso_title']} ({row['iso_filename']}). Se você não tem, baixe na página "
                             "do amistoso." if row["iso_title"] else "")
                          + ("\nPlacar agregado: no fim, os gols de todos os jogos são somados." if row["aggregate"]
                             else "")
                          + (f"\n\nRecado: {row['message']}" if row["message"] else "")
                          + "\n\nAceite ou recuse em:")
    flash(f"Convite de amistoso enviado para {row['away_name']}.", "success")
    return redirect(url_for("tournaments.friendly_show", friendly_id=friendly_id))


@bp.get("/amistosos/<int:friendly_id>")
def friendly_show(friendly_id):
    """O amistoso: os dois jogadores (e o admin) veem sempre; os outros, só depois de confirmado."""
    db = get_db()
    row = friendly.get(db, friendly_id)
    me = session.get("player_id")
    if row is None or (row["status"] != "confirmed" and me not in (row["home_id"], row["away_id"])
                       and not is_admin()):
        abort(404)
    # Os artilheiros ficam no verso do card (a bola do título).
    return render_template("tournaments/friendly.html", f=row, labels=friendly.STATUS_LABELS,
                           needs_me=friendly.needs_me, card_games=_friendly_card_games(db, row))


def _friendly_card_games(db, row):
    """O que o card do amistoso mostra das partidas: (jogos, soma, artilharia
    do verso do card - com um jogo na soma já vale)."""
    from . import live
    games, total = live.friendly_games(db, row)
    return games, total, live.friendly_scorers(db, row, games)


def _send_iso_file(stored_name, original_filename, archive_url, local_removed_at, protected=False):
    """Entrega uma ISO: do archive.org depois de confirmada lá; antes, deste
    servidor (teste local, ou pelo Nginx - X-Accel - nas ISOs dos Campeonatos)."""
    if not local_removed_at and stored_name and current_app.config["SERVE_DOWNLOADS_LOCALLY"]:
        return send_from_directory(current_app.config["DOWNLOADS_DIR"], stored_name, as_attachment=True,
                                   download_name=original_filename)
    if archive_url:
        return redirect(archive_url)
    if protected and stored_name and not local_removed_at:
        response = current_app.response_class()
        response.headers["X-Accel-Redirect"] = f"/_protected_downloads/{stored_name}"
        response.headers["Content-Disposition"] = attachment_header(original_filename)
        return response
    return None


@bp.get("/amistosos/<int:friendly_id>/iso")
@player_required
def friendly_iso(friendly_id):
    """A ISO do amistoso, para os dois jogadores (e o admin)."""
    db = get_db()
    row = friendly.get(db, friendly_id)
    if row is None or (session["player_id"] not in (row["home_id"], row["away_id"]) and not is_admin()):
        abort(404)
    response = None
    if row["iso_tournament_id"]:
        found = iso.get(db, row["iso_tournament_id"])
        if found and iso.downloadable(found, current_app):
            response = _send_iso_file(found["stored_name"], found["original_filename"], found["archive_url"],
                                      found["local_removed_at"])
    elif row["iso_catalog_id"]:
        found = iso_catalog.get(db, row["iso_catalog_id"])
        if found and found["file_id"]:
            response = _send_iso_file(found["file_stored_name"], found["file_filename"], found["file_archive_url"],
                                      found["file_local_removed_at"], protected=True)
        elif found and iso.downloadable(found, current_app):
            response = _send_iso_file(found["stored_name"], found["original_filename"], found["archive_url"],
                                      found["local_removed_at"])
    else:
        abort(404)
    if response is None:
        flash("A ISO ainda está sendo enviada para o archive.org. Tente de novo em alguns minutos.", "error")
        return redirect(url_for("tournaments.friendly_show", friendly_id=friendly_id))
    return response


@bp.post("/amistosos/isos/enviar")
def friendly_iso_upload():
    """O jogador envia a ISO que não achou na lista: vai para o archive.org e
    fica esperando a administração do WE Camp liberar."""
    # Sem @player_required + CSRF antes: o tamanho é conferido antes de ler o
    # formulário (pode ter centenas de MB), como no envio da ISO do torneio.
    if "player_id" not in session:
        return redirect(url_for("players.login", next=url_for("tournaments.friendlies")))
    back = redirect(url_for("tournaments.friendlies", _anchor="minhas-isos"))
    try:
        iso.check_size(request, current_app.config["TOURNAMENT_ISO_MAX_BYTES"])
    except TournamentError as error:
        flash(str(error), "error")
        return back
    validate_csrf()
    db = get_db()
    upload = request.files.get("iso")
    try:
        catalog_id = iso_catalog.request_upload(db, session["player_id"], request.form.get("name"),
                                                request.form.get("game_title_id", type=int),
                                                upload if upload and upload.filename else None)
        db.commit()
    except TournamentError as error:
        db.rollback()
        flash(f"A ISO não foi enviada: {error}", "error")
        return back
    app = current_app._get_current_object()
    iso.start_archive(app, catalog_id, "catalog_isos")
    notify.iso_request(db, iso_catalog.get(db, catalog_id))
    flash("ISO recebida! Ela está indo para o archive.org e a administração do WE Camp vai conferir e liberar. "
          "Acompanhe em \"Minhas ISOs enviadas\".", "success")
    return back


@bp.get("/isos/<int:catalog_id>/status")
def catalog_iso_status(catalog_id):
    """Situação do envio para o archive.org (barra de progresso) - admin ou quem enviou."""
    row = iso_catalog.get(get_db(), catalog_id)
    if row is None or not (is_admin() or (session.get("player_id") and session["player_id"] == row["requested_by"])):
        abort(404)
    if row["file_id"]:
        return jsonify({"state": "done", "progress": 100})
    return jsonify(iso.status(row, current_app))


_FRIENDLY_MESSAGES = {
    "aceitar": "Amistoso aceito! Depois do jogo, um de vocês informa o placar.",
    "recusar": "Convite recusado.", "cancelar": "Amistoso cancelado.",
    "placar": "Placar enviado. Agora o adversário precisa confirmar.",
    "rejeitar": "Você não aceitou o placar: o amistoso fica suspenso até vocês chegarem a um placar.",
}


@bp.post("/amistosos/<int:friendly_id>/<acao>")
@player_required
def friendly_action(friendly_id, acao):
    validate_csrf()
    db = get_db()
    me = session["player_id"]
    form = request.form
    try:
        if acao in ("aceitar", "recusar"):
            row = friendly.respond(db, friendly_id, me, acao == "aceitar")
        elif acao == "cancelar":
            row = friendly.cancel(db, friendly_id, me)
        elif acao == "placar":
            row = friendly.report(db, friendly_id, me, form.get("home_goals"), form.get("away_goals"))
        elif acao == "confirmar":
            # O adversário de quem saiu no meio de um jogo: a caixa "perde o triplo de XP".
            seen = form.get("quem_saiu", "")
            row = friendly.confirm(db, friendly_id, me, penalize=(form.get("penalizar") == "1") if seen else None,
                                   quitter_seen=int(seen) if seen.isdigit() else None)
        elif acao == "rejeitar":
            row = friendly.reject(db, friendly_id, me, form.get("motivo"))
        else:
            abort(404)
        db.commit()
    except TournamentError as error:
        db.rollback()
        flash(str(error), "error")
        return _back(url_for("tournaments.friendly_show", friendly_id=friendly_id))
    row = friendly.get(db, friendly_id)
    other = friendly.opponent_id(row, me)
    my_name = row["home_name"] if row["home_id"] == me else row["away_name"]
    score = friendly.score_text(row)
    mails = {
        "aceitar": (f"{my_name} aceitou o amistoso", f"{my_name} aceitou o amistoso de {row['game_name']}. "
                    "Depois do jogo, informe o placar em:"),
        "placar": (f"{my_name} informou o placar do amistoso", f"{my_name} informou o placar do amistoso: "
                   f"{score}.\n\nConfirme ou não aceite em:"),
        "rejeitar": (f"{my_name} não aceitou o placar do amistoso",
                     f"{my_name} não aceitou o placar {score}."
                     + (f"\nMotivo: {row['rejected_note']}" if row["rejected_note"] else "")
                     + "\n\nO amistoso fica suspenso até um de vocês propor um placar que o outro aceite:"),
    }
    if acao in mails:
        notify.friendly_event(db, row, other, *mails[acao])
    if acao == "confirmar" and row["status"] != "confirmed":
        flash("Confirmação registrada: falta o adversário confirmar (ou o prazo acabar).", "success")
    elif acao == "confirmar":
        flash("Placar confirmado! " + ("O amistoso vale para o Ranking e o XP." if row["ranked"]
                                       else f"Não vale para o Ranking: {row['unranked_reason']}."),
              "success" if row["ranked"] else "info")
    else:
        flash(_FRIENDLY_MESSAGES[acao], "success")
    return _back(url_for("tournaments.friendly_show", friendly_id=friendly_id))


def _send_invites(db, tournament, player_ids):
    """Grava os convites e manda os e-mails; devolve quantos foram convidados."""
    me = session.get("player_id")
    invited = friends.invite(db, tournament, me, player_ids)
    db.commit()
    if invited:
        inviter = db.execute("SELECT username FROM players WHERE id = ?", (me,)).fetchone() if me else None
        notify.tournament_invites(db, tournament, inviter[0] if inviter else "O organizador", invited)
    return len(invited)


def _invite_ids():
    return [int(v) for v in request.form.getlist("invite_ids") if v.isdigit()]


@bp.post("/<slug>/convidar")
@manager_required
def invite(tournament):
    db = get_db()
    try:
        count = _send_invites(db, tournament, _invite_ids())
    except TournamentError as error:
        db.rollback()
        flash(str(error), "error")
    else:
        flash(f"{count} convite{'s' if count != 1 else ''} enviado{'s' if count != 1 else ''}." if count
              else "Marque pelo menos um jogador que ainda não foi convidado.", "success" if count else "error")
    return redirect(url_for("tournaments.manage", slug=tournament["slug"], _anchor="convidar"))


@bp.post("/<slug>/convite/recusar")
@player_required
def invite_decline(slug):
    validate_csrf()
    tournament = _load(slug)
    db = get_db()
    friends.decline(db, tournament["id"], session["player_id"])
    db.commit()
    flash("Convite recusado.", "success")
    return _back(url_for("tournaments.my_tournaments"))


def _plain(value):
    return normalize("NFKD", value or "").encode("ascii", "ignore").decode().casefold()


def _share(tournament, enrolled):
    """Como o link do campeonato aparece ao ser compartilhado (WhatsApp, Telegram,
    Discord...): as tags og: da página (templates/_share.html) e o link que o
    botão de compartilhar copia. A descrição traz o convite para se cadastrar,
    as datas prováveis, as vagas preenchidas, a inscrição e se há prêmio (o
    prêmio sai das inscrições: sem inscrição paga não há; o valor não entra,
    porque muda a cada inscrito e os aplicativos guardam a prévia). A imagem é o
    cartão JPEG 1200 x 630 (tournament_share_image); enquanto ele não existe,
    vale a imagem como foi enviada (WebP, que nem todo aplicativo mostra)."""
    parts = []
    start, end = date_br(tournament["starts_on"]), date_br(tournament["ends_on"])
    if start and end:
        parts.append(f"Datas prováveis: {start} a {end}")
    elif start or end:
        parts.append("Datas prováveis: " + (f"início em {start}" if start else f"fim em {end}"))
    parts.append(f"Vagas Preenchidas: {enrolled} de {tournament['max_players']}")
    fee = tournament["entry_fee_cents"]
    parts.append("Inscrição: " + (format_cents(fee) if fee else "Grátis"))
    parts.append("Prêmio: " + ("Moeda W$ entre os primeiros colocados." if fee else "Não possui."))
    image = tournament["share_image"] or tournament["image"]
    return {
        "url": absolute_url(url_for("tournaments.show", slug=tournament["slug"])),
        "title": tournament["name"],
        # O convite na frente: o WhatsApp corta o fim da descrição quando ela não cabe.
        "description": "Cadastre-se, participe, ganhe W$ e suba seu Rank! " + " · ".join(parts),
        "image": absolute_url(images.url("torneio", image)) if image else None,
        "image_alt": f"Imagem do campeonato {tournament['name']}",
        "card": bool(tournament["share_image"]),
    }


@bp.get("/<slug>")
def show(slug):
    db = get_db()
    tournament = _load(slug, private_ok=True)
    # "Somente amigos" aberto por quem não é amigo (o link compartilhado): a página avisa
    # que o campeonato é privado e mostra só a Visão geral - sem as outras abas, a ISO e
    # a inscrição (que o store.enroll também recusa). O resto do campeonato continua 404.
    outsider = not can_view(db, tournament)
    entries = store.entries_of(db, tournament["id"])
    player_id = session.get("player_id")
    my_entry = next((e for e in entries if e["player_id"] == player_id), None)
    stages = [] if outsider else _stage_views(db, tournament)
    config = store.settings_of(tournament)
    # Encerrado abre na Visão geral, com os vencedores; em andamento, na tabela.
    tab = request.args.get("aba", "tabela" if stages and tournament["status"] != "finished" else "geral")
    if outsider or tab not in ("geral", "tabela", "jogos", "artilharia") or (tab == "artilharia" and not stages):
        tab = "geral"
    pool = store.prize_pool(db, tournament["id"])
    split = engine.parse_split(tournament["prize_split"])
    names = _names(db, tournament)
    my_pending = []
    if my_entry and tournament["status"] == "running":
        my_pending = db.execute(
            "SELECT m.* FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
            "WHERE s.tournament_id = ? AND s.status = 'running' AND m.status != 'confirmed' "
            "AND ? IN (m.home_entry_id, m.away_entry_id) ORDER BY m.round, m.leg, m.id",
            (tournament["id"], my_entry["id"])).fetchall()
    # Times da lista (modos choose/draw): todos, com o dono quando já tem.
    team_list = [dict(row, image=team_image(row)) for row in db.execute(
        "SELECT t.*, p.username AS owner FROM tournament_teams tt JOIN teams t ON t.id = tt.team_id "
        "LEFT JOIN tournament_entries e ON e.tournament_id = tt.tournament_id AND e.team_id = t.id "
        "LEFT JOIN players p ON p.id = e.player_id WHERE tt.tournament_id = ? ORDER BY t.name COLLATE NOCASE",
        (tournament["id"],))]
    # Modo livre: a base de times vira sugestão do campo, e o nome digitado que
    # bate com um time da base usa esse time (store._entry_team).
    free_teams = []
    # (Também para o inscrito sem time: store.choose_team.)
    if (tournament["team_mode"] == "free" and tournament["status"] == "registration" and player_id and not outsider
            and (not my_entry or not my_entry["team_name"])):
        free_teams = [{"name": row["name"], "image": team_image(row)} for row in db.execute(
            "SELECT * FROM teams WHERE is_active = 1 ORDER BY name COLLATE NOCASE")]
    iso_row = None if outsider else iso.get(db, tournament["id"])
    # Campeonato em andamento: na aba Tabela, "Ao vivo", o placar dos jogos
    # rolando agora e a classificação com os placares ainda não confirmados;
    # na aba Jogos, o link "Ao vivo" no jogo que está rolando.
    live_table = None
    if tab == "tabela" and tournament["status"] == "running":
        live_table, stages = _live_table_view(db, tournament)
    elif tab == "jogos" and tournament["status"] == "running":
        from . import live
        live_table = live.tournament_live(db, tournament["id"])
    scorers = None
    if tab == "artilharia":
        from . import live
        scorers = live.tournament_scorers(db, tournament["id"])
    return render_template(
        "tournaments/show.html", t=tournament, entries=entries, my_entry=my_entry, stages=stages, tab=tab,
        live_table=live_table, confirm_hours=setting(db, "torneios_confirmacao_horas"), scorers=scorers,
        team_list=team_list, free_teams=free_teams, team_modes=TEAM_MODES, crests=_crests(names), my_pending=my_pending,
        names=names, can_manage=can_manage(tournament), iso=iso_row, iso_ready=iso.downloadable(iso_row, current_app),
        format_label=formats.FORMATS[tournament["format"]], summary=formats.describe(tournament["format"], config),
        status_labels=STATUS_LABELS, pool=pool, split=split, share=_share(tournament, len(entries)),
        outsider=outsider, prizes=engine.split_prize(pool, split, 3) if pool else [],
        podium=sorted((e for e in entries if e["final_position"]), key=lambda e: e["final_position"]),
        invite=(friends.my_invite(db, tournament["id"], player_id)
                if player_id and not my_entry and not outsider else None))


@bp.get("/<slug>/tabela-ao-vivo")
def live_tables(slug):
    """As tabelas de grupos/pontos corridos da aba Tabela, que ela recarrega a
    cada poucos segundos (static/js/live-table.js) enquanto o campeonato anda."""
    db = get_db()
    tournament = _load(slug)
    if tournament["status"] != "running":
        return ""
    live_table, stages = _live_table_view(db, tournament)
    return render_template("tournaments/_live_tables.html", t=tournament, stages=stages, live_table=live_table,
                           names=_names(db, tournament), confirm_hours=setting(db, "torneios_confirmacao_horas"))


# --- Jogador: criar, inscrever-se, carteira -----------------------------------

def _team_catalog(db):
    """Grupos de times ativos com as imagens, para escolher os times do torneio."""
    groups = []
    for group in db.execute("SELECT * FROM team_groups ORDER BY seq, name"):
        teams = [dict(row, image=team_image(row)) for row in db.execute(
            "SELECT t.* FROM team_group_members m JOIN teams t ON t.id = m.team_id "
            "WHERE m.group_id = ? AND t.is_active = 1 ORDER BY t.name COLLATE NOCASE", (group["id"],))]
        if teams:
            groups.append({"group": group, "teams": teams})
    return groups


LOOSE_GROUP_SLUG = "avulsos"


def _loose_group(db):
    """Grupo "Times avulsos": times criados por quem monta um torneio (fora da base)."""
    db.execute("INSERT OR IGNORE INTO team_groups (slug, name, seq, created_at) VALUES (?, 'Times avulsos', 900, ?)",
               (LOOSE_GROUP_SLUG, now()))
    return db.execute("SELECT id FROM team_groups WHERE slug = ?", (LOOSE_GROUP_SLUG,)).fetchone()[0]


@bp.post("/times-avulsos")
@player_required
def loose_team_new():
    """Cria (ou reaproveita, se o nome já existe) um time avulso; usado pelo Criar torneio.
    Fica na base para os próximos torneios; a administração corrige nome e escudo em Times."""
    validate_csrf()
    db = get_db()
    name = " ".join(request.form.get("name", "").split())[:60]
    if len(name) < 2:
        return {"error": "Digite o nome do time (pelo menos 2 letras)."}, 400
    existing = next((row for row in db.execute("SELECT * FROM teams WHERE is_active = 1")
                     if _plain(row["name"]) == _plain(name)), None)
    if existing:
        team_id = existing["id"]
        # Time ativo fora de qualquer grupo não aparece na lista: entra nos avulsos.
        if not db.execute("SELECT 1 FROM team_group_members WHERE team_id = ?", (team_id,)).fetchone():
            db.execute("INSERT INTO team_group_members (group_id, team_id) VALUES (?, ?)", (_loose_group(db), team_id))
            db.commit()
    else:
        team_id = db.execute("INSERT INTO teams (name, kind, created_at, updated_at) VALUES (?, 'custom', ?, ?)",
                             (name, now(), now())).lastrowid
        db.execute("INSERT INTO team_group_members (group_id, team_id) VALUES (?, ?)", (_loose_group(db), team_id))
        db.commit()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    return {"id": team["id"], "name": team["name"], "image": team_image(team), "existing": bool(existing)}


def _form_config(form, fmt):
    raw = {key: form.get(key) for key in formats.DEFAULTS[fmt] if form.get(key) is not None}
    raw["_form"] = "1"
    return formats.normalize(fmt, raw)


def _games(db):
    return db.execute("SELECT * FROM game_titles WHERE is_active = 1 ORDER BY year, name").fetchall()


def _tournament_form(db, form, games, max_fee, keep_team_ids=()):
    """O formulário de criar campeonato (e o de editar antes do início) validado:
    dict com os valores ou TournamentError/ValueError com a mensagem.
    keep_team_ids: times que já estão no campeonato valem mesmo inativos."""
    name = form.get("name", "").strip()
    if not 3 <= len(name) <= 80:
        raise TournamentError("O nome precisa ter entre 3 e 80 caracteres.")
    game_id = int(form.get("game_title_id") or 0)
    if not any(g["id"] == game_id for g in games):
        raise TournamentError("Escolha o jogo base.")
    fmt = form.get("format", "")
    if fmt not in formats.FORMATS:
        raise TournamentError("Escolha um formato.")
    max_players = int(form.get("max_players") or 0)
    if max_players < 2:
        raise TournamentError("O campeonato precisa de pelo menos 2 vagas.")
    fee = parse_cents(form.get("entry_fee") or "0")
    if fee > max_fee:
        raise TournamentError(f"A inscrição pode ser de no máximo {format_cents(max_fee)}.")
    # Sem inscrição não há prêmio para dividir: o campo nem aparece.
    split = form.get("prize_split", "") if fee else PRIZE_SPLITS[0]
    if split not in PRIZE_SPLITS:
        raise TournamentError("Escolha a divisão do prêmio.")
    config = _form_config(form, fmt)
    error = formats.config_error(fmt, config, max_players)
    if error:
        raise TournamentError(f"Com {max_players} vagas: {error}")
    starts_on, ends_on = _dates(form)
    if not starts_on:
        raise TournamentError("Informe o início provável.")
    rules = form.get("rules", "").strip()
    if not rules:
        raise TournamentError("Escreva o regulamento / observações.")
    team_mode = form.get("team_mode", "free")
    if team_mode not in TEAM_MODES:
        raise TournamentError("Escolha como serão os times.")
    team_ids = []
    if team_mode != "free":
        wanted = {int(v) for v in form.getlist("team_ids") if v.isdigit()}
        if wanted:
            keep = [i for i in keep_team_ids if i in wanted]
            team_ids = [row[0] for row in db.execute(
                f"SELECT id FROM teams WHERE (is_active = 1 OR id IN ({','.join('?' * len(keep)) or 'NULL'})) "
                f"AND id IN ({','.join('?' * len(wanted))}) ORDER BY id", [*keep, *wanted])]
        if len(team_ids) < max_players:
            raise TournamentError(f"Marque pelo menos {max_players} times na lista (um por vaga); "
                                  f"há {len(team_ids)} marcados.")
    return {"name": name, "game_id": game_id, "format": fmt, "max_players": max_players, "fee": fee, "split": split,
            "config": config, "starts_on": starts_on, "ends_on": ends_on, "rules": rules[:5000],
            "team_mode": team_mode, "friends_only": 1 if form.get("friends_only") else 0, "team_ids": team_ids}


def _form_values(db, tournament):
    """Os dados do campeonato como o formulário de criar os manda (strings), para
    editar antes do início com o mesmo formulário."""
    values = {"name": tournament["name"], "starts_on": tournament["starts_on"] or "",
              "ends_on": tournament["ends_on"] or "", "game_title_id": str(tournament["game_title_id"]),
              "format": tournament["format"], "max_players": str(tournament["max_players"]),
              "entry_fee": format_cents(tournament["entry_fee_cents"]), "prize_split": tournament["prize_split"],
              "team_mode": tournament["team_mode"], "rules": tournament["rules"] or "",
              "friends_only": "on" if tournament["friends_only"] else ""}
    for key, value in store.settings_of(tournament).items():
        values[key] = ("on" if value else "") if isinstance(value, bool) else str(value)
    return values


@bp.route("/novo", methods=("GET", "POST"))
@player_required
def new():
    db = get_db()
    games = _games(db)
    max_fee = setting(db, "torneios_inscricao_max")
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        validate_csrf()
        try:
            values = _tournament_form(db, form, games, max_fee)
            name, game_id, fmt, max_players, fee, split, config, starts_on, ends_on, rules, team_mode, \
                friends_only, team_ids = (values[key] for key in (
                    "name", "game_id", "format", "max_players", "fee", "split", "config", "starts_on", "ends_on",
                    "rules", "team_mode", "friends_only", "team_ids"))
            if not is_admin():
                open_count = db.execute("SELECT COUNT(*) FROM tournaments WHERE organizer_id = ? AND status IN "
                                        "('draft', 'registration', 'running')", (session["player_id"],)).fetchone()[0]
                limit = setting(db, "torneios_abertos_por_jogador")
                if open_count >= limit:
                    raise TournamentError(f"Você já tem {open_count} campeonato(s) em aberto (o limite é {limit}). "
                                          "Encerre ou cancele um antes de criar outro.")
            # Por último: só grava o arquivo quando o resto do formulário passou.
            upload = request.files.get("image")
            image_name = images.save("torneio", upload) if upload and upload.filename else None
        except (TournamentError, images.ImageError, ValueError) as error:
            flash(str(error), "error")
        else:
            tournament_id = db.execute(
                "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
                "entry_fee_cents, prize_split, settings_json, rules, team_mode, team_display, starts_on, ends_on, "
                "image, friends_only, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'registration', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"tmp-{now()}-{session['player_id']}", name, game_id, session["player_id"], fmt, max_players, fee,
                 split, json.dumps(config), rules[:5000], team_mode, "flag", starts_on,
                 ends_on, image_name, friends_only, now(), now())).lastrowid
            slug = f"{slugify(name)}-{tournament_id}"
            db.execute("UPDATE tournaments SET slug = ? WHERE id = ?", (slug, tournament_id))
            db.executemany("INSERT INTO tournament_teams (tournament_id, team_id) VALUES (?, ?)",
                           [(tournament_id, team_id) for team_id in team_ids])
            db.commit()
            invited = _send_invites(db, store.get_tournament(db, tournament_id), _invite_ids())
            flash("Campeonato criado com inscrições abertas" + (f" e {invited} convite(s) enviado(s)" if invited else "")
                  + ". Agora envie a ISO do jogo (com o patch do campeonato) aqui em Gerenciar; dá para trocar quando "
                  "quiser.", "success")
            return redirect(url_for("tournaments.manage", slug=slug, _anchor="iso"))
    selected = {int(v) for v in form.getlist("team_ids") if v.isdigit()} if form else set()
    return render_template("tournaments/new.html", games=games, form=form, max_fee=max_fee,
                           format_labels=formats.FORMATS, defaults=formats.DEFAULTS, splits=PRIZE_SPLITS,
                           min_players=formats.MIN_PLAYERS, max_players=formats.MAX_PLAYERS,
                           team_modes=TEAM_MODES, catalog=_team_catalog(db), selected=selected,
                           candidates=friends.candidates(db, session["player_id"]), invite_selected=set(_invite_ids()))


@bp.post("/<slug>/inscrever")
@player_required
def enroll(slug):
    validate_csrf()
    tournament = _load(slug)
    team_id = request.form.get("team_id", type=int)
    _run(lambda db, t: store.enroll(db, t, session["player_id"], request.form.get("team_name", ""), team_id),
         tournament, "Inscrição feita!")
    return redirect(url_for("tournaments.show", slug=slug, aba="geral"))


@bp.post("/<slug>/meu-time")
@player_required
def choose_team(slug):
    """O inscrito sem time (o dele saiu da lista ou o modo de times mudou) escolhe outro."""
    validate_csrf()
    tournament = _load(slug)
    entry = get_db().execute("SELECT * FROM tournament_entries WHERE tournament_id = ? AND player_id = ?",
                             (tournament["id"], session["player_id"])).fetchone()
    if entry:
        team_id = request.form.get("team_id", type=int)
        _run(lambda db, t: store.choose_team(db, t, entry, request.form.get("team_name", ""), team_id),
             tournament, "Time escolhido!")
    return redirect(url_for("tournaments.show", slug=slug, aba="geral"))


@bp.post("/<slug>/sair")
@player_required
def leave(slug):
    validate_csrf()
    tournament = _load(slug)
    entry = get_db().execute("SELECT * FROM tournament_entries WHERE tournament_id = ? AND player_id = ?",
                             (tournament["id"], session["player_id"])).fetchone()
    if entry:
        _run(lambda db, t: store.withdraw(db, t, entry), tournament, "Você saiu do campeonato; a inscrição foi devolvida.")
    return redirect(url_for("tournaments.show", slug=slug, aba="geral"))


@bp.get("/carteira")
@player_required
def wallet():
    db = get_db()
    if ensure_wallet(db, session["player_id"]):
        db.commit()
    player = db.execute("SELECT username, wallet_cents FROM players WHERE id = ?", (session["player_id"],)).fetchone()
    entries = db.execute(
        "SELECT w.*, t.name AS tournament_name, t.slug FROM wallet_entries w "
        "LEFT JOIN tournaments t ON t.id = w.tournament_id WHERE w.player_id = ? ORDER BY w.id DESC LIMIT 200",
        (session["player_id"],)).fetchall()
    return render_template("tournaments/wallet.html", player=player, entries=entries, kinds=KINDS,
                           values={key: setting(db, key) for key in MONEY_SETTINGS},
                           chart=_wallet_chart(player["wallet_cents"], entries))


def _wallet_chart(current, entries):
    """Saldo depois de cada movimento do extrato (gráfico da Carteira W$), do mais
    antigo ao mais recente. `entries` vem do mais recente para o mais antigo: o
    saldo é refeito de trás para frente a partir do saldo atual. O eixo vai do
    menor ao maior saldo (com marcações redondas), não do zero: perto do saldo
    inicial de W$1.000, os movimentos de centavos sumiriam."""
    running, points = current, []
    for e in entries:
        description = KINDS.get(e["kind"], e["kind"])
        if e["tournament_name"]:
            description += f" · {e['tournament_name']}"
        elif e["note"]:
            description += f" · {e['note']}"
        amount = e["amount_cents"]
        points.append({"balance": running, "balance_label": format_cents(running),
                       "amount_label": ("+" if amount > 0 else "") + format_cents(amount), "positive": amount > 0,
                       "description": description, "date": format_local(e["created_at"])})
        running -= amount
    points.reverse()
    if not points:
        return {"points": []}
    low, high = min(p["balance"] for p in points), max(p["balance"] for p in points)
    # Passo "redondo" (1, 2, 2,5 ou 5 x 10^n centavos) para umas 4 faixas.
    raw = max((high - low) / 4, 25)
    magnitude = 10 ** (len(str(int(raw))) - 1)
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    low, high = (low // step) * step, -(-high // step) * step
    if high == low:
        high = low + step
    ticks, value = [], low
    while value <= high + 1e-9:
        ticks.append({"value": value, "label": format_cents(round(value))})
        value += step
    return {"points": points, "low": low, "high": high, "ticks": ticks}


# --- Organizador --------------------------------------------------------------

@bp.get("/<slug>/gerenciar")
def manage(slug):
    tournament = _load(slug)
    if not can_manage(tournament):
        abort(403)
    db = get_db()
    stages = _stage_views(db, tournament)
    current = stages[-1] if stages else None
    decisive = {}  # último jogo de cada confronto -> gols dos outros jogos (pênaltis só com empate)
    if current and current["stage"]["kind"] == "knockout":
        for rnd in current["bracket"]:
            for item in rnd["items"]:
                if item["legs"]:
                    decisive[item["legs"][-1]["id"]] = store.other_legs_goals(db, item["legs"][-1])
    can_advance = can_finish = False
    if current and tournament["status"] == "running":
        complete = store.stage_complete(db, current["stage"])
        next_stage = store.has_next_stage(tournament, current["stage"])
        can_advance, can_finish = complete and next_stage, complete and not next_stage
    open_reports = db.execute(
        "SELECT m.* FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
        "WHERE s.tournament_id = ? AND m.status IN ('disputed', 'reported') ORDER BY m.status = 'reported', m.id",
        (tournament["id"],)).fetchall()
    my_entry = db.execute("SELECT id FROM tournament_entries WHERE tournament_id = ? AND player_id = ?",
                          (tournament["id"], session.get("player_id"))).fetchone()
    iso_row = iso.get(db, tournament["id"])
    entries = store.entries_of(db, tournament["id"])
    # Antes do início: o formulário de criar, com os dados atuais (edit). Os times do
    # campeonato fora dos grupos da base (inativos) aparecem num grupo à parte.
    edit_form = None
    if tournament["status"] == "registration":
        selected = {row[0] for row in db.execute("SELECT team_id FROM tournament_teams WHERE tournament_id = ?",
                                                 (tournament["id"],))}
        catalog = _team_catalog(db)
        shown = {team["id"] for group in catalog for team in group["teams"]}
        extra = [dict(row, image=team_image(row)) for row in db.execute(
            "SELECT t.* FROM tournament_teams tt JOIN teams t ON t.id = tt.team_id WHERE tt.tournament_id = ? "
            "ORDER BY t.name COLLATE NOCASE", (tournament["id"],)) if row["id"] not in shown]
        if extra:
            catalog.append({"group": {"name": "Outros times deste campeonato", "slug": "outros"}, "teams": extra})
        organizer = tournament["organizer_id"]
        edit_form = {"games": _games(db), "max_fee": setting(db, "torneios_inscricao_max"),
                     "form": _form_values(db, tournament), "catalog": catalog, "selected": selected,
                     # O aviso antes de salvar (quantos inscritos saem ou ficam sem time).
                     "edit_entries": [{"team": e["team_id"], "named": bool(e["team_name"]),
                                  "friend": e["player_id"] == organizer or store.are_friends(db, e["player_id"], organizer)}
                                 for e in entries]}
    entry_search = request.args.get("inscrito", "").strip()
    shown_entries = [e for e in entries if entry_search.lower() in " ".join(
        str(e[key] or "") for key in ("username", "team_name")).lower()] if entry_search else entries
    return render_template("tournaments/manage.html", t=tournament, entries=entries, shown_entries=shown_entries,
                           entry_search=entry_search, editing=edit_form is not None, **(edit_form or {}),
                           format_labels=formats.FORMATS,
                           defaults=formats.DEFAULTS, splits=PRIZE_SPLITS, min_players=formats.MIN_PLAYERS,
                           max_players=formats.MAX_PLAYERS, team_modes=TEAM_MODES,
                           open_reports=open_reports, my_entry_id=None if is_admin() or not my_entry else my_entry[0],
                           current=current, stages=stages, names=_names(db, tournament), decisive=decisive,
                           auto_finish_at=notify.auto_finish_at(tournament),
                           can_advance=can_advance, can_finish=can_finish, is_admin=is_admin(),
                           status_labels=STATUS_LABELS, iso=iso_row, iso_uploading=iso.uploading(iso_row),
                           archive_configured=iso.archive_configured(current_app), **_iso_limits(),
                           candidates=friends.candidates(db, tournament["organizer_id"], tournament["id"],
                                                         tournament["friends_only"])
                           if tournament["status"] == "registration" else None,
                           invited=friends.invite_status(db, tournament["id"]), invite_labels=friends.INVITE_LABELS)


@bp.post("/<slug>/editar")
@manager_required
def edit(tournament):
    db = get_db()
    if tournament["status"] == "registration":
        # Antes do início: tudo o que se escolheu ao criar (store.update_before_start
        # cuida de quem já está inscrito e manda os avisos).
        current = store.get_tournament(db, tournament["id"])
        keep = [row[0] for row in db.execute("SELECT team_id FROM tournament_teams WHERE tournament_id = ?",
                                             (tournament["id"],))]
        try:
            values = _tournament_form(db, request.form, _games(db), setting(db, "torneios_inscricao_max"), keep)
            notices = store.update_before_start(db, current, values, session.get("player_id"))
        except (TournamentError, InsufficientFunds, ValueError) as error:
            db.rollback()
            flash(str(error), "error")
        else:
            db.commit()
            notify.entry_notices(db, store.get_tournament(db, tournament["id"]), notices)
            players = len({player for player, _, _ in notices})
            flash("Campeonato atualizado." + (f" {players} inscrito(s) avisado(s) por e-mail." if players else ""),
                  "success")
        return redirect(url_for("tournaments.manage", slug=tournament["slug"], _anchor="dados"))
    name = request.form.get("name", "").strip()
    try:
        if not 3 <= len(name) <= 80:
            raise TournamentError("O nome precisa ter entre 3 e 80 caracteres.")
        starts_on, ends_on = _dates(request.form)
    except TournamentError as error:
        flash(str(error), "error")
    else:
        db = get_db()
        db.execute("UPDATE tournaments SET name = ?, rules = ?, starts_on = ?, ends_on = ?, friends_only = ?, "
                   "updated_at = ? WHERE id = ?",
                   (name, request.form.get("rules", "").strip()[:5000], starts_on, ends_on,
                    1 if request.form.get("friends_only") else 0, now(), tournament["id"]))
        db.commit()
        flash("Campeonato atualizado.", "success")
    return redirect(url_for("tournaments.manage", slug=tournament["slug"]))


@bp.post("/<slug>/imagem")
@manager_required
def tournament_image(tournament):
    """Envia, troca ou remove a imagem do torneio (organizador ou admin)."""
    db = get_db()
    old = tournament["image"]
    if request.form.get("acao") == "remover":
        new_name, message = None, "Imagem do campeonato removida."
    else:
        upload = request.files.get("image")
        if not upload or not upload.filename:
            flash("Escolha uma imagem.", "error")
            return redirect(url_for("tournaments.manage", slug=tournament["slug"], _anchor="imagem"))
        try:
            new_name = images.save("torneio", upload)
        except images.ImageError as error:
            flash(str(error), "error")
            return redirect(url_for("tournaments.manage", slug=tournament["slug"], _anchor="imagem"))
        message = "Imagem do campeonato salva."
    # O cartão do link compartilhado era da imagem antiga: o navegador monta o da
    # nova ao voltar para Gerenciar (tournament_share_image).
    db.execute("UPDATE tournaments SET image = ?, share_image = NULL, updated_at = ? WHERE id = ?",
               (new_name, now(), tournament["id"]))
    db.commit()
    images.delete("torneio", old)
    images.delete("torneio", tournament["share_image"])
    flash(message, "success")
    return redirect(url_for("tournaments.manage", slug=tournament["slug"], _anchor="imagem"))


@bp.post("/<slug>/imagem-compartilhar")
@manager_required
def tournament_share_image(tournament):
    """Guarda a imagem do campeonato como ela aparece no link compartilhado: um
    JPEG 1200 x 630 que o navegador de quem gerencia monta a partir da imagem
    (static/js/share-image.js) ao abrir o campeonato ou o Gerenciar. A imagem é
    guardada em WebP, que WhatsApp e Telegram não mostram, e o servidor não
    mexe em imagem (app/images.py). `origem` é a imagem de que o cartão saiu:
    se ela já foi trocada, o cartão não serve mais."""
    if not tournament["image"] or request.form.get("origem") != tournament["image"]:
        return "", 409
    upload = request.files.get("image")
    try:
        if not upload or not upload.filename:
            raise images.ImageError("Faltou a imagem.")
        new_name = images.save("torneio", upload, only=(".jpg",))
    except images.ImageError as error:
        return str(error), 400
    db = get_db()
    db.execute("UPDATE tournaments SET share_image = ? WHERE id = ?", (new_name, tournament["id"]))
    db.commit()
    images.delete("torneio", tournament["share_image"])
    return "", 204


# --- ISO do torneio (app/tournament/iso.py) ----------------------------------

def _iso_limits():
    return {"iso_max_bytes": current_app.config["TOURNAMENT_ISO_MAX_BYTES"],
            "iso_accept": ",".join(f".{e}" for e in sorted(iso.EXTENSIONS))}


def _save_iso(tournament, upload):
    """Guarda a ISO e começa o envio para o archive.org (mensagem nos dois casos)."""
    db = get_db()
    try:
        iso.check_upload(upload)
        iso.save(db, tournament, upload, session.get("player_id"))
        db.commit()
    except TournamentError as error:
        db.rollback()
        flash(f"A ISO não foi aceita: {error}", "error")
        return
    app = current_app._get_current_object()
    if iso.archive_configured(app):
        flash("ISO recebida. Ela está sendo enviada para o archive.org; o download fica disponível quando o envio "
              "terminar.", "success")
    else:
        flash("ISO recebida. O archive.org não está configurado neste servidor: a ISO fica só aqui.", "success")
    iso.start_archive(app, tournament["id"])


@bp.post("/<slug>/iso")
def iso_upload(slug):
    # Sem @manager_required: o tamanho é conferido antes de ler o formulário
    # (o CSRF lê o corpo inteiro, que pode ter centenas de MB).
    tournament = _load(slug)
    if not can_manage(tournament):
        abort(403)
    try:
        iso.check_size(request, current_app.config["TOURNAMENT_ISO_MAX_BYTES"])
    except TournamentError as error:
        flash(str(error), "error")
    else:
        validate_csrf()
        upload = request.files.get("iso")
        if upload is None or not upload.filename:
            flash("Selecione o arquivo da ISO.", "error")
        else:
            _save_iso(tournament, upload)
    return redirect(url_for("tournaments.manage", slug=slug))


@bp.post("/<slug>/iso/archive-org")
@manager_required
def iso_retry(tournament):
    row = iso.get(get_db(), tournament["id"])
    if row is None or row["local_removed_at"]:
        flash("A ISO já está no archive.org.", "error")
    elif iso.uploading(row):
        flash("O envio para o archive.org já está em andamento.", "error")
    elif not iso.archive_configured(current_app):
        flash("O archive.org não está configurado neste servidor.", "error")
    else:
        iso.start_archive(current_app._get_current_object(), tournament["id"])
        flash("Enviando a ISO para o archive.org de novo.", "success")
    return redirect(url_for("tournaments.manage", slug=tournament["slug"]))


@bp.get("/<slug>/iso/status")
def iso_status(slug):
    """Situação do envio da ISO do torneio para o archive.org (barra de progresso em Gerenciar)."""
    tournament = _load(slug)
    if not can_manage(tournament):
        abort(403)
    return jsonify(iso.status(iso.get(get_db(), tournament["id"]), current_app))


@bp.get("/<slug>/iso")
def iso_download(slug):
    """Só para quem está logado. Depois de confirmada no archive.org, a cópia
    deste servidor não existe mais: redireciona para lá."""
    tournament = _load(slug)
    if "player_id" not in session and not is_admin():
        return redirect(url_for("players.login", next=request.path))
    row = iso.get(get_db(), tournament["id"])
    if row is None:
        abort(404)
    if not iso.downloadable(row, current_app):
        flash("A ISO ainda está sendo enviada para o archive.org. Tente de novo em alguns minutos.", "error")
        return redirect(url_for("tournaments.show", slug=slug, aba="geral"))
    if not row["local_removed_at"] and current_app.config["SERVE_DOWNLOADS_LOCALLY"]:
        return send_from_directory(current_app.config["DOWNLOADS_DIR"], row["stored_name"], as_attachment=True,
                                   download_name=row["original_filename"])
    return redirect(row["archive_url"])


@bp.post("/<slug>/iniciar")
@manager_required
def start(tournament):
    ok = _run(store.start, tournament, "Sorteio feito: o campeonato começou!")
    if ok:
        notify.tournament_started(get_db(), store.get_tournament(get_db(), tournament["id"]))
    return redirect(url_for("tournaments.show", slug=tournament["slug"]) if ok
                    else url_for("tournaments.manage", slug=tournament["slug"]))


@bp.post("/<slug>/inscritos/<int:entry_id>/remover")
@manager_required
def remove_entry(tournament, entry_id):
    entry = get_db().execute("SELECT * FROM tournament_entries WHERE id = ? AND tournament_id = ?",
                             (entry_id, tournament["id"])).fetchone()
    if entry:
        # O convite sai junto: o jogador volta a aparecer sem marcação em "Convidar jogadores"
        # e o organizador pode convidá-lo de novo se quiser.
        def remove(db, t):
            store.withdraw(db, t, entry)
            friends.remove_invite(db, t["id"], entry["player_id"])
        _run(remove, tournament, "Inscrição removida e devolvida ao jogador.")
    return redirect(url_for("tournaments.manage", slug=tournament["slug"]))


def _score_form():
    return tuple(_int_or_none(request.form.get(key)) for key in ("home_goals", "away_goals", "home_pens", "away_pens"))


@bp.post("/<slug>/partidas/<int:match_id>")
@manager_required
def result(tournament, match_id):
    """Organizador/admin define, corrige ou apaga o resultado."""
    db = get_db()
    back = request.form.get("voltar") == "jogo"
    target = (url_for("tournaments.match", slug=tournament["slug"], match_id=match_id) if back
              else url_for("tournaments.manage", slug=tournament["slug"]) + f"#jogo-{match_id}")
    match = db.execute("SELECT * FROM tournament_matches WHERE id = ?", (match_id,)).fetchone()
    if match and not is_admin() and reports.player_entry(db, tournament, match, session.get("player_id")):
        flash("Você joga esta partida: reporte o placar como jogador. Contestações dos seus jogos são decididas "
              "pela administração do WE Camp.", "error")
        return redirect(target)
    user_id = session.get("user_id") if is_admin() else None
    organizer = None if user_id else db.execute(
        "SELECT * FROM tournament_entries WHERE tournament_id = ? AND player_id = ?",
        (tournament["id"], session.get("player_id"))).fetchone()
    comment = request.form.get("comment", "")
    if request.form.get("acao") == "apagar":
        _run(lambda db, t: reports.clear(db, t, match_id, user_id=user_id, entry=organizer), tournament,
             "Resultado apagado." if match and match["status"] == "confirmed"
             else "Reporte descartado: os jogadores podem enviar um novo placar.")
    else:
        try:
            score = _score_form()
        except TournamentError as error:
            flash(str(error), "error")
        else:
            _run(lambda db, t: reports.resolve(db, t, match_id, score, user_id=user_id, entry=organizer,
                                               comment=comment), tournament, "Resultado salvo.")
    notify.check_completion(db, tournament)
    return redirect(target)


# --- Jogador: reportar, confirmar, contestar ----------------------------------

def _match_or_404(db, tournament, match_id):
    match = db.execute(
        "SELECT m.*, s.name AS stage_name, s.status AS stage_status, s.kind AS stage_kind, s.tournament_id "
        "FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id WHERE m.id = ?",
        (match_id,)).fetchone()
    if match is None or match["tournament_id"] != tournament["id"]:
        abort(404)
    return match


@bp.get("/<slug>/jogos/<int:match_id>")
def match(slug, match_id):
    db = get_db()
    tournament = _load(slug)
    m = _match_or_404(db, tournament, match_id)
    names = _names(db, tournament)
    me = reports.player_entry(db, tournament, m, session.get("player_id")) if session.get("player_id") else None
    manager = can_manage(tournament) and (is_admin() or me is None)
    sides = {row["id"]: row for row in db.execute(
        "SELECT e.id, p.id AS player_id, p.username FROM tournament_entries e JOIN players p ON p.id = e.player_id "
        "WHERE e.id IN (?, ?)", (m["home_entry_id"], m["away_entry_id"]))}
    home, away = sides.get(m["home_entry_id"]), sides.get(m["away_entry_id"])
    quitter = next((s["username"] for s in sides.values() if m["quitter_id"] and s["player_id"] == m["quitter_id"]), None)
    h2h = {"home": home["username"], "away": away["username"],
           "record": stats.player_record(db, home["player_id"], opponent_id=away["player_id"])} if home and away else None
    live_now = None
    if tournament["status"] == "running" and m["status"] != "confirmed":
        from . import live
        rolling = live.tournament_live(db, tournament["id"])
        if match_id in rolling["matches"]:
            live_now = {"score": rolling["matches"][match_id], "url": rolling["match_urls"][match_id],
                        "id": rolling["live_ids"][match_id]}
    # Quem fez os gols e deu as assistências e o card no Ao vivo: da partida
    # rolando agora ou da lida do jogo com o placar confirmado (ou o reportado,
    # ainda esperando a confirmação).
    reported = reports.last_report(db, match_id) if m["status"] in ("reported", "disputed") else None
    from . import live
    goals = live.match_goals(db, m, (m["home_goals"], m["away_goals"]) if m["status"] == "confirmed"
                             else (reported["home_goals"], reported["away_goals"]) if reported else None,
                             live_now["id"] if live_now else None)
    return render_template(
        "tournaments/match.html", t=tournament, m=m, names=names, me=me, manager=manager, h2h=h2h, live_now=live_now,
        quitter=quitter, goals=goals,
        confirm_hours=setting(db, "torneios_confirmacao_horas"),
        history=reports.history(db, match_id), labels=reports.ACTION_LABELS,
        disputer=reports.disputer(db, match_id) if m["status"] == "disputed" else None,
        reported=reported,
        confirmed_by=reports.confirmed_by(db, match_id) if m["status"] == "reported" else [],
        dispute=reports.last_dispute(db, match_id) if m["status"] == "disputed" else None,
        deadline=reports.deadline(db, m) if m["status"] == "reported" else None,
        decisive=store.needs_pens(db, m), pens_agg=store.other_legs_goals(db, m), open=tournament["status"] == "running" and m["stage_status"] == "running")


def _player_action(slug, match_id, action, success, after=None):
    """Ação do jogador num jogo; depois do commit, os e-mails (`after`) e o aviso de fim do torneio."""
    tournament = _load(slug)
    validate_csrf()
    if _run(lambda db, t: action(db, t), tournament, success):
        if after:
            after(get_db(), tournament)
        notify.check_completion(get_db(), tournament)
    if request.form.get("voltar") == "meus-jogos":
        # Volta com o card do torneio aberto, na altura do jogo.
        return redirect(url_for("tournaments.my_matches", aberto=tournament["id"], _anchor=f"jogo-{match_id}"))
    return redirect(url_for("tournaments.match", slug=slug, match_id=match_id))


@bp.post("/<slug>/jogos/<int:match_id>/reportar")
@player_required
def match_report(slug, match_id):
    try:
        score = _score_form()
    except TournamentError as error:
        flash(str(error), "error")
        return redirect(url_for("tournaments.match", slug=slug, match_id=match_id))
    return _player_action(slug, match_id, lambda db, t: reports.report(
        db, t, match_id, session["player_id"], score, request.form.get("comment", "")),
        "Placar enviado. Agora o adversário confirma ou contesta.",
        after=lambda db, t: notify.score_reported(db, t, match_id))


@bp.post("/<slug>/jogos/<int:match_id>/confirmar")
@player_required
def match_confirm(slug, match_id):
    def action(db, t):
        # Placar lido do jogo: a 1ª confirmação só marca a do jogador (falta o adversário).
        if not reports.confirm(db, t, match_id, session["player_id"]):
            flash("Confirmação registrada: falta o adversário confirmar (ou o prazo acabar).", "success")
        else:
            flash("Resultado confirmado!", "success")
    return _player_action(slug, match_id, action, None)


@bp.post("/<slug>/jogos/<int:match_id>/aceitar")
@player_required
def match_accept(slug, match_id):
    return _player_action(slug, match_id, lambda db, t: reports.accept_dispute(db, t, match_id, session["player_id"]),
                          "Placar da contestação aceito: resultado confirmado!")


@bp.post("/<slug>/jogos/<int:match_id>/contestar")
@player_required
def match_dispute(slug, match_id):
    try:
        score = _score_form()
    except TournamentError as error:
        flash(str(error), "error")
        return redirect(url_for("tournaments.match", slug=slug, match_id=match_id))
    if score[0] is None or score[1] is None:
        score = None
    return _player_action(slug, match_id, lambda db, t: reports.dispute(
        db, t, match_id, session["player_id"], request.form.get("comment", ""), score),
        "Contestação enviada ao organizador.",
        after=lambda db, t: notify.score_disputed(db, t, match_id))


@bp.get("/meus-jogos")
@player_required
def my_matches():
    db = get_db()
    rows = reports.matches_for_player(db, session["player_id"])
    groups = {}  # tournament_id -> jogos, na ordem da consulta (em andamento primeiro)
    for row in rows:
        groups.setdefault(row["tournament_id"], []).append(row)
    names = {}
    for tournament_id in groups:
        names.update(_names(db, store.get_tournament(db, tournament_id)))
    counter = {r["id"] for r in rows if r["status"] == "disputed" and reports.last_dispute(db, r["id"])}
    return render_template("tournaments/my_matches.html", groups=list(groups.values()), names=names, counter=counter,
                           opened=request.args.get("aberto", type=int),
                           deadlines={r["id"]: reports.deadline(db, r) for r in rows if r["status"] == "reported"})



@bp.get("/status")
@player_required
def my_status():
    """Resumo do jogador logado: últimas 10 partidas (com o popup de cada uma),
    V/E/D no geral e a evolução do aproveitamento jogo a jogo."""
    db = get_db()
    me = session["player_id"]
    rows = stats.history(db, me, limit=100)
    last = rows[:10]
    games_by_id, names = _match_popups(db, last)
    return render_template("tournaments/status.html", record=stats.player_record(db, me),
                           position=stats.position(db, me), last=last, outcome=stats.outcome,
                           games_by_id=games_by_id, names=names, evolution=stats.evolution(rows),
                           total=len(rows))

# --- Ranking, histórico e comparação (app/tournament/stats.py) ----------------

def _match_popups(db, rows):
    """Jogos de torneio das linhas do histórico (para o popup de cada um) e os
    nomes com escudo dos torneios deles. Amistosos não têm (kind 'a')."""
    rows = [r for r in rows if r["kind"] == "t"]
    games_by_id, names = {}, {}
    if rows:
        ids = [r["match_id"] for r in rows]
        for row in db.execute(f"SELECT * FROM tournament_matches WHERE id IN ({','.join('?' * len(ids))})", ids):
            games_by_id[row["id"]] = row
        for tournament_id in {r["tournament_id"] for r in rows}:
            names.update(_names(db, store.get_tournament(db, tournament_id)))
    return games_by_id, names


def _game_filter(db):
    """Jogo escolhido no filtro (?jogo=) e a lista de jogos para o <select>."""
    games = db.execute("SELECT id, name FROM game_titles ORDER BY name").fetchall()
    game = request.args.get("jogo", type=int)
    return (game if any(g["id"] == game for g in games) else None), games


def _player_by_name(db, username):
    return db.execute("SELECT id, username, created_at FROM players WHERE username = ?",
                      ((username or "").strip(),)).fetchone()


@bp.get("/ranking")
def ranking():
    db = get_db()
    game, games = _game_filter(db)
    me = session.get("player_id")
    my_name = db.execute("SELECT username FROM players WHERE id = ?", (me,)).fetchone() if me else None
    return render_template("tournaments/ranking.html", rows=stats.ranking(db, game), games=games, game=game,
                           xp_table=xp.table(), xp_rules={"base": xp.BASE, "step": xp.STEP, "min": xp.MIN_POINTS},
                           my_position=stats.position(db, me, game) if my_name else None,
                           my_name=my_name[0] if my_name else None,
                           my_record=stats.player_record(db, me, game) if me else None)


@bp.get("/jogadores/<nome>")
def player(nome):
    db = get_db()
    target = _player_by_name(db, nome)
    if target is None:
        abort(404)
    game, games = _game_filter(db)
    rows = stats.history(db, target["id"], game)
    return render_template("tournaments/player.html", p=target, games=games, game=game,
                           record=stats.player_record(db, target["id"], game),
                           position=stats.position(db, target["id"], game), rows=rows, form=stats.form(rows),
                           opponents=stats.opponents(db, target["id"], game), outcome=stats.outcome,
                           relation=friends.relation(db, session.get("player_id"), target["id"]))


@bp.get("/jogadores/<nome>/resumo")
def player_card(nome):
    """Resumo do jogador para o popup das listas (miniatura da foto - _avatar.html)."""
    db = get_db()
    target = _player_by_name(db, nome)
    if target is None:
        abort(404)
    # Depois de "Adicionar amigo" no popup, volta para a página onde ele foi aberto.
    referrer = urlsplit(request.referrer or "")
    back = referrer.path + (f"?{referrer.query}" if referrer.query else "")
    if not back.startswith("/torneios"):
        back = url_for("tournaments.player", nome=target["username"])
    return render_template("tournaments/_player_card.html", p=target, record=stats.player_record(db, target["id"]),
                           position=stats.position(db, target["id"]), rows=stats.history(db, target["id"], limit=5),
                           outcome=stats.outcome, relation=friends.relation(db, session.get("player_id"), target["id"]),
                           back=back)


@bp.post("/jogadores/<int:player_id>/foto/remover")
def player_avatar_remove(player_id):
    """Moderação: o admin tira a foto de um jogador."""
    if not is_admin():
        abort(403)
    validate_csrf()
    db = get_db()
    row = db.execute("SELECT username, avatar FROM players WHERE id = ?", (player_id,)).fetchone()
    if row is None:
        abort(404)
    db.execute("UPDATE players SET avatar = NULL, updated_at = ? WHERE id = ?", (now(), player_id))
    db.commit()
    images.delete("avatar", row["avatar"])
    flash("Foto removida.", "success")
    return redirect(url_for("tournaments.player", nome=row["username"]))


@bp.get("/comparar")
def compare():
    db = get_db()
    game, games = _game_filter(db)
    names = (request.args.get("a", "").strip(), request.args.get("b", "").strip())
    a, b = (_player_by_name(db, name) if name else None for name in names)
    if not names[0] and session.get("player_id"):  # sem escolha: começa por quem está logado
        a = db.execute("SELECT id, username FROM players WHERE id = ?", (session["player_id"],)).fetchone()
    result = None
    if (names[0] and a is None) or (names[1] and b is None):
        flash("Jogador não encontrado.", "error")
    elif a and b and a["id"] == b["id"]:
        flash("Escolha dois jogadores diferentes.", "error")
    elif a and b:
        result = stats.compare(db, a["id"], b["id"], game)
    # Os últimos jogos de quem está logado contra cada jogador escolhido
    # (menos ele mesmo), com o jogo (para o popup) e os nomes com escudo.
    me_id = session.get("player_id")
    mine = [{"opponent": other, "matches": stats.history(db, me_id, game, opponent_id=other["id"], limit=5)}
            for other in (a, b) if result and me_id and other["id"] != me_id]
    games_by_id, names = _match_popups(db, [r for card in mine for r in card["matches"]])
    return render_template("tournaments/compare.html", a=a, b=b, result=result, games=games, game=game,
                           players=stats.players_with_matches(db),
                           outcome=stats.outcome, mine=mine, games_by_id=games_by_id, names=names)


_last_auto_confirm = [0.0]


@bp.before_app_request
def _auto_confirm_reports():
    """Confirma placares sem resposta no prazo - no máximo uma vez por minuto,
    a partir das páginas de torneio (não há cron no servidor)."""
    if not (request.path.startswith("/torneios") or request.path.startswith("/admin/torneios")):
        return
    try:
        notify.empty_wallets(get_db())  # saldo zerado por débito: avisa logo (barato: só lê uma flag)
    except sqlite3.OperationalError:
        return  # banco sem as colunas/tabelas ainda
    if time.monotonic() - _last_auto_confirm[0] < 60:
        return
    _last_auto_confirm[0] = time.monotonic()
    try:
        db = get_db()
        disputed = []
        from . import live
        live.reap(db)  # jogos do Ao vivo encerrados viram reporte (live.report_finished)
        reports.auto_confirm(db, disputed=disputed)
        friendly.auto_confirm(db)  # placares de amistoso lidos do jogo, sem resposta no prazo
        for tournament_id, match_id in disputed:
            notify.score_disputed(db, store.get_tournament(db, tournament_id), match_id)
        notify.sweep(db)  # avisa o fim dos jogos e encerra quem passou do prazo
    except sqlite3.OperationalError:
        pass  # banco sem as tabelas ainda


@bp.post("/<slug>/proxima-fase")
@manager_required
def advance(tournament):
    if _run(store.advance, tournament, "Próxima fase gerada."):
        notify.stage_started(get_db(), store.get_tournament(get_db(), tournament["id"]))
    return redirect(url_for("tournaments.manage", slug=tournament["slug"]))


@bp.post("/<slug>/encerrar")
@manager_required
def finish(tournament):
    ok = _run(store.finish, tournament, "Campeonato encerrado e prêmio distribuído!")
    return redirect(url_for("tournaments.show", slug=tournament["slug"], aba="geral") if ok
                    else url_for("tournaments.manage", slug=tournament["slug"]))


@bp.post("/<slug>/cancelar")
@manager_required
def cancel(tournament):
    if tournament["status"] == "running" and not is_admin():
        flash("Campeonato em andamento só pode ser cancelado pela administração do WE Camp.", "error")
    elif request.form.get("confirmar") != "CANCELAR":
        flash('Para cancelar, digite CANCELAR no campo de confirmação.', "error")
    else:
        _run(store.cancel, tournament, "Campeonato cancelado; as inscrições foram devolvidas.")
    return redirect(url_for("tournaments.manage", slug=tournament["slug"]))


# --- Administração WE Camp ----------------------------------------------------

@admin_bp.get("/")
@login_required
def admin_home():
    db = get_db()
    values = {key: setting(db, key) for key in SETTING_LABELS}
    from .listing import cap, like
    recent_player = request.args.get("ajuste_jogador", "").strip()
    recent, recent_total = cap(db.execute(
        "SELECT w.*, p.username FROM wallet_entries w JOIN players p ON p.id = w.player_id "
        "WHERE w.kind = 'ajuste_admin' AND p.username LIKE ? ESCAPE '\\' ORDER BY w.id DESC",
        (like(recent_player),)).fetchall())
    players = db.execute("SELECT id, username FROM players ORDER BY username COLLATE NOCASE").fetchall()
    return render_template("tournaments/admin.html", values=values, labels=SETTING_LABELS, money=MONEY_SETTINGS,
                           players=players, recent_player=recent_player, recent_total=recent_total,
                           recent=recent, admin_emails=", ".join(notify.admin_emails(db)))


@admin_bp.post("/ajustes")
@login_required
def admin_settings():
    validate_csrf()
    db = get_db()
    try:
        updates = {}
        for key in SETTING_LABELS:
            raw = request.form.get(key, "")
            if key in MONEY_SETTINGS:
                try:
                    updates[key] = parse_cents(raw, allow_negative=key in SIGNED_SETTINGS)
                except ValueError as error:
                    raise ValueError(f"{SETTING_LABELS[key]}: {error}")
            else:
                try:
                    updates[key] = int(raw)
                except ValueError:
                    raise ValueError(f"{SETTING_LABELS[key]}: use um número inteiro.")
                if updates[key] < 1:
                    raise ValueError(f"{SETTING_LABELS[key]}: precisa ser pelo menos 1.")
    except ValueError as error:
        flash(f"{error}", "error")
    else:
        for key, value in updates.items():
            db.execute("INSERT OR REPLACE INTO site_settings (key, value, updated_at) VALUES (?, ?, ?)",
                       (key, str(value), now()))
        db.commit()
        flash("Valores do W$ salvos.", "success")
    return redirect(url_for("tournaments_admin.admin_home"))


@admin_bp.post("/email")
@login_required
def admin_email():
    """E-mail(s) da administração do WE Camp: recebem as contestações que ela decide."""
    validate_csrf()
    emails = notify.parse_emails(request.form.get("emails", ""))
    if emails is None:
        flash("E-mail inválido. Separe vários endereços por vírgula.", "error")
    else:
        db = get_db()
        db.execute("INSERT OR REPLACE INTO site_settings (key, value, updated_at) VALUES (?, ?, ?)",
                   (notify.ADMIN_EMAIL_SETTING, ", ".join(emails), now()))
        db.commit()
        flash("E-mail da administração salvo." if emails else "E-mail da administração removido.", "success")
    return redirect(url_for("tournaments_admin.admin_home"))


@admin_bp.post("/carteira")
@login_required
def admin_wallet_adjust():
    validate_csrf()
    db = get_db()
    player = db.execute("SELECT id, username FROM players WHERE username = ? COLLATE NOCASE",
                        (request.form.get("username", "").strip(),)).fetchone()
    note = request.form.get("note", "").strip()[:200]
    try:
        if player is None:
            raise ValueError("Jogador não encontrado.")
        if not note:
            raise ValueError("Informe o motivo do ajuste.")
        amount = parse_cents(request.form.get("amount", ""))
        if request.form.get("direction") == "debito":
            amount = -amount
        if amount == 0:
            raise ValueError("Informe um valor maior que zero.")
        ensure_wallet(db, player["id"])
        post(db, player["id"], amount, "ajuste_admin", f"ajuste:{player['id']}:{secrets.token_hex(8)}",
             note=note, user_id=session["user_id"], allow_negative=True)
        db.commit()
        flash(f"{format_cents(amount)} lançado na carteira de {player['username']}.", "success")
    except ValueError as error:
        db.rollback()
        flash(str(error), "error")
    return redirect(url_for("tournaments_admin.admin_home"))


# --- Times: escudos enviados e administração ---------------------------------

LOGO_TYPES = {b"\x89PNG": ".png", b"\xff\xd8\xff": ".jpg", b"RIFF": ".webp"}
MAX_LOGO_BYTES = 512 * 1024


@bp.get("/escudos/<path:filename>")
def team_logo(filename):
    response = send_from_directory(current_app.config["TEAM_LOGOS_DIR"], filename, max_age=86400)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _logo_extension(data):
    """Só PNG, JPEG e WebP (SVG enviado poderia levar script)."""
    for magic, ext in LOGO_TYPES.items():
        if data.startswith(magic) and (ext != ".webp" or data[8:12] == b"WEBP"):
            return ext
    return None


@admin_bp.get("/times")
@login_required
def admin_teams():
    db = get_db()
    query = request.args.get("q", "").strip()
    group_id = request.args.get("grupo", type=int)
    sql = ("SELECT t.*, (SELECT GROUP_CONCAT(g.name, ', ') FROM team_group_members m JOIN team_groups g "
           "ON g.id = m.group_id WHERE m.team_id = t.id) AS groups FROM teams t WHERE 1 = 1")
    params = []
    from .listing import cap, like
    if query:
        sql += " AND t.name LIKE ? ESCAPE '\\'"
        params.append(like(query))
    if group_id:
        sql += " AND t.id IN (SELECT team_id FROM team_group_members WHERE group_id = ?)"
        params.append(group_id)
    teams, total = cap(db.execute(sql + " ORDER BY t.name COLLATE NOCASE", params).fetchall())
    teams = [dict(row, image=team_image(row)) for row in teams]
    groups = db.execute("SELECT g.*, (SELECT COUNT(*) FROM team_group_members m WHERE m.group_id = g.id) AS size "
                        "FROM team_groups g ORDER BY g.seq, g.name").fetchall()
    return render_template("tournaments/admin_teams.html", teams=teams, total=total, groups=groups, query=query,
                           group_id=group_id, kinds=TEAM_KINDS)


@admin_bp.route("/times/novo", methods=("GET", "POST"))
@admin_bp.route("/times/<int:team_id>", methods=("GET", "POST"))
@login_required
def admin_team_edit(team_id=None):
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone() if team_id else None
    if team_id and team is None:
        abort(404)
    if request.method == "POST":
        validate_csrf()
        name = " ".join(request.form.get("name", "").split())[:60]
        kind = request.form.get("kind", "custom")
        if len(name) < 2 or kind not in TEAM_KINDS:
            flash("Informe o nome (pelo menos 2 letras) e o tipo do time.", "error")
            return redirect(request.url)
        logo = team["logo"] if team else None
        upload = request.files.get("logo")
        if upload and upload.filename:
            data = upload.read(MAX_LOGO_BYTES + 1)
            ext = _logo_extension(data)
            if len(data) > MAX_LOGO_BYTES or not ext:
                flash("Escudo inválido: envie PNG, JPG ou WebP de até 512 KB.", "error")
                return redirect(request.url)
            filename = hashlib.sha256(data).hexdigest()[:24] + ext
            Path(current_app.config["TEAM_LOGOS_DIR"], filename).write_bytes(data)
            logo = "upload:" + filename
        elif request.form.get("remove_logo") == "on":
            logo = None
        country = request.form.get("country", "").strip().lower()[:10]
        active = 1 if request.form.get("is_active") == "on" else 0
        if team:
            db.execute("UPDATE teams SET name = ?, kind = ?, country = ?, logo = ?, is_active = ?, updated_at = ? "
                       "WHERE id = ?", (name, kind, country, logo, active, now(), team_id))
        else:
            team_id = db.execute("INSERT INTO teams (name, kind, country, logo, is_active, created_at, updated_at) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (name, kind, country, logo, active, now(),
                                                                  now())).lastrowid
        db.execute("DELETE FROM team_group_members WHERE team_id = ?", (team_id,))
        db.executemany("INSERT OR IGNORE INTO team_group_members (group_id, team_id) VALUES (?, ?)",
                       [(int(g), team_id) for g in request.form.getlist("groups") if g.isdigit()])
        db.commit()
        flash("Time salvo.", "success")
        return redirect(url_for("tournaments_admin.admin_team_edit", team_id=team_id))
    groups = db.execute("SELECT * FROM team_groups ORDER BY seq, name").fetchall()
    member_of = {row[0] for row in db.execute("SELECT group_id FROM team_group_members WHERE team_id = ?",
                                              (team_id,))} if team else set()
    return render_template("tournaments/admin_team_edit.html", team=team, groups=groups, member_of=member_of,
                           kinds=TEAM_KINDS, image=team_image(team) if team else None)


@admin_bp.post("/times/grupos")
@login_required
def admin_team_group_new():
    validate_csrf()
    db = get_db()
    name = " ".join(request.form.get("name", "").split())[:60]
    if len(name) < 2:
        flash("Informe o nome do grupo.", "error")
    else:
        try:
            db.execute("INSERT INTO team_groups (slug, name, seq, created_at) VALUES (?, ?, 1000, ?)",
                       (slugify(name), name, now()))
            db.commit()
            flash("Grupo criado. Adicione times a ele na página de cada time.", "success")
        except sqlite3.IntegrityError:
            db.rollback()
            flash("Já existe um grupo com esse nome.", "error")
    return redirect(url_for("tournaments_admin.admin_teams"))


# --- Cadastro de ISOs para os amistosos (app/tournament/iso_catalog.py) --------

@admin_bp.get("/isos")
@login_required
def admin_isos():
    from .listing import cap
    db = get_db()
    rows = iso_catalog.all_rows(db)
    filters = {"name": request.args.get("nome", "").strip(), "status": request.args.get("ativa", "")}
    pending = [row for row in rows if not row["approved_at"]]
    approved = [row for row in rows if row["approved_at"]]
    if filters["name"]:
        text = filters["name"].lower()
        approved = [row for row in approved if text in " ".join(
            str(row[key] or "") for key in ("name", "game_name", "requester_name")).lower()
            or text in (iso_catalog.filename(row) or "").lower()]
    if filters["status"] in ("sim", "nao"):
        approved = [row for row in approved if bool(row["is_active"]) == (filters["status"] == "sim")]
    approved, approved_total = cap(approved)
    return render_template("tournaments/admin_isos.html", pending=pending, approved=approved,
                           approved_total=approved_total, filters=filters, filtered=any(filters.values()),
                           uploading=iso.uploading, filename=iso_catalog.filename)


def _iso_form_context(db):
    return {"games": db.execute("SELECT id, name FROM game_titles WHERE is_active = 1 ORDER BY year, name").fetchall(),
            "files": iso_catalog.championship_files(db), **_iso_limits()}


@admin_bp.route("/isos/nova", methods=("GET", "POST"))
@admin_bp.route("/isos/<int:catalog_id>", methods=("GET", "POST"))
def admin_iso_edit(catalog_id=None):
    # Sem @login_required: o tamanho é conferido antes de ler o formulário
    # (o CSRF lê o corpo inteiro, que pode ter centenas de MB), como no envio
    # da ISO do torneio.
    if not is_admin():
        return redirect(url_for("admin.login", next=request.path))
    db = get_db()
    row = iso_catalog.get(db, catalog_id) if catalog_id else None
    if catalog_id and row is None:
        abort(404)
    if request.method == "POST":
        try:
            iso.check_size(request, current_app.config["TOURNAMENT_ISO_MAX_BYTES"])
        except TournamentError as error:
            flash(str(error), "error")
            return redirect(request.path)
        validate_csrf()
        form = request.form
        upload = request.files.get("iso")
        upload = upload if upload and upload.filename else None
        image = request.files.get("image")
        new_image = None
        try:
            if image and image.filename:
                new_image = images.save("torneio", image)
            pending = bool(row and not row["approved_at"])
            releasing = pending and "liberar" in form
            saved_id = iso_catalog.save(db, catalog_id, form.get("name"), form.get("game_title_id", type=int),
                                        form.get("file_id", type=int), upload,
                                        ("is_active" in form) and (not pending or releasing))
            if releasing:
                iso_catalog.approve(db, saved_id)
            if new_image or "remove_image" in form:
                db.execute("UPDATE catalog_isos SET image = ? WHERE id = ?", (new_image, saved_id))
            db.commit()
        except (TournamentError, images.ImageError) as error:
            db.rollback()
            images.delete("torneio", new_image)
            flash(str(error), "error")
            return render_template("tournaments/admin_iso_edit.html", row=row, form=form, **_iso_form_context(db))
        if row and (new_image or "remove_image" in form):
            images.delete("torneio", row["image"])
        if releasing:
            notify.iso_approved(db, iso_catalog.get(db, saved_id))
            flash("ISO liberada: ela já aparece para os jogadores escolherem no amistoso.", "success")
        app = current_app._get_current_object()
        if upload is not None:
            iso.start_archive(app, saved_id, "catalog_isos")
            flash("ISO salva. " + ("Ela está sendo enviada para o archive.org; o download fica disponível quando o "
                                   "envio terminar." if iso.archive_configured(app)
                                   else "O archive.org não está configurado neste servidor: a ISO fica só aqui."),
                  "success")
        elif not releasing:
            flash("ISO salva.", "success")
        return redirect(url_for("tournaments_admin.admin_isos"))
    return render_template("tournaments/admin_iso_edit.html", row=row, form={}, **_iso_form_context(db))


@admin_bp.post("/isos/<int:catalog_id>/recusar")
@login_required
def admin_iso_reject(catalog_id):
    validate_csrf()
    db = get_db()
    try:
        row = iso_catalog.reject(db, catalog_id)
        db.commit()
    except TournamentError as error:
        db.rollback()
        flash(str(error), "error")
        return redirect(url_for("tournaments_admin.admin_iso_edit", catalog_id=catalog_id))
    images.delete("torneio", row["image"])
    notify.iso_rejected(db, row, request.form.get("motivo"))
    flash("ISO recusada e removida.", "success")
    return redirect(url_for("tournaments_admin.admin_isos"))


@admin_bp.post("/isos/<int:catalog_id>/archive-org")
@login_required
def admin_iso_retry(catalog_id):
    validate_csrf()
    db = get_db()
    row = iso_catalog.get(db, catalog_id)
    if row is None or not row["stored_name"] or row["local_removed_at"]:
        flash("Esta ISO não tem cópia neste servidor para enviar.", "error")
    elif iso.uploading(row):
        flash("O envio para o archive.org já está em andamento.", "error")
    elif not iso.archive_configured(current_app):
        flash("O archive.org não está configurado neste servidor.", "error")
    else:
        iso.start_archive(current_app._get_current_object(), catalog_id, "catalog_isos")
        flash("Enviando a ISO para o archive.org de novo.", "success")
    return redirect(url_for("tournaments_admin.admin_isos"))


# --- Template helpers ---------------------------------------------------------

@bp.app_context_processor
def inject_wallet():
    """Saldo W$ no menu (e abre a carteira na primeira página vista depois do login)."""
    player_id = session.get("player_id")
    if not player_id:
        return {}
    db = get_db()
    try:
        if ensure_wallet(db, player_id):
            db.commit()
        row = db.execute("SELECT wallet_cents FROM players WHERE id = ?", (player_id,)).fetchone()
        # Jogos esperando uma ação do jogador: sem placar, com placar do adversário
        # para confirmar, ou com o próprio placar contestado com outro placar para aceitar.
        todo = db.execute(
            "SELECT COUNT(*) FROM tournament_entries e JOIN tournaments t ON t.id = e.tournament_id "
            "JOIN tournament_stages s ON s.tournament_id = t.id AND s.status = 'running' "
            "JOIN tournament_matches m ON m.stage_id = s.id AND (m.home_entry_id = e.id OR m.away_entry_id = e.id) "
            "WHERE e.player_id = ? AND t.status = 'running' AND (m.status = 'scheduled' OR "
            "(m.status = 'reported' AND (m.reported_by_entry_id != e.id OR (m.reported_by_entry_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM match_reports c WHERE c.match_id = m.id AND c.action = 'confirm' AND c.entry_id = e.id AND c.id > (SELECT MAX(r2.id) FROM match_reports r2 WHERE r2.match_id = m.id AND r2.action = 'report'))))) OR "
            "(m.status = 'disputed' AND EXISTS (SELECT 1 FROM match_reports r WHERE r.match_id = m.id "
            "AND r.action = 'dispute' AND r.home_goals IS NOT NULL AND (m.reported_by_entry_id = e.id OR "
            "(m.reported_by_entry_id IS NULL AND r.entry_id != e.id)))))",
            (player_id,)).fetchone()[0]
        friends_todo = friends.pending_count(db, player_id)
        invites_todo = len(friends.my_invites(db, player_id))
        friendlies_todo = friendly.todo_count(db, player_id)
    except sqlite3.OperationalError:
        return {}  # banco ainda sem as colunas da carteira
    return {"wallet_balance": row["wallet_cents"] if row else None, "matches_todo": todo,
            "friends_todo": friends_todo, "invites_todo": invites_todo, "friendlies_todo": friendlies_todo}
