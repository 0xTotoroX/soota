# providers/

> L2 | 父级：[项目约定](../../../AGENTS.md)

## 成员与职责

- `__init__.py`：Provider 包入口。
- `base.py`：音频、字幕和通用 Provider 协议。
- `audio.py`：音频来源选择、费用估算、已有音频复用和延迟派发。
- `downie.py`：显式选择的 Downie 下载与完成状态处理。
- `downie_complete.sh`：Downie 完成回调。
- `managed.py`：隔离 OpenCLI MCP／TikHub 运行环境的绑定。
- `mcp_client.py`：MCP 客户端连接与工具调用。
- `mcp_contract.py`：MCP 只读命令参数契约校验。
- `opencli.py`：OpenCLI Provider 获取入口。
- `opencli_commands.py`：OpenCLI 命令发现与能力清单。
- `opencli_context.py`：浏览器 profile、daemon 上下文与连接预检。
- `opencli_errors.py`：OpenCLI 错误分类和安全诊断。
- `opencli_mcp.py`：可选 OpenCLI MCP Provider。
- `opencli_patches.py`：受管 OpenCLI 命令补丁。
- `opencli_runtime.py`：OpenCLI 运行环境与安装检查。
- `quotes.py`：按地址和凭据隔离的时效报价、任务内共享、过期刷新与失败去重。
- `priority.py`：按平台／能力读取优先级和报价，规划预览与执行共用 rank_quotes 排序。
- `process.py`：有超时和输出上限的子进程运行。
- `process_pipes.py`：受控进程管道读取与资源限制。
- `router.py`：采集 Provider 路由、策略与预算检查。
- `soota.py`：旧名称对 yt-dlp Provider 的兼容导入。
- `tikhub_worker.py`：隔离环境中的 TikHub SDK 桥。
- `weir.py`：免费余额检查、跨 Key 轮询／401 跳过、HTTP 获取与 credits 回执结算。
- `weir_keys.py`：私有主用／命名备用凭据、启停、环境覆盖、持久轮询游标、脱敏清单与绑定当前 Key 的赠送额度确认。
- `windows_process.py`：Windows 进程树与管道控制。
- `ytdlp.py`：yt-dlp 元数据、字幕和媒体获取。
- `monid/`：Monid 获取和费用策略，见其局部地图。
- `treg/`：固定读取映射、报价与回执、字幕获取，见其局部地图。
- `tikhub/`：TikHub 注册表、原生接口与平台适配。

Provider 获取原始数据；Weir 正文绑定和确定性转换在 `content/weir.py`，跨 Key／任务的 UTC 月度账本在 `storage/credit_budget.py`。主源同步要求见父级约定。

[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
