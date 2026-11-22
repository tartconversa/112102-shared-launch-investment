"""命令行入口：报告、回执、决定、重审。

用法示例
--------
python -m app.cli report --sources sources
python -m app.cli demand --sources sources --json
python -m app.cli funding --sources sources
python -m app.cli submit --data data --kind commitment --file new_offer.csv --note "新承诺"
python -m app.cli decide --sources sources --data data --title 一期拨款 --projects S1 CORE
python -m app.cli reevaluate --sources sources --data data
python -m app.cli decisions --data data
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import demand as demand_mod
from . import funding as funding_mod
from .storage import SUBMISSION_KINDS, Store


# ----------------------------------------------------------------- 文本渲染

def _line(char: str = "-", n: int = 72) -> str:
    return char * n


def render_demand(load) -> str:
    report = demand_mod.build_demand_report(load)
    out: list[str] = ["需求证据分析", _line("=")]

    out.append("一、试点履约证据（公告容量 offered 仅对照，actual 才是真实服务）")
    out.append(_line())
    header = f"{'片区':<4}{'试点数':>5}{'公告合计':>9}{'实际服务':>9}{'质量确认':>9}{'待签收':>7}{'取消记录':>7}"
    out.append(header)
    for z in sorted(report["zone_evidence"].values(), key=lambda e: e.zone):
        out.append(
            f"{z.zone:<4}{z.pilots:>5}{z.offered_total:>9}{z.actual_total:>9}"
            f"{z.actual_quality_confirmed:>9}{z.actual_pending:>7}{z.cancelled:>7}")
    no_evidence = report["zones_without_evidence"]
    if no_evidence:
        out.append("")
        out.append("⚠ 没有可外推试点证据的片区: " + "、".join(no_evidence)
                   + "（无实际服务记录，需求不得由其他片区或公告容量外推）")

    out.append("")
    out.append("二、客户承诺分组（shared_claim 同一权益，替代申请不逐行累加）")
    out.append(_line())
    for g in report["claim_groups"]:
        kind = {"binding": "有约束力承诺", "show_interest": "巡回展演意向(不计承诺)",
                "withdrawn": "全部撤回"}[g.kind]
        out.append(f"{g.shared_claim} [{kind}] 申请 {','.join(g.offers)} "
                   f"片区 {','.join(g.zones)} 客户 {','.join(g.customers)} "
                   f"可兑现上限 {g.max_units}")
        if g.alternatives:
            for offer, alt in g.alternatives:
                out.append(f"   替代关系: {offer} -> {alt}（同一用户群，只取上限一次）")
        if g.withdrawn_offers:
            out.append("   撤回: " + ",".join(g.withdrawn_offers) + "（不产生需求）")
        if g.note:
            out.append("   说明: " + g.note)

    out.append("")
    out.append("三、投运情景下的可兑现需求范围")
    out.append(_line())
    out.append(f"{'情景':<14}{'投运项目':<22}{'服务片区':<10}{'承诺需求':>8}{'巡演潜力':>8}{'建成能力':>8}")
    for s in report["named_scenarios"]:
        out.append(
            f"{s.name:<14}{'+'.join(s.projects):<22}{','.join(s.zones_served) or '-':<10}"
            f"{s.binding_total:>8}{s.upside_total:>8}{s.capacity_total:>8}")
        detail = "；".join(
            f"{claim} 落 {zone} 范围[{lo}~{hi}]"
            for claim, (zone, lo, hi) in sorted(s.per_claim.items()))
        if detail:
            out.append("   " + detail)
    out.append("")
    out.append("全部依赖封闭组合（含相邻项目不投运的增量视角）共 "
               f"{len(report['all_scenarios'])} 个，--json 可取完整重算结果。")
    return "\n".join(out)


def render_funding(load) -> str:
    report = funding_mod.build_funding_report(load)
    out: list[str] = ["分期出资组合分析", _line("=")]

    out.append("一、配套协议与项目门槛")
    out.append(_line())
    for p, info in sorted(report["startable"].items()):
        status = "可列资金" if info["can_start"] else "受阻"
        agreement = (f"协议signed(owner={info['agreement_owner']})"
                     if info["agreement_signed"] else "缺有效配套协议")
        out.append(f"{p} [{status}] {info['zone']} 第{info['period']}期 {info['stage']} "
                   f"{agreement} 押金已付 {info['deposit_paid']} "
                   f"押金释放 {info['deposit_released']}")
        if info["exclusion"]:
            out.append("   阻断: " + "；".join(info["exclusion"]))
        if info["blockers"]:
            out.append("   下款门槛: " + "；".join(info["blockers"]))
        if info["next_payment_ready"]:
            out.append(f"   ✓ 双方验收齐备，认可能力 {info['accepted_capacity']}，"
                       "可进入下一笔付款")

    out.append("")
    out.append("二、出资方分期余额（押金在验收/退款前冻结，cash 与 in_kind 隔离）")
    out.append(_line())
    out.append(f"{'出资方/期':<10}{'现金预算':>8}{'冻结押金':>8}{'现金可用':>9}{'实物可用':>9}{'共享额度':>9}")
    for (funder, period), pos in sorted(report["positions"].items()):
        out.append(f"{funder}-P{period:<7}{pos.cash_budget:>8}{pos.deposit_frozen:>8}"
                   f"{pos.cash_available:>9}{pos.in_kind_available:>9}{pos.units_available:>9}")

    out.append("")
    out.append("三、可比较的出资组合（受阻组合附未满足约束）")
    out.append(_line())
    out.append(f"{'期':>2} {'结论':<6}{'组合':<20}{'承诺需求':>8}{'能力':>6}{'全款':>6}{'押金':>6}{'共享额度':>8}")
    for c in report["combinations"]:
        verdict = "可行" if c.feasible else "受阻"
        out.append(
            f"{c.period:>2} {verdict:<6}{'+'.join(c.projects):<20}"
            f"{c.binding_demand_supported:>8}{c.capacity_total:>6}"
            f"{c.cost_total:>6}{c.deposit_total:>6}{c.shared_units_total:>8}")
        if c.excluded_reasons:
            for p, reasons in c.excluded_reasons.items():
                out.append(f"   {p} 排除: " + "；".join(reasons))
        for u in c.unmet:
            out.append("   未满足: " + u)

    out.append("")
    out.append("四、已履约款项台账")
    out.append(_line())
    ledger = report["ledger"]
    for funder, cash in ledger["paid_by_funder"].items():
        frozen = ledger["deposit_frozen_by_funder"].get(funder, 0)
        out.append(f"{funder}: 已付 {cash}，其中未验收冻结押金 {frozen}，"
                   f"可释放 {cash - frozen}")
    out.append(f"合计已付 {ledger['total_paid']}，冻结 {ledger['total_frozen']}")

    out.append("")
    out.append("五、分期验收状态（双方 accepted + 真实服务证据才放下笔款）")
    out.append(_line())
    for p, st in sorted(report["stages"].items(), key=lambda kv: kv[0]):
        out.append(
            f"{p}: 运营方验收 {st.operator_accepted}(cap {st.operator_capacity}) "
            f"区县审核 {st.district_accepted}(cap {st.district_capacity}) "
            f"认可能力 {st.accepted_capacity} 服务证据 {','.join(st.service_refs) or '无'}")
        if st.partial_caps:
            out.append("   单方 partial: " + ", ".join(
                f"{who}={cap}" for who, cap in sorted(st.partial_caps.items())))
        for w in st.quality_warnings:
            out.append("   ⚠ " + w)
        if not st.next_payment_ready and st.blockers:
            out.append("   待办: " + "；".join(st.blockers))
    return "\n".join(out)


def _json_default(obj):
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    if isinstance(obj, set):
        return sorted(obj)
    raise TypeError(type(obj).__name__)


def _json_safe(payload):
    """递归把元组键/数据类转成 JSON 可表达的结构。"""
    if hasattr(payload, "__dict__") and not isinstance(payload, (dict, list)):
        payload = payload.__dict__
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            if isinstance(key, tuple):
                key = "-".join(str(part) for part in key)
            out[key] = _json_safe(value)
        return out
    if isinstance(payload, (list, tuple, set)):
        return [_json_safe(item) for item in payload]
    return payload


def print_json(payload) -> None:
    print(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2,
                     sort_keys=False))


# ----------------------------------------------------------------- 子命令

def cmd_report(args) -> int:
    from . import loader
    load = loader.load(Path(args.sources))
    _print_validation(load)
    print(render_demand(load))
    print()
    print(render_funding(load))
    return 0


def cmd_demand(args) -> int:
    from . import loader
    load = loader.load(Path(args.sources))
    _print_validation(load)
    if args.json:
        print_json(demand_mod.build_demand_report(load))
    else:
        print(render_demand(load))
    return 0


def cmd_funding(args) -> int:
    from . import loader
    load = loader.load(Path(args.sources))
    _print_validation(load)
    if args.json:
        print_json(funding_mod.build_funding_report(load))
    else:
        print(render_funding(load))
    return 0


def _print_validation(load) -> None:
    for w in load.warnings:
        print(f"[警告] {w}", file=sys.stderr)
    if not load.ok:
        for e in load.errors:
            print(f"[错误] {e}", file=sys.stderr)
        raise SystemExit(2)


def cmd_submit(args) -> int:
    store = Store(Path(args.data))
    text = Path(args.file).read_text(encoding="utf-8")
    record = store.submit_csv_text(args.kind, text, args.note)
    load = store.current_load(Path(args.sources))
    print(f"已接收回执 {record['submission_id']}（{args.kind}，"
          f"{len(record['rows'])} 行）")
    if load.warnings:
        for w in load.warnings:
            print(f"[警告] {w}")
    if not load.ok:
        for e in load.errors:
            print(f"[错误] {e}")
        return 2
    print("合并后来源校验通过。")
    return 0


def cmd_decide(args) -> int:
    store = Store(Path(args.data))
    snapshot = store.issue_decision(
        Path(args.sources), args.title, args.projects, args.note)
    print(f"已发出决定 {snapshot['decision_id']}: {snapshot['title']}")
    for a in snapshot["approved"]:
        print(f"  批准 {a['project']}（owner={a['owner']}，期 {a['period']}，"
              f"全款 {a['cost']}，共享额度 {a['shared_units']}，"
              f"下款就绪 {a['next_payment_ready']}）")
    for r in snapshot["rejected"]:
        print(f"  拒绝 {r['project']}: " + "；".join(r["reasons"]))
    scope = snapshot["approved_service_scope"]
    print(f"  获准服务范围: 片区 {','.join(scope['zones']) or '-'}，"
          f"承诺需求 {scope['binding_demand']}，能力 {scope['capacity_total']}")
    return 0


def cmd_reevaluate(args) -> int:
    store = Store(Path(args.data))
    results = store.reevaluate(Path(args.sources))
    if not results:
        print("尚无已发出的决定。")
        return 0
    for r in results:
        flag = "需要重审" if r["needs_review"] else "仍然有效"
        print(f"{r['decision_id']} [{flag}] {r['title']}")
        for t in r["triggers"]:
            print("   触发: " + t)
        print(f"   当前承诺需求 {r['current_binding_demand']}，"
              f"已履约款项 {r['paid']}，冻结押金 {r['deposit_frozen']}")
        for key, pos in r["remaining"].items():
            print(f"   余额 {key}: 现金 {pos['cash_available']}，"
                  f"实物 {pos['in_kind_available']}，"
                  f"共享额度 {pos['shared_units_available']}")
    if args.json:
        print_json(results)
    return 0


def cmd_decisions(args) -> int:
    store = Store(Path(args.data))
    records = store.list_decisions()
    if not records:
        print("尚无决定记录。")
        return 0
    for s in records:
        print(f"{s['decision_id']} {s['issued_at']} {s['title']}")
        print("   证据指纹: " + ", ".join(
            f"{t}={h[:10]}…" for t, h in s["source_fingerprints"].items()))
        print("   回执: " + (", ".join(s["submissions_applied"]) or "无"))
        print("   预留: " + json.dumps(s["reserved"], ensure_ascii=False))
        scope = s["approved_service_scope"]
        print(f"   服务范围: {','.join(scope['zones']) or '-'}，"
              f"承诺 {scope['binding_demand']}，能力 {scope['capacity_total']}")
        if s.get("reevaluation"):
            re = s["reevaluation"]
            print(f"   最近重审: {'需要重审' if re['needs_review'] else '有效'}"
                  + ("；" + "；".join(re["triggers"]) if re["triggers"] else ""))
    if args.json:
        print_json(records)
    return 0


def cmd_validate(args) -> int:
    from . import loader
    load = loader.load(Path(args.sources))
    print(f"指纹: {json.dumps(load.fingerprints, indent=2)}")
    print(f"警告 {len(load.warnings)} 条，错误 {len(load.errors)} 条")
    for w in load.warnings:
        print(f"[警告] {w}")
    for e in load.errors:
        print(f"[错误] {e}")
    return 0 if load.ok else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app", description="低空服务配套：需求证据与分期出资组合本地分析")
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--sources", default="sources", help="来源 CSV 目录")

    p = sub.add_parser("report", parents=[common], help="需求+出资完整文本报告")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("demand", parents=[common], help="需求证据与情景分析")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_demand)

    p = sub.add_parser("funding", parents=[common], help="出资组合与验收门槛")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_funding)

    p = sub.add_parser("validate", parents=[common], help="只做来源校验与指纹")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("submit", parents=[common], help="提交经办回执")
    p.add_argument("--data", default="data", help="本地数据目录")
    p.add_argument("--kind", required=True, choices=sorted(SUBMISSION_KINDS))
    p.add_argument("--file", required=True, help="回执 CSV 文件路径")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("decide", parents=[common], help="发出分期决定快照")
    p.add_argument("--data", default="data")
    p.add_argument("--title", required=True)
    p.add_argument("--projects", nargs="+", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("reevaluate", parents=[common], help="资料更正后重审决定")
    p.add_argument("--data", default="data")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_reevaluate)

    p = sub.add_parser("decisions", help="查回历史决定及其证据/预留/范围")
    p.add_argument("--data", default="data")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_decisions)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
