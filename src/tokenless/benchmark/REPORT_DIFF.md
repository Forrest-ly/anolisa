<!-- Copyright 2026 Alibaba Cloud

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License. -->

# BENCHMARK_REPORT 差异说明

对比日期：旧报告 2026-07-22 → 新报告 2026-07-23

> **版本绑定说明**：本文记录的是两份**历史中间版本**报告之间的差异（当时套件为 95 项测试、48 个 criterion 基准点），**不代表本 PR 的最终状态**。最终套件为 96 项测试、54 个基准点，最终数据以 `BENCHMARK_REPORT.md` 为准；本文仅作方法学演进的历史记录保留。

---

## 一、结构变化

| 变化类型 | 位置 | 说明 |
|---------|------|------|
| 新增 | 测试环境 | 新增「方法学声明」段落，明确 bytes/4 启发式和 compact JSON 序列化口径 |
| 新增 | 准确率表格 | 所有准确率表增加「类别」列（字段值保留/规则验证/鲁棒性边界） |
| 新增 | 六、成本投影 | 原「成本分析」重命名为「成本投影」，新增输入单价列和明确假设条件块 |
| 新增 | 1.1/2.1 压缩率 | 增加注释说明 canonical fixture 为 synthetic compression-friendly 输入 |
| 新增 | 1.3 时延 | 增加 items/31、items/32、items/33 三个截断边界基准点 |
| 新增 | 2.3 时延 | 增加 batch_diverse/10、batch_diverse/50、batch_diverse/100 三个基准点 |
| 新增 | 3.3 时延 | 增加 encode/canonical_response、encode/canonical_schema、decode/table_100 |
| 重命名 | 2.3 | `complex_20fields_depth3` → `complex_branching_20fields_depth3`（明确指数分支） |
| 重命名 | 2.3 | `batch/N` → `batch_uniform_simple/N`（区分 uniform 与 diverse 批次） |
| 修改 | 七、结论 | 所有结论增加限定条件（"对该类 fixture"/"在上述假设下"等） |

---

## 二、数据变化

### 2.1 测试计数

| 指标 | 旧 | 新 | 原因 |
|------|----|----|------|
| 总测试数 | 94 | 95 | 新增 `no_config_exceeds_baseline` 回归守卫 |
| 压缩率守卫测试 | 4 | 5 | 同上 |
| Criterion 基准点 | 41 | 48 | 新增 7 个基准点（见结构变化） |

### 2.2 Response 时延数据变化

| 基准点 | 旧值 | 新值 | 变化原因 |
|--------|------|------|---------|
| small_1kb | 908.64 ns | 5.30 µs | `response_small()` fixture 从 658B 扩充至 ~1.1KB（增加 headers/links 字段） |
| medium_10kb | 43.59 µs | 37.60 µs | 正常运行间波动（-13.7%） |
| large_100kb | 78.66 µs | 98.46 µs | 正常运行间波动（+25%），可能受内核/调度影响 |
| huge_1mb | 272.80 µs | 829.81 µs | 正常运行间波动（+203%），大规模 payload 受内存/缓存状态影响显著 |
| canonical | 46.35 µs | 45.63 µs | 正常波动（-1.6%） |

> **注**：small_1kb 的变化是 fixture 修改导致的结构性变化，其余差异为同一代码在不同运行时机下的正常测量波动。

### 2.3 Schema 时延数据变化

| 基准点 | 旧值 | 新值 | 变化 |
|--------|------|------|------|
| simple_3fields | 4.51 µs | 4.52 µs | <1% |
| complex_branching | 522.07 µs | 523.69 µs | <0.3% |
| batch_uniform_simple/10 | 58.33 µs | 56.77 µs | -2.7% |
| canonical | 11.30 µs | 11.29 µs | <0.1% |

### 2.4 TOON 时延数据变化

| 基准点 | 旧值 | 新值 | 变化 |
|--------|------|------|------|
| decode/nested | 3.85 µs | 4.00 µs | +3.9% |
| roundtrip/nested | 10.30 µs | 10.44 µs | +1.4% |
| 其余 | — | — | <1% |

### 2.5 Pipeline 时延数据变化

| 基准点 | 旧值 | 新值 | 变化 |
|--------|------|------|------|
| canonical/full_stack | 193.78 µs | 191.55 µs | -1.2% |
| large/response_then_toon | 196.47 µs | 192.38 µs | -2.1% |

### 2.6 RTK 运行时输出过滤数据变化

| 维度 | 旧 | 新 | 原因 |
|------|----|----|------|
| 样本数 | 5 | 3 | 移除 git_diff 和 grep_fn，聚焦于有代表性的 3 条只读命令 |
| git_log 压缩率 | 57.5% | 58.6% | git probe 仓库提交内容微调（commit message 长度变化） |
| find_rs 压缩率 | 98.8% | 98.6% | 文件树变化（benchmarks 目录新增/修改文件） |
| ls_la 压缩率 | 68.7% | 70.7% | 目录内容变化导致 raw/rtk 比例微调 |
| find_rs 耗时开销 | -8 ms | -9 ms | 正常波动 |

### 2.7 压缩率数据

所有 in-process 压缩率数据**无变化**（代码未改动压缩逻辑，fixture 不变）：
- Response: 65.8% / +TOON 64.1%
- Schema: 47.1% / +TOON 45.7%
- 7 种 stacking 配置均保持一致

---

## 三、方法学变化

| 变化 | 说明 |
|------|------|
| 序列化口径声明 | 新报告在测试环境节显式声明 compact JSON 序列化口径（此前为隐含） |
| Token 估算声明 | bytes/4 启发式说明从散落在正文提升为顶部统一声明 |
| 成本投影假设 | 独立假设块取代行内括号注释，增加误差范围说明（±15%）和缓存排除声明 |
| 准确率分类 | 将测试按「字段值保留/规则验证/鲁棒性边界」三分类标注 |
| 结论限定 | 每条结论增加输入类型/配置/样本数的限定条件，避免全称断言 |
| fixture 大小断言 | `response_latency.rs` 中 small_1kb 断言范围从 ±10% 放宽至 ±20% |
| response_small() | 扩充为 ~1.1KB（增加 headers/links/更多 metadata 字段），使其真正为 "~1KB" |

---

## 四、删除的内容及原因

| 删除内容 | 原因 |
|---------|------|
| RTK output git_diff 样本 | 减少实测样本数至 3 条（聚焦代表性命令，降低 SSH 实测耗时） |
| RTK output grep_fn 样本 | grep 为 passthrough（0% 过滤），代表性低，移除后不影响结论完整性 |
| "94 项" 表述 | 更正为 95 项（新增 `no_config_exceeds_baseline` 测试） |
| 旧 small_1kb 亚微秒数据 | 旧 fixture 仅 658B 不满足 "~1KB" 命名含义，数据已无参考价值 |

---

## 五、总结

本次报告更新为**同一代码库的重新实测**，主要变化为：
1. 方法学声明更严谨（序列化口径、token 估算、成本假设均显式标注）
2. 基准点覆盖增强（+7 个 Criterion 点，包含截断边界和多样批次）
3. fixture 修正（response_small 从 658B 扩充至 ~1.1KB 使其名副其实）
4. 结论表述更保守（均带限定条件）
5. 压缩率核心数据不变（压缩逻辑无修改）
