# 河段生态修复复核

面向流域管理人员的河段治理复核应用：导入带时间与河段范围的水质观测、
生态调查、污染源处置记录和社区反馈，按治理项目与河段形成**可解释的
复核结论**，支持事项分派与新观测到达后的待更新清单。

仅使用 Python 3.11+ 标准库，无第三方依赖。

## 领域规则（不可违背，均有测试覆盖）

1. **季节/河段隔离**：不同采样季节、不同河段的观测分组评估，绝不跨组
   并入同一基线或求平均。同季同段同时出现达标与不达标读数判为
   “证据矛盾”。
2. **重复反馈不重复计数**：社区反馈按显式 `linked_group` 或自动聚类
   （同河段、同归一化描述、30 天窗）关联为一个反馈簇，整簇只算一项
   独立证据；跨河段不得合并。
3. **处置只支持实际覆盖范围**：只有 `complete` 且完成日期不晚于复核
   基准日、并显式覆盖污染源（精确 `source_id` 或同河段里程区间）的
   处置记录，才能消除该污染源的阻碍；在途/计划处置与其他河段的处置
   均不生效。
4. **不确定性阻止恢复标记**：证据矛盾、缺少观测、观测超时效、开放
   反馈簇、未处置污染源、开放高风险事项任一存在时，河段结论只能是
   `uncertain` 或 `not_recovered`，且不允许进入低频巡查；项目汇总
   要求其下所有河段均 `recovered`。
5. **全程可回看**：结论修订（含前后状态、原因码、采用证据）、事项
   分派与状态变化、巡查调整均写入仅追加的 `events.jsonl` 审计日志。

## 目录结构

```
riverreview/
  models.py      领域模型、状态与原因码常量
  seasons.py     气象季节划分与跨年排序键
  ingest.py      CSV 解析与结构/外键/覆盖区间校验
  clustering.py  社区反馈去重聚类（并查集）
  coverage.py    处置记录覆盖判定
  review.py      准则→河段→项目复核引擎与增量差异
  schedules.py   巡查频率调度
  events.py      仅追加 JSONL 审计日志
  store.py       工作区状态持久化（state.json + events.jsonl）
  cli.py         命令行入口
data/            合成输入夹具（字段说明见 data/README.md）
examples/batch_july/  增量导入演示批次
tests/           夹具契约测试与领域规则单元测试
```

## 快速开始

```bash
# 1. 从 CSV 目录初始化工作区（状态写入 .riverreview/）
python -m riverreview init data

# 2. 以指定基准日执行复核，输出每个河段的准则评估、采用证据与原因码
python -m riverreview review --as-of 2026-07-01

# 3. 查看河段/项目的可解释结论
python -m riverreview reaches
python -m riverreview conclusion R-B
python -m riverreview conclusion REST-1

# 4. 查看反馈聚类与事项
python -m riverreview actions

# 5. 分派调查/处置事项（可由社区反馈派生），记录责任方、期限与证据
python -m riverreview assign T01 --kind investigation \
    --from-report C04 --owner ecologist --due 2026-07-20
python -m riverreview update-action A02 --status complete \
    --add-evidence D03 --note "cleanup verified"

# 6. 查看并应用巡查频率调整（只有 recovered 河段可降频）
python -m riverreview schedule
python -m riverreview schedule --apply

# 7. 新观测到达：增量导入并输出结论/事项/巡查待更新清单
python -m riverreview ingest examples/batch_july --as-of 2026-07-05

# 8. 回看证据采用、待办变化与结论修订原因
python -m riverreview history
python -m riverreview history --event-type conclusion_revised --target R-B
```

> 在隔离模式（`python -I`）下运行时，标准库不会自动把当前目录加入
> `sys.path`，可用
> `python -I -c "import sys; sys.path.insert(0,'.'); from riverreview.cli import main; main()" ...`
> 引导；测试文件已内置该引导。

## 结论状态与原因码

- 河段结论：`recovered`（已达标，可申请降频）、`not_recovered`（有明确
  未达标证据或未决阻碍）、`uncertain`（证据矛盾/覆盖不全，禁止标记恢复）。
- 原因码（写入审计日志，稳定可检索）：`THRESHOLD_FAIL`、
  `CONTRADICTORY_EVIDENCE`、`NO_BASELINE_DATA`、`STALE_EVIDENCE`、
  `OPEN_COMMUNITY_CLUSTER`、`OPEN_HIGH_RISK_ACTION`、
  `SOURCE_NOT_ADDRESSED`、`DUPLICATE_LINKED`、`SEASON_SPLIT`、
  `OUTSIDE_VALIDITY`。

## 本地验证

```bash
python -m unittest discover -s tests -v   # 27 项测试：契约 + 领域规则 + CLI
python -m compileall -q tests riverreview # 语法检查
```
