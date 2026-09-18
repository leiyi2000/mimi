---
status: active
owner: Mimi maintainers
updated: 2026-09-18
---

# 开发规范

本文件定义编码、测试和 Git 交付规则。可执行硬约束以
[`AGENTS.md`](../../AGENTS.md) 为准。

## 1. Python

- 目标版本为 Python 3.12+，使用现代类型语法。
- 依赖由各项目自己的 `pyproject.toml` 和 `uv.lock` 管理。
- HTTP 使用 `httpx`；异步文件使用异步 API。
- 配置使用不可变 `dataclass` 或入口集中装配。
- 不引入兼容性导入、复杂装饰器、元编程或隐式上下文。
- 注释少量、英文，只解释非显然约束。

## 2. 命名与结构

- Python 模块使用 snake_case，文档路径使用 kebab-case。
- 业务代码留在所属插件；插件入口不堆积领域转换和渲染细节。
- 用户可见文案使用中文。
- `commands.toml` 的 `name` 是真实触发词，`usage` 只用于展示。

## 3. 测试

- 除纯文档修改外，必须运行所有受影响插件的测试，并在交付说明中记录命令和结果。
- 默认测试离线运行，真实网络测试由环境变量显式开启。
- Bug 修复先增加能够复现问题的测试或固定 fixture。
- 外部接口覆盖成功、错误响应、缺字段和超时。
- 海报测试验证 HTML 关键内容、PNG 非空和合理尺寸。
- 测试覆盖图片回复时，必须把最终 PNG 写入 `plugins/<name>/tests/output/`，便于直接
  对比布局、字体、裁切和资源降级效果；输出目录不得提交 Git。
- 持久化改动验证空库初始化、CRUD 和旧数据兼容。

## 4. Git

- 修改前执行 `git status --short`，不覆盖用户已有改动。
- 提交信息使用 `feat`、`fix`、`docs`、`refactor`、`chore` 等语义前缀。
- 一个提交聚焦一项可解释变化。
- 不提交 `.env`、数据库、Cookie、缓存、虚拟环境或生成图片。
- 特性档案中的 commit hash 必须来自真实 Git 历史。

## 5. 文档同步

- 新增、删除或修改命令时，逐项核对处理器、`commands.toml`、插件 README 和测试。
- `commands.toml` 的 `name`、`usage`、`description`、`badge` 必须与实际触发词、参数、
  行为和权限一致。
- 面向所有用户的能力、命令入口或输出样式变化时，检查并更新根 README；无须修改时也
  必须完成核对。
- 架构变化：更新根 `ARCHITECTURE.md`。
- 设计规则变化：更新 `docs/spec/`。
- 可复用排障或优化经验：更新 `docs/develop-guide/development.md`。
- 新的独立能力：按首次提交日期增加 `docs/feature/<date-slug>/design.md`。
- 文档路径只写仓库相对路径，不记录本机目录、其他项目路径或临时文件路径。

## 6. 提交前验证

```bash
git diff --check
docker compose config --quiet
cd plugins/help && uv run pytest
cd plugins/apex && uv run pytest
cd plugins/lol && uv run pytest
```

按改动范围运行，不要求文档变更重复执行全部业务测试。

提交前还必须执行命令文档一致性检查：

1. 从实际处理器确认触发词、参数范围、权限和失败行为。
2. 对照所属插件的 `commands.toml`。
3. 对照所属插件 README 的命令表。
4. 检查根 README 是否仍准确描述用户可见能力。
