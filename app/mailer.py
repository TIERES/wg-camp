"""Envio de e-mails (cadastro, recuperação de senha, avisos de campeonatos e amistosos).

Todo e-mail sai com texto e HTML (compose): o HTML é o card padrão do WE Camp
(templates/email/_base.html: logo e "Campeonatos / Winning Eleven" no alto, o
aviso de que não é preciso responder no fim da mensagem e a logo menor com o
link "WE Camp" para o portal no rodapé). Quem manda só o texto ganha o card com
o texto dentro (templates/email/message.html, links clicáveis); quem tem um HTML
próprio (email/tournament_started.html) estende o _base.html.

Usa o SMTP configurado em ARENA17_SMTP_*. Sem SMTP configurado (ambiente
local) o e-mail é gravado como .eml em instance/outbox/ e registrado no log,
para que os links possam ser testados à mão. Nos testes (TESTING), as
mensagens só são guardadas em app.extensions["mail_outbox"].
"""
import re
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from flask import current_app, has_request_context, render_template
from markupsafe import Markup, escape

NO_REPLY = "Este é um e-mail automático: não é preciso responder."
SIGNATURE = "--\nWE Camp - Campeonatos Winning Eleven\n"
_URL = re.compile(r"https?://[^\s<>\"']+")


class MailError(Exception):
    pass


def outbox():
    return current_app.extensions.setdefault("mail_outbox", [])


def links():
    """Endereços completos do portal e da logo para o card dos e-mails; None fora de uma
    requisição (sem SERVER_NAME o url_for não monta endereço) - o e-mail sai só com o texto."""
    if not has_request_context() and not current_app.config.get("SERVER_NAME"):
        return None
    from .accounts import external_url
    return {"portal": external_url("public.index"),
            "logo": external_url("static", filename="images/logo/wecamp-192.png"),
            "logo_small": external_url("static", filename="images/logo/wecamp-96.png")}


def _linkify(text):
    parts, last = [], 0
    for match in _URL.finditer(text):
        url = match.group(0).rstrip(".,;:!?)")
        parts += [escape(text[last:match.start()]),
                  Markup('<a href="{0}" style="color:#34d399;word-break:break-all;">{0}</a>').format(url)]
        last = match.start() + len(url)
    parts.append(escape(text[last:]))
    return Markup("").join(parts)


def paragraphs(body):
    """O texto do e-mail em parágrafos HTML: linha em branco separa parágrafos, as outras
    quebras viram <br>, o recuo das listas ("  - item") é mantido e os endereços viram links."""
    result = []
    for block in re.split(r"\n(?:[ \t]*\n)+", body.strip("\n")):
        if not block.strip():
            continue
        lines = []
        for line in block.rstrip().split("\n"):
            text = line.lstrip(" ")
            lines.append(Markup("&nbsp;" * (len(line) - len(text))) + _linkify(text.rstrip()))
        result.append(Markup("<br>").join(lines))
    return result


def compose(subject, body, html=None):
    """(texto, html) do e-mail: o texto ganha o aviso de que não é preciso responder e a
    assinatura com o endereço do portal; sem html, o card padrão com o texto dentro.
    Chame dentro da requisição (os endereços completos precisam do url_for)."""
    mail = links()
    text = body.rstrip("\n") + "\n\n" + NO_REPLY + "\n" + SIGNATURE + (mail["portal"] + "\n" if mail else "")
    if html is None and mail:
        html = render_template("email/message.html", subject=subject, paragraphs=paragraphs(body), mail=mail)
    return text, html


def send_mail(to, subject, body, html=None):
    """Monta (compose) e envia na hora; html: um HTML próprio que estende email/_base.html."""
    deliver(to, subject, *compose(subject, body, html))


def deliver(to, subject, body, html=None):
    """Envia o e-mail já montado (compose): o texto e, se houver, o HTML como alternativa."""
    config = current_app.config
    message = EmailMessage()
    sender = config.get("MAIL_FROM") or config.get("SMTP_USER") or "nao-responda@localhost"
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")

    if config.get("TESTING"):
        outbox().append(message)
        return

    if not config.get("SMTP_HOST"):
        folder = Path(current_app.instance_path) / "outbox"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}.eml"
        path.write_bytes(bytes(message))
        current_app.logger.warning("SMTP não configurado - e-mail para %s gravado em %s", to, path)
        return

    security = (config.get("SMTP_SECURITY") or "starttls").lower()
    try:
        if security == "ssl":
            smtp = smtplib.SMTP_SSL(config["SMTP_HOST"], config["SMTP_PORT"], timeout=20,
                                    context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"], timeout=20)
        with smtp:
            if security == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            if config.get("SMTP_USER"):
                smtp.login(config["SMTP_USER"], config.get("SMTP_PASSWORD", ""))
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        current_app.logger.error("Falha ao enviar e-mail para %s: %s", to, error)
        raise MailError(str(error)) from error
