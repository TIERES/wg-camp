from pathlib import Path

from flask import Blueprint, Response, abort, current_app, redirect, render_template, request, send_from_directory, url_for

from .accounts import absolute_url
from .db import get_db, get_setting
from .storage import attachment_header

bp = Blueprint("public", __name__)

# Prévia padrão dos links do portal (WhatsApp, Telegram, Discord...): toda página
# que não monta a sua (campeonato, ISO) sai com o título da aba, a descrição da
# página (ou a geral) e o cartão da logo do WE Camp (static/images/logo/wecamp-share.jpg,
# 1200 x 630 - as variações menores da logo weCamp.png). O convite vem na frente:
# o WhatsApp corta o fim da descrição quando ela não cabe.
SHARE_IMAGE = "images/logo/wecamp-share.jpg"
SHARE_DESCRIPTION = ("Campeonatos, amistosos, ranking e partidas ao vivo de Winning Eleven online. "
                     "Cadastre-se e entre em campo!")
SHARE_DESCRIPTIONS = {
    "public.index": "Entre em campo! Campeonatos, amistosos, ranking, replays e partidas ao vivo de Winning Eleven "
                    "online, com o RetroArch TIERES.",
    "players.register": "Cadastre-se no WE Camp: dispute campeonatos, registre suas partidas, suba de rank e "
                        "acompanhe as suas estatísticas.",
    "players.login": "Entre na sua conta WE Camp: seus jogos, campeonatos, amistosos e carteira W$.",
    "tournaments.index": "Inscreva-se nos campeonatos de Winning Eleven online: jogue pelo Kaillera e dispute W$, "
                         "troféus e o topo do ranking.",
    "tournaments.ranking": "Ranking dos jogadores do WE Camp: pontos, vitórias, aproveitamento e o rank por XP.",
    "tournaments.player": "Perfil no WE Camp: rank, estatísticas, troféus e os últimos resultados.",
    "tournaments.compare": "Compare jogadores do WE Camp: confronto direto, campanha e troféus.",
    "tournaments.match": "Jogo de campeonato no WE Camp: placar, gols e histórico da partida.",
    "tournaments.friendlies": "Amistosos no WE Camp: desafie os seus amigos, jogue pelo Kaillera e registre o placar.",
    "tournaments.friendly_show": "Amistoso no WE Camp: placar, gols e histórico da partida.",
    "live.index": "Winning Eleven ao vivo: placar, gols, cartões e estatísticas das partidas acontecendo agora "
                  "no WE Camp.",
    "replays.index": "Replays das partidas do WE Camp para assistir no RetroArch TIERES.",
}


@bp.app_template_global()
def site_share(title):
    """As tags og: padrão (templates/_share.html) da página atual - base.html usa
    quando a página não preenche o bloco `share`. `title`: o título da aba."""
    title = " ".join(str(title).split()) or "WE Camp"
    return {
        "url": absolute_url(request.full_path.rstrip("?")),
        "title": title,
        "description": SHARE_DESCRIPTIONS.get(request.endpoint, SHARE_DESCRIPTION),
        "image": absolute_url(url_for("static", filename=SHARE_IMAGE)),
        "image_alt": "Logo do WE Camp - Winning Eleven",
        "card": True,
    }

# Ajuste da administração (painel): mostra os cards de atualização manual
# ("Atualização light" e "kailleraclient.dll"). Ocultos por padrão desde
# 2026-10-15 - dentro do RetroArch TIERES o DLL baixa o pacote sozinho.
SETTING_MANUAL_UPDATE_CARDS = "mostrar_atualizacao_manual"


def manual_update_cards_visible():
    return get_setting(SETTING_MANUAL_UPDATE_CARDS, "0") == "1"


def _championship_game(db, files, championship_name=""):
    """O jogo (name, code) da ISO do campeonato, para a capa do card: pelo
    conteúdo (CRC32:TAMANHO) no perfil da ISO, no cadastro de amistosos ou num
    campeonato do portal, ou pelo nome; sem nada, o Winning Eleven 2002."""
    for entry in files:
        if not entry["content_id"]:
            continue
        for sql in ("SELECT game_title_id FROM iso_profiles WHERE content_id = ? COLLATE NOCASE AND game_title_id IS NOT NULL",
                    "SELECT game_title_id FROM catalog_isos WHERE content_id = ? COLLATE NOCASE AND is_active = 1",
                    "SELECT t.game_title_id FROM tournament_isos i JOIN tournaments t ON t.id = i.tournament_id "
                    "WHERE i.content_id = ? COLLATE NOCASE"):
            row = db.execute(sql + " LIMIT 1", (entry["content_id"],)).fetchone()
            if row and row[0]:
                game = db.execute("SELECT name, code FROM game_titles WHERE id = ?", (row[0],)).fetchone()
                if game:
                    return game
    # ISO sem cadastro: o nome do jogo no nome do campeonato ou do arquivo
    # ("Winning Eleven 3 Final" -> Winning Eleven 3), o título mais longo primeiro.
    names = " ".join([name or "" for name in [championship_name] + [entry["original_filename"] for entry in files]]).lower()
    titles = db.execute("SELECT name, code FROM game_titles").fetchall()
    for game in sorted(titles, key=lambda g: len(g["name"]), reverse=True):
        if game["name"].lower() in names:
            return game
    return db.execute("SELECT name, code FROM game_titles WHERE code = 'we2002'").fetchone()


@bp.get("/")
def index():
    db = get_db()
    championships = db.execute("SELECT * FROM championships WHERE is_published = 1 ORDER BY start_date DESC, created_at DESC").fetchall()
    files_by_championship = {}
    if championships:
        placeholders = ",".join("?" for _ in championships)
        entries = db.execute(f"SELECT * FROM files WHERE is_published = 1 AND championship_id IN ({placeholders}) ORDER BY id DESC", tuple(item["id"] for item in championships)).fetchall()
        for entry in entries:
            files_by_championship.setdefault(entry["championship_id"], []).append(entry)
    games = {item["id"]: _championship_game(db, files_by_championship.get(item["id"], []), item["name"]) for item in championships}
    version_file = Path(current_app.config["UPDATES_DIR"]) / "version.txt"
    kailleraclient_version = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else None
    kailleraclient_available = kailleraclient_version is not None and (Path(current_app.config["UPDATES_DIR"]) / "kailleraclient-x64.dll").exists()
    return render_template(
        "public/index.html",
        championships=championships,
        files_by_championship=files_by_championship,
        championship_games=games,
        kailleraclient_version=kailleraclient_version,
        kailleraclient_available=kailleraclient_available,
        mostrar_atualizacao_manual=manual_update_cards_visible(),
    )


@bp.get("/download/<int:file_id>")
def download(file_id):
    """Autoriza o arquivo; em produção o Nginx faz a transmissão via X-Accel."""
    entry = get_db().execute(
        """SELECT files.* FROM files JOIN championships ON championships.id = files.championship_id
           WHERE files.id = ? AND files.is_published = 1 AND championships.is_published = 1""",
        (file_id,),
    ).fetchone()
    if not entry:
        abort(404)
    if entry["local_removed_at"]:
        if not entry["archive_url"]:
            abort(404)
        return redirect(entry["archive_url"])
    if current_app.config["SERVE_DOWNLOADS_LOCALLY"]:
        return send_from_directory(current_app.config["DOWNLOADS_DIR"], entry["stored_name"], as_attachment=True, download_name=entry["original_filename"])
    response = Response()
    response.headers["X-Accel-Redirect"] = f"/_protected_downloads/{entry['stored_name']}"
    response.headers["Content-Disposition"] = attachment_header(entry["original_filename"])
    return response
