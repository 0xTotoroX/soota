# Monid 与来源优先级

Monid 是可选的托管 API Provider，默认停用。Soota 直接调用 Monid HTTP API，不依赖其 CLI。YouTube 标准字幕使用 Monid → Apify 的单视频字幕工具；Bilibili 字幕、双平台元数据、目录与音频复用 Monid → TikHub 的已适配接口。其他平台通过 `api call` 或原生任务取得已登记的读取接口数据，不因此获得通用字幕或文章转换能力。

## 完全通过 CLI 配置

在源码目录使用 `uv run soota`；以下示例中的 `soota` 也可替换为该前缀。

```bash
soota providers auth monid             # 隐藏输入 API Key
soota providers enable monid
soota providers show
soota providers priority show youtube captions
soota providers priority show bilibili captions --refresh

# 字幕按公布的整条请求链费用排序
soota providers priority set youtube captions --mode cost

# 指定平台、能力的顺序；未列出的可用来源排在后面
soota providers priority set youtube captions --mode manual --order monid,yt-dlp,opencli,tikhub
soota providers priority set bilibili captions --mode manual --order opencli,yt-dlp,tikhub,monid

# 恢复报价排序；通配规则用于没有单独配置的平台
soota providers priority set youtube captions --mode cost
soota providers priority set '*' api --mode cost

# 单次强制选择优先于已保存规则
soota ingest 'YouTube 视频 URL' --provider monid --transcript captions-only --max-cost 0.005 --json
soota providers disable monid
```

密钥也可使用 `MONID_API_KEY`，或 `providers auth monid --key-file /path/to/key` 从已有文件读取。环境变量优先；文件保存到配置目录 `credentials/monid.key`，权限为 `0600`。凭据不写入项目或输出。路由配置保存在配置目录的 `providers.json`，覆盖 `config.toml` 中对应的 routing/monid 设置；启停与优先级设置不改变 `policy` 的付费确认方式。

能力名为 `inspect`、`captions`、`catalog`、`api`、`audio`。`cost` 使用已知的美元基准报价，从低到高排序；`--order` 在 cost 模式仅决定同价顺序。`manual` 先使用指定顺序；停用、未覆盖、字幕价格未知或整条链超过预算的来源不会被自动选择。免费来源默认排在付费来源前；同价默认 TikHub 在 Monid 前。自动排序不会为了更便宜而降级到旧 API 版本。

音频默认仍为 yt-dlp；`--audio-provider auto` 才按平台的 `audio` 规则选择转写音频。统一任务仅请求音频且 `--provider auto` 时也使用 audio 规则。Downie 仅支持显式选择，不进入自动排序。显式 Provider 不会因失败自动改用其他收费来源。

## 按能力比较费用

本表比较 TikHub 与 Monid；已启用 treg 时也参与同一规则，见 [treg 能力与价格基准](treg.md)。

内置报价快照日期为 2026-10-12，来源是官方接口目录及报价元数据。以下为无缓存的一次请求链预估，不含重试、音频转写、折扣或免费额度抵扣。价格不保证长期不变。

| 能力 | TikHub 直连 | Monid | 默认付费来源顺序 |
| --- | ---: | ---: | --- |
| YouTube 字幕 | $0.001 元数据 + $0.008 轨道 + $0.008 正文，约 $0.017 | Apify 单视频约 $0.005，包含元数据与字幕 | Monid → TikHub |
| Bilibili 字幕 | $0.001 元数据 + $0.001 字幕信息，约 $0.002 | TikHub 转发约 $0.002 | TikHub → Monid |
| YouTube / Bilibili 单视频元数据 | 约 $0.001 | TikHub 转发约 $0.001 | TikHub → Monid |
| 目录与其他原生 API | 按具体路径报价 | 仅比较 Monid 已覆盖的同一路径 | 同价 TikHub 优先 |

字幕接口可能直接返回正文，或查询一次就确认无字幕，因此实付可能低于完整链预估。Monid/Apify 的空字幕错误对象也可能计为一个结果：`COMPLETED` 与 HTTP 200 不代表取得可用字幕，更不代表免费。Soota 记录上游 `runId`、实际 Provider、供应商、预估和返回的实际费用；没有账单依据的实际费用保持未知。

`providers priority show PLATFORM CAPABILITY --refresh` 免费查询报价并保存到 `provider-quotes.json`。执行中的自动排序和每次收费提交共用有时效的报价，默认 24 小时，过期或缺失时自动刷新；同一任务的阶段和并行条目复用刷新结果。刷新失败使旧值失效，该候选不自动派发。普通查看与 dry-run 可使用本地快照或旧版 `provider-prices.json` 估价，但后者不能授权实际收费。TikHub 标准采集和原生 API、Monid、treg 均使用这套缓存，详见 [报价缓存](treg.md#报价缓存)。

```bash
soota providers priority show xiaohongshu api \
  --endpoint xiaohongshu.web.fetch_note_detail --refresh --json
soota api call bilibili.web.fetch_one_video --provider monid \
  --param 'url=https://www.bilibili.com/video/BV1M1421t7hT' --max-cost 0.001 --json
```

接口名以 `api list` / `api show` 为准。`api call` 默认 `auto`，按平台的 api 规则比较同一接口；`--provider tikhub` 保留直连，`--provider monid` 强制转发。Monid 不覆盖全部 TikHub 路径，不能把另一平台或另一接口的价格用于排序。`capabilities --provider monid` 显示本地已登记的接入，动态发现和模拟契约验证不等于逐接口真实验收。

原生任务可以使用 `monid.<TikHub 接口 key>`；既有 `tikhub.<key>` 在 `--provider auto` 下也接受平台的 api 规则。需要强制直连时同时指定 `--provider tikhub`。输出来源保留实际的 Monid/TikHub 身份，不将转发冒充直连。

## 预算与恢复

`--max-cost 0` 禁止收费接口。Monid 和 TikHub 共用每次运行的剩余费用额度；统一任务的费用预留在提交前持久化，恢复继续使用原账本。`ask` 仍逐次确认，`auto` 仍遵守已配置的余额门槛。

Monid 异步任务保存 `runId`，轮询超时后再次执行相同输入会查询原任务，不自动重新收费提交。YouTube 字幕完成后可复用原回执，转换成功的字幕另由统一转换层缓存。若提交请求已发送但响应丢失，无法知道 `runId`，Soota 会保留“提交结果未知”并阻止重复提交；需在 Monid 运行记录中核实后处理本地回执，不自动删除记录或重新收费。

明确无字幕时结束整个字幕 Provider 链；`auto` 才继续音频和本地转写。普通空响应、身份不匹配和格式错误不会冒充无字幕。字幕转换保留上游提供的正文和时间轴，不能恢复上游漏掉的片段，价格也不代表完整性保证。
