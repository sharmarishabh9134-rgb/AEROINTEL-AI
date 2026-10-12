from datetime import datetime, timedelta, timezone
import secrets
import jwt
from pwdlib import PasswordHash
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.app.config import get_settings
from backend.app.database import get_db
from backend.app.models import Profile

password_hash = PasswordHash.recommended()
bearer = HTTPBearer(auto_error=False)
ALGORITHM = "HS256"
_LOCAL_DEMO_SECRET = secrets.token_urlsafe(48)


def get_jwt_secret() -> str:
    secret = get_settings().jwt_secret_key
    if len(secret) >= 32 and not secret.startswith("replace-"):
        return secret
    if get_settings().app_env.lower() in {"production", "prod"}:
        raise RuntimeError("JWT_SECRET_KEY must be set to a random value of at least 32 characters in production.")
    return _LOCAL_DEMO_SECRET


def create_access_token(profile: Profile) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(profile.id), "role": profile.role, "iat": now, "exp": now + timedelta(minutes=settings.jwt_expire_minutes)}, get_jwt_secret(), algorithm=ALGORITHM)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> Profile:
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(credentials.credentials, get_jwt_secret(), algorithms=[ALGORITHM])
        profile_id = int(payload.get("sub", ""))
    except (jwt.PyJWTError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid or expired access token", headers={"WWW-Authenticate": "Bearer"})
    profile = db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=401, detail="User no longer exists")
    return profile


def require_roles(*roles: str):
    def check(profile: Profile = Depends(current_user)) -> Profile:
        if profile.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return profile
    return check
