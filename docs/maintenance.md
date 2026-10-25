# 独立安装与统一维护

Soota 的源码、文档、测试和完整 `skills/soota/` 在 LoreLab 的 `modules/soota/` 维护。独立 Soota 仓库是单向生成的发行出口，保留原提交历史；日常使用、采集任务和配置不依赖 LoreLab。

## 独立使用

从独立仓库克隆，并按 README 准备 Python 3.12+ 与 uv：

```sh
git clone https://github.com/0xTotoroX/soota.git
cd soota
uv sync --locked
uv run soota --help
```

使用本地 Agent 时，注册此 checkout 的完整 `skills/soota/`，保留其引用文件与相对路径。源码安装包含 Skill；仅安装 Python wheel 不会自动注册 Agent Skill。包不要求 LoreLab，旧知识命令的兼容转发仍需要另行安装 LoreLab。

升级前检查 `git status --short`。干净的发行 checkout 可 `git pull --ff-only` 后 `uv sync --locked`；Skill 随同一 checkout 一起更新。已有本地修改时停止更新并先审阅，不 reset/clean/stash 用户改动。可以选择已有的 `vX.Y.Z` 标签安装固定版本。用户配置、采集任务和内容包保留在原位置。

已由 LoreLab 成套安装时，使用 `lorelab update` 更新绑定 CLI 与 Skill，不用独立安装覆盖其中一个入口。

## 修改与发行

需要修复 Soota 时，改 LoreLab 的 `modules/soota/`，同时检查对应 Skill 和测试。由 LoreLab 的 `release prepare` 更新版本和组件锁，再经检查、提交和同步；独立仓库不再作为第二个开发入口。

同步回执 `lorehub-provenance.json` 记录主源提交、组件指纹、完整文件清单和 Skill 路径，文件名保留历史兼容。同步提交在原历史之后追加，重复执行不产生相同内容的重复提交。独立仓库若出现直接修改，自动同步会停止，先将有效修正审阅并纳入主源，再处理发行分支。

GitHub 同步需要维护者配置 LoreLab 工作流与跨仓库认证；看到导出目录或此说明不代表在线同步已经启用。具体命令和验收边界以 LoreLab 的 `docs/releases.md` 为准。
