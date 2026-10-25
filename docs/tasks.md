# 统一内容任务

`ingest` 接受链接、本地文件、输入清单和已有内容包，通过 `--want` 选择目标产物。每个条目使用相同的获取、处理和保存流程；同一批任务共享费用上限，并保留逐项进度。

默认获取供 Agent 使用的原文 context，不润色、合段、总结或生成文章。没有 `--want` 的原有 YouTube/Bilibili 单视频命令保留旧输出契约；显式目标及新输入使用 v2 内容包。高层职责图见 [处理流程与职责](providers.md#处理流程与职责)。

Windows 的任务文件、缓存和恢复输入按 UTF-8 读写，任务锁与共享预算使用内核互斥，恢复不会清除预留。需要外部 yt-dlp 下载或 FFmpeg 提取／合轨的媒体目标，因无法保证硬文件字节上限而明确停止；不会将必需目标降为可选，也不会回退为无上限调用。已有媒体、字幕和直接 HTTP 文件的边界见 [Windows 运行](providers.md#windows-运行)。

## 选择入口

| 目的 | 入口 | 契约 |
| --- | --- | --- |
| 采集原文、媒体或数据 | `soota ingest`，以 `soota jobs resume` 接续 | 本文 |
| 单来源分析、笔记、文章、大纲、渲染或整包导出 | 外部 `lorelab content` | [迁移说明](knowledge.md) |
| 从 Cubox 收藏或已有内容包整理多源文章、写入并核对 | 外部 `lorelab knowledge` | [迁移说明](knowledge.md) |
| 从明确选定的已有文章制作学习大纲 | 外部 `lorelab study` | [迁移说明](knowledge.md) |

知识任务和学习任务是显式请求的流程，不修改普通 ingest 默认停在原文 context 的行为。`note/article/analysis/outline` 及目的地导出由 LoreLab content 实现；LoreLab 的 `knowledge/study` 管理知识文章、学习与交付回读。分类、查重和合并由 Agent 审阅，publish 不自动覆盖合并旧笔记。内容判断和写作由 Agent 完成，程序只做确定性处理与核验。

## 选择目标

以下命令中的 URL、路径和任务编号均为占位值，执行前替换为实际输入。

```bash
# 只保存视频；不会因为视频目标而自动转写或总结
soota ingest "视频 URL" --want video --max-cost 0 --json

# 同时保存视频、音频和字幕
soota ingest "视频 URL" --want video,audio,transcript --max-cost 0 --json

# 只要已有字幕，不允许本地转写；同时导出 SRT 和 VTT
soota ingest "视频 URL" --want transcript --transcript captions-only \
  --subtitle-format srt,vtt --max-cost 0 --json

# 保存网页原文和图片
soota ingest "文章 URL" --want context,images --max-cost 0 --json

# 图片为可选目标，失败不影响必需的 context 目标
soota ingest "文章 URL" --want context --optional images --max-cost 0 --json
```

| `--want` | 交付内容 |
| --- | --- |
| `video` | 经过媒体验证的视频文件；默认要求音轨，可明确使用 `--allow-silent` |
| `audio` | 独立音频文件；已有合格视频时可从中提取 |
| `transcript` | 字幕及 `--subtitle-format` 指定的导出文件 |
| `context` | 按原顺序保存的正文或字幕文字，供 Agent 使用 |
| `markdown` | 未加工正文的 Markdown 输出目标，沿用 context 的处理边界 |
| `images` | 实际取得并验证的图片；图文正文使用相对附件链接 |
| `data` | 原生读取结果或该输入的结构化数据；不自动猜测任意 JSON 的正文含义 |

新 Soota 任务的 `--want` / `--optional` 仅接受以上七种采集目标。保存网站已有文章正文使用 `context` 或 `markdown`；语义文章、分析、笔记和大纲交给 LoreLab content，CLI 不调用大模型。

`--language` 要求实际字幕语言，不能用语言未知或其他语言的轨道冒充。本地字幕未提供可靠语言元数据时保留未知，不按默认偏好填成中文。`--min-height` 指定视频最低高度，`--max-bytes` 限制单个媒体文件大小。字幕导出支持 `srt,vtt,txt,json`；缺少时间轴时可以导出 TXT/JSON，不能生成虚构时间的 SRT/VTT。

字幕模式沿用 `auto`、`captions-only`、`asr`、`none`：`auto` 只在确认无字幕时进入本地转写，`asr` 明确要求语音转写，`none` 不请求字幕。只取元数据可用 `inspect` 或 `--want data`；没有可复用正文且禁止取得字幕或转写时，CLI 会报告缺少目标产物。

## Provider 优先级

`--provider auto` 使用按平台和能力保存的 cost/manual 规则；`--provider monid` 显式使用可选 Monid。通过 `providers auth/enable/disable/priority set/priority show` 配置，详见 [Monid 与来源优先级](monid.md)。元数据、字幕、目录、原生 API、音频分别比较，不按平台给出一个混合单价。Monid 报价预留与其他付费来源共享任务累计上限，异步恢复查询原 runId；无字幕结果也可能收费。

`monid.<TikHub 接口 key>` 原生任务只取得已经登记的读取契约，不自动提供任意平台的文章或字幕转换。`tikhub.<key>` 在 auto 下遵守 api 规则，显式 `--provider tikhub` 强制直连。

## 本地文件、清单与旧内容包

```bash
soota ingest "/path/to/video.mp4" --want audio,context --json
soota ingest "/path/to/audio.m4a" --want transcript,context --json
soota ingest "/path/to/captions.vtt" --want transcript --subtitle-format srt,json --json
soota ingest "/path/to/article.md" --want context,images --json
soota ingest "/path/to/bundle/manifest.json" --want context --json
soota ingest --inputs-file "/path/to/links.txt" --want context --workers 2 --max-items 20 --max-cost 0 --json
```

本地处理保留用户原文件，在任务目录处理转换和转写所需的中间文件。普通文本 TXT 按无时间正文读取；若需要将它明确作为字幕输入，可指定 `--input-format txt`。JSON 字幕后缀相同、内部结构可能不同，必要时使用 `--input-format` 指定已支持的格式，详见 [统一字幕转换层](subtitle-formats.md#统一转换层)。

文本清单每行一个链接或路径，空行及以 `#` 开始的行忽略。相对文件路径以清单所在目录为基准。JSON 清单接受字符串或 `InputSpec` 对象，例如：

```json
{
  "inputs": [
    "https://www.youtube.com/watch?v=Example0001",
    "./captions.vtt",
    {"kind": "file", "value": "./article.md"}
  ]
}
```

分享文本中的 HTTP(S) 链接也可作为输入。链接所需的访问参数在受控请求中保留；公开结果会对签名参数和凭据脱敏，不能把脱敏后的媒体地址当作可再次下载的地址。

已有内容包接受目录或 `manifest.json`，可读取 v1/v2。读取旧包不会自动改写文件；补做目标时复用满足要求的已有产物。普通目录不是内容包，不作为输入文件夹递归采集。

### 粘贴文字与知识归纳

用户要求把帖子、片段或混合话题文字保存为知识时，Agent 先把原文逐字保存为独立 TXT，再用本地正文入口。文件里的 URL 保留为文字，不因此访问网站。普通聊天总结不要求摄取或写知识库。

```bash
soota ingest "/path/to/sources/CONTENT_SHA256/source.txt" \
  --provider local --want context --max-cost 0 \
  --output "/path/to/source-bundles" --json
```

如需单来源分析、笔记、文章、大纲或整包导出，把返回的内容包交给 LoreLab Skill，由 `lorelab content` 接续；知识文章交付使用 `lorelab knowledge`。Soota 到源内容包结束，不自行选择知识目录、写作规则或清理策略。

CLI 当前没有纯粘贴正文或标准输入入口；位置参数中的分享文本只提取链接，`--inputs-file` 读取的是条目清单。本地文件的 `full` 仅说明完整读取了该文件，不证明取得原帖全文。CLI 不调用模型，LoreLab content 保存的单份笔记或整包导出也不代替多主题知识交付。

Cubox 批次读取、补抓编排、文章交付与幕布学习任务由 LoreLab 负责；相关操作使用其工作流与 Skill，不在独立 Soota 中复制。旧 `soota knowledge/study` 命令的兼容边界见[迁移说明](knowledge.md)。

## 有界条目并行

统一任务使用 `--workers N` 指定同时处理的独立条目数，范围 **1–8**，默认 **1**。集合枚举与分页保持顺序；去重保留来源第一次出现的位置，结果 `items` 按输入顺序返回，完成顺序可能不同。非默认 `--workers` 也会选择统一任务入口；省略它的旧单视频命令保持原契约。

并行的是每个条目的完整获取／转换／保存流程，不会让单条目的多个步骤同时写入。调度器最多提交 N 个在途条目，每次提交独立复制 `contextvars`，保留共享预算和调用上下文。同一实际内容包目录的不同输入按原顺序处理；跨进程内容包锁继续防止其他任务同时修改。语义产物和目的地导出归 LoreLab content，本采集调度器不承担这些执行。

worker 修改自己的条目副本，保存时在锁内合并该行、序列化并原子替换任务记录，保留其他条目的最新进度。单项获取、格式或本地输入变化错误只阻塞对应条目；枚举或任务记录错误仍可能阻塞任务级步骤。并发竞争付费额度时以实际预留顺序分配，不能保证输入靠前者优先获得预算。

Ctrl-C 停止新条目调度，取消尚未开始的调用，等待已经开始的 worker 完成写入后保存 `paused` 并释放任务锁。收尾期间再次 Ctrl-C 不提前释放锁；等待时间取决于在途 Provider 的超时。强制终止进程不具备同样的收尾保证；已有预算预留仍保留。此实现使用标准库线程池，不新增任务服务或模型调用。

## 集合与原生操作

频道、作者目录、播放列表和已绑定的搜索结果先展开为条目，再进入同一条单项流程：

```bash
soota ingest "频道或播放列表 URL" --want context \
  --max-items 20 --max-pages 3 --max-cost 0.05 --json

soota ingest --inputs-file "/path/to/catalog.json" --want context \
  --max-items 20 --max-cost 0 --json

# 按本机当前参数契约发起一次原生读取，只保存其数据
soota ingest --operation opencli.youtube.search --param query=example \
  --want data --max-cost 0 --json

# 将已有列表转换契约的搜索结果展开为内容任务
soota ingest --operation opencli.youtube.search --param query=example \
  --expand --want context --max-items 5 --max-cost 0 --json
```

YouTube/Bilibili 的可续跑目录沿用原有 `catalog` 分页契约；YouTube 播放列表使用 yt-dlp 枚举；X 最近快照保留完整性未知的边界。`catalog` 本身仍只保存目录，使用统一 `ingest` 才继续处理各条目。搜索展开只接受已绑定的 URL 行结果，不递归扫描任意 JSON 寻找链接。

`--max-items` 限制本次处理的条目数；`--max-pages` 传入目录分页任务。达到本次范围与收齐作者全部历史分别报告，不能从 `ok=true` 推断历史完整性。检查 `enumeration`、`discovered`、`limit_reached` 和各条目状态。

查看本机能力：

```bash
soota capabilities --search download --limit 30 --json
soota capabilities --provider opencli --search article --json
```

此命令读取本地运行时清单、已打包接口注册表和处理器状态，不执行站点采集、不验证登录、不启动 MCP。`discovered` 表示发现能力，`native` 表示原生调用契约可用，`standard` 表示已有标准转换；这些状态都不等于当前网站已真实验收。`verified` 需要具体来源、运行时与产物证据，能力发现不会自行产生该状态。

目前标准正文绑定包括通用网页读取、微信/知乎 Markdown、X 文章和小红书正文；原生媒体绑定包括小红书、X 和 Bilibili。实际执行还取决于当前 OpenCLI 版本的命令、只读标记、参数契约、登录与站点状态。其他已发现命令可以按原生数据路径调用，不自动获得标准正文或媒体能力。

`xiaohongshu/note` 必须提供带非空 `xsec_token` 的完整笔记链接；裸 ID、普通无签名链接在浏览器派发前返回 `input` / `missing_signed_note_url`。从正常搜索结果或作者页取得同一笔记的完整链接后创建新任务；Soota 不伪造 token，也不自行搜索猜测来源。小红书搜索兼容补丁仍执行和验证全部请求筛选，包括默认筛选，见 [受管运行时](providers.md#独立升级与回滚)。

`twitter/thread` 的绑定读取要求返回对象数组中包含请求的目标帖 ID；空数组、只有父帖或缺 ID 返回 `contract` / `target_not_returned`。检查可见性并提高 `limit`（例如 50），同时检查 `top-by-engagement` 是否排除了目标，再以更正参数创建新任务。Soota 不擅自增加 `limit`、排序或裁剪结果；包含目标帖不证明线程完整。该校验也应用于任务恢复的历史原生缓存，其他搜索的合法空数组保持有效。

TikHub 原生读取使用 `--operation tikhub.<接口标识>` 和对应参数；接口标识及参数先查 `api list/show`。统一任务只接收已审查的读取接口，付费请求仍经过费用策略。`api call`、`features call` 保留原有查询用途。

可在用户配置中停用来源：

```toml
[routing]
disabled_providers = ["tikhub", "downie"]
```

停用来源不参与新任务获取，也不能通过原生操作或播放列表绕过门禁。`--provider local` 仅处理已有本地输入，不获取远程媒体或图片；MCP 的 `enabled=false` 仍独立生效。

## 费用与恢复

统一任务内的目录枚举、各条目、字幕和音频阶段共享 `--max-cost`（USD）。每次计费请求在发送前检查并持久化预算预留；失败、中断和 `jobs resume` 不清零已预留费用。返回值 `reserved_cost_usd` 是预留估算，`actual_cost_usd` 未核实账单时保持未知。

`--max-cost 0` 禁止计费接口，允许本地处理和免费来源。上限不替代付费授权：`ask` 逐次确认，非交互调用返回 `payment_required`；`auto` 使用用户已配置的自动授权和余额阈值。不要通过修改付款模式绕过一次任务的暂停。

```bash
# 只生成计划，不采集、不创建任务
soota ingest --inputs-file "/path/to/links.txt" --want video,context --dry-run --json

soota jobs list --json
soota jobs resume JOB_ID --json
soota jobs resume JOB_ID --workers 2 --json
soota jobs resume JOB_ID --max-items 40 --max-pages 6 --max-cost 0.10 --json
```

条数、页数和费用恢复参数表示新的总上限。`workers` 保存在任务请求中；resume 省略时沿用，显式值校验后保存，旧请求缺少此字段时按 1 处理。单独改变 workers 不重置预算、不改变费用上限，也不重做已经完成的枚举；旧 `catalog` 任务不接受这个覆盖选项。

已有目录的 Provider 和游标语义继续保留，不把一个来源的游标交给另一个。下载或转换失败时，已经保存的有效产物保留；再次运行会校验文件与采集条件，补做未满足的步骤。新任务将未绑定浏览器的随机执行作用域一并持久化，重复恢复不会仅因新建随机标识而重新采集；不同新任务仍隔离，已知 profile、采集设置或有效文件变化时会重新校验。没有此作用域的历史请求沿用首次恢复时的重新校验行为。输入文件在任务创建后变化时，仅该条目阻塞，使用新任务处理新的输入。

有效缓存和现有产物不会重复采集或重复预留；失败或结果不确定的付费调用仍保留原预留，重新尝试也必须从剩余额度预留。此账本不能证明上游恰好执行一次，也不把估算当作账单。

OpenCLI 原生读取和标准 Provider 共用安全诊断：错误 `details` 包含已识别的 `upstream_code`、进程 `exit_code`、`category`、固定 `reason`、`retryable` 及本次派发的 `attempts`。未知上游码归为 `UNKNOWN`；本地超时没有上游退出码，`exit_code=null`；签名预检未派发时 `attempts=0`。不输出原始 stderr、上游自由文本提示、headers、trace 路径或私有 URL。`hint` 是对应原因的固定恢复建议。

本层不自动重放。明确暂时导航、网络或等待失败的只读命令标为 `retryable=true`，表示可评估手动恢复，既不是自动重试开关，也不保证原请求未执行。确认任务只含已授权的免费读取后，可使用 `soota jobs resume JOB_ID --max-cost 0 --json`，复用有效成功产物，仅补未满足步骤。该命令会处理任务中的所有未完成项，没有按 `retryable` 自动筛选；混有登录、权限、限流、参数、页面契约或不明结果时先逐项处理原因，必要时另建仅含明确可重试输入的清单。写操作和付费调用不得据此自动重放。更换签名 URL 或 `limit` 是新输入，应新建任务，不改旧任务私有请求来绕过身份校验。

| 状态 | 如何理解 |
| --- | --- |
| `completed` | 本次范围内的必需产物完成；仍需查看集合完整性和可选目标结果 |
| `waiting_analysis`（旧语义任务） | 交由 LoreLab content 接续；新 Soota 采集任务不产生语义交接 |
| `partial` | 部分目标已保存，还有缺失产物或未完成的枚举 |
| `blocked` / `failed` | 查看条目的 `steps`、`errors` 和任务警告，处理对应原因后恢复 |
| `paused` | 中断已收尾并保存；使用同一任务编号恢复 |

JSON 中 `workers` 表示本次并发设置，`items` 分别记录每项的 `bundle`、`steps`、`errors`，`items[].data.missing_outputs` 列出缺失目标，`items[].data.artifacts` 列出产物。整个任务只返回一份状态文档，不将某一步成功当作全部目标完成。Ctrl-C 通过中断退出，`paused` 从保存的任务记录或 `jobs list` 查看。

## 分析接续

单来源分析、笔记、文章、大纲及其提交校验、渲染全部归 LoreLab content。新操作用 `lorelab content ingest INPUT --want context,note`、`lorelab content resume JOB --analysis FILE`；由 Agent 读取实际 context 写作，CLI 校验，不强加文章前置。完整操作与 schema 指导从 LoreLab Skill 进入，Soota 不再维护第二份。

旧语义或交付任务通过隐藏兼容桥恢复，原 `task_id/item_id`、source 证据、路径、预算和产物保持。未安装 LoreLab 时明确报需要安装，并在修改任务或文件前停止。旧 `TaskSpec` 语义词/目标字段与 `AnalysisManifest` 状态仅保留数据兼容，没有对应的语义生成或导出实现。兼容桥只动态调用同一 Python 环境的 LoreLab；交接身份、指纹和目标不变时，LoreLab 保留 `analysis-request.json` 原字节与冻结 hash。见[迁移说明](knowledge.md)。

## 内容包与导出

v2 的 `manifest.json` 保留兼容来源、字幕、分析和费用字段，并使用 `artifacts` 登记实际文件。每个产物记录相对路径、类型、格式、字节数、SHA-256、Provider、验证类型和完整性信息。没有请求或没有取得的产物，不用空占位文件冒充。

| 文件 | 用途 |
| --- | --- |
| `context.md` | 原文输入，含稳定的字幕或正文块 ID |
| `transcript.md` / `transcript.json` | 取得字幕时保存的时间轴和片段数据 |
| `captions.srt` 等 | 用户请求的字幕导出格式 |
| `transcript.original.md` | 显式过滤时保留的过滤前字幕；改变区间时从这份原文重新处理 |
| `body.md` / `content.json` | 取得文章正文时保存的原始 Markdown 与有序正文块 |
| `attachments/` | 视频、音频、图片等实际文件 |
| `data.json` | 请求的数据产物 |
| 既有 `analysis.md` / `analysis.json` / `note.md` / `outline.md` / `content.md` | 历史或 LoreLab 写入的语义产物，采集保留；新写入、校验和渲染由 LoreLab content 负责 |

媒体成功需要文件有效、可解码、目标轨道与质量符合要求，并在已知来源时长时校验时长。文件哈希匹配只说明文件未变；来源时长未知时，不能据此宣称视频全长。标题、简介、节选或范围未知的正文也不能当作全文分析。

Soota 的 `--output` / `SOOTA_OUTPUT_DIR` 只设置采集包位置；整包导出使用 `lorelab content export --bundle PATH --destination PATH`，或 `--vault PATH --subdir DIR`。即使仅导出原文包，目的地写入也由 LoreLab content 负责。

旧 `soota ingest` 的导出参数只走隐藏兼容桥，不保留独立导出实现。导出沿用稳定目录、相对附件链接及 `.soota-export.json` 人工修改保护；采集或渲染 `--force` 不绕过导出冲突。CLI 不解释 `AGENTS.md` 或推断分类；Agent 按 LoreLab Skill 读取目的地规则及相关旧笔记，审阅分类、查重和合并。采集与原文包独立不依赖 LoreLab。

## 实现与验证边界

任务执行位于 `tasks/`，正文结果绑定与发现位于 `content/`，字幕转换位于 `transcripts/`，共享媒体处理位于 `media/`，原文内容包和文件校验位于 `artifacts/`；语义提交、渲染与整包导出位于 LoreLab content，知识文章、学习与交付位于 LoreLab knowledge/study，见[职责图](providers.md#处理流程与职责)。费用预留复用 TikHub 发送前入口，统一任务账本位于 `storage/task_budget.py`。

自动测试使用合成内容、模拟网络响应和本地媒体样本，检查费用、恢复、文件与证据契约。它们不证明某个真实网站已登录、某份视频已完整下载或付费接口当前可用；应按实际来源单独记录运行验收。


## Weir credits 任务限额

可选 `--provider weir` 从 X 帖子／Article、Reddit 帖子读取正文：

```sh
soota ingest "帖子 URL" --provider weir --want context,markdown,data --weir-max-credits 50 --json
soota jobs resume JOB_ID --weir-max-credits 100 --json
```

`providers limits weir --monthly-credits N --task-credits N` 配置跨任务月度上限和新任务默认上限；单任务参数覆盖默认值，续跑参数是新总上限。所有 worker 共用持久账本，跨月不清除任务累计，缓存不重复扣费。`weir_credits` 分别报告实际扣费、未决预留及累计值；不能把 credits 混入 USD 估算。默认仅免费模式，付费等级或购买余额会被拒绝；`--max-cost 0` 继续禁止新的 credits 计费请求。来源、计费估算、外部并发和失败恢复的完整边界见 [Weir](weir.md)。
