"""经办回执与决定快照的本地存储。

目录结构（默认 ``./data``）::

    data/submissions/<id>.json     经办提交（预算/试运行/分期回执），只增不改
    data/decisions/<seq>.json      已发出的决定快照（含证据指纹、预留额度、服务范围）
    data/state.json                决定序号与提交序列

决定一旦发出即不可变；资料更正后通过 ``reevaluate`` 标记受影响决定，
保留原快照用于查回"当时接受的证据、预留的额度和获准的服务范围"。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import funding as funding_mod
from . import loader as loader_mod
from .loader import HEADERS

SUBMISSION_KINDS = {
    "budget": ("funding",),
    "pilot": ("pilot_service",),
    "commitment": ("customer_commitments",),
    "option": ("investment_options",),
    "agreement": ("agreements",),
    "stage": ("stage_events",),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Submission:
    submission_id: str
    kind: str
    table: str
    rows: list[dict]
    note: str
    received_at: str


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.sub_dir = self.root / "submissions"
        self.dec_dir = self.root / "decisions"
        self.state_path = self.root / "state.json"
        self.sub_dir.mkdir(parents=True, exist_ok=True)
        self.dec_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self._write_state({"next_decision": 1, "submissions": []})

    # ------------------------------------------------------------- 基础读写
    def _write_state(self, state: dict) -> None:
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def list_submissions(self) -> list[dict]:
        return list(self._state()["submissions"])

    def list_decisions(self) -> list[dict]:
        out = []
        for path in sorted(self.dec_dir.glob("*.json"),
                           key=lambda p: int("".join(c for c in p.stem if c.isdigit()))):
            out.append(json.loads(path.read_text(encoding="utf-8")))
        return out

    # ------------------------------------------------------------- 回执提交
    def submit(self, kind: str, rows: list[dict], note: str = "") -> dict:
        if kind not in SUBMISSION_KINDS:
            raise ValueError(f"未知回执类型 {kind}，可选 {sorted(SUBMISSION_KINDS)}")
        if not rows:
            raise ValueError("回执至少包含一行记录")
        table = SUBMISSION_KINDS[kind][0]
        expected = HEADERS[table]
        for i, row in enumerate(rows):
            if list(row.keys()) != expected:
                raise ValueError(
                    f"第 {i + 1} 行表头不符: 得到 {list(row.keys())}，期望 {expected}")
        state = self._state()
        seq = len(state["submissions"]) + 1
        submission_id = f"SUB{seq:04d}"
        record = {
            "submission_id": submission_id,
            "kind": kind,
            "table": table,
            "rows": rows,
            "note": note,
            "received_at": _now(),
        }
        (self.sub_dir / f"{submission_id}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        state["submissions"].append(submission_id)
        self._write_state(state)
        return record

    def submit_csv_text(self, kind: str, text: str, note: str = "") -> dict:
        reader = csv.DictReader(io.StringIO(text))
        rows = [dict(r) for r in reader]
        return self.submit(kind, rows, note)

    def merged_extras(self) -> dict[str, list[tuple[dict, str]]]:
        """按提交顺序产出 loader 需要的 (行, 来源标签) 列表。"""
        extras: dict[str, list[tuple[dict, str]]] = {}
        for submission_id in self._state()["submissions"]:
            rec = json.loads(
                (self.sub_dir / f"{submission_id}.json").read_text(encoding="utf-8"))
            extras.setdefault(rec["table"], []).extend(
                (dict(row), submission_id) for row in rec["rows"])
        return extras

    def current_load(self, sources_dir: Path) -> loader_mod.LoadResult:
        return loader_mod.load(Path(sources_dir), self.merged_extras())

    # ------------------------------------------------------------- 决定快照
    def issue_decision(self, sources_dir: Path, title: str,
                       scope_projects: list[str], note: str = "") -> dict:
        """发出一笔分期决定；门槛不满足的项目拒绝入册并写明原因。"""
        load = self.current_load(sources_dir)
        if not load.ok:
            raise ValueError("来源校验未通过，不能发出决定: "
                             + "；".join(load.errors))
        agreements = funding_mod.project_agreements(load)
        stages = {p.project: funding_mod.project_stage(load, p.project, agreements)
                  for p in load.rows["investment_options"]}
        options = {o.project: o for o in load.rows["investment_options"]}
        closure = {p: set(closure_p)
                   for p, closure_p in
                   funding_mod.demand_mod.dependency_closure(load).items()}

        approved, rejected = [], []
        reserved_cash: dict[str, int] = {}
        reserved_units: dict[str, int] = {}
        scope_set = set(scope_projects)
        for p in scope_projects:
            reasons = funding_mod._project_exclusion(load, p, agreements)
            # 配套设施有必须同时投运的部分：先行依赖不在本决定范围内同样拒绝
            missing_deps = sorted(dep for dep in options[p].dependencies
                                  if dep not in scope_set)
            if missing_deps:
                reasons.append(
                    f"先行项目 {','.join(missing_deps)} 未同时列入，"
                    "配套设施须同时投运")
            if not closure.get(p):
                reasons.append("依赖链存在悬空/循环引用，项目不可投运")
            if reasons:
                rejected.append({"project": p, "reasons": reasons})
                continue
            owner = agreements[p].owner
            approved.append({
                "project": p,
                "owner": owner,
                "period": options[p].period,
                "cost": options[p].cost,
                "shared_units": options[p].shared_units,
                "deposit_paid": stages[p].deposit_total,
                "next_payment_ready": stages[p].next_payment_ready,
                "accepted_capacity": stages[p].accepted_capacity,
                "service_refs": stages[p].service_refs,
                "quality_warnings": stages[p].quality_warnings,
            })
            reserved_cash[owner] = reserved_cash.get(owner, 0) + options[p].cost
            reserved_units[owner] = reserved_units.get(owner, 0) + options[p].shared_units

        # 决定时刻获准的服务范围：只包含实际批准（含其同时投运的先行项）的项目
        approved_projects = tuple(sorted(
            set(a["project"] for a in approved)
            | {d for a in approved for d in closure.get(a["project"], set())}))
        scenario = funding_mod.demand_mod.scenario_demand(
            load, title, approved_projects)
        # 固化当时仍有效的约束力承诺，供日后识别"需求客户退出"
        binding_snapshot = [
            {"offer": c.offer, "customer": c.customer, "zone": c.zone,
             "shared_claim": c.shared_claim, "units": c.units, "state": c.state}
            for c in load.rows["customer_commitments"] if c.state == "binding"]

        state = self._state()
        seq = state["next_decision"]
        decision_id = f"DEC{seq:04d}"
        snapshot = {
            "decision_id": decision_id,
            "title": title,
            "issued_at": _now(),
            "note": note,
            "source_fingerprints": dict(load.fingerprints),
            "submissions_applied": list(self._state()["submissions"]),
            "approved": approved,
            "rejected": rejected,
            "reserved": {"cash_by_funder": reserved_cash,
                         "shared_units_by_funder": reserved_units},
            "approved_service_scope": {
                "projects": list(scenario.projects),
                "zones": list(scenario.zones_served),
                "binding_demand": scenario.binding_total,
                "binding_by_zone": scenario.binding_by_zone,
                "upside_by_zone": scenario.upside_by_zone,
                "per_claim": scenario.per_claim,
                "capacity_total": scenario.capacity_total,
            },
            "binding_commitments_snapshot": binding_snapshot,
            "evidence": {
                "agreements": {p: {"signed": a.has_signed,
                                   "signed_day": a.signed_day,
                                   "owner": a.owner}
                               for p, a in agreements.items()},
                "stage_events": [
                    {"event": e.event, "project": e.project,
                     "reporter": e.reporter, "state": e.state,
                     "observed_capacity": e.observed_capacity,
                     "service_ref": e.service_ref, "cash": e.cash,
                     "origin": e.origin}
                    for e in load.rows["stage_events"]],
                "warnings": list(load.warnings),
            },
            "superseded_by": None,
            "reevaluation": None,
        }
        (self.dec_dir / f"{decision_id}.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        state["next_decision"] = seq + 1
        self._write_state(state)
        return snapshot

    def get_decision(self, decision_id: str) -> dict:
        path = self.dec_dir / f"{decision_id}.json"
        if not path.exists():
            raise KeyError(decision_id)
        return json.loads(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------- 重审
    def reevaluate(self, sources_dir: Path) -> list[dict]:
        """对照当前资料重算每个已发决定，标记需要重审的组合与原因。

        触发条件：
        - 需求客户退出（binding 承诺撤回/消失，或其片区不再有投运项目）；
        - 共同设施迟验（决定依赖的共享项仍无双方 accepted，或协议变化）；
        - 来源指纹/回执集合变化；
        - 原批准项目现在不满足协议或验收门槛。
        """
        load = self.current_load(sources_dir)
        current_fp = load.fingerprints
        current_subs = self._state()["submissions"]
        agreements = funding_mod.project_agreements(load)
        stages = {p.project: funding_mod.project_stage(load, p.project, agreements)
                  for p in load.rows["investment_options"]}
        options = {o.project: o for o in load.rows["investment_options"]}
        report = []

        for snapshot in self.list_decisions():
            triggers: list[str] = []
            # 来源指纹/回执只在决定发出之后又发生变化时才提示（决定已含当时的回执）
            if snapshot["submissions_applied"] != current_subs:
                new_subs = [s for s in current_subs
                            if s not in snapshot["submissions_applied"]]
                triggers.append("决定后新增经办回执: " + ",".join(new_subs))
            if (snapshot["submissions_applied"] == current_subs
                    and snapshot["source_fingerprints"] != current_fp):
                changed = sorted(
                    set(snapshot["source_fingerprints"]) | set(current_fp))
                triggers.append("决定后来源资料已更正: " + ",".join(changed))

            approved_projects = [a["project"] for a in snapshot["approved"]]
            # 需求客户退出：对照决定时刻固化的 binding 承诺快照
            old_bindings = snapshot.get("binding_commitments_snapshot", [])
            current_binding = {c.offer: c for c in load.rows["customer_commitments"]
                               if c.state == "binding"}
            for old in old_bindings:
                now = current_binding.get(old["offer"])
                if now is None or now.state != "binding":
                    triggers.append(
                        f"需求客户退出: 承诺 {old['offer']}（客户 {old['customer']}，"
                        f"权益 {old['shared_claim']}，{old['units']} 单位）已撤回或失效")
            # 情景重算：同一批承诺在组合调整后的支持范围变化
            scenario_now = funding_mod.demand_mod.scenario_demand(
                load, snapshot["title"], tuple(sorted(approved_projects)))
            old_demand = snapshot["approved_service_scope"]["binding_demand"]
            if scenario_now.binding_total < old_demand:
                triggers.append(
                    f"组合调整后可兑现承诺收窄: {old_demand} -> "
                    f"{scenario_now.binding_total}（片区 "
                    f"{snapshot['approved_service_scope'].get('binding_by_zone')} -> "
                    f"{scenario_now.binding_by_zone}）")
            # 共同设施迟验
            for p in approved_projects:
                chain = (p,) + options[p].dependencies if p in options else (p,)
                for dep in chain:
                    st = stages.get(dep)
                    if st is None:
                        triggers.append(f"{dep} 项目记录已消失")
                        continue
                    if options[dep].zone == "shared" and not st.next_payment_ready:
                        triggers.append(
                            f"共同设施 {dep} 尚未通过双方验收"
                            + ("（含质量证据待签收）" if st.quality_warnings else ""))
                    if not agreements[dep].has_signed:
                        triggers.append(f"{dep} 的有效配套协议已失效/缺失")
                    if st.blockers and options[dep].zone != "shared":
                        triggers.append(f"{dep} 验收门槛: " + "；".join(st.blockers))

            # 剩余出资与已履约款项（当前口径）
            ledger = funding_mod.payment_ledger(load)
            result = {
                "decision_id": snapshot["decision_id"],
                "title": snapshot["title"],
                "needs_review": bool(triggers),
                "triggers": sorted(set(triggers)),
                "current_binding_demand": scenario_now.binding_total,
                "current_scope": {
                    "projects": list(scenario_now.projects),
                    "zones": list(scenario_now.zones_served),
                    "binding_by_zone": scenario_now.binding_by_zone,
                },
                "remaining": _remaining_positions(load, agreements, stages),
                "paid": ledger["paid_by_funder"],
                "deposit_frozen": ledger["deposit_frozen_by_funder"],
            }
            snapshot["reevaluation"] = result
            (self.dec_dir / f"{snapshot['decision_id']}.json").write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8")
            report.append(result)
        return report


def _remaining_positions(load, agreements, stages) -> dict:
    funded = {p for p, info in agreements.items() if info.has_signed}
    positions = funding_mod.funder_positions(load, stages, funded)
    return {
        f"{funder}-P{period}": {
            "cash_available": pos.cash_available,
            "in_kind_available": pos.in_kind_available,
            "shared_units_available": pos.units_available,
            "deposit_frozen": pos.deposit_frozen,
        }
        for (funder, period), pos in sorted(positions.items())
    }
