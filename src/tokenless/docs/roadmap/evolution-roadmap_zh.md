# Tokenless 演进路线 —— 统一压缩管线

[English](evolution-roadmap.md)

> **状态：长期维护文档。** 本路线汇总了 tokenless 各 crate 已按章节号
> （§4.x / §5.x）引用的演进计划。已有实现之处，以代码为准；
> `crates/tokenless-protocol/src/tests/protocol_tests.rs` 中的协议契约测试
> 是本文档与线上类型之间的防漂移护栏。

## 1. 为什么要统一管线

Tokenless 在模型可见内容（工具结果、发布的工具 schema）到达模型之前对其进行
压缩，让 agent 把 token 花在任务内容而非样板信息上。在本路线之前，每个集成点
各自实现压缩决策：

- Python hooks、CLI 子命令（`compress-response`、`compress-schema`、
  `compress-toon`）与进程内 runtime 各自重复实现 JSON 检测、阈值选择与
  最终大小验收；
- 无法替换模型可见输出、或无法取回暂存（stash）内容的宿主，没有统一的方式
  声明这一事实；
- 各入口之间的处置结果（disposition）与统计不可比较。

目标是单一的压缩管线：

1. 适配器与管线之间唯一的**协议边界**（§4.1）；
2. 唯一的**内容分类**、确定性的**检测器**与静态的**压缩器注册表**（§4.2）；
3. 唯一的**分阶段执行引擎**，配合端到端仲裁（§4.3）；
4. 所有适配器共享的**唯一外部入口**（`tokenless compress` 子命令与进程内
   `TokenlessRuntime::compress`）（§5.4）；
5. CLI、runtime 与语言绑定之间可比较的**处置与统计**（§5.5–§5.6）。

适配器收缩为信封转换：构造一个协议请求，经由共享入口路由，并原样输出返回的
`output` —— 不需要自己的兜底逻辑。

## 2. 原则

实现按编号引用以下原则。已编码的原则列出其落点代码。

| # | 原则 | 落点 |
|---|------|------|
| 2 | **按内容路由，按缝（seam）约束。** 压缩器成为候选，当且仅当它支持检测出的内容类型、运行于请求的缝、且适配器声明了它要求的全部能力。 | `CompressorSpec::matches` / `candidates`（tokenless-pipeline） |
| 3 | **分阶段升级。** 无损先于可取回有损，可取回有损先于有界截断；仅在配置的大小策略未满足时才升级。 | `Stage` 顺序 + `run`（tokenless-pipeline） |
| 5 | **显式可逆性。** 每个应用结果声明其恢复状态——无损、可取回或不可恢复；required-reversible 模式直接拒绝不可恢复的候选。 | `Reversibility` + 端到端仲裁 |
| 6 | **失败开放，诊断有界。** 压缩是可选优化：失败步骤永不使请求失败，首个失败保留为不超过 4096 字节的诊断，`output` 始终恰好是适配器应输出的内容。 | `Disposition::Error`、`DIAGNOSTIC_MAX_BYTES`、fail-open 契约 |
| 7 | **静态注册。** 注册表是编译期 `const` 切片；动态插件加载与配置驱动注册不在范围内。 | `REGISTRY`（tokenless-pipeline） |

原则 1 与 4 属于草案编号，但尚无实现引用将其固化；在编码它们的步骤落地之前，
此处刻意不作陈述。

## 3. 目标架构（§4）

### §4.1 协议 v1 —— 兼容性边界

`tokenless-protocol` 定义协议 v1：agent 专用适配器与共享压缩管线之间的边界。
它刻意不采用 OpenAI 或 Anthropic 的请求形状。

- `CompressionRequest` 只携带模型可见内容加管线所需的归因与能力事实：
  `protocol_version`、`content`、`agent_id`、可选 `session_id` /
  `tool_use_id` / `tool_name`、`seam` 与 `capabilities`。
- `CompressionResponse` 携带最终内容加适配器构造宿主信封所需的决策：
  `output`、`disposition`、可选 `content_type`、`compressor_chain`、
  `reversibility`、`before_tokens` / `after_tokens`、`stash_keys`、
  `tokenizer_id` 与可选的有界 `diagnostic`。

线上示例（请求）：

```json
{
  "protocol_version": 1,
  "content": "...",
  "agent_id": "claude-code",
  "session_id": "...",
  "tool_use_id": "...",
  "tool_name": "Bash",
  "seam": "post_tool",
  "capabilities": {
    "replace_output": true,
    "publish_retrieve_tool": true
  }
}
```

线上示例（响应，压缩器 ID 为示意）：

```json
{
  "protocol_version": 1,
  "output": "...",
  "disposition": "applied",
  "content_type": "build_log",
  "compressor_chain": ["terminal-cleanup", "build-log"],
  "reversibility": "retrievable",
  "before_tokens": 1200,
  "after_tokens": 340,
  "stash_keys": ["0123456789abcdef01234567"]
}
```

兼容性规则：

- 在受支持的主协议版本内，读取方忽略未知字段，因此可选字段可在不升版本号的
  情况下添加；
- 不兼容的形状必须使用新的 `protocol_version`，绝不允许并行的适配器专用负载；
- 先检查版本再完整解析，未来版本报告为 `UnsupportedVersion` 而非形状错误；
- **失败开放（fail-open）契约**：`output` 始终恰好是适配器应输出的内容。任何
  非 `applied` 处置下它就是原始模型可见内容，适配器无需自带兜底逻辑。

处置词汇表由所有前端共享：`applied`、`dry_run`、`passthrough`、
`no_savings`、`reversibility_unavailable`、`timeout`、`error`。

### §4.2 内容分类、检测器与注册表

检测是内容的纯函数、确定性且有界：只检查前 64 KiB / 200 行（JSON 括号嗅探
另含一段有界尾部），且不完整解析任何格式——昂贵的解析属于被选中的压缩器，
而不是每个输入都要运行的检测器。当没有廉价信号足以定夺时，检测器倾向更通用
的类别，最终为 `plain_text` 或 `unknown`；在保守兜底压缩器出现之前，管线把
它们路由为 passthrough。

第一版分类（稳定线上值）：`json_records`、`search_results`、`build_log`、
`stack_trace`、`diff`、`html`、`tabular`、`source_code`、`plain_text`、
`unknown`。检查从最特殊的形状到最通用的形状依次进行：仅仅*包含* traceback 的
构建日志仍是 `build_log`，而*以* traceback *开头*的内容是 `stack_trace`。
歧义片段不归类为 HTML（**M4 策略**）。

按检测内容路由是契约：记录形状的 JSON（`{...}`/`[...]`）路由到 JSON 清理；
标量 JSON 根原样通过，等待它自己的压缩器，而不是被不理解它的压缩器截断。

压缩器注册表是静态的（原则 7）：编译期 `const` 切片，元素为
`CompressorSpec { id, content_types, seams, required_capabilities, stage,
cost_class }`。条目与其实现者一同追加，绝不投机性注册。`candidates` 按
原则 2 过滤注册表——内容类型 ∩ 缝 ∩ 已声明能力。

### §4.3 分阶段执行与端到端仲裁

`tokenless_pipeline::run` 把一个请求带过检测、路由与升级阶梯（原则 3）：

1. **步骤 1–2**——所有适用的无损变换全部执行；未减少归一化 token 的结果
   直接丢弃；
2. **步骤 3–4**——仅在配置的大小策略未满足时升级，每阶段一个压缩器：至多
   一个内容专用的可取回有损压缩器，然后至多一次有界截断。专用有损决策是
   终局——其后不再运行第二个有损压缩器或通用有损兜底；只有有界截断阶段
   （步骤 4，阶梯的最后手段）仍可跟随。标记被后续阶段从输出中切掉的暂存写入
   会回滚而非提交；
3. **步骤 5–6**——仲裁是端到端的：原始内容与最终候选只比较一次。未减少
   归一化 token、违反必需可逆性或超出总超时预算的候选被整体拒绝——新建的
   暂存键被回滚，输出原始内容不变。

单个 `PipelineConfig` 携带一次调用的仲裁策略：总 `timeout` 预算（在阶段边界
检查）、`max_tokens`（大小策略；`None` 表示永不越过无损阶段升级）、
`require_reversibility` 与 `dry_run`。

### §4.5 适配器保有私有的宿主契约

适配器保留其宿主专用信封与业务对象；只有模型可见值被复制进协议请求。必须
保持不变的 UI 或业务对象绝不进入协议。

### §4.6 缝（Seam）

`seam` 字段记录内容在 agent 循环中被拦截的位置：`before_model`（如 schema
发布）、`pre_tool`（如命令改写）、`post_tool`（工具执行后的输出——主要压缩
缝）、`proxy`（观察模型流量的代理前端）。暂存键只在应用且已输出的结果中
报告：被回滚的候选绝不泄漏键。

## 4. 迁移计划（§5）

### §5.1 Token 计数器身份

协议 v1 的所有 token 计数使用同一计数器身份：`heuristic-v1`，即
`tokenless-stats` 实现的字符类启发式（CJK ≈ 1 token/字符，其他内容 ≈
1 token/4 字符）——而非提供方分词器。计数是用于仲裁与归因的归一化 token，
不是计费估算。估算器的字符类或比例发生任何变更都需要新的 `tokenizer_id`；
不同 ID 产出的行与响应，若没有显式的按计数器拆分，绝不合并进同一序列。
缺失该字段的负载按 `heuristic-v1` 读取：启发式估算器是该字段存在之前唯一
发布过的计数器。

### §5.2–§5.3 共享管线 crate 与第一个压缩器

`tokenless-pipeline` 承载位于协议边界与压缩器之间的部分：分类与检测器
（§4.2）、注册表与能力过滤（§4.2）、以及带端到端仲裁的分阶段执行引擎
（§4.3）。

第一个生产压缩器是既有的 JSON 响应清理（`response-cleanup`），被移到管线的
`Compressor` trait 之后：对记录形状的 JSON 做结构清理与有界截断，截断内容在
挂载了暂存库时写入暂存并以标记引用。其可执行实现位于 runtime——runtime 拥有
暂存库与按调用配置；其 `spec()` 返回注册表条目的引用，使两者不会漂移。
Runtime 遵守单预算契约：单一的总体管线预算（进程内 10 秒）取代按步骤的超时，
超时即失败开放。

后续压缩器以同样方式加入注册表——与实现一同追加，绝不投机性注册。

### §5.4 唯一外部入口

所有外部钩子汇聚到单一入口：协议 v1 的 `tokenless compress` 子命令（stdin
`CompressionRequest` → stdout `CompressionResponse`）与进程内
`TokenlessRuntime::compress`，二者都经由 runtime 中共享的缝路由器。此前在
公共 Python hooks 与 CLI 子命令间重复的决策——JSON 检测（含字符串解包语义）、
工具阈值选择、TOON 选择与最终大小验收——在 Rust 中恰好发生一次。

Python hooks 变为仅做信封转换的适配器：各自构造一个请求、至多启动一个
tokenless 子进程，并把响应翻译成宿主的信封。Matchers、hooks 配置与扩展
清单保持不变。

### §5.5 统计迁移

统计跟随赢家：每次调用至多一行，以获胜操作为键（如 TOON 胜 →
`compress-toon`、清理胜 → `compress-response`、schema → `compress-schema`），
节省率以原始输入到最终输出重新计算基线。遗留的测量旁路（runtime 单独的
`compressed_output` 候选字段与协议前处置类型）随本次迁移退役。

### §5.6 处置对等与适配器契约夹具

CLI 与 runtime 必须共用一套处置词汇表——协议 v1 的共享 `Disposition` 枚举
（**M1 出口门**）——以及同一个标准 passthrough 形状，使处置在各入口之间可比。
适配器行为由契约夹具固化，覆盖五类——passthrough、replacement、no-savings、
timeout、malformed——针对 mock 协议二进制在每个已迁移 agent 上执行，另有黄金
对等套件：用共享语料回放改写后的 hooks，与改写前 hooks 捕获的信封比对。

### 遗留移除

遗留的 `compress-response` / `compress-schema` / `compress-toon` 子命令与旧
Python helpers 保留到移除步骤，直到所有适配器都汇聚到统一入口。

## 5. 实现状态

| 步骤 | 内容 | 章节 | 状态 |
|------|------|------|------|
| 协议 v1 请求/响应类型 | `tokenless-protocol` crate | §4.1 | 已合并（[#2783](https://github.com/alibaba/anolisa/pull/2783)） |
| 内容分类、检测器、静态注册表 | `tokenless-pipeline` crate | §4.2 | 已合并（[#2788](https://github.com/alibaba/anolisa/pull/2788)） |
| 分阶段管线与端到端仲裁 | `tokenless-pipeline` crate | §4.3 | 已合并（[#2799](https://github.com/alibaba/anolisa/pull/2799)） |
| 响应压缩置于注册表之后 | `tokenless-runtime` | §5.3 | 已合并（[#2816](https://github.com/alibaba/anolisa/pull/2816)） |
| 统一外部钩子入口 + 契约夹具 | runtime 入口路由器、信封化 hooks | §5.4 / §5.6 | 开放（[#2844](https://github.com/alibaba/anolisa/pull/2844)，序列 PR 6） |
| 统计归因重构 | 每次调用一行、以赢家为键 | §5.5 | 计划中（路线 PR 7） |
| 遗留子命令/助手移除 | 退役 `compress-response` / `compress-schema` / `compress-toon` | — | 计划中（路线 PR 18） |

## 6. 相关文档

- [Response 压缩规则](../response-compression.md)——`response-cleanup`
  压缩器背后的七条清理规则
- [暂存与可逆压缩](../stash-reversible-compression.md)——暂存库与标记取回
  契约
- [Runtime 库](../design/runtime-library.md)——前端共享的有状态 runtime API
