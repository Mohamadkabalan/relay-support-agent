"""The policy configuration: `policy.yaml`, validated.

The file ships inside the package, so the container image, the tests and the help-article renderer
all read the same values.
"""

from functools import cached_property, lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field

from relay.domain.enums import OrderStatus, ShipmentStatus

NonReturnableClass = Literal["final_sale", "gift_card", "digital"]


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ReturnsPolicy(_Section):
    window_days: int = Field(gt=0)
    window_days_refurbished: int = Field(gt=0)
    non_returnable: tuple[NonReturnableClass, ...]


class RefundsPolicy(_Section):
    auto_approve_limit_cents: int = Field(ge=0)
    velocity_refunds_before_review: int = Field(gt=0)
    velocity_window_days: int = Field(gt=0)
    lost_package_days_without_scan: int = Field(gt=0)
    late_delivery_grace_days: int = Field(ge=0)
    settles_in: str


class ShippingPolicy(_Section):
    standard_cents: int = Field(ge=0)
    express_cents: int = Field(ge=0)
    free_standard_from_cents: int = Field(ge=0)
    standard_promise_days: int = Field(gt=0)
    express_promise_days: int = Field(gt=0)


class AddressChangePolicy(_Section):
    allowed_order_statuses: tuple[OrderStatus, ...]
    approval_carrier_statuses: tuple[ShipmentStatus, ...]
    supported_countries: tuple[str, ...]


class ConfirmationPolicy(_Section):
    token_ttl_minutes: int = Field(gt=0)


class ApprovalsPolicy(_Section):
    expire_after_hours: int = Field(gt=0)
    expected_decision: str


class EscalationPolicy(_Section):
    response_time_text: str


class PolicyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    store_timezone: str
    returns: ReturnsPolicy
    refunds: RefundsPolicy
    shipping: ShippingPolicy
    address_changes: AddressChangePolicy
    confirmation: ConfirmationPolicy
    approvals: ApprovalsPolicy
    escalation: EscalationPolicy

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.store_timezone)

    def lookup(self, dotted: str) -> Any:
        """Resolve a placeholder such as `returns.window_days`. Raises KeyError if unknown."""
        node: Any = self
        for part in dotted.split("."):
            if not isinstance(node, BaseModel) or part not in type(node).model_fields:
                raise KeyError(dotted)
            node = getattr(node, part)
        if isinstance(node, BaseModel):
            raise KeyError(dotted)
        return node


def load_policy(path: Path | None = None) -> PolicyConfig:
    """Load and validate a policy file. With no path, the packaged `policy.yaml`."""
    if path is None:
        text = resources.files("relay.domain").joinpath("policy.yaml").read_text("utf-8")
    else:
        text = path.read_text("utf-8")
    return PolicyConfig.model_validate(yaml.safe_load(text))


@lru_cache(maxsize=1)
def default_policy() -> PolicyConfig:
    return load_policy()
