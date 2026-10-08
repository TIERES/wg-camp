"""Envio de e-mails (confirmação de cadastro, recuperação de senha).

Usa o SMTP configurado em ARENA17_SMTP_*. Sem SMTP configurado (ambiente
local) o e-mail é gravado como .eml em instance/outbox/ e registrado no log,
para que os links possam ser testados à mão. Nos testes (TESTING), as
mensagens só são guardadas em app.extensions["mail_outbox"].
"""
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from flask import current_app


class MailError(Exception):
    pass


def outbox():
    return current_app.extensions.setdefault("mail_outbox", [])


def send_mail(to, subject, body):
    config = current_app.config
    message = EmailMessage()
    sender = config.get("MAIL_FROM") or config.get("SMTP_USER") or "nao-responda@localhost"
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.set_content(body)

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
