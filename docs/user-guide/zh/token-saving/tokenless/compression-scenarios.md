# 压缩率与适用场景

[English](../../../en/token-saving/tokenless/compression-scenarios.md)

Tokenless 报告的压缩率是 Payload 级指标。本页说明各策略在不同场景下的预期压缩率与影响因素，并提供一组标准测试负载，便于你在自己的环境中验证压缩行为。

## 如何计算压缩率

- 压缩率 =（压缩前 − 压缩后）÷ 压缩前，大小以 UTF-8 字节数计算。
- Token 数使用 `ceil(字节数 ÷ 4)` 近似估算，不调用模型 Tokenizer。
- 无收益的操作不入库：压缩后估算 Token 数没有下降时，CLI 输出原文，不产生统计记录。
- `stats summary` 与面板中的聚合压缩率只覆盖经过 Tokenless 的 Payload，不等于会话总体节省率，换算方法见[正确解释节省率](measuring-savings.md#正确解释节省率)。

## 各策略的适用场景与参考压缩率

| 策略 | 适用场景 | 参考压缩率 | 主要影响因素 |
|------|----------|------------|--------------|
| Schema 压缩 | Function Calling 工具定义多、工具或参数描述冗长、带示例 | ~57% | 描述长度、examples/title 多少、参数数量 |
| 响应压缩 | 结构化工具/API JSON 响应：重复记录数组、null/空值、debug 字段、长字符串 | ~26%–78% | 结构冗余度、数组长度、字符串长度、截断阈值 |
| TOON 编码 | 字段统一、记录重复的表格型 JSON | 15%–40% | 记录同质性、字段数量 |
| 命令重写（RTK） | 构建、测试、包管理等高噪声命令输出 | 60%–90% | 命令类型、输出中噪声占比 |

参考压缩率是常见负载下的典型值，不是承诺值；实际高低由内容本身决定，可用下文的标准测试负载在本地验证。

### 压缩率较高的典型场景

- **重复结构化响应**：列表接口、搜索结果、批量状态查询。数组越长、记录越同质，压缩率越高；数组超过截断阈值时尾部进入 Stash，可按标记取回，端到端无损。
- **冗余字段多的 API 响应**：包含 `debug`、`trace`、`logs` 等默认黑名单字段、`null` 值、空字符串/数组/对象的响应，这些内容会被直接移除。
- **工具定义多的会话**：Agent 注册几十上百个工具时，Schema 压缩移除定义中的示例、标题和超长描述。
- **命令行输出**：构建工具、测试框架、包管理器的高噪声输出经 RTK 过滤。RTK 是独立二进制，作用于命令输出而非 JSON Payload。

### 压缩率偏低或不适用的场景

| 场景 | 原因 | 预期表现 |
|------|------|----------|
| 短响应、结构紧凑 | 压缩无 Token 收益 | 输出原文、不记录统计（预期行为） |
| 自然语言长文（文档检索、网页正文） | 可移除的结构冗余少 | 低个位数到一成左右 |
| 源码为主的响应 | 代码自身冗余低 | 一成到两成左右，取决于结构 |
| 高熵内容：base64/二进制、已压缩或加密数据、随机字符串 | 无冗余可移除 | 几乎无收益 |
| 已被上游精简的输出（已过滤字段、已分页截断） | 冗余已提前移除 | 收益取决于剩余内容 |
| 模型推理输出、system prompt、对话历史 | 不在 Tokenless 处理范围 | 不涉及 |

不同 Adapter 使用不同的截断阈值（共享 Shell 策略 `65536`/`128`/`8`，其他结构化工具策略 `1048576`/`65536`/`32`，详见 [Adapter 处理规则](framework-integration.md#adapter-处理规则)），因此同一内容在独立 CLI 与 Agent 内的实测压缩率可能不同。

## 用标准测试负载验证

仓库提供一组确定性标准负载，位于 [`src/tokenless/benchmark/standard-payload/`](https://github.com/alibaba/anolisa/tree/main/src/tokenless/benchmark/standard-payload)，覆盖从高到低的典型场景：

| 负载 | 场景 | 对应命令 |
|------|------|----------|
| `schema_tools.json` | 描述冗长的 Function Calling Schema 数组 | `tokenless compress-schema --batch` |
| `response_api_records.json` | 结构化 API 响应（48 条重复记录，含 debug/trace/logs 字段） | `tokenless compress-response`、`tokenless compress-toon` |
| `response_code.json` | 代码搜索结果（内容为源码） | `tokenless compress-response` |
| `response_prose.json` | 文档搜索结果（内容为自然语言长文） | `tokenless compress-response` |

负载内容全部为构造的合成数据，不含真实用户数据。

### 运行

克隆仓库并运行配套检查脚本（需要已安装 tokenless）：

```bash
git clone https://github.com/alibaba/anolisa.git
cd anolisa/src/tokenless/benchmark/standard-payload
./run-standard-check.sh
```

也可以只下载单个负载手动运行：

```bash
curl -fsSL -O https://raw.githubusercontent.com/alibaba/anolisa/main/src/tokenless/benchmark/standard-payload/response_api_records.json
tokenless compress-response -f response_api_records.json \
  --session-id stdpay-api
tokenless stats summary --json
```

### 参考结果

以下数值在 tokenless 0.7.6、默认截断阈值下实测，环境为 Linux x86_64。字符与 Token 指标都是基于内容的度量，与平台无关，在其他受支持平台上应可复现。

| 用例 | 输入（字节） | 输出（字节） | 字符节省 | 估算 Token 节省 |
|------|--------------|--------------|----------|------------------|
| Schema 压缩（`schema_tools.json`） | 10,060 | 4,976 | ~50.5% | ~50.7% |
| 响应压缩 · 结构化（`response_api_records.json`） | 37,018 | 15,579 | ~57.9% | ~57.9% |
| 响应压缩 · 代码（`response_code.json`） | 5,991 | 4,927 | ~17.8% | ~17.8% |
| 响应压缩 · 长文（`response_prose.json`） | 4,697 | 4,410 | ~6.1% | ~6.1% |
| TOON 编码（`response_api_records.json`） | 37,018 | 29,475 | ~20.4% | ~20.4% |

### 如何解读结果

- **标准负载结果与参考表差异明显**：先用 `tokenless --version` 确认版本，再确认输入文件与仓库一致（`gen_standard_payload.py` 可重新生成，输出应与仓库文件逐字节相同）。
- **真实业务负载的压缩率与参考表不同**：这是正常现象，压缩率由内容冗余度决定。对照上文两个场景表，可以判断自己的负载落在哪一档。
- **估算会话总体节省**：总体估算节省率 ≈ Payload 压缩率 × 工具响应占会话总 Token 的比例，见[正确解释节省率](measuring-savings.md#正确解释节省率)。
