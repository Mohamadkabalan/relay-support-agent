"""'Create eval scenario' (§10): draft a scenario from a conversation, then review it.

Drafting is one model call of up to a minute, so it runs in the background: the POST answers 202
with a `generating` draft, and the console polls it until it is `valid`, `invalid` (with the
problems) or `failed`. The call counts against the daily spend cap like any other. The demo
operator sees only the drafts it made.
"""

import logging
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from relay.agent.spend import SpendCapReachedError, worst_case_for
from relay.api.deps import OperatorAuth, OperatorSession, Svc, operator_rate
from relay.api.routes.console import find_conversation
from relay.api.services import Services
from relay.db.models import ScenarioDraft
from relay.domain.enums import DraftStatus
from relay.evals import drafts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/console", tags=["console"], dependencies=[Depends(operator_rate)])


class DraftRequest(BaseModel):
    note: str = Field(min_length=1, max_length=1000)  # what should have happened


class DraftView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    note: str
    status: DraftStatus
    yaml: str | None
    rationale: str | None
    problems: list[str]
    model: str
    cost_micros: int | None
    created_at: datetime
    finished_at: datetime | None


@router.post(
    "/conversations/{conversation_id}/scenario-drafts", status_code=status.HTTP_202_ACCEPTED
)
async def create_draft(
    conversation_id: uuid.UUID, body: DraftRequest, operator: OperatorAuth, services: Svc
) -> DraftView:
    if services.drafter is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Drafting scenarios needs a model: set ANTHROPIC_API_KEY.",
        )
    async with services.sessionmaker() as db, db.begin():
        await find_conversation(db, operator, conversation_id)
        row = ScenarioDraft(
            conversation_id=conversation_id,
            created_by=operator.operator_id,
            note=body.note.strip(),
            status=DraftStatus.GENERATING,
            problems=[],
            model=services.settings.drafter_model,
            created_at=services.clock.now(),
        )
        db.add(row)
        await db.flush()
        view = DraftView.model_validate(row)
    services.spawn(_generate(services, view.id, conversation_id, view.note))
    return view


@router.get("/scenario-drafts")
async def list_drafts(
    operator: OperatorAuth,
    services: Svc,
    conversation_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[DraftView]:
    query = select(ScenarioDraft).order_by(ScenarioDraft.created_at.desc()).limit(100)
    if conversation_id is not None:
        query = query.where(ScenarioDraft.conversation_id == conversation_id)
    if operator.visitor_id is not None:
        query = query.where(ScenarioDraft.created_by == operator.operator_id)
    async with services.sessionmaker() as db:
        return [DraftView.model_validate(row) for row in await db.scalars(query)]


@router.get("/scenario-drafts/{draft_id}")
async def get_draft(draft_id: uuid.UUID, operator: OperatorAuth, services: Svc) -> DraftView:
    async with services.sessionmaker() as db:
        row = await db.get(ScenarioDraft, draft_id)
    if row is None or not _yours(row, operator):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such draft.")
    return DraftView.model_validate(row)


def _yours(row: ScenarioDraft, operator: OperatorSession) -> bool:
    return operator.visitor_id is None or row.created_by == operator.operator_id


async def _generate(
    services: Services, draft_id: uuid.UUID, conversation_id: uuid.UUID, note: str
) -> None:
    values: dict[str, object]
    try:
        async with services.sessionmaker() as db:
            material, pii = await drafts.conversation_material(db, conversation_id)
        model = services.settings.drafter_model
        reservation = None
        if services.spend is not None:
            chars = len(material) + len(drafts.INSTRUCTIONS) + 4000
            reservation = await services.spend.reserve(
                worst_case_for(model, chars, drafts.MAX_TOKENS)
            )
        if services.drafter is None:  # the route checked; a shutdown may have closed it since
            raise RuntimeError("no drafter")
        result = await drafts.draft(services.drafter, model, material, note, pii)
        if services.spend is not None and reservation is not None:
            await services.spend.settle(reservation, result.cost_micros)
        values = {
            "status": DraftStatus.INVALID if result.problems else DraftStatus.VALID,
            "yaml": result.yaml_text or None,
            "rationale": result.rationale or None,
            "problems": result.problems,
            "model": result.model,
            "cost_micros": result.cost_micros,
        }
    except SpendCapReachedError:
        values = {"status": DraftStatus.FAILED, "problems": ["The daily spend cap was reached."]}
    except Exception as error:
        logger.exception("Drafting a scenario from conversation %s failed", conversation_id)
        values = {
            "status": DraftStatus.FAILED,
            "problems": [f"The model call failed ({type(error).__name__})."],
        }
    async with services.sessionmaker() as db, db.begin():
        await db.execute(
            update(ScenarioDraft)
            .where(ScenarioDraft.id == draft_id)
            .values(**values, finished_at=services.clock.now())
        )
