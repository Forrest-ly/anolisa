# OS 技能库

OS Skills 是面向 AI Agent 的系统管理与 DevOps 技能库。它提供预构建的技能，使 Agent 能够执行常见的系统管理和自动化任务。

---

## 概述

OS Skills 的技能分为六大类：

- **AI 工具** — 安装与配置 Agent 工具链（Claude Code、OpenClaw、QwenPaw、Hermes、Tokenless、MCP）
- **系统管理** — Alibaba Cloud Linux 系统管理、磁盘扩容、内核升级与调优、备份恢复、Shell 与正则编写
- **开发运维** — GitHub 工作流、内核研发、SysOM 诊断、AgentSight token 与审计查询
- **阿里云** — 通过阿里云 CLI 管理 ECS 实例生命周期
- **安全** — Alibaba Cloud Linux CVE 漏洞查询
- **其他** — 产品指南、技能管理、文档与图像工具

---

## 安装

```bash
anolisa install os-skills
```

---

## 快速开始

安装后，OS Skills 可供任何 ANOLISA 兼容的 Agent 运行时使用。Agent 可通过自然语言调用技能：

```
> "查询某个 CVE 是否影响我的 Alibaba Cloud Linux 实例"
> "云盘扩容后扩展 XFS 文件系统"
> "安装并配置 Claude Code，使用自定义 API 端点"
```

---

## 技能分类

技能按 `src/os-skills/` 下的分类目录组织。表中名称为技能安装后的目录名，技能的权威定义位于对应目录的 `SKILL.md`。

### AI 工具（`ai/`）

| 技能 | 说明 |
|------|------|
| `install-claude-code` | 在 RPM 系 Linux 上安装配置 Claude Code，提供多种回退安装方式与 DashScope/Qwen API 端点配置模板 |
| `install-hermes` | 安装 Hermes Agent，并配置其 LLM 提供商、消息平台与 Web 工作台 |
| `install-openclaw` | 以非交互方式安装 OpenClaw，配置阿里云百炼凭据与计费方案 |
| `install-qwenpaw` | 在 Alibaba Cloud Linux 4 上部署 QwenPaw AI 助理，包含百炼 API Key 与钉钉频道配置 |
| `install-tokenless` | 安装 Tokenless（LLM token 优化工具），并为受支持的 Agent 框架启用 |
| `qwenpaw-usage` | QwenPaw 命令行操作：心跳任务、定时任务、Docker 打包部署、工作区导出迁移 |
| `setup-mcp` | 在 cosh 中添加与配置 MCP 服务器，自动把粘贴的 JSON 转换为合法配置 |

### 系统管理（`system-admin/`）

| 技能 | 说明 |
|------|------|
| `alinux-admin` | Alibaba Cloud Linux 4 系统管理：Shell、systemd 单元、SSH、firewalld、NetworkManager、权限、进程与磁盘 |
| `backup-restore` | 覆盖工作区、用户数据、操作系统与阿里云快照四个层面的备份与恢复 |
| `ktuner` | 内核参数调优：分析系统配置、输出调优建议、按需一键应用并支持自动回滚 |
| `regex-mastery` | 正则表达式编写、调试与性能优化 |
| `shell-scripting` | 可移植的 bash/zsh 脚本编写：参数解析、错误处理与 CLI 自动化 |
| `storage-resize` | 云盘扩容后完成分区调整与 XFS/EXT4/Btrfs 文件系统扩容 |
| `upgrade-alinux-kernel` | 升级 Alibaba Cloud Linux 实例的 Linux 内核 |

### 开发运维（`devops/`）

| 技能 | 说明 |
|------|------|
| `github` | 通过 `gh` CLI 完成 GitHub 工作流：issue、pull request、CI 运行与 API 查询 |
| `kernel-dev` | Alibaba Cloud Linux 内核研发自动化：SRPM 与 Upstream 双方式编译、module 开发环境搭建、依赖安装（x86_64 与 aarch64） |
| `sysom-agentsight` | 查询 AgentSight 的 token 消耗、token 节省、审计事件与会话中断事件 |
| `sysom-diagnosis` | SysOM 系统诊断，覆盖 CPU、内存、磁盘 IO 与网络问题；输出诊断建议但不自动修复 |

### 阿里云（`aliyun/`）

| 技能 | 说明 |
|------|------|
| `aliyun-ecs` | 通过阿里云 CLI 管理 ECS：实例、磁盘、快照、镜像、安全组、密钥对与弹性网卡 |

### 安全（`security/`）

| 技能 | 说明 |
|------|------|
| `alinux-cve-query` | 查询 Alibaba Cloud Linux 的 CVE 信息：是否受影响、是否已修复及对应安全公告 |

### 其他（`others/`）

| 技能 | 说明 |
|------|------|
| `anolisa-guide` | 解答 ANOLISA（Alibaba Cloud Linux 4 Agentic Edition）及其组件问题：使用方式、配置、命令、额度与计费 |
| `anolisa-register` | 管理 Agentic OS 注册状态：查询状态、注册加入共建计划、注销退出、查看隐私策略 |
| `clawhub-skill-mng` | 通过 ClawHub CLI 搜索、安装、卸载、升级与列出 Agent 技能 |
| `cosh-guide` | Copilot Shell（cosh）CLI 用户指南：使用方法、命令参考、配置方式与功能特性 |
| `humanizer` | 去除文本中的 AI 写作痕迹 |
| `image-gen` | 通过 DashScope/Qwen 根据文本提示生成图像 |
| `pdf-reader` | 从 PDF 文件中提取文本 |
| `xlsx` | 打开、创建、读取、分析、编辑与校验电子表格文件（`.xlsx`、`.xlsm`、`.csv`、`.tsv`） |

---

## 与 Agent 运行时集成

OS Skills 与 cosh 及其他 ANOLISA 兼容运行时自动集成。技能在启动时被发现并加入 Agent 的工具清单。

```bash
# 验证技能已加载
anolisa status os-skills
```

技能以普通目录形式安装到运行时的技能路径下 —— system profile 为 `/usr/share/anolisa/skills/`，user profile 为 `~/.copilot-shell/skills/` —— 并完全由各自的 `SKILL.md` 定义。os-skills 不读取任何配置文件：`anolisa install os-skills` 安装技能集，`anolisa uninstall os-skills` 将其移除。

---

## 参见

- [Copilot Shell](copilot-shell/QUICKSTART.md)
- [anolisa CLI](anolisa-cli.md)
