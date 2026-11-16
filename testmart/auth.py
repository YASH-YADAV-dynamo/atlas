import hashlib
import hmac

from fastapi import Request

from testmart.config import load

COOKIE = "tm_session"
USER = "admin"


class LoginRequired(Exception):
    """Raised when a page needs a session; the app turns it into a redirect to /login."""


def _sign(value: str, secret: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    return f"{USER}.{_sign(USER, load().secret)}"


def check_credentials(username: str, password: str) -> bool:
    cfg = load()
    return hmac.compare_digest(username, USER) and hmac.compare_digest(password, cfg.password)


def require_user(request: Request) -> str:
    token = request.cookies.get(COOKIE, "")
    user, _, sig = token.partition(".")
    if user != USER or not hmac.compare_digest(sig, _sign(user, load().secret)):
        raise LoginRequired
    return user
