import secrets
from functools import wraps
from urllib.parse import urlsplit

from flask import abort, current_app, redirect, request, session, url_for


def logged_in():
    """Alguém entrou: um jogador (Conta WE Camp) ou a administração."""
    return "player_id" in session or "user_id" in session


def safe_next(target):
    """O "next" do login: só um caminho deste site ("/torneios/..."), nunca
    outro endereço (//outro.site, /\\outro.site)."""
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return None
    return target


def login_redirect():
    """Página que pede o login: vai para /conta/entrar e, depois de entrar, volta
    para ela. Num formulário enviado (POST, ex. sessão expirada) a volta é a
    página de onde ele saiu (Referer) - o endereço do POST não abre com GET."""
    if request.method == "GET":
        target = request.full_path.rstrip("?")
    else:
        ref = urlsplit(request.referrer or "")
        target = None
        if ref.netloc == request.host:
            target = ref.path + (f"?{ref.query}" if ref.query else "")
    target = safe_next(target)
    if target and target.startswith(url_for("players.login")):
        target = None
    return redirect(url_for("players.login", next=target) if target else url_for("players.login"))


def deny(code):
    """Página que existe mas não é para quem pediu: sem login, pede o login
    (login_redirect); logado, o erro (403 ou 404)."""
    if not logged_in():
        abort(login_redirect())
    abort(code)


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def validate_csrf():
    token = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(token, expected):
        abort(400, "Formulário inválido ou expirado.")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("admin.login"))
        return view(*args, **kwargs)
    return wrapped


def init_template_helpers(app):
    app.jinja_env.globals["csrf_token"] = csrf_token
