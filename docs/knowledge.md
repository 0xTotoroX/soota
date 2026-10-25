# 内容处理与知识流程迁移到 LoreLab

Soota 负责内容采集、下载、解析、context 和采集任务恢复。新任务只接受 `video/audio/transcript/context/markdown/images/data`，原文内容包可独立使用，不依赖 LoreLab。

单来源 `note/article/analysis/outline`、提交校验、渲染和整包导出全部由 LoreLab content 实现；知识文章、学习大纲、交付回读与授权清理由 LoreLab knowledge/study 维护。Agent 阅读来源、写作并审阅分类查重合并，CLI 校验与保存，不自行调用模型。普通聊天总结不要求入库，也不强加文章前置或三点格式。

安装 LoreLab 后，从 `lorelab skills list` 定位完整 LoreLab Skill；其内容操作见 `references/content.md`，知识流程见 `references/process.md`。产品契约分别在 LoreLab 的 `docs/content.md` 与 `docs/knowledge.md`，Soota 不复制这些语义指导。

| 请求 | 正式入口 |
| --- | --- |
| 单来源语义产物 | `lorelab content ingest [INPUT] --want context,note` |
| 提交并接续任务 | `lorelab content resume JOB --analysis submission.json` |
| 渲染已有内容包 | `lorelab content render --bundle PATH --analysis PATH [--force]` |
| 导出内容包 | `lorelab content export --bundle PATH --destination PATH`，或 `--vault PATH --subdir DIR` |
| 知识文章或学习交付 | `lorelab knowledge` / `lorelab study` |

## 配置、独立组件与兼容入口

旧 `soota ingest` 带语义目标或 `--destination/--vault/--subdir`、`soota jobs resume` 旧语义或交付任务、`soota render` 仅作隐藏兼容桥转 LoreLab content；旧 `soota knowledge/study` 转发对应 LoreLab 工作流。内容 ingest/resume/render 兼容桥仅动态调用同一 Python 环境中的 LoreLab，不从 PATH 另选产品。旧 knowledge/study 保留原转发规则；受管安装缺少绑定入口时不改用 PATH 上另一份产品。

独立 Soota 未安装 LoreLab 时，语义与交付入口明确报告需要安装，并在修改任务、预算或产物前停止。不得修改旧任务请求、伪造完成状态或重新清零预算绕过安装要求。普通采集和原文包不受影响。

已有任务按原 ID/JOB 路径恢复，`task_id/item_id`、source 证据、原路径、预算预留、产物以及知识任务冻结状态、删除条件保持。Soota 的旧 `TaskSpec` 语义词/目标字段和 `AnalysisManifest` 状态只作数据兼容，没有对应的语义生成或导出实现。交接身份、正文指纹、目标一致时保留 `analysis-request.json` 原字节，旧冻结 hash 不变。

`Analysis`、`KeyPoint`、`Chapter` 和全部语义渲染实现已归 LoreLab `src/lorelab/content/`；Analysis schema 位于 LoreLab 主仓 `schemas/analysis.v1.json`，不在 Soota 组件内维护。

旧配置和显式 `SOOTA_KNOWLEDGE_*` 变量由 LoreLab 兼容读取；不搬动任务或复制私人配置。`lorehub` 命令不再提供。

## 目的地与既有笔记

采集的 `--output` 仍指定源包位置；`--destination/--vault/--subdir` 的导出实现归 LoreLab。导出保留相对附件链接，沿用 `.soota-export.json` 检查人工修改，`--force` 不绕过冲突。

分类、查重与既有笔记合并属于 Agent 的审阅与编辑行为。当前 `knowledge publish` 只接受新路径或相同 bytes，不自动覆盖合并；冲突需在当前授权内单独审阅处理，保全人工内容，不另存重复主题规避。
