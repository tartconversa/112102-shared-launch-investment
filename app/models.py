"""来源记录的数据模型。所有金额/单位均为合成数据，整数口径。"""

from __future__ import annotations

from dataclasses import dataclass, field


def _int(value: str) -> int:
    return int(value) if value not in (None, "") else 0


@dataclass(frozen=True)
class PilotRow:
    service: str
    zone: str
    operator: str
    customer: str
    offered: int
    actual: int
    quality: str  # accepted | pending
    service_day: int
    evidence_day: int
    origin: str = "source"

    @classmethod
    def from_row(cls, row: dict, origin: str) -> "PilotRow":
        return cls(
            row["service"], row["zone"], row["operator"], row["customer"],
            _int(row["offered"]), _int(row["actual"]), row["quality"],
            _int(row["service_day"]), _int(row["evidence_day"]), origin,
        )


@dataclass(frozen=True)
class CommitmentRow:
    offer: str
    customer: str
    zone: str
    operator: str
    shared_claim: str
    units: int
    state: str  # binding | show_interest | withdrawn
    known_day: int
    alternative_to: str
    origin: str = "source"

    @classmethod
    def from_row(cls, row: dict, origin: str) -> "CommitmentRow":
        return cls(
            row["offer"], row["customer"], row["zone"], row["operator"],
            row["shared_claim"], _int(row["units"]), row["state"],
            _int(row["known_day"]), row.get("alternative_to", "") or "", origin,
        )


@dataclass(frozen=True)
class OptionRow:
    project: str
    zone: str
    stage: str  # base | extension
    period: int
    cost: int
    deposit: int
    capacity: int
    dependencies: tuple[str, ...]
    shared_units: int
    origin: str = "source"

    @classmethod
    def from_row(cls, row: dict, origin: str) -> "OptionRow":
        deps = tuple(d.strip() for d in row["dependencies"].split("|") if d.strip())
        return cls(
            row["project"], row["zone"], row["stage"], _int(row["period"]),
            _int(row["cost"]), _int(row["deposit"]), _int(row["capacity"]),
            deps, _int(row["shared_units"]), origin,
        )


@dataclass(frozen=True)
class FundingRow:
    funder: str
    period: int
    cash: int
    in_kind: int
    shared_units: int
    origin: str = "source"

    @classmethod
    def from_row(cls, row: dict, origin: str) -> "FundingRow":
        return cls(
            row["funder"], _int(row["period"]), _int(row["cash"]),
            _int(row["in_kind"]), _int(row["shared_units"]), origin,
        )


@dataclass(frozen=True)
class AgreementRow:
    agreement: str
    project: str
    owner: str
    state: str  # signed | pending | ...
    known_day: int
    origin: str = "source"

    @classmethod
    def from_row(cls, row: dict, origin: str) -> "AgreementRow":
        return cls(
            row["agreement"], row["project"], row["owner"], row["state"],
            _int(row["known_day"]), origin,
        )


@dataclass(frozen=True)
class EventRow:
    event: str
    project: str
    known_day: int
    reporter: str  # funder | district | operator
    observed_capacity: int
    service_ref: str
    state: str  # deposit | partial | accepted
    cash: int
    origin: str = "source"

    @classmethod
    def from_row(cls, row: dict, origin: str) -> "EventRow":
        return cls(
            row["event"], row["project"], _int(row["known_day"]), row["reporter"],
            _int(row["observed_capacity"]), row.get("service_ref", "") or "",
            row["state"], _int(row["cash"]), origin,
        )


TABLE_MODELS = {
    "pilot_service": PilotRow,
    "customer_commitments": CommitmentRow,
    "investment_options": OptionRow,
    "funding": FundingRow,
    "agreements": AgreementRow,
    "stage_events": EventRow,
}

# 各表用于去重/更正的主键（同一主键保留 known_day 最新且来源优先级最高的记录）
PRIMARY_KEYS = {
    "pilot_service": ("service",),
    "customer_commitments": ("offer",),
    "investment_options": ("project",),
    "funding": ("funder", "period"),
    "agreements": ("agreement",),
    "stage_events": ("event",),
}
