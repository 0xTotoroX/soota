# tasks/

> L2 | 父级：[项目约定](../../../AGENTS.md)

原文采集任务的输入、枚举、调度、获取编排与恢复。语义目标和知识交付由 LoreLab 接续；旧字段只用于识别与转交。高层职责以 [docs/providers.md](../../../docs/providers.md) 为主源，对外契约以 [docs/tasks.md](../../../docs/tasks.md) 为主源。

## 成员与入口

- `__init__.py`：包入口。
- `models.py`：输入／目标、持久化并发设置与执行作用域、Weir 任务 credits 上限、条目／任务状态；任务级进度和导出互斥锁不序列化。
- `inputs.py`：清单、链接、本地文件与旧包解析，来源标识和文件指纹。
- `entry.py`：`ingest` 入口及 `prepare_task` 原始计划验证；旧语义参数在写入前转交 LoreLab。
- `policy.py`：已选方案的路由快照、当前停用合并与采集缓存指纹，执行/恢复共用；不修改全局费用权限。
- `dependencies.py`：规划与执行共用的媒体/正文依赖和必需先于可选的阶段划分，图片所需正文不漏报。
- `enumeration.py`：有序展开集合；首个请求前绑定目录子任务，复用原游标、预算和回执。
- `scheduler.py`：最多 N 个在途条目，逐提交复制上下文，同一实际目录有序处理，中断时停止调度并等待写入结束。
- `runner.py`：`execute`／`resume`，串联获取、产物复用、历史原生缓存目标身份校验、失败正文不以包元数据冒充原始数据、步骤状态、任务内共享时效报价和全批预算；识别旧语义／交付任务后转交 LoreLab。
- `acquire.py`：复用 Provider、原生绑定和媒体服务，返回正文、字幕、媒体及真实获取状态；本地 ASR 与旧视频入口共用 `transcripts/quality.py` 时间边界和重复异常门禁。
- `outputs.py`：字幕格式导出和图片获取，不生成分析请求、语义产物或知识交付。
- `state.py`：私有请求／输入文件、去重、锁内合并独立条目快照、任务记录保存及内容包文件锁。

并发 worker 只修改自己的 TaskItem；保存使用 `save_job(..., item=row)` 合并到当前任务。枚举和最终状态归主执行线程；LoreLab 复用采集执行器后处理语义与交付状态。预算预留必须在实际请求前经过共享 `TaskBudget`，不能用单 worker 的局部费用替代全批上限。

[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
