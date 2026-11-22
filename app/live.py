"""Partidas ao vivo (/ao-vivo): o placar que o RetroArch TIERES lê da RAM do
Winning Eleven e o kailleraclient.dll manda para cá durante a partida.

O fork (retroarch-k3, kaillera/kaillera_sync.c) acompanha a partida - times,
quem controla cada lado, placar, tempo e autores dos gols - e o DLL de cada PC
envia o estado (POST /api/live/match) a cada gol, troca de tempo, fim de jogo
e a cada ~15 s. Os PCs da mesma partida veem a mesma RAM (o jogo roda em
sincronia), então a partida é uma só: a chave junta a ISO, os jogadores e o
frame do pontapé inicial, que é igual em todos eles.

Cada PC tem o seu informe (live_match_reports); a partida mostra o mais
adiantado e, no fim, diz se os PCs concordaram no placar.

Contexto: com cada lado controlado por uma conta do site, a partida vai para o
próximo jogo ainda sem placar entre os dois num campeonato em andamento (ida,
depois volta); sem jogo de campeonato, para um amistoso aceito entre eles; sem
nenhum dos dois, é uma partida livre.

Nomes dos times: o jogo só tem o número do time (slot) na tabela da ISO, e os
patches nem sempre renomeiam a tabela (no CAN 2025 o slot 16 ainda se chama
FINLAND e é Camarões). Ordem: o time da inscrição de quem controla o lado
(jogo de campeonato - e a ISO aprende o slot), o cadastro por ISO
(iso_team_slots: aprendido ou do admin), o nome da RAM e, por fim, "Time N".

Reconectar (o botão do Kaillera que retoma uma partida que caiu a partir do
replay do Stream ao vivo): quando o host escolhe o replay, o DLL de cada PC
avisa (POST /api/live/reconnect) e a partida interrompida daquele replay
volta, em primeiro na lista, como "Reconectando"; quando o host libera os
controles, o fork volta a mandar o placar (com resume = a sessão do replay) e
ela segue "Ao vivo" no mesmo card, valendo para o mesmo jogo.
"""
import hashlib
from pathlib import Path
import json
import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for
from markupsafe import Markup

from .db import get_db, now
from .memcards import _api_key_ok, _api_player, _error, _reply
from .security import login_required, validate_csrf
from .tournament.teams import generated_badge

api_bp = Blueprint("live_api", __name__, url_prefix="/api/live")
bp = Blueprint("live", __name__)

# Sem informe há mais que isso: o card diz "sem sinal"; bem mais: a partida fecha.
STALE = timedelta(minutes=2)
GONE = timedelta(minutes=20)
# Partidas encerradas que ainda aparecem nos cards.
RECENT = timedelta(hours=6)
# Um PC que entrou no meio da partida (sem o pontapé inicial) junta-se à
# partida ao vivo dos mesmos jogadores/ISO atualizada há menos que isso.
JOIN_WINDOW = timedelta(minutes=10)
MAX_PLAYERS = 8
# Disputa de pênaltis no card: as cobranças de cada lado antes das alternadas
# (as bolas brancas que esperam a vez) e o máximo guardado de um lado.
PK_ROUND = 5
PK_MAX = 30
# Motor do jogo (executável que o fork lê na RAM) -> jogo base (game_titles.code):
# o logo do jogo base mesmo com uma ISO que o site não conhece.
ENGINE_GAMES = {"SLPM_870.56": "we2002", "SLPM_862.91": "we4", "SLPM_861.62": "we3fv",
                "SLPM_868.35": "we2001", "SLPM_866.00": "we2000u23"}
NAME_RE = re.compile(r"^[A-Za-z0-9 .'&/-]{2,24}$")


def _ago(delta):
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def _is_admin():
    return "user_id" in session


# --- Estado que chega do DLL --------------------------------------------------

def _int(value, low, high):
    value = int(value)
    if not low <= value <= high:
        raise ValueError(value)
    return value


def _text(value, cap):
    return str(value or "").strip()[:cap]


# WE 2002 (fork): estatísticas da tela RESULT do fim da partida, na ordem do card,
# e se o maior número é o melhor (o card destaca o melhor de cada linha).
STATS = (("shots", "Chutes ao gol", True), ("fouls", "Faltas", False), ("yellow", "Cartões amarelos", False),
         ("red", "Cartões vermelhos", False), ("offsides", "Impedimentos", False), ("corners", "Escanteios", True))
# Cartão: amarelo, 2º amarelo (expulso) e vermelho direto. Contusão: cruz
# amarela (segue em campo) e vermelha (sai e não volta).
CARD_KINDS = {"y": "Cartão amarelo", "y2": "2º amarelo (expulso)", "r": "Cartão vermelho"}
INJURY_KINDS = {"y": "Contusão leve (cruz amarela): seguiu em campo", "r": "Contusão grave (cruz vermelha): saiu de campo"}


def _team_extras(team):
    """Estatísticas, cartões e contusões de um time (só o WE 2002 manda): só
    as chaves que vieram, para o estado dos outros motores ficar como era.
    Item de tipo desconhecido (fork mais novo) fica de fora."""
    extras = {}
    stats = team.get("stats")
    if isinstance(stats, dict):
        extras["stats"] = {key: _int(stats.get(key) or 0, 0, 255) for key, _, _ in STATS}
    for field, kinds in (("cards", CARD_KINDS), ("injuries", INJURY_KINDS)):
        items = team.get(field)
        if isinstance(items, list):
            extras[field] = [[_text(item[0], 24), _int(item[1], 0, 200), item[2]] for item in items[:32]
                             if isinstance(item, list) and len(item) >= 3 and item[2] in kinds]
    return extras


def parse_state(raw):
    """JSON do fork -> dict validado (ValueError se não serve). Do v2 em
    diante o nome de cada time vem da tela de seleção de times (o que os
    jogadores viram, ex. "PAL99"), com o código de 3 letras; no v1 vinha de
    uma tabela que muitos patches não renomeiam - não vale."""
    data = json.loads(raw)
    version = _int(data.get("v") or 1, 1, 99)
    teams = []
    for team in data["teams"][:2]:
        scorers = []
        for item in team.get("scorers", [])[:32]:
            scorer = [_text(item[0], 24), _int(item[1], 0, 200), _text(item[2] if len(item) > 2 else "", 24)]
            # 4º item (fork): "og" = gol contra, com o autor no lugar da assistência; "pen" = de pênalti.
            if len(item) > 3 and item[3] in ("og", "pen"):
                scorer.append(item[3])
            scorers.append(scorer)
        entry = {"id": _int(team["id"], 0, 255), "name": _text(team.get("name"), 24) if version >= 2 else "",
                 "code": _text(team.get("code"), 3), "goals": _int(team["goals"], 0, 99), "scorers": scorers}
        entry.update(_team_extras(team))
        teams.append(entry)
    if len(teams) != 2:
        raise ValueError("teams")
    pens = data.get("pens")
    pens = [_int(pens[0], 0, 99), _int(pens[1], 0, 99)] if isinstance(pens, list) and len(pens) == 2 else None
    # Cada cobrança da disputa, na ordem, por lado (fork novo): 1 convertida, 0 perdida.
    pk = data.get("pk")
    pk = ([[1 if kick else 0 for kick in side[:PK_MAX]] for side in pk]
          if pens and isinstance(pk, list) and len(pk) == 2 and all(isinstance(side, list) for side in pk) else None)
    return {
        "v": version,
        "engine": _text(data.get("engine"), 16),
        "content": _text(data["content"], 32),
        "kickoff": _int(data.get("kickoff") or 0, 0, 2 ** 31),
        "frame": _int(data.get("frame") or 0, 0, 2 ** 31),
        "period": _int(data.get("period") or 0, 0, 9),
        "clock": _int(data.get("clock") or 0, 0, 65535),
        "half": _int(data.get("half") or 18000, 1, 65535),
        "final": bool(data.get("final")),
        "teams": teams,
        "pads": [_int(p, 0, 255) for p in data.get("pads", [])[:MAX_PLAYERS]],
        # Disputa de pênaltis (LOCAL, VISITA): o período vem 4; prorrogação = 2 e 3.
        "pens": pens,
        "pk": pk,
        # O juiz encerrou o tempo (fim do 1º/2º tempo, da prorrogação...): None
        # para o fork que não manda - aí o "Intervalo" vem do relógio em 0.
        "over": bool(data["over"]) if "over" in data else None,
        # Saiu pelo menu de pausa do jogo (EXIT MATCH): o controle (1-4) de quem
        # abriu a pausa - o último estado da partida; 0 = não.
        "quit": _int(data.get("quit") or 0, 0, MAX_PLAYERS),
        # Fork novo: o começo do card (tela de configuração da partida, "Repeat"
        # ou pontapé - a chave dele), a espera pelo pontapé (1 início, 2 reinício),
        # o quadro do apito inicial e "cancel" (esperou demais: descartado).
        "setup": _int(data.get("setup") or 0, 0, 2 ** 31),
        "waiting": _int(data.get("waiting") or 0, 0, 2),
        "whistle": _int(data.get("whistle") or 0, 0, 2 ** 31),
        "cancel": bool(data.get("cancel")),
    }


def match_key(content, players, kickoff):
    """A chave do card: o jogo, os jogadores e o começo dele - o quadro do
    pontapé (fork antigo) ou o "setup" do fork novo (o mesmo do pontapé
    quando o card começou nele)."""
    raw = "%s|%s|%d" % (content, ",".join(p.lower() for p in players), kickoff)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# Saíram da partida até 2 minutos (tempo real, 60 quadros/s) depois do apito
# inicial, com 0x0: não é partida interrompida (Walter, 2026-11-22) - o fork
# devolve o card ao "Aguardando o reinício da partida"; se saíram da sala (a
# DLL avisa "left") ou o card ficou sem sinal, ele é descartado.
EARLY_FRAMES = 2 * 60 * 60
# Card esperando o pontapé sem sinal (o fork manda a cada 15 s): descartado.
WAITING_GONE = timedelta(minutes=5)
DISCARDED_PREFIX = "descartada"


def is_early(state):
    """O estado é de um card do fork novo que ainda não vale como partida:
    esperando o pontapé ou com menos de EARLY_FRAMES desde o apito e 0x0."""
    if not state.get("setup"):
        return False  # fork antigo: a regra de antes
    if state.get("waiting"):
        return True
    goals = [team.get("goals") or 0 for team in state.get("teams", [])[:2]]
    if any(goals):
        return False
    whistle = state.get("whistle") or 0
    return not whistle or (state.get("frame") or 0) - whistle < EARLY_FRAMES


def discard(db, live_id, reason):
    """O card que não valeu (is_early): sai do site e não vale nada - sem
    campeonato nem amistoso, sem placar, sem quem saiu. Fica para o
    administrador (oculto, "Descartado"). A transmissão dele, se houver, para
    com o vídeo "interrompida" (broadcaster: status != live)."""
    stamp = now()
    db.execute("UPDATE live_matches SET status = 'abandoned', waiting = 0, hidden = 1, deleted_at = COALESCE(deleted_at, ?), "
               "game_report = ?, tournament_match_id = NULL, friendly_id = NULL, reconnecting_at = NULL, "
               "finished_at = COALESCE(finished_at, ?), updated_at = ? WHERE id = ?",
               (stamp, "%s: %s" % (DISCARDED_PREFIX, reason), stamp, stamp, live_id))


def _discarded(match):
    return (match["game_report"] or "").startswith(DISCARDED_PREFIX)


@api_bp.post("/match")
def api_match():
    """Estado da partida mandado pelo DLL: form com state (JSON do fork),
    players (nicks em ordem 1P, 2P...), room (o jogo da sala) e test=1.
    Só com o token da conta WE Camp de um dos jogadores (o DLL não manda sem
    login); a chave do DLL só vale para as simulações (test=1, só o admin vê,
    reporter = o nick)."""
    player = _api_player()
    is_test = request.form.get("test") == "1"
    if player is None and not (is_test and _api_key_ok()):
        return _error(401, "invalid_token", "Entre na sua conta WE Camp (botao Conta WE Camp) para o placar ao vivo.")
    try:
        state = parse_state(request.form.get("state", ""))
    except (KeyError, TypeError, ValueError, IndexError):
        return _error(400, "bad_state", "Estado da partida invalido.")
    players = [p.strip()[:32] for p in request.form.get("players", "").split(",") if p.strip()][:MAX_PLAYERS]
    reporter = player["username"] if player else _text(request.form.get("reporter"), 32)
    if not players or not reporter or not state["content"]:
        return _error(400, "bad_state", "Faltam jogadores ou o jogo.")
    if player and reporter.casefold() not in {nick.casefold() for nick in players}:
        return _error(400, "not_a_player", "O seu nick no Kaillera tem que ser o usuario da sua conta WE Camp.")
    db = get_db()
    # Partida retomada pelo Reconectar: resume = o session_id do replay escolhido pelo host.
    live_id = ingest(db, state, players, reporter, player["id"] if player else None,
                     _text(request.form.get("room"), 120), is_test, _text(request.form.get("resume"), 64))
    if request.form.get("left") == "1" and live_id:
        # A DLL avisa que o jogador saiu (Leave, fechou o Kaillera) com o último
        # estado que mandou: a partida que ainda corria fica interrompida na
        # hora (sem esperar o tempo sem sinal) - um fim que chegue depois, do PC
        # de quem ficou, ainda a encerra. A que ainda não valia (esperando o
        # pontapé, ou menos de 2 minutos com 0x0 - is_early) é descartada.
        row = db.execute("SELECT status, state_json FROM live_matches WHERE id = ?", (live_id,)).fetchone()
        if row is not None and row["status"] == "live" and is_early(_match_state(row)):
            discard(db, live_id, "saíram antes de a partida valer")
        else:
            db.execute("UPDATE live_matches SET status = 'abandoned', waiting = 0, finished_at = COALESCE(finished_at, ?), "
                       "updated_at = ? WHERE id = ? AND status = 'live'", (now(), now(), live_id))
    db.commit()
    return _reply(ok=1, id=live_id)


def resume_key(content, players, session_id):
    """Chave da partida retomada pelo Reconectar: a ISO, os jogadores (em
    qualquer ordem - a sala nova pode trocar 1P e 2P) e a sessão do replay."""
    raw = "resume|%s|%s|%s" % (content, ",".join(sorted(p.casefold() for p in players)), session_id)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def ingest(db, state, players, reporter, player_id, room, is_test=False, resume=""):
    if resume and not is_test:
        row = _resumed_match(db, state, players, resume)
        if row is not None:
            return _store_report(db, row["id"], state, reporter, player_id, row["frame_base"])
    # Retomada sem partida para continuar: um card novo, com a chave da sessão (o
    # pontapé vem 0 - sem ela, todas as retomadas dos dois nesta ISO teriam a mesma).
    # Fork novo: a chave é o começo do card ("setup" - a tela de configuração, o
    # "Repeat" ou o pontapé), o mesmo do "Aguardando" ao fim.
    start = state["setup"] or state["kickoff"]
    key = resume_key(state["content"], players, resume) if resume else match_key(state["content"], players, start)
    row = db.execute("SELECT * FROM live_matches WHERE match_key = ?", (key,)).fetchone()
    if row is None and not start:
        row = db.execute(
            "SELECT * FROM live_matches WHERE content_id = ? AND players_json = ? AND status = 'live' "
            "AND updated_at >= ? ORDER BY updated_at DESC LIMIT 1",
            (state["content"], json.dumps(players), _ago(JOIN_WINDOW))).fetchone()
    if row is not None and _discarded(row):
        return row["id"]  # descartado (o outro PC ainda mandando o card): fica como está
    if state["cancel"]:
        # O card esperou demais pelo pontapé (o fork desistiu dele).
        if row is not None:
            discard(db, row["id"], "esperou o pontapé inicial por muito tempo")
            return row["id"]
        return 0
    stamp = now()
    if row is None and start:
        # Partida nova dos mesmos jogadores e ISO: a anterior que ficou aberta foi
        # abandonada (WE4: saíram pelo menu de pausa; o fork não manda o fim dela;
        # ou um saiu/caiu e voltaram a jogar - com 1P e 2P em qualquer ordem). A que
        # ainda não valia (esperando o pontapé, ou menos de 2 minutos com 0x0) é descartada.
        same = sorted(nick.casefold() for nick in players)
        for old in db.execute("SELECT id, players_json, state_json FROM live_matches WHERE status = 'live' "
                              "AND content_id = ? AND match_key != ?", (state["content"], key)).fetchall():
            if sorted(nick.casefold() for nick in json.loads(old["players_json"])) == same:
                if is_early(_match_state(old)):
                    discard(db, old["id"], "começaram outra partida antes de esta valer")
                    continue
                # (Também a que estava "Reconectando": em vez de retomar, começaram outra.)
                db.execute("UPDATE live_matches SET status = 'abandoned', reconnecting_at = NULL, waiting = 0, "
                           "finished_at = COALESCE(finished_at, ?) WHERE id = ?", (stamp, old["id"]))
    if row is None:
        live_id = db.execute(
            "INSERT INTO live_matches (match_key, content_id, room, players_json, kickoff_frame, setup_frame, is_test, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, state["content"], room, json.dumps(players), state["kickoff"], state["setup"], int(is_test),
             stamp, stamp)).lastrowid
    else:
        live_id = row["id"]
    return _store_report(db, live_id, state, reporter, player_id)


def _store_report(db, live_id, state, reporter, player_id, frame_base=0):
    """Grava o informe deste PC e recalcula a partida. frame_base: a partida
    retomada pelo Reconectar conta os quadros de novo do 1 - somado a eles o
    maior quadro de antes, o informe novo é sempre o mais adiantado."""
    db.execute(
        "INSERT OR REPLACE INTO live_match_reports (live_match_id, reporter, player_id, frame, final, state_json, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (live_id, reporter, player_id, (frame_base or 0) + state["frame"], int(state["final"]), json.dumps(state), now()))
    refresh(db, live_id)
    return live_id


# --- Reconectar ------------------------------------------------------------------

RECONNECT_NOTE = ("Reconectaram a partida no RetroArch TIERES (Ao vivo nº %d): o placar de quando ela parou "
                  "foi retirado - vale o fim da partida retomada.")
REPLAYED_NOTE = ("Jogaram de novo no RetroArch TIERES (Ao vivo nº %d): o placar de quando a partida anterior "
                 "parou foi retirado - vale a partida nova.")
REPLAYED_OUTCOME = "jogaram de novo: vale a partida seguinte"


def _nickset(nicks):
    return {nick.strip().casefold() for nick in nicks if nick and nick.strip()}


def _reconnect_target(db, session_id, players=None, content=None):
    """A partida que o Reconectar retoma: a que ele já reabriu com esta sessão
    ou a última partida (não de teste, com os dois logados) jogada no replay
    escolhido (live_sessions.session_id) - entre os jogadores dele e, com
    players/content, dos mesmos jogadores e ISO. None sem nenhuma."""
    row = db.execute("SELECT * FROM live_matches WHERE reconnect_session = ? AND is_test = 0 "
                     "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
    if row is not None:
        return row
    replay = db.execute("SELECT * FROM live_sessions WHERE session_id = ?", (session_id,)).fetchone()
    if replay is None:
        return None
    names = _nickset((replay["player_names"] or "").split(","))
    until = replay["ended_at"] or now()
    for row in db.execute(
            "SELECT * FROM live_matches WHERE is_test = 0 AND verified = 1 AND started_at <= ? "
            "AND COALESCE(finished_at, updated_at) >= ? ORDER BY COALESCE(finished_at, updated_at) DESC, id DESC",
            (until, replay["started_at"])):
        nicks = _nickset(json.loads(row["players_json"]))
        if not nicks or not nicks <= names:
            continue
        if players is not None and nicks != _nickset(players):
            continue
        if content is not None and row["content_id"] != content:
            continue
        return row  # a última dos jogadores no replay (terminada ou não)
    return None


def _can_reopen(db, match):
    """A partida ainda pode voltar a valer? Não terminada e, no jogo de
    campeonato, sem placar liberado (só o de quando parou, sem confirmação) e
    sem outra partida dos dois para o mesmo jogo depois dela; no amistoso,
    antes de as partidas da sessão virarem o placar dele."""
    if match["status"] == "finished" or match["is_test"] or match["deleted_at"]:
        return False  # (o card que os jogadores excluíram não volta: o Reconectar abre outro)
    if match["quit_nick"]:
        return False  # abandonada pelo menu do jogo: vale o placar de quando ele saiu
    if match["tournament_match_id"]:
        game = db.execute("SELECT status, interrupted, reported_by_entry_id FROM tournament_matches WHERE id = ?",
                          (match["tournament_match_id"],)).fetchone()
        if game is None:
            return False
        pending = game["status"] == "reported" and game["interrupted"] and not game["reported_by_entry_id"]
        if game["status"] != "scheduled" and not pending:
            return False
        return db.execute("SELECT 1 FROM live_matches WHERE tournament_match_id = ? AND is_test = 0 AND id > ?",
                          (match["tournament_match_id"], match["id"])).fetchone() is None
    if match["friendly_id"]:
        return match["status"] == "live" or not match["game_report"]
    return True


def reopen(db, match, session_id):
    """O host escolheu o replay no Reconectar: a partida interrompida volta,
    em primeiro na lista, como "Reconectando" (até o placar da partida
    retomada chegar - refresh). No jogo de campeonato, o placar de quando ela
    parou (sem confirmação) é retirado: vale o fim da partida retomada.
    O fim da partida (finished_at) e o último informe (updated_at) ficam: se o
    Reconectar não for adiante, ela volta a ser a interrompida que era."""
    from .tournament import reports
    if match["status"] == "live" and match["reconnect_session"] == session_id and not match["reconnecting_at"]:
        return False  # já retomada: o aviso do outro PC chegou depois do placar
    if not _can_reopen(db, match):
        return False
    clear = False
    if match["tournament_match_id"]:
        if (match["game_report"] or "").startswith("reportado"):
            reports.withdraw_interrupted(db, match["tournament_match_id"], RECONNECT_NOTE % match["id"])
        clear = True  # sem placar no jogo: a partida retomada reporta no fim (ou ao parar de novo)
    db.execute("UPDATE live_matches SET status = 'live', reconnecting_at = ?, reconnect_session = ?, "
               "game_report = CASE WHEN ? THEN NULL ELSE game_report END WHERE id = ?",
               (now(), session_id, int(clear), match["id"]))
    return True


def cancel_reconnect(db, match, session_id):
    """O jogo do Reconectar acabou antes de o host liberar os controles: a
    partida volta a ser a interrompida (e o placar de quando parou volta a
    valer da mesma forma: _report_abandoned)."""
    if match["reconnect_session"] != session_id or not match["reconnecting_at"] or match["status"] != "live":
        return False
    db.execute("UPDATE live_matches SET status = 'abandoned', reconnecting_at = NULL, "
               "finished_at = COALESCE(finished_at, updated_at) WHERE id = ?", (match["id"],))
    return True


def _resumed_match(db, state, players, session_id):
    """A partida que o placar retomado (resume) continua: a que já recebeu
    informes desta sessão (resume_key) ou a do replay (_reconnect_target) -
    reaberta aqui mesmo se o aviso do Reconectar não chegou. Ela volta a ser
    "Ao vivo" (refresh), sem o fim de antes, e os quadros novos somam o maior
    quadro de antes (frame_base). None: não há o que continuar (card novo)."""
    key = resume_key(state["content"], players, session_id)
    row = db.execute("SELECT * FROM live_matches WHERE resume_key = ?", (key,)).fetchone()
    if row is not None:
        return row
    target = _reconnect_target(db, session_id, players, state["content"])
    if target is None:
        return None
    if not target["reconnecting_at"] and not reopen(db, target, session_id):
        return None
    base = db.execute("SELECT COALESCE(MAX(frame), 0) AS frame FROM live_match_reports WHERE live_match_id = ?",
                      (target["id"],)).fetchone()["frame"]
    # Os jogadores na ordem da sala nova (1P, 2P...): os controles do placar retomado são os dela.
    db.execute("UPDATE live_matches SET status = 'live', resume_key = ?, frame_base = ?, finished_at = NULL, "
               "players_json = ? WHERE id = ?", (key, base, json.dumps(players), target["id"]))
    return db.execute("SELECT * FROM live_matches WHERE id = ?", (target["id"],)).fetchone()


@api_bp.post("/reconnect")
def api_reconnect():
    """Reconectar do Kaillera, mandado pelo DLL de cada PC: phase=start logo
    depois que o host escolhe o replay (session = o session_id dele no Stream
    ao vivo) - a partida interrompida daquele replay volta como "Reconectando";
    phase=cancel se o jogo acabar antes de o host liberar os controles. Só com
    o token da conta WE Camp de um dos jogadores da partida."""
    player = _api_player()
    if player is None:
        return _error(401, "invalid_token", "Entre na sua conta WE Camp (botao Conta WE Camp) para o placar ao vivo.")
    session_id = _text(request.form.get("session"), 64)
    phase = request.form.get("phase") or "start"
    if not session_id or phase not in ("start", "cancel"):
        return _error(400, "bad_request", "Faltam dados do Reconectar.")
    db = get_db()
    target = _reconnect_target(db, session_id)
    if target is None or player["username"].casefold() not in _nickset(json.loads(target["players_json"])):
        return _reply(ok=1, id=0)  # nada a reabrir
    done = reopen(db, target, session_id) if phase == "start" else cancel_reconnect(db, target, session_id)
    db.commit()
    return _reply(ok=1, id=target["id"] if done else 0)


def _sides(players, pads):
    """Nicks que controlam o LOCAL (0) e o VISITA (1): controle i = jogador i+1."""
    sides = ([], [])
    for i, side in enumerate(pads):
        if side in (0, 1) and i < len(players):
            sides[side].append(players[i])
    return sides


def _all_logged_in(db, players, reports):
    """Cada jogador da partida mandou o placar do próprio PC, logado na conta
    dele (o nick dele é o usuário dessa conta)?"""
    senders = {r["player_id"] for r in reports if r["player_id"]}
    return bool(players) and all(_player_id(db, nick) in senders for nick in players)


def refresh(db, live_id):
    """Recalcula a partida a partir dos informes dos PCs."""
    match = db.execute("SELECT * FROM live_matches WHERE id = ?", (live_id,)).fetchone()
    reports = db.execute("SELECT * FROM live_match_reports WHERE live_match_id = ? ORDER BY frame DESC, updated_at DESC",
                         (live_id,)).fetchall()
    best = json.loads(reports[0]["state_json"])
    finals = [json.loads(r["state_json"]) for r in reports if r["final"]]
    scores = {(s["teams"][0]["goals"], s["teams"][1]["goals"]) for s in finals}
    players = json.loads(match["players_json"])
    local, visita = _sides(players, best["pads"])
    finished = bool(finals)
    # Saiu pelo menu do jogo: o controle de quem abriu a pausa é o do jogador
    # nessa ordem (controle i = jogador i, como em _sides). A partida para na hora.
    pad = next((s.get("quit") for s in (json.loads(r["state_json"]) for r in reports) if s.get("quit")), 0)
    quit_nick = match["quit_nick"] or (players[pad - 1] if 0 < pad <= len(players) else None)
    stopped = bool(quit_nick) or match["status"] == "abandoned"
    verified = 1 if match["is_test"] else int(_all_logged_in(db, players, reports))
    # Fork novo: antes do pontapé o card espera (1 início, 2 reinício); o pontapé e o
    # apito da partida que está valendo (o reinício traz outros).
    waiting = 0 if finished or stopped else best.get("waiting") or 0
    # Um informe novo: a partida está rolando - a que estava "Reconectando" segue "Ao vivo".
    db.execute(
        "UPDATE live_matches SET status = ?, period = ?, clock_left = ?, half_frames = ?, local_slot = ?, visita_slot = ?, "
        "local_goals = ?, visita_goals = ?, state_json = ?, local_players_json = ?, visita_players_json = ?, "
        "reporters = ?, agreed = ?, verified = ?, updated_at = ?, finished_at = COALESCE(finished_at, ?), "
        "reconnecting_at = NULL, quit_nick = ?, waiting = ?, "
        "kickoff_frame = CASE WHEN ? > 0 THEN ? ELSE kickoff_frame END, "
        "setup_frame = CASE WHEN ? > 0 THEN ? ELSE setup_frame END, whistle_frame = ? WHERE id = ?",
        ("finished" if finished else ("abandoned" if stopped else "live"), best["period"], best["clock"], best["half"], best["teams"][0]["id"],
         best["teams"][1]["id"], best["teams"][0]["goals"], best["teams"][1]["goals"], json.dumps(best),
         json.dumps(local), json.dumps(visita), len(reports), int(len(finals) >= 2 and len(scores) == 1), verified,
         now(), now() if finished or quit_nick else None, quit_nick, waiting,
         best.get("kickoff") or 0, best.get("kickoff") or 0, best.get("setup") or 0, best.get("setup") or 0,
         0 if waiting else best.get("whistle") or 0, live_id))
    for side, team in enumerate(best["teams"]):
        ram_name = team["name"] if NAME_RE.match(team["name"]) else ""
        db.execute("INSERT OR IGNORE INTO iso_team_slots (content_id, slot, updated_at) VALUES (?, ?, ?)",
                   (match["content_id"], team["id"], now()))
        db.execute("UPDATE iso_team_slots SET room = ?, ram_name = CASE WHEN ? != '' THEN ? ELSE ram_name END "
                   "WHERE content_id = ? AND slot = ?",
                   (match["room"], ram_name, ram_name, match["content_id"], team["id"]))
    match = db.execute("SELECT * FROM live_matches WHERE id = ?", (live_id,)).fetchone()
    if (verified and match["tournament_match_id"] is None and match["friendly_id"] is None
            and len(local) == 1 and len(visita) == 1):
        detect_context(db, match, local[0], visita[0])
        match = db.execute("SELECT * FROM live_matches WHERE id = ?", (live_id,)).fetchone()
    if match["tournament_match_id"] and not match["is_test"] and match["status"] == "live" and not is_early(best):
        _replace_interrupted(db, match)


def _replace_interrupted(db, match):
    """Jogaram de novo o jogo de campeonato com o placar de quando a partida
    anterior parou ainda sem liberar: ele sai, e quem reporta é esta partida -
    só quando ela vale (passou dos 2 minutos ou saiu gol: is_early), para um
    card que só esperou o pontapé e foi descartado não levar o placar junto."""
    from .tournament import reports
    if reports.withdraw_interrupted(db, match["tournament_match_id"], REPLAYED_NOTE % match["id"]):
        db.execute("UPDATE live_matches SET game_report = ? WHERE tournament_match_id = ? AND id != ? "
                   "AND status = 'abandoned' AND game_report LIKE 'reportado%'",
                   (REPLAYED_OUTCOME, match["tournament_match_id"], match["id"]))


# --- Campeonato, amistoso ou partida livre ------------------------------------

def _player_id(db, nick):
    row = db.execute("SELECT id FROM players WHERE username = ? COLLATE NOCASE", (nick,)).fetchone()
    return row["id"] if row else None


def detect_context(db, match, local_nick, visita_nick):
    """Liga a partida a um amistoso aceito entre os dois (prioridade: com
    amistoso marcado, as partidas vão para ele, mesmo com jogo de campeonato
    em aberto - todas as da sessão, somadas no fim, _report_friendlies) ou ao
    próximo jogo de campeonato entre eles (ainda sem placar e sem outra
    partida ao vivo ligada). A ISO tem que ser a do amistoso/campeonato. As
    simulações (is_test) só disputam os jogos entre si e não ensinam slots."""
    a, b = _player_id(db, local_nick), _player_id(db, visita_nick)
    if not a or not b or a == b:
        return
    content = match["content_id"]
    # Amistoso: a ISO escolhida no convite (a de um campeonato ou uma do
    # cadastro) tem que bater; sem ISO escolhida ou sem identificação, vale.
    row = db.execute(
        "SELECT f.id FROM friendly_matches f LEFT JOIN tournament_isos ti ON ti.tournament_id = f.iso_tournament_id "
        "LEFT JOIN catalog_isos ci ON ci.id = f.iso_catalog_id "
        "WHERE f.status = 'accepted' "
        "AND ((f.home_id = ? AND f.away_id = ?) OR (f.home_id = ? AND f.away_id = ?)) "
        "AND (COALESCE(ti.content_id, ci.content_id) IS NULL OR COALESCE(ti.content_id, ci.content_id) = ? COLLATE NOCASE "
        "OR EXISTS (SELECT 1 FROM iso_profiles p WHERE p.content_id = ? COLLATE NOCASE "
        "AND (p.tournament_id = f.iso_tournament_id OR p.catalog_iso_id = f.iso_catalog_id))) "
        # Mais de um amistoso aceito entre os dois (cada um com uma ISO - friendly.invite): o da
        # ISO exata, depois o de uma ISO ligada a ela (iso_profiles), por último o sem ISO.
        "ORDER BY CASE WHEN COALESCE(ti.content_id, ci.content_id) = ? COLLATE NOCASE THEN 0 "
        "WHEN COALESCE(ti.content_id, ci.content_id) IS NULL THEN 2 ELSE 1 END, f.created_at LIMIT 1",
        (a, b, b, a, content, content, content)).fetchone()
    if row:
        db.execute("UPDATE live_matches SET friendly_id = ? WHERE id = ?", (row["id"], match["id"]))
        return
    # Uma partida interrompida não prende o jogo enquanto o placar de quando
    # ela parou não for liberado (um dos dois confirma ou o organizador):
    # voltaram e jogaram de novo - vale a partida nova (_report_abandoned).
    # A abandonada pelo menu do jogo (quit_nick) prende: a revanche não a substitui
    # (só a exclusão do card pelos jogadores a solta).
    taken = ("SELECT tournament_match_id FROM live_matches WHERE tournament_match_id IS NOT NULL AND id != ? "
             "AND is_test = ? AND (status != 'abandoned' OR (quit_nick IS NOT NULL AND deleted_at IS NULL) "
             "OR (game_report LIKE 'reportado%' AND tournament_match_id "
             "NOT IN (SELECT id FROM tournament_matches WHERE status = 'reported' AND interrupted = 1 "
             "AND reported_by_entry_id IS NULL)))")
    # A ISO tem que ser a do campeonato (content_id dela ou uma ligada pelo
    # admin/aprendida em iso_profiles); campeonato sem ISO identificada vale.
    row = db.execute(
        "SELECT m.id FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
        "JOIN tournaments t ON t.id = s.tournament_id LEFT JOIN tournament_isos ti ON ti.tournament_id = t.id "
        "JOIN tournament_entries he ON he.id = m.home_entry_id JOIN tournament_entries ae ON ae.id = m.away_entry_id "
        "WHERE t.status = 'running' AND s.status = 'running' AND m.status != 'confirmed' "
        "AND ((he.player_id = ? AND ae.player_id = ?) OR (he.player_id = ? AND ae.player_id = ?)) "
        "AND (ti.content_id IS NULL OR ti.content_id = ? COLLATE NOCASE OR t.id IN "
        "(SELECT tournament_id FROM iso_profiles WHERE content_id = ? COLLATE NOCASE)) "
        f"AND m.id NOT IN ({taken}) ORDER BY s.seq, m.round, m.leg, m.id LIMIT 1",
        (a, b, b, a, content, content, match["id"], match["is_test"])).fetchone()
    if row:
        db.execute("UPDATE live_matches SET tournament_match_id = ? WHERE id = ?", (row["id"], match["id"]))
        if not match["is_test"]:
            learn_slots(db, match["id"])
            # (Jogaram de novo com o placar de quando a anterior parou ainda sem
            # liberar: refresh -> _replace_interrupted, quando esta partida vale.)


def _entry_team(db, match, nick):
    """Time inscrito no campeonato por quem controla um lado: (nome, linha de teams)."""
    row = db.execute(
        "SELECT e.team_name, e.team_id FROM tournament_matches m JOIN tournament_entries e "
        "ON e.id IN (m.home_entry_id, m.away_entry_id) JOIN players p ON p.id = e.player_id "
        "WHERE m.id = ? AND p.username = ? COLLATE NOCASE", (match["tournament_match_id"], nick)).fetchone()
    if not row or not row["team_name"]:
        return None, None
    team = db.execute("SELECT * FROM teams WHERE id = ?", (row["team_id"],)).fetchone() if row["team_id"] else None
    return row["team_name"], team


def learn_slots(db, live_id):
    """Jogo de campeonato: o time inscrito de quem controla cada lado ensina o
    nome do slot desta ISO, e o campeonato ensina o jogo base e o campeonato
    da ISO (sem mexer no que o admin cadastrou)."""
    match = db.execute("SELECT * FROM live_matches WHERE id = ?", (live_id,)).fetchone()
    for slot, nicks in ((match["local_slot"], json.loads(match["local_players_json"])),
                        (match["visita_slot"], json.loads(match["visita_players_json"]))):
        if slot is None or len(nicks) != 1:
            continue
        name, team = _entry_team(db, match, nicks[0])
        if name:
            db.execute("UPDATE iso_team_slots SET name = ?, team_id = ?, source = 'aprendido', updated_at = ? "
                       "WHERE content_id = ? AND slot = ? AND source != 'admin'",
                       (name, team["id"] if team else None, now(), match["content_id"], slot))
    tournament = db.execute(
        "SELECT t.id, t.game_title_id FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
        "JOIN tournaments t ON t.id = s.tournament_id WHERE m.id = ?", (match["tournament_match_id"],)).fetchone()
    if tournament:
        db.execute("INSERT OR IGNORE INTO iso_profiles (content_id, updated_at) VALUES (?, ?)",
                   (match["content_id"], now()))
        db.execute("UPDATE iso_profiles SET game_title_id = ?, tournament_id = ?, source = 'aprendido', updated_at = ? "
                   "WHERE content_id = ? AND source != 'admin'",
                   (tournament["game_title_id"], tournament["id"], now(), match["content_id"]))


# --- Cards ----------------------------------------------------------------------

UNKNOWN_TEAM = "Não identificado"
CARDS_LIMIT = 10


def reap(db):
    """Partidas sem informe há muito tempo: encerradas (se paradas no fim do
    2º tempo) ou abandonadas - a "Reconectando", contado do Reconectar (o
    host não liberou os controles: volta a ser a interrompida). As que ainda
    não valiam (is_early: esperando o pontapé - sem sinal há WAITING_GONE -,
    ou menos de 2 minutos com 0x0) são descartadas."""
    for row in db.execute("SELECT id, waiting, state_json, updated_at FROM live_matches WHERE status = 'live' "
                          "AND reconnecting_at IS NULL AND updated_at < ?", (_ago(WAITING_GONE),)).fetchall():
        if is_early(_match_state(row)) and (row["waiting"] or row["updated_at"] < _ago(GONE)):
            discard(db, row["id"], "ficou sem sinal antes de a partida valer")
    db.execute("UPDATE live_matches SET status = CASE WHEN period = 1 AND clock_left = 0 THEN 'finished' "
               "ELSE 'abandoned' END, finished_at = COALESCE(finished_at, updated_at), reconnecting_at = NULL "
               "WHERE status = 'live' AND updated_at < ? AND (reconnecting_at IS NULL OR reconnecting_at < ?)",
               (_ago(GONE), _ago(GONE)))
    db.commit()
    report_finished(db)


def _team_view(db, match, side, team_state, nicks):
    """Time de um lado: o da inscrição (jogo de campeonato), o cadastrado para
    o slot desta ISO ou o nome da tela de seleção de times (estado v2, ou o
    último visto deste slot); sem nenhum, "Não identificado"."""
    from .tournaments import team_image
    slot = team_state.get("id")
    name, team = (None, None)
    if match["tournament_match_id"] and len(nicks) == 1:
        name, team = _entry_team(db, match, nicks[0])
    if not name:
        row = db.execute("SELECT s.*, t.name AS team_name FROM iso_team_slots s LEFT JOIN teams t ON t.id = s.team_id "
                         "WHERE s.content_id = ? AND s.slot = ?", (match["content_id"], slot)).fetchone()
        if row and (row["name"] or row["team_id"]):
            name = row["name"] or row["team_name"]
            team = db.execute("SELECT * FROM teams WHERE id = ?", (row["team_id"],)).fetchone() if row["team_id"] else None
        elif NAME_RE.match(team_state.get("name") or ""):
            name = team_state["name"]
        elif row and row["ram_name"]:
            name = row["ram_name"]
    image = team_image(team) if team else generated_badge(name or "?")
    return {"name": name or UNKNOWN_TEAM, "known": bool(name), "image": image, "goals": team_state.get("goals", 0),
            "players": list(nicks), "scorers": team_state.get("scorers", []),
            # Os grupos do card, abaixo dos gols: expulsos (vermelho direto e 2º amarelo),
            # amarelos (o 2º também, na linha do jogador: "Casemiro 1',37'") e as contusões
            # de cruz vermelha e de cruz amarela.
            "reds": [c for c in team_state.get("cards") or [] if c[2] in ("r", "y2")],
            "yellows": _by_player(c for c in team_state.get("cards") or [] if c[2] in ("y", "y2")),
            "injuries_red": [c for c in team_state.get("injuries") or [] if c[2] == "r"],
            "injuries_yellow": [c for c in team_state.get("injuries") or [] if c[2] == "y"]}


def _stats_view(match, teams):
    """As estatísticas do fim da partida (WE 2002), para o botão Gols/Estatísticas
    do card: [(rótulo, LOCAL, VISITA, o lado do melhor número - 0, 1 ou None no
    empate)] - só com a partida encerrada e as dos dois times."""
    if match["status"] != "finished" or len(teams) < 2 or not all(isinstance(t.get("stats"), dict) for t in teams[:2]):
        return None
    rows = []
    for key, label, more_is_better in STATS:
        a, b = teams[0]["stats"].get(key, 0), teams[1]["stats"].get(key, 0)
        rows.append((label, a, b, None if a == b else int((b > a) == more_is_better)))
    return rows


# WE3: o relógio fica em 0 durante o acréscimo (não é o intervalo).
_ADDED_TIME_AT_ZERO = {"SLPM_861.62"}


def _match_state(match):
    try:
        return json.loads(match["state_json"] or "{}")
    except ValueError:
        return {}


def _interval(match):
    """Entre dois tempos: o juiz apitou o fim do 1º tempo (ou do 1º da
    prorrogação) ou o seguinte ainda não começou (relógio cheio), até o fork
    mandar o relógio andando. Fork sem o "over": o relógio em 0 (o WE 2002
    ainda joga um pouco depois dele - um gol nesse tempo aparecia no
    "Intervalo")."""
    if match["status"] != "live":
        return False
    period, left, half = match["period"], match["clock_left"], match["half_frames"] or 18000
    if 1 <= period <= 3 and left >= half:
        return True
    if period in (0, 2) and left == 0:
        state = _match_state(match)
        if state.get("over") is not None:
            return bool(state["over"])
        return state.get("engine") not in _ADDED_TIME_AT_ZERO
    return False


def _period_over(match):
    """O juiz apitou o fim do 2º tempo ou da prorrogação e a partida segue
    (empate: prorrogação ou pênaltis a seguir) - o fim, de fato, vem no final."""
    return match["status"] == "live" and match["period"] in (1, 3) and bool(_match_state(match).get("over"))


def _reconnecting(match):
    """Reaberta pelo Reconectar, esperando o host liberar os controles."""
    return match["status"] == "live" and bool(match["reconnecting_at"])


# O card antes do pontapé (fork novo: a tela de configuração da partida ou o "Repeat").
WAITING_LABELS = {1: "Aguardando o início da partida", 2: "Aguardando o reinício da partida"}


def _clock_label(match):
    period, left, half = match["period"], match["clock_left"], match["half_frames"] or 18000
    if match["status"] != "live":
        return "Encerrado" if match["status"] == "finished" else "Interrompido"
    if _reconnecting(match):
        return "Reconectando"
    if match["waiting"]:
        return WAITING_LABELS.get(match["waiting"], WAITING_LABELS[1])
    if period >= 4:
        return "Pênaltis"
    if _interval(match):
        return "Intervalo"
    if _period_over(match):
        return "Fim do tempo normal" if period == 1 else "Fim da prorrogação"
    if period > 1:  # prorrogação: 2 tempos de 15 minutos
        half_no = period - 1
        if left == 0:
            return "Prorrogação · %dº tempo · %d+'" % (half_no, 90 + 15 * half_no)
        return "Prorrogação · %dº tempo · %d'" % (half_no, 90 + 15 * (half_no - 1) + int(15 * (1 - left / half)))
    if left == 0:
        return "%dº tempo · %d+'" % (period + 1, 45 * (period + 1))
    return "%dº tempo · %d'" % (period + 1, int(45 * (1 - left / half)) + 45 * period)


def _context(db, match):
    """(linhas, link) do campeonato/amistoso da partida: no jogo de campeonato,
    o campeonato numa linha e, na outra, a fase com a rodada e a ida/volta."""
    if match["tournament_match_id"]:
        row = db.execute(
            "SELECT m.round, m.leg, m.id, s.name AS stage_name, s.kind, t.name, t.slug, tie.legs FROM tournament_matches m "
            "JOIN tournament_stages s ON s.id = m.stage_id JOIN tournaments t ON t.id = s.tournament_id "
            "LEFT JOIN tournament_ties tie ON tie.id = m.tie_id WHERE m.id = ?", (match["tournament_match_id"],)).fetchone()
        if row:
            # 2 linhas: o campeonato e "Fase de grupos: Rodada 6" (mata-mata de ida e volta: "Final: Ida").
            detail = []
            if row["kind"] != "knockout":
                detail.append("Rodada %d" % row["round"])
            if row["legs"] == 2:
                detail.append("Ida" if row["leg"] == 1 else "Volta")
            stage = row["stage_name"] or ""
            second = "%s: %s" % (stage, " · ".join(detail)) if stage and detail else stage or " · ".join(detail)
            return [p for p in (row["name"], second) if p], url_for("tournaments.show", slug=row["slug"], aba="tabela")
    if match["friendly_id"]:
        # A página do amistoso é só dos dois jogadores: o link, só para eles.
        row = db.execute("SELECT home_id, away_id FROM friendly_matches WHERE id = ?", (match["friendly_id"],)).fetchone()
        mine = row is not None and session.get("player_id") in (row["home_id"], row["away_id"])
        return ["Amistoso"], url_for("tournaments.friendly_show", friendly_id=match["friendly_id"]) if mine else None
    return ["Partida livre"], None


def _tie_view(db, match, sides, goals):
    """Mata-mata de ida e volta: "Final · Jogo 2 de 2" e, no jogo de volta, o
    total somando o jogo de ida, na ordem do card (LOCAL, VISITA). O placar da
    ida vale confirmado ou, enquanto não, o reportado (provisional)."""
    if not match["tournament_match_id"]:
        return None
    row = db.execute(
        "SELECT m.leg, m.tie_id, m.home_entry_id, m.away_entry_id, s.name AS stage_name, tie.legs, "
        "hp.username AS home_nick, ap.username AS away_nick FROM tournament_matches m "
        "JOIN tournament_stages s ON s.id = m.stage_id LEFT JOIN tournament_ties tie ON tie.id = m.tie_id "
        "JOIN tournament_entries he ON he.id = m.home_entry_id JOIN players hp ON hp.id = he.player_id "
        "JOIN tournament_entries ae ON ae.id = m.away_entry_id JOIN players ap ON ap.id = ae.player_id "
        "WHERE m.id = ?", (match["tournament_match_id"],)).fetchone()
    if row is None or not row["tie_id"] or row["legs"] != 2:
        return None
    view = {"label": "%s · Jogo %d de 2" % (row["stage_name"], row["leg"]), "aggregate": None, "provisional": False}
    if row["leg"] != 2:
        return view
    first = db.execute("SELECT * FROM tournament_matches WHERE tie_id = ? AND leg = 1", (row["tie_id"],)).fetchone()
    score = None
    if first is not None and first["status"] == "confirmed":
        score = (first["home_goals"], first["away_goals"])
    elif first is not None and first["status"] in ("reported", "disputed"):
        from .tournament.reports import last_report
        reported = last_report(db, first["id"])
        if reported and reported["home_goals"] is not None:
            score, view["provisional"] = (reported["home_goals"], reported["away_goals"]), True
    entry_of = {row["home_nick"].casefold(): row["home_entry_id"], row["away_nick"].casefold(): row["away_entry_id"]}
    entries = [next((entry_of[n.casefold()] for n in nicks if n.casefold() in entry_of), None) for nicks in sides]
    if score is None or None in entries or entries[0] == entries[1]:
        return view
    first_goals = {first["home_entry_id"]: score[0], first["away_entry_id"]: score[1]}
    view["aggregate"] = (goals[0] + first_goals.get(entries[0], 0), goals[1] + first_goals.get(entries[1], 0))
    return view


def _file_name(match):
    return re.sub(r"^PCSX ReARMed:\s*", "", match["room"] or "")


def _catalog_iso(db, match):
    """A ISO do cadastro de amistosos da partida (a imagem do card quando não
    há campeonato com imagem): a do perfil da ISO (admin), a mesma ISO
    (CRC32:TAMANHO) ou, sem identificação, o mesmo nome de arquivo."""
    row = db.execute("SELECT catalog_iso_id FROM iso_profiles WHERE content_id = ? COLLATE NOCASE "
                     "AND catalog_iso_id IS NOT NULL", (match["content_id"],)).fetchone()
    if row:
        return row["catalog_iso_id"]
    for column, value in (("content_id", match["content_id"]), ("original_filename", _file_name(match))):
        row = db.execute(f"SELECT id FROM catalog_isos WHERE {column} = ? COLLATE NOCASE AND is_active = 1 "
                         "ORDER BY approved_at IS NULL, id DESC LIMIT 1", (value,)).fetchone() if value else None
        if row:
            return row["id"]
    return None


def _profile(db, match):
    """Jogo base, campeonato e ISO do cadastro da partida (os logos do card):
    o do jogo de campeonato ligado, o do amistoso (com a ISO escolhida no
    convite), o cadastrado/aprendido para a ISO ou o da ISO de
    campeonato/amistoso com o mesmo nome de arquivo. O card mostra a imagem do
    campeonato ou, sem ela, a da ISO do cadastro."""
    game_id = tournament_id = catalog_id = None
    if match["tournament_match_id"]:
        row = db.execute("SELECT t.id, t.game_title_id FROM tournament_matches m JOIN tournament_stages s "
                         "ON s.id = m.stage_id JOIN tournaments t ON t.id = s.tournament_id WHERE m.id = ?",
                         (match["tournament_match_id"],)).fetchone()
        if row:
            game_id, tournament_id = row["game_title_id"], row["id"]
    if game_id is None and match["friendly_id"]:
        row = db.execute("SELECT game_title_id, iso_tournament_id, iso_catalog_id FROM friendly_matches WHERE id = ?",
                         (match["friendly_id"],)).fetchone()
        if row:
            game_id, tournament_id, catalog_id = row["game_title_id"], row["iso_tournament_id"], row["iso_catalog_id"]
    if game_id is None:
        row = db.execute("SELECT * FROM iso_profiles WHERE content_id = ?", (match["content_id"],)).fetchone()
        if row:
            game_id, tournament_id = row["game_title_id"], row["tournament_id"]
            if game_id is None and row["catalog_iso_id"]:
                catalog = db.execute("SELECT game_title_id FROM catalog_isos WHERE id = ?",
                                     (row["catalog_iso_id"],)).fetchone()
                game_id = catalog["game_title_id"] if catalog else None
    if game_id is None:
        # A ISO exata (CRC32:TAMANHO) de um campeonato ou do cadastro de amistosos.
        row = db.execute("SELECT t.id, t.game_title_id FROM tournament_isos i JOIN tournaments t ON t.id = i.tournament_id "
                         "WHERE i.content_id = ? COLLATE NOCASE AND t.status != 'cancelled' "
                         "ORDER BY t.updated_at DESC LIMIT 1", (match["content_id"],)).fetchone()
        if row:
            game_id, tournament_id = row["game_title_id"], row["id"]
        else:
            row = db.execute("SELECT game_title_id FROM catalog_isos WHERE content_id = ? COLLATE NOCASE "
                             "AND is_active = 1 LIMIT 1", (match["content_id"],)).fetchone()
            game_id = row["game_title_id"] if row else None
    if game_id is None:
        engine = json.loads(match["state_json"] or "{}").get("engine")
        row = db.execute("SELECT id FROM game_titles WHERE code = ?", (ENGINE_GAMES.get(engine),)).fetchone()
        game_id = row["id"] if row else None
    file_name = _file_name(match)
    if game_id is None and file_name:
        row = db.execute("SELECT t.id, t.game_title_id FROM tournament_isos i JOIN tournaments t ON t.id = i.tournament_id "
                         "WHERE i.original_filename = ? COLLATE NOCASE AND t.status != 'cancelled' "
                         "ORDER BY t.updated_at DESC LIMIT 1", (file_name,)).fetchone()
        if row:
            game_id, tournament_id = row["game_title_id"], row["id"]
        else:
            row = db.execute("SELECT game_title_id FROM catalog_isos WHERE original_filename = ? COLLATE NOCASE "
                             "AND is_active = 1 LIMIT 1", (file_name,)).fetchone()
            game_id = row["game_title_id"] if row else None
    game = db.execute("SELECT name, code FROM game_titles WHERE id = ?", (game_id,)).fetchone() if game_id else None
    tournament = db.execute("SELECT name, slug, image FROM tournaments WHERE id = ?",
                            (tournament_id,)).fetchone() if tournament_id else None
    catalog = None
    if not (tournament and tournament["image"]):
        catalog_id = catalog_id or _catalog_iso(db, match)
        catalog = db.execute("SELECT id, name, image, approved_at, is_active FROM catalog_isos WHERE id = ?",
                             (catalog_id,)).fetchone() if catalog_id else None
    return game, tournament, catalog


def _confirmation(db, match, views):
    """"Confirmar" e "Contestar" no card e quem já confirmou o placar dele - o
    jogo de campeonato reportado ou confirmado (só no card da partida que deu
    esse placar), o jogo do amistoso sem "Placar agregado" (friendly.confirm_game)
    ou o amistoso com placar agregado já reportado (friendly.confirm, o amistoso
    inteiro). confirmed_by = os nomes de quem já confirmou (o selo verde
    "TIERES confirmou" embaixo do status, para todos); state, para quem está
    logado e joga a partida: "pending" (os dois botões), "done" (já confirmou: só
    o selo) ou None (não joga, ou o jogo já fechou). None sem nada a mostrar."""
    from .tournament import friendly, reports
    if match["is_test"] or match["status"] == "live":
        return None
    me = session.get("player_id")
    label = "%s x %s" % (views[0]["name"], views[1]["name"])
    info = {"label": label, "confirm_url": url_for("live.confirm", live_id=match["id"])}
    if match["tournament_match_id"]:
        row = db.execute(
            "SELECT m.*, t.slug, t.id AS tournament_id, hp.username AS home_nick, ap.username AS away_nick, "
            "he.player_id AS home_player, ae.player_id AS away_player FROM tournament_matches m "
            "JOIN tournament_stages s ON s.id = m.stage_id JOIN tournaments t ON t.id = s.tournament_id "
            "JOIN tournament_entries he ON he.id = m.home_entry_id JOIN players hp ON hp.id = he.player_id "
            "JOIN tournament_entries ae ON ae.id = m.away_entry_id JOIN players ap ON ap.id = ae.player_id "
            "WHERE m.id = ?", (match["tournament_match_id"],)).fetchone()
        if row is None or row["status"] not in ("reported", "confirmed"):
            return None
        if row["status"] == "reported":
            reported = reports.last_report(db, row["id"])
            score = (reported["home_goals"], reported["away_goals"]) if reported else None
        else:
            score = (row["home_goals"], row["away_goals"])
        sides = dict(match, home_nick=row["home_nick"], away_nick=row["away_nick"])
        if score is None or _home_away(sides, (match["local_goals"] or 0, match["visita_goals"] or 0)) != score:
            return None  # outra partida do mesmo jogo (jogaram de novo): o placar é de outra
        # Quem confirmou o último placar reportado (ou o informou ele mesmo), na ordem.
        # (Partida interrompida: a confirmação de um já libera o placar - nunca fica esperando o outro.)
        order = [row["reported_by_entry_id"]] if row["reported_by_entry_id"] else []
        order += reports.confirmed_by(db, row["id"])
        by_entry = {row["home_entry_id"]: row["home_player"], row["away_entry_id"]: row["away_player"]}
        players = {row["home_player"]: row["home_nick"], row["away_player"]: row["away_nick"]}
        done = list(dict.fromkeys(by_entry[entry] for entry in order if entry in by_entry))
        open_ = row["status"] == "reported"
        url = url_for("tournaments.match", slug=row["slug"], match_id=row["id"])
        info.update(kind="tournament", tournament_id=row["tournament_id"], match_id=row["id"], url=url,
                    dispute_url=url + "#contestar", what="a partida " + label)
    elif match["friendly_id"]:
        row = friendly.get(db, match["friendly_id"])
        if row is None:
            return None
        players = {row["home_id"]: row["home_name"], row["away_id"]: row["away_name"]}
        url = url_for("tournaments.friendly_show", friendly_id=row["id"])
        if friendly.per_game(row):
            if row["status"] == "accepted":
                if (match["game_report"] or "").startswith(_NOT_SUMMED):
                    return None
            elif row["status"] != "reported" or not row["game_report"] or db.execute(
                    "SELECT 1 FROM friendly_games WHERE friendly_id = ? AND live_match_id = ?",
                    (row["id"], match["id"])).fetchone() is None:
                return None
            confirms = friendly.game_confirms(db, row["id"]).get(match["id"], ())
            done = [pid for pid in players if pid in confirms or (
                row["status"] == "reported" and row["first_confirmed_by"] == pid)]
            what = "a partida " + label
        else:
            if row["status"] != "reported":
                return None
            done = [pid for pid in players if row["reported_by"] == pid or bool(
                row["game_report"] and row["first_confirmed_by"] == pid)]
            what = "o amistoso %s x %s" % (row["home_name"], row["away_name"])
        open_ = True
        # "Não aceitar o placar" só existe depois do fim da sessão (amistoso reportado).
        info.update(kind="friendly", friendly_id=row["id"], per_game=friendly.per_game(row), url=url,
                    dispute_url=url + ("#rejeitar-%d" % row["id"] if row["status"] == "reported" else ""),
                    what=what)
    else:
        return None
    info["confirmed_by"] = [players[pid] for pid in done]
    info["state"] = ("done" if me in done else "pending") if open_ and me in players else None
    if not info["confirmed_by"] and info["state"] is None:
        return None
    return info


@bp.post("/ao-vivo/<int:live_id>/confirmar")
def confirm(live_id):
    """O "Confirmar" do card (static/js/live-confirm.js): confirma por quem está
    logado o jogo de campeonato, o jogo do amistoso ou o amistoso (_confirmation)
    e responde em JSON {ok, message, url} - url = a página do jogo/amistoso."""
    from .tournament import friendly, notify, reports, store
    from .tournament.store import TournamentError
    if "player_id" not in session:
        return jsonify(ok=0, message="Entre na sua conta para confirmar."), 401
    validate_csrf()
    db = get_db()
    match = db.execute("SELECT * FROM live_matches WHERE id = ?", (live_id,)).fetchone()
    info = card(db, match)["confirm"] if match else None
    if not info or info["state"] != "pending":
        return jsonify(ok=0, message="Esta partida não está esperando a sua confirmação.",
                       url=info["url"] if info else None), 400
    me, tournament = session["player_id"], None
    try:
        if info["kind"] == "tournament":
            tournament = store.get_tournament(db, info["tournament_id"])
            reports.confirm(db, tournament, info["match_id"], me)
        elif info["per_game"]:
            friendly.confirm_game(db, info["friendly_id"], me, live_id)
        else:
            friendly.confirm(db, info["friendly_id"], me)
        db.commit()
    except TournamentError as error:
        db.rollback()
        return jsonify(ok=0, message=str(error), url=info["url"]), 400
    if tournament is not None:
        notify.check_completion(db, tournament)  # o aviso de fim do campeonato, como na página do jogo
    # Quem já confirmou: os selos verdes "TIERES confirmou" que entram no lugar dos botões (live-confirm.js).
    after = card(db, db.execute("SELECT * FROM live_matches WHERE id = ?", (live_id,)).fetchone())["confirm"]
    return jsonify(ok=1, message="Obrigado por confirmar %s" % info["what"], url=info["url"],
                   confirmed_by=after["confirmed_by"] if after else [])


def card(db, match):
    from .images import url as image_url
    from .timeutil import format_local
    from .tournament.games import cover_url
    state = json.loads(match["state_json"] or "{}")
    teams = state.get("teams") or [{}, {}]
    sides = (json.loads(match["local_players_json"]), json.loads(match["visita_players_json"]))
    label, link = _context(db, match)
    reconnecting = _reconnecting(match)
    # "Reconectando": sem informe até o host liberar os controles - não é "Sem sinal".
    stale = match["status"] == "live" and not reconnecting and match["updated_at"] < _ago(STALE)
    game, tournament, catalog = _profile(db, match)
    # Clicar nos logos, no nome ou na descrição da partida: a página do
    # campeonato da ISO ou, sem ele, a página da ISO do cadastro (só a liberada).
    tournament_url = url_for("tournaments.show", slug=tournament["slug"], aba="tabela") if tournament else None
    iso_url = (url_for("tournaments.iso_page", catalog_id=catalog["id"])
               if catalog and catalog["approved_at"] and catalog["is_active"] else None)
    views = [_team_view(db, match, i, teams[i] if i < len(teams) else {}, sides[i]) for i in (0, 1)]
    # Lado sem controle: a CPU (o outro lado tem jogador) ou, sem a tabela de controles, "Player N".
    nobody = not any(view["players"] for view in views)
    for number, view in enumerate(views, 1):
        view["people"] = _people(db, view["players"])
        if not view["players"]:
            view["players"] = ["Player %d" % number] if nobody else ["CPU"]
            view["people"] = [{"id": None, "name": view["players"][0]}]
    from .broadcaster import card_view as broadcast_view
    return {"id": match["id"], "status": match["status"], "stale": stale, "is_test": match["is_test"],
            "reconnecting": reconnecting,
            # Antes do pontapé ("Aguardando o início/reinício da partida"): sem placar nem o risco animado.
            "waiting": bool(match["waiting"]),
            # Transmissão no YouTube (app/broadcaster.py): o link e, para o administrador, Transmitir/Parar.
            "broadcast": broadcast_view(db, match["id"]),
            "hidden": bool(match["hidden"]),  # ocultado pelo administrador (só ele vê, com "Mostrar")
            "deleted": bool(match["deleted_at"]),  # excluído pelos jogadores (só o administrador vê)
            "discarded": _discarded(match),  # não chegou a valer (discard - só o administrador vê)
            # "Excluir" o card da partida interrompida, para quem a jogou (_deletion).
            "delete": _deletion(db, match),
            # Quem já excluiu (o selo embaixo do "Interrompido", para todos).
            "deleted_by": _deleted_by(db, match),
            # Quem saiu da partida pelo menu do jogo (o selo embaixo do "Interrompido").
            "quit": match["quit_nick"],
            "clock": "Sem sinal" if stale else _clock_label(match),
            # sem o risco animado (no "Reconectando", com ele)
            "interval": not reconnecting and (bool(match["waiting"]) or _interval(match) or _period_over(match)),
            "context": label, "link": link,
            # Confirmar/Contestar no card, para quem joga a partida e ainda precisa responder.
            "confirm": _confirmation(db, match, views),
            # Jogo de campeonato: o nome dele já está no título (_context); nos outros (amistoso com a
            # ISO de um campeonato, partida livre) ele vai embaixo do título.
            "championship": bool(match["tournament_match_id"]),
            "agreed": match["agreed"], "reporters": match["reporters"], "file": _file_name(match),
            "when": format_local(match["started_at"], "%d/%m/%Y %H:%M"),
            "game": {"name": game["name"], "cover": cover_url(game["code"], mini=True)} if game else None,
            "tournament": {"name": tournament["name"], "image": image_url("torneio", tournament["image"]),
                           "url": tournament_url} if tournament else None,
            # Sem campeonato com imagem: a ISO do cadastro (a do amistoso ou a mesma ISO).
            "iso": {"name": catalog["name"], "image": image_url("torneio", catalog["image"]), "url": iso_url}
            if catalog else None,
            "target": tournament_url or iso_url,
            "pens": state.get("pens"), "pk": _shootout_view(state), "pk_winner": _shootout_winner(match, state),
            "teams": views, "stats": _stats_view(match, teams),
            "tie": _tie_view(db, match, sides, (views[0]["goals"], views[1]["goals"])),
            "final": _free_final(db, match)}


def _shootout_view(state):
    """As bolas da disputa de pênaltis no card, por lado (LOCAL, VISITA):
    "goal" (verde), "miss" (vermelha) e, as que ainda faltam, "todo" (branca)
    - 5 de cada lado e, nas alternadas, uma a mais por rodada (os dois lados
    com o mesmo número). Fork sem a lista de cobranças: None (só o placar)."""
    pk = state.get("pk")
    if not state.get("pens") or not pk:
        return None
    slots = max(PK_ROUND, len(pk[0]), len(pk[1]))
    return [["goal" if kick else "miss" for kick in side] + ["todo"] * (slots - len(side)) for side in pk]


def _shootout_winner(match, state):
    """Partida encerrada nos pênaltis: o lado que ganhou a disputa (0 LOCAL, 1
    VISITA) - o placar dele vai em verde no card. None sem vencedor (ao vivo,
    interrompida ou sem disputa)."""
    pens = state.get("pens")
    if match["status"] != "finished" or not pens or pens[0] == pens[1]:
        return None
    return 0 if pens[0] > pens[1] else 1


def _people(db, nicks):
    """Quem controla um lado do card: a conta de cada nick (id e o nome dela -
    a foto com o rank, _avatar.html, e o link do perfil) ou só o nick."""
    people = []
    for nick in nicks:
        row = db.execute("SELECT id, username FROM players WHERE username = ? COLLATE NOCASE", (nick,)).fetchone()
        people.append({"id": row["id"], "name": row["username"]} if row else {"id": None, "name": nick})
    return people


def _free_final(db, match):
    """Partida livre interrompida (tournament/free.py): o placar final que os
    dois combinam - {"text", "url"} para o rodapé do card (o link, só para os
    dois jogadores), ou None."""
    if match["status"] != "abandoned" or match["tournament_match_id"] or match["friendly_id"] or match["is_test"]:
        return None
    from .tournament import free
    found = free.get(db, match["id"])
    if found is None:
        return None
    me = session.get("player_id")
    mine = free.side_of(found, me) is not None
    url = url_for("tournaments.free_match", live_id=match["id"]) if mine else None
    if found["score_status"] == "confirmed":
        return {"text": "Placar final combinado: %d x %d" % found["score"], "url": url, "pending": False}
    if not mine:
        return None
    # Pendente: um botão na linha de botões do card - action = o rótulo dele.
    if found["score_status"] == "reported" and found["reported_by"] == me:
        text, action = "Placar final informado: falta o adversário confirmar", "wait"
    elif found["score_status"] == "reported":
        text, action = "O adversário informou o placar final: confirme ou não aceite", "confirm"
    else:
        text, action = "Registrar o placar final da partida interrompida", "report"
    return {"text": text, "url": url, "pending": True, "action": action}


# --- Excluir o card de uma partida interrompida ----------------------------------
#
# Partida que parou logo (conexão ruim, desistiram): cada jogador clica em
# "Excluir" no card (live_match_deletions); o primeiro avisa os outros por
# e-mail (notify.live_card_deleted) e, com todos, o card sai do site (deleted_at,
# hidden) e a partida não vale mais nada. Os que o jogador ainda pode excluir
# ficam em Meus jogos e no número do menu (deletable_cards).

DELETED_OUTCOME = "excluída pelos jogadores"
DELETED_NOTE = ("Os jogadores excluíram o card da partida interrompida no Ao vivo (nº %d): o placar de quando "
                "ela parou foi retirado - o jogo volta a ficar sem placar.")


def _card_players(db, match):
    """[(id, nome)] das contas dos jogadores da partida, na ordem da sala; None
    se algum nick não tem conta (então ninguém exclui)."""
    people = []
    for nick in json.loads(match["players_json"] or "[]"):
        row = db.execute("SELECT id, username FROM players WHERE username = ? COLLATE NOCASE", (nick,)).fetchone()
        if row is None:
            return None
        if all(row["id"] != pid for pid, _ in people):
            people.append((row["id"], row["username"]))
    return people


def _pending_score(db, match):
    """O placar de quando a partida parou, ainda sem confirmação, é o do jogo de
    campeonato dela? (Excluindo o card, ele é retirado, e quem saiu não perde
    mais o triplo de XP.) A interrompida (um confirma) ou a abandonada pelo
    menu do jogo (quit_nick: os dois confirmam, ou o prazo)."""
    if not match["tournament_match_id"] or not (match["game_report"] or "").startswith("reportado"):
        return False
    game = db.execute("SELECT status, interrupted, reported_by_entry_id FROM tournament_matches WHERE id = ?",
                      (match["tournament_match_id"],)).fetchone()
    return bool(game and game["status"] == "reported" and (game["interrupted"] or match["quit_nick"])
                and not game["reported_by_entry_id"])


def undeletable(db, match):
    """Por que o card não pode ser excluído pelos jogadores (o texto), ou None
    se pode: só o de uma partida interrompida (não de teste, com todos
    logados) que não vale nada - não de amistoso (as partidas dele fazem o
    placar do amistoso), partida livre sem o placar final combinado e, no
    jogo de campeonato, sem placar valendo por ela (o de quando parou ainda
    sem confirmação sai junto: _pending_score)."""
    if match["status"] != "abandoned" or match["is_test"] or not match["verified"] or match["deleted_at"]:
        return "Só o card de uma partida interrompida pode ser excluído."
    if match["friendly_id"]:
        return "O card de um jogo de amistoso não pode ser excluído: ele faz parte do placar do amistoso."
    if match["tournament_match_id"]:
        if (match["game_report"] or "").startswith("reportado") and not _pending_score(db, match):
            game = db.execute("SELECT status FROM tournament_matches WHERE id = ?",
                              (match["tournament_match_id"],)).fetchone()
            if game and game["status"] != "scheduled":
                return "O placar desta partida já vale no jogo do campeonato: o card não pode ser excluído."
    else:
        score = db.execute("SELECT status FROM free_match_scores WHERE live_match_id = ?", (match["id"],)).fetchone()
        if score and score["status"] == "confirmed":
            return "O placar final desta partida já foi combinado: o card não pode ser excluído."
    if not _card_players(db, match):
        return "Só dá para excluir o card quando todos os jogadores da partida têm conta no WE Camp."
    return None


def deletion_requests(db, live_id):
    """Ids de quem já excluiu o card."""
    return {row["player_id"] for row in db.execute(
        "SELECT player_id FROM live_match_deletions WHERE live_match_id = ?", (live_id,))}


def _names_text(names):
    names = list(names)
    return " e ".join([", ".join(names[:-1]), names[-1]]) if len(names) > 1 else (names[0] if names else "")


def _deleted_by(db, match):
    """Os nomes de quem já excluiu o card da partida interrompida, enquanto
    falta alguém (o selo "tassio excluiu" embaixo do "Interrompido")."""
    if match["status"] != "abandoned" or match["deleted_at"]:
        return []
    return [row["username"] for row in db.execute(
        "SELECT p.username FROM live_match_deletions d JOIN players p ON p.id = d.player_id "
        "WHERE d.live_match_id = ? ORDER BY d.created_at, p.username COLLATE NOCASE", (match["id"],))]


def _deletion(db, match):
    """O "Excluir" no card para quem está logado e jogou a partida
    (undeletable): state "pending" (ainda não excluiu) ou "done" (já excluiu,
    falta outro); by/waiting = os nomes de quem já excluiu e de quem falta;
    score = o placar de quando parou que sai do jogo de campeonato (texto) ou
    None; free = há placar final proposto (sai junto). None: nada a mostrar."""
    me = session.get("player_id")
    if not me or match["status"] != "abandoned" or match["deleted_at"]:
        return None
    people = _card_players(db, match)
    if not people or all(pid != me for pid, _ in people) or undeletable(db, match):
        return None
    done = deletion_requests(db, match["id"])
    score = quitter = None
    if _pending_score(db, match):
        score = "%d x %d" % (match["local_goals"] or 0, match["visita_goals"] or 0)
        # Quem saiu (pela queda ou pelo menu do jogo) deixa de perder o triplo de XP.
        row = db.execute("SELECT p.username FROM tournament_matches m JOIN players p ON p.id = m.quitter_id "
                         "WHERE m.id = ?", (match["tournament_match_id"],)).fetchone()
        quitter = row["username"] if row else None
    free_score = None if match["tournament_match_id"] else db.execute(
        "SELECT 1 FROM free_match_scores WHERE live_match_id = ?", (match["id"],)).fetchone()
    return {"state": "done" if me in done else "pending",
            "by": [name for pid, name in people if pid in done and pid != me],
            "waiting": _names_text(name for pid, name in people if pid not in done and pid != me),
            "url": url_for("live.delete_card", live_id=match["id"]), "score": score, "quitter": quitter,
            "free": bool(free_score)}


def deletable_cards(db, player_id):
    """As partidas interrompidas cujo card o jogador ainda pode excluir (sem o
    pedido dele), da mais recente: Meus jogos e o número do menu. A partida
    livre com o placar final sendo combinado (free.py: informado ou não
    aceito) fica de fora - ela já está lá pelo placar; o card ainda exclui."""
    from .listing import like
    row = db.execute("SELECT username FROM players WHERE id = ?", (player_id,)).fetchone()
    if row is None:
        return []
    found = []
    for match in db.execute(
            "SELECT * FROM live_matches m WHERE m.status = 'abandoned' AND m.is_test = 0 AND m.verified = 1 "
            "AND m.deleted_at IS NULL AND m.friendly_id IS NULL AND m.players_json LIKE ? ESCAPE '\\' "
            "AND NOT EXISTS (SELECT 1 FROM live_match_deletions d WHERE d.live_match_id = m.id AND d.player_id = ?) "
            "AND NOT EXISTS (SELECT 1 FROM free_match_scores s WHERE s.live_match_id = m.id) "
            "ORDER BY m.started_at DESC, m.id DESC", (like('"%s"' % row["username"]), player_id)).fetchall():
        people = _card_players(db, match)
        if people and any(pid == player_id for pid, _ in people) and not undeletable(db, match):
            found.append(match)
    return found


def _delete_card(db, match):
    """Todos os jogadores excluíram: o card sai do site e a partida não vale
    mais - o placar de quando parou sai do jogo de campeonato (sem
    confirmação: _pending_score) e a partida livre some do perfil (free.py)."""
    from .tournament import reports
    if _pending_score(db, match):
        reports.withdraw_game_report(db, match["tournament_match_id"], DELETED_NOTE % match["id"])
    db.execute("UPDATE live_matches SET deleted_at = ?, hidden = 1, game_report = ? WHERE id = ?",
               (now(), DELETED_OUTCOME, match["id"]))


def _local_day(value, end=False):
    """AAAA-MM-DD (dia em São Paulo) -> início do dia (ou do dia seguinte) em UTC ISO."""
    from .timeutil import LOCAL_TZ
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    moment = day.replace(tzinfo=LOCAL_TZ) + (timedelta(days=1) if end else timedelta())
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


# Amistosos que valem para o Ranking: o "Rankeado" do convite enquanto em
# aberto; depois de confirmado, o que valeu de fato (friendly.py).
_RANKED_FRIENDLIES = ("SELECT id FROM friendly_matches "
                      "WHERE (CASE WHEN status = 'confirmed' THEN ranked ELSE ranked_requested END) = 1")
# O filtro "Campeonato" nas opções fixas (stats.KIND_FILTERS): os três cards do perfil.
_KIND_WHERE = {
    "ranked": f"(m.tournament_match_id IS NOT NULL OR m.friendly_id IN ({_RANKED_FRIENDLIES}))",
    "friendly": ("(m.tournament_match_id IS NULL AND m.friendly_id IS NOT NULL "
                 f"AND m.friendly_id NOT IN ({_RANKED_FRIENDLIES}))"),
    "free": "(m.tournament_match_id IS NULL AND m.friendly_id IS NULL)",
}
# Campeonatos com partidas no Ao vivo (as sugestões do filtro "Campeonato").
_LIVE_TOURNAMENTS = (
    "SELECT t.id, t.name FROM tournaments t WHERE t.id IN ("
    "SELECT s.tournament_id FROM live_matches m JOIN tournament_matches tm ON tm.id = m.tournament_match_id "
    "JOIN tournament_stages s ON s.id = tm.stage_id) ORDER BY t.name COLLATE NOCASE")


def _plain(value):
    from unicodedata import normalize
    return " ".join(normalize("NFKD", value or "").encode("ascii", "ignore").decode().casefold().split())


def tournaments_named(db, text):
    """Campeonatos com partidas no Ao vivo pelo nome digitado no filtro: o nome
    exato (sem acento/maiúscula) ou, sem ele, os que contêm o texto."""
    rows = db.execute(_LIVE_TOURNAMENTS).fetchall()
    typed = _plain(text)
    exact = [row["id"] for row in rows if _plain(row["name"]) == typed]
    return exact or [row["id"] for row in rows if typed in _plain(row["name"])]


# Filtro "ISO" do Ao vivo: a ISO de cada partida como o card a identifica
# (_profile, _catalog_iso) - a do cadastro de amistosos ou a de um campeonato.
NO_ISO = "Sem ISO"
# O nome do arquivo da partida no SQL (_file_name): a sala sem o "PCSX ReARMed: ".
_ROOM_FILE = "(CASE WHEN m.room LIKE 'PCSX ReARMed:%' THEN ltrim(substr(m.room, 14)) ELSE m.room END)"
# As ISOs que valem: as ativas do cadastro e as enviadas nos campeonatos (não cancelados).
_CATALOG_ISO_IDS = "SELECT id FROM catalog_isos WHERE is_active = 1"
_TOURNAMENT_ISO_IDS = ("SELECT i.tournament_id FROM tournament_isos i JOIN tournaments t ON t.id = i.tournament_id "
                       "WHERE t.status != 'cancelled'")
# A partida tem o CRC32:TAMANHO de alguma ISO cadastrada (ou ligada a uma no perfil
# da ISO): sem isso, o card cai no nome do arquivo.
_KNOWN_CONTENT = (
    "m.content_id COLLATE NOCASE IN (SELECT content_id FROM catalog_isos WHERE is_active = 1 "
    "AND COALESCE(content_id, '') != '' UNION SELECT content_id FROM tournament_isos "
    f"WHERE tournament_id IN ({_TOURNAMENT_ISO_IDS}) AND COALESCE(content_id, '') != '' "
    "UNION SELECT content_id FROM iso_profiles WHERE catalog_iso_id IS NOT NULL OR tournament_id IS NOT NULL)")


def _iso_where(catalog_in=None, tournament_in=None):
    """SQL de "a partida m foi jogada com uma destas ISOs": catalog_in e
    tournament_in vão no IN (...) - ids do cadastro e de campeonatos com ISO
    (só números) ou uma subconsulta; None = nenhuma. Vale a ISO escolhida no
    amistoso, o jogo do campeonato, o perfil da ISO (admin/aprendido), a mesma
    ISO (CRC32:TAMANHO) ou, sem o CRC32 de nenhuma ISO cadastrada, o mesmo
    nome de arquivo. Sempre 0 ou 1, nunca NULL (o "Sem ISO" é o NOT dela)."""
    parts, files = [], []
    if catalog_in:
        catalog = f"SELECT id FROM catalog_isos WHERE is_active = 1 AND id IN ({catalog_in})"
        parts += [f"m.friendly_id IN (SELECT id FROM friendly_matches WHERE iso_catalog_id IN ({catalog}))",
                  f"m.content_id COLLATE NOCASE IN (SELECT content_id FROM iso_profiles WHERE catalog_iso_id IN ({catalog}))",
                  f"m.content_id COLLATE NOCASE IN (SELECT content_id FROM catalog_isos WHERE id IN ({catalog}) "
                  "AND COALESCE(content_id, '') != '')"]
        files.append(f"SELECT original_filename FROM catalog_isos WHERE id IN ({catalog}) "
                     "AND COALESCE(original_filename, '') != ''")
    if tournament_in:
        tournaments = f"SELECT tournament_id FROM tournament_isos WHERE tournament_id IN ({tournament_in}) " \
                      f"AND tournament_id IN ({_TOURNAMENT_ISO_IDS})"
        parts += ["m.tournament_match_id IN (SELECT tm.id FROM tournament_matches tm JOIN tournament_stages ts "
                  f"ON ts.id = tm.stage_id WHERE ts.tournament_id IN ({tournaments}))",
                  f"m.friendly_id IN (SELECT id FROM friendly_matches WHERE iso_tournament_id IN ({tournaments}))",
                  f"m.content_id COLLATE NOCASE IN (SELECT content_id FROM iso_profiles WHERE tournament_id IN ({tournaments}))",
                  f"m.content_id COLLATE NOCASE IN (SELECT content_id FROM tournament_isos "
                  f"WHERE tournament_id IN ({tournaments}) AND COALESCE(content_id, '') != '')"]
        files.append(f"SELECT original_filename FROM tournament_isos WHERE tournament_id IN ({tournaments}) "
                     "AND COALESCE(original_filename, '') != ''")
    if files:
        parts.append(f"(NOT {_KNOWN_CONTENT} AND {_ROOM_FILE} COLLATE NOCASE IN ({' UNION '.join(files)}))")
    return "COALESCE(%s, 0)" % " OR ".join(parts) if parts else "0"


def _isos(db):
    """As ISOs que o filtro conhece: [(nome, "c" do cadastro / "t" do campeonato, id)]."""
    rows = db.execute("SELECT id, name FROM catalog_isos WHERE is_active = 1").fetchall()
    result = [(row["name"], "c", row["id"]) for row in rows]
    rows = db.execute(f"SELECT id, name FROM tournaments WHERE id IN ({_TOURNAMENT_ISO_IDS})").fetchall()
    return result + [(row["name"], "t", row["id"]) for row in rows]


def iso_options(db):
    """Sugestões do filtro "ISO": as ISOs do cadastro de amistosos e as dos
    campeonatos (o nome do campeonato) com partidas no Ao vivo, por nome."""
    visible = "m.verified = 1" + ("" if _is_admin() else " AND m.is_test = 0 AND m.hidden = 0")
    names = set()
    for name, kind, iso_id in _isos(db):
        where = _iso_where(**{"catalog_in" if kind == "c" else "tournament_in": str(int(iso_id))})
        if name not in names and db.execute(f"SELECT 1 FROM live_matches m WHERE {visible} AND {where} LIMIT 1").fetchone():
            names.add(name)
    return sorted(names, key=_plain)


def iso_filter(db, text):
    """(SQL, parâmetros) do filtro "ISO" pelo texto do campo: "Sem ISO" (as
    partidas sem nenhuma ISO cadastrada - o card só com o nome do arquivo), o
    nome exato de uma ISO (sem acento/maiúscula) ou, sem ele, as ISOs que
    contêm o texto e as partidas com ele no nome do arquivo (o rodapé do card)."""
    typed = _plain(text)
    if typed == _plain(NO_ISO):
        return f"NOT {_iso_where(_CATALOG_ISO_IDS, _TOURNAMENT_ISO_IDS)}", []
    isos = _isos(db)
    chosen = [(kind, iso_id) for name, kind, iso_id in isos if _plain(name) == typed]
    partial = not chosen
    if partial:
        chosen = [(kind, iso_id) for name, kind, iso_id in isos if typed in _plain(name)]
    ids = {kind: ",".join(str(int(i)) for k, i in chosen if k == kind) or None for kind in ("c", "t")}
    where = _iso_where(ids["c"], ids["t"])
    if not partial:
        return where, []
    from .listing import like
    return f"({where} OR {_ROOM_FILE} LIKE ? ESCAPE '\\')", [like(text)]


def list_cards(db, tournament_id=None, kind=None, tournament_query="", only_live=False, recent_only=False, player="",
               date_from="", date_to="", match_id=None, iso_query="", interrupted=False, limit=CARDS_LIMIT,
               count=False):
    """As últimas partidas (ao vivo primeiro), com os filtros da página:
    tournament_id = os jogos daquele campeonato (o link "Ao vivo" da tabela);
    kind = Rankeado, Amistoso ou Partida livre (_KIND_WHERE); tournament_query =
    o nome digitado de um campeonato (tournaments_named); match_id = só aquela
    partida (o placar ao lado do time na tabela do campeonato); iso_query = o
    texto do filtro "ISO" (iso_filter); interrupted = só as interrompidas (a
    caixa "Partidas interrompidas")."""
    reap(db)
    where, params = ["m.verified = 1"], []  # só com os dois jogadores logados (refresh)
    if only_live:
        where.append("m.status = 'live'")
    elif recent_only:
        where.append("(m.status = 'live' OR m.finished_at >= ?)")
        params.append(_ago(RECENT))
    if not _is_admin():
        where.append("m.is_test = 0")
        where.append("m.hidden = 0")  # card ocultado pelo administrador (hide)
    ids = [tournament_id] if tournament_id else (tournaments_named(db, tournament_query) if tournament_query else None)
    if ids is not None:
        marks = ",".join("?" * len(ids)) or "NULL"  # nenhum campeonato com esse nome: nada
        where.append("m.tournament_match_id IN (SELECT tm.id FROM tournament_matches tm JOIN tournament_stages ts "
                     f"ON ts.id = tm.stage_id WHERE ts.tournament_id IN ({marks}))")
        params.extend(ids)
    elif kind in _KIND_WHERE:
        where.append(_KIND_WHERE[kind])
    if match_id:
        where.append("m.id = ?")
        params.append(match_id)
    if iso_query:
        sql, values = iso_filter(db, iso_query)
        where.append(sql)
        params.extend(values)
    if interrupted:
        where.append("m.status = 'abandoned'")
    if player:
        # O nick de uma conta (o que a sugestão do campo preenche): só as
        # partidas dele ("TIERES" não traz as do TIERES_ART); um pedaço do
        # nick procura em todos. "_" e "%" valem como letras (listing.like).
        from .listing import like
        exact = db.execute("SELECT 1 FROM players WHERE username = ? COLLATE NOCASE", (player,)).fetchone()
        where.append("m.players_json LIKE ? ESCAPE '\\'")
        params.append(like('"%s"' % player if exact else player))
    start, end = _local_day(date_from), _local_day(date_to, end=True)
    if start:
        where.append("m.started_at >= ?")
        params.append(start)
    if end:
        where.append("m.started_at < ?")
        params.append(end)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    if count:
        return db.execute("SELECT COUNT(*) FROM live_matches m " + where_sql, params).fetchone()[0]
    # A que está "Reconectando" em primeiro, depois as ao vivo e as mais novas.
    rows = db.execute(
        "SELECT m.* FROM live_matches m %s ORDER BY (m.status = 'live' AND m.reconnecting_at IS NOT NULL) DESC, "
        "m.status = 'live' DESC, m.started_at DESC LIMIT ?" % where_sql, (*params, limit)).fetchall()
    return [card(db, row) for row in rows]


_TOURNAMENT_LIVE_SQL = (
    "SELECT m.*, tm.group_id, tm.home_entry_id, tm.away_entry_id, hp.username AS home_nick, "
    "ap.username AS away_nick, s.tournament_id FROM live_matches m "
    "JOIN tournament_matches tm ON tm.id = m.tournament_match_id JOIN tournament_stages s ON s.id = tm.stage_id "
    "JOIN tournament_entries he ON he.id = tm.home_entry_id JOIN players hp ON hp.id = he.player_id "
    "JOIN tournament_entries ae ON ae.id = tm.away_entry_id JOIN players ap ON ap.id = ae.player_id ")


def _home_side(row):
    """(lado do mandante, do visitante) - 0 LOCAL, 1 VISITA - pelo que cada
    um controla no jogo, ou None."""
    sides = [{nick.casefold() for nick in json.loads(row[key] or "[]")}
             for key in ("local_players_json", "visita_players_json")]
    home = next((i for i in (0, 1) if row["home_nick"].casefold() in sides[i]), None)
    away = next((i for i in (0, 1) if row["away_nick"].casefold() in sides[i]), None)
    if home is None or away is None or home == away:
        return None
    return home, away


def _home_away(row, goals):
    """(gols do mandante, do visitante) do jogo de campeonato a partir dos gols
    (LOCAL, VISITA) do jogo e de quem controla cada lado, ou None."""
    sides = _home_side(row)
    return (goals[sides[0]], goals[sides[1]]) if sides else None


def tournament_live(db, tournament_id):
    """Jogos do campeonato rolando agora (com sinal), para a tabela: os grupos
    com jogo ao vivo, por inscrição o placar do ponto de vista do time ("1-0",
    ganhando/perdendo/empatando, link para a partida no Ao vivo) e, por jogo,
    o placar (mandante, visitante) que a tabela soma como não confirmado e o
    link da partida no Ao vivo (aba Jogos).
    As simulações só aparecem para o admin, como nos cards."""
    rows = db.execute(
        _TOURNAMENT_LIVE_SQL + "WHERE s.tournament_id = ? AND m.status = 'live' AND m.waiting = 0 AND m.verified = 1 "
        "AND m.updated_at >= ?"
        + ("" if _is_admin() else " AND m.is_test = 0 AND m.hidden = 0"), (tournament_id, _ago(STALE))).fetchall()
    groups, entries, matches, match_urls, live_ids = set(), {}, {}, {}, {}
    for row in rows:
        score = _home_away(row, (row["local_goals"] or 0, row["visita_goals"] or 0))
        if score is None:
            continue
        matches[row["tournament_match_id"]] = score
        link = url_for("live.index", partida=row["id"])
        match_urls[row["tournament_match_id"]] = link
        live_ids[row["tournament_match_id"]] = row["id"]
        for entry, mine, theirs in ((row["home_entry_id"], score[0], score[1]), (row["away_entry_id"], score[1], score[0])):
            entries[entry] = {"score": "%d-%d" % (mine, theirs), "url": link,
                              "result": "win" if mine > theirs else "loss" if mine < theirs else "draw"}
        if row["group_id"]:
            groups.add(row["group_id"])
    return {"groups": groups, "entries": entries, "matches": matches, "match_urls": match_urls, "live_ids": live_ids}


# O 2º PC manda o fim logo depois do 1º: espera isso antes de reportar.
REPORT_SETTLE = timedelta(minutes=1)
GAME_REPORT_NOTE = "Placar lido do jogo pelo RetroArch TIERES (Ao vivo nº %d)."


def report_finished(db):
    """Jogo de campeonato que terminou no RetroArch TIERES (algum PC mandou o
    fim, e os que mandaram concordam): o placar lido vira o reporte do jogo
    (tournament/reports.py report_from_game). Os jogadores confirmam ou
    contestam; sem resposta, confirma sozinho no prazo (torneios_confirmacao_horas,
    24 h, contado do fim do jogo). Simulações nunca."""
    from .tournament import notify, reports, store
    from .tournament.store import TournamentError
    rows = db.execute(_TOURNAMENT_LIVE_SQL + "WHERE m.status = 'finished' AND m.is_test = 0 AND m.verified = 1 "
                      "AND m.game_report IS NULL AND m.finished_at <= ?", (_ago(REPORT_SETTLE),)).fetchall()
    for row in rows:
        finals = {(s["teams"][0]["goals"], s["teams"][1]["goals"], tuple(s.get("pens") or ())) for s in (
            json.loads(r["state_json"]) for r in db.execute(
                "SELECT state_json FROM live_match_reports WHERE live_match_id = ? AND final = 1", (row["id"],)))}
        final_state = next(iter(finals)) if len(finals) == 1 else None
        score = _home_away(row, final_state[:2]) if final_state else None
        pens = (_home_away(row, final_state[2]) if final_state and len(final_state[2]) == 2 else None) or (None, None)
        outcome = ("sem o fim mandado pelos PCs" if not finals else "os PCs discordam do placar" if len(finals) > 1
                   else "não deu para saber quem é o mandante" if score is None else None)
        tournament = store.get_tournament(db, row["tournament_id"])
        if outcome is None:
            # Modo Server: quem ficou segue jogando e o PC dele manda o fim sozinho.
            outcome = _report_game(db, tournament, row, (*score, *pens), GAME_REPORT_NOTE % row["id"])
        db.execute("UPDATE live_matches SET game_report = ? WHERE id = ?", (outcome, row["id"]))
        db.commit()
        if outcome.startswith("reportado"):
            notify.score_from_game(db, tournament, row["tournament_match_id"])
    _report_abandoned(db)
    _report_friendlies(db)


# (O começo "Partida interrompida no RetroArch TIERES" marca, em db.ensure_live_columns,
# os placares de antes da coluna tournament_matches.interrupted.)
ABANDONED_NOTE = ("Partida interrompida no RetroArch TIERES (Ao vivo nº %d): o placar de quando parou vale se "
                  "um dos jogadores confirmar - até lá, dá para reconectar ou jogar de novo.")
MENU_QUIT_NOTE = "Partida abandonada pelo menu do jogo no RetroArch TIERES (Ao vivo nº %d): vale o placar de quando ela parou."


def _report_game(db, tournament, row, score, note, interrupted=False):
    """Reporta o placar lido do jogo de campeonato (reports.report_from_game)
    com quem saiu no meio dele (_game_quitter); devolve a anotação da partida.
    interrupted: o placar de quando a partida parou (sem prazo, um confirma)."""
    from .tournament import reports
    from .tournament.store import TournamentError
    quitter = _game_quitter(db, (row["home_nick"], row["away_nick"]), row)
    left = ("%s saiu da partida pelo menu do jogo" if row["quit_nick"] else "%s saiu no meio do jogo") % quitter
    if quitter:
        note += " %s: perde o triplo de XP se este placar valer." % left
    try:
        reports.report_from_game(db, tournament, row["tournament_match_id"], score,
                                 row["finished_at"] or row["updated_at"], note,
                                 _player_id(db, quitter) if quitter else None, interrupted)
    except TournamentError as error:
        return str(error)[:200]
    return "reportado" + ("; " + left if quitter else "")


def _report_abandoned(db):
    """Jogo de campeonato interrompido (alguém saiu ou caiu: reap, ou partida
    nova dos dois no ingest): o placar de quando parou vira o reporte do jogo,
    mas sem prazo - vale quando UM dos jogadores confirma ou o organizador
    decide (reports.report_from_game interrupted) -, e quem saiu no meio perde
    o triplo de XP se ele valer. Até ser liberado, reconectar (reopen) ou
    jogar de novo o mesmo jogo (detect_context) o retira: vale a partida nova."""
    from .tournament import notify, store
    rows = db.execute(_TOURNAMENT_LIVE_SQL + "WHERE m.status = 'abandoned' AND m.is_test = 0 AND m.verified = 1 "
                      "AND m.game_report IS NULL AND COALESCE(m.finished_at, m.updated_at) <= ?",
                      (_ago(REPORT_SETTLE),)).fetchall()
    for row in rows:
        # Abandonada pelo menu do jogo: vale o placar de quando ela parou, como
        # um jogo terminado (os dois confirmam ou o prazo) - jogar de novo não a substitui.
        newer = [] if row["quit_nick"] else db.execute(
            "SELECT status FROM live_matches WHERE tournament_match_id = ? AND is_test = 0 AND id > ?",
            (row["tournament_match_id"], row["id"])).fetchall()
        if any(n["status"] == "live" for n in newer):
            continue  # jogando de novo
        tournament = store.get_tournament(db, row["tournament_id"])
        score = _home_away(row, (row["local_goals"] or 0, row["visita_goals"] or 0))
        if newer:
            outcome = REPLAYED_OUTCOME
        elif score is None:
            outcome = "não deu para saber quem é o mandante"
        elif row["quit_nick"]:
            outcome = _report_game(db, tournament, row, (*score, None, None), MENU_QUIT_NOTE % row["id"])
        else:
            outcome = _report_game(db, tournament, row, (*score, None, None), ABANDONED_NOTE % row["id"],
                                   interrupted=True)
        db.execute("UPDATE live_matches SET game_report = ? WHERE id = ?", (outcome, row["id"]))
        db.commit()
        if outcome.startswith("reportado"):
            notify.score_from_game(db, tournament, row["tournament_match_id"])


# Sem a transmissão do Stream ao vivo para dizer que a sessão do Kaillera
# acabou, ela acaba depois disso sem um jogo novo entre os dois.
SESSION_GAP = timedelta(minutes=20)


def _session_over(db, nicks, last_finished):
    """A sessão do Kaillera em que os dois jogaram já acabou (um deles saiu)?
    A transmissão do Stream ao vivo dos dois terminou depois do último jogo,
    ou passou SESSION_GAP sem jogo novo."""
    if last_finished <= _ago(SESSION_GAP):
        return True
    for row in db.execute("SELECT player_names FROM live_sessions WHERE status = 'finished' AND ended_at >= ?",
                          (last_finished,)):
        names = {n.strip().casefold() for n in (row["player_names"] or "").split(",")}
        if all(nick.casefold() in names for nick in nicks):
            return True
    return False


def _krec_drops(path):
    """(nick, quadro) de quem saiu da partida, em ordem, no replay (.krec) da
    transmissão: registros 0x14 (ver kaillera-client tools/krec_harness/krec.py).
    O quadro é o número de registros de controles (0x12, um por quadro) antes
    dele - o mesmo "frame" que o fork manda no placar ao vivo (os dois contam
    do início do jogo no Kaillera)."""
    import struct
    try:
        data = Path(path).read_bytes()
    except OSError:
        return []
    pos, drops, frame = (400 if data[:4] == b"KRC1" else 272), [], 0
    while pos < len(data):
        kind = data[pos]
        pos += 1
        try:
            if kind == 0x12:
                (size,) = struct.unpack_from("<h", data, pos)
                if size < 0:
                    break
                pos += 2 + size
                frame += 1
            elif kind == 0x08:
                pos = data.index(b"\0", data.index(b"\0", pos) + 1) + 1
            elif kind == 0x14:
                end = data.index(b"\0", pos)
                drops.append((data[pos:end].decode("latin-1"), frame))
                pos = end + 1 + 4
            else:
                break
        except (ValueError, struct.error):
            break
    return drops


# Interrompido: a saída pode vir até um informe do fork depois do último
# (ele manda a cada mudança e pelo menos a cada 15 s), com folga.
QUIT_MARGIN_FRAMES = 1200


def _game_quitter(db, nicks, game):
    """Quem saiu deste jogo: pelo menu do jogo (quit_nick) ou da sessão do
    Kaillera no meio dele - o primeiro dos dois com saída no replay da
    transmissão do Stream ao vivo (obrigatório com 2+ logados) entre o
    pontapé inicial e o fim - o apito final (no modo Server o jogo segue no
    PC de quem ficou, que manda o fim sozinho) ou, sem fim, o último informe
    dos PCs. None sem a transmissão."""
    from flask import current_app
    wanted = {nick.casefold(): nick for nick in nicks}
    # Saiu pelo menu do jogo (o fork disse quem abriu a pausa).
    if game["quit_nick"] and game["quit_nick"].casefold() in wanted:
        return wanted[game["quit_nick"].casefold()]
    finals = [r["frame"] for r in db.execute(
        "SELECT frame FROM live_match_reports WHERE live_match_id = ? AND final = 1", (game["id"],))]
    if finals:
        until = min(finals) - 1
    else:
        last = db.execute("SELECT MAX(frame) AS frame FROM live_match_reports WHERE live_match_id = ?",
                          (game["id"],)).fetchone()["frame"] or 0
        until = last + QUIT_MARGIN_FRAMES
    for row in db.execute("SELECT player_names, stored_name FROM live_sessions WHERE started_at <= ? "
                          "AND (status = 'live' OR ended_at >= ?) ORDER BY started_at DESC",
                          (game["started_at"], game["updated_at"])):
        names = {n.strip().casefold() for n in (row["player_names"] or "").split(",")}
        if not all(nick in names for nick in wanted):
            continue
        for nick, frame in _krec_drops(Path(current_app.config["LIVE_DIR"]) / row["stored_name"]):
            if nick.casefold() in wanted and game["kickoff_frame"] <= frame <= until:
                return wanted[nick.casefold()]
        return None  # a transmissão desta sessão: ninguém saiu no meio
    return None


def _friendly_numbers(db, friendly_id):
    """{id da partida: nº do jogo no amistoso} - a ordem da lista de partidas
    (friendly_games) e da chave do XP de cada jogo (friendly_games da tabela)."""
    rows = db.execute("SELECT id FROM live_matches WHERE friendly_id = ? AND is_test = 0 AND verified = 1 "
                      "ORDER BY started_at, id", (friendly_id,)).fetchall()
    return {row["id"]: number for number, row in enumerate(rows, 1)}


def _friendly_game_scores(db, nicks, games):
    """O placar de cada partida do amistoso do ponto de vista dele: os
    terminados com o placar final de acordo nos PCs e os interrompidos com o
    de quando pararam. Devolve ({id: (gols do mandante, do visitante, pênaltis
    ou None)}, {id: motivo de não contar})."""
    scores, notes = {}, {}
    for g in games:
        row = dict(g, home_nick=nicks[0], away_nick=nicks[1])
        if g["status"] == "abandoned":
            # Interrompido: vale o placar de quando parou.
            finals = {(g["local_goals"], g["visita_goals"])}
        else:
            finals = {(s["teams"][0]["goals"], s["teams"][1]["goals"]) for s in (
                json.loads(r["state_json"]) for r in db.execute(
                    "SELECT state_json FROM live_match_reports WHERE live_match_id = ? AND final = 1", (g["id"],)))}
        score = _home_away(row, next(iter(finals))) if len(finals) == 1 else None
        if score is None:
            notes[g["id"]] = ("sem o fim mandado pelos PCs" if not finals else "os PCs discordam do placar"
                              if len(finals) > 1 else "não deu para saber quem é o mandante")
            continue
        pens = json.loads(g["state_json"] or "{}").get("pens") if (g["period"] or 0) >= 4 else None
        scores[g["id"]] = (*score, _home_away(row, pens) if pens and len(pens) == 2 else None)
    return scores, notes


def _store_friendly_games(db, friendly_id, games, scores, quit_game_id=None):
    """Grava os jogos que contam (friendly.set_games): cada um vale sozinho no XP."""
    from .tournament import friendly
    numbers = _friendly_numbers(db, friendly_id)
    friendly.set_games(db, friendly_id, [
        {"number": numbers.get(g["id"], index), "live_match_id": g["id"], "home_goals": scores[g["id"]][0],
         "away_goals": scores[g["id"]][1], "home_pens": (scores[g["id"]][2] or (None, None))[0],
         "away_pens": (scores[g["id"]][2] or (None, None))[1], "quit": g["id"] == quit_game_id}
        for index, g in enumerate(games, 1) if g["id"] in scores])


def _report_friendlies(db):
    """Amistoso: as partidas da sessão (cada uma com o seu card) entram juntas
    quando a sessão acaba - um dos jogadores saiu. Contam os jogos terminados
    (placar final de acordo nos PCs) e os interrompidos, com o placar de quando
    pararam: cada um vale sozinho no XP e no V/E/D (friendly_games) e a soma
    vira o placar do amistoso (friendly.report_from_game - mostrada só com
    "Placar agregado"). Quem saiu no meio do último jogo fica marcado e perde
    o triplo de XP nele se o amistoso for confirmado (xp.QUIT_FACTOR). Os dois
    confirmam ou ele vale sozinho no prazo, contado do fim do último jogo."""
    from .tournament import friendly
    from .tournament.store import TournamentError
    for f in db.execute(
            "SELECT f.id, hp.username AS home_nick, ap.username AS away_nick FROM friendly_matches f "
            "JOIN players hp ON hp.id = f.home_id JOIN players ap ON ap.id = f.away_id WHERE f.status = 'accepted' "
            "AND EXISTS (SELECT 1 FROM live_matches m WHERE m.friendly_id = f.id AND m.is_test = 0 AND m.verified = 1 "
            "AND m.game_report IS NULL)").fetchall():
        games = db.execute(
            "SELECT * FROM live_matches WHERE friendly_id = ? AND is_test = 0 AND verified = 1 AND game_report IS NULL "
            "ORDER BY started_at, id", (f["id"],)).fetchall()
        if any(g["status"] == "live" for g in games):
            continue  # ainda jogando
        last = max((g["finished_at"] or g["updated_at"] for g in games), default=None)
        nicks = (f["home_nick"], f["away_nick"])
        if not last or last > _ago(REPORT_SETTLE) or not _session_over(db, nicks, last):
            continue
        # Quem saiu pelo menu do jogo (o último jogo abandonado assim - a revanche
        # não o substitui) ou, sem isso, quem saiu no meio do último jogo
        # (terminado no PC de quem ficou, ou interrompido).
        quit_game = next((g for g in reversed(games) if g["quit_nick"]), games[-1])
        quitter = _game_quitter(db, nicks, quit_game)
        scores, notes = _friendly_game_scores(db, nicks, games)
        total = [sum(score[0] for score in scores.values()), sum(score[1] for score in scores.values())]
        counted = len(scores)
        outcome = "nenhum jogo para somar"
        if counted:
            quitter_id = _player_id(db, quitter) if quitter else None
            try:
                friendly.report_from_game(db, f["id"], total[0], total[1], last, quitter_id)
                _store_friendly_games(db, f["id"], games, scores, quit_game["id"] if quitter_id else None)
                # Sem "Placar agregado": os jogos já confirmados pelos dois durante a sessão fecham o amistoso.
                friendly.settle_games(db, f["id"])
                outcome = "reportado (soma de %d jogo%s)" % (counted, "s" if counted > 1 else "")
                if quitter:
                    outcome += ("; %s saiu de um jogo pelo menu do jogo" if quit_game["quit_nick"]
                                else "; %s saiu no meio do último jogo") % quitter
            except TournamentError as error:
                outcome = str(error)[:200]
        for g in games:
            db.execute("UPDATE live_matches SET game_report = ? WHERE id = ?", (notes.get(g["id"]) or outcome, g["id"]))
        db.commit()


def backfill_friendly_games(db):
    """Amistosos com placar lido do jogo de antes da tabela friendly_games:
    grava os jogos deles (refeitos das partidas, como em _report_friendlies;
    quem saiu, no último). Roda a cada início (db.migrate_db) e só mexe nos
    que ainda não têm jogos gravados."""
    for f in db.execute(
            "SELECT f.id, f.quitter_id, hp.username AS home_nick, ap.username AS away_nick FROM friendly_matches f "
            "JOIN players hp ON hp.id = f.home_id JOIN players ap ON ap.id = f.away_id "
            "WHERE f.game_report = 1 AND NOT EXISTS (SELECT 1 FROM friendly_games fg WHERE fg.friendly_id = f.id) "
            "AND EXISTS (SELECT 1 FROM live_matches m WHERE m.friendly_id = f.id AND m.is_test = 0 "
            "AND m.verified = 1 AND m.status != 'live')").fetchall():
        games = db.execute("SELECT * FROM live_matches WHERE friendly_id = ? AND is_test = 0 AND verified = 1 "
                           "AND status != 'live' ORDER BY started_at, id", (f["id"],)).fetchall()
        scores, _ = _friendly_game_scores(db, (f["home_nick"], f["away_nick"]), games)
        _store_friendly_games(db, f["id"], games, scores, games[-1]["id"] if f["quitter_id"] else None)


# Anotações de _report_friendlies de um jogo que ficou fora da soma.
_NOT_SUMMED = ("sem o fim mandado pelos PCs", "os PCs discordam do placar", "não deu para saber quem é o mandante")


def friendly_games(db, friendly_row):
    """As partidas do amistoso (cada uma com o seu card no Ao vivo), em ordem,
    do ponto de vista dele (mandante x visitante): times, placar (o final, ou
    o de quando parou), pênaltis, situação, se entrou na soma e, nas que
    valem sozinhas no XP (friendly_games, com o placar lido do jogo), a chave
    do XP (xp_key, 'a<id>-<nº>') e se foi a de que quem saiu no meio saiu
    (quit) - para os jogadores conferirem. Devolve (jogos, soma)."""
    from .timeutil import format_local
    from .tournament.friendly import game_confirms
    nicks = {"home_nick": friendly_row["home_name"], "away_nick": friendly_row["away_name"]}
    games, total = [], [0, 0]
    confirms = game_confirms(db, friendly_row["id"])
    rows = db.execute("SELECT * FROM live_matches WHERE friendly_id = ? AND is_test = 0 AND verified = 1 "
                      "ORDER BY started_at, id", (friendly_row["id"],)).fetchall()
    # O placar informado pelos jogadores (game_report = 0) vale como um jogo só: os jogos não têm XP próprio.
    stored = {row["live_match_id"]: row for row in db.execute(
        "SELECT * FROM friendly_games WHERE friendly_id = ?", (friendly_row["id"],))} if friendly_row["game_report"] else {}
    for number, match in enumerate(rows, 1):
        sides = _home_side(dict(match, **nicks))
        state = json.loads(match["state_json"] or "{}")
        teams = state.get("teams") or []
        players = [json.loads(match[key] or "[]") for key in ("local_players_json", "visita_players_json")]
        views = [_team_view(db, match, i, teams[i] if i < len(teams) else {}, players[i]) for i in (0, 1)]
        goals = (match["local_goals"] or 0, match["visita_goals"] or 0)
        pens = state.get("pens") if (match["period"] or 0) >= 4 else None
        note = match["game_report"] or ""
        summed = sides is not None and match["status"] != "live" and not note.startswith(_NOT_SUMMED)
        game = {"number": number, "id": match["id"], "url": url_for("live.index", partida=match["id"]),
                "when": format_local(match["started_at"], "%d/%m/%Y %H:%M"),
                "date": format_local(match["started_at"], "%d/%m/%Y"),
                "time": format_local(match["started_at"], "%H:%M"),
                "status": {"finished": "Encerrado", "abandoned": "Interrompido"}.get(match["status"], "Ao vivo"),
                "summed": summed, "note": note if note.startswith(_NOT_SUMMED) else "",
                "xp_key": None, "quit": False,
                # Sem "Placar agregado": quem já confirmou este jogo (friendly.confirm_game) - e,
                # depois do fim da sessão, quem já confirmou todos (first_confirmed_by; também
                # quem confirmou o placar inteiro pelo botão de antes).
                "confirmed_by": confirms.get(match["id"], set()) | (
                    {friendly_row["first_confirmed_by"]} if friendly_row["status"] == "reported"
                    and friendly_row["first_confirmed_by"] and summed else set()),
                "live": match["status"] == "live"}
        if match["id"] in stored:
            game.update(xp_key="a%d-%d" % (friendly_row["id"], stored[match["id"]]["number"]),
                        quit=bool(stored[match["id"]]["quit"]))
        if sides:
            home, away = sides
            game.update(home_team=views[home]["name"], away_team=views[away]["name"],
                        home_image=views[home]["image"], away_image=views[away]["image"],
                        home_goals=goals[home], away_goals=goals[away],
                        pens=(pens[home], pens[away]) if pens and len(pens) == 2 else None)
            if summed:
                total[0] += goals[home]
                total[1] += goals[away]
        else:
            game.update(home_team=views[0]["name"], away_team=views[1]["name"], home_image=views[0]["image"],
                        away_image=views[1]["image"], home_goals=goals[0],
                        away_goals=goals[1], pens=None)
        games.append(game)
    return games, tuple(total)


# Artilharia e assistências (campeonato; amistoso com mais de um jogo): os gols
# que o fork lê da RAM - [autor, minuto, assistência, "og"/"pen"] por time -
# das partidas que contam. Gol contra não entra (nem quem o fez, que vem no
# lugar da assistência); gol de pênalti conta (e aparece à parte). Os pênaltis
# da disputa não estão na lista de gols. O mesmo jogador do jogo controlado
# por dois jogadores do WE Camp (ex.: os dois de Brasil no amistoso) são dois.
OWN_GOAL_NAME = "Gol contra"


def goal_events(db, match):
    """Os gols da partida para a artilharia, em ordem: quem fez, quem deu a
    assistência, se foi de pênalti e o lado (nicks, time e escudo do card)."""
    state = _match_state(match)
    teams = state.get("teams") or []
    events = []
    for side, team in enumerate(teams[:2]):
        nicks = json.loads(match[("local_players_json", "visita_players_json")[side]] or "[]")
        view = None
        for item in team.get("scorers") or []:
            name = (item[0] or "").strip()
            kind = item[3] if len(item) > 3 else ""
            # Japoneses em Shift-JIS saem sem nome; o fork antigo mandava o gol contra só pelo nome.
            if kind == "og" or not name or name == OWN_GOAL_NAME:
                continue
            view = view or _team_view(db, match, side, team, nicks)
            events.append({"name": name, "assist": ((item[2] if len(item) > 2 else "") or "").strip(),
                           "pen": kind == "pen", "nick": " / ".join(nicks), "team": view["name"],
                           "image": view["image"], "match_id": match["id"]})
    return events


def leaderboard(events):
    """Artilharia e assistências dos gols (goal_events, em ordem dos jogos):
    por jogador do jogo e quem o controlou, com o time do último gol; empate
    no número fica na mesma posição. Os gols de jogo ainda não confirmado
    (pending) somam e ficam contados à parte (pending_goals/pending_assists).
    Devolve {"scorers": [...], "assists": [...]}."""
    tables = ({}, {})
    for e in events:
        for table, name, field in ((tables[0], e["name"], "goals"), (tables[1], e["assist"], "assists")):
            if not name:
                continue
            row = table.setdefault((name.casefold(), e["nick"].casefold()),
                                   {"name": name, "nick": e["nick"], "goals": 0, "assists": 0, "pens": 0,
                                    "pending_goals": 0, "pending_assists": 0, "games": set()})
            row.update(team=e["team"], image=e["image"])
            row[field] += 1
            row["pending_" + field] += int(bool(e.get("pending")))
            row["games"].add(e["match_id"])
            if field == "goals" and e["pen"]:
                row["pens"] += 1

    def ranked(table, field):
        rows = sorted(table.values(), key=lambda r: (-r[field], r["pens"] if field == "goals" else 0,
                                                     len(r["games"]), r["name"].casefold()))
        position, last = 0, None
        for index, row in enumerate(rows, 1):
            if row[field] != last:
                position, last = index, row[field]
            row.update(position=position, games=len(row["games"]))
        return rows
    return {"scorers": ranked(tables[0], "goals"), "assists": ranked(tables[1], "assists")}


def tournament_scorers(db, tournament_id):
    """Artilharia do campeonato: os jogos confirmados cuja partida lida do jogo
    (a mais recente desse jogo, se jogaram de novo) tem o mesmo placar do
    resultado oficial - um placar corrigido à mão não traz gols de outro jogo.
    Os reportados/contestados ainda sem confirmação entram pelo placar
    reportado, com os gols marcados como a confirmar (pending). Simulações
    nunca. Devolve o leaderboard com "games" (confirmados que contaram) e
    "pending_games"."""
    rows = db.execute(
        "SELECT m.*, tm.status AS tm_status, tm.home_goals AS official_home, tm.away_goals AS official_away, "
        "(SELECT r.home_goals FROM match_reports r WHERE r.match_id = tm.id AND r.action = 'report' "
        "ORDER BY r.id DESC LIMIT 1) AS reported_home, "
        "(SELECT r.away_goals FROM match_reports r WHERE r.match_id = tm.id AND r.action = 'report' "
        "ORDER BY r.id DESC LIMIT 1) AS reported_away, hp.username AS home_nick, "
        "ap.username AS away_nick FROM live_matches m JOIN tournament_matches tm ON tm.id = m.tournament_match_id "
        "JOIN tournament_stages s ON s.id = tm.stage_id "
        "JOIN tournament_entries he ON he.id = tm.home_entry_id JOIN players hp ON hp.id = he.player_id "
        "JOIN tournament_entries ae ON ae.id = tm.away_entry_id JOIN players ap ON ap.id = ae.player_id "
        "WHERE s.tournament_id = ? AND tm.status IN ('confirmed', 'reported', 'disputed') AND m.is_test = 0 "
        "AND m.verified = 1 AND m.status != 'live' ORDER BY m.started_at DESC, m.id DESC", (tournament_id,)).fetchall()
    seen, counted = set(), []
    for row in rows:
        if row["tournament_match_id"] in seen:
            continue
        confirmed = row["tm_status"] == "confirmed"
        score = ((row["official_home"], row["official_away"]) if confirmed
                 else (row["reported_home"], row["reported_away"]))
        if _home_away(row, (row["local_goals"] or 0, row["visita_goals"] or 0)) == score:
            seen.add(row["tournament_match_id"])
            counted.append((row, not confirmed))
    counted.reverse()
    board = leaderboard([dict(e, pending=pending) for row, pending in counted for e in goal_events(db, row)])
    board["pending_games"] = sum(1 for _, pending in counted if pending)
    board["games"] = len(counted) - board["pending_games"]
    return board


def stage_cards(db, stage_id):
    """Cartões de cada inscrição nos jogos confirmados de uma fase - o
    desempate da classificação (engine.standings). Como na artilharia, vale a
    partida lida do jogo (a mais recente, se jogaram de novo) com o placar
    oficial; jogo sem ela (placar lançado à mão, PC sem o RetroArch TIERES que
    manda os cartões) não soma. Simulações nunca.
    Devolve {inscrição: {"yellow": amarelos, "red": expulsões, "points": pontos}}."""
    from .tournament.engine import card_points
    rows = db.execute(
        "SELECT m.*, tm.home_entry_id, tm.away_entry_id, tm.home_goals AS official_home, "
        "tm.away_goals AS official_away, hp.username AS home_nick, ap.username AS away_nick FROM live_matches m "
        "JOIN tournament_matches tm ON tm.id = m.tournament_match_id "
        "JOIN tournament_entries he ON he.id = tm.home_entry_id JOIN players hp ON hp.id = he.player_id "
        "JOIN tournament_entries ae ON ae.id = tm.away_entry_id JOIN players ap ON ap.id = ae.player_id "
        "WHERE tm.stage_id = ? AND tm.status = 'confirmed' AND m.is_test = 0 AND m.verified = 1 "
        "AND m.status != 'live' ORDER BY m.started_at DESC, m.id DESC", (stage_id,)).fetchall()
    seen, totals = set(), {}
    for row in rows:
        if row["tournament_match_id"] in seen:
            continue
        sides = _home_side(row)
        if not sides or _home_away(row, (row["local_goals"] or 0, row["visita_goals"] or 0)) != (
                row["official_home"], row["official_away"]):
            continue
        seen.add(row["tournament_match_id"])
        teams = _match_state(row).get("teams") or []
        for entry, side in ((row["home_entry_id"], sides[0]), (row["away_entry_id"], sides[1])):
            kinds = [item[2] for item in (teams[side].get("cards") or []) if len(item) > 2] if side < len(teams) else []
            total = totals.setdefault(entry, {"yellow": 0, "red": 0, "points": 0})
            total["yellow"] += sum(kind in ("y", "y2") for kind in kinds)
            total["red"] += sum(kind in ("y2", "r") for kind in kinds)
            total["points"] += card_points(kinds)
    return totals


def match_goals(db, match, score, live_id=None):
    """A partida do Ao vivo do jogo de campeonato (página do jogo), do ponto de
    vista dele: {"home": [gols], "away": [gols], "url": card no Ao vivo,
    "live": rolando agora}. A que está rolando (live_id, de tournament_live)
    ou a lida do jogo mais recente com o placar `score` (mandante, visitante:
    o confirmado ou o reportado), como na artilharia. None sem ela."""
    if live_id is None and (not score or score[0] is None):
        return None
    rows = db.execute(
        "SELECT m.*, hp.username AS home_nick, ap.username AS away_nick FROM live_matches m "
        "JOIN tournament_entries he ON he.id = ? JOIN players hp ON hp.id = he.player_id "
        "JOIN tournament_entries ae ON ae.id = ? JOIN players ap ON ap.id = ae.player_id "
        "WHERE m.tournament_match_id = ? AND m.is_test = 0 AND m.verified = 1 "
        + ("AND m.id = ? " if live_id is not None else "AND m.status != 'live' ")
        + "ORDER BY m.started_at DESC, m.id DESC",
        (match["home_entry_id"], match["away_entry_id"], match["id"], *([live_id] if live_id is not None else []))
    ).fetchall()
    for row in rows:
        sides = _home_side(row)
        if sides and (live_id is not None
                      or _home_away(row, (row["local_goals"] or 0, row["visita_goals"] or 0)) == tuple(score)):
            teams = _match_state(row).get("teams") or [{}, {}]
            goals = [(teams[side].get("scorers") or []) if side < len(teams) else [] for side in sides]
            # Card ocultado pelo administrador: os gols ficam, sem o botão do card.
            url = url_for("live.index", partida=row["id"]) if _is_admin() or not row["hidden"] else None
            return {"home": goals[0], "away": goals[1], "url": url, "live": row["status"] == "live"}
    return None


def friendly_scorers(db, friendly_row, games):
    """Artilharia do amistoso (verso do card): os jogos que entram na soma
    (friendly_games: summed). None sem nenhum."""
    ids = [g["id"] for g in games if g["summed"]]
    if not ids:
        return None
    rows = db.execute("SELECT * FROM live_matches WHERE id IN (%s) ORDER BY started_at, id" % ",".join("?" * len(ids)),
                      ids).fetchall()
    # Amistoso ainda não confirmado: todos os gols dele estão a confirmar.
    pending = friendly_row["status"] != "confirmed"
    board = leaderboard([dict(e, pending=pending) for row in rows for e in goal_events(db, row)])
    board["games"], board["pending_games"] = (0, len(rows)) if pending else (len(rows), 0)
    return board


def _filters():
    """Filtros da página. Campeonato (autocompletar): Rankeado, Amistoso ou
    Partida livre (stats.kind_filter), o nome de um campeonato ou o id dele
    (o link "Ao vivo" da tabela do campeonato); interrompidas=1 = só as
    partidas interrompidas."""
    from .tournament.stats import kind_filter
    args = request.args
    championship = args.get("campeonato", "").strip()[:120]
    kind = kind_filter(championship)
    return {"player": args.get("jogador", "").strip()[:32], "kind": kind,
            "tournament_id": int(championship) if championship.isdigit() else None,
            "tournament_query": championship if championship and not kind and not championship.isdigit() else "",
            "match_id": int(args["partida"]) if args.get("partida", "").isdigit() else None,
            "interrupted": args.get("interrompidas") == "1",
            "date_from": args.get("de", ""), "date_to": args.get("ate", ""),
            "iso_query": args.get("iso", "").strip()[:120]}


@bp.get("/ao-vivo")
def index():
    db = get_db()
    filters = _filters()
    tournaments = db.execute(_LIVE_TOURNAMENTS).fetchall()
    # O que aparece no campo Campeonato: o texto digitado ou, pelo id (link da tabela), o nome.
    from .tournament.stats import KIND_FILTERS
    championship = request.args.get("campeonato", "").strip()[:120]
    if filters["tournament_id"]:
        championship = next((t["name"] for t in tournaments if t["id"] == filters["tournament_id"]), championship)
    # Sugestões do filtro "Jogador": quem já mandou placar logado (aparece nos cards).
    players = db.execute("SELECT DISTINCT p.id, p.username FROM live_match_reports r JOIN players p ON p.id = r.player_id "
                         "ORDER BY p.username COLLATE NOCASE").fetchall()
    # Quantas: 20, 50 ou 100 (o "1–3 de [20 ▾]" do carrossel - app/listing.py).
    from .listing import page_size
    limit = page_size()
    return render_template("live/index.html", cards=list_cards(db, limit=limit, **filters),
                           total=list_cards(db, count=True, **filters), filters=filters, players=players,
                           tournaments=tournaments, is_admin=_is_admin(), limit=limit, championship=championship,
                           kind_options=[label for label, _ in KIND_FILTERS], iso_options=iso_options(db), no_iso=NO_ISO,
                           cards_url=url_for("live.cards_fragment", **dict(request.args.to_dict(), mostrar=limit)))


@bp.get("/ao-vivo/cards")
def cards_fragment():
    """Só os cards (as páginas atualizam com isto a cada poucos segundos): a
    inicial pede so_ao_vivo=1, a do campeonato as recentes dele (recentes=1)."""
    from .listing import page_size
    recent = request.args.get("recentes") == "1"
    # A página Ao vivo manda o "mostrar" dela (cards_url): a recarga traz a mesma quantidade.
    cards = list_cards(get_db(), only_live=request.args.get("so_ao_vivo") == "1", recent_only=recent,
                       limit=4 if recent else (page_size() if request.args.get("mostrar") else CARDS_LIMIT),
                       **_filters())
    return render_template("live/_cards.html", cards=cards)


@bp.post("/ao-vivo/<int:live_id>/ocultar")
@login_required
def hide(live_id):
    """Administrador: oculta o card (ocultar=1) do Ao vivo e do placar ao vivo
    da tabela para jogadores e visitantes, ou volta a mostrar (ocultar=0). Só
    a visualização: o placar e os resultados da partida não mudam. Mostrar o
    card que os jogadores excluíram desfaz a exclusão (o placar retirado do
    jogo de campeonato não volta)."""
    validate_csrf()
    db = get_db()
    hidden = request.form.get("ocultar") == "1"
    if db.execute("UPDATE live_matches SET hidden = ?, deleted_at = CASE WHEN ? THEN deleted_at END WHERE id = ?",
                  (int(hidden), int(hidden), live_id)).rowcount:
        if not hidden:
            db.execute("DELETE FROM live_match_deletions WHERE live_match_id = ?", (live_id,))
        db.commit()
        flash("Card ocultado: jogadores e visitantes não o veem mais." if hidden
              else "Card visível de novo para todos.", "success")
    return _redirect_back()


def _redirect_back():
    """De volta à página do card: "voltar" do formulário (meus-jogos = Meus
    jogos) ou, sem ele (o card recarregado sozinho pela página não traz), a de
    onde veio - só deste site; senão, o Ao vivo."""
    target = request.form.get("voltar") or ""
    if target == "meus-jogos":
        return redirect(url_for("tournaments.my_matches"))
    if not target and request.referrer:
        from urllib.parse import urlsplit
        ref = urlsplit(request.referrer)
        target = (ref.path + ("?" + ref.query if ref.query else "")) if ref.netloc == request.host else ""
    return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("live.index"))


@bp.post("/ao-vivo/<int:live_id>/excluir")
def delete_card(live_id):
    """Um jogador exclui o card da partida interrompida (undeletable): o
    primeiro avisa os outros por e-mail (com o link do card); com todos, o
    card sai do site e a partida não vale mais (_delete_card)."""
    from .tournament import notify
    if "player_id" not in session:
        flash("Entre na sua conta para excluir o card.", "error")
        return redirect(url_for("players.login"))
    validate_csrf()
    db = get_db()
    match = db.execute("SELECT * FROM live_matches WHERE id = ?", (live_id,)).fetchone()
    if match is None:
        abort(404)
    me = session["player_id"]
    people = _card_players(db, match) or []
    reason = undeletable(db, match)
    if not reason and all(pid != me for pid, _ in people):
        reason = "Só quem jogou a partida pode excluir o card."
    if reason:
        flash(reason, "error")
        return _redirect_back()
    new = db.execute("INSERT OR IGNORE INTO live_match_deletions (live_match_id, player_id, created_at) "
                     "VALUES (?, ?, ?)", (live_id, me, now())).rowcount
    done = deletion_requests(db, live_id)
    waiting = [(pid, name) for pid, name in people if pid not in done]
    if not waiting:
        _delete_card(db, match)
        db.commit()
        flash("Card excluído: ele saiu do site.", "success")
        return _redirect_back()
    db.commit()
    if new:
        info = card(db, match)
        # "Time (jogador)"; sem o time identificado, só o jogador.
        sides = [(view["name"] if view["known"] else "", ", ".join(p["name"] for p in view["people"]))
                 for view in info["teams"]]
        label = " x ".join("%s (%s)" % side if side[0] else side[1] for side in sides)
        notify.live_card_deleted(db, match["id"], dict(people)[me], [pid for pid, _ in waiting], label,
                                 info["when"], len(people), _pending_score(db, match))
    flash("Você excluiu o card: ele sai do site quando %s excluir também. Avisamos por e-mail."
          % _names_text(name for _, name in waiting), "success")
    return _redirect_back()


@bp.post("/ao-vivo/limpar-testes")
@login_required
def clear_tests():
    validate_csrf()
    db = get_db()
    count = db.execute("DELETE FROM live_matches WHERE is_test = 1").rowcount
    db.commit()
    flash("%d partida(s) de teste apagada(s)." % count, "success")
    return redirect(url_for("live.index"))


# --- Admin: times, jogo base e campeonato de cada ISO ----------------------------

def _save_slot(db, content_id, slot, name):
    """Nome de um slot: um time da base (com escudo/bandeira) ou um nome livre; vazio apaga."""
    team = db.execute("SELECT id, name FROM teams WHERE name = ? COLLATE NOCASE", (name,)).fetchone() if name else None
    db.execute("UPDATE iso_team_slots SET name = ?, team_id = ?, source = ?, updated_at = ? "
               "WHERE content_id = ? AND slot = ?",
               (team["name"] if team else name, team["id"] if team else None, "admin" if name else "", now(),
                content_id, slot))


@bp.route("/admin/ao-vivo/times", methods=("GET", "POST"))
@login_required
def admin_slots():
    db = get_db()
    if request.method == "POST":
        validate_csrf()
        content_id, action = request.form.get("content_id", ""), request.form.get("acao", "slot")
        if action == "perfil":
            game_id = request.form.get("game_title_id", type=int) or None
            tournament_id = request.form.get("tournament_id", type=int) or None
            catalog_id = request.form.get("catalog_iso_id", type=int) or None
            db.execute("INSERT OR IGNORE INTO iso_profiles (content_id, updated_at) VALUES (?, ?)", (content_id, now()))
            db.execute("UPDATE iso_profiles SET game_title_id = ?, tournament_id = ?, catalog_iso_id = ?, source = ?, "
                       "updated_at = ? WHERE content_id = ?",
                       (game_id, tournament_id, catalog_id, "admin" if game_id or tournament_id or catalog_id else "",
                        now(), content_id))
            flash("Jogo base e campeonato da ISO salvos.", "success")
        elif action == "nomes_do_jogo":
            rows = db.execute("SELECT slot, ram_name FROM iso_team_slots WHERE content_id = ? AND ram_name != '' "
                              "AND name = '' AND team_id IS NULL", (content_id,)).fetchall()
            for row in rows:
                _save_slot(db, content_id, row["slot"], row["ram_name"].title())
            flash("%d slot(s) com o nome do jogo." % len(rows), "success")
        else:
            _save_slot(db, content_id, request.form.get("slot", type=int), request.form.get("name", "").strip()[:40])
            flash("Time salvo.", "success")
        db.commit()
        return redirect(url_for("live.admin_slots"))
    rows = db.execute("SELECT s.*, t.name AS team_name FROM iso_team_slots s LEFT JOIN teams t ON t.id = s.team_id "
                      "ORDER BY s.room, s.content_id, s.slot").fetchall()
    groups = {}
    for row in rows:
        groups.setdefault((row["content_id"], re.sub(r"^PCSX ReARMed:\s*", "", row["room"] or "")), []).append(row)
    profiles = {row["content_id"]: row for row in db.execute("SELECT * FROM iso_profiles")}
    teams = [r["name"] for r in db.execute("SELECT DISTINCT name FROM teams WHERE is_active = 1 ORDER BY name")]
    games = db.execute("SELECT id, name FROM game_titles WHERE is_active = 1 ORDER BY year, name").fetchall()
    tournaments = db.execute("SELECT id, name FROM tournaments WHERE status != 'cancelled' "
                             "ORDER BY name COLLATE NOCASE").fetchall()
    catalog = db.execute("SELECT id, name FROM catalog_isos WHERE is_active = 1 ORDER BY name COLLATE NOCASE").fetchall()
    # Qual ISO cadastrada tem cada content_id visto (a do campeonato ou a do cadastro de amistosos).
    known = {}
    for row in db.execute("SELECT i.content_id, t.name FROM tournament_isos i JOIN tournaments t ON t.id = i.tournament_id "
                          "WHERE i.content_id IS NOT NULL"):
        known.setdefault(row["content_id"].upper(), []).append("ISO do campeonato " + row["name"])
    for row in db.execute("SELECT content_id, name FROM catalog_isos WHERE content_id IS NOT NULL"):
        known.setdefault(row["content_id"].upper(), []).append("ISO de amistoso " + row["name"])
    unidentified = db.execute("SELECT (SELECT COUNT(*) FROM tournament_isos WHERE content_id IS NULL) + "
                              "(SELECT COUNT(*) FROM catalog_isos WHERE content_id IS NULL "
                              "AND (file_id IS NOT NULL OR original_filename IS NOT NULL))").fetchone()[0]
    # Filtro pelo nome do jogo/ISO ou identificador, e no máximo page_size() ISOs na tela.
    from .listing import cap
    search = request.args.get("iso", "").strip().lower()
    if search:
        groups = {key: rows for key, rows in groups.items()
                  if search in key[0].lower() or search in key[1].lower()
                  or any(search in name.lower() for name in known.get(key[0].upper(), []))}
    shown, total = cap(groups.items())
    groups = dict(shown)
    return render_template("live/admin_slots.html", groups=groups, total=total, search=search,
                           teams=teams, profiles=profiles, games=games,
                           tournaments=tournaments, catalog=catalog, known=known, unidentified=unidentified)


@bp.post("/admin/ao-vivo/identificar-isos")
@login_required
def identify_isos():
    """CRC32:TAMANHO das ISOs de campeonato/amistoso que ainda não têm (o
    mesmo botão de "Jogos do Memory Card online" - app/admin.py)."""
    from flask import current_app
    import threading
    from .admin import _identify_isos, _identify_isos_lock
    validate_csrf()
    app = current_app._get_current_object()
    if app.config.get("TESTING"):
        found, missing = _identify_isos(app) or (0, [])
        flash("%d ISO(s) identificada(s).%s" % (found, " Sem identificação: " + ", ".join(missing) if missing else ""),
              "success" if not missing else "error")
    elif _identify_isos_lock.locked():
        flash("A identificação das ISOs já está em andamento.", "error")
    else:
        threading.Thread(target=_identify_isos, args=(app,), daemon=True).start()
        flash("Identificando as ISOs (pelo archive.org ou pela cópia deste servidor). Recarregue em alguns segundos.",
              "success")
    return redirect(url_for("live.admin_slots"))


@bp.app_template_filter("scorer")
def scorer_line(item):
    name, minute, assist = player_label(item[0]), item[1], player_label(item[2] if len(item) > 2 else "")
    kind = item[3] if len(item) > 3 else ""
    if kind == "og":  # o autor do gol contra (no lugar da assistência), entre parênteses
        return Markup("Gol contra {}' <small>({})</small>").format(minute, assist or name)
    if kind == "pen":
        return Markup("{} {}' <small>(Pênalti)</small>").format(name, minute)
    # A assistência entre parênteses, só o nome: "J.Alvarez 1' (L.Messi)".
    return Markup("{} {}'{}").format(name, minute, Markup(' <small>({})</small>').format(assist) if assist else "")


@bp.app_template_filter("event")
def event_line(item):
    """Cartão ou contusão do card ([nome, minuto ou [minutos], tipo] - _team_view):
    o grupo (o ícone no meio) já diz o tipo."""
    minutes = item[1] if isinstance(item[1], list) else [item[1]]
    return Markup("{} {}").format(player_label(item[0]) or "?",
                                  Markup(", ").join(Markup("{}'").format(m) for m in minutes))


_SHIRT = re.compile(r"^#(\d{1,2})$")


@bp.app_template_filter("player")
def player_label(name):
    """O jogador como o card mostra: o fork manda "#8" quando não consegue ler o
    nome (os japoneses do WE3, em Shift-JIS) - aqui vira "Número 8"."""
    match = _SHIRT.match(name or "")
    return "Número %s" % match.group(1) if match else (name or "")


def _by_player(cards):
    """Os cartões de cada jogador numa linha só, na ordem do primeiro: [nome, [minutos], tipo]."""
    lines = {}
    for name, minute, kind in cards:
        lines.setdefault(name, [name, [], kind])[1].append(minute)
    return list(lines.values())
