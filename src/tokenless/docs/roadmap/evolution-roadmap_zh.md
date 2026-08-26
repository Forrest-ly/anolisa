# Tokenless 演进路线

[English](evolution-roadmap.md)

Tokenless 统一压缩管线演进的规范参考。各 crate 中引用的章节编号
（`§4.1`–`§6`）、设计原则与里程碑标记（`M1`、`M4`）均指向本文档。
本文档汇总了已落地代码与已合并实现 PR 中所编码的演进路线，状态以
Tokenless 0.7.13 为准。

## 目标

一条共享的 Rust 压缩管线，同时服务 CLI hook、进程内 Runtime 与各框架
适配器。以单一版本化兼容边界（protocol v1）取代各适配器私有的载荷格式：
适配器只把模型可见内容拷贝进 `CompressionRequest`，取回最终内容与构建
宿主信封所需的决策事实。必须保持原样的 UI 或业务对象永不进入协议。

压缩始终是可选优化：管线中的任何失败都不允许使请求失败，任何未应用的
结果都原样输出原始内容。

## 设计原则

原则编号与已发布代码中的引用保持一致；未被已发布代码引用的编号在此
不做复述。

- **原则 2 —— 按内容路由，按接缝约束。** 一个压缩器只有在支持检测出的
  内容类型、运行于请求的接缝、且适配器声明了其所需的全部能力时才是
  候选者。重塑响应形态的压缩器永远不会到达无法替换模型可见输出的宿主；
  可恢复有损压缩器永远不会到达没有检索工具的宿主。
- **原则 3 —— 分阶段升级。** 阶梯依次为无损、可恢复有损、有界截断。
  仅在配置的尺寸策略未满足时才升级，每个有损阶段至多运行一个压缩器；
  专项有损决策一经做出即为最终决策。
- **原则 5 —— 显式可逆性声明。** 每个应用的变换都报告其恢复状态——
  `lossless`、`retrievable` 或 `unrecoverable`；require-reversible 模式
  直接拒绝不可恢复的候选者。
- **原则 6 —— 失败开放，诊断有界。** 压缩器失败绝不使请求失败；首个
  失败被保留为以 `DIAGNOSTIC_MAX_BYTES`（4 KiB）为界的诊断，`output`
  始终恰好携带适配器应输出的内容。
- **原则 7 —— 静态注册。** 注册表是编译期组装的 `const` 切片；动态插件
  与配置驱动加载不在范围内。条目与其实现者一同入库，绝不做投机性注册。

## 架构（§4）

### §4.1 Protocol v1 边界

`tokenless-protocol` 定义兼容边界：带 `protocol_version` 的
`CompressionRequest` / `CompressionResponse`，以及 `Seam`、
`Capabilities`、`Disposition`、`Reversibility` 类型。兼容规则：

- 在受支持的主版本内，读取方忽略未知字段，因此可选字段可以不加版本
  号地新增。
- 不兼容的形态必须启用新的 `protocol_version`，绝不允许并行的适配器
  私有载荷。
- `from_json` 先校验版本再解析形态，未来版本会报告
  `UnsupportedVersion` 而非误导性的形态错误。

### §4.2 内容检测与注册表路由

`tokenless-pipeline` 承载内容分类（`json_records`、`search_results`、
`build_log`、`stack_trace`、`diff`、`html`、`tabular`、`source_code`、
`plain_text`、`unknown`）、确定性的有界成本检测器，以及带原则 2 路由
过滤的编译期注册表。

检测是内容的纯函数，至多检查 64 KiB / 200 行，绝不完整解析任何格式；
昂贵解析留在被选中的压缩器内部。检查从最具特征的形态到最一般的形态
依次进行，且检测天生保守：HTML 仅识别以其自身起始的完整文档，源代码
需要 shebang 或多个声明关键字行，类二进制输入归为 `unknown`（里程碑
M4 策略：歧义片段不做分类）。

### §4.3 分阶段执行与端到端裁决

`tokenless_pipeline::run` 让请求经过检测、路由与升级阶梯，然后对原始
内容与最终候选者做一次性比较。未移除归一化 token、违反必需可逆性、
或超出整体超时预算的候选者被整体拒绝；其新建的 Stash 写入按
`(key, generation)` 回滚，并原样输出原始内容。

### §4.5 适配器边界

适配器拥有各自的宿主私有契约。只有模型可见值被拷贝进请求；响应携带
最终内容与适配器构建宿主信封所需的决策事实（`disposition`、token
计数、`compressor_chain`、`stash_keys`、有界的 `diagnostic`）。适配器
无需本地兜底逻辑：`output` 永远可直接输出。

### §4.6 接缝

Agent 循环中的四个拦截点：`before_model`（如 schema 发布）、
`pre_tool`（如命令重写）、`post_tool`（主压缩接缝）、`proxy`（观察
模型流量的前端）。响应中只出现已提交且已应用结果的 Stash key；被
回滚的候选者绝不泄漏 key。

## 决策与契约（§5）

### §5.1 Token 计数器决策

Protocol v1 的所有 token 计数使用字符类启发式 `heuristic-v1`
（CJK ≈ 每字符 1 token，其他 ≈ 每 4 字符 1 token），由
`tokenless-stats` 统一实现——不使用 provider 的 tokenizer。计数是用于
裁决与归因的归一化 token，而非计费估算。估算器的字符类别或比率一旦
变化就必须启用新的计数器 ID，不同 ID 产出的记录在没有显式按计数器
拆分的情况下永不合并为同一序列。

### §5.2 路由契约

未知或歧义内容路由到 passthrough。检测只把记录形态的 JSON
（`{...}` / `[...]`）路由给 JSON 清理；标量根原样通过。误分类在设计
上退化为失败开放的 passthrough 路径。

### §5.3 Response 清理接入管线

既有的 JSON response 清理注册为 `RESPONSE_CLEANUP`（内容
`json_records`、接缝 `post_tool`、阶段为可恢复有损、成本 moderate、
要求 `replace_output`），CLI `compress-response` 命令、
`TokenlessRuntime::compress_response` 与 Python 绑定背后的共享路径
改经 `tokenless_pipeline::run`。一个整体超时预算（进程内 10 秒）守护
检测与所有阶段；超时时返回原始内容并回滚 Stash 写入。可逆性按实际
发生的情况声明：无截断 → 无损，全部截断已入 Stash → 可恢复，其余 →
不可恢复。

### §5.4 单一外部 hook 入口

此前分散在公共 Python hook 与 CLI 子命令中的四项决策——JSON 检测、
工具阈值选择、TOON 选择、最终尺寸接受——收敛到一个共享接缝路由器：

- protocol v1 的 `tokenless compress` 子命令（stdin
  `CompressionRequest` → stdout `CompressionResponse`）与进程内
  `TokenlessRuntime::compress`，二者经由同一入口路由；
- 外部 hook 变为仅处理信封的适配器：构建一个请求、至多启动一个
  `tokenless` 子进程、把响应翻译为宿主信封；
- 适配器契约夹具按已迁移的 agent 覆盖五类行为（passthrough /
  replacement / no-savings / timeout / malformed）；
- 新路由行为由运行时配置开关控制，默认关闭，随接线变更一并引入。

状态：评审中（PR #2844），首先迁移公共 Python hook
（`compress_response_hook.py`、`compress_schema_hook.py`）；codex /
hermes / openclaw / dsh / SDK 适配器在迁移前保持现有路径。

### §5.5 统计迁移

归因列进入统计 schema，遗留的 dry-run 测量通道
（`CompressResult.compressed_output`，记录预测候选文本）改用实测数值
并移除。归因随请求进入统计，不再单独上报。

### §5.6 共享词汇与平价

CLI、Runtime 与语言绑定共享同一套 disposition 名称与线格式字符串
（protocol 的 `Disposition` 枚举），所有计数经由同一个
`heuristic-v1` 估算器，保证一切裁决使用相同数字。里程碑 M1 出口门槛
要求 CLI 与 Runtime 在该词汇上达成一致；行为平价按五类行为对每个已
迁移的 agent 做断言。

## §6 压缩器包

新的内容专项压缩器通过为注册表条目实现 `Compressor` trait 加入管线。
规划方向包括面向 schema 接缝的 `SchemaCompressor`。按原则 7，条目与其
实现者一同入库，绝不做投机性注册。

## 里程碑标记

- **M1** —— 出口门槛：CLI 与 Runtime 就共享 disposition 词汇达成
  一致（§5.6）。在 response 压缩接入注册表、Runtime 的 pre-protocol
  disposition 枚举退役时达成。
- **M4** —— 保守检测策略：歧义片段不做分类（§4.2）。已编码于已发布
  的检测器中。

## 实现状态

| 章节 | 交付物 | 状态 | 参考 |
|---------|-------------|--------|-----------|
| §4.1 | `tokenless-protocol` v1 类型与线格式契约 | 已随 0.7.13 发布 | PR #2783 |
| §4.2 | 内容分类、检测器、静态注册表 | 已随 0.7.13 发布 | PR #2788 |
| §4.3 | 分阶段执行与端到端裁决 | 已随 0.7.13 发布 | PR #2799 |
| §5.3 | Response 清理改经管线路由 | 已随 0.7.13 发布 | PR #2816 |
| §5.4 | 统一外部 hook 入口、契约夹具、运行时开关 | 评审中 | PR #2844 |
| §5.5 | 统计归因迁移 | 规划中 | 紧随 §5.4 |
| §6 | 新压缩器包（含 schema 接缝） | 规划中 | — |

遗留的 `compress-response` / `compress-schema` / `compress-toon`
子命令与 pre-pipeline Python helper 保留到所有使用方迁移到统一入口
为止；其移除是之后的专项步骤。
