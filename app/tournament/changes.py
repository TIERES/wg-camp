"""Alteração do campeonato esperando a confirmação de outro responsável.

Sem ajudantes (editors.py), o organizador edita direto, como sempre. Com
ajudantes, o que um responsável muda - os dados do campeonato, a imagem ou a
ISO - fica guardado em tournament_changes até outro responsável confirmar (a
administração do WE Camp também pode). O e-mail para os outros responsáveis
leva o antes e o depois (notify.tournament_change_request).

Uma alteração por campeonato, com partes opcionais (dados, imagem, ISO):
enviar de novo uma parte substitui a que estava guardada, e quem enviou por
último passa a ser o autor - quem confirma é sempre outro responsável
(editors.can_confirm). A ISO nova fica só neste servidor até a confirmação:
só então substitui a atual e vai para o archive.org.

Não faz commit: a view confirma ou desfaz, como no store.py.
"""
import json

from .. import images
from ..db import now
from ..storage import delete_stored_file, human_size, store_upload
from ..wallet import format_cents
from . import editors, formats, iso, store
from .store import TournamentError
from .teams import TEAM_MODES

# Os dados que mudam depois do início (o formulário curto do Gerenciar).
BASIC_KEYS = ("name", "rules", "starts_on", "ends_on", "friends_only")
FULL_KEYS = BASIC_KEYS + ("game_id", "format", "max_players", "fee", "split", "config", "team_mode", "team_ids")


def get(db, tournament_id):
    """A alteração pendente do campeonato (None se não houver)."""
    return db.execute(
        "SELECT c.*, p.username AS requester_name, p.email AS requester_email FROM tournament_changes c "
        "LEFT JOIN players p ON p.id = c.requested_by WHERE c.tournament_id = ?", (tournament_id,)).fetchone()


def responsible(db, tournament):
    """Os responsáveis: o organizador e os ajudantes (id, username, email)."""
    return editors.responsible(db, "tournament", tournament["id"], tournament["organizer_id"])


def needs_confirmation(db, tournament):
    """Com ajudantes, o que um responsável muda espera a confirmação de outro."""
    return bool(editors.helpers(db, "tournament", tournament["id"]))


def can_confirm(db, tournament, change, player_id, admin=False):
    """Quem confirma (ou recusa): outro responsável ou a administração do WE Camp.
    Se os ajudantes saíram e só sobrou o autor, ele mesmo."""
    if change is None:
        return False
    if admin:
        return True
    return editors.can_confirm([p["id"] for p in responsible(db, tournament)], change["requested_by"], player_id,
                               alone_ok=True)


# --- Os dados do campeonato --------------------------------------------------

def values_of(change):
    """Os dados do formulário guardados na alteração (dict) ou None."""
    if change is None or not change["values_json"]:
        return None
    try:
        return json.loads(change["values_json"])
    except ValueError:
        return None


def current_values(db, tournament, kind="full"):
    """Os dados atuais do campeonato, no formato do formulário validado
    (tournaments._tournament_form), para comparar com a alteração."""
    values = {"kind": kind, "name": tournament["name"], "rules": tournament["rules"] or "",
              "starts_on": tournament["starts_on"], "ends_on": tournament["ends_on"],
              "friends_only": 1 if tournament["friends_only"] else 0}
    if kind == "full":
        values.update({
            "game_id": tournament["game_title_id"], "format": tournament["format"],
            "max_players": tournament["max_players"], "fee": tournament["entry_fee_cents"],
            "split": tournament["prize_split"],
            "config": formats.normalize(tournament["format"], store.settings_of(tournament)),
            "team_mode": tournament["team_mode"],
            "team_ids": [row[0] for row in db.execute(
                "SELECT team_id FROM tournament_teams WHERE tournament_id = ? ORDER BY team_id", (tournament["id"],))]})
    return values


def _comparable(values):
    keys = FULL_KEYS if values.get("kind") == "full" else BASIC_KEYS
    out = {key: values.get(key) for key in keys}
    out["starts_on"], out["ends_on"] = out["starts_on"] or None, out["ends_on"] or None
    if "team_ids" in out:
        out["team_ids"] = sorted(out["team_ids"] or [])
        if not out["fee"]:
            out["split"] = None  # sem inscrição não há prêmio para dividir
    return out


def _split_text(split):
    return " / ".join(f"{part}%" for part in (split or "").split(","))


def _date(value):
    return f"{value[8:10]}/{value[5:7]}/{value[:4]}" if value else "sem data"


def _values_diff(db, current, new):
    """[{"field", "label", "before", "after"}] do que os dados novos mudam."""
    items = []

    def add(field, label, before, after):
        if before != after:
            items.append({"field": field, "label": label, "before": before, "after": after})

    add("name", "Nome", current["name"], new["name"])
    if new.get("kind") == "full":
        games = {row["id"]: row["name"] for row in db.execute("SELECT id, name FROM game_titles")}
        add("game", "Jogo base", games.get(current["game_id"], "-"), games.get(new["game_id"], "-"))

        def format_text(values):
            config = formats.normalize(values["format"], values["config"])
            return f"{formats.FORMATS[values['format']]}. {formats.describe(values['format'], config)}"

        add("format", "Formato", format_text(current), format_text(new))
        add("max_players", "Vagas", str(current["max_players"]), str(new["max_players"]))
        fee = lambda cents: format_cents(cents) if cents else "Grátis"
        add("fee", "Inscrição", fee(current["fee"]), fee(new["fee"]))
        if new["fee"]:
            add("split", "Divisão do prêmio", _split_text(current["split"]) if current["fee"] else "sem prêmio",
                _split_text(new["split"]))
        add("team_mode", "Times", TEAM_MODES[current["team_mode"]], TEAM_MODES[new["team_mode"]])
        before, after = set(current["team_ids"]), set(new["team_ids"])
        if before != after:
            names = {row["id"]: row["name"] for row in db.execute("SELECT id, name FROM teams")}
            listed = lambda ids: ", ".join(sorted((names.get(i, "?") for i in ids), key=str.casefold))
            came, left = after - before, before - after
            detail = "; ".join(part for part in (f"entram: {listed(came)}" if came else "",
                                                 f"saem: {listed(left)}" if left else "") if part)
            add("team_ids", "Lista de times", f"{len(before)} time(s)", f"{len(after)} time(s) ({detail})")
    add("starts_on", "Início provável", _date(current["starts_on"]), _date(new["starts_on"]))
    add("ends_on", "Fim provável", _date(current["ends_on"]), _date(new["ends_on"]))
    add("rules", "Regulamento", current["rules"], new["rules"])
    yes_no = lambda value: "Sim" if value else "Não"
    add("friends_only", "Somente para amigos", yes_no(current["friends_only"]), yes_no(new["friends_only"]))
    return items


def diff(db, tournament, change):
    """O antes e o depois de tudo o que a alteração muda: [{"field", "label",
    "before", "after"}]. Na imagem, before/after são os nomes dos arquivos
    ("" = sem imagem) - notify.change_lines e a página montam os links."""
    if change is None:
        return []
    items = []
    values = values_of(change)
    if values:
        items += _values_diff(db, current_values(db, tournament, values.get("kind", "full")), values)
    if change["image_change"]:
        items.append({"field": "image", "label": "Imagem", "before": tournament["image"] or "",
                      "after": change["image"] or ""})
    if change["stored_name"]:
        current = iso.get(db, tournament["id"])
        before = f"{current['original_filename']} ({human_size(current['file_size'])})" if current else "nenhuma ISO"
        items.append({"field": "file", "label": "ISO", "before": before,
                      "after": f"{change['original_filename']} ({human_size(change['file_size'])})"})
    return items


# --- Guardar, cancelar e confirmar -------------------------------------------

def _drop_file(stored_name):
    if stored_name:
        try:
            delete_stored_file(stored_name)
        except (ValueError, OSError):
            pass


def _save_part(db, tournament, player_id, **columns):
    """Grava uma parte da alteração (cria a linha se preciso); quem enviou vira
    o autor e o pedido de confirmação recomeça. Sem parte nenhuma, a linha sai.
    Devolve a alteração como ficou (ou None)."""
    stamp = now()
    change = get(db, tournament["id"])
    if change is None:
        db.execute("INSERT INTO tournament_changes (tournament_id, requested_by, submitted_at, created_at, updated_at) "
                   "VALUES (?, ?, ?, ?, ?)", (tournament["id"], player_id, stamp, stamp, stamp))
    columns.update(requested_by=player_id, submitted_at=stamp, updated_at=stamp)
    db.execute(f"UPDATE tournament_changes SET {', '.join(f'{key} = ?' for key in columns)} WHERE tournament_id = ?",
               (*columns.values(), tournament["id"]))
    change = get(db, tournament["id"])
    if not (change["values_json"] or change["image_change"] or change["stored_name"]):
        db.execute("DELETE FROM tournament_changes WHERE id = ?", (change["id"],))
        return None
    return change


def propose_values(db, tournament, player_id, values, kind="full"):
    """Guarda os dados do formulário (já validados) para outro responsável
    confirmar. Iguais aos atuais, essa parte da alteração é descartada."""
    values = dict(values, kind=kind)
    if kind == "full":
        store.check_before_start(db, tournament, values)
    elif tournament["status"] in ("finished", "cancelled"):
        raise TournamentError("O campeonato já terminou: os dados não mudam mais.")
    same = _comparable(values) == _comparable(current_values(db, tournament, kind))
    return _save_part(db, tournament, player_id, values_json=None if same else json.dumps(values))


def propose_image(db, tournament, player_id, image):
    """Guarda a imagem nova (já gravada - images.save) ou, com None, o pedido de
    tirar a imagem (sem imagem para tirar, essa parte é descartada)."""
    previous = get(db, tournament["id"])
    change = _save_part(db, tournament, player_id, image_change=1 if image or tournament["image"] else 0, image=image)
    if previous is not None and previous["image_change"] and previous["image"] != image:
        images.delete("torneio", previous["image"])
    return change


def propose_iso(db, tournament, player_id, upload):
    """Guarda a ISO nova só neste servidor, até a confirmação. Igual à atual,
    essa parte é descartada."""
    previous = get(db, tournament["id"])
    try:
        stored = store_upload(upload)
    except ValueError as error:
        raise TournamentError(str(error)) from None
    if stored["file_size"] == 0:
        _drop_file(stored["stored_name"])
        raise TournamentError("O arquivo enviado está vazio.")
    current = iso.get(db, tournament["id"])
    try:
        if current is not None and current["md5"] == stored["md5"]:
            _drop_file(stored["stored_name"])
            change = _save_part(db, tournament, player_id, stored_name=None, original_filename=None, file_size=None,
                                md5=None, content_id=None)
        else:
            change = _save_part(db, tournament, player_id, stored_name=stored["stored_name"],
                                original_filename=stored["original_filename"], file_size=stored["file_size"],
                                md5=stored["md5"], content_id=iso.content_id_of(stored))
    except BaseException:
        _drop_file(stored["stored_name"])
        raise
    if previous is not None and previous["stored_name"]:
        _drop_file(previous["stored_name"])
    return change


def discard(db, change):
    """Apaga a alteração (cancelada por quem fez ou recusada) e o que ela guardava."""
    if change is None:
        return
    db.execute("DELETE FROM tournament_changes WHERE id = ?", (change["id"],))
    _drop_file(change["stored_name"])
    if change["image_change"]:
        images.delete("torneio", change["image"])


def preview(db, tournament, change):
    """O que a confirmação faz com quem já está inscrito, sem gravar nada:
    {"error": por que não dá para confirmar agora (ou None), "effects": [frases]}."""
    values = values_of(change)
    if not values or values.get("kind") != "full":
        return {"error": None, "effects": []}
    db.execute("SAVEPOINT change_preview")
    try:
        notices = store.update_before_start(db, tournament, values)
    except (TournamentError, ValueError) as error:
        return {"error": str(error), "effects": []}
    finally:
        db.execute("ROLLBACK TO change_preview")
        db.execute("RELEASE change_preview")
    counts = {}
    for _, subject, _ in notices:
        key = subject.rsplit(": ", 1)[-1]
        counts[key] = counts.get(key, 0) + 1
    texts = {"sua inscrição foi cancelada": "{} inscrito(s) saem do campeonato e recebem de volta o valor pago",
             "escolha o seu time": "{} inscrito(s) ficam sem time e precisam escolher outro",
             "os times serão sorteados": "os times de {} inscrito(s) voltam para o sorteio do início"}
    return {"error": None, "effects": [texts[key].format(count) for key, count in counts.items() if key in texts]}


def confirm(db, tournament, change):
    """Aplica a alteração confirmada: os dados (store.update_before_start antes
    do início, com os avisos aos inscritos), a imagem e a ISO (que passa a ser a
    do campeonato; o envio para o archive.org fica com quem chama, depois do
    commit). Devolve {"notices": avisos aos inscritos, "iso": trocou a ISO?}."""
    tournament = store.get_tournament(db, tournament["id"])
    values = values_of(change)
    notices = []
    if values and values.get("kind") == "full":
        notices = store.update_before_start(db, tournament, values, change["requested_by"])
    elif values:
        if tournament["status"] in ("finished", "cancelled"):
            raise TournamentError("O campeonato já terminou: os dados não mudam mais.")
        db.execute("UPDATE tournaments SET name = ?, rules = ?, starts_on = ?, ends_on = ?, friends_only = ?, "
                   "updated_at = ? WHERE id = ?",
                   (values["name"], values["rules"], values["starts_on"], values["ends_on"],
                    1 if values["friends_only"] else 0, now(), tournament["id"]))
    previous_iso = iso.get(db, tournament["id"]) if change["stored_name"] else None
    if change["stored_name"]:
        if iso.uploading(previous_iso):
            raise TournamentError("A ISO atual ainda está sendo enviada para o archive.org. Confirme de novo em alguns "
                                  "minutos.")
        db.execute(
            "INSERT OR REPLACE INTO tournament_isos (tournament_id, stored_name, original_filename, file_size, md5, "
            "content_id, archive_name, archive_status, uploaded_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (tournament["id"], change["stored_name"], change["original_filename"], change["file_size"], change["md5"],
             change["content_id"], f"torneios/{tournament['slug']}/{change['original_filename']}",
             change["requested_by"], now(), now()))
    if change["image_change"]:
        # O cartão do link compartilhado era da imagem antiga (o navegador monta o novo).
        db.execute("UPDATE tournaments SET image = ?, share_image = NULL, updated_at = ? WHERE id = ?",
                   (change["image"], now(), tournament["id"]))
    db.execute("DELETE FROM tournament_changes WHERE id = ?", (change["id"],))
    if previous_iso is not None and not previous_iso["local_removed_at"]:
        _drop_file(previous_iso["stored_name"])
    if change["image_change"]:
        images.delete("torneio", tournament["image"])
        images.delete("torneio", tournament["share_image"])
    return {"notices": notices, "iso": bool(change["stored_name"])}
