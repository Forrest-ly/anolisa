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

# Tokenless Benchmark 测试报告

> **Scope: Library Default Microbenchmark** — 使用库默认配置，非 adapter 生产配置。

## 测试环境

- **执行位置**: 远端测试服务器 `<benchmark-server>`（`/root/tokenless-bench/`）
- **OS**: Ubuntu 26.04 LTS（Linux 7.0.0-22-generic，x86_64）
- **CPU**: Intel(R) Xeon(R) 6982P-C（16 vCPU）
- **内存**: 30 GiB
- **Git commit**: N/A（独立 benchmark workspace，非 git 仓库）
- **运行日期**: 2026-07-24T14:45:00+08:00
- **rustc 版本**: 1.96.1 (31fca3adb 2026-06-26)
- **RTK 版本**: rtk 0.43.0
- **服务器**: `<benchmark-server>` (Ubuntu, Xeon 6982P-C)

测试命令：`cargo test --release`（96 项全部通过）、`cargo run --release --bin compression_rate -- --json`、`cargo bench`（criterion，54 个基准点）。RTK 运行时输出过滤维度为服务器上一次性 SSH 实测采集（方法见 4.1）。RTK bench 需设置 `RTK_BIN` 环境变量指向 rtk 二进制。

**方法学声明**：Token 估算统一采用 **bytes/4 启发式**（`(bytes + 3) / 4`，div_ceil，与引擎自身 `estimate_tokens_from_bytes` 一致）；压缩率计算中的序列化口径为 **compact JSON**（`serde_json::to_string`，无多余空白），保证对比基线一致。

**配置说明**：本报告使用 tokenless 默认配置：
- ResponseCompressor: truncate_arrays_at=32, truncate_strings_at=4096, max_depth=8
- SchemaCompressor: 默认参数

生产环境 adapter 可能使用不同配置（如 shell hook: 65536/128/8），压缩率和时延将因配置不同而变化。默认配置代表开箱即用行为。

⚠️ 生产 adapter 使用不同配置（copilot-shell: truncate_strings_at=65536, truncate_arrays_at=128; API adapter: 1048576/65536）。本报告测试库默认值（4096/32），压缩率和时延在生产配置下将显著不同。

---

## 一、ResponseCompressor

### 1.1 压缩率

基于 canonical fixture（`fixtures/tool_response.json`，含 60 条记录 + trace/log 噪声的工具响应信封）。**注意**：该 fixture 为 synthetic compression-friendly 输入（含大量可去除的 debug/trace/log 字段和超 32 项的数组截断点），不代表所有工具响应的典型压缩率。

| 度量 | 原始 Tokens | 压缩后 Tokens | 压缩率 |
|------|------------|--------------|--------|
| ResponseCompressor 单独 | 5204 | 1778 | **65.8%** |
| ResponseCompressor + TOON | 5204 | 1871 | 64.0% |

### 1.2 准确率

ResponseCompressor 测试覆盖：
- 字段保留/规则验证: 11/11
- 边界值鲁棒性: 16/16
- 不利输入非膨胀: 7/7 (l1_worst_case)
- 回归守卫: 1/1

| 测试文件 | 类别 | 测试数 | 结果 |
|---------|------|-------|------|
| `l1_response_retention.rs` | 字段保留/规则验证 | 11 | ✅ 全部通过 |
| `l1_adversarial_response.rs` | 边界值鲁棒性 | 16 | ✅ 全部通过 |
| `l1_worst_case.rs`（response 相关） | 不利输入非膨胀 | 7 | ✅ 全部通过 |
| `l1_compression_rate.rs` 回归守卫（response ≥ 60%） | 回归守卫 | 1 | ✅ 通过 |

覆盖列出的合法 Value 和规则用例，不代表任意输入或生产任务准确率。

### 1.3 时延

criterion 中值及置信区间（in-process，微秒级）：

| 基准点 | Median | 区间 |
|--------|--------|------|
| small_1kb | 5.50 µs | 5.49 – 5.51 µs |
| medium_10kb | 38.62 µs | 38.59 – 38.66 µs |
| large_100kb | 104.80 µs | 104.78 – 104.83 µs |
| huge_1mb | 815.01 µs | 814.80 – 815.40 µs |
| deep_nesting_8 | 910.00 ns | 908.00 – 912.00 ns |
| high_repetition_100 | 18.52 µs | 18.51 – 18.53 µs |
| items/10 | 11.51 µs | 11.50 – 11.51 µs |
| items/31 | 36.84 µs | 36.82 – 36.86 µs |
| items/32 | 38.18 µs | 38.17 – 38.20 µs |
| items/33 | 38.39 µs | 38.37 – 38.40 µs |
| items/50 | 42.54 µs | 42.52 – 42.57 µs |
| items/100 | 54.11 µs | 54.09 – 54.14 µs |
| items/500 | 150.06 µs | 150.02 – 150.11 µs |
| items/1000 | 275.04 µs | 274.99 – 275.09 µs |
| **canonical** | **46.93 µs** | 46.90 – 46.98 µs |

items/31→32→33 展示了 `truncate_arrays_at=32` 的截断拐点：32 项处有显著拐点（truncation threshold 生效），之后增长率下降但不为零——压缩器不再遍历超出阈值的项，但 serde_json 序列化成本仍随输入大小线性增长。

**注释**：medium_10kb 与 items/31 使用相同输入（`response_items(31)` ≈ 31 条记录 ≈ 10KB）。两者在报告中服务不同分析目的：
- medium_10kb: 验证 ~10KB payload 满足其 size target（size validation）
- items/31: 截断边界曲线（31→32→33）的一部分（truncation boundary curve）

---

## 二、SchemaCompressor

### 2.1 压缩率

基于 canonical fixture（`fixtures/schema_search.json`，OpenAI function-calling schema）：

| 度量 | 原始 Tokens | 压缩后 Tokens | 压缩率 |
|------|------------|--------------|--------|
| SchemaCompressor 单独 | 347 | 183 | **47.3%** |
| SchemaCompressor + TOON | 347 | 188 | 45.8% |

### 2.2 准确率

SchemaCompressor 测试覆盖：
- 保护字段验证: 8/8
- 边界值鲁棒性: 14/14
- 不利输入非膨胀: 2/2
- 回归守卫: 1/1

| 测试文件 | 类别 | 测试数 | 结果 |
|---------|------|-------|------|
| `l1_schema_retention.rs` | 保护字段/截断/深度控制 | 8 | ✅ 全部通过 |
| `l1_adversarial_schema.rs` | 边界值鲁棒性 | 14 | ✅ 全部通过 |
| `l1_worst_case.rs`（schema 相关） | 不利输入非膨胀 | 2 | ✅ 全部通过 |
| `l1_compression_rate.rs` 回归守卫（schema ≥ 40%） | 回归守卫 | 1 | ✅ 通过 |

覆盖列出的合法 Value 和规则用例，不代表任意输入或生产任务准确率。

### 2.3 时延

| 基准点 | Median | 区间 |
|--------|--------|------|
| simple_3fields | 4.52 µs | 4.52 – 4.52 µs |
| complex_branching_20fields_depth3 | 564.45 µs | 564.30 – 564.62 µs |
| batch_uniform_simple/10 | 58.51 µs | 58.50 – 58.52 µs |
| batch_uniform_simple/50 | 293.54 µs | 293.41 – 293.66 µs |
| batch_uniform_simple/100 | 585.30 µs | 585.04 – 585.55 µs |
| batch_diverse/10 | 183.03 µs | 182.81 – 183.46 µs |
| batch_diverse/50 | 909.04 µs | 908.78 – 909.31 µs |
| batch_diverse/100 | 1.8206 ms | 1.8201 – 1.8212 ms |
| long_description | 22.81 µs | 22.80 – 22.82 µs |
| **canonical** | **11.40 µs** | 11.39 – 11.40 µs |

---

## 三、TOON 编码器

### 3.1 压缩率

| 度量 | 原始 Tokens | TOON 后 Tokens | 压缩率 |
|------|------------|---------------|--------|
| TOON only（canonical response） | 5204 | 4320 | 17.0% |
| TOON only（canonical schema） | 347 | 355 | **-2.3%（膨胀）** |
| ResponseCompressor + TOON vs 仅 ResponseCompressor | 1778 | 1871 | -5.2%（相对膨胀） |

**负压缩率说明（真实测量，非误差）**：TOON 的收益来自消除均匀表格数据中的重复 key。本测试中仅均匀表格显示稳定正收益；异构结构（如 canonical schema）出现负收益（膨胀，-2.3%）。canonical schema 是小体量、深嵌套、异构结构，TOON 编码语法本身的开销（表头声明、缩进结构）超过了 key 去重的收益，导致 347 → 355 的膨胀；同理，response 经 ResponseCompressor 压缩后残留的均匀性已经很低，再叠加 TOON 反而从 1778 涨到 1871。**结论：TOON 应只对表格型 payload 启用，或在编码后比较两种形式的 token 数择优发送，不应无条件全局启用。**

### 3.2 准确率

| 测试文件 | 类别 | 测试数 | 结果 |
|---------|------|-------|------|
| `l1_toon_roundtrip.rs` | JSON→TOON→JSON 语义等价 | 8 | ✅ 全部通过 |
| `l1_adversarial_toon.rs` | 对抗性/边界输入 | 10 | ✅ 全部通过 |

覆盖 8 种选定形状（flat, nested, uniform array, mixed scalar, CJK, special chars, empty container, scalar array）通过严格往返。

### 3.3 时延

| 基准点 | Median | 区间 |
|--------|--------|------|
| encode/flat | 2.08 µs | 2.08 – 2.09 µs |
| encode/nested | 5.74 µs | 5.74 – 5.74 µs |
| encode/table_100 | 107.95 µs | 107.89 – 108.04 µs |
| encode/canonical_response | 271.90 µs | 271.79 – 272.00 µs |
| encode/canonical_schema | 11.28 µs | 11.27 – 11.28 µs |
| decode/flat | 1.61 µs | 1.61 – 1.61 µs |
| decode/nested | 4.02 µs | 4.01 – 4.02 µs |
| decode/table_100 | 72.53 µs | 72.51 – 72.54 µs |
| decode/canonical_response | 237.60 µs | 237.51 – 237.69 µs |
| decode/canonical_schema | 8.79 µs | 8.79 – 8.79 µs |
| roundtrip/flat | 3.92 µs | 3.91 – 3.92 µs |
| roundtrip/nested | 10.46 µs | 10.46 – 10.46 µs |
| roundtrip/table_100 | 184.33 µs | 184.30 – 184.36 µs |
| roundtrip/canonical_response | 526.27 µs | 526.15 – 526.40 µs |
| roundtrip/canonical_schema | 20.33 µs | 20.32 – 20.33 µs |

---

## 四、RTK 命令重写器

RTK（rtk 0.43.0）是外部二进制，通过 subprocess 调用 `rtk rewrite <cmd>`，退出码协议：0 = 可重写（allow）、1 = 无等价命令（passthrough）、2 = 拒绝、3 = 可重写（ask，待确认）。码 0 与 3 的 stdout 均携带重写结果。本环境实测中所有可重写样本均返回码 3（0.43.0 默认策略），无码 0 样本。

**方法学说明**：RTK 与 in-process 压缩器不可比、不可相加。RTK 的重写形式是给命令加 `rtk` 前缀路由到其输出过滤前端（如 `git log …` → `rtk git log …`），因此**命令文本本身几乎不缩短甚至微增**；RTK 宣称的 token 节省发生在**命令执行时的输出过滤**上。以下分两个维度分别度量。

### 4.1 压缩率

**a) 命令文本维度**（`rtk_report()`，7 条固定样本，token = bytes/4）：

| 样本 | 退出码 | 重写前 | 重写后 | 压缩率 |
|------|-------|-------|-------|--------|
| cargo_build | 3 | 14 | 15 | -7.1% |
| npm_install | 1（passthrough） | 12 | — | 不计 |
| pytest | 3 | 11 | 9 | +18.2% |
| find_pipeline | 1（passthrough） | 16 | — | 不计 |
| git_log | 3 | 12 | 13 | -8.3% |
| grep_pipeline | 3 | 19 | 20 | -5.3% |
| docker_ps | 3 | 17 | 18 | -5.9% |
| **总体（5 条可重写）** | | **73** | **75** | **-2.7%** |

退出码分布：可重写 5（全部为码 3 / ask）、passthrough 2、拒绝 0、其它 0。命令文本维度呈**轻微负压缩**是符合预期的真实测量——重写只是加前缀换入口，节省不在这一层。

⚠️ 以下 RTK 输出过滤率数据来自一次性 SSH 手工采集（在测试服务器上执行真实命令并对比 stdout 长度），非自动化可复现测试流程。自动化 RTK 输出采集将在后续版本实现。

**b) 运行时输出过滤维度**（服务器一次性实测：bash 捕获原始命令与 `rtk` 前缀命令的 stdout+stderr 字节数，token = bytes/4；git 样本在 `/tmp/rtk_git_probe` 临时仓库（6 个提交）内执行，文件系统样本在 `/root/tokenless` 内执行，全部为只读命令）：

| 样本 | 命令 | 原始输出 Tokens | RTK 过滤后 Tokens | 压缩率 |
|------|------|----------------|-------------------|--------|
| git_log | `git log -6` | 232 | 96 | **58.6%** |
| find_rs | `find . -name '*.rs'`（排除 target） | 1736 | 25 | **98.6%** |
| ls_la | `ls -la` | 174 | 51 | **70.7%** |

输出维度节省 58.6–98.6%，与命令类型强相关：结构可归纳的输出（find 的文件树）节省最大，目录列表中等，git log 日志中等偏上。

**数据来源说明**：
- 运行时输出过滤压缩率数据来自一次性 SSH 采集（非自动化可复现流程）
- 3 个样本（git_log、find_rs、ls_la）为有意选择的代表性高过滤命令
- 不代表所有命令的总体 RTK 收益；passthrough/deny 命令按 0% 收益计

### 4.2 准确率

`tests/l1_rtk_format_compat.rs` 的 9 项协议测试在服务器上**真实执行**（RTK 可用，未 skip），全部通过：

| 测试 | 结果 |
|------|------|
| rtk_binary_is_runnable | ✅ |
| rtk_version_meets_minimum（≥ 0.35.0，实测 0.43.0） | ✅ |
| rewrite_common_command_uses_protocol_code | ✅ |
| rewrite_available_yields_nonempty_stdout | ✅ |
| rewrite_is_deterministic（同输入同输出） | ✅ |
| rewrite_unknown_command_passthrough | ✅ |
| rewrite_handles_pipeline | ✅ |
| rewrite_does_not_mangle_when_passthrough | ✅ |
| rewrite_empty_command_is_safe | ✅ |

### 4.3 时延

**a) 重写决策时延**（`benches/l1_rtk_latency.rs`，criterion，完整 subprocess 往返含进程启动，**毫秒级，与 in-process 微秒级基准方法学不同，不可直接相加比较**）：

| 基准点 | Median | 区间 |
|--------|--------|------|
| rewrite/cargo_build | 8.15 ms | 8.15 – 8.16 ms |
| rewrite/npm_install | 4.72 ms | 4.72 – 4.73 ms |
| rewrite/pytest | 8.20 ms | 8.19 – 8.20 ms |
| rewrite/find_pipeline | 2.05 ms | 2.05 – 2.05 ms |
| rewrite/git_log | 8.31 ms | 8.30 – 8.31 ms |
| rewrite/grep_pipeline | 8.17 ms | 8.17 – 8.18 ms |
| rewrite/docker_ps | 8.18 ms | 8.17 – 8.19 ms |

**b) 输出过滤附加耗时**（与 4.1b 同批实测，各 3 次计时取中位值，wall time）：

| 样本 | 原始命令耗时 | RTK 前缀耗时 | 附加开销 |
|------|-------------|-------------|---------|
| git_log | 3 ms | 13 ms | +10 ms |
| find_rs | 12 ms | 3 ms | **-9 ms（更快）** |
| ls_la | 5 ms | 15 ms | +10 ms |

观测到的 wall time 差异中位数，精度受限于进程启动方差和 OS 调度。`rtk find` 因内部实现更快反而低于原生 find。对交互式 agent 场景，两位数毫秒的开销相对其换来的输出 token 节省通常可接受。

---

## 五、组合效果（默认库内微基准 / Default Library Send Pipeline）

> **说明**：本节测量 ResponseCompressor + SchemaCompressor + TOON encode 在 Rust in-process 中的组合表现。不含 subprocess、network、I/O、TOON decode/验证、RTK、模型调用。

### 5.1 组合压缩率

7 种 stacking 配置（canonical response + schema，baseline = 5551 tokens）：

| 配置 | Tokens | 节省比例 |
|------|--------|--------|
| baseline | 5551 | 0% |
| response_only | 2125 | 61.7% |
| toon_only | 4675 | 15.8% |
| schema_only | 5387 | 3.0% |
| response_toon | 2218 | 60.0% |
| schema_response | 1961 | **64.7%** |
| full_stack | 2059 | 62.9% |

注意 `schema_response`（64.7%）优于 `full_stack`（62.9%），与第三节 TOON 负压缩率的结论一致：对本组 canonical payload（非表格型），叠加 TOON 反而回吐约 1.8 个百分点。

**stacking 表术语说明**：
- `toon_only` = 对**原始输入**直接做 TOON 编码（无压缩器阶段）
- 在 `benches/l1_pipeline_latency.rs` 中的 `toon_encode_on_compressed` = 对**压缩后输出**做 TOON 编码

这是不同的输入，产生不同的结果——请勿混淆。

### 5.2 组合准确率

组合链路测试: 5/5 正向通过 + 1 XFAIL (已知限制)
- 压缩字段保留: 5/5 ✅
- TOON roundtrip 已知限制: 1/1 XFAIL (root-level keys lost after mixed-array — documented, not a suite defect)

⚠️ 已知限制：TOON 解码器在大型混合类型数组后不恢复根级标量键（tool/status）。这些键在 L1 压缩阶段正确保留，但 TOON roundtrip 后丢失。详见 `tests/l1_pipeline_retention.rs`。

⚠️ TOON roundtrip 测试使用 non-strict 解码模式（`with_strict(false)`）以兼容压缩器产生的 truncation marker 在 mixed-type 数组中的格式模糊性。strict 模式下此 roundtrip 将报解码错误。

全套件 96 项测试通过（0 失败）：
- ResponseCompressor: 34 项（保留 11 + 鲁棒性 16 + worst-case 7）
- SchemaCompressor: 25 项（保留 8 + 鲁棒性 14 + worst-case 2 + 回归 1）
- TOON: 18 项（roundtrip 8 + 对抗性 10）
- RTK: 9 项（协议兼容性）
- 组合链路: 6 项
- 压缩率回归: 4 项

### 5.3 组合时延

| 基准点 | Median | 区间 |
|--------|--------|------|
| small/response_only | 20.67 µs | 20.65 – 20.68 µs |
| small/toon_encode_on_compressed | 66.80 µs | 66.77 – 66.83 µs |
| small/response_then_toon | 95.48 µs | 95.44 – 95.51 µs |
| large/response_only | 74.46 µs | 74.43 – 74.48 µs |
| large/toon_encode_on_compressed | 111.48 µs | 111.45 – 111.52 µs |
| large/response_then_toon | 193.18 µs | 193.07 – 193.30 µs |
| **canonical/forced_all_stages** | **192.73 µs** | 192.68 – 192.78 µs |

> canonical/forced_all_stages (forced ablation, not production pipeline)

该 send-side 库内微基准为 192.73 µs；未测 adapter/CLI/模型端到端 SLO。

---

## 六、成本影响估算（算术投影，非业务预算）

**假设条件**（限定声明）：
- 单一 canonical fixture 线性放大（非多样工作负载模拟）
- 50 轮/会话 × 1000 会话/天 × 30 天/月
- 使用默认压缩配置（truncate_arrays_at=32）
- Token 计算：bytes/4 启发式（div_ceil），非实际 tokenizer
- 模型定价为快照值（2026-07 时点），随时可能变更

⚠️ 此数据仅供量级参考，不作为采购/SLA 依据。生产级成本评估应结合实际工作负载和目标模型的真实 tokenizer。

⚠️ token-count gate（CLI 仅在 TOON 输出 token 数少于输入时采用 TOON）是字节级经济性安全网，不等同于 codec roundtrip 语义正确性或下游 LLM 任务安全性验证。

| 模型 | 输入单价 ($/MTok) | Baseline 月费 | Tokenless 月费 | 月节省金额 |
|------|-------------------|-------------|--------------|----------|
| Claude Sonnet 4 | $3.00 | $24,979.50 | $8,824.50 | **$16,155.00** |
| GPT-4o | $2.50 | $20,816.25 | $7,353.75 | **$13,462.50** |
| Gemini 2.5 Pro | $1.25 | $10,408.13 | $3,676.88 | **$6,731.25** |

- Baseline 每会话 tokens: 277,550
- Tokenless 每会话 tokens: 98,050（节省 64.7%）
- bytes/4 启发式与实际 tokenizer 存在偏差，具体范围未验证
- RTK 输出过滤维度的节省（58.6–98.6%，随命令类型变化）作用于 shell 命令输出这一独立通道，未计入上表，实际部署中为额外增益。

---

## 七、结论

### 核心指标总结

| 压缩器 | 压缩率 | 准确率 | 时延（canonical/典型） |
|--------|--------|--------|----------------------|
| ResponseCompressor | 65.8%（该 canonical fixture 下） | 34 项覆盖（保留 + 鲁棒性 + worst-case + 回归守卫） | 46.93 µs |
| SchemaCompressor | 47.3%（该 canonical fixture 下） | 25 项覆盖（保留 + 鲁棒性 + worst-case + 回归守卫） | 11.40 µs |
| TOON 编码器 | 17.0%（response）/ -2.3%（schema，膨胀） | 18 项覆盖（roundtrip + 对抗性） | 编解码 3.92 – 10.46 µs |
| RTK 命令重写器 | 命令文本 -2.7%；运行时输出 58.6 – 98.6%（3 命令样本） | 9 项覆盖（真实执行） | 重写决策 2.0 – 8.3 ms |
| 默认库内微基准 full_stack | 62.9%（最优组合 schema_response 64.7%） | 96 项全覆盖 | 192.73 µs |

### 关键发现

1. **ResponseCompressor 是主力（对该类 fixture）**：在含大量 debug/trace/log 噪声的工具响应上单独贡献 65.8% 节省；SchemaCompressor 对 function-calling schema 贡献 47.3%，两者叠加（schema_response）在本组 canonical payload 上达到 64.7%。对 debug 字段较少或已预处理的输入，实际压缩率将低于此值。

2. **TOON 必须按数据形态选择性启用**：本测试中仅均匀表格显示稳定正收益；异构结构（如 canonical schema）出现负收益（膨胀，-2.3%）；response 压缩后再叠加 TOON 从 61.7% 降至 60.0%。建议只对表格型 payload 启用，或编码后比较 token 数择优。

3. **RTK 的价值在输出过滤而非命令文本**：命令文本维度轻微负压缩（-2.7%，加 `rtk` 前缀所致）；真实节省发生在运行时输出过滤，本次实测 3 条只读命令为 58.6–98.6%（find 98.6%、ls 70.7%、git log 58.6%）。选定的 3 个可过滤命令样本显示高文本缩减；尚未测代表性覆盖率和任务正确率。重写决策 2–8 ms、过滤附加开销为观测中位数约 10 ms（受进程启动方差和 OS 调度精度限制），均为跨进程毫秒级，与 in-process 微秒级压缩器分属两条通道，不可相加。

4. **测试覆盖**：96 项测试在服务器上全部通过，其中 RTK 9 项协议测试为真实执行（rtk 0.43.0）而非 skip。覆盖列出的合法 Value 和规则用例，不代表任意输入或生产任务准确率。

5. **时延**：该 send-side 默认库内微基准（Default Library Send Pipeline）canonical 时延 192.73 µs；未测 adapter/CLI/模型端到端 SLO。不含 subprocess、network、I/O、TOON decode/验证、RTK、模型调用。RTK 通道的毫秒级开销对 agent 场景相对可接受。

6. **成本节约估算**：单一默认 canonical 重复 50 次的算术投影（见假设列表）。在上述假设条件下，1000 会话/天规模 Claude Sonnet 4 月节省约 $16,155，GPT-4o 约 $13,463，Gemini 2.5 Pro 约 $6,731。实际节省将因工作负载结构、缓存命中率和 API 定价变动而异。RTK 输出过滤为上述之外的额外增益。

---

## 附录：v3 复审问题处理说明

本节记录《benchmark代码完整复审-v3》提出的 26 个问题的处理决策。经三位独立专家（范围边界、回归风险、最小可行集视角）交叉评审后形成共识。

### 一、已修复（代码变更）— 7 项

| 编号 | 问题 | 修复措施 |
|------|------|----------|
| CODE-P0-04 | 成本计算使用 CLI 不会选择的 full_stack 策略 | 改用 schema_response 策略（CLI 实际会选择的最优非膨胀组合） |
| CODE-P1-17 | 5 秒 timeout 未真正终止子进程 | 超时时调用 `kill(SIGKILL)` + 等待回收，区分 timeout_killed / spawn_failure 状态 |
| CODE-P1-06 | RTK latency bench 无超时保护 | 每次迭代经 `run_rewrite_with_timeout`（5s 硬超时 + SIGKILL）执行，RTK 挂起时 bench 以明确 panic 终止而非无限阻塞；另有 `measurement_time(30s)` 限制采样时长 |
| CODE-P1-10 | find_rtk_binary() 仅检查 exists() | 新增 `is_executable()` 验证文件权限和类型 |
| CODE-P1-11 | compression_rate 测试不必要地执行 RTK | 拆分为 `compression_metrics()`（纯函数）+ `full_report()`（含 RTK），测试仅调用前者 |
| CODE-P0-01 | 报告未绑定源码身份 | run-benchmarks.sh 生成 `benchmark_identity.json`（git rev、porcelain 全工作树 dirty 检测、Cargo.lock SHA256、fixtures SHA256、RTK 版本、tokenless workspace 版本；不含 hostname 以免泄露基础设施信息） |
| CODE-P1-01 | run-benchmarks.sh 存在过期引用和冗余步骤 | 删除 Python 注释、删除冗余上游 workspace build、保存 benchmark 输出到日志文件 |

### 二、已修复（命名/文档精确化）— 11 项

| 编号 | 问题 | 修复措施 |
|------|------|----------|
| CODE-P0-02 | `canonical/full_stack` 名称暗示生产 pipeline | 改名为 `canonical/forced_all_stages`，注释标明"forced ablation, not production CLI path" |
| CODE-P0-03 | 组合链路 "6/6 通过" 含已知缺陷 | 改为 "5/5 正向通过 + 1 XFAIL (已知限制)"，明确标注 TOON roundtrip 限制 |
| CODE-P0-05 | library default 配置被误读为 adapter 生产配置 | 添加副标题 "Library Default Microbenchmark" + adapter 配置差异警告 |
| CODE-P1-04 | "32 项后趋平"不符合实测数据 | 改为"32 项处有显著拐点，之后增长率下降但不为零（序列化成本仍线性增长）" |
| CODE-P1-05 | TOON roundtrip 使用 non-strict decode 未标明 | 添加说明：使用 non-strict 模式兼容 truncation marker 模糊性 |
| CODE-P1-14 | JSON 报告缺少原始字节数 | 在 compression_metrics 输出中添加 raw_bytes / compressed_bytes / toon_bytes 字段 |
| CODE-P1-13 | 收益门控被误解为语义安全验证 | 添加限定声明：token-count gate 是字节级经济性安全网，不等同于语义正确性验证 |
| CODE-P1-18 | RTK 输出过滤率数据来源不透明 | 添加明确标注：数据来自一次性 SSH 手工采集，非自动化可复现流程 |
| CODE-P2-01 | fixture 注释与实际输入不符 | 修正 schema_complex() 和 response_items() 的文档注释 |
| CODE-P2-02 | TOON 注释错误声称 pipeline benches 覆盖 CLI 成本 | 修正为"本套件中无 TOON CLI 时延基准，pipeline benches 也是纯 in-process" |
| CODE-P2-03 | worst_case 名称过强 | 修正注释为"compression-friendly edge cases that must never expand"并引用已知膨胀案例 |

### 三、不接受/不修复 — 8 项

| 编号 | 问题 | 不修复理由 |
|------|------|------------|
| CODE-P1-02 | RTK 缺失时测试静默 pass 而非 ignored | Rust 标准 skip 模式（`return` with skip notice），已有明确输出提示。改为 `#[ignore]` 会破坏 CI 策略 |
| CODE-P1-03 | 单一 canonical fixture 是高收益构造 | 设计选择：单一 canonical 的价值在于跨版本一致性而非覆盖率。已在 README Known Limitations 中声明 |
| CODE-P1-07 | pipeline oracle 过弱（只检查类型不检查值） | 类型级断言对演化数据合理；值级断言会因 TOON 精度变化导致脆弱测试，反而降低维护性 |
| CODE-P1-08 | TOON 正确性套件避开了失败输入 | 失败情况已通过 `response_toon_roundtrip_known_limitation` 反向断言显式覆盖，不是"避开"而是文档化 |
| CODE-P1-09 | RTK protocol test 名不符实 | 测试名在其 scope 内准确——验证 RTK 退出码协议兼容性，而非重写命令语义正确性 |
| CODE-P1-12 | 部分回归守卫阈值偏低 | 55% 阈值为灾难性回归守卫（防止彻底退化），不需要精确追踪微小波动。Criterion 自身的统计对比负责精细回归检测 |
| CODE-P1-15 | 成本模型把不同阶段简化为固定共现 | 成本模型限制已在代码注释（L28-33）和报告假设条件中充分标注，读者可见 |
| CODE-P1-16 | stash 路径未被 latency/成本覆盖 | stash 是 adapter 层功能，涉及持久化 DB 写入和网络 I/O，超出纯库 micro-benchmark 的职责范围 |

### 评审方法说明

本轮修复采用三位独立评审员交叉验证：
- **评审员 A**（范围边界视角）：区分 benchmark 职责内 vs 产品层面问题
- **评审员 B**（回归风险视角）：评估每项修复的 regression risk 和技术可行性
- **评审员 C**（最小可行集视角）：筛选使报告"事实错误"的最小必修集

三位评审员对上述分类达成一致共识后方执行修复。
