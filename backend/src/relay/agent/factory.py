"""Building an agent from settings: the provider, the tool connection and the store."""

import uuid

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.agent.events import Listener
from relay.agent.locks import LocalLocks, Locks
from relay.agent.loop import Agent, AgentLimits
from relay.agent.providers.anthropic import AnthropicProvider, PrefixMismatch
from relay.agent.providers.base import ModelProvider
from relay.agent.providers.openai import OpenAIProvider
from relay.agent.spend import SpendGuard
from relay.agent.store import ConversationStore
from relay.agent.tools import HttpTools, InProcessTools, ToolConnector
from relay.clock import Clock, SystemClock
from relay.config import Settings
from relay.domain.policy_config import PolicyConfig, default_policy
from relay.domain.scope import CustomerScope
from relay.kb.embeddings import Embedder
from relay.mcp_server.identity import BoundIdentity
from relay.mcp_server.runtime import ToolRuntime

# The SDK retries 408, 409, 429, 5xx and connection errors with backoff (§5.6).
MAX_RETRIES = 2
TIMEOUT_S = 60.0

OPENAI_EFFORTS = {"low", "medium", "high"}

# The in-process runtime's identity until the first turn binds a real customer.
_NOBODY = uuid.UUID(int=0)


class MissingCredentialsError(RuntimeError):
    pass


def build_provider(
    settings: Settings, *, prefix_mismatch: PrefixMismatch | None = None
) -> ModelProvider:
    if settings.provider == "openai":
        if settings.openai_api_key is None:
            raise MissingCredentialsError("Set OPENAI_API_KEY to use RELAY_PROVIDER=openai.")
        openai_client = AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            max_retries=MAX_RETRIES,
            timeout=TIMEOUT_S,
        )
        effort = settings.agent_effort if settings.agent_effort in OPENAI_EFFORTS else "high"
        return OpenAIProvider(openai_client, model=settings.openai_model, effort=effort)
    if settings.anthropic_api_key is None:
        raise MissingCredentialsError("Set ANTHROPIC_API_KEY (in .env) to talk to Claude.")
    client = AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        max_retries=MAX_RETRIES,
        timeout=TIMEOUT_S,
    )
    return AnthropicProvider(
        client,
        model=settings.agent_model,
        effort=settings.agent_effort,
        fallbacks=settings.agent_fallbacks,
        prefix_mismatch=prefix_mismatch or settings.thinking_prefix_mismatch,
    )


def http_tools(settings: Settings) -> HttpTools:
    if settings.mcp_jwt_secret is None:
        raise MissingCredentialsError("Set RELAY_MCP_JWT_SECRET to call the MCP server over HTTP.")
    return HttpTools(settings.mcp_url, settings.mcp_jwt_secret.get_secret_value())


def in_process_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    *,
    clock: Clock,
    policy: PolicyConfig,
    store_name: str,
) -> InProcessTools:
    runtime = ToolRuntime(
        sessionmaker=sessionmaker,
        identity=BoundIdentity(CustomerScope(customer_id=_NOBODY)),  # replaced on every connect
        embedder=embedder,
        clock=clock,
        policy=policy,
    )
    return InProcessTools(runtime, store_name)


def build_agent(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    provider: ModelProvider,
    tools: ToolConnector,
    clock: Clock | None = None,
    policy: PolicyConfig | None = None,
    locks: Locks | None = None,
    limits: AgentLimits | None = None,
    listener: Listener | None = None,
    spend: SpendGuard | None = None,
    trace_content: bool = False,
) -> Agent:
    clock = clock or SystemClock()
    policy = policy or default_policy()
    secret = settings.mcp_jwt_secret
    return Agent(
        store=ConversationStore(sessionmaker, clock, policy),
        provider=provider,
        tools=tools,
        locks=locks or LocalLocks(),
        clock=clock,
        policy=policy,
        store_name=settings.store_name,
        limits=limits or AgentLimits(),
        user_key_secret=secret.get_secret_value() if secret else None,
        listener=listener,
        spend=spend,
        trace_content=trace_content,
    )
