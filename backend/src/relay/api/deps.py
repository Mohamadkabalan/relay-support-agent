"""FastAPI dependencies: who is calling (sessions), and how often (rate limits)."""

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status

from relay.api.services import Services, get_services
from relay.api.sessions import SessionData

COOKIE = "relay_session"

Svc = Annotated[Services, Depends(get_services)]


@dataclass(frozen=True, slots=True)
class Session:
    token: str
    data: SessionData


@dataclass(frozen=True, slots=True)
class CustomerSession:
    customer_id: uuid.UUID
    visitor_id: str
    token: str


@dataclass(frozen=True, slots=True)
class OperatorSession:
    operator_id: uuid.UUID
    name: str
    visitor_id: str | None  # set for the demo operator: only this visitor's data is visible


async def current_session(request: Request, services: Svc) -> Session | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    data = await services.sessions.load(token)
    return Session(token, data) if data else None


MaybeSession = Annotated[Session | None, Depends(current_session)]


def require_customer(session: MaybeSession) -> CustomerSession:
    if session is None or session.data.customer_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to chat.")
    return CustomerSession(session.data.customer_id, session.data.visitor_id, session.token)


def require_operator(session: MaybeSession) -> OperatorSession:
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in as an operator.")
    data = session.data
    if data.operator_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This needs an operator session.")
    return OperatorSession(
        data.operator_id,
        data.operator_name or "Operator",
        data.visitor_id if data.operator_scoped else None,
    )


CustomerAuth = Annotated[CustomerSession, Depends(require_customer)]
OperatorAuth = Annotated[OperatorSession, Depends(require_operator)]


def set_cookie(response: Response, token: str, services: Services) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=services.settings.session_ttl_s,
        httponly=True,
        secure=services.settings.cookie_secure,
        samesite="lax",
        path="/api",
    )


def clear_cookie(response: Response, services: Services) -> None:
    response.delete_cookie(
        COOKIE, path="/api", secure=services.settings.cookie_secure, httponly=True, samesite="lax"
    )


# ---------------------------------------------------------------- rate limits

MINUTE = 60
DAY = 24 * 3600


def client_ip(request: Request) -> str:
    """The caller's address. Behind Caddy, uvicorn has already resolved X-Forwarded-For."""
    return request.client.host if request.client else "unknown"


async def enforce(services: Services, name: str, subject: str, limit: int, window_s: int) -> None:
    retry_after = await services.limiter.hit(name, subject, limit, window_s)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many requests. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )


async def chat_rate(request: Request, services: Svc) -> None:
    """30 chat requests a minute per IP address, whoever is signed in."""
    limit = services.settings.chat_requests_per_minute
    await enforce(services, "chat", client_ip(request), limit, MINUTE)


async def login_rate(request: Request, services: Svc) -> None:
    """10 logins a minute per IP address: each demo login can create customer rows."""
    await enforce(
        services, "login", client_ip(request), services.settings.logins_per_minute, MINUTE
    )


async def operator_rate(operator: OperatorAuth, services: Svc) -> None:
    limit = services.settings.operator_requests_per_minute
    await enforce(services, "operator", str(operator.operator_id), limit, MINUTE)


async def message_rate(customer: CustomerAuth, services: Svc) -> None:
    """8 messages a minute and 120 a day per visitor (a browser keeps its visitor ID across
    logins, so signing in again doesn't reset the count)."""
    settings = services.settings
    await enforce(
        services, "messages-min", customer.visitor_id, settings.messages_per_minute, MINUTE
    )
    await enforce(services, "messages-day", customer.visitor_id, settings.messages_per_day, DAY)
