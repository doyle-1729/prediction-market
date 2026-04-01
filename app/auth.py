import os
import bcrypt
from itsdangerous import URLSafeTimedSerializer
from fastapi import Request, HTTPException
from sqlalchemy.orm import Session

from app.models import User

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
serializer = URLSafeTimedSerializer(SECRET_KEY)
SESSION_COOKIE = "pm_session"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_session(response, user_id: int):
    token = serializer.dumps(user_id, salt="session")
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30
    )


def get_current_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        user_id = serializer.loads(token, salt="session", max_age=60 * 60 * 24 * 30)
    except Exception:
        return None
    return db.get(User, user_id)


def require_user(request: Request, db: Session) -> User:
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user
