import secrets

from fastapi import Request, HTTPException

from .config import settings


def check_credentials(email: str, senha: str) -> bool:
    ok_email = secrets.compare_digest(email.strip().lower(), settings.AUTH_EMAIL.strip().lower())
    ok_senha = secrets.compare_digest(senha, settings.AUTH_PASSWORD)
    return ok_email and ok_senha


def require_login(request: Request) -> None:
    if not request.session.get("auth"):
        raise HTTPException(status_code=303, headers={"Location": "/login"})
