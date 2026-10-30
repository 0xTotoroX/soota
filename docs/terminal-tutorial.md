# Soota 终端参考教程

所有示例都从项目当前代码运行，避免终端里旧版 `soota` 与源码不一致。统一目标、输入清单、v2 内容包和恢复契约见 [统一内容任务](tasks.md)；本页提供常用终端操作。

## 1. 检查环境

```bash
cd /path/to/soota
uv sync --extra dev
uv run soota doctor --json
uv run soota runtime status --json
```

OpenCLI 需要 Node.js（上游 README 要求至少 20.18.1）、Chrome 和 Browser Bridge 扩展。你已有的全局 OpenCLI 可以直接复用；无需重新安装。先打开已登录目标网站的 Chrome，再在终端运行：

```bash
opencli doctor
opencli profile list
```

opencli-mcp 当前默认停用，无需安装或连接。

若有多个浏览器 profile，通过 `OPENCLI_PROFILE` 指定 `profile list` 中的别名或 context ID。Soota 不读取或导出浏览器密码。

## 本地转写引擎

Apple Silicon 可安装 `mlx-whisper==0.4.3`，在项目外的 `config.toml` 中选择 MLX：

```toml
[asr]
engine = "mlx-whisper"
model = "base"
mlx_whisper_bin = "/path/to/runtime/bin/mlx_whisper"
mlx_model_path = "/path/to/downloaded/whisper-base-mlx"
```

MLX 使用 Metal GPU 和 FP16，采用 greedy 解码，不使用原 Whisper CLI 的 beam search。`mlx_model_path` 可指定本地模型目录或显式 Hugging Face repo ID；省略时 `base` 明确映射到 `mlx-community/whisper-base-mlx`，不会使用上游 CLI 的其他默认模型。其他模型需显式指定。运行环境和模型放在长期用户目录，不依赖实验或聊天目录。

绝对路径、`./`、`../`、`~` 前缀或已存在的路径按本地模型处理。Soota 在调用 MLX 前检查目录、`config.json` 和 `weights.safetensors` / `weights.npz`：目录缺失返回 `local_model_missing`，关键文件缺失返回 `local_model_incomplete`，不会将失效本地路径解释为远程仓库，也不自动下载或切换模型。该检查只确认文件存在，不验证权重内容。显式 `组织/模型名` repo ID 保持原有 MLX 加载行为，可能访问 Hugging Face；相对本地路径请明确写 `./` 或 `../`。

`doctor --json` 的 `asr_engine`、`asr_model` 和 `mlx_whisper_cli` 可核对选择与命令是否存在。切回原命令行引擎时将 `engine` 改为 `whisper-cli`；`auto` 沿用原来的引擎选择顺序。MLX 命令的临时 JSON 输出在读取后清理，音频与转写缓存仍按现有规则保存。

引擎选择不改变 Provider 的费用策略；零预算可以使用免费音频来源及本地转写，仍需对应的本机依赖和可访问来源。ASR 缓存按引擎设置与质量检查版本隔离；验证新引擎时使用新的音频或缓存目录。已有内容包不会因升级自动重新审阅。

本地 ASR 的两个入口（单视频转写、统一任务的本地媒体转写）共用质量门禁：时间戳必须顺序有效，不能超过实际音轨 EOF（仅容许 20 ms 帧对齐余量）；无法取得音轨时长时停止验收。容器视频时长不能替代较短的音轨时长，原来的源媒体完整性校验不因此放宽。

五段及以上且重复比例至少 60%、连续十段相同文字，或单段内长串循环，会返回 `asr_quality_rejected`，`details.flags` 指明异常。少量重复回应仍可保留；异常重复也可能来自真实讲话，CLI 不自行判断语义。原始模型输出保存在唯一的 `asr/quality-rejected-*.json`，路径见 `details.diagnostic`；不会裁剪时间、删除字词或写入 ready 转写缓存。通过门禁也不保证文字准确或内容完整。

遇到重复、错语言或无文字时，先检查实际音轨是否存在、静音及音量，再用已缓存模型和独立本地引擎复核。能量正常也可能只有音乐，空识别也不能证明无语音；未确认的音轨保留人工判读。显式语言应与音轨一致，多语言素材需核对语言设置，不能把强制语言下的乱码当翻译或原字幕。恢复时保留旧包及其 hash，新结果另存；不要用截图字幕替换未听清的人声。

## 2. 零 API 费用获取最近帖子

将 `USERNAME` 换成 X 用户名，不包含 `@`：

```bash
uv run soota catalog "https://x.com/USERNAME" --provider auto \
  --max-items 50 --max-cost 0 --json
```

JSON 中的 `data.catalog` 是帖子数据文件，`data.content` 是可读 Markdown，`data.job_id` 是任务编号。输出默认在 `~/Documents/Soota/catalogs/任务编号/`。正文位于条目的 `text` 字段。

下次只保留没收录过的帖子：

```bash
uv run soota catalog "https://x.com/USERNAME" --max-items 50 \
  --max-cost 0 --incremental --json
```

这是最近快照：OpenCLI 最多 200 条，完整性未知。不能据此宣称已收齐历史，也不能 `jobs resume` 翻到更早帖子。`--max-cost 0` 禁止转入任何计费请求；浏览器采集仍需要本地运行时间和网络。

## 3. 读取或保存视频元数据

```bash
uv run soota inspect "https://www.bilibili.com/video/BV1M1421t7hT" \
  --provider auto --max-cost 0 --json

uv run soota ingest "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
  --transcript none --max-cost 0 --json
```

第一个命令返回标题、作者和已有元数据；第二个保存元数据 Markdown 内容包。`none` 只保存元数据；需要字幕时使用默认 `auto`，Bilibili 和 YouTube 都从 OpenCLI 开始。

确实愿意在免费元数据来源不可用时使用 TikHub，可明确给出兜底和费用上限（USD）：

```bash
uv run soota inspect "https://www.bilibili.com/video/BV1M1421t7hT" \
  --provider auto --fallback tikhub --max-cost 0.01 --json
```

TikHub 需要自己的 API Key。登录、验证码、限流和契约错误会停止，不会触发付费兜底。**这个 fallback 示例适用于已接入的元数据，暂不适用于 X 目录。**

视频字幕按已接入能力选择免费来源，零预算即可尝试：

```bash
uv run soota ingest "视频 URL" --max-cost 0 --json
```

以上零预算示例使用默认 `auto`，确认无字幕后会尝试免费音频获取及本地转写；只想保存原字幕时加 `--transcript captions-only`。省略零预算时，默认 auto 会按付费策略处理 TikHub 兜底；也可显式 `--fallback tikhub --max-cost 0.05`。如需直接选择 TikHub：

```bash
uv run soota estimate "视频 URL" --provider tikhub --json
uv run soota ingest "视频 URL" --provider tikhub --transcript auto \
  --max-cost 0.05 --json
```

这里的 0.05 是示例预算上限，不是固定收费。

B站同作者同一期视频可选 YouTube 优先：

```bash
uv run soota ingest "B站视频 URL" --prefer-youtube --max-cost 0 --json
# 本次关闭跨平台查找：
uv run soota ingest "B站视频 URL" --no-prefer-youtube --max-cost 0 --json
```

开启时默认过滤已标注的推广。没有标注或身份无法确认时会明确报告，详见 [字幕选择与过滤](subtitle-selection.md)。

`--transcript auto` 确认没有字幕轨道后直接下载音频并本地转写；`captions-only` 不转写，`asr` 跳过字幕查询，`none` 只获取元数据。空响应、登录失败和未知状态不会被当成确认无字幕。

音频默认用 yt-dlp。可显式添加 `--audio-provider downie`，或选择经费用授权的 `tikhub`；Downie 不参与字幕查询。Downie 必须先设置下载完成脚本，具体步骤及流程图见 [Provider 路由](providers.md#downie-可选音频接入)。`download_pending` 表示继续检查原 Downie 任务，不能重复提交下载。

安装好 yt-dlp 后，YouTube/Bilibili 自动尝试 OpenCLI → yt-dlp。可单独使用 yt-dlp 方案：

```bash
uv run soota ingest "YouTube 或 B站视频 URL" --provider yt-dlp --transcript captions-only --max-cost 0 --json
```

默认读取 Chrome Cookie；用 `SOOTA_YTDLP_COOKIES_BROWSER` 选择浏览器/profile，设为空关闭。`SOOTA_YTDLP_BINARY` 可指定程序。JSON 返回实际 provider，内容包记录实际字幕语言和轨道类型。此字幕命令不保存完整视频；独立媒体目标使用 `--want video`。

## 4. 三个外部 Provider 的自动更新

在线采集和接口调用前默认检查最新发布包，有新版就安装，通过入口验证后使用。无需每次手动操作。MCP 更新后仍停用；TikHub 的付款方式不受更新影响。首次安装需要 npm、Node.js 和 uv。yt-dlp Provider 适配代码随本项目更新，yt-dlp 程序由原安装工具维护。

```bash
# 立即检查并安装三个 Provider 的最新发布包
soota updates apply --json
# 暂时关闭自动更新；重新启用时改成 true
export SOOTA_AUTO_UPDATE=false
```

可以在用户配置 `config.toml` 中设置检查间隔：

```toml
[updates]
enabled = true
interval_seconds = 0  # 每次在线任务检查；3600 表示一小时
```

帮助、预估和离线预览不触发更新，Soota 退出后也没有后台更新任务。显式指定外部 binary 的 Provider 交由用户管理。自动更新会报告安装或兼容性错误；查看 `runtime_updates` 和 warnings，不要把检查完成当作全部安装成功。下列命令只读查询版本与仓库变化：

```bash
uv run soota updates check --json
```

结果分别给出本机版本、npm／PyPI 最新包版本和上游主分支相对本机版本标签的提交关系。`package_update_available` 表示有较新的包版本；主分支 `ahead` 只表示有新提交，`diverged` 表示提交历史已分叉，都不能直接当作可安装的新版本。网络查询失败会返回 partial 和退出码 2；命令不会安装更新，也不会同步私有备份。

OpenCLI 仍可单独升级和回滚：

```bash
uv run soota runtime check-update --json
# 省略版本号，即查询并安装最新稳定版。
uv run soota runtime update --json
uv run soota runtime connect --json
uv run soota runtime status --json
# 如需恢复：
uv run soota runtime rollback --json
```

更新安装在 Soota 自己的数据目录，不覆盖你现有全局 OpenCLI 或其中的本地补丁。自动更新可切换空闲 daemon；若仍有任务会延期。手动升级后版本不同则停止采集，先等已有采集结束，再运行 `soota runtime connect` 切换空闲 daemon，或直接 rollback。升级检查通过只证明 CLI 契约兼容，还应执行一次零预算 `inspect` 检查网站与扩展。

完整边界和配置见 [Provider 与运行时](providers.md)，可续跑的 TikHub 视频目录见 [目录说明](catalog.md)。

## 查询任务能力与设置付款方式

```bash
# 查阅上游 OpenCLI
opencli list -f json
uv run soota features list --site bilibili --json
uv run soota features show twitter.profile --json
uv run soota features call twitter.profile --param username=jack --json
uv run soota policy show --json
uv run soota policy set --mode ask
# 下面两个是可选设置，不要一起执行：
uv run soota policy set --mode auto --min-balance 5
uv run soota policy set --mode auto --no-balance-limit
```

ask 是初始默认值：按能力先试 OpenCLI，轮到 TikHub 收费前在终端询问 y/N。auto 只改变 TikHub 的付费授权，不改来源顺序；预估扣费后余额触及阈值即暂停，或明确选择不限制余额。`--max-cost` 是额外上限，不会取消 ask 确认。现有业务入口和 TikHub 封装见 [功能入口与费用策略](features.md)。

## 个人资料与知识库规则

个人路径、浏览器 profile 和凭据通过环境变量传入。终端会话或项目外的私人 shell 配置保存实际值；不要将这些值写入仓库、共享 Skill、日志或聊天。下列示例只有占位值：

```bash
export SOOTA_OUTPUT_DIR="/path/to/private-vault/收件箱/Soota"
# 仅在需要覆盖默认路径或选择浏览器上下文时设置：
export SOOTA_CONFIG_DIR="/path/to/private-soota-config"
export OPENCLI_PROFILE="PROFILE_ALIAS"
```

还支持 `SOOTA_CACHE_DIR`、`SOOTA_DATA_DIR`、`SOOTA_OPENCLI_BINARY`、`SOOTA_OPENCLI_MCP_BINARY`。TikHub 凭据使用 `TIKHUB_API_KEY`；不要把真实值写在共享示例中。已有项目外的 `config.toml` 继续兼容，环境变量优先覆盖对应字段。

个人关注名单和执行规则是结构化资料，应保存在项目外的私人文件中；环境变量只保存需要传递的路径或配置值，不把整份名单塞入环境变量。当前 Soota 尚没有名单管理、定时追踪或规则执行命令，不会自动读取名单文件。

Obsidian 中的 `AGENTS.md` 可引用本地处理规则和 prompt，约定文章整理、分析格式、分类、引用和落盘位置。采集时 Soota Skill 读取相关的来源与保存规则；语义分析、文章、分类查重合并由 LoreLab Skill 指导 Agent 读取对应规则和 prompt。CLI 不解释这些文件，也不自动分类或合并。个人名单、规则正文、私人知识库路径与账号资料不写入共享 Skill。面向阅读的来源名称可自定义，命令标识和 CLI 管理的内容包路径仍遵循原有契约。

项目外不等于不会上传：知识库、用户配置或云盘若另外开启同步，需要在各自的同步/Git 配置中排除私人名单、状态和采集结果。Git 忽略规则不会清除已经提交过的历史内容。

## 组合产物与批量输入

```bash
# 文件目标可以组合，不默认添加语义处理
uv run soota ingest "视频 URL" --want video,audio,transcript --max-cost 0 --json
uv run soota ingest "视频 URL" --want video --min-height 720 --max-cost 0 --json
uv run soota ingest "/path/to/captions.vtt" --want context,transcript --subtitle-format srt,json --json
uv run soota ingest "/path/to/article.md" --want context,images --json

# 清单每行一个链接或路径；JSON 也可使用 inputs 数组
uv run soota ingest --inputs-file "/path/to/links.txt" --want context --max-items 20 --max-cost 0 --json
uv run soota ingest --inputs-file "/path/to/links.txt" --want video,context --dry-run --json
uv run soota jobs resume JOB_ID --json
```

必需目标写在 `--want`，可选目标写在 `--optional`。运行结果中的 `items` 分别说明目标是否满足；`partial` 会保留已取得的文件。预算在全批次内累计，恢复不清零；`--max-bytes` 限制单个媒体文件。已有内容包目录或 `manifest.json` 也可以作为输入，补做目标。

本地输入使用任务目录完成转换，不覆盖源媒体或其旁边的同名音频。媒体文件会验证可读性、解码、轨道与已知时长；完整性未知时，结果仍明确保留未知。

## 能力发现与原生结果

```bash
uv run soota capabilities --search download --limit 30 --json
uv run soota capabilities --provider opencli --search article --json
uv run soota ingest --operation opencli.youtube.search --param query=example --want data --max-cost 0 --json
uv run soota ingest --operation opencli.youtube.search --param query=example \
  --expand --want context --max-items 5 --max-cost 0 --json
```

`capabilities` 只查本机命令、受控接口注册表和处理器，不访问站点或核实登录。`native` 表示可以按原生契约调用，`standard` 表示已有结果转换；发现完成不能替代实际文件验收。原生参数以当前能力清单和 `--help` 为准，只有已绑定的列表结构可以使用 `--expand`。

可以在用户配置中关闭不使用的来源：

```toml
[routing]
disabled_providers = ["tikhub"]
```

MCP 仍默认停用。显式来源发生获取错误时保留错误，不因另一工具也能下载就自动改用它；TikHub 音频失败也不再隐式换成 yt-dlp。

## Context 与分析接续

原单视频命令默认保存 `context.md`、`transcript.md` 和 `manifest.json`，不生成文章。显式 `--want` 使用 v2 产物清单，目标限于 `video/audio/transcript/context/markdown/images/data`，详情见[内容包](tasks.md#内容包与导出)。`markdown` 与 `context` 都保持未加工正文。

需要保存分析、笔记、阅读文章或大纲时，使用已安装的 LoreLab：

```bash
lorelab content ingest "内容 URL" --want context,note
lorelab content resume JOB --analysis /path/to/submission.json
lorelab content render --bundle /path/to/bundle --analysis /path/to/submission.json
```

LoreLab Skill 指导 Agent 读取真实 context 和交接模板、写作并提交；CLI 校验身份、指纹和引用证据，不调用模型。普通聊天总结无需入库或先写文章。采集保留旧文章，来源变化时标记旧分析过时。旧语义/交付 ingest 和 resume、`soota render` 仅隐藏兼容转发，未安装 LoreLab 时明确报错且不修改任务，见[迁移说明](knowledge.md)。

## 导出到目录或 Obsidian

```bash
lorelab content export --bundle /path/to/bundle --destination /path/to/archive
lorelab content export --bundle /path/to/bundle --vault /path/to/vault --subdir 收件箱
```

目的地导出由 LoreLab content 实现；Soota `--output` 只决定采集包位置。导出保留相对附件链接，沿用稳定来源目录及 `.soota-export.json` 文件校验值，人工修改或未登记文件冲突时停止，`--force` 不绕过保护。

分类、查重和与既有笔记合并由 Agent 阅读目标规则与相关旧文后审阅，不是 CLI 自动行为。`knowledge publish` 只写新路径或复用相同 bytes，不自动覆盖合并。采集和原文内容包独立运行，不要求安装 LoreLab。
