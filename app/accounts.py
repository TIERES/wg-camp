"""Contas de jogadores: validação de cadastro e tokens.

O nome de usuário é o nick usado no Kaillera e é fixo depois do cadastro -
o Memory Card online de cada jogador é identificado por ele, então permitir
trocá-lo deixaria um jogador assumir o cartão de outro.
"""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import current_app, request, url_for

from .db import now

# Nicks do Kaillera vão até 31 bytes. Sem espaço nem vírgula (a API recebe a
# lista de jogadores separada por vírgula) e só ASCII, para o nick do
# kailleraclient.dll bater byte a byte com o cadastro.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-\[\]()!+=~|]{2,31}$")
# E-mail: gravado e procurado sempre em minúsculas (normalize_email) e
# conferido por email_error.
EMAIL_LOCAL_CHAR_RE = re.compile(r"[a-z0-9._%+\-]")
EMAIL_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?$")
EMAIL_TLD_RE = re.compile(r"^[a-z]{2,24}$")
# Provedores gratuitos mais usados, pelo nome logo depois do @. Estes só
# existem com um final: qualquer outro é erro (gmail.com.br, bol.com).
EMAIL_FIXED_ENDINGS = {"gmail": "com", "googlemail": "com", "icloud": "com", "bol": "com.br", "uol": "com.br"}
# Estes têm finais por país (hotmail.es, yahoo.com.ar, outlook.pt...): só é
# erro o final com cara de .com / .com.br mal digitado.
EMAIL_COUNTRY_ENDINGS = {"hotmail", "outlook", "live", "msn", "yahoo", "ymail", "protonmail"}
# Nomes em que uma letra errada é erro de digitação (gmial, hotmal, outlok)...
EMAIL_TYPO_NAMES = ("gmail", "hotmail", "outlook", "yahoo", "icloud", "protonmail")
# ...menos estes, que são domínios de verdade.
EMAIL_NOT_TYPOS = {"mail", "email", "ymail", "cloud"}
# Finais que existem e ficam a uma letra de "com": não são erro de digitação
# num domínio qualquer (.cam, .bom, .mom; nom.br, cim.br, coz.br).
EMAIL_REAL_TLDS = {"cam", "bom", "mom"}
EMAIL_REAL_BR_CATEGORIES = {"nom", "cim", "coz"}
PASSWORD_MIN = 8
PASSWORD_MAX = 128

VERIFY_TTL = timedelta(hours=48)
RESET_TTL = timedelta(hours=1)
# Um cadastro nunca confirmado deixa de reservar o nome/e-mail depois disso,
# contado do último link de confirmação enviado (o do cadastro ou um reenvio).
UNVERIFIED_TTL = timedelta(hours=48)
# Cadastro pendente "vencido" (o p. da consulta; o parâmetro é o limite UNVERIFIED_TTL):
# nunca confirmado, feito antes do limite e sem link de confirmação enviado depois dele.
_STALE = ("p.email_verified_at IS NULL AND p.created_at < ? AND NOT EXISTS (SELECT 1 FROM player_tokens t "
          "WHERE t.player_id = p.id AND t.kind = 'verify' AND t.created_at >= ?)")
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


USERNAME_ALLOWED = "letras sem acento, números ou _ . - [ ] ( ) ! + = ~ |"


def username_error(username):
    """Erro do usuário (nick do Kaillera), dizendo o que está errado - a mesma
    conferência é feita no navegador antes do envio (players/register.html)."""
    username = username or ""
    if not username:
        return "Informe o usuário (o seu nick do Kaillera)."
    if any(ch.isspace() for ch in username):
        return "O usuário não pode ter espaços: o Kaillera não aceita espaço no nick."
    if any(ord(ch) > 127 for ch in username):
        return ("O usuário não pode ter acentos nem ç (ex.: á, ã, é, ç): eles quebram o nick no Kaillera. "
                "Troque por letras sem acento.")
    if not 2 <= len(username) <= 31:
        return "O usuário deve ter de 2 a 31 caracteres."
    if not USERNAME_RE.match(username):
        bad = "".join(sorted({ch for ch in username if not USERNAME_RE.match(ch * 2)}))
        return f"O usuário tem caractere não permitido ({bad}). Use {USERNAME_ALLOWED}."
    return None


def normalize_email(email):
    """E-mail como é gravado e procurado: sem espaços nas pontas e em minúsculas."""
    return (email or "").strip().lower()


def _near(word, target):
    """word é target com um erro de digitação - uma letra trocada, faltando ou
    sobrando, ou duas vizinhas invertidas?"""
    if word == target or abs(len(word) - len(target)) > 1:
        return False
    if len(word) == len(target):
        diff = [i for i, (a, b) in enumerate(zip(word, target)) if a != b]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1
                                  and word[diff[0]] == target[diff[1]] and word[diff[1]] == target[diff[0]])
    short, longer = sorted((word, target), key=len)
    return any(longer[:i] + longer[i + 1:] == short for i in range(len(longer)))


def _fixed_ending(ending, big_provider):
    """Final do domínio (as partes depois do nome) com cara de .com ou .com.br
    mal digitado -> o final certo; senão, o mesmo final. Num domínio qualquer,
    final de duas letras pode ser de país (.co, .cm) e fica como está."""
    last = ending[-1]
    if last == "combr":
        return ending[:-1] + ["com", "br"]
    if len(ending) >= 2:
        before = ending[-2]
        if last == "br" and _near(before, "com") and before not in EMAIL_REAL_BR_CATEGORIES:
            return ending[:-2] + ["com", "br"]
        if before == "com" and _near(last, "br") and len(last) != 2:  # com.ar, com.tr... existem
            return ending[:-2] + ["com", "br"]
    if _near(last, "com") and (big_provider or (len(last) != 2 and last not in EMAIL_REAL_TLDS)):
        return ending[:-1] + ["com"]
    return ending


def _email_provider(name):
    """Provedor conhecido com esse nome, ou do qual o nome é erro de digitação."""
    if name in EMAIL_FIXED_ENDINGS or name in EMAIL_COUNTRY_ENDINGS:
        return name
    if name in EMAIL_NOT_TYPOS:
        return None
    return next((known for known in EMAIL_TYPO_NAMES if _near(name, known)), None)


def _fixed_domain(labels):
    """Partes do domínio sem os erros de digitação reconhecidos (as mesmas
    quando não há o que corrigir); None quando falta o final (nome@gmail)."""
    name, ending = labels[0], labels[1:]
    if not ending:
        # Sem ponto: "gmail", "gmailcom", "hotmailcombr".
        for glued in ("combr", "com"):
            if name.endswith(glued) and _email_provider(name[:-len(glued)]):
                name, ending = name[:-len(glued)], (["com", "br"] if glued == "combr" else ["com"])
                break
    provider = _email_provider(name)
    if provider in EMAIL_FIXED_ENDINGS:
        return [provider] + EMAIL_FIXED_ENDINGS[provider].split(".")
    if provider:
        return [provider] + (_fixed_ending(ending, True) if ending else ["com"])
    return [name] + _fixed_ending(ending, False) if ending else None


def email_error(email):
    """Erro do e-mail (já passado por normalize_email), dizendo o que está
    errado, ou None. Além do formato, pega o que mais faz o link de confirmação
    nunca chegar: domínio dos provedores gratuitos mal digitado (gmial.com,
    hotmail.con, gmail.com.br), com a sugestão do endereço certo."""
    email = email or ""
    if not email:
        return "Informe o e-mail."
    if any(ch.isspace() for ch in email):
        return "O e-mail não pode ter espaços."
    if email != email.lower():
        return "Use apenas letras minúsculas no e-mail."
    local, at, domain = email.partition("@")
    if not at or not local or not domain or "@" in domain or len(local) > 64 or len(email) > 254:
        return "Informe um e-mail válido, como nome@gmail.com."
    if any(ord(ch) > 127 for ch in email):
        return "O e-mail não pode ter acentos nem ç."
    bad = "".join(sorted({ch for ch in local if not EMAIL_LOCAL_CHAR_RE.match(ch)}))
    if bad:
        return f"O e-mail tem caractere não permitido antes do @ ({bad})."
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return "Antes do @, o e-mail não pode começar nem terminar com ponto, nem ter dois pontos seguidos."
    labels = domain.split(".")
    if not all(EMAIL_LABEL_RE.match(label) for label in labels):
        return f"O endereço depois do @ ({domain}) não é válido."
    fixed = _fixed_domain(labels)
    if fixed is None:
        return "Falta o final do e-mail depois do @ (como em gmail.com)."
    if fixed != labels:
        return f"\"{domain}\" parece ter erro de digitação. Você quis dizer {local}@{'.'.join(fixed)}?"
    if not EMAIL_TLD_RE.match(labels[-1]):
        return f"O endereço depois do @ ({domain}) não é válido."
    if domain in ("gmail.com", "googlemail.com"):
        # Regras do Gmail para o nome (o que vem depois de um "+" é livre).
        user = local.split("+", 1)[0]
        if not re.fullmatch(r"[a-z0-9.]+", user):
            return "No Gmail, antes do @ só há letras, números e pontos (não existe _ nem -). Confira o seu e-mail."
        if len(user) < 6 or len(user.replace(".", "")) > 30:
            return "No Gmail, o nome antes do @ tem de 6 a 30 caracteres. Confira o seu e-mail."
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
    """Libera nome/e-mail presos por um cadastro nunca confirmado e sem link de
    confirmação enviado nas últimas UNVERIFIED_TTL."""
    limit = iso(utcnow() - UNVERIFIED_TTL)
    db.execute(
        f"DELETE FROM players WHERE id IN (SELECT p.id FROM players p WHERE {_STALE} "
        "AND (p.username = ? COLLATE NOCASE OR p.email = ? COLLATE NOCASE))",
        (limit, limit, username, email),
    )


def already_registered(db, field, value, except_id=None):
    """Já existe conta com esse usuário/e-mail (sem diferenciar maiúsculas)? Um
    cadastro nunca confirmado e sem link enviado há mais que UNVERIFIED_TTL não
    conta - o novo cadastro o apaga (purge_stale_unverified). except_id: a
    própria conta (a administração corrigindo o e-mail dela)."""
    column = {"username": "username", "email": "email"}[field]
    limit = iso(utcnow() - UNVERIFIED_TTL)
    return db.execute(
        f"SELECT 1 FROM players p WHERE p.{column} = ? COLLATE NOCASE AND p.id != ? AND NOT ({_STALE})",
        ((value or "").strip(), except_id or 0, limit, limit)).fetchone() is not None


def revoke_api_tokens(db, player_id):
    db.execute(
        "UPDATE player_tokens SET used_at = ? WHERE player_id = ? AND kind = 'api' AND used_at IS NULL",
        (now(), player_id),
    )


def absolute_url(path):
    """Caminho do site ("/torneios/copa") -> URL completa, para quem lê a página
    de fora (as tags og: do link compartilhado). Como em external_url, vale o
    PUBLIC_URL: atrás do Caddy o Flask só vê http."""
    base = (current_app.config.get("PUBLIC_URL") or request.host_url).rstrip("/")
    return base + path


def external_url(endpoint, **values):
    base = (current_app.config.get("PUBLIC_URL") or "").rstrip("/")
    if base:
        return base + url_for(endpoint, **values)
    return url_for(endpoint, _external=True, **values)
