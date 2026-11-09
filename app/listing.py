"""Listas em cards (carrossel): no máximo `page_size()` registros por lista grande -
20 por padrão; o jogador escolhe 20, 50 ou 100 no "Mostrar" do filtro
(templates/_carousel.html: per_page_field). Para achar os outros, o filtro
(list_note: "Mostrando 20 de N")."""
from flask import has_request_context, request

LIST_LIMIT = 20
PAGE_SIZES = (20, 50, 100)


def page_size():
    """Quantos registros a lista mostra: ?mostrar=20|50|100 (outro valor = 20)."""
    if has_request_context():
        value = request.args.get("mostrar", type=int)
        if value in PAGE_SIZES:
            return value
    return LIST_LIMIT


def cap(rows, limit=None):
    """(os primeiros `limit` - o escolhido no filtro -, o total) - o total vai na
    nota "Mostrando 20 de N"."""
    rows = list(rows)
    return rows[:limit or page_size()], len(rows)


def like(text):
    """Padrão de LIKE para um texto digitado no filtro (sem curingas dele)."""
    text = (text or "").strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{text}%"


def local_day(value, end=False):
    """AAAA-MM-DD (dia em São Paulo) -> início do dia (ou do dia seguinte) em UTC
    ISO, para comparar com as datas gravadas em UTC (filtros De/Até)."""
    from datetime import datetime, timedelta, timezone
    from .timeutil import LOCAL_TZ
    try:
        day = datetime.strptime(value or "", "%Y-%m-%d")
    except ValueError:
        return None
    moment = day.replace(tzinfo=LOCAL_TZ) + (timedelta(days=1) if end else timedelta())
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
