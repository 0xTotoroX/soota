# 分析与渲染迁移到 LoreLab

单来源分析、笔记、文章、大纲的写作指导及提交、校验、渲染和导出均由 LoreLab content 与 LoreLab Skill 维护。安装后用 `lorelab skills list` 找到 LoreLab Skill，按其 `references/content.md` 执行；不在独立 Soota 复制语义流程或 schema 指导。

新任务使用 `lorelab content ingest/resume/render/export`。旧 `soota render`、带语义目标或导出参数的 `soota ingest`、旧语义或交付任务的 `soota jobs resume` 仅作隐藏兼容转发，动态调用同一 Python 环境中的 LoreLab，不从 PATH 另选产品。未安装 LoreLab 时明确报需要安装，在修改任务、预算或产物前停止。兼容详情见[迁移说明](../../../docs/knowledge.md)。

Soota 采集默认到 context 结束，原文包与采集恢复独立可用。普通聊天总结可直接阅读 context，不要求文章前置、内容任务或知识库写入。保留旧 task/item 身份、source 证据、原路径和预算；兼容字段不意味着 Soota 仍执行语义处理。
