"""Contas de jogadores: validação de cadastro e tokens.

O nome de usuário é o nick usado no Kaillera e é fixo depois do cadastro -
o Memory Card online de cada jogador é identificado por ele, então permitir
trocá-lo deixaria um jogador assumir o cartão de outro.
"""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import current_app, url_for

from .db import now

# Nicks do Kaillera vão até 31 bytes. Sem espaço nem vírgula (a API recebe a
# lista de jogadores separada por vírgula) e só ASCII, para o nick do
# kailleraclient.dll bater byte a byte com o cadastro.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-\[\]()!+=~|]{2,31}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
PASSWORD_MIN = 8
PASSWORD_MAX = 128

VERIFY_TTL = timedelta(hours=48)
RESET_TTL = timedelta(hours=1)
# Um cadastro nunca confirmado deixa de reservar o nome/e-mail depois disso.
UNVERIFIED_TTL = timedelta(hours=48)
# Intervalo mínimo entre dois e-mails do mesmo tipo para a mesma conta.
MAIL_COOLDOWN = timedelta(minutes=2)


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.isoformat()


def parse_iso(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def username_error(username):
    if not USERNAME_RE.match(username or ""):
        return ("O usuário deve ter de 2 a 31 caracteres, sem espaços, usando letras, números "
                "ou _ . - [ ] ( ) ! + = ~ |. Use exatamente o nick do Kaillera.")
    return None


def email_error(email):
    if not EMAIL_RE.match(email or "") or len(email) > 254:
        return "Informe um e-mail válido."
    return None


def password_error(password, confirm=None):
    if len(password or "") < PASSWORD_MIN:
        return f"A senha deve ter pelo menos {PASSWORD_MIN} caracteres."
    if len(password) > PASSWORD_MAX:
        return f"A senha deve ter no máximo {PASSWORD_MAX} caracteres."
    if confirm is not None and password != confirm:
        return "As senhas não conferem."
    return None


def token_hash(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_token(db, player_id, kind):
    """Cria um token e devolve o valor em texto - só o hash fica no banco.
    Tokens de confirmação/redefinição anteriores do mesmo tipo são anulados."""
    raw = secrets.token_urlsafe(32)
    expires = None
    if kind == "verify":
        expires = iso(utcnow() + VERIFY_TTL)
    elif kind == "reset":
        expires = iso(utcnow() + RESET_TTL)
    if kind in ("verify", "reset"):
        db.execute(
            "UPDATE player_tokens SET used_at = ? WHERE player_id = ? AND kind = ? AND used_at IS NULL",
            (now(), player_id, kind),
        )
    db.execute(
        "INSERT INTO player_tokens (player_id, kind, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (player_id, kind, token_hash(raw), now(), expires),
    )
    return raw


def find_token(db, raw, kind):
    """Token válido (não usado, não expirado) com os dados do jogador, ou None."""
    if not raw:
        return None
    row = db.execute(
        "SELECT t.id AS token_id, t.expires_at, t.used_at, p.* FROM player_tokens t "
        "JOIN players p ON p.id = t.player_id WHERE t.token_hash = ? AND t.kind = ?",
        (token_hash(raw), kind),
    ).fetchone()
    if not row or row["used_at"]:
        return None
    if row["expires_at"]:
        expires = parse_iso(row["expires_at"])
        if not expires or expires < utcnow():
            return None
    return row


def recently_sent(db, player_id, kind):
    row = db.execute(
        "SELECT created_at FROM player_tokens WHERE player_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (player_id, kind),
    ).fetchone()
    created = parse_iso(row["created_at"]) if row else None
    return bool(created and utcnow() - created < MAIL_COOLDOWN)


def purge_stale_unverified(db, username, email):
    """Libera nome/e-mail presos por um cadastro antigo nunca confirmado."""
    limit = iso(utcnow() - UNVERIFIED_TTL)
    db.execute(
        "DELETE FROM players WHERE email_verified_at IS NULL AND created_at < ? "
        "AND (username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE)",
        (limit, username, email),
    )


def revoke_api_tokens(db, player_id):
    db.execute(
        "UPDATE player_tokens SET used_at = ? WHERE player_id = ? AND kind = 'api' AND used_at IS NULL",
        (now(), player_id),
    )


def external_url(endpoint, **values):
    base = (current_app.config.get("PUBLIC_URL") or "").rstrip("/")
    if base:
        return base + url_for(endpoint, **values)
    return url_for(endpoint, _external=True, **values)
