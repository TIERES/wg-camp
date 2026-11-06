"""Imagens enviadas pelos jogadores: foto do perfil (avatar) e imagem do torneio.

O navegador já reduz a imagem antes de enviar (static/js/image-resize.js: a foto
vira um quadrado de 256 px, a do torneio no máximo 600 px), então o servidor só
confere o tipo pelo conteúdo (PNG, JPEG ou WebP; SVG poderia levar script) e o
tamanho. Cada envio ganha um nome aleatório: trocar ou remover apaga o arquivo
antigo sem risco de outro registro apontar para ele.
"""
import re
import secrets
from pathlib import Path

from flask import Blueprint, abort, current_app, g, send_from_directory, url_for

from .db import get_db

bp = Blueprint("images", __name__, url_prefix="/imagens")

# tipo -> (config com a pasta, tamanho máximo)
KINDS = {
    "avatar": ("AVATARS_DIR", 1024 * 1024),
    "torneio": ("TOURNAMENT_IMAGES_DIR", 2 * 1024 * 1024),
}
TYPES = {b"\x89PNG": ".png", b"\xff\xd8\xff": ".jpg", b"RIFF": ".webp"}
NAME_RE = re.compile(r"^[0-9a-f]{24}\.(png|jpg|webp)$")


class ImageError(ValueError):
    pass


def image_extension(data):
    """Extensão pelos primeiros bytes (só PNG, JPEG e WebP), ou None."""
    for magic, ext in TYPES.items():
        if data.startswith(magic) and (ext != ".webp" or data[8:12] == b"WEBP"):
            return ext
    return None


def _folder(kind):
    return Path(current_app.config[KINDS[kind][0]])


def save(kind, upload):
    """Grava o arquivo enviado e devolve o nome; ImageError se não servir."""
    limit = KINDS[kind][1]
    data = upload.read(limit + 1)
    ext = image_extension(data)
    if not ext or len(data) > limit:
        raise ImageError(f"Imagem inválida: envie PNG, JPG ou WebP de até {limit // 1024 // 1024} MB.")
    name = secrets.token_hex(12) + ext
    folder = _folder(kind)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    return name


def delete(kind, name):
    if name and NAME_RE.match(name):
        (_folder(kind) / name).unlink(missing_ok=True)


def url(kind, name):
    return url_for("images.serve", kind=kind, name=name) if name else None


@bp.app_template_global()
def image_url(kind, name):
    return url(kind, name)


@bp.app_template_global()
def player_avatar(player_id):
    """URL da foto do jogador (ou None). Um mapa por requisição com quem tem foto,
    para as listas (ranking, inscritos) não precisarem trazer a coluna."""
    if "avatars" not in g:
        g.avatars = {row[0]: row[1] for row in get_db().execute(
            "SELECT id, avatar FROM players WHERE avatar IS NOT NULL")}
    return url("avatar", g.avatars.get(player_id))


@bp.get("/<kind>/<name>")
def serve(kind, name):
    if kind not in KINDS or not NAME_RE.match(name):
        abort(404)
    # Nome novo a cada envio: o arquivo nunca muda, pode ficar no cache.
    response = send_from_directory(_folder(kind), name, max_age=30 * 86400)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
