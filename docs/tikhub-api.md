# TikHub 接口与 CLI 能力

2026-09-12 已接入统一接口注册表与 `platforms`、`api list/show/call`。现已增加 [功能入口与费用策略](features.md)，默认先尝试已核验的 OpenCLI，再按付费策略使用 TikHub；opencli-mcp 默认停用。

注册表来自当天的 [TikHub 公开 OpenAPI](https://api.tikhub.io/openapi.json)，服务文档版本为 `V5.3.2`。共登记 1,063 个 GET/POST 接口；同功能去除旧版本后有 964 个首选操作，其中 901 个属于可调用的内容查询，覆盖 21 个平台/产品入口。微信拆为视频号、公众号、搜一搜，因此按通常平台名称合并为 19 个平台。

这里的覆盖表示请求 Schema、路由与通用 SDK 调用已接入，不表示 901 个接口逐一通过真实网络验收。标准化 `ingest`、字幕/ASR 和可续跑 `catalog` 目前只适配 YouTube、Bilibili。

`api call` 默认按平台的 api 规则选择来源，可使用 `--provider tikhub` 强制直连，或 `--provider monid` / `--provider treg` 转发各自已覆盖接口。来源配置、比较费用和覆盖限制见 [Monid 与来源优先级](monid.md)。接口注册表的覆盖不等于 Monid 或 treg 全部覆盖；treg 固定映射与自动报价缓存见 [接入说明](treg.md)。

## 版本规则

同平台、同接口族、同功能优先选择最高的未废弃版本：V3 > V2 > V1，按数字比较，V10 也高于 V3。先比较资源版本，再比较该资源内的操作版本。`web` 与 `app` 是不同接口族；新版没有的独有功能继续保留其现有版本。

当前 API 根路径都是 `/api/v1`，不能据此判断具体接口是否为 V1。版本可能位于 `/web_v2`、`/app/v3`、`/v3`，或操作名的 `_v2`、`_v3` 后缀。示例：

| 稳定接口标识 | 当前选择 |
| --- | --- |
| `bilibili.web.fetch_one_video` | `/api/v1/bilibili/web/fetch_one_video_v3` |
| `bilibili.web.fetch_user_post_videos` | `/api/v1/bilibili/web/fetch_user_post_videos_v2` |
| `youtube.web.get_video_info` | `/api/v1/youtube/web_v2/get_video_info_v2` |
| `instagram.get_user_posts` | `/api/v1/instagram/v3/get_user_posts` |
| `wechat_mp.fetch_article_detail_h5` | `/api/v1/wechat_mp/v2/fetch_article_detail_h5` |
| `douyin.web.fetch_one_video` | `/api/v1/douyin/web/fetch_one_video_v2` |
| `tiktok.web.fetch_post_detail` | `/api/v1/tiktok/web/fetch_post_detail_v2` |

`api show/call` 接受注册表中的 key 或完整路径；传入已登记的旧路径也会解析到同功能新版。调用前必须使用新版参数，例如 Bilibili 视频详情 V3 使用 `url`，不会静默将旧版 `bv_id` 改写成其他参数。经过核对的 Instagram `fetch_*` → `get_*` 更名使用显式别名归并。

“最新”指本软件携带且经过审查的注册表快照。启动和离线查询不会自动下载或执行远端定义；后续版本通过更新注册表、检查参数及响应变化、运行契约测试后交付。不得为了降低费用选择旧版，也不在新版失败时自动尝试一串旧接口。

## 平台清单

数量为各平台去重后、标记 `callable: true` 的首选查询操作。资源路径只列 API 根路径和平台名之后的目录；具体操作还可能带版本后缀。出现旧资源目录表示它仍包含新版尚未提供的独有功能。

| 平台/产品 | CLI 平台标识 | 查询操作 | 已接入资源路径 |
| --- | --- | ---: | --- |
| Bilibili | `bilibili` | 83 | `app`, `huahuo`, `web` |
| 抖音 | `douyin` | 288 | `app/v3`, `billboard`, `creator`, `creator_v2`, `douplus`, `index`, `search`, `web`, `xingtu`, `xingtu_v2` |
| Instagram | `instagram` | 46 | `v1`, `v2`, `v3` |
| 快手 | `kuaishou` | 34 | `app`, `web` |
| Lemon8 | `lemon8` | 16 | `app` |
| LinkedIn | `linkedin` | 8 | `web_v2` |
| 皮皮虾 | `pipixia` | 17 | `app` |
| Reddit | `reddit` | 28 | `app` |
| Telegram | `telegram` | 7 | `web` |
| Threads | `threads` | 10 | `web` |
| TikTok | `tiktok` | 133 | `ads`, `analytics`, `app/v3`, `creator`, `shop/web`, `web` |
| 今日头条 | `toutiao` | 7 | `app`, `web` |
| X / Twitter | `twitter` | 25 | `web` |
| 微信视频号 | `wechat_channels` | 12 | `v2` |
| 微信公众号 | `wechat_mp` | 11 | `v2` |
| 微信搜一搜 | `wechat_search` | 2 | `v2` |
| 微博 | `weibo` | 56 | `app`, `web_v2` |
| 小红书 | `xiaohongshu` | 44 | `app_v2`, `pgy`, `web_v3` |
| 西瓜视频 | `xigua` | 6 | `app/v2` |
| YouTube | `youtube` | 27 | `web`, `web_v2` |
| 知乎 | `zhihu` | 41 | `web` |

`soota platforms --json` 可查看数量、接口族、任务类型、需要登录态的操作数和标准化能力。部分创作者或分析接口要求额外 Cookie 等参数；以 `api show` 的必填字段为准。通用层不会替用户登录，也不执行设备注册、签名生成、账户或支付管理等辅助接口。这些定义在 api 视图中 `callable=false`；完整功能并集的 `features call tikhub.KEY --allow-write` 可显式调用非废弃辅助接口，仍经过参数和费用检查。

## CLI 可以完成的任务

| 任务 | 命令 | 当前范围与输出 |
| --- | --- | --- |
| 查看平台与能力 | `platforms` | 离线列出上述入口，并区分 API 查询与标准化流程 |
| 查找接口 | `api list --platform P --task T` | 按平台、任务或 `--search` 搜索；默认首选版本，`--all-versions` 查看历史版本 |
| 查看参数契约 | `api show KEY` | 当前路径、GET/POST、必填参数、JSON 请求体及文档链接 |
| 验证请求 | `api call KEY ... --dry-run` | 完全离线选版本与验证字段、类型、范围；不创建任务或输出文件 |
| 查询并导出 | `api call KEY ... --max-cost USD` | 一次内容查询；返回脱敏平台 JSON，`--output` 保存到新文件 |
| 识别和查看视频 | `inspect URL [--offline]` | YouTube、Bilibili；`--offline` 只识别来源，否则可能调用接口 |
| 获取字幕或本地转写 | `ingest URL --transcript MODE` | YouTube、Bilibili；`auto`、`captions-only`、`asr`、`none`，生成内容包 |
| 获取创作者目录 | `catalog URL --max-cost USD` | YouTube 频道、Bilibili UP 主；分页、去重、增量快照与检查点 |
| 查看或续跑任务 | `jobs list` / `jobs resume ID` | 旧单条任务可查看；只有目录任务支持续跑 |
| 预估摄取或目录 | `estimate URL --mode ingest\|catalog` | 目录预估完全离线；摄取预估可能查询钱包 |
| 渲染分析内容 | `render --bundle ... --analysis ...` | 校验外部提供的 Analysis JSON 后写入 Markdown，不重新获取视频 |
| 检查环境 | `doctor` | 配置、密钥是否存在、工具依赖等本机状态 |

`api list --task` 接受以下单个值：`detail` 详情、`profile` 用户/频道资料、`catalog` 作品列表、`search` 搜索、`comments` 评论/回复、`media` 字幕/媒体地址、`trends` 热榜、`live` 直播信息、`analytics` 数据分析、`commerce` 商品/广告信息、`utility` ID/链接辅助查询。一个接口可以属于多个类型；各平台并不具备全部能力。

通用 `api call` 不自动跟随游标、不创建可续跑任务、不缓存查询结果，也不执行 ASR 或 Markdown 转换。它可以获取原始列表或评论，但目录标准化、批量深度处理和评论分析是另一些能力。CLI 本身不调用 LLM 生成摘要；Codex 可根据结果准备 Analysis 再交给 `render`。

## 调用示例

在项目目录使用 `uv run soota`，以下简写为 `soota`：

```bash
soota platforms --json
soota api list --platform instagram --task comments --limit 20 --json
soota api list --platform xiaohongshu --search note --json
soota api show bilibili.web.fetch_one_video --json
soota api call bilibili.web.fetch_one_video \
  --param 'url=https://www.bilibili.com/video/BV1M1421t7hT' --dry-run --json
```

`api list` 默认最多返回 50 项；用 `data.next_offset` 作为下一次的 `--offset`。返回的 `callable` 决定是否可执行，不能只根据 HTTP 方法判断。

执行一次查询并保存 JSON：

```bash
soota api call bilibili.web.fetch_one_video \
  --param 'url=https://www.bilibili.com/video/BV1M1421t7hT' \
  --max-cost 0.01 --output ./bilibili-detail.json --json
```

查询参数可重复使用 `--param name=value`；复杂值也可放入 `--params-file query.json`。只有契约要求数值、布尔或数组时才转换文本类型，字符串形式的长 ID 和前导零保留原样。重复参数、未知字段、非法类型、越界值和缺失必填字段在联网前拒绝。

POST 的 `--body request.json` 根据契约接受 JSON 对象、数组或字符串。例如公众号文章详情的 `request.json`：

```json
{"url": "https://mp.weixin.qq.com/s/ARTICLE_ID", "raw": false}
```

```bash
soota api show wechat_mp.fetch_article_detail_h5 --json
soota api call wechat_mp.fetch_article_detail_h5 --body request.json --dry-run --json
```

其他 POST 也可能把全部参数定义在 query 中、不接受 JSON body；少数接口把 Cookie 定义为整个 JSON 字符串请求体。以当前 Schema 为准，不能假定 POST 都接收对象。凭据只放本地参数文件，输出和文件中的凭据值及其编码回显会脱敏。

## 费用与结果

执行 API 查询默认逐次询问付费，也可明确配置自动付费；`--max-cost`（USD）为可选附加上限。CLI 先请求免费的 `/api/v1/tikhub/user/get_endpoint_info` 定价元数据，校验它对应当前所选接口，再检查费用、确认付款或自动模式下检查余额，写入账本预留，最后发送目标请求。未知、负数、无效或超过预算的单价会阻止目标请求；SDK 自动重试关闭。

`reserved_cost_usd` 是按查询时单价计算的预留，`actual_cost_usd` 为 `null`，不能当作已核实账单。失败后也保留已发送请求的预留。该本地检查不是服务端扣费上限；实际费用以 TikHub 账单为准。

`--dry-run` 不查询定价；本地快照未记录的接口显示 `estimated_cost_usd: null`。成功查询的 `--json` 输出只包含一个 JSON 文档；不带 `--output` 时结果在 `data.response`，带 `--output` 时返回文件路径且不会覆盖已有文件。

通用输出沿用项目的脱敏规则，带签名的临时媒体 URL 也会被处理，因此导出的此类链接不能直接用于下载。YouTube/Bilibili 的 `ingest` 在内部使用有效媒体地址完成音频下载。

## 实现与更新

| 文件 | 职责 |
| --- | --- |
| `src/soota/providers/tikhub/endpoint_registry.json` | 随包交付的接口、版本、查询参数与请求体 Schema 快照 |
| `src/soota/providers/tikhub/registry.py` | 编译公开契约、归并版本、解析稳定标识与分类能力 |
| `scripts/update_tikhub_registry.py` | 从已下载的 OpenAPI 重建注册表 |
| `src/soota/providers/tikhub/client.py` | 统一 TikHub SDK 请求、实时定价与平台标准化入口 |
| `src/soota/api.py` | 接口发现、请求校验、单次预算、脱敏和 JSON 导出 |
| `src/soota/api_cli.py` | `api list/show/call` 命令参数 |
| `src/soota/cli_output.py` | 根命令与 API 命令共用的结果、错误与退出码输出 |

更新流程：

```bash
curl --fail --location --user-agent 'Soota/0.1.0' \
  https://api.tikhub.io/openapi.json --output /tmp/soota-openapi.json
uv run python scripts/update_tikhub_registry.py \
  --spec /tmp/soota-openapi.json --as-of YYYY-MM-DD
uv run ruff check .
uv run ruff format --check .
uv run mypy src/soota
uv run pytest
```

提交前审查注册表 diff，特别是操作更名、必填参数、资源版本和废弃标记。注册表负责选择新路径，已有 `ingest/catalog` 适配器的请求参数与响应标准化仍需同步检查；仅刷新快照不能证明新版响应兼容。

当前保持 TikHub SDK `2.1.2`，使用其 REST 请求能力，不需要逐平台复制适配器。请求通过 JSON Schema 校验；约束解释依照 [python-jsonschema 官方文档](https://python-jsonschema.readthedocs.io/en/stable/validate/)。所有当前请求 Schema 已进行语法及引用校验。

## 验证边界

本轮全量测试 113 项通过，覆盖版本数字排序、旧路径升级、新版参数、JSON 请求形态、凭据脱敏、SDK 传输、预算拒绝及原有摄取/目录流程。测试采用真实 SDK 和模拟 HTTP，未执行付费内容接口验收；已联网核对的是公开 OpenAPI 和免费定价元数据。

Ruff、格式与 mypy 检查通过；从原始 OpenAPI 重建的注册表与随包文件一致。构建 wheel 并在独立虚拟环境安装后，8 项 CLI 检查通过，确认注册表加载、平台/接口发现、新版参数校验、POST 预演、目录预演及任务列表可用。发现与预演不创建应用状态；旧有 `jobs list` 会初始化空任务目录。
