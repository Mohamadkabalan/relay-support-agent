"""Who may call what, and how much (PLAN.md §11, Phase 5 acceptance criteria)."""

import os
import secrets

import httpx2
from argon2 import PasswordHasher
from tests.conftest import make_settings
from tests.support.api import ORIGIN, running_api

from relay.agent.providers.scripted import Reply
from relay.api.app import create_app
from relay.clock import FrozenClock


async def sign_in(client: httpx2.AsyncClient, persona: str = "dana") -> None:
    response = await client.post("/api/auth/demo", json={"persona": persona})
    assert response.status_code == 200, response.text


async def test_a_customer_cannot_reach_another_customers_conversation(
    api_database: str, clock: FrozenClock
) -> None:
    async with (
        running_api(api_database, [Reply(text="Hi Dana!")], clock=clock) as api,
        api.client() as alice,
        api.client() as bob,
        api.client() as nobody,
    ):
        await sign_in(alice)
        await sign_in(bob)  # another visitor's private copy of the same persona
        created = await alice.post("/api/conversations")
        assert created.status_code == 201
        conversation = created.json()["id"]
        path = f"/api/conversations/{conversation}"
        assert (await alice.post(f"{path}/messages", json={"text": "Hi"})).status_code == 202
        await api.idle()

        assert (await alice.get(path)).status_code == 200
        for response in (
            await bob.get(path),
            await bob.get(f"{path}/events"),
            await bob.post(f"{path}/messages", json={"text": "x"}),
            await bob.post(f"{path}/feedback", json={"rating": "up"}),
            await bob.post(f"{path}/close"),
        ):
            assert response.status_code == 404, response.text
        assert (await bob.get("/api/conversations")).json() == []
        assert (await nobody.get(path)).status_code == 401
        assert (await nobody.post("/api/conversations")).status_code == 401
    assert len(api.provider.requests) == 1  # only Alice's message reached the model


async def test_operator_routes_need_an_operator_session(
    api_database: str, clock: FrozenClock
) -> None:
    async with (
        running_api(api_database, clock=clock) as api,
        api.client() as nobody,
        api.client() as customer,
        api.client() as operator,
    ):
        await sign_in(customer)
        assert (await operator.post("/api/auth/demo-operator")).status_code == 200
        for path in ("/api/console/conversations", "/api/console/approvals"):
            assert (await nobody.get(path)).status_code == 401
            assert (await customer.get(path)).status_code == 403
            assert (await operator.get(path)).status_code == 200
        decision = {"decision": "approve"}
        post = "/api/console/approvals/APR-10001/decision"
        assert (await nobody.post(post, json=decision)).status_code == 401
        assert (await customer.post(post, json=decision)).status_code == 403
        assert (await operator.post(post, json=decision)).status_code == 404


async def test_the_demo_operator_sees_only_its_own_visitor(
    api_database: str, clock: FrozenClock
) -> None:
    async with (
        running_api(api_database, clock=clock) as api,
        api.client() as visitor,
        api.client() as other,
    ):
        await sign_in(visitor)
        conversation = (await visitor.post("/api/conversations")).json()["id"]
        assert (await visitor.post("/api/auth/demo-operator")).status_code == 200
        assert (await other.post("/api/auth/demo-operator")).status_code == 200

        mine = await visitor.get("/api/console/conversations")
        assert [row["id"] for row in mine.json()] == [conversation]
        assert (await visitor.get(f"/api/console/conversations/{conversation}")).status_code == 200
        assert (await other.get("/api/console/conversations")).json() == []
        theirs = await other.get(f"/api/console/conversations/{conversation}")
        assert theirs.status_code == 404


async def test_the_operator_signs_in_with_the_password_and_sees_everything(
    api_database: str, clock: FrozenClock
) -> None:
    password = "correct horse battery staple"
    hashed = PasswordHasher().hash(password)
    async with (
        running_api(api_database, clock=clock, operator_password_hash=hashed) as api,
        api.client() as visitor,
        api.client() as operator,
    ):
        await sign_in(visitor)
        conversation = (await visitor.post("/api/conversations")).json()["id"]

        wrong = {"email": "operator@relay.example", "password": "guess"}
        assert (await operator.post("/api/auth/operator", json=wrong)).status_code == 401
        right = {"email": "Operator@Relay.example", "password": password}
        signed_in = await operator.post("/api/auth/operator", json=right)
        assert signed_in.status_code == 200
        assert signed_in.json()["operator"] == {"name": "Operator", "scoped_to_visitor": False}

        listed = await operator.get("/api/console/conversations")
        assert conversation in [row["id"] for row in listed.json()]
        assert (await operator.post("/api/auth/logout")).status_code == 204
        assert (await operator.get("/api/console/conversations")).status_code == 401


async def test_over_the_rate_limit_is_429(api_database: str, clock: FrozenClock) -> None:
    replies = [Reply(text="One."), Reply(text="Two.")]
    async with (
        running_api(
            api_database, replies, clock=clock, messages_per_minute=2, logins_per_minute=3
        ) as api,
        api.client() as client,
    ):
        await sign_in(client)
        conversation = (await client.post("/api/conversations")).json()["id"]
        path = f"/api/conversations/{conversation}/messages"
        for text in ("one", "two"):
            assert (await client.post(path, json={"text": text})).status_code == 202
            await api.idle()
        third = await client.post(path, json={"text": "three"})
        assert third.status_code == 429
        assert int(third.headers["Retry-After"]) > 0

        logins = [
            (await client.post("/api/auth/demo", json={"persona": "leo"})).status_code
            for _ in range(3)
        ]
        assert logins == [200, 200, 429]  # the first sign-in above counted too
    assert len(api.provider.requests) == 2


async def test_oversized_requests_are_413(api_database: str, clock: FrozenClock) -> None:
    async with running_api(api_database, clock=clock) as api, api.client() as client:
        await sign_in(client)
        conversation = (await client.post("/api/conversations")).json()["id"]
        path = f"/api/conversations/{conversation}/messages"

        too_long = await client.post(path, json={"text": "x" * 2001})
        assert too_long.status_code == 413
        too_big = await client.post(path, content=b'{"text": "' + b"y" * 20_000 + b'"}')
        assert too_big.status_code == 413
        assert not await api.services.store.has_waiting(conversation)
    assert api.provider.requests == []


async def test_a_cross_site_post_is_refused(api_database: str, clock: FrozenClock) -> None:
    async with running_api(api_database, clock=clock) as api, api.client() as client:
        evil = {"Origin": "https://evil.example"}
        refused = await client.post("/api/auth/demo", json={"persona": "dana"}, headers=evil)
        assert refused.status_code == 403
        assert (await client.get("/api/auth/session")).json()["customer"] is None
        allowed = await client.post(
            "/api/auth/demo", json={"persona": "dana"}, headers={"Origin": ORIGIN}
        )
        assert allowed.status_code == 200


async def test_without_a_model_the_chat_answers_503(api_database: str) -> None:
    settings = make_settings(
        database_url=api_database,
        redis_url=os.environ["REDIS_URL"],
        redis_prefix=f"relay-test-{secrets.token_hex(4)}:",
        demo_mode=True,
        cookie_secure=False,
    )
    app = create_app(settings)  # no provider override, and these settings have no model key
    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        await sign_in(client)
        response = await client.post("/api/conversations")
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]
