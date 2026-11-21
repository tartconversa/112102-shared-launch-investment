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
