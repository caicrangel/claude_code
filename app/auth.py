"""Autenticação via banco. Senhas com bcrypt (passlib)."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .database import get_db
from .models import Usuario

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

ROLE_ADMIN = "admin"
ROLE_COMUM = "comum"


def hash_senha(plain: str) -> str:
    return pwd.hash(plain)


def verificar_senha(plain: str, hashed: str) -> bool:
    try:
        return pwd.verify(plain, hashed)
    except Exception:  # noqa: BLE001
        return False


def autenticar(db: Session, email: str, senha: str) -> Usuario | None:
    email = (email or "").strip().lower()
    user = db.query(Usuario).filter(Usuario.email == email).first()
    if user is None or not user.ativo:
        return None
    if not verificar_senha(senha, user.senha_hash):
        return None
    return user


def current_user(request: Request, db: Session = Depends(get_db)) -> Usuario | None:
    uid = request.session.get("user_id")
    if not uid:
        return None
    user = db.get(Usuario, uid)
    if user is None or not user.ativo:
        return None
    return user


def require_login(request: Request, db: Session = Depends(get_db)) -> Usuario:
    user = current_user(request, db)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> Usuario:
    user = require_login(request, db)
    if user.role != ROLE_ADMIN:
        raise HTTPException(status_code=303, headers={"Location": "/dashboard"})
    return user
