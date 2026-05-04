from fastapi import Request, HTTPException, Depends
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .database import get_db
from .models import Usuario

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(p: str) -> str:
    return pwd_ctx.hash(p)


def verify_password(p: str, h: str) -> bool:
    return pwd_ctx.verify(p, h)


def current_user(request: Request, db: Session = Depends(get_db)) -> Usuario:
    uid = request.session.get("uid")
    if not uid:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    user = db.query(Usuario).filter(Usuario.id == uid, Usuario.ativo.is_(True)).first()
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> Usuario | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.query(Usuario).filter(Usuario.id == uid, Usuario.ativo.is_(True)).first()
