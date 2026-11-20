# 合成输入数据

用于跟进河段修复的合成水质观察、生态调查、污染源处置与社区反馈。
所有记录均为虚构，仅供本地开发使用，日期与数值均为示例。

## 文件与字段

| 文件 | 说明 |
| --- | --- |
| `reaches.csv` | 河段与所属治理项目；`upstream_km`/`downstream_km` 为里程边界 |
| `review_criteria.csv` | 项目复核准则；`operator` 支持 ge/le/eq/gt/lt，`valid_from`/`valid_to` 限定生效区间 |
| `evidence.csv` | 水质（water）与生态（ecology）观测；`season` 留空时按日期自动归入气象季节 |
| `pollution_sources.csv` | 已知污染源及其里程位置 |
| `disposals.csv` | 处置记录；必须显式给出 `source_id` 或里程覆盖区间，且区间不得越出河段 |
| `reports_actions.csv` | 社区反馈（kind=community）与事项；`linked_group` 可显式标注重复反馈 |
| `inspection_policy.csv` | 各河段标准/降频巡查间隔（天），要求 reduced < standard |

## 夹具中的领域规则示例

- `evidence.csv` 中 R-B 在春季（E06, DO 7.4）与夏季（E02, DO 5.2）各有读数，
  两者分属不同季节基线，复核时不得平均成“达标”。
- `reports_actions.csv` 中 C01、C02 描述同一处油膜迹象，应用自动聚类为
  一个反馈簇，只计一项独立证据。
- `disposals.csv` 的 D01 只覆盖 R-A 的 P02；R-B 的 P01 仅有在途处置 D02，
  不能据此判定 P01 已消除。
- R-C 的 E08/E09 同为夏季且结论相反（DO 5.8 与 7.1），构成矛盾证据，
  复核结论保留“不确定”并阻止标记为已恢复。

`../examples/batch_july/` 是增量导入演示批次，用于展示新观测到达后的
结论修订、反馈关联与巡查安排更新。
