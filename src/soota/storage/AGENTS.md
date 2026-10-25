# storage/

> L2 | 父级：[项目约定](../../../AGENTS.md)

## 成员与职责

- `__init__.py`：包入口。
- `atomic.py`：同目录临时文件、fsync 和原子替换。
- `locking.py`：Unix flock／Windows LockFileEx，共享／排他以及阻塞／立即失败的跨进程内核锁。
- `cache.py`：采集缓存读取、过期判断与保存。
- `jobs.py`：任务记录与跨进程任务执行锁；统一任务的线程合并由 `tasks/state.py` 负责。
- `ledger.py`：请求费用流水记录。
- `credit_budget.py`：跨任务月度／任务 credits 预留、回执结算、异常计费停用与未决请求持久保留。
- `task_budget.py`：全批持久化预算、线程与文件锁、请求前预留，以及通过 contextvars 传递的活动任务身份。

共享 TaskBudget 的预留读取／检查／写入不可分割。恢复只更新总上限，不清除历史预留；原子文件替换本身不能替代并发读改写互斥。高层职责见 [docs/providers.md](../../../docs/providers.md)。

[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
