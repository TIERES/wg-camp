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

  - ajudantes (editors.py): quem entra ou sai dos ajudantes de uma ISO ou de um campeonato
    é avisado; a alteração feita por um responsável vai para os outros responsáveis com o
    antes e o depois (change_lines) e o link para confirmar ou recusar.

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
SIGNATURE = "\n--\nWE Camp - Campeonatos\n"
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
            f"Tabela e todos os jogos do campeonato:\n{page}\n")))
    return messages


def tournament_started(db, tournament):
    """Para cada inscrito: os jogos dele na primeira fase e o link de Meus jogos."""
    stage = store.stages_of(db, tournament["id"])[0]
    _deliver(_games_messages(db, tournament, stage, [e["id"] for e in store.entries_of(db, tournament["id"])],
                             f"O campeonato {tournament['name']} começou!",
                             f"O sorteio foi feito e o campeonato {tournament['name']} começou."))


def stage_started(db, tournament):
    """Próxima fase gerada (mata-mata depois dos grupos/pontos corridos/liga):
    cada classificado recebe os jogos dele na fase nova."""
    stage = store.stages_of(db, tournament["id"])[-1]
    qualified = sorted({entry_id for row in db.execute(
        "SELECT entry_a, entry_b FROM tournament_ties WHERE stage_id = ?", (stage["id"],))
        for entry_id in row if entry_id})
    _deliver(_games_messages(db, tournament, stage, qualified,
                             f"{tournament['name']}: você está no {stage['name']}!",
                             f"Você se classificou e a fase {stage['name']} do campeonato {tournament['name']} "
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
        f"{title} no campeonato {tournament['name']}:\n\n  {_label(home)} {_score_text(reported)} {_label(away)}\n\n"
        f"Confirme ou conteste em:\n{link}\n\n"
        f"Sem resposta até {deadline}, o placar é confirmado automaticamente.\n"))])


def score_from_game(db, tournament, match_id):
    """Para os dois jogadores: o placar que o RetroArch TIERES leu do jogo e o
    link para confirmar ou contestar."""
    match, home, away, title, link = _match_context(db, tournament, match_id)
    reported = reports.last_report(db, match_id)
    deadline = format_local(reports.deadline(db, match), "%d/%m/%Y às %H:%M")
    _deliver([(player["email"], f"Confira o placar: {title}", (
        f"Olá, {player['username']}!\n\nO RetroArch TIERES leu o placar do jogo {title} no campeonato "
        f"{tournament['name']}:\n\n  {_label(home)} {_score_text(reported)} {_label(away)}\n\n"
        f"Os dois jogadores confirmam (ou contestam) em:\n{link}\n\n"
        f"Sem resposta até {deadline}, o placar é confirmado automaticamente.\n")) for player in (home, away)])


def score_disputed(db, tournament, match_id):
    """Para os dois jogadores e o organizador: os dois placares e o motivo da contestação."""
    match, home, away, title, link = _match_context(db, tournament, match_id)
    reported = reports.last_report(db, match_id)
    dispute = db.execute("SELECT * FROM match_reports WHERE match_id = ? AND action = 'dispute' ORDER BY id DESC "
                         "LIMIT 1", (match_id,)).fetchone()
    who = _entry(db, dispute["entry_id"]) if dispute and dispute["entry_id"] else None
    by = who["username"] if who else "O sistema"
    text = f"{by} contestou o placar do jogo {title} no campeonato {tournament['name']}.\n\n"
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
    _deliver([(organizer["email"], f"Todos os jogos terminaram: encerre o campeonato {tournament['name']}", (
        f"Olá, {organizer['username']}!\n\nO último jogo do campeonato {tournament['name']} foi confirmado.\n\n"
        "Confira os resultados e encerre o campeonato para registrar a classificação final e distribuir o prêmio:\n"
        f"{external_url('tournaments.manage', slug=tournament['slug'])}\n\n"
        f"Se o campeonato não for encerrado até {limit}, ele é encerrado automaticamente.\n"))])


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
    _deliver([(organizer["email"], f"{stage['name']} terminou: gere a próxima fase do campeonato {tournament['name']}", (
        f"Olá, {organizer['username']}!\n\nO último jogo da fase {stage['name']} do campeonato {tournament['name']} "
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
            current_app.logger.warning("Encerramento automático do campeonato %s falhou: %s", tournament["id"], error)


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
                "derrota.\n\nPara creditar um novo valor, use o ajuste de saldo em Campeonatos: administração:\n"
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
    _deliver([(p["email"], f"Convite para o campeonato {tournament['name']}", (
        f"Olá, {p['username']}!\n\n{inviter_name} convidou você para o campeonato \"{tournament['name']}\"."
        f"{starts}\n\nVeja o campeonato e faça a sua inscrição em:\n{page}\n")) for p in players])


def entry_notices(db, tournament, notices):
    """Avisos aos inscritos quando o organizador muda os dados do campeonato antes
    do início (store.update_before_start): [(player_id, assunto, texto)], com o
    link do campeonato."""
    page = external_url("tournaments.show", slug=tournament["slug"], aba="geral")
    messages = []
    for player_id, subject, text in notices:
        to = db.execute("SELECT username, email FROM players WHERE id = ?", (player_id,)).fetchone()
        if to:
            messages.append((to["email"], subject, f"Olá, {to['username']}!\n\n{text}\n\nCampeonato:\n{page}\n"))
    _deliver(messages)


def friendly_event(db, friendly, to_id, subject, text):
    """Aviso de um amistoso (convite, aceite, placar, placar não aceito), com o link dele."""
    to = db.execute("SELECT username, email FROM players WHERE id = ?", (to_id,)).fetchone()
    if not to:
        return
    link = external_url("tournaments.friendly_show", friendly_id=friendly["id"])
    _deliver([(to["email"], subject, f"Olá, {to['username']}!\n\n{text}\n{link}\n")])


def _image_text(name):
    return external_url("images.serve", kind="torneio", name=name) if name else "sem imagem"


def change_lines(items):
    """O antes e o depois de uma alteração, para o corpo do e-mail. items:
    [{"field", "label", "before", "after"}] (iso_catalog._changes, changes.diff).
    Valor curto: "Nome: antes → depois"; texto longo, de várias linhas ou imagem
    (o link dela): um bloco com "Antes" e "Depois"."""
    lines = []
    for item in items:
        before, after = item["before"], item["after"]
        if item["field"] == "image":
            before, after = _image_text(before), _image_text(after)
        if "\n" in before + after or len(before) + len(after) > 90:
            lines.append(f"  {item['label']}:")
            for title, text in (("Antes", before), ("Depois", after)):
                lines.append(f"    {title}:")
                lines += [f"      {line}" for line in (text or "-").splitlines()]
        else:
            lines.append(f"  {item['label']}: {before} → {after}")
    return "\n".join(lines)


def _names(people):
    return ", ".join(p["username"] for p in people)


def _player(db, player_id):
    return db.execute("SELECT id, username, email FROM players WHERE id = ?", (player_id,)).fetchone() \
        if player_id else None


def helpers_changed(db, kind, row, by_id, added, removed):
    """Quem entrou ou saiu dos ajudantes de uma ISO (kind "iso", row de
    catalog_isos) ou de um campeonato (kind "tournament"): cada um é avisado."""
    if not added and not removed:
        return
    by = _player(db, by_id)
    who = by["username"] if by else "A administração do WE Camp"
    if kind == "iso":
        what, link = f"da ISO \"{row['name']}\"", external_url("tournaments.friendlies") + "#minhas-isos"
        can = ("Agora você pode editar o nome, o jogo base e a imagem dessa ISO e enviar um arquivo novo, em "
               "Amistosos > Minhas ISOs:")
    else:
        what, link = f"do campeonato \"{row['name']}\"", external_url("tournaments.manage", slug=row["slug"])
        can = "Agora você pode editar os dados, a imagem e a ISO desse campeonato:"
    messages = [(p["email"], f"{who} adicionou você como ajudante {what}", (
        f"Olá, {p['username']}!\n\n{who} adicionou você como ajudante {what}. {can}\n{link}\n\n"
        "O que um responsável altera é confirmado por outro responsável: você recebe um e-mail com o antes e o "
        "depois de cada alteração para confirmar.\n")) for p in added]
    messages += [(p["email"], f"Você não é mais ajudante {what}", (
        f"Olá, {p['username']}!\n\n{who} tirou você dos ajudantes {what}.\n")) for p in removed]
    _deliver(messages)


def iso_request(db, row):
    """ISO enviada por um jogador: a administração confere e libera."""
    from . import editors
    link = external_url("tournaments_admin.admin_iso_edit", catalog_id=row["id"])
    helpers = editors.helpers(db, "iso", row["id"])
    extra = (f"Imagem: {_image_text(row['image'])}\n" if row["image"] else "") + (
        f"Ajudantes: {_names(helpers)}\n" if helpers else "")
    check = "Confira o nome, o jogo base e a imagem" if row["image"] else "Confira o nome, o jogo base, cadastre a imagem"
    _deliver([(email, f"{row['requester_name']} enviou uma ISO para liberar", (
        f"{row['requester_name']} ({row['requester_email']}) enviou uma ISO para os amistosos:\n\n"
        f"Nome: {row['name']}\nJogo base: {row['game_name']}\nArquivo: {row['original_filename']}\n{extra}\n"
        f"{check} e libere em:\n{link}\n")) for email in admin_emails(db)])


def _iso_people(db, row, *extra_ids):
    """Os responsáveis pela ISO (quem enviou e os ajudantes) e mais quem vier em
    extra_ids (quem fez a alteração, se já não é mais responsável), sem repetir."""
    from . import iso_catalog
    people = {p["id"]: p for p in iso_catalog.responsible(db, row)}
    for player_id in extra_ids:
        player = _player(db, player_id) if player_id and player_id not in people else None
        if player:
            people[player["id"]] = player
    return list(people.values())


def iso_approved(db, row):
    """Avisa os responsáveis pela ISO que ela foi liberada."""
    if not row:
        return
    link = external_url("tournaments.iso_page", catalog_id=row["id"])
    _deliver([(p["email"], f"{'Sua' if p['id'] == row['requested_by'] else 'A'} ISO {row['name']} foi liberada", (
        f"Olá, {p['username']}!\n\nA administração do WE Camp liberou a ISO \"{row['name']}\" "
        f"({row['game_name']}). Ela já aparece para escolher no convite de amistoso e tem a sua página, "
        f"com o link para compartilhar:\n{link}\n")) for p in _iso_people(db, row)])


def iso_rejected(db, row, reason=""):
    """Avisa quem enviou a ISO que ela foi recusada."""
    if not row or not row["requester_email"]:
        return
    why = f"\nMotivo: {reason.strip()[:300]}" if reason and reason.strip() else ""
    _deliver([(row["requester_email"], f"Sua ISO {row['name']} não foi liberada", (
        f"Olá, {row['requester_name']}!\n\nA administração do WE Camp não liberou a ISO \"{row['name']}\"."
        f"{why}\n"))])


def iso_edit_request(db, row, result, editor_id=None):
    """Um responsável alterou a ISO (iso_catalog.request_edit).

    A administração confere - e, se a ISO já estava liberada, aprova ou recusa a
    alteração, que é aprovada sozinha se ninguém responder no prazo. Os outros
    responsáveis recebem o antes e o depois: com a ISO já liberada, o link para
    confirmar (a alteração entra assim que um deles confirma)."""
    from .iso_catalog import auto_approve_at
    link = external_url("tournaments_admin.admin_iso_edit", catalog_id=row["id"])
    editor = _player(db, editor_id) or {"id": row["requested_by"], "username": row["requester_name"],
                                        "email": row["requester_email"]}
    who = f"{editor['username']} ({editor['email']})"
    changes = change_lines(result["changes"])
    others = [p for p in _iso_people(db, row) if p["id"] != editor["id"]]
    if result["mode"] == "direct":
        subject = f"{editor['username']} alterou a ISO {row['name']}, que aguarda liberação"
        text = (f"{who} alterou a ISO \"{row['name']}\", que ainda aguarda a liberação:\n\n{changes}\n\n"
                f"Confira o nome, o jogo base e a imagem e libere em:\n{link}\n")
        messages = [(email, subject, text) for email in admin_emails(db)]
        messages += [(p["email"], f"{editor['username']} alterou a ISO {row['name']}", (
            f"Olá, {p['username']}!\n\n{editor['username']} alterou a ISO \"{row['name']}\", da qual você é um dos "
            f"responsáveis. O que mudou (antes → depois):\n\n{changes}\n\nA ISO ainda aguarda a liberação da "
            "administração do WE Camp, então a alteração já entrou. Veja em Amistosos > Minhas ISOs:\n"
            f"{external_url('tournaments.friendlies')}#minhas-isos\n")) for p in others]
        _deliver(messages)
        return
    deadline = format_local(auto_approve_at(row["rev_submitted_at"]), "%d/%m/%Y às %H:%M")
    subject = f"{editor['username']} alterou a ISO {row['name']}: aprove a alteração"
    also = (f"Os outros responsáveis pela ISO ({_names(others)}) também receberam o pedido: se um deles confirmar, "
            "a alteração entra sem passar por aqui.\n\n" if others else "")
    text = (f"{who} alterou a ISO \"{row['name']}\" ({row['game_name']}), já liberada para os amistosos:\n\n"
            f"{changes}\n\nAté a aprovação, os amistosos continuam com a versão atual. Aprove ou recuse em:\n"
            f"{link}\n\n{also}Sem resposta até {deadline}, a alteração é aprovada automaticamente.\n")
    messages = [(email, subject, text) for email in admin_emails(db)]
    confirm = external_url("tournaments.iso_change", catalog_id=row["id"])
    messages += [(p["email"], f"Confirme a alteração de {editor['username']} na ISO {row['name']}", (
        f"Olá, {p['username']}!\n\n{editor['username']} alterou a ISO \"{row['name']}\" ({row['game_name']}), da qual "
        f"você é um dos responsáveis. O que muda (antes → depois):\n\n{changes}\n\n"
        f"Confira e confirme (ou recuse) a alteração em:\n{confirm}\n\n"
        "Assim que você ou outro responsável confirmar, a alteração entra no lugar da versão atual. Sem confirmação, "
        f"vale a aprovação da administração do WE Camp ou, sem resposta até {deadline}, a automática.\n"))
        for p in others]
    _deliver(messages)


def iso_edit_approved(db, row, auto=False, rev=None):
    """A alteração da ISO entrou: avisa quem a fez e os outros responsáveis (menos
    quem confirmou) e, se foi pelo prazo, a administração. rev: a alteração como
    estava (iso_catalog.revision); sem ela, quem fez foi quem enviou a ISO."""
    if not row:
        return
    link = external_url("tournaments.iso_page", catalog_id=row["id"])
    author_id = rev["requested_by"] if rev is not None else row["requested_by"]
    confirmer_id = rev["confirmed_by"] if rev is not None else None
    confirmer = _player(db, confirmer_id)
    author = _player(db, author_id)
    author_name = author["username"] if author else "um responsável"
    messages = []
    for p in _iso_people(db, row, author_id):
        if p["id"] == confirmer_id:
            continue
        mine = p["id"] == author_id
        whose = "a sua alteração" if mine else f"a alteração de {author_name}"
        if auto:
            how = f"Como ninguém respondeu em 24 h, {whose} na ISO foi aprovada automaticamente"
        elif confirmer:
            how = f"{confirmer['username']} confirmou {whose} na ISO"
        else:
            how = f"A administração do WE Camp aprovou {whose} na ISO"
        subject = (f"Sua alteração na ISO {row['name']} foi aprovada" if mine
                   else f"A alteração de {author_name} na ISO {row['name']} foi aprovada")
        messages.append((p["email"], subject, (
            f"Olá, {p['username']}!\n\n{how}. A ISO \"{row['name']}\" ({row['game_name']}) já está "
            f"atualizada nos amistosos:\n{link}\n")))
    if auto:
        admin_link = external_url("tournaments_admin.admin_iso_edit", catalog_id=row["id"])
        messages += [(email, f"Alteração da ISO {row['name']} aprovada automaticamente", (
            f"A alteração que {author_name if author else 'o jogador'} pediu na ISO \"{row['name']}\" "
            f"({row['game_name']}) ficou 24 h sem resposta e foi aprovada automaticamente.\n\n"
            f"Confira a ISO em:\n{admin_link}\n")) for email in admin_emails(db)]
    _deliver(messages)


def iso_edit_rejected(db, row, revision, reason="", by_id=None):
    """A alteração da ISO foi recusada (a ISO continua como estava): avisa quem a
    fez e os outros responsáveis. by_id: o responsável que recusou; sem ele, foi
    a administração."""
    if not row:
        return
    why = f"\nMotivo: {reason.strip()[:300]}" if reason and reason.strip() else ""
    refuser = _player(db, by_id)
    author_id = revision["requested_by"]
    author = _player(db, author_id)
    author_name = author["username"] if author else "um responsável"
    messages = []
    for p in _iso_people(db, row, author_id):
        if p["id"] == by_id:
            continue
        mine = p["id"] == author_id
        whose = "a alteração que você pediu" if mine else f"a alteração que {author_name} pediu"
        who = f"{refuser['username']} recusou" if refuser else "A administração do WE Camp não aprovou"
        subject = (f"Sua alteração na ISO {row['name']} não foi aprovada" if mine
                   else f"A alteração de {author_name} na ISO {row['name']} não foi aprovada")
        messages.append((p["email"], subject, (
            f"Olá, {p['username']}!\n\n{who} {whose} na ISO "
            f"\"{row['name']}\" (o nome pedido era \"{revision['name']}\"). A ISO continua como estava.{why}\n")))
    _deliver(messages)


# --- Alteração do campeonato esperando confirmação (changes.py) ----------------

def _tournament_people(db, tournament, *extra_ids):
    from . import changes
    people = {p["id"]: p for p in changes.responsible(db, tournament)}
    for player_id in extra_ids:
        player = _player(db, player_id) if player_id and player_id not in people else None
        if player:
            people[player["id"]] = player
    return list(people.values())


def tournament_change_request(db, tournament, change, items, effects=()):
    """Um responsável alterou o campeonato: os outros recebem o antes e o depois
    e o link para confirmar ou recusar (Gerenciar)."""
    author = change["requester_name"] or "Um responsável"
    link = external_url("tournaments.manage", slug=tournament["slug"]) + "#alteracao"
    after = ("Ao confirmar:\n" + "\n".join(f"  - {effect}" for effect in effects)
             + "\n  Quem sai é avisado por e-mail.\n\n") if effects else ""
    _deliver([(p["email"], f"Confirme a alteração de {author} no campeonato {tournament['name']}", (
        f"Olá, {p['username']}!\n\n{author} alterou o campeonato \"{tournament['name']}\", do qual você é um dos "
        "responsáveis. A alteração só entra depois que outro responsável confirmar.\n\n"
        f"O que muda (antes → depois):\n\n{change_lines(items)}\n\n{after}"
        f"Confira e confirme (ou recuse) em:\n{link}\n"))
        for p in _tournament_people(db, tournament) if p["id"] != change["requested_by"]])


def tournament_change_done(db, tournament, change, items, outcome, by_id=None, reason=""):
    """A alteração do campeonato foi confirmada, recusada ou cancelada por quem a
    fez (outcome: "confirmed", "refused", "cancelled"): avisa quem a fez e os
    outros responsáveis, menos quem agiu (by_id; None = a administração)."""
    actor = _player(db, by_id)
    who = actor["username"] if actor else "A administração do WE Camp"
    author = change["requester_name"] or "um responsável"
    did = {"confirmed": "confirmou", "refused": "recusou", "cancelled": "cancelou"}[outcome]
    title = {"confirmed": "confirmada", "refused": "recusada", "cancelled": "cancelada"}[outcome]
    link = external_url("tournaments.show", slug=tournament["slug"], aba="geral")
    why = f"\nMotivo: {reason.strip()[:300]}\n" if reason and reason.strip() else ""
    end = ("O campeonato já está atualizado:" if outcome == "confirmed" else "O campeonato continua como estava:")
    messages = []
    for p in _tournament_people(db, tournament, change["requested_by"]):
        if p["id"] == by_id:
            continue
        whose = "a sua alteração" if p["id"] == change["requested_by"] else f"a alteração de {author}"
        messages.append((p["email"], f"Alteração {title} no campeonato {tournament['name']}", (
            f"Olá, {p['username']}!\n\n{who} {did} {whose} no campeonato \"{tournament['name']}\":\n\n"
            f"{change_lines(items)}\n{why}\n{end}\n{link}\n")))
    _deliver(messages)
