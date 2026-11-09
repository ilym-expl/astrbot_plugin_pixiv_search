# AstrBot Pixiv 搜索与原图解析

按 Tag、画师 UID 搜索插画/漫画；按作品 PID 查看详情和首图，或下载整个作品。源码、配置面板、离线测试均包含在此目录中。

v1.2.0 新增：全局请求调度、429 统一暂停、元数据和原图/ZIP 缓存复用、重复任务合并，以及按任务编号关联的详细轮转日志。保留 v1.1.0 的作品链接自动解析功能。

## v1.2.0 请求调度与缓存

所有会话、用户的请求共用当前插件实例的调度器：API 串行请求，默认每 1500ms 启动一次；图片默认每 500ms 启动一次，并受下载并发上限约束。这些间隔是保守初始值，不能保证不被 Pixiv 限流，不代表 Pixiv 公布的配额。

| 配置 | 默认值 | 行为 |
| --- | --- | --- |
| `api_interval_ms` | 1500 | API 请求开始之间的最小间隔，0 可关闭 |
| `image_interval_ms` | 500 | 图片请求开始之间的最小间隔，0 可关闭 |
| `download_concurrency` | 2 | 图片同时传输的数量 |
| `rate_limit_cooldown_seconds` | 180 | 任意 API/图片收到 429 后统一暂停新请求的时长；尊重更长的 `Retry-After` |
| `retry_backoff_seconds` | 5 | 网络异常和 5xx 首次重试等待，后续翻倍，基础等待最多 60 秒 |
| `metadata_cache_minutes` | 10 | 接口数据的内存缓存时间，最多 32 项 |
| `cache_ttl_minutes` | 60 | 原图及 ZIP 按最后使用时间计算的缓存有效期 |
| `cache_max_mb` | 2048 | 磁盘资源缓存软上限，每 5 分钟按最久未使用顺序回收 |

收到 429 后 API 和图片共用暂停状态，不再让其他任务继续发起新请求；已经发出的请求无法撤回。`Retry-After` 支持秒数和 HTTP 日期，未提供时使用配置的暂停时间。默认额外重试 2 次，因此触发限流时任务可能排队等待数分钟，日志中会记录每次等待。暂停中若请求命中缓存，则可以直接复用。

相同接口的进行中请求合并，结果在 TTL 内复用。相同 PID、版本和读取模式（首图预览/完整作品分别处理）的原图任务也会合并；单个请求者断开不会取消其他人共用的下载。成功的完整作品会同时缓存原图和 ZIP，重复发送直接复用，避免重复下载和打包。首图模式只下载首图，仍不会为了预览而下载整个作品。

磁盘缓存位于 `data/plugin_data/astrbot_plugin_pixiv_search/cache/assets/`，成功写入完成标记后才会复用；缺文件或大小不一致时重新构建。磁盘缓存可跨重载复用，但内存接口缓存重载后重新建立。Cookie/API 根地址/图片代理、下载限制或作品版本改变会使用新的缓存身份。缓存中的原图、ZIP 受大小上限和回收策略约束，活跃任务期间总空间可以暂时超过软上限。

此调度器和任务合并只覆盖单个插件实例；多个 AstrBot 进程/服务器之间不共享限流状态。重载会重置内存中的暂停状态，限流期间不要通过反复重载来重试。

升级时覆盖插件源码并在 WebUI 重载，新配置项会按 Schema 补齐。**已有配置值会保留**，例如旧版 `download_concurrency=6` 不会自动改成 2；需要在配置页自行调整。Cookie 和代理配置无需重新填写。

## 每次处理的详细日志

日志默认始终开启，写到 AstrBot 数据目录内：

```text
data/plugin_data/astrbot_plugin_pixiv_search/logs/pixiv.jsonl
```

每行是一条 JSON，时间采用 UTC（北京时间为 UTC+8）。每次 `/p` 命令或自动解析都有独立的 `run_id`，每次插件加载都有 `boot_id`。成功、失败、帮助、参数错误、用户冷却及任务已满都会记录开始、结果和结束；无 Pixiv 链接的普通消息不会产生处理日志。失败回复中会附日志任务编号，方便定位。

日志包含以下信息：

- `run_start` / `command_parsed` / `run_end`：来源、动作、PID、结果、总耗时；会话与发送者使用哈希标识。
- `request_attempt` / `request_start` / `request_status` / `request_end`：API 或图片请求、请求编号、接口类别、实际 HTTP 状态及耗时。
- `request_wait` / `request_retry` / `global_cooldown`：排队、间隔、重试次数与全局暂停时长。
- `metadata_cache_hit` / `asset_cache_hit` / `*_task_join`：缓存命中或等待共享任务；共享任务记录原生产任务编号。
- `image_downloaded` / `zip_start` / `zip_end` / `send_start` / `send_end`：文件名、大小、打包和发送耗时。
- `request_error` / `run_error` / `send_error`：异常类型、异常链类型、系统错误码和调用栈位置（文件名、函数、行号）。

不记录 Cookie、请求头、代理密码、完整聊天原文、完整 URL 查询参数、服务端响应正文或未经处理的异常消息；已知配置凭据还会额外脱敏。调用栈不包含局部变量或源码行。文件中的 PID、时间和会话哈希仍属于运行记录，分享日志时请按自己的需求处理。

日志由后台线程写入，默认单文件 `log_max_mb=10` MiB，保留 `log_backups=5` 份备份，加上当前文件约 60 MiB；旧日志会轮转覆盖，并非永久保存。停用插件时会排空队列并关闭日志；进程被强制杀死或磁盘故障时，末尾未写出的日志可能丢失。日志目录与缓存目录分开，缓存清理不会删除日志。

查找某个任务，例如：

```bash
grep '你的run_id' data/plugin_data/astrbot_plugin_pixiv_search/logs/pixiv.jsonl*
```

## 直接发链接自动解析

在群聊或私聊中直接发作品链接，或在普通文字中附上链接，无需输入 `/p`，例如：

```text
看看这组图 https://www.pixiv.net/artworks/123456789
```

插件自动获取作品信息，并按原有规则下载、发送整个作品：总页数 ≤ `direct_send_limit` 时逐张发送，超过时全部打成一个 ZIP；并发、原图发送方式、Cookie、代理、大小限制等共用已有配置。

- `auto_parse_links`：自动解析开关，默认开启；关闭后仍可使用 `/p` 指令。
- `auto_max_links`：每条消息最多处理的不同作品数，默认 3，可设 1–10。按出现顺序依次处理；同一消息内相同 PID 只下载一次，超出时给出提示。
- 支持 `https://www.pixiv.net/artworks/PID`、`https://pixiv.net/artworks/PID`、带语言路径及分享参数的链接，以及旧版 `member_illust.php?illust_id=PID`；也支持省略协议的 `www.pixiv.net/artworks/PID` 和 `pixiv.net/artworks/PID`。
- `/p 搜 链接` 保持详情加首图的行为，`/p 链接` 保持全作品下载；自动监听不会重复执行这些指令，也忽略机器人自己的消息。
- 多个链接共用一次任务数及用户冷却检查，一个作品的预期读取/下载错误会提示后继续处理后面的链接。
- 只识别适配器传给插件的普通文本链接，不展开短链接、图片中的网址或仅存在于分享卡片内部的隐藏链接。

更新时覆盖原插件目录后，在 WebUI 重载插件，确认新增的“自动解析 Pixiv 作品链接”开关开启；原有 Cookie 和代理配置保存在 AstrBot 的配置目录中，不需要重新复制。自动解析只处理 AstrBot 实际收到并允许插件处理的消息。

## 命令

| 命令 | 行为 |
| --- | --- |
| `/p tag 初音ミク` | 按 Tag 搜索，默认 10 项，可在配置中设置 10–30 |
| `/p tag 初音ミク --count 30 --page 2` | 本次显示最多 30 项，读取 Pixiv 搜索第 2 页 |
| `/p tag "初音ミク VOCALOID" --count 20` | 多关键词以空格连接，交由 Pixiv 解释 |
| `/p 用户 123456 --count 20 --page 1` | 列出该画师最新的插画/漫画 |
| `/p user https://www.pixiv.net/users/123456` | 通过用户链接查询作品 |
| `/p 搜 123456789` | 标题、简介、Tag、作者、UID、页数、尺寸、收藏、浏览量、发布时间、链接，以及第一张原图的图片消息 |
| `/p 123456789` | 下载该作品全部原图，按阈值决定逐张发送或 ZIP |
| `/p https://www.pixiv.net/artworks/123456789` | 作品链接等同于 PID |
| `/p 帮助` | 查看命令说明 |

示例 ID 仅用于说明语法。`tag/标签`、`user/用户/画师/uid`、`搜/搜索/info/id/作品` 支持别名。只输入数字表示下载整个作品；`搜` 后的数字始终解释为作品 PID。

搜索结果采用文字列表，含作品标题、作者、PID、页数、上游提供的 Tag 和作品链接，每 10 项拆成一条消息。不会一次下载 30 个作品的原图。Tag 搜索的 `--page` 对应 Pixiv 原生搜索页，每页从该页选取前 `--count` 项；不同页可能跳过上游未显示的项目。用户搜索按 PID 从大到小排序，按 `--count` 连续分页；翻页时请保持同一个数量。作品不足时按实际数量返回。

“用户搜索”使用稳定的 UID / 用户链接查询该用户作品，**当前不提供按昵称模糊查找用户**。支持插画、漫画及动图原始资源，不包含小说。

## 安装

建议使用 AstrBot 4.x（元数据最低版本为 4.9.2）和 Python 3.10+，实际平台上传行为请在自己的适配器上验证。

1. 解压安装包，把 `astrbot_plugin_pixiv_search` 目录放入 AstrBot 的 `data/plugins/`。检查结构为 `data/plugins/astrbot_plugin_pixiv_search/main.py`，不要额外套两层目录。
2. 在 **AstrBot 使用的同一个 Python 环境或容器内**安装依赖：

   ```bash
   python -m pip install -r data/plugins/astrbot_plugin_pixiv_search/requirements.txt
   ```

3. 在 AstrBot WebUI 的插件页重载插件，进入插件配置，设置 Cookie 和代理，保存后重载。`config.example.json` 是参考值，不会自动导入；不要覆盖 WebUI 配置后忘记重载。
4. 确认 AstrBot 的唤醒前缀包含 `/`，且当前会话允许使用 `p` 指令。若其他插件已经占用 `/p`，请停用冲突指令或修改本插件 `@filter.command("p")`。
5. 先试 `/p 帮助`，再以一个自己可访问的 PID 测试 `/p 搜 PID` 和 `/p PID`。

GitHub 仓库：[ilym-expl/astrbot_plugin_pixiv_search](https://github.com/ilym-expl/astrbot_plugin_pixiv_search)。可在 AstrBot 插件页使用该仓库 URL 安装，也可[下载 v1.2.0 安装包](https://github.com/ilym-expl/astrbot_plugin_pixiv_search/raw/refs/heads/main/dist/astrbot_plugin_pixiv_search_v1.2.0.zip)后按上面的步骤安装。

## Cookie

使用 Pixiv 网页 AJAX 接口，不需要 App API 的 refresh_token。匿名访问可能可用，登录可见内容和某些搜索需要有效的登录态。

在浏览器登录 Pixiv 后，从开发者工具的 Application/存储 → Cookies → `https://www.pixiv.net` 中取 `PHPSESSID`。可在 `cookie` 填：

```text
PHPSESSID=你的实际值
```

也可只填 PHPSESSID 值或完整的一行 Cookie。不要填 JSON、Cookie 导出文件或多行请求头。

读取优先级：**非空环境变量 `PIXIV_COOKIE` → `cookie_file` 文件 → 配置项 `cookie`**。环境变量/文件适合避免把凭据放进插件配置。Cookie 文件为 UTF-8 单行文本，Docker 中要挂载到容器并填写容器内路径。修改 Cookie 后重载插件。

Cookie 不通过聊天命令设置，不在回复或日志中回显；只发给 `api_base_url`，不发给图片 CDN。若你配置了 API 反向代理，该代理会收到 Cookie，因此应使用自建或可信服务。

## 网络代理与接口

| 设置 | 示例/说明 |
| --- | --- |
| `proxy` | `http://127.0.0.1:7890`、`socks5://127.0.0.1:1080`；支持带账号密码的代理 URL |
| `api_base_url` | 默认 `https://www.pixiv.net`；必须兼容 `/ajax/...` 的请求路径和 Pixiv JSON 返回格式 |
| `image_base_url` | 留空直连 `pximg.net`；设置后用该根地址替换 CDN 域名，保留路径和查询参数 |
| `user_agent` | 可改为你登录 Pixiv 的浏览器 User-Agent |

`proxy` 同时用于 API 与图片请求。留空表示直连，插件不读取 `HTTP_PROXY/HTTPS_PROXY` 等系统代理变量。图片请求带 `Referer: https://www.pixiv.net/`，先下载到本地再发送，避免平台直接抓图时缺少 Referer。

Docker 中 `127.0.0.1` 指的是容器本身。若代理运行在宿主机，Docker Desktop 通常使用 `http://host.docker.internal:7890`；Linux 可根据部署使用宿主机网关，并确认代理允许容器访问。不要直接照抄示例端口。

反向代理可与正向网络代理同时使用。API 示例：`https://your-gateway.example/pixiv`，请求会变成 `https://your-gateway.example/pixiv/ajax/illust/PID`；图片示例：`https://your-image-gateway.example`，请求会保留原始 `/img-original/...` 路径。第三方返回格式不同的“随机涩图/搜图 API”不能直接填入此配置。不会自动回退到不可信公共接口，也不支持绕过 Pixiv 登录或验证码。

## 原图、并发与 ZIP

- `direct_send_limit=n`，默认 **5**。页数 **≤n** 时逐张发送，**>n** 时把全部原图合并为一个 ZIP；`n=0` 时一律 ZIP。
- `direct_send_mode=file` 为默认值，逐张用原文件附件发送以保留字节。设置为 `image` 则显示成图片消息，发送的源文件仍为原图，但 QQ 等平台可能压缩。
- `download_concurrency` 首次安装默认 **2**。网络使用异步并发 I/O，并受全局启动间隔约束；压缩和大文件操作通过工作线程运行。
- 所有作品共享下载并发上限。下载按页码排序，文件名如 `PID_p0000.png`，使用接口返回的真实扩展名，不猜测原图地址。
- ZIP 中包含原图、`artwork.json` 和可读的 `README.txt`；图片不重编码，ZIP 使用存储模式，尽量减少 CPU 开销。
- 任意一页失败、页数不一致或超限时停止，不会把缺页的 ZIP 作为完整作品发送。逐张上传时，如果平台中途拒绝上传，先前已发送的附件不能撤回，插件会提示上传失败。
- Pixiv 动图单独处理：打包原始帧 ZIP、含帧延时的 `ugoira.json` 及作品信息。不转 GIF/MP4，不把首图误报为完整动图。

`max_file_mb` 默认 50 MiB，`max_total_mb` 默认 500 MiB（包括失败重试流量），`max_pages` 默认 300。下载上限和平台上传上限不同；平台可能拒绝大 ZIP，当前不会自动分卷。ZIP 生成时原图仍保留，因此磁盘需求可达到下载数据的两倍。动图源 ZIP 也受单文件下载上限限制。

缓存位于 `data/plugin_data/astrbot_plugin_pixiv_search/cache/`。`cache_ttl_minutes` 默认 60 分钟，资源缓存按最后使用时间计时，每 5 分钟清理一次；初始化时也清理过期资源。正在构建和发送的资源不清理，停用插件后清理会暂停，重启后恢复。旧版临时目录仍会按原规则回收。请按实际适配器上传速度设置保留时间。

## 平台兼容性

文本、图片使用 AstrBot 的通用消息组件；ZIP 和默认的原图附件使用 `Comp.File`。是否能发送、是否允许群文件、单文件大小和上传是否压缩，取决于你选用的消息适配器和平台。

OneBot/NapCat 等协议端与 AstrBot 分开部署时，文件路径必须能被协议端读取。可将 AstrBot 的数据目录挂载到两个容器中相同的绝对路径，并检查协议端权限。单纯能收到文字不代表文件上传已配置好。平台不支持 `File` 时，ZIP 无法通过该平台发送；本插件会明确提示失败，下载资源会保留到 TTL 到期。

## 错误处理

网络异常、HTTP 429 和 5xx 默认额外重试 2 次；429 会先全局暂停，其他错误使用指数退避。超时、代理连接失败、普通连接错误、HTTP 429 和 5xx 分别提示，不再全部归为“网络失败或频率受限”。401/403/404 直接提示实际状态码。接口返回登录页、验证码或格式不兼容时会提示 JSON/登录态问题。插件不会自动更新 Cookie。

`max_active_jobs` 默认 2，超出时提示忙碌；已受理任务在全局请求调度器中排队。同一会话内同一用户默认间隔 5 秒，`cooldown_seconds=0` 可关闭；用户冷却与全局请求间隔是不同的限制。详细日志格式与位置见前面的日志说明。

## 测试与验证范围

在插件目录执行：

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

离线测试使用 HTTPX MockTransport，覆盖指令/链接解析、10–30 数量边界、用户分页、并发上限、原图字节一致、ZIP 内容、重试、下载大小上限、Cookie 隔离、失败取消、缓存回收，以及 AstrBot 指令路由和 n / n+1 发送分支。自动解析测试覆盖普通文字中的链接、链接去重、数量上限、共享冷却、开关、自身消息忽略、伪造域名拒绝、指令不重复触发及错误后继续处理。入口契约测试使用精简 AstrBot 替身，**不代表真实 AstrBot 适配器上传已经通过**。

本交付没有使用你的 Pixiv Cookie，也没有连接你的 AstrBot 实例；真实网络和平台发送需在安装后验证。Pixiv AJAX 属于网页内部接口，后续可能变更；代理不能消除账号权限和反爬限制。

v1.2.0 的新增回归测试覆盖 API/图片分别限速、跨通道 429 暂停、HTTP 日期及较长等待时间、真实错误分类、缓存跨实例复用、取消一个等待者、文件缺失/大小异常重建、失败不缓存、资源使用期间不回收、日志脱敏和轮转，以及每次处理的日志起止和请求关联。

## 参考

实现按以下公开接口资料独立编写，没有复制其他 Pixiv 插件代码：

- [AstrBot 官方开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)：入口与插件目录结构。
- [AstrBot 消息组件](https://docs.astrbot.app/dev/star/guides/send-message.html)：文本、图片与文件发送。
- [AstrBot 配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html)：配置面板 Schema。
- [AstrBot 插件存储](https://docs.astrbot.app/dev/star/guides/storage.html)：数据目录。
- [PixivNow 的网页 API 调研](https://github.com/FreeNowOrg/PixivNow/blob/master/docs/pixiv-web-api.md)：搜索、作品页面、画师作品与动图元数据。此资料是社区调研，不是 Pixiv 官方承诺。

许可证：MIT，见 `LICENSE`。
