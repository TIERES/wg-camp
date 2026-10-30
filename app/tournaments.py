"""Portal de torneios (/torneios) e a administração dele (/admin/torneios).

Qualquer jogador logado cria um torneio e vira o organizador dele: abre as
inscrições, sorteia, lança e corrige resultados, gera as fases e encerra. O
Admin WE Camp (tabela `users`) pode tudo em qualquer torneio e mantém o
catálogo de jogos e os valores do W$ (app/wallet.py).

Regras dos formatos em app/tournament/ (engine/formats/store).
"""
import json
import re
import secrets
import sqlite3
from functools import wraps
from unicodedata import normalize

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from .db import get_db, now
from .players import player_required
from .security import login_required, validate_csrf
from .tournament import engine, formats, store
from .tournament.store import TournamentError
from .wallet import (KINDS, MONEY_SETTINGS, SETTING_LABELS, InsufficientFunds, ensure_wallet, format_cents,
                     parse_cents, post, setting)

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


def can_manage(tournament):
    return is_admin() or session.get("player_id") == tournament["organizer_id"]


def _load(slug):
    db = get_db()
    tournament = db.execute(
        "SELECT t.*, g.name AS game_name, g.platform, p.username AS organizer_name FROM tournaments t "
        "JOIN game_titles g ON g.id = t.game_title_id JOIN players p ON p.id = t.organizer_id WHERE t.slug = ?",
        (slug,)).fetchone()
    if tournament is None or (tournament["status"] == "draft" and not can_manage(tournament)):
        abort(404)
    return tournament


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

def _names(db, tournament_id):
    return {row["id"]: row["username"] + (f" ({row['team_name']})" if row["team_name"] else "")
            for row in store.entries_of(db, tournament_id)}


def _stage_views(db, tournament):
    """Fases com tabelas, jogos por rodada e chaves, prontas para o template."""
    views = []
    for stage in store.stages_of(db, tournament["id"]):
        matches = db.execute("SELECT * FROM tournament_matches WHERE stage_id = ? ORDER BY round, tie_id, leg, id",
                             (stage["id"],)).fetchall()
        view = {"stage": stage, "settings": store.settings_of(stage), "rounds": [], "groups": [], "bracket": [],
                "highlight": 0, "playoff": 0}
        rounds = {}
        for match in matches:
            rounds.setdefault(match["round"], []).append(match)
        if stage["kind"] == "knockout":
            ties = db.execute("SELECT * FROM tournament_ties WHERE stage_id = ? ORDER BY round, is_third_place, "
                              "position", (stage["id"],)).fetchall()
            last = max(t["round"] for t in ties)
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
                    items.append({"tie": tie, "legs": legs, "aggregate": aggregate,
                                  "label": "Disputa de 3º lugar" if tie["is_third_place"] else None})
                view["bracket"].append({"name": name, "round": round_no, "items": items, "is_last": round_no == last})
            names = {r["round"]: r["name"] for r in view["bracket"]}
        else:
            for group, table in store.group_tables(db, stage["id"]):
                view["groups"].append({"group": group, "table": table})
            names = {r: f"Rodada {r}" for r in rounds}
            config = store.settings_of(tournament)
            if stage["kind"] == "groups":
                view["highlight"] = config.get("qualifiers", 0)
            elif stage["kind"] == "swiss":
                view["highlight"], view["playoff"] = config.get("direct", 0), config.get("direct", 0) * 2
        view["rounds"] = [{"name": names.get(r, f"Rodada {r}"), "matches": rounds[r]} for r in sorted(rounds)]
        views.append(view)
    return views


@bp.get("/")
def index():
    db = get_db()
    status = request.args.get("status", "")
    game = request.args.get("jogo", type=int)
    sql = ("SELECT t.*, g.name AS game_name, p.username AS organizer_name, "
           "(SELECT COUNT(*) FROM tournament_entries e WHERE e.tournament_id = t.id) AS entries "
           "FROM tournaments t JOIN game_titles g ON g.id = t.game_title_id JOIN players p ON p.id = t.organizer_id "
           "WHERE t.status != 'draft'")
    params = []
    if status in STATUS_LABELS:
        sql += " AND t.status = ?"
        params.append(status)
    if game:
        sql += " AND t.game_title_id = ?"
        params.append(game)
    sql += (" ORDER BY CASE t.status WHEN 'registration' THEN 0 WHEN 'running' THEN 1 WHEN 'finished' THEN 2 "
            "ELSE 3 END, t.updated_at DESC LIMIT 200")
    tournaments = db.execute(sql, params).fetchall()
    games = db.execute("SELECT id, name FROM game_titles WHERE is_active = 1 ORDER BY name").fetchall()
    return render_template("tournaments/index.html", tournaments=tournaments, games=games, status=status,
                           game=game, status_labels=STATUS_LABELS, format_labels=formats.FORMATS)


@bp.get("/<slug>")
def show(slug):
    db = get_db()
    tournament = _load(slug)
    entries = store.entries_of(db, tournament["id"])
    player_id = session.get("player_id")
    my_entry = next((e for e in entries if e["player_id"] == player_id), None)
    stages = _stage_views(db, tournament)
    config = store.settings_of(tournament)
    tab = request.args.get("aba", "tabela" if stages else "geral")
    if tab not in ("geral", "tabela", "jogos"):
        tab = "geral"
    pool = store.prize_pool(db, tournament["id"])
    split = engine.parse_split(tournament["prize_split"])
    return render_template(
        "tournaments/show.html", t=tournament, entries=entries, my_entry=my_entry, stages=stages, tab=tab,
        names=_names(db, tournament["id"]), can_manage=can_manage(tournament),
        format_label=formats.FORMATS[tournament["format"]], summary=formats.describe(tournament["format"], config),
        status_labels=STATUS_LABELS, pool=pool, split=split,
        prizes=engine.split_prize(pool, split, 3) if pool else [],
        podium=sorted((e for e in entries if e["final_position"]), key=lambda e: e["final_position"]))


# --- Jogador: criar, inscrever-se, carteira -----------------------------------

def _form_config(form, fmt):
    raw = {key: form.get(key) for key in formats.DEFAULTS[fmt] if form.get(key) is not None}
    raw["_form"] = "1"
    return formats.normalize(fmt, raw)


@bp.route("/novo", methods=("GET", "POST"))
@player_required
def new():
    db = get_db()
    games = db.execute("SELECT * FROM game_titles WHERE is_active = 1 ORDER BY name").fetchall()
    max_fee = setting(db, "torneios_inscricao_max")
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        validate_csrf()
        try:
            name = form.get("name", "").strip()
            if not 3 <= len(name) <= 80:
                raise TournamentError("O nome precisa ter entre 3 e 80 caracteres.")
            game_id = int(form.get("game_title_id") or 0)
            if not any(g["id"] == game_id for g in games):
                raise TournamentError("Escolha um jogo.")
            fmt = form.get("format", "")
            if fmt not in formats.FORMATS:
                raise TournamentError("Escolha um formato.")
            max_players = int(form.get("max_players") or 0)
            fee = parse_cents(form.get("entry_fee") or "0")
            if fee > max_fee:
                raise TournamentError(f"A inscrição pode ser de no máximo {format_cents(max_fee)}.")
            split = form.get("prize_split", PRIZE_SPLITS[0])
            engine.parse_split(split)
            config = _form_config(form, fmt)
            error = formats.config_error(fmt, config, max_players)
            if error:
                raise TournamentError(f"Com {max_players} vagas: {error}")
            if not is_admin():
                open_count = db.execute("SELECT COUNT(*) FROM tournaments WHERE organizer_id = ? AND status IN "
                                        "('draft', 'registration', 'running')", (session["player_id"],)).fetchone()[0]
                limit = setting(db, "torneios_abertos_por_jogador")
                if open_count >= limit:
                    raise TournamentError(f"Você já tem {open_count} torneio(s) em aberto (o limite é {limit}). "
                                          "Encerre ou cancele um antes de criar outro.")
        except (TournamentError, ValueError) as error:
            flash(str(error), "error")
        else:
            tournament_id = db.execute(
                "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
                "entry_fee_cents, prize_split, settings_json, rules, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'registration', ?, ?, ?, ?, ?, ?, ?)",
                (f"tmp-{now()}-{session['player_id']}", name, game_id, session["player_id"], fmt, max_players, fee,
                 split, json.dumps(config), form.get("rules", "").strip()[:5000], now(), now())).lastrowid
            slug = f"{slugify(name)}-{tournament_id}"
            db.execute("UPDATE tournaments SET slug = ? WHERE id = ?", (slug, tournament_id))
            db.commit()
            flash("Torneio criado com inscrições abertas. Divulgue o link!", "success")
            return redirect(url_for("tournaments.show", slug=slug))
    return render_template("tournaments/new.html", games=games, form=form, max_fee=max_fee,
                           format_labels=formats.FORMATS, defaults=formats.DEFAULTS, splits=PRIZE_SPLITS,
                           min_players=formats.MIN_PLAYERS, max_players=formats.MAX_PLAYERS)


@bp.post("/<slug>/inscrever")
@player_required
def enroll(slug):
    validate_csrf()
    tournament = _load(slug)
    _run(lambda db, t: store.enroll(db, t, session["player_id"], request.form.get("team_name", "")),
         tournament, "Inscrição feita!")
    return redirect(url_for("tournaments.show", slug=slug, aba="geral"))


@bp.post("/<slug>/sair")
@player_required
def leave(slug):
    validate_csrf()
    tournament = _load(slug)
    entry = get_db().execute("SELECT * FROM tournament_entries WHERE tournament_id = ? AND player_id = ?",
                             (tournament["id"], session["player_id"])).fetchone()
    if entry:
        _run(lambda db, t: store.withdraw(db, t, entry), tournament, "Você saiu do torneio; a inscrição foi devolvida.")
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
                           values={key: setting(db, key) for key in MONEY_SETTINGS})


# --- Organizador --------------------------------------------------------------

@bp.get("/<slug>/gerenciar")
def manage(slug):
    tournament = _load(slug)
    if not can_manage(tournament):
        abort(403)
    db = get_db()
    stages = _stage_views(db, tournament)
    current = stages[-1] if stages else None
    decisive = set()
    if current and current["stage"]["kind"] == "knockout":
        for rnd in current["bracket"]:
            for item in rnd["items"]:
                if item["legs"]:
                    decisive.add(item["legs"][-1]["id"])
    can_advance = can_finish = False
    if current and tournament["status"] == "running":
        complete = store.stage_complete(db, current["stage"])
        next_stage = store.has_next_stage(tournament, current["stage"])
        can_advance, can_finish = complete and next_stage, complete and not next_stage
    return render_template("tournaments/manage.html", t=tournament, entries=store.entries_of(db, tournament["id"]),
                           current=current, names=_names(db, tournament["id"]), decisive=decisive,
                           can_advance=can_advance, can_finish=can_finish, is_admin=is_admin(),
                           status_labels=STATUS_LABELS)


@bp.post("/<slug>/editar")
@manager_required
def edit(tournament):
    name = request.form.get("name", "").strip()
    if not 3 <= len(name) <= 80:
        flash("O nome precisa ter entre 3 e 80 caracteres.", "error")
    else:
        db = get_db()
        db.execute("UPDATE tournaments SET name = ?, rules = ?, updated_at = ? WHERE id = ?",
                   (name, request.form.get("rules", "").strip()[:5000], now(), tournament["id"]))
        db.commit()
        flash("Torneio atualizado.", "success")
    return redirect(url_for("tournaments.manage", slug=tournament["slug"]))


@bp.post("/<slug>/iniciar")
@manager_required
def start(tournament):
    ok = _run(store.start, tournament, "Sorteio feito: o torneio começou!")
    return redirect(url_for("tournaments.show", slug=tournament["slug"]) if ok
                    else url_for("tournaments.manage", slug=tournament["slug"]))


@bp.post("/<slug>/inscritos/<int:entry_id>/remover")
@manager_required
def remove_entry(tournament, entry_id):
    entry = get_db().execute("SELECT * FROM tournament_entries WHERE id = ? AND tournament_id = ?",
                             (entry_id, tournament["id"])).fetchone()
    if entry:
        _run(lambda db, t: store.withdraw(db, t, entry), tournament, "Inscrição removida e devolvida ao jogador.")
    return redirect(url_for("tournaments.manage", slug=tournament["slug"]))


@bp.post("/<slug>/partidas/<int:match_id>")
@manager_required
def result(tournament, match_id):
    if request.form.get("acao") == "apagar":
        _run(lambda db, t: store.clear_result(db, t, match_id), tournament, "Resultado apagado.")
    else:
        try:
            values = [_int_or_none(request.form.get(key)) for key in ("home_goals", "away_goals", "home_pens",
                                                                       "away_pens")]
        except TournamentError as error:
            flash(str(error), "error")
        else:
            _run(lambda db, t: store.record_result(db, t, match_id, *values), tournament, "Resultado salvo.")
    return redirect(url_for("tournaments.manage", slug=tournament["slug"]) + f"#jogo-{match_id}")


@bp.post("/<slug>/proxima-fase")
@manager_required
def advance(tournament):
    _run(store.advance, tournament, "Próxima fase gerada.")
    return redirect(url_for("tournaments.manage", slug=tournament["slug"]))


@bp.post("/<slug>/encerrar")
@manager_required
def finish(tournament):
    ok = _run(store.finish, tournament, "Torneio encerrado e prêmio distribuído!")
    return redirect(url_for("tournaments.show", slug=tournament["slug"], aba="geral") if ok
                    else url_for("tournaments.manage", slug=tournament["slug"]))


@bp.post("/<slug>/cancelar")
@manager_required
def cancel(tournament):
    if tournament["status"] == "running" and not is_admin():
        flash("Torneio em andamento só pode ser cancelado pela administração do WE Camp.", "error")
    elif request.form.get("confirmar") != "CANCELAR":
        flash('Para cancelar, digite CANCELAR no campo de confirmação.', "error")
    else:
        _run(store.cancel, tournament, "Torneio cancelado; as inscrições foram devolvidas.")
    return redirect(url_for("tournaments.manage", slug=tournament["slug"]))


# --- Administração WE Camp ----------------------------------------------------

@admin_bp.get("/")
@login_required
def admin_home():
    db = get_db()
    values = {key: setting(db, key) for key in SETTING_LABELS}
    games = db.execute("SELECT g.*, (SELECT COUNT(*) FROM tournaments t WHERE t.game_title_id = g.id) AS used "
                       "FROM game_titles g ORDER BY g.name").fetchall()
    championships = db.execute("SELECT id, name FROM championships ORDER BY name").fetchall()
    recent = db.execute("SELECT w.*, p.username FROM wallet_entries w JOIN players p ON p.id = w.player_id "
                        "WHERE w.kind = 'ajuste_admin' ORDER BY w.id DESC LIMIT 20").fetchall()
    return render_template("tournaments/admin.html", values=values, labels=SETTING_LABELS, money=MONEY_SETTINGS,
                           games=games, championships=championships, recent=recent)


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
                updates[key] = parse_cents(raw)
            else:
                updates[key] = int(raw)
                if updates[key] < 1:
                    raise ValueError("O limite de torneios em aberto precisa ser pelo menos 1.")
    except ValueError as error:
        flash(f"{error}", "error")
    else:
        for key, value in updates.items():
            db.execute("INSERT OR REPLACE INTO site_settings (key, value, updated_at) VALUES (?, ?, ?)",
                       (key, str(value), now()))
        db.commit()
        flash("Valores do W$ salvos.", "success")
    return redirect(url_for("tournaments_admin.admin_home"))


@admin_bp.post("/jogos")
@login_required
def admin_game_save():
    validate_csrf()
    db = get_db()
    name = request.form.get("name", "").strip()
    game_id = request.form.get("game_id", type=int)
    championship_id = request.form.get("championship_id", type=int) or None
    if not name:
        flash("Informe o nome do jogo.", "error")
        return redirect(url_for("tournaments_admin.admin_home"))
    values = (name[:80], request.form.get("platform", "").strip()[:40], championship_id,
              1 if request.form.get("is_active") == "on" else 0)
    try:
        if game_id:
            db.execute("UPDATE game_titles SET name = ?, platform = ?, championship_id = ?, is_active = ?, "
                       "updated_at = ? WHERE id = ?", values + (now(), game_id))
        else:
            db.execute("INSERT INTO game_titles (name, platform, championship_id, is_active, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?, ?, ?)", values + (now(), now()))
        db.commit()
        flash("Jogo salvo.", "success")
    except sqlite3.IntegrityError:
        db.rollback()
        flash("Já existe um jogo com esse nome.", "error")
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
    except sqlite3.OperationalError:
        return {}  # banco ainda sem as colunas da carteira
    return {"wallet_balance": row["wallet_cents"] if row else None}
