# treg 与报价缓存

treg 是可选的托管 API 来源，默认停用。Soota 直接使用 HTTP，不需要安装 treg CLI，也不会自动执行目录搜索结果。启用后沿用平台和能力的 cost/manual 顺序、付费确认、余额门槛与任务累计预算。

```bash
soota providers auth treg
soota providers enable treg
soota providers priority show youtube captions --refresh --json
soota ingest 'YouTube 视频 URL' --provider treg --transcript captions-only --max-cost 0.01 --json
soota providers disable treg
```

Token 使用隐藏输入或 `--key-file /path/to/token`，保存到配置目录 `credentials/treg.key`（权限 0600）；`TREG_TOKEN` 优先于文件。使用自己的组织 Token，不把凭据放入项目。启用不会改变 `policy` 的付费方式。

`config.toml` 可设置：

```toml
[routing]
price_ttl_seconds = 86400

[treg]
youtube_captions = "crawl4ai" # 或 "scrapecreators"
timeout_seconds = 90
```

`providers.json` 中已保存的字段优先于 `config.toml`；若 CLI 已保存上述配置，修改配置目录的 `providers.json` 对应字段。凭据不保存到这两个配置文件。

## 支持范围

| 能力 | 路线 | 2026-10-28 公开目录基准估价 |
| --- | --- | ---: |
| YouTube 字幕 | 默认 Crawl4AI，一次请求提供字幕及可用元数据 | $0.00165 |
| YouTube 字幕 | 可配置 ScrapeCreators，一次请求 | $0.00188 |
| YouTube / Bilibili 单视频元数据 | 已登记的 TikHub 路径 | $0.001 |
| Bilibili 字幕 | TikHub 元数据 + 字幕信息 | 约 $0.002 |
| YouTube 音频地址 | TikHub streams v2 | $0.003 |
| Bilibili 音频地址 | TikHub 元数据 + playurl | 约 $0.002 |
| YouTube 频道目录 | TikHub 频道视频；别名可能另需一次解析 | $0.001/次 |

价格是无缓存的估计，不保证结算不变；在线执行按有效报价重新排序。来源为 [treg 公开目录](https://treg.to/catalog)；Crawl4AI 与 ScrapeCreators 使用各自的固定单次接口。Bilibili 目录 v2 尚无匹配的已核实 treg 接口，当前不支持，不替换成旧版。treg 不覆盖全部 TikHub API；已登记原生接口可使用 `api call ... --provider treg` 或 `treg.<TikHub key>`，不开放任意 treg endpoint。

字幕正文由统一转换层解析，校验视频 ID 和时间轴后才进入字幕缓存。Crawl4AI 保留实际语言和自动字幕标志；ScrapeCreators 的人类语言名称不冒充语言代码，来源类型保持未知。空结果可能是指定语言不匹配，记为 unknown；受阻、部分响应、身份不符或格式错误不会冒充无字幕。

## 报价缓存

TikHub、Monid 和 treg 共用 `provider-quotes.json`，按 Provider、服务地址、凭据和接口隔离。文件只存凭据的哈希命名空间，不存 Token。默认 TTL 为 24 小时，可通过 `price_ttl_seconds` 设置正整数秒。

- 在线自动排序和收费派发前检查有效期。首次或过期时查询免费报价；仍有效则复用。
- 同一任务的阶段及并行 worker 共用内存结果；跨进程使用文件锁，避免同时刷新同一个过期条目。长任务中再次过期仍会刷新。
- `providers priority show ... --refresh` 强制刷新；同一查看过程的排序不重复查询。刷新失败会使旧条目失效，并在该任务内复用失败状态，避免反复探测。
- 普通查看和 dry-run 不因报价缓存联网，可展示快照估价；旧 `provider-prices.json` 仅用于离线估计。实际执行不凭旧快照或未知价格收费。
- 报价缓存不是费用授权或预算余额。每一次收费请求仍经过 PaymentGate 和任务账本，恢复不会重置预留。

## 预算与回执

发送前保存预算预留及幂等键，响应记录 `X-Treg-Call-Id` 和可用的实际费用。余额门槛通过 `/auth/me` 和组织余额查询；`balance_micro` 已扣除 holds，不二次扣减。treg 的余额查询可能检查账户原有的自动充值设置，Soota 不修改该设置。

同一任务恢复可复用完成回执，无需再次收费。超时、202、409、410、非完成响应或提交结果不明时保留回执和费用预留，当前停止并要求核查原 call id；不自动重发、轮询或切换收费来源。服务端幂等键有时效，不能据此假设永久防重复。没有实际费用头时金额保持未知，不将 HTTP 200 视为免费。

Soota 发送 `X-Treg-Route-Max-Cost` 并仅接受固定单次价格；这项服务端限制主要约束预留，并非最终结算的绝对保证。若已知实付超过预留，当前调用报错并保留回执，原任务持久暂停后续收费；恢复或提高上限也不会自动解除，须先核对结算差异。已经发出的并行请求仍可能完成。不会将其他上游 credits 余额当成用户 treg 余额。

接入依据为官方公开接口与模拟 HTTP 契约测试；没有使用用户 Token 做真实收费采集验收。目录中的可用标志和本地测试通过不代表每个站点、语言、账号都能取得完整字幕。
