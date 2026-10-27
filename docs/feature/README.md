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
| [英雄联盟手游战绩适配](20260920-lol-mobile-game/design.md) | - | - | shipped |
| [群撤回消息查询](20261012-recalled-message-query/design.md) | - | - | implemented |
| [Pumpkin AI 虚拟人物](20261020-pumpkin-agent/design.md) | - | - | implemented |
| [Pumpkin 能力评测与自动优化](20261021-pumpkin-capability-evaluation/design.md) | - | - | draft |
| [Pumpkin Harness Agent 重构](20261022-pumpkin-harness-agent/design.md) | - | - | implemented |
| [Pumpkin 完全对齐 DeepSeek Harness](20261023-pumpkin-harness-alignment/design.md) | - | - | implemented |
| [Pumpkin 按 Agent 角色配置思考深度](20261023-pumpkin-role-reasoning-effort/design.md) | - | - | implemented |
| [Pumpkin Turn 失败恢复](20261023-pumpkin-turn-failure-recovery/design.md) | - | - | implemented |
| [Pumpkin 安静参与与有界话题扩散](20261023-pumpkin-quiet-participation/design.md) | - | - | implemented |
| [Pumpkin 任务性能与 Web 访问治理](20261023-pumpkin-task-performance/design.md) | - | - | implemented |
| [Pumpkin 任务交付与纠正门禁](20261023-pumpkin-delivery-gating/design.md) | - | - | implemented |
| [Pumpkin Markdown 图片渲染](20261024-pumpkin-markdown-render/design.md) | - | - | implemented |
| [Pumpkin GIF 首帧读取](20261026-pumpkin-gif-first-frame/design.md) | - | - | implemented |
| [Pumpkin 图片与代码产物上限](20261026-pumpkin-artifact-size-limits/design.md) | - | - | implemented |
| [Pumpkin 音色调整](20261026-pumpkin-voice-adjustment/design.md) | - | - | implemented |
| [Pumpkin Qwen 本地语音 Provider](20261027-pumpkin-qwen-audio-provider/design.md) | - | - | implemented |
| [Pumpkin 表情包收集与发送](20261027-pumpkin-sticker-library/design.md) | - | - | implemented |
