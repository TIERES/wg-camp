"""Portal de torneios (/torneios) e a administração dele (/admin/torneios).

Qualquer jogador logado cria um torneio e vira o organizador dele: abre as
inscrições, sorteia, lança e corrige resultados, gera as fases e encerra. O
Admin WE Camp (tabela `users`) pode tudo em qualquer torneio e mantém o
catálogo de jogos e os valores do W$ (app/wallet.py).

Regras dos formatos em app/tournament/ (engine/formats/store).
"""
import hashlib
import json
import re
import secrets
import sqlite3
import time
from datetime import date
from functools import wraps
from pathlib import Path
from unicodedata import normalize

from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request, send_from_directory,
                   session, url_for)
from markupsafe import Markup

from .db import get_db, now
from .players import player_required
from .security import login_required, validate_csrf
from .tournament import engine, formats, iso, reports, stats, store
from .tournament.store import TournamentError
from .tournament.teams import KINDS as TEAM_KINDS, TEAM_MODES, generated_badge
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

def team_image(team, display="logo"):
    """URL da imagem de um time (linha de `teams`), ou o escudo gerado."""
    if team is None:
        return generated_badge("?")
    if display == "flag" and team["flag"]:
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
        image = team_image(row, tournament["team_display"]) if row["name"] else generated_badge(row["team_name"])
        names[row["id"]] = Markup(
            '<span class="entry"><img class="crest" src="{}" alt="" loading="lazy">'
            '<span class="entry-name">{}<small>{}</small></span></span>').format(image, row["team_name"],
                                                                               row["username"])
    return names


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
    names = _names(db, tournament)
    my_pending = []
    if my_entry and tournament["status"] == "running":
        my_pending = db.execute(
            "SELECT m.* FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
            "WHERE s.tournament_id = ? AND s.status = 'running' AND m.status != 'confirmed' "
            "AND ? IN (m.home_entry_id, m.away_entry_id) ORDER BY m.round, m.leg, m.id",
            (tournament["id"], my_entry["id"])).fetchall()
    # Times da lista (modos choose/draw): todos, com o dono quando já tem.
    team_list = [dict(row, image=team_image(row, tournament["team_display"])) for row in db.execute(
        "SELECT t.*, p.username AS owner FROM tournament_teams tt JOIN teams t ON t.id = tt.team_id "
        "LEFT JOIN tournament_entries e ON e.tournament_id = tt.tournament_id AND e.team_id = t.id "
        "LEFT JOIN players p ON p.id = e.player_id WHERE tt.tournament_id = ? ORDER BY t.name COLLATE NOCASE",
        (tournament["id"],))]
    iso_row = iso.get(db, tournament["id"])
    return render_template(
        "tournaments/show.html", t=tournament, entries=entries, my_entry=my_entry, stages=stages, tab=tab,
        team_list=team_list, team_modes=TEAM_MODES, crests=_crests(names), my_pending=my_pending,
        names=names, can_manage=can_manage(tournament), iso=iso_row, iso_ready=iso.downloadable(iso_row, current_app),
        format_label=formats.FORMATS[tournament["format"]], summary=formats.describe(tournament["format"], config),
        status_labels=STATUS_LABELS, pool=pool, split=split,
        prizes=engine.split_prize(pool, split, 3) if pool else [],
        podium=sorted((e for e in entries if e["final_position"]), key=lambda e: e["final_position"]))


# --- Jogador: criar, inscrever-se, carteira -----------------------------------

def _team_catalog(db):
    """Grupos de times ativos com as imagens, para escolher os times do torneio."""
    groups = []
    for group in db.execute("SELECT * FROM team_groups ORDER BY seq, name"):
        teams = [dict(row, image=team_image(row), flag_image=team_image(row, "flag")) for row in db.execute(
            "SELECT t.* FROM team_group_members m JOIN teams t ON t.id = m.team_id "
            "WHERE m.group_id = ? AND t.is_active = 1 ORDER BY t.name COLLATE NOCASE", (group["id"],))]
        if teams:
            groups.append({"group": group, "teams": teams})
    return groups


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
    if request.method == "POST":
        try:
            iso.check_size(request, current_app.config["TOURNAMENT_ISO_MAX_BYTES"])
        except TournamentError as error:
            flash(str(error), "error")
            return redirect(url_for("tournaments.new"))
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        validate_csrf()
        upload = request.files.get("iso")
        if upload is not None and not upload.filename:
            upload = None
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
            starts_on, ends_on = _dates(form)
            if upload:
                iso.check_upload(upload)
            team_mode = form.get("team_mode", "free")
            if team_mode not in TEAM_MODES:
                raise TournamentError("Escolha como serão os times.")
            team_display = "flag" if form.get("team_display") == "flag" else "logo"
            team_ids = []
            if team_mode != "free":
                wanted = {int(v) for v in form.getlist("team_ids") if v.isdigit()}
                if wanted:
                    team_ids = [row[0] for row in db.execute(
                        f"SELECT id FROM teams WHERE is_active = 1 AND id IN ({','.join('?' * len(wanted))})",
                        list(wanted))]
                if len(team_ids) < max_players:
                    raise TournamentError(f"Marque pelo menos {max_players} times na lista (um por vaga); "
                                          f"há {len(team_ids)} marcados.")
            if not is_admin():
                open_count = db.execute("SELECT COUNT(*) FROM tournaments WHERE organizer_id = ? AND status IN "
                                        "('draft', 'registration', 'running')", (session["player_id"],)).fetchone()[0]
                limit = setting(db, "torneios_abertos_por_jogador")
                if open_count >= limit:
                    raise TournamentError(f"Você já tem {open_count} torneio(s) em aberto (o limite é {limit}). "
                                          "Encerre ou cancele um antes de criar outro.")
        except (TournamentError, ValueError) as error:
            # O navegador não preenche o campo de arquivo de novo.
            flash(str(error) + (" Selecione a ISO de novo." if upload else ""), "error")
        else:
            tournament_id = db.execute(
                "INSERT INTO tournaments (slug, name, game_title_id, organizer_id, format, status, max_players, "
                "entry_fee_cents, prize_split, settings_json, rules, team_mode, team_display, starts_on, ends_on, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'registration', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"tmp-{now()}-{session['player_id']}", name, game_id, session["player_id"], fmt, max_players, fee,
                 split, json.dumps(config), form.get("rules", "").strip()[:5000], team_mode, team_display, starts_on,
                 ends_on, now(), now())).lastrowid
            slug = f"{slugify(name)}-{tournament_id}"
            db.execute("UPDATE tournaments SET slug = ? WHERE id = ?", (slug, tournament_id))
            db.executemany("INSERT INTO tournament_teams (tournament_id, team_id) VALUES (?, ?)",
                           [(tournament_id, team_id) for team_id in team_ids])
            db.commit()
            flash("Torneio criado com inscrições abertas. Divulgue o link!", "success")
            if upload:
                _save_iso(store.get_tournament(db, tournament_id), upload)
            return redirect(url_for("tournaments.show", slug=slug))
    selected = {int(v) for v in form.getlist("team_ids") if v.isdigit()} if form else set()
    return render_template("tournaments/new.html", games=games, form=form, max_fee=max_fee,
                           format_labels=formats.FORMATS, defaults=formats.DEFAULTS, splits=PRIZE_SPLITS,
                           min_players=formats.MIN_PLAYERS, max_players=formats.MAX_PLAYERS,
                           team_modes=TEAM_MODES, catalog=_team_catalog(db), selected=selected, **_iso_limits())


@bp.post("/<slug>/inscrever")
@player_required
def enroll(slug):
    validate_csrf()
    tournament = _load(slug)
    team_id = request.form.get("team_id", type=int)
    _run(lambda db, t: store.enroll(db, t, session["player_id"], request.form.get("team_name", ""), team_id),
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
    open_reports = db.execute(
        "SELECT m.* FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
        "WHERE s.tournament_id = ? AND m.status IN ('disputed', 'reported') ORDER BY m.status = 'reported', m.id",
        (tournament["id"],)).fetchall()
    my_entry = db.execute("SELECT id FROM tournament_entries WHERE tournament_id = ? AND player_id = ?",
                          (tournament["id"], session.get("player_id"))).fetchone()
    iso_row = iso.get(db, tournament["id"])
    return render_template("tournaments/manage.html", t=tournament, entries=store.entries_of(db, tournament["id"]),
                           open_reports=open_reports, my_entry_id=None if is_admin() or not my_entry else my_entry[0],
                           current=current, names=_names(db, tournament), decisive=decisive,
                           can_advance=can_advance, can_finish=can_finish, is_admin=is_admin(),
                           status_labels=STATUS_LABELS, iso=iso_row, iso_uploading=iso.uploading(iso_row),
                           archive_configured=iso.archive_configured(current_app), **_iso_limits())


@bp.post("/<slug>/editar")
@manager_required
def edit(tournament):
    name = request.form.get("name", "").strip()
    try:
        if not 3 <= len(name) <= 80:
            raise TournamentError("O nome precisa ter entre 3 e 80 caracteres.")
        starts_on, ends_on = _dates(request.form)
    except TournamentError as error:
        flash(str(error), "error")
    else:
        db = get_db()
        db.execute("UPDATE tournaments SET name = ?, rules = ?, starts_on = ?, ends_on = ?, updated_at = ? WHERE id = ?",
                   (name, request.form.get("rules", "").strip()[:5000], starts_on, ends_on, now(), tournament["id"]))
        db.commit()
        flash("Torneio atualizado.", "success")
    return redirect(url_for("tournaments.manage", slug=tournament["slug"]))


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
             "Resultado apagado.")
    else:
        try:
            score = _score_form()
        except TournamentError as error:
            flash(str(error), "error")
        else:
            _run(lambda db, t: reports.resolve(db, t, match_id, score, user_id=user_id, entry=organizer,
                                               comment=comment), tournament, "Resultado salvo.")
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
    h2h = {"home": home["username"], "away": away["username"],
           "record": stats.player_record(db, home["player_id"], opponent_id=away["player_id"])} if home and away else None
    return render_template(
        "tournaments/match.html", t=tournament, m=m, names=names, me=me, manager=manager, h2h=h2h,
        history=reports.history(db, match_id), labels=reports.ACTION_LABELS,
        reported=reports.last_report(db, match_id) if m["status"] in ("reported", "disputed") else None,
        deadline=reports.deadline(db, m) if m["status"] == "reported" else None,
        decisive=store.needs_pens(db, m), open=tournament["status"] == "running" and m["stage_status"] == "running")


def _player_action(slug, match_id, action, success):
    tournament = _load(slug)
    validate_csrf()
    _run(lambda db, t: action(db, t), tournament, success)
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
        "Placar enviado. Agora o adversário confirma ou contesta.")


@bp.post("/<slug>/jogos/<int:match_id>/confirmar")
@player_required
def match_confirm(slug, match_id):
    return _player_action(slug, match_id, lambda db, t: reports.confirm(db, t, match_id, session["player_id"]),
                          "Resultado confirmado!")


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
        "Contestação enviada ao organizador.")


@bp.get("/meus-jogos")
@player_required
def my_matches():
    db = get_db()
    rows = reports.pending_for_player(db, session["player_id"])
    tournaments = {}
    for row in rows:
        if row["tournament_id"] not in tournaments:
            tournaments[row["tournament_id"]] = store.get_tournament(db, row["tournament_id"])
    names = {}
    for tournament in tournaments.values():
        names.update(_names(db, tournament))
    return render_template("tournaments/my_matches.html", rows=rows, names=names,
                           deadlines={r["id"]: reports.deadline(db, r) for r in rows if r["status"] == "reported"})


# --- Ranking, histórico e comparação (app/tournament/stats.py) ----------------

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
                           opponents=stats.opponents(db, target["id"], game), outcome=stats.outcome)


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
    return render_template("tournaments/compare.html", a=a, b=b, result=result, games=games, game=game,
                           players=stats.players_with_matches(db), outcome=stats.outcome)


_last_auto_confirm = [0.0]


@bp.before_app_request
def _auto_confirm_reports():
    """Confirma placares sem resposta no prazo - no máximo uma vez por minuto,
    a partir das páginas de torneio (não há cron no servidor)."""
    if not (request.path.startswith("/torneios") or request.path.startswith("/admin/torneios")):
        return
    if time.monotonic() - _last_auto_confirm[0] < 60:
        return
    _last_auto_confirm[0] = time.monotonic()
    try:
        reports.auto_confirm(get_db())
    except sqlite3.OperationalError:
        pass  # banco sem as tabelas ainda


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
    if query:
        sql += " AND t.name LIKE ?"
        params.append(f"%{query}%")
    if group_id:
        sql += " AND t.id IN (SELECT team_id FROM team_group_members WHERE group_id = ?)"
        params.append(group_id)
    teams = [dict(row, image=team_image(row)) for row in
             db.execute(sql + " ORDER BY t.name COLLATE NOCASE LIMIT 300", params)]
    groups = db.execute("SELECT g.*, (SELECT COUNT(*) FROM team_group_members m WHERE m.group_id = g.id) AS size "
                        "FROM team_groups g ORDER BY g.seq, g.name").fetchall()
    return render_template("tournaments/admin_teams.html", teams=teams, groups=groups, query=query,
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
        # Jogos esperando uma ação do jogador: sem placar, ou com placar do adversário para confirmar.
        todo = db.execute(
            "SELECT COUNT(*) FROM tournament_entries e JOIN tournaments t ON t.id = e.tournament_id "
            "JOIN tournament_stages s ON s.tournament_id = t.id AND s.status = 'running' "
            "JOIN tournament_matches m ON m.stage_id = s.id AND (m.home_entry_id = e.id OR m.away_entry_id = e.id) "
            "WHERE e.player_id = ? AND t.status = 'running' AND (m.status = 'scheduled' OR "
            "(m.status = 'reported' AND m.reported_by_entry_id != e.id))", (player_id,)).fetchone()[0]
    except sqlite3.OperationalError:
        return {}  # banco ainda sem as colunas da carteira
    return {"wallet_balance": row["wallet_cents"] if row else None, "matches_todo": todo}
