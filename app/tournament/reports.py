"""Resultado informado pelos próprios jogadores (fase 2).

  scheduled --reportar--> reported --confirmar--> confirmed
                             |  +--organizador libera novo reporte--> scheduled
                             |  +--sem resposta em N horas--> confirmed (automático)
                             +--contestar--> disputed --organizador/admin decide--> confirmed
                                                |  +--quem reportou aceita o placar da contestação--> confirmed

O placar só vale (tabela, chave, W$) quando confirmado - store.record_result
continua sendo quem grava, paga e avança o mata-mata. O placar proposto fica
em match_reports, que também é o histórico do jogo.

O organizador que joga o próprio torneio é tratado como jogador nos jogos
dele: reporta e confirma como qualquer um, e a contestação de um jogo dele
só o Admin WE Camp decide.
"""
from datetime import datetime, timedelta, timezone

from ..db import now
from ..wallet import setting
from . import store
from .store import TournamentError

ACTION_LABELS = {
    "report": "reportou", "confirm": "confirmou", "dispute": "contestou", "resolve": "definiu o resultado",
    "auto_confirm": "confirmado automaticamente", "clear": "apagou o resultado",
}


def log(db, match_id, action, entry=None, user_id=None, score=(None, None, None, None), comment=""):
    db.execute(
        "INSERT INTO match_reports (match_id, action, entry_id, player_id, user_id, home_goals, away_goals, "
        "home_pens, away_pens, comment, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (match_id, action, entry["id"] if entry else None, entry["player_id"] if entry else None, user_id,
         *score, (comment or "").strip()[:500], now()))


def history(db, match_id):
    return db.execute(
        "SELECT r.*, p.username, u.username AS admin_name FROM match_reports r "
        "LEFT JOIN players p ON p.id = r.player_id LEFT JOIN users u ON u.id = r.user_id "
        "WHERE r.match_id = ? ORDER BY r.id", (match_id,)).fetchall()


def last_report(db, match_id):
    return db.execute("SELECT * FROM match_reports WHERE match_id = ? AND action = 'report' ORDER BY id DESC LIMIT 1",
                      (match_id,)).fetchone()


def _match(db, tournament, match_id):
    match = db.execute(
        "SELECT m.*, s.status AS stage_status, s.tournament_id FROM tournament_matches m "
        "JOIN tournament_stages s ON s.id = m.stage_id WHERE m.id = ?", (match_id,)).fetchone()
    if match is None or match["tournament_id"] != tournament["id"]:
        raise TournamentError("Jogo não encontrado.")
    if tournament["status"] != "running" or match["stage_status"] != "running":
        raise TournamentError("Este jogo não está mais aberto para resultados.")
    return match


def player_entry(db, tournament, match, player_id):
    """A inscrição do jogador neste jogo, ou None se ele não joga."""
    entry = db.execute("SELECT * FROM tournament_entries WHERE tournament_id = ? AND player_id = ?",
                       (tournament["id"], player_id)).fetchone()
    if entry and entry["id"] in (match["home_entry_id"], match["away_entry_id"]):
        return entry
    return None


def _check_score(db, tournament, match_id, score):
    """Testa o placar como se fosse confirmado (pênaltis no jogo decisivo,
    fase seguinte já jogada...) e desfaz tudo."""
    db.execute("SAVEPOINT check_score")
    try:
        store.record_result(db, tournament, match_id, *score)
    finally:
        db.execute("ROLLBACK TO check_score")
        db.execute("RELEASE check_score")


def report(db, tournament, match_id, player_id, score, comment=""):
    match = _match(db, tournament, match_id)
    entry = player_entry(db, tournament, match, player_id)
    if entry is None:
        raise TournamentError("Só os jogadores deste jogo podem reportar o resultado.")
    if match["status"] == "confirmed":
        raise TournamentError("Este jogo já tem resultado confirmado. Se houver erro, fale com o organizador.")
    if match["status"] == "disputed":
        raise TournamentError("O resultado deste jogo foi contestado e está com o organizador.")
    if match["status"] == "reported" and match["reported_by_entry_id"] != entry["id"]:
        raise TournamentError("O adversário já reportou o placar: confirme ou conteste.")
    if match["status"] == "reported":
        # Placar enviado não muda: só o organizador corrige ou libera um novo reporte.
        raise TournamentError("Você já reportou o placar deste jogo. Para mudar, fale com o organizador: ele pode "
                              "corrigir o resultado ou liberar um novo reporte.")
    _check_score(db, tournament, match_id, score)
    db.execute("UPDATE tournament_matches SET status = 'reported', reported_by_entry_id = ?, reported_at = ?, "
               "updated_at = ? WHERE id = ?", (entry["id"], now(), now(), match_id))
    log(db, match_id, "report", entry, score=score, comment=comment)


def confirm(db, tournament, match_id, player_id):
    match = _match(db, tournament, match_id)
    entry = player_entry(db, tournament, match, player_id)
    if entry is None or match["status"] != "reported" or match["reported_by_entry_id"] == entry["id"]:
        raise TournamentError("Não há placar do adversário para você confirmar neste jogo.")
    reported = last_report(db, match_id)
    _confirm_reported(db, tournament, match, reported)
    log(db, match_id, "confirm", entry)


def _confirm_reported(db, tournament, match, reported):
    store.record_result(db, tournament, match["id"], reported["home_goals"], reported["away_goals"],
                        reported["home_pens"], reported["away_pens"])


def dispute(db, tournament, match_id, player_id, comment, score=None):
    match = _match(db, tournament, match_id)
    entry = player_entry(db, tournament, match, player_id)
    if entry is None or match["status"] != "reported" or match["reported_by_entry_id"] == entry["id"]:
        raise TournamentError("Não há placar do adversário para você contestar neste jogo.")
    if len((comment or "").strip()) < 3:
        raise TournamentError("Explique o motivo da contestação.")
    db.execute("UPDATE tournament_matches SET status = 'disputed', updated_at = ? WHERE id = ?", (now(), match_id))
    log(db, match_id, "dispute", entry, score=score or (None, None, None, None), comment=comment)


def last_dispute(db, match_id):
    """Última contestação de um jogo que traz o placar que o adversário diz ser o certo."""
    return db.execute("SELECT * FROM match_reports WHERE match_id = ? AND action = 'dispute' "
                      "AND home_goals IS NOT NULL ORDER BY id DESC LIMIT 1", (match_id,)).fetchone()


def accept_dispute(db, tournament, match_id, player_id):
    """Quem reportou aceita o placar da contestação: o jogo é confirmado sem
    precisar do organizador."""
    match = _match(db, tournament, match_id)
    entry = player_entry(db, tournament, match, player_id)
    disputed = last_dispute(db, match_id) if match["status"] == "disputed" else None
    if entry is None or disputed is None or match["reported_by_entry_id"] != entry["id"]:
        raise TournamentError("Não há placar de contestação para você aceitar neste jogo.")
    score = (disputed["home_goals"], disputed["away_goals"], disputed["home_pens"], disputed["away_pens"])
    store.record_result(db, tournament, match_id, *score)
    log(db, match_id, "confirm", entry, score=score, comment="Aceitou o placar da contestação.")


def resolve(db, tournament, match_id, score, user_id=None, entry=None, comment=""):
    """Organizador/admin define (ou corrige) o placar."""
    store.record_result(db, tournament, match_id, *score)
    db.execute("UPDATE tournament_matches SET reported_by_entry_id = NULL, reported_at = NULL WHERE id = ?",
               (match_id,))
    log(db, match_id, "resolve", entry, user_id=user_id, score=score, comment=comment)


def clear(db, tournament, match_id, user_id=None, entry=None):
    """Apaga o placar confirmado (estornando o W$) ou descarta um reporte/contestação."""
    match = _match(db, tournament, match_id)
    if match["status"] == "confirmed":
        store.clear_result(db, tournament, match_id)
    elif match["status"] in ("reported", "disputed"):
        db.execute("UPDATE tournament_matches SET status = 'scheduled', updated_at = ? WHERE id = ?",
                   (now(), match_id))
    else:
        raise TournamentError("Este jogo não tem resultado.")
    db.execute("UPDATE tournament_matches SET reported_by_entry_id = NULL, reported_at = NULL WHERE id = ?",
               (match_id,))
    log(db, match_id, "clear", entry, user_id=user_id)


def deadline(db, match):
    if not match["reported_at"]:
        return None
    reported = datetime.fromisoformat(match["reported_at"])
    return (reported + timedelta(hours=setting(db, "torneios_confirmacao_horas"))).replace(microsecond=0).isoformat()


def auto_confirm(db, at=None, disputed=None):
    """Confirma os placares reportados sem resposta no prazo. Cada jogo em
    sua própria transação; um placar que não dá mais para confirmar (ex. a
    fase seguinte já andou) vira contestação, para o organizador ver (e entra
    em `disputed` como (torneio, jogo), para o aviso por e-mail)."""
    at = at or datetime.now(timezone.utc)
    limit = (at - timedelta(hours=setting(db, "torneios_confirmacao_horas"))).replace(microsecond=0).isoformat()
    rows = db.execute(
        "SELECT m.id, s.tournament_id FROM tournament_matches m JOIN tournament_stages s ON s.id = m.stage_id "
        "WHERE m.status = 'reported' AND m.reported_at <= ?", (limit,)).fetchall()
    done = 0
    for row in rows:
        tournament = store.get_tournament(db, row["tournament_id"])
        try:
            match = _match(db, tournament, row["id"])
            _confirm_reported(db, tournament, match, last_report(db, row["id"]))
            log(db, row["id"], "auto_confirm")
            db.commit()
            done += 1
        except TournamentError as error:
            db.rollback()
            db.execute("UPDATE tournament_matches SET status = 'disputed', updated_at = ? WHERE id = ?",
                       (now(), row["id"]))
            log(db, row["id"], "dispute", comment=f"Não foi possível confirmar automaticamente: {error}")
            db.commit()
            if disputed is not None:
                disputed.append((row["tournament_id"], row["id"]))
    return done


def matches_for_player(db, player_id):
    """Todos os jogos do jogador nos torneios em andamento e encerrados (os
    em andamento primeiro), na ordem da tabela."""
    return db.execute(
        "SELECT m.*, t.id AS tournament_id, t.slug, t.name AS tournament_name, t.status AS tournament_status, "
        "s.name AS stage_name, s.status AS stage_status, e.id AS my_entry_id, o.username AS organizer_name "
        "FROM tournament_entries e JOIN tournaments t ON t.id = e.tournament_id "
        "JOIN players o ON o.id = t.organizer_id "
        "JOIN tournament_stages s ON s.tournament_id = t.id "
        "JOIN tournament_matches m ON m.stage_id = s.id AND (m.home_entry_id = e.id OR m.away_entry_id = e.id) "
        "WHERE e.player_id = ? AND t.status IN ('running', 'finished') "
        "ORDER BY t.status = 'finished', t.id DESC, s.seq, m.round, m.leg, m.id", (player_id,)).fetchall()
