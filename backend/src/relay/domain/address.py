"""Postal addresses: validated and normalized before the policy ever sees them."""

import re
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

US_STATES = frozenset(
    [
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "DC",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "PR",
    ]
)
_US_ZIP = re.compile(r"^[0-9]{5}(-[0-9]{4})?$")

Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
PostalCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=12)]
Code = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, min_length=2, max_length=2),
]


class Address(BaseModel):
    """A shipping address.

    Any two-letter country passes validation, so that the policy (ADR-4), not a schema error, tells
    the customer we don't ship abroad. US addresses must have a real state code and a ZIP code.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    recipient_name: Annotated[Line, Field(description="Who receives the package.")]
    line1: Annotated[Line, Field(description="Street address.")]
    line2: Annotated[
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None,
        Field(description="Apartment, suite or unit, if any."),
    ] = None
    city: Short
    state: Annotated[Short, Field(description="Two-letter state code for US addresses, e.g. NY.")]
    postal_code: Annotated[PostalCode, Field(description="ZIP code for US addresses, e.g. 10001.")]
    country: Annotated[Code, Field(description="Two-letter country code.")] = "US"

    @model_validator(mode="after")
    def _check_us_formats(self) -> Self:
        if self.country == "US":
            if self.state.upper() not in US_STATES:
                raise ValueError(
                    f"{self.state!r} is not a US state code (use two letters, e.g. NY)"
                )
            if not _US_ZIP.match(self.postal_code):
                raise ValueError(f"{self.postal_code!r} is not a ZIP code like 12345")
        return self

    def normalized(self) -> Address:
        """Collapse whitespace and upper-case US state codes, so equal addresses compare equal."""
        line2 = _squash(self.line2) if self.line2 else None
        return Address(
            recipient_name=_squash(self.recipient_name),
            line1=_squash(self.line1),
            line2=line2 or None,
            city=_squash(self.city),
            state=self.state.upper() if self.country == "US" else _squash(self.state),
            postal_code=self.postal_code,
            country=self.country,
        )

    def one_line(self) -> str:
        parts = [self.recipient_name, self.line1, self.line2, self.city]
        region = f"{self.state} {self.postal_code}"
        text = ", ".join(part for part in parts if part)
        suffix = "" if self.country == "US" else f", {self.country}"
        return f"{text}, {region}{suffix}"


def _squash(value: str) -> str:
    return " ".join(value.split())
