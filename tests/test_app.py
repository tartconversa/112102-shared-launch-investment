import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app import demand, funding, loader, storage

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "sources"

COMMITMENT_HEADER = ("offer,customer,zone,operator,shared_claim,units,state,"
                     "known_day,alternative_to\n")
STAGE_HEADER = ("event,project,known_day,reporter,observed_capacity,"
                "service_ref,state,cash\n")
AGREEMENT_HEADER = "agreement,project,owner,state,known_day\n"


def load_default():
    return loader.load(SOURCES)


class DemandTests(unittest.TestCase):
    def setUp(self):
        self.load = load_default()
        self.report = demand.build_demand_report(self.load)

    def test_pilot_evidence_distinguishes_actual_from_offered(self):
        ev = {z.zone: z for z in self.report["zone_evidence"].values()}
        # 公告容量 95 不是需求；实际服务 36，其中质量确认 24、待签收 12、取消 5
        self.assertEqual(ev["X"].offered_total, 95)
        self.assertEqual(ev["x".upper()].actual_total, 36)
        self.assertEqual(ev["X"].actual_quality_confirmed, 24)
        self.assertEqual(ev["X"].actual_pending, 12)
        self.assertEqual(ev["X"].cancelled, 5)

    def test_zone_without_pilot_evidence_is_named(self):
        # Z 只有巡演意向，没有任何试点服务记录，禁止外推
        self.assertEqual(self.report["zones_without_evidence"], ["Z"])

    def test_shared_claim_alternatives_counted_once(self):
        groups = {g.shared_claim: g for g in self.report["claim_groups"]}
        k1 = groups["K1"]
        self.assertEqual(k1.kind, "binding")
        self.assertEqual(set(k1.offers), {"J1", "J2"})
        self.assertEqual(k1.max_units, 7)  # 同一客户权益，绝不 7+7 累加
        self.assertTrue(k1.alternatives)
        # 撤回申请不产生需求，但保留轨迹
        k3 = groups["K3"]
        self.assertEqual(k3.max_units, 5)
        self.assertEqual(k3.withdrawn_offers, ["J5"])
        # 巡演意向不进承诺需求
        k5 = groups["K5"]
        self.assertEqual(k5.kind, "show_interest")

    def test_binding_demand_totals_and_no_double_count(self):
        full = next(s for s in self.report["named_scenarios"]
                    if s.name == "全部投运")
        # K1=7（J1/J2 只计一次）+ K3=5；K5 巡演 8 不计承诺
        self.assertEqual(full.binding_total, 12)
        self.assertEqual(full.upside_total, 8)
        self.assertEqual(sum(full.binding_by_zone.values()), full.binding_total)

    def test_adjacent_project_offline_changes_support_scope(self):
        no_y = next(s for s in self.report["named_scenarios"]
                    if s.name == "片区Y不投运")
        # Y 不投运：K1 落 X 仍可兑现 7，K3 失去服务片区
        self.assertEqual(no_y.binding_total, 7)
        self.assertEqual(no_y.per_claim["K3"][1], 0)
        self.assertEqual(no_y.per_claim["K1"][1], 7)
        no_x = next(s for s in self.report["named_scenarios"]
                    if s.name == "片区X不投运")
        # X 不投运：K1 随替代申请 J2 转到 Y，总承诺不变但片区支持变化
        self.assertEqual(no_x.binding_total, 12)
        self.assertNotIn("X", no_x.zones_served)

    def test_feasible_sets_are_dependency_closed(self):
        options = {o.project: o for o in self.load.rows["investment_options"]}
        for ps in demand.feasible_project_sets(self.load):
            for p in ps:
                for dep in options[p].dependencies:
                    self.assertIn(dep, ps, f"{p} 的先行依赖 {dep} 未同组")


class FundingTests(unittest.TestCase):
    def setUp(self):
        self.load = load_default()
        self.report = funding.build_funding_report(self.load)

    def test_agreement_gate_blocks_unfundable_projects(self):
        startable = self.report["startable"]
        self.assertTrue(startable["S1"]["can_start"])
        self.assertFalse(startable["S3"]["can_start"])   # 无协议
        self.assertFalse(startable["S4"]["can_start"])   # 无协议
        combos = self.report["combinations"]
        for c in combos:
            if "S3" in c.projects or "S4" in c.projects:
                self.assertFalse(c.feasible)
                self.assertTrue(c.excluded_reasons)

    def test_shared_unit_budget_constraint(self):
        blocked = next(c for c in self.report["combinations"]
                       if set(c.projects) == {"CORE", "S1", "S2"})
        self.assertFalse(blocked.feasible)
        self.assertTrue(any("共享配套额度" in u for u in blocked.unmet))

    def test_deposit_frozen_until_acceptance(self):
        stages = self.report["stages"]
        # S1 双方验收 -> 押金可释放；S2 仅押金事件 -> 继续冻结
        self.assertTrue(stages["S1"].deposit_released)
        self.assertFalse(stages["S2"].deposit_released)
        ledger = self.report["ledger"]
        self.assertEqual(ledger["paid_by_funder"], {"F1": 8, "F2": 9})
        self.assertEqual(ledger["deposit_frozen_by_funder"], {"F2": 9})

    def test_dual_acceptance_gate_with_real_service_evidence(self):
        stages = self.report["stages"]
        # S1 双方 accepted 且引用 PX12，但 PX12 质量 pending -> 暂不放下笔款
        self.assertTrue(stages["S1"].operator_accepted)
        self.assertTrue(stages["S1"].district_accepted)
        self.assertEqual(stages["S1"].accepted_capacity, 6)
        self.assertFalse(stages["S1"].next_payment_ready)
        self.assertTrue(stages["S1"].quality_warnings)
        # CORE 只有单方 partial
        self.assertFalse(stages["CORE"].next_payment_ready)
        self.assertEqual(stages["CORE"].accepted_capacity, 0)

    def test_period_without_budget_cannot_be_funded(self):
        support = next(c for c in self.report["combinations"]
                       if c.projects == ("SUPPORT",))
        self.assertFalse(support.feasible)
        self.assertTrue(any("没有预算到账" in u for u in support.unmet))

    def test_cash_and_in_kind_kept_separate(self):
        positions = self.report["positions"]
        p = positions[("F1", 1)]
        # 68 预算 - S1/CORE 已列资金 61 = 7；实物 15 独立保留
        self.assertEqual(p.cash_available, 7)
        self.assertEqual(p.in_kind_available, 15)
        self.assertEqual(p.units_available, 3)


class LoaderAndStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        self.store = storage.Store(self.data)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_fingerprints_stable_and_warnings_emitted(self):
        result = loader.load(SOURCES)
        self.assertTrue(result.ok)
        self.assertEqual(set(result.fingerprints), set(loader.HEADERS))
        self.assertTrue(any("pending" in w for w in result.warnings))
        self.assertTrue(any("实际服务为 0" in w for w in result.warnings))

    def test_dangling_reference_is_error(self):
        bad_sources = self.tmp / "sources"
        shutil.copytree(SOURCES, bad_sources)
        text = (bad_sources / "stage_events.csv").read_text(encoding="utf-8")
        text += "EX,S1,30,operator,3,PX99,accepted,\n"
        (bad_sources / "stage_events.csv").write_text(text, encoding="utf-8")
        result = loader.load(bad_sources)
        self.assertFalse(result.ok)
        self.assertTrue(any("悬空引用" in e for e in result.errors))

    def test_submission_correction_overrides_same_key(self):
        # 撤回 J1：同主键回执覆盖来源行
        csv_text = COMMITMENT_HEADER + (
            "J1,C1,X,O1,K1,7,withdrawn,30,\n")
        self.store.submit_csv_text("commitment", csv_text, "客户退出")
        load = self.store.current_load(SOURCES)
        j1 = next(c for c in load.rows["customer_commitments"]
                  if c.offer == "J1")
        self.assertEqual(j1.state, "withdrawn")
        self.assertEqual(j1.origin, "SUB0001")
        # 撤回后 X 片区无可兑现 binding（J2 仍在 Y）
        report = demand.build_demand_report(load)
        k1 = next(g for g in report["claim_groups"] if g.shared_claim == "K1")
        self.assertEqual(k1.offers, ["J2"])

    def test_budget_submission_changes_available_cash(self):
        # F2 原无第 2 期预算；补到账后 SUPPORT 才可行（须覆盖 17 全款，
        # 且 F2 另有 9 押金在第 1 期冻结——不同期不挤占，额度按第 2 期单算）
        csv_text = ("funder,period,cash,in_kind,shared_units\n"
                    "F2,2,25,5,4\n")
        self.store.submit_csv_text("budget", csv_text, "F2 二期到账")
        load = self.store.current_load(SOURCES)
        report = funding.build_funding_report(load)
        support = next(c for c in report["combinations"]
                       if c.projects == ("SUPPORT",))
        self.assertTrue(support.feasible)

    def test_decision_snapshot_and_reevaluation_customer_exit(self):
        # 先在原始资料下发决定
        snap = self.store.issue_decision(
            SOURCES, "基线一期", ["S1", "CORE"])
        self.assertEqual(
            snap["approved_service_scope"]["binding_demand"], 7)
        self.assertEqual(snap["reserved"]["cash_by_funder"], {"F1": 61})
        # 随后客户 C1 撤回 J1
        self.store.submit_csv_text(
            "commitment",
            COMMITMENT_HEADER + "J1,C1,X,O1,K1,7,withdrawn,30,\n",
            "客户C1退出")
        results = {r["decision_id"]: r
                   for r in self.store.reevaluate(SOURCES)}
        re = results[snap["decision_id"]]
        self.assertTrue(re["needs_review"])
        self.assertTrue(any("需求客户退出" in t for t in re["triggers"]))
        # 历史快照仍可查回当时的证据与预留额度（不可变）
        again = self.store.get_decision(snap["decision_id"])
        self.assertEqual(
            again["approved_service_scope"]["binding_demand"], 7)
        self.assertEqual(again["reserved"], snap["reserved"])
        self.assertTrue(again["evidence"]["stage_events"])

    def test_decision_rejects_missing_co_facility(self):
        # S2 依赖 CORE，未同时列入必须拒绝，排序靠前也不豁免
        snap = self.store.issue_decision(SOURCES, "缺共享项", ["S2"])
        self.assertEqual(snap["approved"], [])
        self.assertTrue(snap["rejected"])
        self.assertIn("CORE", snap["rejected"][0]["reasons"][0])

    def test_late_shared_facility_acceptance_triggers_review(self):
        snap = self.store.issue_decision(
            SOURCES, "含共享设施", ["S1", "CORE"])
        result = self.store.reevaluate(SOURCES)
        re = result[0]
        # CORE 尚无双方 accepted -> 共同设施迟验触发重审
        self.assertTrue(any("共同设施 CORE" in t for t in re["triggers"]))
        # 补齐 CORE 的双方验收（含真实服务证据）后不再报迟验
        events = (
            STAGE_HEADER
            + "E7,CORE,30,operator,5,PX1,accepted,\n"
            + "E8,CORE,30,district,5,PX1,accepted,\n")
        self.store.submit_csv_text("stage", events, "CORE 验收补齐")
        re2 = self.store.reevaluate(SOURCES)[0]
        self.assertFalse(
            any("共同设施 CORE" in t for t in re2["triggers"]))


if __name__ == "__main__":
    unittest.main()
