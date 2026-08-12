# Tokenless AgentScope 适配器

[English](agentscope-adapter.md)

## 目标

为 [AgentScope](https://github.com/agentscope-ai/agentscope) 提供 Tokenless
适配器，使基于 AgentScope 的 agent 在不修改应用代码的情况下获得 Tokenless
压缩能力。

## AgentScope 集成点

AgentScope 暴露多个可用于实现压缩的扩展点：

| AgentScope 概念 | 拦截内容 | Tokenless 映射 |
|---|---|---|
| `Middleware` hook | Reply、reasoning、acting、model calling | 在 model 调用和 tool 执行前后运行压缩 |
| `Model` wrapper | 原始 LLM、embedding、TTS 调用 | 包装 Provider 调用，压缩请求 schema 和 tool 结果 |
| `Toolkit` / skills | Tool 定义与执行 | 压缩 tool schema 和 tool 输出 |
| Event system | 流式 reasoning、tool calls、多模态内容 | 可选的统计/诊断 |

首选集成方式是通过 **Middleware**，因为它是框架原生的 hook 层，并且与
Tokenless 在 Hermes、Codex、OpenCode 中的集成方式保持一致。

## 适配器职责

| 能力 | 执行时机 | 说明 |
|---|---|---|
| Tool schema 压缩 | Model 调用前 | 压缩传给模型的 `tools` |
| Response 压缩 | Tool 执行后 | 压缩 JSON tool 结果 |
| TOON 编码 | Response 压缩后 | 在有利时将压缩后的 JSON 编码为 TOON |
| Tool Ready | Tool 执行前 | 环境未就绪时阻止调用 |
| Command rewriting | Shell tool 执行前 | 建议 RTK 改写后的命令（若 AgentScope 不允许修改参数，则采用 block + suggest） |
| Session tracking | Session / run 启动时 | 将 ID 透传给 tokenless stats |

## 建议的适配器目录结构

```
adapters/tokenless/agentscope/
├── __init__.py              # 插件入口，注册 middleware
├── middleware.py            # AgentScope middleware 实现
├── plugin.yaml.in           # AgentScope 插件清单
├── scripts/
│   ├── detect.sh            # 检测 AgentScope 安装
│   ├── install.sh           # 安装/创建符号链接
│   └── uninstall.sh         # 移除适配器
```

Python 实现复用 `adapters/tokenless/common/hooks/hook_utils.py` 进行二进制解析，
并通过共享 hook 脚本完成实际压缩逻辑，与 Hermes 适配器做法一致。

## Middleware Hook

AgentScope middleware 可在 agent 循环的多个阶段拦截。Tokenless 适配器在对应
Tokenless 策略的阶段注册处理器：

1. `pre_model_call`（或等价 pre-model 阶段）
   - 使用 `compress_schema_hook.py` 压缩模型请求中的 `tools`。
   - 记录 agent/session 上下文。

2. `post_tool_execute` / `post_acting`
   - 对 JSON 字符串形式的 tool 结果运行 `compress_response_hook.py`。
   - 该 hook 可进一步应用 TOON 编码。

3. `pre_tool_execute` / `pre_acting`
   - 运行 `tool_ready_hook.sh` 检查环境就绪状态。
   - 对 shell tool 运行 `rewrite_hook.py` 获取 RTK 改写后的命令。
   - 若 AgentScope 允许修改参数，则直接替换命令；否则 block 调用并返回改写后的命令作为反馈。

4. `on_session_start`
   - 将 `agent_id` 和 `session_id` 写入环境变量供 stats 使用。

## Manifest 条目

`adapters/tokenless/manifest.json.in` 新增 `agentscope` target：

```json
"agentscope": {
  "compatibleVersions": ">=2.0.0",
  "capabilities": {
    "hooks": [
      "tool-ready",
      "rewrite",
      "compress-response",
      "compress-toon",
      "compress-schema"
    ]
  },
  "actions": {
    "detect":    "agentscope/scripts/detect.sh",
    "install":   "agentscope/scripts/install.sh",
    "uninstall": "agentscope/scripts/uninstall.sh"
  }
}
```

## 待决设计问题

1. **Middleware API 确切名称** — AgentScope middleware 的阶段名和签名需要对照已安装 SDK 版本确认。适配器应做版本保护，API 不兼容时 fail-open。

2. **参数修改能力** — 若 AgentScope middleware 不能修改 tool 参数，command rewriting 必须采用与 Hermes 适配器相同的 "block + suggest" 模式。

3. **Tool 结果形态** — AgentScope 可能把 tool 结果包装在自定义 message 对象中。适配器需要在压缩前提取 JSON payload，并在压缩后重新包装。

4. **安装路径** — 需要确定 AgentScope 的插件/middleware 发现路径，以便 `install.sh` 将适配器放到正确的用户目录或虚拟环境目录。

## 建议的 Phase 1 范围

- 调研 AgentScope middleware 签名和插件发现机制。
- 实现 `agentscope/middleware.py`，包含 schema 压缩、response 压缩、tool ready 的 fail-open hook。
- 新增 `agentscope/plugin.yaml.in` 及安装/卸载脚本。
- 在 `manifest.json.in` 中注册 `agentscope` target。
- 增加 smoke test，在固定 AgentScope 版本下导入 middleware。

## 与 LLM Provider 代理的关系

如果某个 AgentScope 应用无法使用 middleware（例如旧版本或受限运行时），仍可通过
Tokenless LLM Provider 代理修改模型端点来透明地获得压缩能力。
在 middleware 可用时，本适配器是首选集成方式。
