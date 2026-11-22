"""需求证据分析。

口径（对应规划组要求）
--------------------
1. ``shared_claim`` 是同一个真实客户服务权益的共同编号；同组申请互为替代时，
   可兑现需求按组内**单个申请上限的最大值**计一次，绝不沿申请逐行累加。
2. ``binding`` 才是有约束力的客户承诺；``show_interest`` 仅为巡回展演人次，
   只列示上行潜力，不进承诺需求；``withdrawn`` 不产生需求，只保留撤回轨迹。
3. 公告线路容量（pilot.offered）不是需求；只有 ``actual`` 是真实服务观察，
   且 ``quality=pending`` 的部分不进入质量确认口径。
4. 某片区没有任何试点服务记录时，不存在可外推的试点证据，明确点名。
5. 投运情景以"具备先行依赖、可实际投运的项目集合"表达；同一批承诺在组合
   调整时落在哪个片区、能否兑现，由情景重算，不做静态加总。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .loader import LoadResult

BINDING = "binding"
SHOW_INTEREST = "show_interest"
WITHDRAWN = "withdrawn"


@dataclass
class ZoneEvidence:
    zone: str
    pilots: int
    offered_total: int          # 公告容量合计（仅作对照，不是需求）
    actual_total: int           # 实际服务合计
    actual_quality_confirmed: int  # 质量已签收(accepted)的实际服务
    actual_pending: int         # 质量证据未签收的实际服务
    cancelled: int              # actual==0 的记录数（取消/未履约）
    customers: set[str] = field(default_factory=set)


@dataclass
class ClaimGroup:
    shared_claim: str
    kind: str                       # binding | show_interest
    offers: list[str]
    zones: list[str]
    customers: list[str]
    max_units: int                  # 组内单个申请上限的最大值（去重后口径）
    alternatives: list[tuple[str, str]]  # (offer, alternative_to)
    withdrawn_offers: list[str]
    note: str = ""


@dataclass
class ScenarioDemand:
    name: str
    projects: tuple[str, ...]
    zones_served: tuple[str, ...]
    binding_by_zone: dict[str, int]
    binding_total: int
    upside_by_zone: dict[str, int]  # 巡演/意向类，不计入承诺需求
    upside_total: int
    capacity_total: int
    per_claim: dict[str, tuple[str, int, int]]  # claim -> (兑现片区, 下限, 上限)


def zone_evidence(load: LoadResult) -> dict[str, ZoneEvidence]:
    evidence: dict[str, ZoneEvidence] = {}
    for p in load.rows["pilot_service"]:
        z = evidence.setdefault(p.zone, ZoneEvidence(
            zone=p.zone, pilots=0, offered_total=0, actual_total=0,
            actual_quality_confirmed=0, actual_pending=0, cancelled=0))
        z.pilots += 1
        z.offered_total += p.offered
        z.actual_total += p.actual
        if p.quality == "accepted":
            z.actual_quality_confirmed += p.actual
        else:
            z.actual_pending += p.actual
        if p.actual == 0:
            z.cancelled += 1
        z.customers.add(p.customer)
    return evidence


def classify_commitments(load: LoadResult) -> list[ClaimGroup]:
    groups: dict[str, list] = {}
    for c in load.rows["customer_commitments"]:
        groups.setdefault(c.shared_claim, []).append(c)

    result: list[ClaimGroup] = []
    for claim_id, rows in sorted(groups.items()):
        active = [r for r in rows if r.state != WITHDRAWN]
        withdrawn = [r for r in rows if r.state == WITHDRAWN]
        kinds = {r.state for r in active}
        kind = BINDING if BINDING in kinds else (
            SHOW_INTEREST if SHOW_INTEREST in kinds else WITHDRAWN)
        max_units = max((r.units for r in active), default=0)
        alternatives = [(r.offer, r.alternative_to) for r in active
                        if r.alternative_to]
        note = ""
        if alternatives:
            note = "含互为替代申请，同一客户权益按上限只计一次"
        if withdrawn:
            note = ("；".join(filter(None, [note,
                f"撤回申请 {', '.join(r.offer for r in withdrawn)} 不产生需求"])))
        result.append(ClaimGroup(
            shared_claim=claim_id,
            kind=kind,
            offers=[r.offer for r in active],
            zones=sorted({r.zone for r in active}),
            customers=sorted({r.customer for r in active}),
            max_units=max_units,
            alternatives=alternatives,
            withdrawn_offers=[r.offer for r in withdrawn],
            note=note,
        ))
    return result


def zones_without_pilot_evidence(load: LoadResult) -> list[str]:
    """承诺/投资涉及、但没有任何试点服务记录可外推的片区。"""
    piloted = {p.zone for p in load.rows["pilot_service"]}
    mentioned = set()
    for c in load.rows["customer_commitments"]:
        mentioned.add(c.zone)
    for o in load.rows["investment_options"]:
        if o.zone != "shared":
            mentioned.add(o.zone)
    return sorted(mentioned - piloted)


def _option_index(load: LoadResult) -> dict[str, object]:
    return {o.project: o for o in load.rows["investment_options"]}


def dependency_closure(load: LoadResult) -> dict[str, tuple[str, ...]]:
    """每个项目的完整先行依赖闭包（含自身），依赖缺失时给出空元组标记。"""
    options = _option_index(load)
    closure: dict[str, tuple[str, ...]] = {}

    def resolve(project: str, stack: tuple[str, ...]) -> tuple[str, ...]:
        if project in closure:
            return closure[project]
        if project not in options or project in stack:
            return ()
        opt = options[project]
        deps: tuple[str, ...] = (project,)
        for dep in opt.dependencies:
            sub = resolve(dep, stack + (project,))
            if not sub:
                closure[project] = ()
                return ()
            deps = tuple(dict.fromkeys(deps + sub))
        closure[project] = deps
        return deps

    for project in options:
        resolve(project, ())
    return closure


def feasible_project_sets(load: LoadResult) -> list[tuple[str, ...]]:
    """枚举所有依赖封闭的项目子集（子集内每个项目的依赖也在子集内）。"""
    options = _option_index(load)
    closure = dependency_closure(load)
    projects = sorted(p for p in options if closure.get(p))
    results: list[tuple[str, ...]] = []
    for mask in range(1, 1 << len(projects)):
        chosen = tuple(p for i, p in enumerate(projects) if mask & (1 << i))
        needed: set[str] = set()
        for p in chosen:
            needed.update(closure[p])
        if needed.issubset(chosen):
            results.append(chosen)
    return sorted(results, key=lambda s: (len(s), s))


def _served_zones(projects: tuple[str, ...], load: LoadResult) -> tuple[str, ...]:
    options = _option_index(load)
    return tuple(sorted({options[p].zone for p in projects
                         if options[p].zone != "shared"}))


def scenario_demand(load: LoadResult, name: str,
                    projects: tuple[str, ...]) -> ScenarioDemand:
    options = _option_index(load)
    served = set(_served_zones(projects, load))
    groups = classify_commitments(load)
    by_zone_binding: dict[str, int] = {}
    by_zone_upside: dict[str, int] = {}
    per_claim: dict[str, tuple[str, int, int]] = {}

    for g in groups:
        active = {r.offer: r for r in load.rows["customer_commitments"]
                  if r.shared_claim == g.shared_claim and r.state != WITHDRAWN}
        in_served = [r for r in active.values() if r.zone in served]
        lower = max((r.units for r in in_served), default=0)
        upper = g.max_units  # 全部申请所在片区都投运时的上限
        if g.kind == BINDING:
            realized_zone = ",".join(sorted({r.zone for r in in_served})) or "无片区投运"
            per_claim[g.shared_claim] = (realized_zone, lower, upper)
            if in_served:
                # 同组多片区同时投运也只计一次：计入代表片区，其余片区不重复加
                anchor = sorted({r.zone for r in in_served})[0]
                by_zone_binding[anchor] = by_zone_binding.get(anchor, 0) + lower
        else:
            # 巡演/意向同样按权益去重：组内只取最大单位一次，计入任一投运片区
            if in_served:
                anchor = sorted({r.zone for r in in_served})[0]
                by_zone_upside[anchor] = by_zone_upside.get(anchor, 0) + lower
            per_claim[g.shared_claim] = (
                ",".join(sorted({r.zone for r in in_served})) or "无片区投运",
                lower, upper)

    capacity = sum(options[p].capacity for p in projects)
    return ScenarioDemand(
        name=name,
        projects=projects,
        zones_served=tuple(sorted(served)),
        binding_by_zone=dict(sorted(by_zone_binding.items())),
        binding_total=sum(by_zone_binding.values()),
        upside_by_zone=dict(sorted(by_zone_upside.items())),
        upside_total=sum(by_zone_upside.values()),
        capacity_total=capacity,
        per_claim=per_claim,
    )


def all_scenarios(load: LoadResult) -> list[ScenarioDemand]:
    """全部依赖封闭组合的需求重算，按承诺需求降序排列。"""
    scenarios = [scenario_demand(load, "组合:" + "+".join(ps), ps)
                 for ps in feasible_project_sets(load)]
    scenarios.sort(key=lambda s: (-s.binding_total, -s.capacity_total, s.projects))
    return scenarios


def named_scenarios(load: LoadResult) -> list[ScenarioDemand]:
    """规划组关心的对照情景。"""
    options = _option_index(load)
    closure = dependency_closure(load)
    feasible = set(feasible_project_sets(load))
    all_projects = tuple(sorted(p for p in options if closure.get(p)))
    base = tuple(sorted(p for p in all_projects if options[p].stage == "base"))
    names: list[tuple[str, tuple[str, ...]]] = []
    if tuple(all_projects) in feasible:
        names.append(("全部投运", tuple(all_projects)))
    if base in feasible:
        names.append(("仅一期基础项", base))
    for zone in sorted({o.zone for o in options.values() if o.zone != "shared"}):
        zset = tuple(sorted(p for p in all_projects if options[p].zone == zone))
        if zset in feasible:
            names.append((f"仅片区{zone}", zset))
    # 相邻项目不投运时的增量视角：去掉每个片区后的组合
    for zone in sorted({o.zone for o in options.values() if o.zone != "shared"}):
        rest = tuple(sorted(p for p in all_projects if options[p].zone != zone))
        if rest in feasible and rest:
            names.append((f"片区{zone}不投运", rest))
    seen: set[tuple[str, ...]] = set()
    out: list[ScenarioDemand] = []
    for name, ps in names:
        if ps not in seen:
            seen.add(ps)
            out.append(scenario_demand(load, name, ps))
    return out


def build_demand_report(load: LoadResult) -> dict:
    """汇总给 CLI/测试使用的结构化报告。"""
    evidence = zone_evidence(load)
    return {
        "zone_evidence": evidence,
        "claim_groups": classify_commitments(load),
        "zones_without_evidence": zones_without_pilot_evidence(load),
        "named_scenarios": named_scenarios(load),
        "all_scenarios": all_scenarios(load),
    }
