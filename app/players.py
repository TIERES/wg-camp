"""Cadastro de jogadores e "Meus Memory Cards".

A conta é requisito para o Memory Card online. O cadastro só vale depois de
confirmado pelo link enviado por e-mail - é por esse e-mail que o jogador
recupera o nome de usuário e redefine a senha. O nome de usuário (= nick do
Kaillera) não pode ser alterado depois.
"""
import sqlite3
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .accounts import (email_error, external_url, find_token, issue_token, password_error,
                       purge_stale_unverified, recently_sent, revoke_api_tokens, username_error)
from .db import get_db, now
from .mailer import MailError, send_mail
from .memcards import add_version, card_error, card_path, get_or_create_memcard
from .security import validate_csrf

bp = Blueprint("players", __name__, url_prefix="/conta")


def player_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "player_id" not in session:
            return redirect(url_for("players.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def current_player():
    if "player_id" not in session:
        return None
    return get_db().execute("SELECT * FROM players WHERE id = ?", (session["player_id"],)).fetchone()


def _send_verification(db, player):
    token = issue_token(db, player["id"], "verify")
    db.commit()
    link = external_url("players.confirm", token=token)
    send_mail(player["email"], "Confirme seu cadastro - WE Camp", (
        f"Olá, {player['username']}!\n\n"
        "Para ativar sua conta e usar o Memory Card online, confirme seu cadastro "
        f"abrindo o link abaixo (válido por 48 horas):\n\n{link}\n\n"
        f"Seu usuário (o mesmo nick do Kaillera): {player['username']}\n\n"
        "Se você não fez este cadastro, ignore esta mensagem.\n"
    ))


def _send_reset(db, player):
    token = issue_token(db, player["id"], "reset")
    db.commit()
    link = external_url("players.reset", token=token)
    send_mail(player["email"], "Recuperar acesso - WE Camp", (
        f"Olá!\n\nSeu usuário é: {player['username']}\n\n"
        "Para criar uma nova senha, abra o link abaixo (válido por 1 hora):\n\n"
        f"{link}\n\n"
        "Se você não pediu isso, ignore esta mensagem - sua senha continua a mesma.\n"
    ))


@bp.route("/cadastro", methods=("GET", "POST"))
def register():
    form = {"username": "", "email": ""}
    if request.method == "POST":
        validate_csrf()
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        form = {"username": username, "email": email}
        error = (username_error(username) or email_error(email)
                 or password_error(password, request.form.get("password_confirm", "")))
        if not error and request.form.get("accept") != "on":
            error = "Confirme que o usuário é o seu nick do Kaillera e que ele não poderá ser alterado."
        if not error:
            db = get_db()
            purge_stale_unverified(db, username, email)
            try:
                db.execute(
                    "INSERT INTO players (username, email, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (username, email, generate_password_hash(password), now(), now()),
                )
            except sqlite3.IntegrityError:
                db.rollback()
                taken = db.execute("SELECT 1 FROM players WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
                error = ("Este usuário já está cadastrado." if taken
                         else "Este e-mail já está cadastrado. Use \"Esqueci minha senha\" para recuperar o acesso.")
            else:
                player = db.execute("SELECT * FROM players WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
                try:
                    _send_verification(db, player)
                except MailError:
                    flash("Cadastro criado, mas não foi possível enviar o e-mail agora. "
                          "Use \"Reenviar confirmação\" em alguns minutos.", "error")
                    return redirect(url_for("players.resend"))
                return render_template("players/message.html", title="Confirme seu e-mail", message=(
                    f"Enviamos um link de confirmação para {email}. Abra-o em até 48 horas para "
                    "ativar sua conta. Verifique também a caixa de spam."))
        flash(error, "error")
    return render_template("players/register.html", form=form)


@bp.get("/confirmar/<token>")
def confirm(token):
    db = get_db()
    row = find_token(db, token, "verify")
    if not row:
        return render_template("players/message.html", title="Link inválido", message=(
            "Este link de confirmação é inválido ou expirou. Peça um novo em \"Reenviar confirmação\"."),
            resend=True), 400
    db.execute("UPDATE player_tokens SET used_at = ? WHERE id = ?", (now(), row["token_id"]))
    if not row["email_verified_at"]:
        db.execute("UPDATE players SET email_verified_at = ?, updated_at = ? WHERE id = ?", (now(), now(), row["id"]))
    db.commit()
    flash("Cadastro confirmado! Agora é só entrar.", "success")
    return redirect(url_for("players.login"))


@bp.route("/reenviar-confirmacao", methods=("GET", "POST"))
def resend():
    if request.method == "POST":
        validate_csrf()
        email = request.form.get("email", "").strip()
        db = get_db()
        player = db.execute(
            "SELECT * FROM players WHERE email = ? COLLATE NOCASE AND email_verified_at IS NULL", (email,)
        ).fetchone()
        if player and not recently_sent(db, player["id"], "verify"):
            try:
                _send_verification(db, player)
            except MailError:
                flash("Não foi possível enviar o e-mail agora. Tente novamente em alguns minutos.", "error")
                return render_template("players/resend.html")
        return render_template("players/message.html", title="Verifique seu e-mail", message=(
            "Se existir um cadastro ainda não confirmado com esse e-mail, enviamos um novo link de confirmação."))
    return render_template("players/resend.html")


@bp.route("/entrar", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        validate_csrf()
        login_value = request.form.get("username", "").strip()
        db = get_db()
        player = db.execute(
            "SELECT * FROM players WHERE username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE",
            (login_value, login_value),
        ).fetchone()
        if not player or not check_password_hash(player["password_hash"], request.form.get("password", "")):
            flash("Usuário ou senha inválidos.", "error")
        elif not player["email_verified_at"]:
            flash("Confirme seu cadastro pelo link enviado ao seu e-mail antes de entrar.", "error")
            return redirect(url_for("players.resend"))
        else:
            admin_id = session.get("user_id")
            session.clear()
            if admin_id:
                session["user_id"] = admin_id
            session["player_id"] = player["id"]
            session["csrf_token"] = __import__("secrets").token_urlsafe(32)
            db.execute("UPDATE players SET last_login_at = ? WHERE id = ?", (now(), player["id"]))
            db.commit()
            target = request.args.get("next", "")
            if not target.startswith("/conta"):
                target = url_for("players.account")
            return redirect(target)
    return render_template("players/login.html")


@bp.post("/sair")
def logout():
    validate_csrf()
    session.pop("player_id", None)
    return redirect(url_for("public.index"))


@bp.route("/recuperar", methods=("GET", "POST"))
def forgot():
    if request.method == "POST":
        validate_csrf()
        email = request.form.get("email", "").strip()
        db = get_db()
        player = db.execute(
            "SELECT * FROM players WHERE email = ? COLLATE NOCASE AND email_verified_at IS NOT NULL", (email,)
        ).fetchone()
        if player and not recently_sent(db, player["id"], "reset"):
            try:
                _send_reset(db, player)
            except MailError:
                flash("Não foi possível enviar o e-mail agora. Tente novamente em alguns minutos.", "error")
                return render_template("players/forgot.html")
        return render_template("players/message.html", title="Verifique seu e-mail", message=(
            "Se existir uma conta confirmada com esse e-mail, enviamos uma mensagem com o seu usuário "
            "e um link para criar uma nova senha (válido por 1 hora)."))
    return render_template("players/forgot.html")


@bp.route("/redefinir/<token>", methods=("GET", "POST"))
def reset(token):
    db = get_db()
    row = find_token(db, token, "reset")
    if not row:
        return render_template("players/message.html", title="Link inválido", message=(
            "Este link é inválido ou expirou. Peça um novo em \"Esqueci minha senha\"."), forgot=True), 400
    if request.method == "POST":
        validate_csrf()
        password = request.form.get("password", "")
        error = password_error(password, request.form.get("password_confirm", ""))
        if error:
            flash(error, "error")
        else:
            db.execute("UPDATE players SET password_hash = ?, updated_at = ? WHERE id = ?",
                       (generate_password_hash(password), now(), row["id"]))
            db.execute("UPDATE player_tokens SET used_at = ? WHERE id = ?", (now(), row["token_id"]))
            revoke_api_tokens(db, row["id"])
            db.commit()
            flash("Senha alterada. Entre com a nova senha (no Kaillera também).", "success")
            return redirect(url_for("players.login"))
    return render_template("players/reset.html", username=row["username"])


@bp.get("/")
@player_required
def account():
    db = get_db()
    player = current_player()
    if not player:
        session.pop("player_id", None)
        return redirect(url_for("players.login"))
    cards = db.execute(
        "SELECT m.id, m.current_version, m.updated_at, g.name AS game_name, g.content_id, v.source, v.note "
        "FROM memcards m JOIN games g ON g.id = m.game_id "
        "JOIN memcard_versions v ON v.memcard_id = m.id AND v.version = m.current_version "
        "WHERE m.player_id = ? ORDER BY g.name COLLATE NOCASE",
        (player["id"],),
    ).fetchall()
    games = db.execute("SELECT id, name, content_id FROM games ORDER BY name COLLATE NOCASE").fetchall()
    tokens = db.execute(
        "SELECT id, created_at, last_used_at FROM player_tokens "
        "WHERE player_id = ? AND kind = 'api' AND used_at IS NULL ORDER BY id DESC",
        (player["id"],),
    ).fetchall()
    return render_template("players/account.html", player=player, cards=cards, games=games, tokens=tokens)


@bp.post("/senha")
@player_required
def change_password():
    validate_csrf()
    db = get_db()
    player = current_player()
    if not check_password_hash(player["password_hash"], request.form.get("current_password", "")):
        flash("Senha atual incorreta.", "error")
        return redirect(url_for("players.account"))
    password = request.form.get("password", "")
    error = password_error(password, request.form.get("password_confirm", ""))
    if error:
        flash(error, "error")
        return redirect(url_for("players.account"))
    db.execute("UPDATE players SET password_hash = ?, updated_at = ? WHERE id = ?",
               (generate_password_hash(password), now(), player["id"]))
    revoke_api_tokens(db, player["id"])
    db.commit()
    flash("Senha alterada. Os PCs conectados precisam entrar de novo no Kaillera.", "success")
    return redirect(url_for("players.account"))


@bp.post("/conexoes/<int:token_id>/revogar")
@player_required
def revoke_token(token_id):
    validate_csrf()
    db = get_db()
    db.execute(
        "UPDATE player_tokens SET used_at = ? WHERE id = ? AND player_id = ? AND kind = 'api'",
        (now(), token_id, session["player_id"]),
    )
    db.commit()
    flash("Conexão removida.", "success")
    return redirect(url_for("players.account"))


def _own_card(memcard_id):
    card = get_db().execute(
        "SELECT m.*, g.name AS game_name, g.content_id FROM memcards m JOIN games g ON g.id = m.game_id "
        "WHERE m.id = ? AND m.player_id = ?",
        (memcard_id, session["player_id"]),
    ).fetchone()
    if not card:
        abort(404)
    return card


@bp.post("/memory-cards/enviar")
@player_required
def upload_card():
    validate_csrf()
    db = get_db()
    game = db.execute("SELECT * FROM games WHERE id = ?", (request.form.get("game_id", type=int),)).fetchone()
    upload = request.files.get("file")
    if not game:
        flash("Escolha o jogo (ISO) deste Memory Card.", "error")
        return redirect(url_for("players.account"))
    if not upload or not upload.filename:
        flash("Escolha o arquivo do Memory Card.", "error")
        return redirect(url_for("players.account"))
    data = upload.read(256 * 1024 + 1)
    error = card_error(data)
    if error:
        flash(error, "error")
        return redirect(url_for("players.account"))
    card = get_or_create_memcard(db, session["player_id"], game["id"])
    version = add_version(db, card["id"], data, "upload", f"Enviado pelo site: {upload.filename[:100]}")
    db.commit()
    flash(f"Memory Card de {game['name']} atualizado (versão {version}).", "success")
    return redirect(url_for("players.card_history", memcard_id=card["id"]))


@bp.get("/memory-cards/<int:memcard_id>")
@player_required
def card_history(memcard_id):
    card = _own_card(memcard_id)
    versions = get_db().execute(
        "SELECT * FROM memcard_versions WHERE memcard_id = ? ORDER BY version DESC", (memcard_id,)
    ).fetchall()
    return render_template("players/memcard.html", card=card, versions=versions)


@bp.get("/memory-cards/<int:memcard_id>/baixar")
@player_required
def download_card(memcard_id):
    card = _own_card(memcard_id)
    db = get_db()
    version_number = request.args.get("versao", type=int) or card["current_version"]
    version = db.execute(
        "SELECT * FROM memcard_versions WHERE memcard_id = ? AND version = ?", (memcard_id, version_number)
    ).fetchone()
    if not version or not card_path(version["sha256"]).exists():
        abort(404)
    player = current_player()
    safe_game = "".join(ch if ch.isalnum() else "_" for ch in card["game_name"])[:60]
    return send_file(card_path(version["sha256"]), mimetype="application/octet-stream", as_attachment=True,
                     download_name=f"{player['username']}_{safe_game}_v{version_number}.mcd")


@bp.post("/memory-cards/<int:memcard_id>/restaurar")
@player_required
def restore_card(memcard_id):
    validate_csrf()
    card = _own_card(memcard_id)
    db = get_db()
    version = db.execute(
        "SELECT * FROM memcard_versions WHERE memcard_id = ? AND version = ?",
        (memcard_id, request.form.get("version", type=int)),
    ).fetchone()
    if not version or not card_path(version["sha256"]).exists():
        abort(404)
    if version["version"] == card["current_version"]:
        flash("Esta já é a versão atual.", "error")
    else:
        data = card_path(version["sha256"]).read_bytes()
        new_version = add_version(db, memcard_id, data, "restore", f"Restaurada a versão {version['version']}")
        db.commit()
        flash(f"Versão {version['version']} restaurada como versão {new_version}.", "success")
    return redirect(url_for("players.card_history", memcard_id=memcard_id))


@bp.app_context_processor
def inject_player():
    return {"player_logged_in": "player_id" in session}

