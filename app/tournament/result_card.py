"""O popup de um resultado do histórico (Comparar - "Seus últimos resultados"):
o jogo base e o campeonato ou a ISO (as imagens e a descrição), os dois lados -
foto com o rank, nick, time usado e gols - e, na partida que vale para o
Ranking (rankeada), o XP que cada um ganhou ou perdeu nela.

Serve para as linhas de stats.all_history (kind 't' campeonato, 'a' amistoso,
'l' partida livre), do ponto de vista de quem é a linha (player_id à esquerda).
Os times e o arquivo da ISO vêm da partida do Ao vivo ligada ao resultado (o
placar lido do jogo), como no card do Ao vivo (live._profile / _team_view); no
campeonato, os times são os da inscrição, mesmo sem a partida do Ao vivo.
"""
import json

from flask import url_for

from . import free, xp
from .games import cover_url
from .teams import generated_badge


def _live_match(db, row):
    """A partida do Ao vivo de uma linha do histórico, ou None (amistoso com o
    placar informado pelos jogadores, jogo de campeonato reportado à mão)."""
    if row["kind"] == free.KIND:
        live_id = row["match_id"]
    elif row["kind"] == "a":
        found = db.execute("SELECT live_match_id FROM friendly_games WHERE friendly_id = ? AND number = ?",
                           (row["match_id"], row["game_number"])).fetchone() if row["game_number"] else None
        live_id = found["live_match_id"] if found else None
    else:  # mais de uma (caiu e jogaram de novo): a encerrada mais recente
        found = db.execute("SELECT id FROM live_matches WHERE tournament_match_id = ? AND is_test = 0 AND verified = 1 "
                           "ORDER BY status = 'finished' DESC, id DESC LIMIT 1", (row["match_id"],)).fetchone()
        live_id = found["id"] if found else None
    return db.execute("SELECT * FROM live_matches WHERE id = ?", (live_id,)).fetchone() if live_id else None


def _profile(db, row, match):
    """(jogo base, campeonato, ISO do cadastro) - as linhas de live._profile: o
    da partida do Ao vivo ou, sem ela, o do campeonato / da ISO do convite."""
    from .. import live
    if match is not None:
        return live._profile(db, match)
    tournament_id = catalog_id = None
    if row["kind"] == "t":
        tournament_id, game_id = row["tournament_id"], row["game_title_id"]
    else:
        friendly = db.execute("SELECT iso_tournament_id, iso_catalog_id, game_title_id FROM friendly_matches "
                              "WHERE id = ?", (row["match_id"],)).fetchone()
        if friendly is None:
            return None, None, None
        tournament_id, catalog_id, game_id = (friendly["iso_tournament_id"], friendly["iso_catalog_id"],
                                              friendly["game_title_id"])
    game = db.execute("SELECT name, code FROM game_titles WHERE id = ?", (game_id,)).fetchone() if game_id else None
    tournament = db.execute("SELECT name, slug, image FROM tournaments WHERE id = ?",
                            (tournament_id,)).fetchone() if tournament_id else None
    catalog = None
    if catalog_id and not (tournament and tournament["image"]):
        catalog = db.execute("SELECT id, name, image, approved_at, is_active FROM catalog_isos WHERE id = ?",
                             (catalog_id,)).fetchone()
    return game, tournament, catalog


def _phase(db, row):
    """Fase do jogo de campeonato: "Grupo A · Rodada 2 · Volta" (como o card do Ao vivo)."""
    found = db.execute("SELECT m.round, m.leg, s.name, s.kind, tie.legs FROM tournament_matches m "
                       "JOIN tournament_stages s ON s.id = m.stage_id LEFT JOIN tournament_ties tie ON tie.id = m.tie_id "
                       "WHERE m.id = ?", (row["match_id"],)).fetchone()
    if found is None:
        return ""
    parts = [found["name"]]
    if found["kind"] != "knockout" and found["round"]:
        parts.append("Rodada %d" % found["round"])
    if found["legs"] == 2:
        parts.append("Ida" if found["leg"] == 1 else "Volta")
    return " · ".join(part for part in parts if part)


def _teams(db, row, match, names):
    """{player_id: {"name", "image", "known"}} dos times usados (None = sem time)."""
    from .. import live
    from ..tournaments import team_image
    me, other = row["player_id"], row["opponent_id"]
    teams = {me: None, other: None}
    if row["kind"] == "t":
        for player_id, entry_id in ((me, row["entry_id"]), (other, row["opponent_entry_id"])):
            entry = db.execute("SELECT e.team_name, t.name, t.logo, t.flag FROM tournament_entries e "
                               "LEFT JOIN teams t ON t.id = e.team_id WHERE e.id = ?", (entry_id,)).fetchone()
            if entry and entry["team_name"]:
                image = team_image(entry) if entry["name"] else generated_badge(entry["team_name"])
                teams[player_id] = {"name": entry["team_name"], "image": image, "known": True}
        return teams
    if match is None:
        return teams
    states = json.loads(match["state_json"] or "{}").get("teams") or []
    for side, column in enumerate(("local_players_json", "visita_players_json")):
        nicks = json.loads(match[column] or "[]")
        view = live._team_view(db, match, side, states[side] if side < len(states) else {}, nicks)
        playing = {nick.casefold() for nick in nicks if isinstance(nick, str)}
        for player_id in (me, other):
            if names.get(player_id, "").casefold() in playing:
                teams[player_id] = {"name": view["name"], "image": view["image"], "known": view["known"]}
    return teams


def build(db, row):
    """O que o popup mostra de uma linha do histórico (stats.all_history)."""
    from .. import live
    from ..images import url as image_url
    match = _live_match(db, row)
    game, tournament, catalog = _profile(db, row, match)
    me, other = row["player_id"], row["opponent_id"]
    names = {found["id"]: found["username"] for found in db.execute(
        "SELECT id, username FROM players WHERE id IN (?, ?)", (me, other))}
    teams = _teams(db, row, match, names)
    ranked = row.get("scope") == "ranked"
    deltas = xp.match_deltas(row["match_key"]) if ranked else {}
    tournament_url = url_for("tournaments.show", slug=tournament["slug"], aba="tabela") if tournament else None
    iso_url = (url_for("tournaments.iso_page", catalog_id=catalog["id"])
               if catalog and catalog["approved_at"] and catalog["is_active"] else None)
    if row["kind"] == "t":
        subtitle, subtitle_url = _phase(db, row), None
    elif tournament:  # amistoso / partida livre com a ISO de um campeonato
        subtitle, subtitle_url = tournament["name"], tournament_url
    elif catalog:
        subtitle, subtitle_url = catalog["name"], iso_url
    else:
        subtitle, subtitle_url = "", None
    pens = (row["pens_for"], row["pens_against"])
    # Os botões: "Ver amistoso" (a página dele) e "Ver partida" - o card no Ao vivo; sem a
    # partida do Ao vivo, o jogo de campeonato abre a página do jogo e o amistoso fica só
    # com o "Ver amistoso" (placar informado pelos jogadores).
    from ..tournaments import history_url
    friendly_url = url_for("tournaments.friendly_show", friendly_id=row["match_id"]) if row["kind"] == "a" else None
    if match is not None:
        match_url = url_for("live.index", partida=match["id"])
    else:
        match_url = None if row["kind"] == "a" else history_url(row)
    return {
        "friendly_url": friendly_url, "match_url": match_url,
        "game": {"name": game["name"], "cover": cover_url(game["code"])} if game else None,
        "tournament": {"name": tournament["name"], "image": image_url("torneio", tournament["image"]),
                       "url": tournament_url} if tournament else None,
        "iso": {"name": catalog["name"], "image": image_url("torneio", catalog["image"]), "url": iso_url}
        if catalog else None,
        "target": tournament_url or iso_url, "subtitle": subtitle, "subtitle_url": subtitle_url,
        "file": live._file_name(match) if match is not None else "",
        "sides": [{"id": player_id, "name": names.get(player_id, ""), "team": teams[player_id], "goals": goals,
                   "xp": deltas.get(player_id) if ranked else None}
                  for player_id, goals in ((me, row["goals_for"]), (other, row["goals_against"]))],
        "pens": pens if None not in pens else None,
        "ranked": ranked, "tripled": ranked and xp.match_tripled(row["match_key"]),
    }
