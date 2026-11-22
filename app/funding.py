"""分期出资组合分析。

规则（对应出资单位要求）
----------------------
1. **配套协议门槛**：项目缺一份 ``signed`` 配套协议即不可列为已获资金；
   其先行项目缺协议时同样连带不可列。单独排序靠前不能豁免。
2. **共享配套额度**：``shared_units`` 是共同设施的占用额度，按出资方×期
   设上限；先承诺一地会挤占同一出资方同时段给另一地的额度。
3. **资金类型隔离**：``cash`` 与 ``in_kind`` 分别记账，不可互相顶替。
4. **工程押金**：验收(accepted)或退款前不可再次使用，作为冻结现金从
   可出资余额中扣除；验收后释放，已付现金仍计入项目已用款。
5. **组合可行性**：逐期枚举依赖封闭的项目组合，协议、现金、实物、共享
   额度任一不满足即列为受阻组合并给出未满足约束，不用重复承诺或未到账
   资金垫高得分。
6. **分期验收门槛**：只有区县(district)与运营方(operator)双方都出具
   ``accepted`` 且引用真实服务记录的项目，才进入下一笔付款；单方
   ``partial`` 仅为部分能力确认，不释放下款。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import demand as demand_mod
from .loader import LoadResult

ACCEPTED = "accepted"
PARTIAL = "partial"
DEPOSIT = "deposit"
DISTRICT = "district"
OPERATOR = "operator"
FUNDER = "funder"
SIGNED = "signed"


# ---------------------------------------------------------------- 协议与门槛

@dataclass
class ProjectAgreement:
    project: str
    has_signed: bool
    signed_day: int | None
    pending_days: list[int]
    owner: str | None


def project_agreements(load: LoadResult) -> dict[str, ProjectAgreement]:
    out: dict[str, ProjectAgreement] = {}
    for project in {o.project for o in load.rows["investment_options"]}:
        rows = [a for a in load.rows["agreements"] if a.project == project]
        signed = [a for a in rows if a.state == SIGNED]
        out[project] = ProjectAgreement(
            project=project,
            has_signed=bool(signed),
            signed_day=max((a.known_day for a in signed), default=None),
            pending_days=sorted(a.known_day for a in rows if a.state != SIGNED),
            owner=signed[0].owner if signed else (rows[0].owner if rows else None),
        )
    return out


@dataclass
class StageStatus:
    project: str
    deposit_events: list[str]
    deposit_total: int
    deposit_released: bool          # 双方验收后押金可释放
    operator_accepted: bool
    district_accepted: bool
    accepted_capacity: int          # 双方认可口径的能力（取小），单方则为 0
    operator_capacity: int
    district_capacity: int
    service_refs: list[str]
    quality_warnings: list[str]
    partial_caps: dict[str, int]    # reporter -> 单方 partial 能力
    next_payment_ready: bool
    blockers: list[str]


def project_stage(load: LoadResult, project: str,
                  agreements: dict[str, ProjectAgreement]) -> StageStatus:
    events = [e for e in load.rows["stage_events"] if e.project == project]
    pilots = {p.service: p for p in load.rows["pilot_service"]}

    deposit_events = [e.event for e in events if e.state == DEPOSIT]
    deposit_total = sum(e.cash for e in events if e.state == DEPOSIT)

    op_accepted = [e for e in events
                   if e.reporter == OPERATOR and e.state == ACCEPTED]
    dt_accepted = [e for e in events
                   if e.reporter == DISTRICT and e.state == ACCEPTED]
    op_cap = max((e.observed_capacity for e in op_accepted), default=0)
    dt_cap = max((e.observed_capacity for e in dt_accepted), default=0)
    service_refs = sorted({e.service_ref for e in op_accepted + dt_accepted
                           if e.service_ref})
    real_evidence = all(e.service_ref and e.service_ref in pilots
                        for e in op_accepted + dt_accepted)

    quality_warnings = []
    for ref in service_refs:
        pilot = pilots.get(ref)
        if pilot is not None and pilot.quality == "pending":
            quality_warnings.append(
                f"{ref} 服务质量证据 pending，付款前须补签收")

    partial_caps: dict[str, int] = {}
    for e in events:
        if e.state == PARTIAL:
            partial_caps[e.reporter] = max(
                partial_caps.get(e.reporter, 0), e.observed_capacity)

    both_accepted = bool(op_accepted) and bool(dt_accepted) and real_evidence
    blockers: list[str] = []
    if not op_accepted:
        blockers.append("缺运营方 accepted 验收")
    if not dt_accepted:
        blockers.append("缺区县 accepted 审核")
    if (op_accepted or dt_accepted) and not real_evidence:
        blockers.append("验收缺少真实服务证据(service_ref)")
    if quality_warnings:
        blockers.append("服务质量证据未签收")
    if not agreements.get(project, ProjectAgreement(
            project, False, None, [], None)).has_signed:
        blockers.append("缺有效配套协议")

    return StageStatus(
        project=project,
        deposit_events=deposit_events,
        deposit_total=deposit_total,
        deposit_released=both_accepted,
        operator_accepted=bool(op_accepted),
        district_accepted=bool(dt_accepted),
        accepted_capacity=min(op_cap, dt_cap) if both_accepted else 0,
        operator_capacity=op_cap,
        district_capacity=dt_cap,
        service_refs=service_refs,
        quality_warnings=quality_warnings,
        partial_caps=partial_caps,
        next_payment_ready=both_accepted and not quality_warnings,
        blockers=blockers,
    )


# ---------------------------------------------------------------- 资金与组合

@dataclass
class FunderPosition:
    funder: str
    period: int
    cash_budget: int
    in_kind_budget: int
    shared_unit_budget: int
    cash_committed_elsewhere: int   # 同周期其他已列资金项目占用
    deposit_frozen: int             # 尚未验收/退款的押金
    cash_available: int
    in_kind_available: int
    units_available: int
    units_committed_elsewhere: int


def funder_positions(load: LoadResult, stages: dict[str, StageStatus],
                     funded_projects: set[str]) -> dict[tuple[str, int], FunderPosition]:
    """按当前事件与"已列资金项目"计算每个出资方×期的可用余额。

    funded_projects: 在评估组合之外、同周期已获准的项目（先承诺先占用）。
    """
    agreements = project_agreements(load)
    options = {o.project: o for o in load.rows["investment_options"]}
    positions: dict[tuple[str, int], FunderPosition] = {}

    for f in load.rows["funding"]:
        key = (f.funder, f.period)
        # 未释放（未验收/未退款）的押金才冻结；只冻结其所属期预算
        frozen = sum(
            st.deposit_total for p, st in stages.items()
            if not st.deposit_released and options[p].period == f.period
            and agreements.get(p) and agreements[p].owner == f.funder)
        cash_elsewhere = 0
        units_elsewhere = 0
        for p in funded_projects:
            opt = options[p]
            owner = agreements[p].owner if agreements.get(p) else None
            if owner == f.funder and opt.period == f.period:
                cash_elsewhere += opt.cost
                units_elsewhere += opt.shared_units
        positions[key] = FunderPosition(
            funder=f.funder, period=f.period,
            cash_budget=f.cash, in_kind_budget=f.in_kind,
            shared_unit_budget=f.shared_units,
            cash_committed_elsewhere=cash_elsewhere,
            deposit_frozen=frozen,
            cash_available=f.cash - frozen - cash_elsewhere,
            in_kind_available=f.in_kind,
            units_available=f.shared_units - units_elsewhere,
            units_committed_elsewhere=units_elsewhere,
        )
    return positions


@dataclass
class CombinationEval:
    projects: tuple[str, ...]
    period: int | None
    feasible: bool
    binding_demand_supported: int
    capacity_total: int
    cost_total: int
    deposit_total: int
    shared_units_total: int
    unmet: list[str]
    excluded_reasons: dict[str, list[str]]   # 项目级门槛原因
    owner_of: dict[str, str]


def _project_exclusion(load: LoadResult, project: str,
                       agreements: dict[str, ProjectAgreement]) -> list[str]:
    reasons: list[str] = []
    opt = next(o for o in load.rows["investment_options"]
               if o.project == project)
    chain = (project,) + opt.dependencies
    for dep in chain:
        info = agreements.get(dep)
        if info is None or not info.has_signed:
            reasons.append(f"{dep} 缺有效配套协议")
    return reasons


def evaluate_combinations(load: LoadResult) -> list[CombinationEval]:
    """逐期评估全部依赖封闭组合，返回可比较的出资组合。"""
    agreements = project_agreements(load)
    options = {o.project: o for o in load.rows["investment_options"]}
    stages = {p: project_stage(load, p, agreements)
              for p in options}
    evals: list[CombinationEval] = []

    for period in sorted({o.period for o in options.values()}):
        period_projects = [p for p in options if options[p].period == period]
        # 依赖闭包可能引入其他期的共享先行项（CORE/SUPPORT），按实际期归集
        closure = demand_mod.dependency_closure(load)
        period_sets = [ps for ps in demand_mod.feasible_project_sets(load)
                       if all(options[p].period == period for p in ps)
                       and any(p in period_projects for p in ps)]
        # 同周期内"先承诺"的项目集合：把其他可行且协议齐备的项目视为已占位，
        # 由调用方在情景中显式选择；组合内部按出资方汇总占用即可。
        for ps in period_sets:
            unmet: list[str] = []
            excluded: dict[str, list[str]] = {}
            owner_of: dict[str, str] = {}
            for p in ps:
                reasons = _project_exclusion(load, p, agreements)
                if reasons:
                    excluded[p] = reasons
                    unmet.extend(f"{p}: {r}" for r in reasons)
                elif agreements[p].owner:
                    owner_of[p] = agreements[p].owner

            cost = sum(options[p].cost for p in ps)
            deposit = sum(options[p].deposit for p in ps)
            units = sum(options[p].shared_units for p in ps)
            capacity = sum(options[p].capacity for p in ps)

            # 资金与额度按出资方汇总（仅协议齐备、可列资金的项目参与占用）
            eligible = [p for p in ps if p not in excluded]
            cash_need: dict[str, int] = {}       # 扣除已付押金后的剩余现金需求
            full_cost: dict[str, int] = {}
            units_need: dict[str, int] = {}
            deposits_paid: dict[str, int] = {}
            for p in eligible:
                owner = owner_of.get(p)
                if not owner:
                    continue
                full_cost[owner] = full_cost.get(owner, 0) + options[p].cost
                units_need[owner] = units_need.get(owner, 0) + options[p].shared_units
                paid = stages[p].deposit_total
                deposits_paid[owner] = deposits_paid.get(owner, 0) + paid
                cash_need[owner] = cash_need.get(owner, 0) + options[p].cost - paid

            budgets = {f.funder: f for f in load.rows["funding"]
                       if f.period == period}
            for owner, need in cash_need.items():
                if owner not in budgets:
                    unmet.append(
                        f"{owner} 在第{period}期没有预算到账，项目不能用未到账资金列资")
                    continue
                f = budgets[owner]
                # 组合外项目的未释放押金仍冻结，但只冻结其所属期的预算
                frozen_outside = sum(
                    st.deposit_total for p, st in stages.items()
                    if p not in ps and not st.deposit_released
                    and options[p].period == period
                    and agreements.get(p) and agreements[p].owner == owner)
                cash_left = f.cash - frozen_outside
                if need > cash_left:
                    unmet.append(
                        f"{owner} 现金不足: 剩余需款 {need} > "
                        f"预算 {f.cash} - 组合外冻结押金 {frozen_outside}")
                if full_cost.get(owner, 0) > f.cash:
                    unmet.append(
                        f"{owner} 现金不足: 组合全款 {full_cost.get(owner, 0)} "
                        f"> 期预算 {f.cash}")
                if units_need.get(owner, 0) > f.shared_units:
                    unmet.append(
                        f"{owner} 共享配套额度不足: 需 "
                        f"{units_need.get(owner, 0)} > 上限 {f.shared_units}")
            for owner in units_need:
                if owner not in budgets:
                    unmet.append(
                        f"{owner} 在第{period}期无共享配套额度（未到账）")
            # 实物出资独立记账：预算为负或被超额占用时提示；cash/in_kind 不互抵
            for f in load.rows["funding"]:
                if f.period == period and f.in_kind < 0:
                    unmet.append(f"{f.funder} 实物出资余额为负，类型不可与现金互抵")

            scenario = demand_mod.scenario_demand(load, "eval", ps)
            evals.append(CombinationEval(
                projects=ps, period=period, feasible=not unmet,
                binding_demand_supported=scenario.binding_total,
                capacity_total=capacity, cost_total=cost,
                deposit_total=deposit, shared_units_total=units,
                unmet=unmet, excluded_reasons=excluded, owner_of=owner_of))

    evals.sort(key=lambda e: (e.period, not e.feasible,
                              -e.binding_demand_supported, -e.capacity_total,
                              e.cost_total, e.projects))
    return evals


def startable_scope(load: LoadResult) -> dict:
    """当前资料下可先启动的范围：逐项目给出门槛状态与可启动结论。"""
    agreements = project_agreements(load)
    options = {o.project: o for o in load.rows["investment_options"]}
    stages = {p: project_stage(load, p, agreements) for p in options}
    out: dict[str, dict] = {}
    for p, opt in sorted(options.items()):
        reasons = _project_exclusion(load, p, agreements)
        stage = stages[p]
        cross_period = [dep for dep in opt.dependencies
                        if dep in options and options[dep].period != opt.period]
        if cross_period:
            prior = "，".join(
                f"{dep}(第{options[dep].period}期，"
                f"{'已列资金' if agreements[dep].has_signed else '协议未齐'})"
                for dep in cross_period)
            reasons.append(f"跨期先行项须先投运: {prior}")
        out[p] = {
            "zone": opt.zone,
            "period": opt.period,
            "stage": opt.stage,
            "agreement_signed": agreements[p].has_signed,
            "agreement_owner": agreements[p].owner,
            "exclusion": reasons,
            "deposit_paid": stage.deposit_total,
            "deposit_released": stage.deposit_released,
            "next_payment_ready": stage.next_payment_ready,
            "accepted_capacity": stage.accepted_capacity,
            "blockers": stage.blockers,
            "can_start": not reasons,
        }
    return out


def payment_ledger(load: LoadResult) -> dict:
    """已履约款项与冻结/释放口径的资金台账。"""
    agreements = project_agreements(load)
    options = {o.project: o for o in load.rows["investment_options"]}
    stages = {p: project_stage(load, p, agreements) for p in options}
    paid_by_funder: dict[str, int] = {}
    frozen_by_funder: dict[str, int] = {}
    for e in load.rows["stage_events"]:
        if e.cash <= 0:
            continue
        owner = agreements[e.project].owner if agreements.get(e.project) else None
        if owner:
            paid_by_funder[owner] = paid_by_funder.get(owner, 0) + e.cash
    for p, st in stages.items():
        owner = agreements[p].owner if agreements.get(p) else None
        if owner and not st.deposit_released and st.deposit_total:
            frozen_by_funder[owner] = frozen_by_funder.get(owner, 0) + st.deposit_total
    return {
        "paid_by_funder": dict(sorted(paid_by_funder.items())),
        "deposit_frozen_by_funder": dict(sorted(frozen_by_funder.items())),
        "total_paid": sum(paid_by_funder.values()),
        "total_frozen": sum(frozen_by_funder.values()),
        "stages": stages,
    }


def build_funding_report(load: LoadResult) -> dict:
    agreements = project_agreements(load)
    stages = {p.project: project_stage(load, p.project, agreements)
              for p in load.rows["investment_options"]}
    funded = {p for p, info in agreements.items() if info.has_signed}
    positions = funder_positions(load, stages, funded)
    return {
        "agreements": agreements,
        "stages": stages,
        "positions": positions,
        "combinations": evaluate_combinations(load),
        "startable": startable_scope(load),
        "ledger": payment_ledger(load),
    }
