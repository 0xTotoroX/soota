# artifacts/

> L2 | 父级：[Soota 项目约定](../../../AGENTS.md)

- `__init__.py`：原文包模型、读写和路径解析的公共入口。
- `models.py`：原始正文、相对产物路径、SHA-256，以及 v2/v3 manifest；v3 强制显式原件根和逐产物存储根。
- `bundle.py`：旧包加载、新包写入、context 生成、原件保护与哈希检查；`safe_path` 检查单根边界，`artifact_path` 统一解析登记文件的实际位置。

默认 v1/v2 原义保持，只有显式 `raw_root` 创建 v3。新控制包与原件根独立；不靠改写旧 manifest 路径迁移冻结任务。Soota 不实现语义生成，LoreLab 复用本模块检查原件。

[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
