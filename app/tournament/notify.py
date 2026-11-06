"""E-mails dos torneios e o encerramento automático.

  - início do torneio: cada inscrito recebe os próprios jogos da fase e o link de "Meus jogos";
  - placar reportado: o adversário (quem precisa aprovar) recebe o placar e o link do jogo;
  - placar contestado: os dois jogadores e o organizador recebem os dois placares e o motivo
    (se o organizador joga a partida, quem decide e recebe é a administração do WE Camp -
    e-mails em site_settings, editados em Torneios: administração);
  - fase com próxima (grupos, pontos corridos, liga suíça) terminada: o organizador é
    avisado para gerar a próxima fase; gerada, os classificados recebem os jogos dela;
  - último jogo confirmado: o organizador é avisado para encerrar; sem isso, o torneio
    encerra sozinho (e paga o prêmio) AUTO_FINISH_HOURS depois (tournaments.all_played_at).

As mensagens são montadas na requisição (precisam de url_for) e enviadas numa
thread, para o SMTP não atrasar a página; falha de envio só vai para o log.
Chame as funções depois do commit da ação.
"""
import re
import threading
from datetime import datetime, timedelta, timezone

from flask import current_app

from ..accounts import external_url
from ..db import now
from ..mailer import MailError, send_mail
from ..timeutil import format_local
from . import reports, store
from .store import TournamentError

AUTO_FINISH_HOURS = 24
SIGNATURE = "\n--\nWE Camp - Torneios\n"
ADMIN_EMAIL_SETTING = "torneios_admin_email"
_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def parse_emails(text):
    """Lista de e-mails separados por vírgula/ponto e vírgula/espaço; None se algum for inválido."""
    emails = [e for e in re.split(r"[,;\s]+", text or "") if e]
    return emails if all(_EMAIL.match(e) for e in emails) else None


def admin_emails(db):
    row = db.execute("SELECT value FROM site_settings WHERE key = ?", (ADMIN_EMAIL_SETTING,)).fetchone()
    return parse_emails(row["value"]) or [] if row else []


def _deliver(messages):
    messages = [m for m in messages if m[0]]
    if not messages:
        return
    app = current_app._get_current_object()

    def work():
        with app.app_context():
            for to, subject, body in messages:
                try:
                    send_mail(to, subject, body + SIGNATURE)
                except MailError:
                    pass  # send_mail já registrou no log

    if app.config.get("TESTING"):
        work()
    else:
        threading.Thread(target=work, daemon=True).start()


def _entry(db, entry_id):
    return db.execute("SELECT e.*, p.username, p.email FROM tournament_entries e JOIN players p ON p.id = e.player_id "
                      "WHERE e.id = ?", (entry_id,)).fetchone()


def _label(entry):
    return f"{entry['team_name']} ({entry['username']})" if entry["team_name"] else entry["username"]


def _score_text(row):
    text = f"{row['home_goals']} x {row['away_goals']}"
    if row["home_pens"] is not None:
        text += f" ({row['home_pens']} x {row['away_pens']} nos pênaltis)"
    return text


def _match_context(db, tournament, match_id):
    match = db.execute("SELECT * FROM tournament_matches WHERE id = ?", (match_id,)).fetchone()
    home, away = _entry(db, match["home_entry_id"]), _entry(db, match["away_entry_id"])
    return match, home, away, f"{_label(home)} x {_label(away)}", external_url(
        "tournaments.match", slug=tournament["slug"], match_id=match_id)


def _games_messages(db, tournament, stage, entry_ids, subject, intro):
    link = external_url("tournaments.my_matches")
    page = external_url("tournaments.show", slug=tournament["slug"], aba="jogos")
    messages = []
    for entry_id in entry_ids:
        entry = _entry(db, entry_id)
        lines = []
        for match in db.execute("SELECT * FROM tournament_matches WHERE stage_id = ? AND "
                                "(home_entry_id = ? OR away_entry_id = ?) ORDER BY round, leg, id",
                                (stage["id"], entry["id"], entry["id"])):
            home, away = _entry(db, match["home_entry_id"]), _entry(db, match["away_entry_id"])
            lines.append(f"  Rodada {match['round']}: {_label(home)} x {_label(away)}")
        games = "\n".join(lines) if lines else "  Seu primeiro jogo depende dos resultados da rodada anterior."
        messages.append((entry["email"], subject, (
            f"Olá, {entry['username']}!\n\n{intro}\n\n"
            f"Seus jogos:\n{games}\n\nVeja seus confrontos e reporte os placares em:\n{link}\n\n"
            f"Tabela e todos os jogos do torneio:\n{page}\n")))
    return messages


def tournament_started(db, tournament):
    """Para cada inscrito: os jogos dele na primeira fase e o link de Meus jogos."""
    stage = store.stages_of(db, tournament["id"])[0]
    _deliver(_games_messages(db, tournament, stage, [e["id"] for e in store.entries_of(db, tournament["id"])],
                             f"O torneio {tournament['name']} começou!",
                             f"O sorteio foi feito e o torneio {tournament['name']} começou."))


def stage_started(db, tournament):
    """Próxima fase gerada (mata-mata depois dos grupos/pontos corridos/liga):
    cada classificado recebe os jogos dele na fase nova."""
    stage = store.stages_of(db, tournament["id"])[-1]
    qualified = sorted({entry_id for row in db.execute(
        "SELECT entry_a, entry_b FROM tournament_ties WHERE stage_id = ?", (stage["id"],))
        for entry_id in row if entry_id})
    _deliver(_games_messages(db, tournament, stage, qualified,
                             f"{tournament['name']}: você está no {stage['name']}!",
                             f"Você se classificou e a fase {stage['name']} do torneio {tournament['name']} "
                             "foi sorteada."))


def score_reported(db, tournament, match_id):
    """Para o adversário de quem reportou: o placar e o link para confirmar ou contestar."""
    match, home, away, title, link = _match_context(db, tournament, match_id)
    reported = reports.last_report(db, match_id)
    reporter = home if match["reported_by_entry_id"] == home["id"] else away
    opponent = away if reporter is home else home
    deadline = format_local(reports.deadline(db, match), "%d/%m/%Y às %H:%M")
    _deliver([(opponent["email"], f"Confirme o placar: {title}", (
        f"Olá, {opponent['username']}!\n\n{reporter['username']} reportou o placar do jogo "
        f"{title} no torneio {tournament['name']}:\n\n  {_label(home)} {_score_text(reported)} {_label(away)}\n\n"
        f"Confirme ou conteste em:\n{link}\n\n"
        f"Sem resposta até {deadline}, o placar é confirmado automaticamente.\n"))])


def score_disputed(db, tournament, match_id):
    """Para os dois jogadores e o organizador: os dois placares e o motivo da contestação."""
    match, home, away, title, link = _match_context(db, tournament, match_id)
    reported = reports.last_report(db, match_id)
    dispute = db.execute("SELECT * FROM match_reports WHERE match_id = ? AND action = 'dispute' ORDER BY id DESC "
                         "LIMIT 1", (match_id,)).fetchone()
    who = _entry(db, dispute["entry_id"]) if dispute and dispute["entry_id"] else None
    by = who["username"] if who else "O sistema"
    text = f"{by} contestou o placar do jogo {title} no torneio {tournament['name']}.\n\n"
    if reported:
        reporter = _entry(db, reported["entry_id"]) if reported["entry_id"] else None
        text += (f"Placar reportado{' por ' + reporter['username'] if reporter else ''}:\n"
                 f"  {_label(home)} {_score_text(reported)} {_label(away)}\n\n")
    if dispute and dispute["home_goals"] is not None:
        text += f"Placar segundo {by}:\n  {_label(home)} {_score_text(dispute)} {_label(away)}\n\n"
    if dispute and dispute["comment"]:
        text += f"Motivo: {dispute['comment']}\n\n"
    organizer = db.execute("SELECT username, email FROM players WHERE id = ?", (tournament["organizer_id"],)).fetchone()
    organizer_plays = tournament["organizer_id"] in (home["player_id"], away["player_id"])
    decides = ("A administração do WE Camp decide o resultado (o organizador joga esta partida)."
               if organizer_plays else f"O organizador ({organizer['username']}) decide o resultado.")
    messages = [(home["email"], f"Placar contestado: {title}", f"Olá, {home['username']}!\n\n{text}{decides}\n{link}\n"),
                (away["email"], f"Placar contestado: {title}", f"Olá, {away['username']}!\n\n{text}{decides}\n{link}\n")]
    manage = external_url("tournaments.manage", slug=tournament["slug"]) + f"#jogo-{match_id}"
    if not organizer_plays:
        messages.append((organizer["email"], f"Placar contestado: {title}", (
            f"Olá, {organizer['username']}!\n\n{text}Como organizador, defina o resultado em:\n{manage}\n")))
    else:
        messages += [(email, f"[Administração] Placar contestado: {title}", (
            f"Olá!\n\n{text}O organizador ({organizer['username']}) joga esta partida, então a administração "
            f"do WE Camp decide o resultado (entre como administrador antes de abrir o link):\n{manage}\n"))
            for email in admin_emails(db)]
    _deliver(messages)


def _all_played(db, tournament):
    stages = store.stages_of(db, tournament["id"])
    return (tournament["status"] == "running" and stages and not store.has_next_stage(tournament, stages[-1])
            and store.stage_complete(db, stages[-1]))


def check_completion(db, tournament):
    """Último jogo confirmado: avisa o organizador uma vez e marca a hora (para o
    encerramento automático). Se um resultado for apagado depois, desmarca."""
    tournament = store.get_tournament(db, tournament["id"])
    if tournament["status"] != "running":
        return
    stages = store.stages_of(db, tournament["id"])
    if stages and store.has_next_stage(tournament, stages[-1]):
        _check_stage(db, tournament, stages[-1])
        return
    if not _all_played(db, tournament):
        if tournament["all_played_at"]:
            db.execute("UPDATE tournaments SET all_played_at = NULL WHERE id = ?", (tournament["id"],))
            db.commit()
        return
    if tournament["all_played_at"]:
        return
    db.execute("UPDATE tournaments SET all_played_at = ? WHERE id = ?", (now(), tournament["id"]))
    db.commit()
    organizer = db.execute("SELECT username, email FROM players WHERE id = ?", (tournament["organizer_id"],)).fetchone()
    limit = format_local((datetime.now(timezone.utc) + timedelta(hours=AUTO_FINISH_HOURS)).replace(microsecond=0)
                         .isoformat(), "%d/%m/%Y às %H:%M")
    _deliver([(organizer["email"], f"Todos os jogos terminaram: encerre o torneio {tournament['name']}", (
        f"Olá, {organizer['username']}!\n\nO último jogo do torneio {tournament['name']} foi confirmado.\n\n"
        "Confira os resultados e encerre o torneio para registrar a classificação final e distribuir o prêmio:\n"
        f"{external_url('tournaments.manage', slug=tournament['slug'])}\n\n"
        f"Se o torneio não for encerrado até {limit}, ele é encerrado automaticamente.\n"))])


def _check_stage(db, tournament, stage):
    """Fase que tem próxima (grupos, pontos corridos, liga suíça) com todos os jogos
    confirmados: avisa o organizador uma vez para gerar a próxima fase. Não há
    geração automática - o organizador confere a tabela antes do sorteio."""
    complete = store.stage_complete(db, stage)
    if complete == bool(stage["all_played_at"]):
        return
    db.execute("UPDATE tournament_stages SET all_played_at = ? WHERE id = ?", (now() if complete else None, stage["id"]))
    db.commit()
    if not complete:
        return
    organizer = db.execute("SELECT username, email FROM players WHERE id = ?", (tournament["organizer_id"],)).fetchone()
    _deliver([(organizer["email"], f"{stage['name']} terminou: gere a próxima fase do torneio {tournament['name']}", (
        f"Olá, {organizer['username']}!\n\nO último jogo da fase {stage['name']} do torneio {tournament['name']} "
        "foi confirmado.\n\nConfira a classificação e clique em \"Gerar próxima fase\" para sortear o mata-mata "
        f"(os classificados recebem os jogos por e-mail):\n{external_url('tournaments.manage', slug=tournament['slug'])}\n"))])


def sweep(db, at=None):
    """Rotina periódica (sem cron: chamada pelas páginas de torneio): marca/avisa
    torneios com todos os jogos confirmados e encerra os que passaram do prazo."""
    at = at or datetime.now(timezone.utc)
    for tournament in db.execute("SELECT * FROM tournaments WHERE status = 'running'").fetchall():
        check_completion(db, tournament)
    limit = (at - timedelta(hours=AUTO_FINISH_HOURS)).replace(microsecond=0).isoformat()
    for tournament in db.execute("SELECT * FROM tournaments WHERE status = 'running' AND all_played_at IS NOT NULL "
                                 "AND all_played_at <= ?", (limit,)).fetchall():
        try:
            if _all_played(db, tournament):
                store.finish(db, tournament)
                db.commit()
        except TournamentError as error:
            db.rollback()
            current_app.logger.warning("Encerramento automático do torneio %s falhou: %s", tournament["id"], error)


def auto_finish_at(tournament):
    """Quando o torneio com todos os jogos confirmados encerra sozinho (ISO), ou None."""
    if tournament["status"] != "running" or not tournament["all_played_at"]:
        return None
    played = datetime.fromisoformat(tournament["all_played_at"])
    return (played + timedelta(hours=AUTO_FINISH_HOURS)).replace(microsecond=0).isoformat()


def empty_wallets(db):
    """Jogadores que um débito por derrota deixou com W$0,00 (players.wallet_empty_pending):
    avisa o jogador (a administração vai creditar um novo valor) e a administração.
    Só lê o que já foi gravado (chamada no começo das páginas de torneio) e desmarca
    antes de enviar, para não repetir o aviso."""
    messages = []
    for p in db.execute("SELECT id, username, email, wallet_cents FROM players WHERE wallet_empty_pending = 1").fetchall():
        if not db.execute("UPDATE players SET wallet_empty_pending = 0 WHERE id = ? AND wallet_empty_pending = 1",
                          (p["id"],)).rowcount:
            continue
        if p["wallet_cents"] > 0:
            continue  # o resultado foi corrigido (estorno) ou já houve crédito
        messages.append((p["email"], "Seu saldo W$ acabou", (
            f"Olá, {p['username']}!\n\nCom o débito por derrota, o seu saldo W$ chegou a W$0,00. Não se preocupe: "
            "a Administração do WE Camp já foi avisada e estará creditando um novo valor na sua carteira.\n\n"
            f"Sua carteira: {external_url('tournaments.wallet')}\n")))
        for admin in admin_emails(db):
            messages.append((admin, f"Jogador sem saldo W$: {p['username']}", (
                f"O jogador {p['username']} ({p['email']}) ficou sem saldo W$ (W$0,00) depois de um débito por "
                "derrota.\n\nPara creditar um novo valor, use o ajuste de saldo em Torneios: administração:\n"
                f"{external_url('tournaments_admin.admin_home')}\n")))
    if messages:
        db.commit()
        _deliver(messages)


# --- Amigos e convites (app/tournament/friends.py) -----------------------------

def friend_request(db, from_id, to_id, accepted=False):
    """Pedido de amizade (ou o aviso de que o pedido foi aceito)."""
    who = db.execute("SELECT username FROM players WHERE id = ?", (from_id,)).fetchone()
    to = db.execute("SELECT username, email FROM players WHERE id = ?", (to_id,)).fetchone()
    if not who or not to:
        return
    link = external_url("tournaments.friends_page")
    if accepted:
        subject, text = f"{who['username']} aceitou sua amizade", f"{who['username']} aceitou seu pedido de amizade."
    else:
        subject = f"{who['username']} quer ser seu amigo no WE Camp"
        text = f"{who['username']} enviou um pedido de amizade. Aceite ou recuse em:\n{link}"
    _deliver([(to["email"], subject, f"Olá, {to['username']}!\n\n{text}\n")])


def tournament_invites(db, tournament, inviter_name, players):
    """Convite para o torneio, para cada jogador convidado agora."""
    page = external_url("tournaments.show", slug=tournament["slug"], aba="geral")
    day = "/".join(reversed((tournament["starts_on"] or "").split("-")))
    starts = f"\nInício provável: {day}" if day else ""
    _deliver([(p["email"], f"Convite para o torneio {tournament['name']}", (
        f"Olá, {p['username']}!\n\n{inviter_name} convidou você para o torneio \"{tournament['name']}\"."
        f"{starts}\n\nVeja o torneio e faça a sua inscrição em:\n{page}\n")) for p in players])


def friendly_event(db, friendly, to_id, subject, text):
    """Aviso de um amistoso (convite, aceite, placar, placar não aceito), com o link dele."""
    to = db.execute("SELECT username, email FROM players WHERE id = ?", (to_id,)).fetchone()
    if not to:
        return
    link = external_url("tournaments.friendly_show", friendly_id=friendly["id"])
    _deliver([(to["email"], subject, f"Olá, {to['username']}!\n\n{text}\n{link}\n")])


def iso_request(db, row):
    """ISO enviada por um jogador: a administração confere e libera."""
    link = external_url("tournaments_admin.admin_iso_edit", catalog_id=row["id"])
    _deliver([(email, f"{row['requester_name']} enviou uma ISO para liberar", (
        f"{row['requester_name']} ({row['requester_email']}) enviou uma ISO para os amistosos:\n\n"
        f"Nome: {row['name']}\nJogo base: {row['game_name']}\nArquivo: {row['original_filename']}\n\n"
        f"Confira o nome, o jogo base, cadastre a imagem e libere em:\n{link}\n")) for email in admin_emails(db)])


def iso_approved(db, row):
    """Avisa quem enviou a ISO que ela foi liberada."""
    if not row or not row["requester_email"]:
        return
    link = external_url("tournaments.friendlies")
    _deliver([(row["requester_email"], f"Sua ISO {row['name']} foi liberada", (
        f"Olá, {row['requester_name']}!\n\nA administração do WE Camp liberou a ISO \"{row['name']}\" "
        f"({row['game_name']}). Ela já aparece para escolher no convite de amistoso:\n{link}\n"))])


def iso_rejected(db, row, reason=""):
    """Avisa quem enviou a ISO que ela foi recusada."""
    if not row or not row["requester_email"]:
        return
    why = f"\nMotivo: {reason.strip()[:300]}" if reason and reason.strip() else ""
    _deliver([(row["requester_email"], f"Sua ISO {row['name']} não foi liberada", (
        f"Olá, {row['requester_name']}!\n\nA administração do WE Camp não liberou a ISO \"{row['name']}\"."
        f"{why}\n"))])
