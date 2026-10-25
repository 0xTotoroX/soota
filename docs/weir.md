# Weir 正文与免费额度

Weir 是可选的 X / Reddit 正文来源，直接使用官方 REST API，无需 MCP。当前接入 X 单帖、分享帖承载的 X Article 全文，以及 Reddit 单帖 `selftext`。使用 `ingest URL --provider weir --want context`；`context,markdown,data` 可组合。暂不接搜索、时间线、线程、评论分页、媒体下载、原生 `--operation` 或单独 `--want data`。

X 输入必须是分享帖的 `/status/ID` 链接；`/i/article/ID` 的 Article ID 不是帖子 ID，不能替换使用。单帖已有完整 Article 时直接复用，只有 Article 存在但全文缺失时再请求 Article 接口，并核对 `postId` 和 Article ID。转帖中的 Article 同样读取全文，缺全文时不能只保存转发链接。不会把 `previewText` 当成全文。Reddit 支持帖子链接与 `redd.it/ID`，评论定位链接拒绝；空正文的链接帖、被删除的正文均不算成功。正文保持原文顺序，不做摘要或语义改写。

## 配置

```sh
soota providers auth weir                    # 隐藏输入 Key；也可从 WEIR_API_KEY 读取
soota providers enable weir
soota providers limits weir --monthly-credits 4000 --task-credits 100 --json
soota providers show --json
soota providers usage weir --json
soota ingest "https://x.com/USERNAME/status/POST_ID" --provider weir --want context --json
soota ingest "https://www.reddit.com/r/COMMUNITY/comments/POST_ID/TITLE/" --provider weir --want context,data --json
soota ingest --inputs-file links.txt --provider weir --want context --weir-max-credits 50 --json
soota jobs resume JOB_ID --weir-max-credits 100 --json
```

链接、4000/100/50 均为占位或示例，不是预置的个人设置。默认 `enabled=false`、`free_only=true`，月度与任务上限均未另设，仅受可用免费余额约束。`providers limits weir` 不带修改参数时显示配置；0 阻止新的 credits 请求，`--clear-monthly` / `--clear-task` 清除相应自定义上限，仍不允许付费使用。配置只在用户目录保存；认证文件权限为 0600，命令结果不输出 Key。

也可在用户 `config.toml` 的 `[weir]` 中配置 `enabled`、`monthly_credits`、`task_credits`、`timeout_seconds`（默认 60）和 `cache_seconds`（默认 300）。CLI 写入的 `providers.json` 对同名字段优先。当前 `free_only` 只接受 true，尚不支持购买额度消费；`policy set --mode auto` 不改变这个边界。

`--weir-max-credits` 是整个任务的累计上限，优先于创建任务时的默认值；任务请求保存这个值。恢复不清零，显式覆盖表示新的总上限，不是额外赠送额度。月度上限每次派发读取当前配置，并跨本机所有任务和 Key 累计。旧任务未保存任务额度时沿用当前默认值。`--max-cost` 仍按 USD 管理其他来源；为兼容原有“禁止所有计费请求”的约定，`--max-cost 0` 也禁止新的 Weir credits 请求，但有效缓存和已验证产物可以复用。

## 免费模式与账本

官方目前提供每月 5,000 免费 credits，先消费免费余额，再消费购买余额；数据接口没有已核实的服务端 free-only 开关。因此每次数据请求前免费查询 `/v1/usage`，要求 free tier、`balance == freeGrant.remaining`、有效且未过期的重置时间，并在扣除在途预留后仍有足够余额。无法确认、没有免费 grant、付费等级、购买余额、余额不足或暂停消费时停止。

**此模式适用于不充值的免费 workspace。** 运行期间不要从其他客户端充值；本地余额查询与远端扣费不是一个原子事务，无法拦截并发充值。其他工具共享免费余额时可能先耗尽，需由服务端拒绝；本地配额只能约束共享同一个 Soota data 目录的调用，不控制其他设备、应用或 Weir 监控任务。

按官方单结果费率，X 单帖或单次 Article 请求预留 2 credits，Reddit 单帖预留 4 credits；补取 Article 是另一请求、再次预留。公开文档未明确嵌套引用对象的额外计费规则，以上是单项结果的契约估算，不是服务端单请求消费锁。按响应 `x-credits-charged` 结算；若实际超过预留，记录实际值并锁住后续 Weir 调用，报告需要核对账单与契约，不能宣称上游变价时仍有绝对硬上限。

`data_dir/billing/weir/credits.json` 在文件锁内持久化请求前预留、服务端 reset 周期、任务身份和实际扣费。月度预算按服务端 `resetsAt` 分组，任务累计跨月保留。共享锁覆盖余额检查到结算，防止本地并发透支。成功按实际回执核账；明确 HTTP 400 及以上按官方规则不收费。客户端超时、中断、未知状态或缺失扣费头仍保留最大预留，不自动重试或清零。账本损坏停止，不创建空账本绕过限制。不要删除账本来“恢复额度”。

`providers usage weir` 离线显示各周期用量、已核实扣费与未决预留，不查询远端、也不代表整个 workspace 的账单。任务结果中的 `weir_credits` 只统计本任务。成功获取的分享帖在任务目录保存独立原始响应检查点；Article 补取失败后，恢复只接续缺失步骤，不重新扣分享帖费用。检查点不代表正文已完成。缓存先于余额查询，按 Key 的散列隔离；缓存只保存已核对目标 ID 与正文的结果，不把过期缓存续期。显式禁用来源后不能再次调用它。

## 路由与验证边界

`auto` 仍先选择已有 OpenCLI 正文能力。仅在派发前发现运行时/能力不可用时，才考虑已启用、输入匹配的 Weir；OpenCLI 已派发后的登录、限流、契约或其他失败不自动改用 Weir。`--provider weir` 严格选择它，不触发其他付费 Provider。

能力清单标记的是标准正文绑定，不是在线验收。回归使用合成数据和 HTTP MockTransport，覆盖正文、Article 身份、月度/任务共享配额、并发、缓存、超时、失败退款、恢复及配置。真实账户的可用性、实际价格及完整性仍需使用者按需小额验收。

官方依据：[计费](https://docs.weirapp.io/billing)、[余额](https://docs.weirapp.io/reference/account/getUsage)、[X 单帖](https://docs.weirapp.io/reference/x/getXPost)、[Article](https://docs.weirapp.io/reference/x/getXArticle)、[Reddit 单帖](https://docs.weirapp.io/reference/reddit/getRedditPost)。
