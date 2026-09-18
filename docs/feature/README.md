# 特性档案

本目录归档 Mimi 主要特性的设计与 Git 演进，回答“为什么做、最终形成什么结构”。
目录使用 `YYYYMMDD-<slug>`，日期取特性首次实现提交日；交付后保持冻结，当前行为以代码、
插件 README 和 [`ARCHITECTURE.md`](../../ARCHITECTURE.md) 为准。
每篇档案从代码入口开始，完整说明分发、服务、模型、持久化、外部调用、输出和失败路径。

| 特性 | 首次提交 | 关键提交 | 状态 |
| --- | --- | --- | --- |
| [插件运行时](20260408-plugin-runtime/design.md) | `15c71fc` | `15c71fc`、`29f7d07`、`9b90be6` | shipped |
| [群消息归档](20260418-message-archive/design.md) | `9b57824` | `9b57824`、`fa072ef` | shipped |
| [APEX 数据与海报](20260804-apex-plugin/design.md) | `5f31918` | `5f31918`、`6a48909`、`0b061b2`、`2cb7cd3` | shipped |
| [网易云共享会话](20260818-music-session/design.md) | `31f3563` | `31f3563`、`95f9b5c`、`00850ea`、`9b9b27b` | shipped |
| [LOL 与命令目录](20260918-lol-plugin/design.md) | `c69bac9` | `c69bac9` | shipped |

早期 Music 与 Niu 的接入属于插件运行时演进，在第一篇档案中说明，不额外拆文档。
