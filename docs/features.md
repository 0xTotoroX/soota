# 功能入口与费用策略

业务别名的 auto 路由遵守按平台保存的费用/手动优先级：视频详情使用 inspect 规则，其他别名使用 api 规则。可显式使用 `--provider monid` / `--provider treg`，或通过 `features list --provider monid` / `--provider treg` 查看各自受控覆盖范围。`monid.<key>` 和 `treg.<key>` 原生入口固定使用对应来源，`tikhub.<key>` 固定直连；不能指定矛盾的 Provider。执行时共用有时效的报价，过期自动刷新，详见 [treg 与报价缓存](treg.md)。配置和报价查看见 [Monid 与来源优先级](monid.md)。

Soota 保留自己的采集、字幕、目录、任务、费用与运行时命令，以及已核验的 11 个统一业务入口和 TikHub 接口封装。`features` 不再批量导入 OpenCLI 的原生命令；旧的 `opencli.*` 功能 ID 已移除，调用时会提示改用上游 CLI。

## 按任务发现能力

对于已有标准流程，直接使用 `ingest`、`inspect` 或 `catalog`，由 Soota 按已接入的能力路由。临时查询或组合任务由 Agent 依次查阅以下入口；这是任务执行顺序，不会把任意上游返回值自动转换成 Soota 内容包。

1. 查 `opencli list -f json` 和具体命令的 `--help`，使用 OpenCLI。
2. YouTube/Bilibili 单视频元数据和字幕可使用 `soota inspect/ingest --provider yt-dlp`，由对应 Provider 调用 yt-dlp。
3. 免费来源无法提供所需能力时，查 `soota api list/show`，通过 Soota 包装的 TikHub 接口执行，遵守费用策略。

opencli-mcp 默认停用，不进行发现或连接；`mcp search/run` 和显式 `--provider opencli-mcp` 会在启动进程前拒绝。用户明确选择其他来源时遵从该选择。空结果、登录失败、限流或请求结果未知，不等于缺少能力，也不授权自动重试或付费。

```bash
opencli list -f json
opencli youtube transcript --help
uv run soota api list --platform youtube --json
uv run soota api show bilibili.web.fetch_one_video --json
```

## Soota 保留的功能注册表

```bash
uv run soota features list --site bilibili --json
uv run soota features list --provider tikhub --search search --json
uv run soota features show twitter.profile --json
uv run soota features call twitter.profile --param username=example --dry-run --json
uv run soota features call twitter.profile --param username=example --json
```

`features` 默认显示 11 个统一业务入口和 TikHub 注册表；停用的 MCP 项不展示。TikHub 快照选取新版后有 964 个入口：900 个内容查询、63 个辅助接口、1 个已废弃入口。辅助接口需要显式 `--allow-write`，未知价格仍停止，已废弃接口不可执行。这个选项不代表每个辅助接口都会写入。

统一入口的 `plans` 说明可用来源；指定 `tikhub.*` ID 表示明确选择 TikHub。复杂参数用 `--params-file`，TikHub 请求体用 `--body`。`features call --output NEW_FILE.json --json` 保存凭据脱敏后的原生响应；需要标准字幕、Markdown 或目录续跑时使用 `ingest` / `catalog`。

## 已核验的重叠映射

默认顺序为 OpenCLI → yt-dlp（已接入能力）→ 经授权的 TikHub。`--provider opencli`、`--provider yt-dlp`、`--provider tikhub` 严格选择对应来源；`--provider auto` 明确恢复默认顺序。自动付费模式不改变来源排序；只有轮到 TikHub 时才改变付费确认方式。

| 统一入口 | 共同输入 | 语义范围 |
|---|---|---|
| `twitter.profile` | `username` | 用户资料，允许去掉前导 @ |
| `instagram.profile` | `username` | 用户名查询，不是数字用户 ID |
| `reddit.profile` | `username` | 用户资料，允许去掉 u/ |
| `tiktok.profile` | `username` | 映射到 TikHub `uniqueId` |
| `weibo.profile` | `uid` | 仅数字 UID |
| `zhihu.answer.detail` | `answer_id` | 数字回答 ID，全文原始格式不同 |
| `instagram.user.search` | `query` | 首批用户搜索，无统一游标 |
| `twitter.post.search` | `query`，可选 `sort` | 首批帖子；top/latest，无统一条数保证 |
| `bilibili.video.detail` | `url` | 标准视频链接 |
| `youtube.video.detail` | `url` | 标准视频链接；soota 返回标准元数据，`output_contract=soota_metadata` |
| `xiaohongshu.note.detail` | `url` | 已展开官方笔记链接，含 xsec_token；输出脱敏 |

仅在请求发送前确认免费来源不可用时，统一入口才考虑下一方案。执行失败、限流、登录问题、无效 JSON、断线后结果不确定都不会触发盲目重试或自动收费。其他能力通过 OpenCLI 原生命令或 TikHub API 查询，不按同名猜测互换参数或分页。

确认没有字幕轨道时不继续查下一字幕来源：`auto` 直接本地转写，`captions-only` 返回无字幕；`--audio-provider yt-dlp/downie/tikhub` 独立选择音频来源，默认 yt-dlp。

已有标准流程也遵循此优先级：Bilibili 和 YouTube 元数据与字幕都先试 OpenCLI，字幕获取未知时可由 yt-dlp 免费兜底，TikHub 保留独立实现。X 的 OpenCLI 目录是最近快照，YouTube/Bilibili 的可续跑目录使用 TikHub；不能交换游标。其他上游命令并不自动等于 Soota 的可续跑历史目录。

## 两种付费模式

```bash
# 查看当前策略；不改配置
uv run soota policy show --json

# 默认模式：免费来源优先，每次 TikHub 计费请求都询问
uv run soota policy set --mode ask

# 保持来源顺序，轮到 TikHub 时自动付费；余额触及 5 USD 就暂停
uv run soota policy set --mode auto --min-balance 5

# 保持来源顺序，轮到 TikHub 时自动付费；不设余额限制
uv run soota policy set --mode auto --no-balance-limit
```

配置保存在 `~/.config/soota/payment-policy.json`（或 `SOOTA_CONFIG_DIR`），不会改写现有 `config.toml`。本次实现没有替用户启用自动收费；初始模式仍为 ask。

ask 在交互终端把接口和预估 USD 费用写到 stderr，默认 `N`，输入 `y` / `yes` 才执行当前请求。一个字幕任务可能含多次请求，会分别询问。stdin 不是终端时返回 `payment_required` 并停止；向管道输入 `yes` 不会自动授权。JSON stdout 保持单个文档。缓存命中与明确免费请求无需付费确认。

auto 配置必须明确给出 `--min-balance` 或 `--no-balance-limit`。余额阈值只使用 TikHub 的 **USD 付费余额 balance**，不加免费额度。每次付费请求前重新读取余额；若 `balance - 本次预估费用 <= 阈值`，或余额无法确认，则暂停。免费余额和定价元数据接口在本机跨进程限速；不缓存旧钱包当成当前余额。

`--max-cost USD` 是可选附加上限，不替代 ask 的逐次确认；0 明确禁止计费接口。目录上限累计计算，续跑不清零，省略新上限保留旧值；旧任务的来源和预算不被新策略覆盖。自动模式仍受该预算、参数校验和登录限制约束。额度预留和余额检查基于预估费用，不是服务端原子扣费控制，其他客户端同时消费或上游涨价仍可能改变实际余额。

## 代码边界

- `features.py`：Soota 业务入口、已接入 MCP 适配器和 TikHub 注册表路由。
- `mcp_cli.py`：按需搜索和执行 MCP 只读站点命令。
- `providers/mcp_contract.py`：根据上游描述验证站点命令与 JSON 参数。
- `feature_aliases.py`：人工核验的参数映射与语义限制。
- `features_cli.py`：功能 list/show/call 命令。
- `providers/opencli_commands.py`：供现有业务入口使用的元数据、参数验证和无 shell 调用。
- `providers/mcp_client.py` 与 `providers/opencli_mcp.py`：内部 stdio 客户端、只读能力白名单和规范化。
- `billing.py`：每请求付费确认、余额暂停和免费元数据限速。
- `policy_cli.py`：策略显示和原子保存。
- `providers/tikhub/client.py`：计费 gate 与 HTTP 发送的统一边界，禁用隐式重试。

[OpenCLI 上游](https://github.com/jackwener/OpenCLI) · [TikHub 用户信息](https://docs.tikhub.io/186826050e0) · [终端教程](terminal-tutorial.md) · [Provider 架构](providers.md)


Weir 的标准入口为 `ingest URL --provider weir --want context`，覆盖 X/Article 与 Reddit 单帖正文，不属于 `features call` / `api call` 的 TikHub 注册表。免费模式、credits 配置和 auto 能力兜底条件见 [Weir](weir.md)。
