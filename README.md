# 共建设施的需求证据与分期出资组合

低空服务配套设施的试营、客户承诺和分期出资资料。 所有标识、金额、地名和观察值均为合成数据。

## 来源资料

- `sources/pilot_service.csv`
- `sources/customer_commitments.csv`
- `sources/investment_options.csv`
- `sources/funding.csv`
- `sources/agreements.csv`
- `sources/stage_events.csv`

pilot_service 中的 actual 为实际完成的服务数，quality 的 pending 表示服务质量证据未签收。customer_commitments 的 shared_claim 为一个真实客户服务权益的共同编号。withdrawn 可引用此前仍有效的承诺，alternative_to 也可用于有效的替代申请。

investment_options 的 dependencies 是必须具备的先行项目，shared_units 为消耗的配套额度。funding 各期余额分别提供，cash 与 in_kind 不是同一资金类型。

agreements 按记录日反映签署进展，stage_events 分别记录资金、区县、运营方的证据。observed_capacity 是该次认可的总能力，service_ref 指被引用的实际服务记录，cash 是该次款项。

## 本地检查

使用 Python 3.14，仅需标准库。在项目目录执行以下命令检查输入表结构、事件引用和基础记录覆盖。

```sh
python -m unittest discover -s tests -v
```

## 分析应用 `app/`

同一标准库约束下的本地分析应用，四个模块对应四类规划问题：

| 模块 | 职责 |
| --- | --- |
| `app/loader.py` | 来源 CSV 与经办回执的加载、同主键更正合并、结构/外键/语义校验、SHA-256 指纹 |
| `app/demand.py` | 承诺分组（`shared_claim` 去重、替代/撤回处理）、试点证据统计、分投运情景的可兑现需求 |
| `app/funding.py` | 配套协议门槛、共享额度与现金/实物约束、押金冻结与释放、双方验收下款门槛、可比较组合 |
| `app/storage.py` | 经办回执台账、不可变决定快照、客户退出/共同设施迟验的重审 |

### 命令

```sh
python -m app.cli report                       # 需求+出资完整文本报告
python -m app.cli demand --sources sources --json
python -m app.cli funding --sources sources --json
python -m app.cli validate --sources sources   # 只做校验与指纹
python -m app.cli submit --data data --kind commitment --file 回执.csv --note "客户退出"
python -m app.cli decide --sources sources --data data --title 一期拨款 --projects S1 CORE
python -m app.cli reevaluate --sources sources --data data
python -m app.cli decisions --data data        # 查回历史决定的证据、预留额度与服务范围
```

回执 `--kind`：`budget`(funding)、`pilot`(pilot_service)、`commitment`(customer_commitments)、
`option`(investment_options)、`agreement`(agreements)、`stage`(stage_events)，
CSV 表头须与对应来源表完全一致；同主键的后到记录覆盖先到记录，天然支持资料更正。

### 口径要点

- **需求**：公告容量 `offered` 不是需求，只有 `actual` 是真实服务；`quality=pending`
  不进质量确认口径，`actual=0` 是取消/未履约。同一 `shared_claim` 下的替代申请按单个
  申请上限只计一次，`withdrawn` 不产生需求，`show_interest` 只作巡演上行、不进承诺需求。
  没有试点记录的片区（当前为 Z）明确标注为"无可外推证据"。
- **出资**：缺 `signed` 配套协议（含先行项目缺协议）的组合不可列资金；`shared_units`
  按出资方×期设上限，先承诺一地会挤占同周期另一地；`cash` 与 `in_kind` 隔离不互抵；
  押金在双方验收或退款前冻结；只有区县与运营方双方 `accepted` 且引用真实服务记录的
  阶段才放下一笔款，单方 `partial` 不释放下款。
- **决定与重审**：决定快照固化来源指纹、回执编号、当时接受的证据、预留额度与获准服务
  范围，发出后不可变；承诺撤回（客户退出）或共同设施迟验在 `reevaluate` 中标记触发原因、
  剩余出资与已履约款项，原快照仍可完整查回。
