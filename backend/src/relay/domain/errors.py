"""Failures, as opposed to policy outcomes (ADR-0006).

A refund the policy forbids is a *decision* (`status: denied`), returned as data, because the agent
should explain it to the customer. The errors here mean the request itself was wrong: an order that
isn't on this account, a line that doesn't exist, a stale confirmation token. The MCP layer turns
them into `ToolError("[CODE] message hint")`, which the model sees as `is_error: true` and can
recover from.
"""

from typing import ClassVar


class DomainError(Exception):
    code: ClassVar[str] = "INTERNAL"
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        text = f"[{self.code}] {self.message}"
        return f"{text} {self.hint}" if self.hint else text


class InvalidArgumentError(DomainError):
    code = "INVALID_ARGUMENT"


class OrderNotFoundError(DomainError):
    """Raised both for unknown orders and for other customers' orders, with the same message."""

    code = "ORDER_NOT_FOUND"

    def __init__(self, order_number: str) -> None:
        super().__init__(
            f"No order {order_number} on this account.",
            "Ask the customer to check the number, or call list_orders.",
        )
        self.order_number = order_number


class LineNotFoundError(DomainError):
    code = "LINE_NOT_FOUND"

    def __init__(self, order_number: str, line_no: int) -> None:
        super().__init__(
            f"Order {order_number} has no line {line_no}.",
            "Use the line numbers from get_order.",
        )


class ConfirmationInvalidError(DomainError):
    code = "CONFIRMATION_INVALID"


class ConfirmationTooEarlyError(DomainError):
    code = "CONFIRMATION_TOO_EARLY"

    def __init__(self) -> None:
        super().__init__(
            "This confirmation token was issued in the current turn.",
            "Show the preview to the customer and wait for them to confirm in their next message.",
        )


class ConflictError(DomainError):
    code = "CONFLICT"


class NotFoundError(DomainError):
    """An operator-side lookup (approval, escalation) that doesn't exist."""

    code = "NOT_FOUND"
