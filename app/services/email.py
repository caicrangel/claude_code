import logging
import smtplib
from email.message import EmailMessage

from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal

log = logging.getLogger(__name__)

Attachment = tuple[str, bytes, str]  # (filename, content, mimetype "type/subtype")
InlineImage = tuple[str, bytes, str]  # (cid, content, mimetype) — referenciado no HTML por cid:<cid>


def _smtp_efetivo(db: Session | None) -> dict:
    """Lê SMTP do banco (com fallback .env). Se nenhum db disponível
    (chamadas de teste antigas), volta direto pro .env."""
    if db is None:
        return {
            "host": settings.SMTP_HOST,
            "port": settings.SMTP_PORT,
            "user": settings.SMTP_USER,
            "password": settings.SMTP_PASSWORD,
            "from": settings.SMTP_FROM,
            "tls": settings.SMTP_TLS,
        }
    # Import local para evitar ciclo (runtime_config importa models).
    from .. import runtime_config
    return runtime_config.get_smtp(db)


def send_email(
    to: str,
    subject: str,
    body: str,
    *,
    html_body: str | None = None,
    attachments: list[Attachment] | None = None,
    inline_images: list[InlineImage] | None = None,
    db: Session | None = None,
) -> bool:
    """Envia e-mail com corpo texto + alternativa HTML opcional + anexos opcionais.

    `inline_images` são imagens embutidas (ex.: logo) referenciadas no HTML
    por `cid:<cid>` — método confiável em Gmail/Outlook (data: URI é bloqueado).

    `db` é usado para ler a configuração SMTP do banco (com fallback .env).
    Quando omitido, abre uma sessão própria — assim handlers/jobs que não
    têm `db` em mãos continuam funcionando sem mudança.
    """
    owns_db = db is None
    if owns_db:
        db = SessionLocal()
    try:
        cfg = _smtp_efetivo(db)
    finally:
        if owns_db and db is not None:
            db.close()

    if not cfg["host"] or not to:
        log.warning("SMTP não configurado ou destinatário vazio - email ignorado")
        return False

    msg = EmailMessage()
    msg["From"] = cfg["from"] or settings.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
        # Imagens inline (cid) ficam num multipart/related junto da parte HTML.
        if inline_images:
            html_part = msg.get_payload()[-1]  # a alternativa HTML
            for cid, content, mimetype in inline_images:
                maintype, _, subtype = mimetype.partition("/")
                html_part.add_related(content, maintype=maintype or "image",
                                      subtype=subtype or "png")
                related = html_part.get_payload()[-1]
                related.add_header("Content-ID", f"<{cid}>")
    for filename, content, mimetype in attachments or []:
        maintype, _, subtype = mimetype.partition("/")
        msg.add_attachment(content, maintype=maintype or "application",
                           subtype=subtype or "octet-stream", filename=filename)
    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as s:
            if cfg["tls"]:
                s.starttls()
            if cfg["user"]:
                s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        log.exception("Falha enviando email: %s", e)
        return False
