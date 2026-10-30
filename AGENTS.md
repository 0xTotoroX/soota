# Soota 项目约定

## 开发主源与独立发行

- Soota 的源码、文档、测试和本 Skill 只在 LoreLab 的 `modules/soota/` 修改。独立仓库保留历史与独立安装能力，由 LoreLab 单向同步；不要在两个仓库各修一份。
- 版本准备和同步从 LoreLab 执行，独立仓库的 `lorehub-provenance.json` 是生成回执，不手改。发现独立修改时先审阅并移回主源，不强推或自动丢弃。详见 `docs/maintenance.md`。

## 公开文档与本地资料

- README 是对外产品文档，只写当前能力、安装、使用、限制和许可证。禁止写内部开发计划、里程碑编号、任务进度或下一阶段安排。
- 内部计划和进度保存在项目外的 `$SOOTA_CONFIG_DIR/private/`（未设置时为 `~/.config/soota/private/`）。历史本地路径 `docs/private/`、`PLAN.md`、`PLAN.zh-CN.md` 继续保持 Git 忽略和构建排除，不得强制加入 Git、提交、上传或放进发行包。
- 公开 `docs/` 同样不得承载私人计划；允许客观说明现有功能限制、架构与验证边界。
- 发布前检查当前文档、历史 README 和发行包元数据，不能只删当前工作区的计划内容而保留在历史版本。
- 修改产品代码与配置前核对现有实现；文档清理不得改变功能行为或历史源码内容。

## 用户数据与 Skill

- 个人关注名单、账号别名、凭据、私人路径和执行规则放在项目外的用户配置或私人知识库中，不得写入源码、公开文档、测试样例或共享 Skill。示例使用通用占位符或合成测试数据，不保留个人使用记录。个人路径、账号选择和凭据由环境变量注入，仓库仅保留变量名称和读取逻辑。
- Skill 只维护通用操作流程和来源顺序；已有标准命令继续由 Soota 路由。MCP 当前默认停用；YouTube/Bilibili 字幕获取按平台和能力的 cost/manual 优先级选择；免费来源默认优先，Monid 和 treg 可选启用，Monid/treg/TikHub 均受费用策略控制。确认没有字幕轨道时结束字幕 Provider 查询，auto 直接获取音频并本地转写；未知或获取失败不能冒充无字幕。音频默认 yt-dlp，Downie 仅显式选择，TikHub 音频遵守同一次运行的剩余预算。临时任务按实际能力查找，不恢复批量 OpenCLI 命令映射。
- 采集、内容包或兼容转发约定变化时，同步检查 `skills/soota/SKILL.md` 及相关引用。个人资料库的规则与 prompt 只在本地维护，Skill 指导 Agent 从目标目录的 `AGENTS.md` 读取对应文件，不复制私人正文或路径。
- 用户数据所在目录需核对自身的 Git 与同步规则，不能把“位于项目外”当作隐私保证。

## Context 与文章

- 默认摄取到 `context.md` 即结束：只做格式解析和按原顺序提取文本，不做语义改写、润色、合段或自动摘要。
- 分析／总结请求直接使用 context；只有用户明确要求详细文章阅读或知识整理归档时才整理文章；知识任务不是普通分析的隐式前置步骤。旧目录规则或提示词不得隐式添加文章中间步骤。
- `transcript.md` 保留时间轴；阅读／分析产物与源正文分开。重新采集不覆盖已有文章；显式语义渲染由 LoreLab content 执行，不重写源文件。

## 处理层与文档同步

- `planning/` 负责采集前的方案比较、结构化预设匹配、确认与保存；自然语言归一和询问由 Agent/Skill 完成。报价按需刷新，不后台轮询。预设不能覆盖当前停用来源或费用保护；执行和恢复继续用 `tasks/` 与原账本。契约见 `docs/planning.md`。

- 高层黑盒流程与模块职责以 `docs/providers.md` 的“处理流程与职责”为主源；采集任务的目标、输入、恢复与 v2 原文产物以 `docs/tasks.md` 为主源；语义目标、分析校验、渲染和整包导出归 LoreLab content，知识整理、学习与交付归 LoreLab knowledge/study；字幕格式与限制以 `docs/subtitle-formats.md` 的“统一转换层”为主源。
- 修改模块职责、阶段顺序、获取／转换接口、输入输出或实现能力时，同步更新受影响的职责图、入口表和格式能力表；图描述已实现状态，不能把目标或计划写成现有能力。
- Provider 只负责获取、轨道与获取格式选择、原始正文和获取状态。字幕正文解析集中在 `transcripts/formats/`，统一入口为 `transcripts/conversion.py`；编排层在转换成功后写字幕缓存。新增 Provider 不复制已有格式解析器。
- 本地 ASR 转换后经 `transcripts/quality.py` 检查音轨 EOF、时间顺序和重复异常；拒绝输出单独留证，不裁剪冒充完整。空转写不证明无语音，旧视频入口的 ASR 转写缓存按质量检查版本隔离；既有任务/内容包不自动重审，源媒体完整性校验保持独立。
- 统一任务的执行与恢复归 `tasks/`；动态发现和已核实的原生结果绑定归 `content/`；媒体下载、合并、提取与验证归 `media/`；原文内容包与文件校验归 `artifacts/`；语义提交、渲染与导出实现全部归外部 LoreLab content；知识文章、学习、交付核对和授权清理归 LoreLab knowledge/study；`workflow_compat.py` 只转发旧命令，不保存流程。新增 Provider 或格式复用这些边界，不复制整条任务流程。
- 修改输入、`--want` 采集目标、采集条件、预算、任务状态、内容包字段、产物路径或兼容桥行为时，同步核对 `docs/tasks.md`、README 命令示例、`skills/soota/SKILL.md` 及 `references/tasks.md`；职责变化同时更新高层图。文档只声明已实现行为，动态发现、契约测试和真实站点验收分别记录。
- 全批计费预留在请求发送前持久化，恢复不能清零；成功以必需产物及其验证结果判断，枚举完整性单独报告。原生读取失败不能用包元数据替代为成功的数据产物。
- Soota 采集保留已有分析或文章，来源变化标记过时。语义提交的正文指纹、证据 ID、task/item 身份校验及目的地冲突保护由 LoreLab content 负责；Soota 不保留另一份语义执行。分类查重合并由 Agent 在 LoreLab Skill 中审阅，不由 CLI 推断。

## 与 LoreLab 的职责边界

- 组件内部平台适配归 `platform_support.py`、`storage/locking.py`、`providers/process_pipes.py` 与 `providers/windows_process.py`。锁、进程树取消及资源上限不能降级为空操作；平台缺少硬限额时派发前明确失败。对外边界维护在 `docs/providers.md` 的 Windows 运行说明。
- Soota 负责正文与媒体采集、解析、context、预算、缓存和采集任务恢复。新任务只接受 `video/audio/transcript/context/markdown/images/data`；`note/article/analysis/outline` 与 `--destination/--vault/--subdir` 的实现全归 LoreLab content。`--output` 仍指定采集包位置。
- 单来源分析、笔记、文章、大纲、分类审阅和整包导出，以及知识学习交付与授权清理由 LoreLab 及其 Skill 维护；不在 Soota 重建工作流、分析校验器或复制写作提示词。
- 旧 `soota ingest` 带语义目标/导出参数、`soota jobs resume` 旧语义或交付任务、`soota render` 仅作隐藏兼容桥，动态调用同一 Python 环境的 LoreLab content，不从 PATH 另选产品；旧 `soota knowledge/study` 通过 `workflow_compat.py` 转发。无 LoreLab 时明确报需要安装，在修改任务、预算或产物前停止；受管安装缺失时不误用其他产品版本。独立采集和原文包不依赖 LoreLab，组件源码与 Skill 引用须自含。
- 旧 `TaskSpec` 的语义词/目标字段与 `AnalysisManifest` 状态只保留数据兼容，没有语义生成或导出实现。`Analysis`、`KeyPoint`、`Chapter` 及全部写作渲染实现归 LoreLab `src/lorelab/content/`，分析 schema 归 LoreLab 主仓 `schemas/analysis.v1.json`，不在独立 Soota 复制。已有采集任务仍用 `soota jobs`；旧语义任务由 LoreLab content 按原 task/item 身份、source 证据、原路径、预算与产物恢复；已有知识任务通过 LoreLab 按原绝对路径恢复，冻结来源、指纹和清理条件不变。`lorehub` 命令继续保持移除，历史配置和环境变量由 LoreLab 兼容读取。
