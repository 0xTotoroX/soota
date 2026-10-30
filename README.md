# Soota

给 Soota 媒体、网页、本地资料或 Cubox 收藏，采集所需的源文本、媒体和内容包。

源码和配套 Skill 统一在 LoreLab 的 `modules/soota/` 开发，独立 Soota 仓库由同步流程生成，保留原有提交历史。使用 Soota 不需要安装 LoreLab；独立安装、升级与开发入口见[维护说明](docs/maintenance.md)。

Soota 是本地优先的内容采集与解析 CLI。`ingest --want` 将视频、音频、字幕、文章正文、图片和数据接入同一套任务与内容包；输入清单和集合复用单项流程，任务保留预算、已完成产物和恢复点。默认保存原文 context。需要保存分析、笔记或阅读文章时，由 Agent 写作并提交给 LoreLab content，Soota 不生成这些语义产物。

YouTube/Bilibili 字幕按平台和能力的费用规则选择来源，支持手动优先级；Monid 和 treg 为可选付费 Provider，免费来源默认优先。确认无字幕时按字幕模式选择本地转写。其他正文和媒体使用已绑定的原生工具或共享媒体处理器。MCP 当前默认停用。动态发现某个命令，不代表它已经接入所有标准产物或通过当前网站验收。

常用组合、输入格式和恢复方法见 [统一内容任务](docs/tasks.md)，语义分析、笔记、文章、大纲、整包导出及知识学习交付由 LoreLab 负责，见[工作流迁移说明](docs/knowledge.md)，高层职责见 [处理流程](docs/providers.md#处理流程与职责)。既有 `inspect`、`catalog`、`features`、`api` 和运行时管理入口继续保留，详见 [功能与费用策略](docs/features.md)、[终端教程](docs/terminal-tutorial.md) 和 [TikHub 接口说明](docs/tikhub-api.md)。

## 安装

需要 Python 3.12+。媒体处理和验证需要 ffmpeg、ffprobe；本地转写支持 `whisper` CLI、`faster-whisper` 和 Apple Silicon 上的 `mlx_whisper` CLI，默认模型为 `base`。MLX 的安装与配置见[本地转写引擎](docs/terminal-tutorial.md#本地转写引擎)。纯 API 查询不需要这些音频工具。

Windows 可运行核心 CLI、任务恢复、预算与运行时管理；文本文件和子进程协议统一使用 UTF-8。Downie、macOS Keychain 和 MLX 仍限对应 Mac 环境，Windows 的 TikHub 凭据使用 `TIKHUB_API_KEY`。当前 Windows 无法为外部媒体工具设置与 Unix `RLIMIT_FSIZE` 等价的硬文件上限，因此 yt-dlp 媒体下载、FFmpeg 提取音轨与合轨会在派发前明确停止；已有媒体验证、受字节上限保护的 HTTP 文件下载及字幕采集不因此停用。不会用下载器提示性上限替代硬限制。具体边界见 [Windows 运行](docs/providers.md#windows-运行)。

```bash
cd /path/to/soota
uv sync --extra dev
uv run soota doctor --json
```

OpenCLI 需要 Node.js、Chrome 和 Browser Bridge 扩展。可用 `uv run soota runtime update --json` 安装最新稳定运行时，再用 `uv run soota runtime connect --json` 启动所选版本并等待扩展连接。无需安装 opencli-mcp。

`yt-dlp` Provider 需要 PATH 中可用的 `yt-dlp`。默认读取 Chrome 登录态；`SOOTA_YTDLP_COOKIES_BROWSER` 可指定 yt-dlp 浏览器/profile，设为空可关闭 Cookie 读取，`SOOTA_YTDLP_BINARY` 可指定程序路径。`captions-only` 只下载字幕；`auto` 确认无字幕时会下载音频并本地转写。可显式使用 `soota ingest "视频 URL" --provider yt-dlp --transcript captions-only --max-cost 0 --json`，支持 YouTube 和 B 站。yt-dlp 由原安装工具维护，不在三个外部 Provider 的自动更新范围内。

音频来源默认是 yt-dlp，可用 `--audio-provider downie` 明确选择本机 Downie，或 `--audio-provider tikhub` 选择付费音频接口。Downie 的一次性完成回调设置及当前 CLI 流程图见 [Provider 路由](docs/providers.md#路由和费用)。

TikHub API Key 只从环境变量或 macOS Keychain 读取：

```bash
export TIKHUB_API_KEY="..."   # 不要把 Key 发给聊天或写入 Git
# 或：
security add-generic-password -s soota.tikhub -a api_key -w
```

## 可选 Monid、treg 和优先级

```bash
uv run soota providers auth monid
uv run soota providers enable monid
uv run soota providers auth treg
uv run soota providers enable treg
uv run soota providers priority show youtube captions
uv run soota providers priority set youtube captions --mode cost
uv run soota providers priority set bilibili captions --mode manual --order opencli,yt-dlp,tikhub,monid
```

启停、凭据和优先级可在 CLI 配置。按平台分别设置 `inspect/captions/catalog/api/audio`；显式 `--provider monid` 或 `--provider tikhub` 优先于规则。启用 Monid 或 treg 不关闭付费确认。YouTube 字幕通过 Monid/Apify，B站字幕和已覆盖的原生接口通过 Monid/TikHub；按公布的整条链费用排序，价格未知时不自动派发。查看与免费刷新报价、密钥配置及恢复边界见 [Monid 与来源优先级](docs/monid.md)。

treg 的 YouTube 字幕默认使用 Crawl4AI，可在配置中选择 ScrapeCreators；双平台元数据、音频和 B站字幕复用已适配的 TikHub 转接，另支持 YouTube 频道目录。执行时自动检查报价有效期（默认 24 小时），过期或缺失时刷新；任务内各阶段和并行条目共用缓存。普通报价查看与 dry-run 使用本地估价；执行前刷新失败会排除该候选。配置和验证边界见 [treg 与报价缓存](docs/treg.md)。

## 可选 Weir 免费正文来源

Weir 支持 X 帖子／Article 和 Reddit 帖子正文，默认停用、只允许免费 workspace。月度和任务 credits 上限可分别配置，恢复累计不清零。

```bash
uv run soota providers auth weir
uv run soota providers auth weir --key-name backup-1
uv run soota providers keys weir --json
uv run soota providers enable weir
uv run soota providers limits weir --monthly-credits 4000 --task-credits 100
uv run soota ingest "X 或 Reddit 帖子 URL" --provider weir --want context --weir-max-credits 50 --json
uv run soota providers usage weir --json
```

数字为示例。新的数据请求按配置顺序持久轮询 Key；认证 401 时同一请求跳到下一个，成功后推进游标。月度按 UTC 自然月跨 Key 共用，任务累计不因轮转或恢复清零。余额不足、限流、上游错误和未知扣费不切 Key。未设置自定义上限时仍受免费余额约束；当前不支持购买额度消费。仅适用于不充值的免费 workspace，运行中不能从其他客户端充值。标准接口费率用于请求前预留，异常扣费会停止后续调用。配置、缓存、原文范围和预算边界见 [Weir 正文与免费额度](docs/weir.md)。

## 命令

自然语言目标由 Agent 转成采集条件，`plan create` 给出可选方案并匹配预设。用户选择后保存，下次匹配先询问是否沿用；报价只在需要时刷新，没有后台轮询。详见[目标规划与预设](docs/planning.md)。

```bash
soota plan create "视频 URL" --goal "保存原字幕" --want transcript,context --max-cost 0.05 --json
soota plan select PLAN_ID --option configured --confirm --save-as "视频原字幕" --json
soota plan run PLAN_ID --json
soota presets list --json
```

零费用快照示例：`uv run soota catalog "https://x.com/USERNAME" --max-items 50 --max-cost 0 --json`。完整性未知，不代表全部历史。

```bash
uv run soota doctor --json
uv run soota updates check --json
uv run soota platforms --json
uv run soota capabilities --search download --limit 20 --json
uv run soota api list --platform bilibili --task detail --json
uv run soota api show bilibili.web.fetch_one_video --json
uv run soota inspect "URL" --json
uv run soota estimate "URL" --mode ingest --json
uv run soota catalog "频道或 UP 主 URL" --dry-run --json
uv run soota ingest "URL" --transcript auto --max-cost 0.05 --output PATH --json
uv run soota ingest "视频 URL" --want video,audio,transcript --max-cost 0 --json
uv run soota ingest "/path/to/captions.vtt" --want context,transcript --subtitle-format srt,json --json
uv run soota ingest --inputs-file links.txt --want context --workers 2 --max-items 20 --max-cost 0 --json
uv run soota jobs list --json
uv run soota jobs resume JOB_ID --json
uv run soota jobs resume JOB_ID --workers 2 --json
```

字幕模式：`auto`、`captions-only`、`asr`、`none`。视频字幕默认免费来源优先，再按整条请求链报价选择 TikHub 或已启用的 Monid/treg。`policy set --mode auto` 只改变付费来源的授权方式，不改变来源顺序；`--provider tikhub` 可单次指定。`--max-cost 0` 禁止计费请求。

B站字幕可加 `--prefer-youtube`：匹配同名频道和同一期视频后优先使用 YouTube，并默认排除已有推广标注。`--no-prefer-youtube` 关闭跨平台检查，`--keep-promotions` 保留推广。过滤保留原字幕供追溯；无标注不代表无推广。详细使用与限制见 [字幕选择与过滤](docs/subtitle-selection.md)。

`--want` 与 `--optional` 的新采集目标限于 `video/audio/transcript/context/markdown/images/data`；只下载视频不会自动转写或总结，`markdown` 保存未加工正文。语义目标 `note/outline/analysis/article` 使用 LoreLab content，由 Agent 写作、CLI 校验。具体区别见 [目标产物](docs/tasks.md#选择目标)。

用户要求将粘贴文字存成知识时，先保存原文 TXT 并摄取为 context，再由 Agent 按目的地规则整理。位置参数中的正文会被当成分享文本解析链接，不能替代原文保存。见 [粘贴文字与知识归纳](docs/tasks.md#粘贴文字与知识归纳)。

`--json` 时 stdout 只有一个 JSON 文档；日志和警告在 stderr。统一任务的 `items` 包含逐项产物、错误和缺失目标。`completed` 表示本次范围内的必需产物完成；集合历史完整性另看 `enumeration`。

统一任务支持 `--workers 1..8`，默认 1。枚举和结果列表保持输入顺序，独立条目并行处理，同一内容包目录按顺序写入；条目失败不阻断其他条目。并发设置随任务保存，resume 可以覆盖；所有 worker 共享全批费用预留，恢复不清零。Ctrl-C 会等待在途条目收尾再保存暂停状态，可能需等 Provider 超时。

单来源语义内容、整包导出、知识文章、学习大纲和最终交付由 LoreLab 负责；Soota 采集任务保留自己的 source bundle、预算和恢复记录。

`updates check` 只读检查三个 Provider 的本机版本、npm／PyPI 最新包版本，以及上游主分支相对本机版本标签的提交关系。仓库有新提交不一定代表已有新版可安装；此命令也不会更新程序或同步备份仓库。

执行在线采集、接口调用或目录续跑前，Soota 默认检查三个 Provider 并安装 npm／PyPI 最新已发布包。可用 `soota updates apply --json` 立即检查并更新；帮助、预估和离线预览不会触发更新。首次运行需要 npm、Node.js 和 uv，并可能花几分钟安装。MCP 包即使更新，接入仍保持停用；浏览器扩展不会自动更新。更新失败会报告原因，已经安装的版本保留。

`SOOTA_AUTO_UPDATE=false` 可关闭自动更新；`config.toml` 的 `[updates] interval_seconds = 3600` 可将检查间隔改为一小时，默认 `0` 表示每次在线任务检查。固定版本或执行 `runtime rollback` 后若要继续使用该版，请关闭自动更新。安装位置、外部路径和兼容性检查见 [Provider 架构](docs/providers.md)。

## 与 LoreLab 配合

Soota 获取、解析和保存原文 context；LoreLab content 接收内容包，校验并保存 Agent 写出的分析、笔记、文章、大纲，负责渲染和整包导出。LoreLab knowledge/study 组织知识文章、学习交付与授权清理；分类查重合并由 Agent 审阅，CLI 不自动覆盖合并旧笔记。这些语义流程及提示词仅由 LoreLab 维护。

安装 LoreLab 后，按所需输出使用：

```sh
lorelab content ingest "内容 URL" --want context,note
lorelab content resume JOB --analysis /path/to/submission.json
lorelab content render --bundle /path/to/bundle --analysis /path/to/submission.json
lorelab content export --bundle /path/to/bundle --destination /path/to/archive
```

`lorelab knowledge` 整理知识、`lorelab study` 制作学习大纲。旧语义/导出 ingest、旧语义或交付任务 resume、render 以及 `soota knowledge/study` 仅作隐藏兼容转发。未安装 LoreLab 时明确报需要安装且不修改任务；已有 task/item 身份、source 证据、原路径、预算与产物保留。采集、下载和原文包可以只安装 Soota。兼容边界见[工作流迁移说明](docs/knowledge.md)。

## 多平台查询

`platforms`、`api list`、`api show` 和 `api call --dry-run` 完全离线。先查看新版参数，再执行有费用上限的查询：

```bash
uv run soota api call bilibili.web.fetch_one_video \
  --param 'url=https://www.bilibili.com/video/BV1M1421t7hT' --dry-run --json
uv run soota api call bilibili.web.fetch_one_video \
  --param 'url=https://www.bilibili.com/video/BV1M1421t7hT' \
  --max-cost 0.01 --output ./bilibili-detail.json --json
```

此标识当前选择 `fetch_one_video_v3`。新版接口即使改了参数，也按新版契约校验；例如 V3 使用 `url`，不再接受旧版 `bv_id`。复杂查询参数使用 `--params-file`，POST 请求体使用 `--body`，均从 JSON 文件读取。

通用查询覆盖详情、搜索、作品列表、评论、用户资料、热榜、媒体地址及部分分析接口，具体能力以 `api list --platform 平台 --task 类型` 为准。返回的是经过凭据脱敏的平台 JSON；不会自动分页、下载、转写或生成 Markdown。可续跑的 `catalog` 分页目前支持 YouTube、Bilibili；其他来源的标准正文和媒体能力见 [统一任务能力边界](docs/tasks.md#集合与原生操作)。

## 频道与 UP 主目录

先做完全离线的预估，再限定范围采集：

```bash
uv run soota catalog "频道或 UP 主 URL" --max-items 100 --max-pages 3 --dry-run --json
uv run soota catalog "频道或 UP 主 URL" --max-items 100 --max-pages 3 --max-cost 0.004 --json
uv run soota jobs resume JOB_ID --max-items 200 --max-pages 6 --max-cost 0.007 --json
uv run soota catalog "频道或 UP 主 URL" --incremental --max-cost 0.004 --json
```

目录采集默认逐次询问付费；可用 `policy set` 明确启用自动付费。`--max-cost`（USD）是可选的额外上限。条数、页数和费用均是整个任务的累计上限；续跑传入的是新总上限，不是追加额度。费用按受控价格快照在每次请求前预留，实际扣费以对应服务账单为准；请求失败或中断后，已预留的额度仍保留。

`--incremental` 创建新快照，跳过本机历史目录中已收录的视频；遇到整页都已收录时停止。默认上限是 100 条、10 页。目录只生成视频链接与已有元数据，不自动获取字幕、音频或摘要。使用方法、恢复边界与接口证据见 [目录采集说明](docs/catalog.md)。

## 输出与个人配置

用户配置位于 `~/.config/soota/`，输出默认位于 `~/Documents/Soota/`。可通过 `SOOTA_OUTPUT_DIR` 或 `--output` 设置采集包位置。另行导出使用 `lorelab content export --bundle PATH --destination PATH`，或 `--vault PATH --subdir DIR`；导出维持相对附件链接并保护人工修改。Soota 不负责导出、定位知识库或选择分类。个人路径、账号选择和凭据从环境变量读取，不应把实际值写入项目或共享 Skill。名单、处理规则和采集结果保存在项目外的私人文件中。若知识库使用 Git，同样需要明确排除私人目录；详见[终端教程](docs/terminal-tutorial.md#个人资料与知识库规则)。

新的统一任务使用 v2 内容包，`manifest.json` 的 `artifacts` 登记实际文件、类型、字节数、SHA-256、来源和验证信息。视频、音频、正文和字幕按实际目标保存；历史或 LoreLab 写入的分析字段与产物继续保留，未取得的产物不以空文件冒充。既有 v1 包仍可读取；原有单视频命令的输出契约保留。

`context.md` 保存给 Agent 的原文输入，保留顺序、重复和断句。取得字幕时保存 `transcript.md`、片段 JSON 及请求的字幕导出；取得文章正文时保存 `body.md` 和有序正文块；媒体进入 `attachments/`。`note.md`、`outline.md`、`analysis.md` 和阅读用 `content.md` 由 LoreLab content 在明确请求并验证 Agent 提交后生成，Soota 采集不会重写它们。文件结构和旧包处理见 [内容包与导出](docs/tasks.md#内容包与导出)。

统一字幕转换支持 SRT、VTT、TTML、SRV/XML、已知字幕 JSON、Whisper TSV/JSONL 和无时间 TXT。获取层选择一份字幕，转换层按实际格式处理；详见 [字幕格式与 context](docs/subtitle-formats.md)。显式选择 TikHub 音频后，获取失败会报告错误，不会自动改用 yt-dlp 重新获取。

同一统一任务的目录枚举和各条目共享费用上限；恢复复用满足要求的文件与检查点，不清零已预留额度。媒体校验会检查文件、解码、轨道和已知时长；来源时长未知时仍保留完整性未知，不能仅凭下载成功声称视频全长。

## 质量

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/soota
uv run pytest
```

测试默认不访问真实 TikHub。目录契约测试使用官方 OpenAPI 形状构造的样例；API 与 CLI 集成测试使用真实 SDK 和模拟 HTTP 传输，不能代替真实接口验收。接口注册表随软件打包，更新方式及实现职责见 [TikHub 接口说明](docs/tikhub-api.md)。

## 许可证

Copyright 2026 Soota contributors. 本次许可变更起，Soota 有权授权的自有代码采用 [GNU GPL 第三版](LICENSE)，固定为 `GPL-3.0-only`，不包含“或更新版本”选项。

允许免费使用和商业使用，也允许收费分发；分发 GPL 覆盖的软件及修改版本时，须按 GPL 第三版保留许可、提供对应源码并满足适用条件。这里没有商业双许可、非商业限制或额外收费条件，也不要求所有私人修改都向全世界公开。

此前已按 Apache-2.0 授权的版本继续保留原授权；本次声明只适用于当前及后续有权授权的版本，不追溯撤回历史授权。原许可证全文保留在 [历史 Apache-2.0 许可](LICENSES/soota-historical-Apache-2.0-LICENSE)。第三方代码、依赖与采集的数据保留各自许可、版权及使用条件，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。TikHub 2.1.2 的许可冲突仍未澄清；可选 SponsorBlock 标注数据的 CC BY-NC-SA 4.0 条件独立于 Soota 代码，不能据此推定数据允许商业使用。
