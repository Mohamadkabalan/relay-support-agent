"""Sign-in (§11, D4).

- `POST /api/auth/demo` signs in as a persona. In demo mode the visitor gets a private copy of it;
  otherwise it is the seeded persona (local development). Real customer authentication is out of
  scope (§2): this login stands in for the store's own session.
- `POST /api/auth/operator` checks the operator's password against an Argon2 hash from the
  environment.
- `POST /api/auth/demo-operator` (demo mode only) opens the console on this visitor's own data.

Every login issues a new session token; the old one is deleted.
"""

import asyncio
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.api.deps import MaybeSession, Session, Svc, clear_cookie, login_rate, set_cookie
from relay.api.services import Services
from relay.api.sessions import SessionData
from relay.db.models import Customer, Operator
from relay.seed.clones import UnknownPersonaError, new_visitor_id, seeded_persona, visitor_copy
from relay.seed.personas import PERSONAS

router = APIRouter(prefix="/api/auth", tags=["auth"])

PersonaKey = Literal["dana", "marcus", "priya", "leo", "sam", "alex"]
DEMO_OPERATOR_NAME = "Demo operator"
_hasher = PasswordHasher()


class PersonaView(BaseModel):
    key: str
    name: str
    story: str


class CustomerInfo(BaseModel):
    name: str
    persona: str | None


class OperatorInfo(BaseModel):
    name: str
    scoped_to_visitor: bool


class SessionView(BaseModel):
    demo_mode: bool
    customer: CustomerInfo | None
    operator: OperatorInfo | None


class DemoLogin(BaseModel):
    persona: PersonaKey


class OperatorLogin(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)


@router.get("/personas")
async def personas() -> list[PersonaView]:
    return [PersonaView(key=p.key, name=p.full_name, story=p.story) for p in PERSONAS]


@router.get("/session")
async def session_info(session: MaybeSession, services: Svc) -> SessionView:
    return await _view(services, session.data if session else None)


@router.post("/demo", dependencies=[Depends(login_rate)])
async def demo_login(
    body: DemoLogin, session: MaybeSession, services: Svc, response: Response
) -> SessionView:
    data = _base(session)
    async with services.sessionmaker() as db, db.begin():
        try:
            if services.settings.demo_mode:
                customer = await visitor_copy(
                    db, body.persona, data.visitor_id, services.clock.now(), services.policy
                )
            else:
                found = await seeded_persona(db, body.persona)
                if found is None:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT, "The personas aren't seeded yet: run make seed."
                    )
                customer = found
        except UnknownPersonaError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such persona.") from error
        data = data.model_copy(update={"customer_id": customer.id, "persona": body.persona})
    return await _sign_in(services, session, data, response)


@router.post("/operator", dependencies=[Depends(login_rate)])
async def operator_login(
    body: OperatorLogin, session: MaybeSession, services: Svc, response: Response
) -> SessionView:
    settings = services.settings
    stored = settings.operator_password_hash
    if stored is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Operator login is not configured: set RELAY_OPERATOR_PASSWORD_HASH.",
        )
    email_ok = body.email.strip().lower() == settings.operator_email.lower()
    password_ok = await asyncio.to_thread(_verify, stored.get_secret_value(), body.password)
    if not (email_ok and password_ok):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password.")
    async with services.sessionmaker() as db, db.begin():
        operator = await _operator(db, settings.operator_email.lower(), "Operator")
    data = _base(session).model_copy(
        update={
            "operator_id": operator.id,
            "operator_name": operator.display_name,
            "operator_scoped": False,
        }
    )
    return await _sign_in(services, session, data, response)


@router.post("/demo-operator", dependencies=[Depends(login_rate)])
async def demo_operator_login(
    session: MaybeSession, services: Svc, response: Response
) -> SessionView:
    if not services.settings.demo_mode:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "The demo operator exists only in demo mode."
        )
    data = _base(session)
    async with services.sessionmaker() as db, db.begin():
        email = f"demo-operator+{data.visitor_id}@relay.example"
        operator = await _operator(db, email, DEMO_OPERATOR_NAME)
    data = data.model_copy(
        update={
            "operator_id": operator.id,
            "operator_name": operator.display_name,
            "operator_scoped": True,
        }
    )
    return await _sign_in(services, session, data, response)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(session: MaybeSession, services: Svc, response: Response) -> None:
    if session is not None:
        await services.sessions.delete(session.token)
    clear_cookie(response, services)


def _base(session: Session | None) -> SessionData:
    return session.data if session else SessionData(visitor_id=new_visitor_id())


async def _sign_in(
    services: Services, old: Session | None, data: SessionData, response: Response
) -> SessionView:
    token = await services.sessions.create(data)
    if old is not None:
        await services.sessions.delete(old.token)
    set_cookie(response, token, services)
    return await _view(services, data)


async def _view(services: Services, data: SessionData | None) -> SessionView:
    customer = operator = None
    if data is not None and data.customer_id is not None:
        async with services.sessionmaker() as db:
            name = await db.scalar(
                select(Customer.full_name).where(Customer.id == data.customer_id)
            )
        customer = CustomerInfo(name=name or "", persona=data.persona)
    if data is not None and data.operator_id is not None:
        operator = OperatorInfo(
            name=data.operator_name or "Operator", scoped_to_visitor=data.operator_scoped
        )
    return SessionView(demo_mode=services.settings.demo_mode, customer=customer, operator=operator)


async def _operator(db: AsyncSession, email: str, name: str) -> Operator:
    operator = await db.scalar(select(Operator).where(Operator.email == email))
    if operator is None:
        operator = Operator(email=email, display_name=name)
        db.add(operator)
        await db.flush()
    return operator


def _verify(stored: str, password: str) -> bool:
    try:
        return _hasher.verify(stored, password)
    except VerificationError, InvalidHashError:
        return False
