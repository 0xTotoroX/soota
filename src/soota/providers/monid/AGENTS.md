# Monid Provider

父级：[项目规则](../../../../AGENTS.md)。高层职责见 `docs/providers.md`，配置与限制见 `docs/monid.md`。

- `client.py`：HTTP、共享时效报价、费用预留与持久化 run 回执；恢复不能再次提交已经接受的请求。
- `provider.py`：YouTube/Apify 单视频字幕获取及 TikHub 合同转发；只返回原始正文和获取状态。
- `prices.json`：公开报价及路径覆盖快照，不含账号、密钥或个人使用记录。
- `__init__.py`：模块声明。

字幕解析复用 `transcripts/formats/`，缓存由获取与转换编排在验证成功后写入。任务预算和已完成回执跨进程保留；未确认提交结果时停止，不能自动删除回执重试。
