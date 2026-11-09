# 目录采集与续跑

X 用户主页当前使用 OpenCLI 最近快照（最多 200 条）；opencli-mcp 默认停用；与下文 TikHub 分页语义不同。`--max-cost 0 --incremental` 可保存并去重，`completeness=unknown`，不能 resume 或证明历史全量。详见 [终端教程](terminal-tutorial.md) 与 [Provider 边界](providers.md)。

目录快照收集视频、帖子或微信文章链接与已有元数据。微信合集与全号目录见下文。目录命令不执行分类或批量字幕/ASR，也不支持 YouTube 播放列表。

## 命令与范围

- `catalog URL --dry-run`、`estimate URL --mode catalog` 完全离线，只根据 URL 和受控价格快照预估，不创建缓存或任务。
- `catalog URL --max-cost USD` 新建独立任务和输出快照；默认最多 100 条、10 页。YouTube 页面大小由接口决定，Bilibili 页大小在创建任务时固定为 `min(100, max_items)`。
- `jobs list` 查看任务 ID；`jobs resume ID` 从已保存状态继续。可提高 `--max-items`、`--max-pages`、`--max-cost`，参数表示整个任务的新总上限。单条视频的旧任务不支持续跑。
- `catalog URL --incremental --max-cost USD` 新建增量快照；相同规范来源下的历史任务记录提供已收录 ID，遇到整页已收录的视频后停止。

增量快照不覆盖旧快照，也不刷新已有视频的标题等元数据。该停止规则依赖页面大体按新到旧排列；置顶旧视频不会阻止同页新视频，但历史内容大幅重排时可能漏掉后面的新增条目。需要完整扫描时，不加 `--incremental` 并设置足够范围。

## 状态与恢复

每页响应先保存到任务记录，再按条数上限消费。中途触发条数上限时保留该页剩余视频，续跑无需重取这一页；页面内及跨页重复的视频只收录一次。Bilibili 续跑不改变页大小，避免页码偏移。

任务记录位于配置的 `data_dir/jobs`，通过临时文件与原子替换写入；macOS/Linux 文件锁阻止同一任务同时续跑。分页游标原样保存在私有任务记录中，不进入公开目录输出。进程中断后已写入的检查点仍可用；若请求已发送但响应未保存，该页可能需要重新请求，前次预算预留仍会保留。

| 结果 | 行为 |
| --- | --- |
| `completed` / exit 0 | 到达末页或增量整页无新增；可重复 resume，零次网络请求 |
| `paused` / exit 2 | 达到条数、页数限制或用户中断；提高相应总上限后继续 |
| 预算/认证错误 | 在预算检查或认证阶段暂停；无已有内容时 exit 4，有部分目录时 exit 2 |
| Provider/契约错误 | 保留前面成功页面；无内容时返回对应错误码，有已发布部分目录时 exit 2 |
| 输出文件写入失败 | 保留任务进度，返回存储错误；修复路径后 resume 可重新发布目录 |

分页游标重复时停止请求，保留本页有效条目；需要检查接口并创建新任务，不能通过增加预算消除重复游标。底层任务存储本身不可写时，只能保证此前成功保存的检查点仍在。

## 费用

真实采集受 [费用策略](features.md) 控制，默认每次付费前确认；`--max-cost` 是可选的附加上限。每次 HTTP 请求前，根据本地快照检查整个任务的剩余额度，将预留写入任务记录及成本账本，再发送请求。目录请求关闭 SDK 自动重试，错误与中断都不自动退回预留额度；缺少密钥或下一次请求超预算时不会预留该次费用。

`reserved_cost_usd` 是基于价格快照的预算预留，`actual_cost_usd` 保持 `null`；实际账单取决于 TikHub 的当时价格、折扣和计费结果。不要把这个本地预算当成服务端账单限额。配置余额阈值后，每次付费前查询当前付费钱包，触及阈值或无法确认余额时暂停并保留任务。

YouTube `@handle`、`/c/name`、`/user/name` 首次使用时需解析频道 ID，该请求计入预算；映射缓存在本地后可复用。稳定的 `/channel/UC...` 不需要解析。新目录快照读取页面需要付费，即使之后发现全部视频已收录；同一任务中已保存的页面与未消费条目不重复请求。

## 接口证据

2026-09-12 核对了 [TikHub 公开 OpenAPI](https://api.tikhub.io/openapi.json) 和免费的 endpoint 定价元数据，未发送付费目录请求。下列三个接口快照单价均为每次 0.001 USD；每项核对日期独立记录在 `price_snapshot.json`，不刷新旧视频接口的历史日期。

| 用途 | 当前 endpoint 与契约 |
| --- | --- |
| YouTube 别名解析 | `/api/v1/youtube/web_v2/get_channel_id`：`channel_url` → `data.channel_id` |
| YouTube 频道分页 | `/api/v1/youtube/web_v2/get_channel_videos`：`channel_id`、`continuation_token`、`need_format=true`；读取 `data.videos` 和下一页游标，`data.channel` 只在首页出现 |
| Bilibili 投稿分页 | `/api/v1/bilibili/web/fetch_user_post_videos_v2`：`uid`、`pn`、`ps`；读取 `data.archives` 与 `data.page.total`，页码无旧接口的 100 页限制 |

旧的 YouTube `/api/v1/youtube/web/get_channel_videos_v2` 未出现在当前 OpenAPI 中；本实现使用上述 `web_v2` 路径。YouTube 相对发布时间保存在 `extras.published_label`，不推算虚构的精确日期。

这些路径与单条视频、通用 API 查询共用[接口注册表](tikhub-api.md)，同功能优先最高未废弃版本。其他平台虽已开放通用列表查询，仍需单独适配响应与分页，才能进入本页描述的可续跑目录流程。

两个目录列表接口不接受免费额度；频道 ID 解析接受免费额度。费用预估仍按原价计算，不假设账户折扣。

## 实现与验证

| 文件 | 职责 |
| --- | --- |
| `src/soota/catalog.py` | 参数校验、离线预估、累计预算、分页消费、增量去重与任务续跑 |
| `src/soota/models.py` | 平台无关的 `CatalogPage` 和持久化 `CatalogJob` 数据契约 |
| `src/soota/providers/tikhub/catalog.py` | TikHub 目录参数与响应标准化，严格校验视频 ID、分页字段和来源 |
| `src/soota/providers/tikhub/client.py` | 频道别名缓存、目录 HTTP 调用和发送前预算回调 |
| `src/soota/storage/jobs.py` | 兼容旧任务的读写、列表和互斥文件锁 |
| `src/soota/render/catalog.py` | 生成 `catalogs/{job_id}/catalog.json` 与 `catalog.md` |

`tests/unit/test_catalog.py` 覆盖预算、页内剩余条目、错误、恢复、增量与锁；`tests/contract/test_catalog_fixtures.py` 校验按官方契约构造的脱敏样例；`tests/contract/test_catalog_cli.py` 使用真实 TikHub SDK 与 `httpx.MockTransport` 验证 CLI、请求参数、HTTP 错误和关闭自动重试的效果。

这些测试不等于真实目录验收。真实目录请求需要授权的来源和费用设置；模拟测试不能证明实际分页、余额扣费及输出均可用。

## 微信公众号

公开合集使用匿名 HTTP，不需要 Cookie、登录或浏览器窗口。全号历史使用已登记的 TikHub `wechat_mp.fetch_account_articles`；两种目录都复用标准 `catalog.json`、分页限制、增量检查点与恢复机制。

```sh
soota catalog 'https://mp.weixin.qq.com/mp/appmsgalbum?__biz=BIZ&album_id=ALBUM_ID' --max-cost 0 --max-items 10000 --max-pages 1000 --json
soota catalog gh_example --provider tikhub --max-cost 0.10 --max-items 1000 --max-pages 10 --json
soota jobs resume JOB_ID --max-items 10000 --max-pages 1000 --json
```

合集请求 `action=getalbum&f=json`，以最后一项的 `msgid/itemidx` 作为下一页 `begin_msgid/begin_itemidx`。`continue_flag=0` 才表示目录到末页；空响应、重复游标、业务错误和验证页都不能冒充完成。条目 `extras` 保留 `pos_num/msgid/itemidx/create_time/album_id/__biz/sn`，有声明总数时输出 `total_items`。完整合集不代表全号历史完整。

全号输入接受 `gh_xxx`，规范来源为 `https://mp.weixin.qq.com/mp/profile_ext?username=gh_xxx`（账号定位标识，不承诺该地址有可匿名读取的主页）。使用 POST body 的 `username/page_size/offset/raw=false`，以返回的 `next_offset` 续页、`is_end` 判定结束，保留 `extras.username`。`page_size` 不保证上游固定返回条数，不用少于一页来推断结束。付款确认、报价时效和请求前持久预留继续生效；`--max-cost 0` 不发送计费请求。当前全号标准目录只绑定 TikHub，不自动切换中转来源。

文章稳定身份按 `__biz + mid + idx` 去重，保留 `sn` 访问参数；分享场景参数或 sn 的变化不制造另一篇文章。到达 `max_items/max_pages` 时仍是暂停状态，必须续跑至 `stop_reason=end` 才能说该目录已完整枚举。编号连续性、跨快照对账和文件/删除回执由 LoreLab tracking 处理，Soota 不维护个人名单。

请求参数与全号响应契约依据 [TikHub SDK 的 OpenAPI](https://github.com/TikHub/TikHub-API-Python-SDK/blob/main/spec/openapi.json)；公开合集可参照 [RSSHub 的公开合集实现](https://github.com/DIYgod/RSSHub/blob/master/lib/routes/wechat/msgalbum.ts)。自动化测试使用合成页面和模拟响应，不代表已完成真实微信和付费接口验收。
