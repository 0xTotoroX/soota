# treg Provider

父级：[项目规则](../../../../AGENTS.md)。配置与限制见 `docs/treg.md`，处理职责见 `docs/providers.md`。

- `catalog.py`：已适配 endpoint 身份、HTTP 方法、上游路径和离线基准报价。未核实接口不得推测映射。
- `client.py`：共享报价缓存、HTTP、余额与费用授权、持久回执；未决请求不能自动重发。
- `provider.py`：YouTube Crawl4AI/ScrapeCreators 字幕获取和已登记 TikHub 契约转接。
- `__init__.py`：模块声明。

正文解析集中在 `transcripts/formats/`。空字幕不证明没有轨道；身份、实际语言及来源类型须按响应保留，验证成功后才缓存字幕。
