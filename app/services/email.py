import logging
import smtplib
from email.message import EmailMessage

from ..config import settings

log = logging.getLogger(__name__)

Attachment = tuple[str, bytes, str]  # (filename, content, mimetype "type/subtype")


def send_email(
    to: str,
    subject: str,
    body: str,
    *,
    html_body: str | None = None,
    attachments: list[Attachment] | None = None,
) -> bool:
    """Envia e-mail com corpo texto + alternativa HTML opcional + anexos opcionais.

    `body` é sempre enviado em text/plain (fallback para clientes sem HTML).
    `html_body` adiciona alternativa text/html (mesma mensagem, melhor formatada).
    `attachments` é lista de (nome_arquivo, bytes, "tipo/subtipo").
    """
    if not settings.SMTP_HOST or not to:
        log.warning("SMTP não configurado ou destinatário vazio - email ignorado")
        return False
    msg = EmailMessage()
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    for filename, content, mimetype in attachments or []:
        maintype, _, subtype = mimetype.partition("/")
        msg.add_attachment(content, maintype=maintype or "application",
                           subtype=subtype or "octet-stream", filename=filename)
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as s:
            if settings.SMTP_TLS:
                s.starttls()
            if settings.SMTP_USER:
                s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        log.exception("Falha enviando email: %s", e)
        return False
