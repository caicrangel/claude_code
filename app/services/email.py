import logging
import smtplib
import time
from email.message import EmailMessage

from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal

log = logging.getLogger(__name__)

TENTATIVAS = 3          # total de tentativas de envio SMTP
ESPERA_RETRY_SEG = 5    # espera entre tentativas


def registrar_envio(canal: str, destinatario: str, assunto: str,
                    ok: bool, erro: str = "") -> None:
    """Grava a tentativa no histórico (tabela log_envio). Sessão própria e
    curta — nunca interfere na transação de quem chamou; falha aqui só loga."""
    from ..models import LogEnvio
    try:
        db = SessionLocal()
        try:
            db.add(LogEnvio(canal=canal, destinatario=(destinatario or "")[:255],
                            assunto=(assunto or "")[:255], ok=ok,
                            erro=(erro or "")[:2000]))
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        log.exception("Falha registrando envio no histórico")

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
        registrar_envio("email", to, subject, ok=False,
                        erro="SMTP não configurado ou destinatário vazio")
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
    ultimo_erro = ""
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as s:
                if cfg["tls"]:
                    s.starttls()
                if cfg["user"]:
                    s.login(cfg["user"], cfg["password"])
                s.send_message(msg)
            registrar_envio("email", to, subject, ok=True)
            return True
        except Exception as e:  # noqa: BLE001
            ultimo_erro = f"{type(e).__name__}: {e}"
            log.warning("Falha enviando email (tentativa %d/%d): %s",
                        tentativa, TENTATIVAS, ultimo_erro)
            if tentativa < TENTATIVAS:
                time.sleep(ESPERA_RETRY_SEG)
    log.error("Email para %s NÃO enviado após %d tentativas: %s",
              to, TENTATIVAS, ultimo_erro)
    registrar_envio("email", to, subject, ok=False, erro=ultimo_erro)
    return False
