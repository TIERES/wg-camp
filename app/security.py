import secrets
from functools import wraps

from flask import abort, current_app, request, session


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
            from flask import redirect, url_for
            return redirect(url_for("admin.login"))
        return view(*args, **kwargs)
    return wrapped


def init_template_helpers(app):
    app.jinja_env.globals["csrf_token"] = csrf_token
