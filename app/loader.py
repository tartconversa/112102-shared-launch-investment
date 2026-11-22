"""来源表与经办回执的加载、更正合并和结构校验。

设计要点
--------
- 来源 CSV 只读，计算 SHA-256 指纹；决定快照保存指纹，资料更正后可识别受影响决定。
- ``storage`` 中的回执行按 ``known_day``/记录日合并：同一主键后到的记录覆盖先到记录，
  更正型回执（同主键新行）与新增型回执走同一条合并路径。
- 校验分两级：``errors`` 为阻断性问题（表头不符、主键缺失、悬空外键、数值非法），
  ``warnings`` 为可继续分析但须提示的问题（如服务质量证据 pending）。
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .models import PRIMARY_KEYS, TABLE_MODELS

HEADERS = {
    "pilot_service": ["service", "zone", "operator", "customer", "offered",
                      "actual", "quality", "service_day", "evidence_day"],
    "customer_commitments": ["offer", "customer", "zone", "operator",
                             "shared_claim", "units", "state", "known_day",
                             "alternative_to"],
    "investment_options": ["project", "zone", "stage", "period", "cost",
                           "deposit", "capacity", "dependencies", "shared_units"],
    "funding": ["funder", "period", "cash", "in_kind", "shared_units"],
    "agreements": ["agreement", "project", "owner", "state", "known_day"],
    "stage_events": ["event", "project", "known_day", "reporter",
                     "observed_capacity", "service_ref", "state", "cash"],
}

# (子表, 外键字段, 父表, 父键字段)；空字符串不参与引用检查
REFERENCES = [
    ("customer_commitments", "alternative_to", "customer_commitments", "offer"),
    ("investment_options", "dependencies", "investment_options", "project"),
    ("stage_events", "project", "investment_options", "project"),
    ("stage_events", "service_ref", "pilot_service", "service"),
]

TABLE_FILES = {name: f"{name}.csv" for name in HEADERS}


def file_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        header = reader.fieldnames or []
        rows = [dict(row) for row in reader]
    return header, rows


@dataclass
class LoadResult:
    """合并后的有效记录与校验结论。"""

    rows: dict[str, list] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fingerprints: dict[str, str] = field(default_factory=dict)
    origins: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _row_key(table: str, raw: dict) -> tuple:
    return tuple(raw.get(k, "") for k in PRIMARY_KEYS[table])


def _merge_table(table: str, source_rows: list[dict],
                 extra_rows: list[tuple[dict, str]]) -> tuple[list[dict], dict[str, str]]:
    """来源行 + 回执行合并。回执行覆盖同主键来源行；回执之间后到者覆盖先到者。

    extra_rows 已按提交顺序排列（先提交在前），因此顺序遍历即可让后者覆盖前者。
    返回去重后的原始 dict 行与 主键->来源标签 的映射。
    """
    chosen: dict[tuple, dict] = {}
    origin_of: dict[tuple, str] = {}
    for raw in source_rows:
        key = _row_key(table, raw)
        chosen[key] = raw
        origin_of[key] = "source"
    for raw, origin in extra_rows:
        key = _row_key(table, raw)
        chosen[key] = raw
        origin_of[key] = origin
    merged = list(chosen.values())
    return merged, {key[0] if len(key) == 1 else "|".join(key): origin
                    for key, origin in origin_of.items()}


def load(sources_dir: Path, extra: dict[str, list[tuple[dict, str]]] | None = None
         ) -> LoadResult:
    """加载来源目录并合并回执。``extra[table]`` 为 (原始行, 来源标签) 列表。"""
    sources_dir = Path(sources_dir)
    extra = extra or {}
    result = LoadResult()
    raw_tables: dict[str, list[dict]] = {}

    for table, filename in TABLE_FILES.items():
        path = sources_dir / filename
        if not path.exists():
            result.errors.append(f"缺少来源文件: {filename}")
            continue
        result.fingerprints[table] = file_fingerprint(path)
        header, rows = _read_csv(path)
        if header != HEADERS[table]:
            result.errors.append(
                f"{filename} 表头不符: 得到 {header}, 期望 {HEADERS[table]}")
        raw_tables[table] = rows
        for raw in rows:
            if None in raw:
                result.errors.append(f"{filename} 存在列数不符的行: {raw}")
            elif not raw.get(HEADERS[table][0]):
                result.errors.append(f"{filename} 存在主键为空的行: {raw}")

    # 回执表头同样需要符合契约
    for table, items in extra.items():
        if table not in HEADERS:
            result.errors.append(f"回执引用未知表: {table}")
            continue
        for raw, _origin in items:
            if list(raw.keys()) != HEADERS[table]:
                result.errors.append(f"回执 {table} 表头不符: {list(raw.keys())}")

    if result.errors:
        return result

    # 合并并构造模型对象
    merged_raw: dict[str, list[dict]] = {}
    for table in HEADERS:
        merged, origin_map = _merge_table(
            table, raw_tables[table], extra.get(table, []))
        merged_raw[table] = merged
        result.origins[table] = origin_map
        model = TABLE_MODELS[table]
        objects = []
        for raw in merged:
            try:
                objects.append(model.from_row(raw, origin_map[_pk_text(table, raw)]))
            except (ValueError, KeyError) as exc:
                result.errors.append(f"{table} 行无法解析 {raw}: {exc}")
        result.rows[table] = objects

    _check_references(result, merged_raw)
    _check_semantics(result)
    return result


def _pk_text(table: str, raw: dict) -> str:
    key = _row_key(table, raw)
    return key[0] if len(key) == 1 else "|".join(key)


def _check_references(result: LoadResult, raw_tables: dict[str, list[dict]]) -> None:
    for child, fk, parent, pk in REFERENCES:
        parent_keys = {row[pk] for row in raw_tables[parent]}
        for row in raw_tables[child]:
            value = (row.get(fk) or "").strip()
            if not value:
                continue
            # dependencies 可能是 '|' 分隔的多个项目
            for token in value.split("|"):
                token = token.strip()
                if token and token not in parent_keys:
                    result.errors.append(
                        f"{child}.{fk} 悬空引用 '{token}'（记录 {row}）")


def _check_semantics(result: LoadResult) -> None:
    """业务口径的软校验：只发警告，不阻断分析。"""
    for pilot in result.rows.get("pilot_service", []):
        if pilot.actual > pilot.offered:
            result.warnings.append(
                f"{pilot.service} 实际服务 {pilot.actual} 超过公告容量 "
                f"{pilot.offered}：公告容量不能当作需求上限，以实际服务为准")
        if pilot.quality == "pending":
            result.warnings.append(
                f"{pilot.service} 服务质量证据未签收(quality=pending)，"
                "实际服务暂不计入质量确认口径")
        if pilot.actual == 0:
            result.warnings.append(
                f"{pilot.service} 实际服务为 0（取消/未履约），公告容量 "
                f"{pilot.offered} 不能计为需求")
    for commit in result.rows.get("customer_commitments", []):
        if commit.state == "withdrawn" and not commit.alternative_to:
            result.warnings.append(
                f"{commit.offer} 已撤回且未指向替代申请")
    for event in result.rows.get("stage_events", []):
        if event.state == "accepted" and not event.service_ref:
            result.errors.append(
                f"{event.event} 验收通过但缺少真实服务证据 service_ref")
        if event.state == "accepted" and event.service_ref:
            pilots = {p.service: p for p in result.rows["pilot_service"]}
            pilot = pilots.get(event.service_ref)
            if pilot is not None and pilot.quality == "pending":
                result.warnings.append(
                    f"{event.event} 引用的 {event.service_ref} 质量证据 pending，"
                    "该期能力只作暂认，付款前须补签收")
