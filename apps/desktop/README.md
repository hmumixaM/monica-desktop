# Monica Desktop（macOS 原型）

Monica Desktop 将现有 Monica Web 和后台程序装进一个 macOS 菜单栏应用。
设置窗口使用 Tauri + React；联系人、时间线和分析结果仍在默认浏览器中查看。
应用负责启动、暂停和退出所需进程。使用者无需安装 Docker、Homebrew、Python、Node.js 或 PostgreSQL。

## 运行方式

应用内包含 Python 3.12、Node.js、PostgreSQL 16、Caddy、Chromium、Chatlog Alpha
以及采集器和人脸识别代码。公开发行版使用本机提供的人脸模型，个人构建也可内置模型。Web/API 只监听 `127.0.0.1`，默认浏览器入口为
`http://127.0.0.1:17394`；端口冲突时会选用空闲端口，以应用显示的地址为准。

PostgreSQL 保留现有 CRM 和微信、Instagram、LinkedIn 四个独立数据库。
采集器把完整文件原子发布到本地快照，导入器再更新平台数据、CRM 和后台任务。
Instagram/LinkedIn 新采集账户各自创建独立 Profile，不依赖旧 Monica contact ID，
同名账户不会自动跨平台合并。

关闭设置窗口后，菜单栏应用继续运行。“暂停后台任务”协作停止采集、导入和分析，
保留数据库、API 和 Web，仍可浏览已有结果；未完成的工作按队列或 checkpoint 重试。
“恢复”重新启动后台任务并补跑当前需要执行的任务，不逐次重放整个休眠期间的计划。
Mac 睡眠时不继续采集，唤醒后恢复检查。菜单栏“退出 Monica”会停止所属后台进程和数据库。
可以在常规设置中启用登录 Mac 后自动运行。

## 首次使用

1. 打开构建好的 `Monica.app`，等待本地服务就绪，在设置窗口创建本地账号，
   然后点击“打开 Monica”在浏览器登录。
2. 连接微信：选择包含 `db_storage` 的本机微信账号目录，选择已有 Chatlog JSON 配置，
   或手动填写现有数据库密钥。配置读取 `data_key` 和可选 `img_key`；数据库密钥应为
   64 个十六进制字符。程序不自动提取微信密钥。首次解密导出和导入需要等待，
   后续更新由文件变化和定期检查触发。
3. 配置模型服务：填写 OpenAI-compatible API Base URL（通常以 `/v1` 结尾）、API Key、
   文本模型和支持图片输入的模型，测试连接后启用模型任务。可直接连接兼容服务，
   无需另行部署 LiteLLM。启用后，分析所需的内容会发送给所配置的模型服务。
4. Instagram：填写自己的用户名，点击登录，在独立 Chromium 窗口输入密码并完成验证。
   验证成功后才保存 session 并开始发现 followees、采集帖子、Story 和 Highlight。
5. LinkedIn：点击登录，在独立 Chromium 窗口完成验证。随后发现 connections，
   按现有批次预算采集资料和动态。平台 checkpoint、cooldown 和请求预算会保留，
   重新登录不会清除这些限制。
6. 根据需要启用人脸识别，在 Monica 中确认识别到的人物。运行概览可查看各来源和任务状态，
   “打开日志”可定位失败的阶段。

完整采集与分析需要使用者自己的微信访问配置、平台登录和模型 API 凭据；安装包不携带开发者的账号或数据。

## 本地数据与凭据

| 内容 | 默认位置 |
| --- | --- |
| 数据目录 | `~/Library/Application Support/Monica/` |
| 设置和服务凭据 | 数据目录下的 `settings.json` |
| PostgreSQL 文件 | 数据目录下的 `postgres/` |
| 微信工作快照和缓存 | 数据目录下的 `chatlog/`、`sns-cache/` |
| 采集器 sessions、浏览器状态、checkpoint | 数据目录下的 `collectors/` |
| 原始导出、媒体、导入快照 | 数据目录下的 `raw/`、`media/`、`staging/` |
| 服务日志 | `~/Library/Logs/Monica/` |
| 平台采集详细日志 | 数据目录下的 `collectors/<provider>/logs/collector.log` |

目前凭据保存在本地 `0600` 配置文件中，数据目录权限为 `0700`，尚未接入 macOS Keychain。
这些权限限制其他本机用户访问，不等于加密存储。原生设置状态只返回“已设置密钥”等标记，
不会把密钥值传回 UI。浏览器登录状态同样属于私有数据。卸载或替换 `.app` 不会自动删除这些数据。

## 开发构建

当前构建目标为运行 macOS 26+ 的 Apple Silicon Mac。构建机器需要 Git、Node.js/npm、uv、Rust/Cargo、
Xcode Command Line Tools、Go 1.24+、PostgreSQL 16，以及已有的 InsightFace `buffalo_l`
检测/识别模型。Homebrew 可以用于准备构建机的 PostgreSQL；最终使用者不需要它。
构建脚本会下载固定版本的运行时和 Chromium，并检查适用的校验和。

在仓库根目录执行：

```sh
npm ci
uv python install 3.12.11

# 从固定 Git commit 加补丁构建，不修改已有 Chatlog checkout。
apps/desktop/patches/chatlog/build.sh "$PWD/apps/desktop/.build/chatlog-alpha"

uv run --no-project --python 3.12.11 python apps/desktop/scripts/bundle_runtime.py \
  --chatlog-binary "$PWD/apps/desktop/.build/chatlog-alpha" \
  --postgres-prefix /opt/homebrew/opt/postgresql@16 \
  --face-models "$HOME/.insightface/models"

npm run build:desktop
```

`--face-models` 指向包含 `buffalo_l/det_10g.onnx` 和 `buffalo_l/w600k_r50.onnx` 的目录。
可通过 `CHATLOG_SOURCE_REPO` 指定现有 Chatlog clone，构建脚本仍会在临时目录检出固定版本。
运行时组装到 `apps/desktop/resources/runtime/`，Tauri 的预期应用产物位置为
`apps/desktop/src-tauri/target/release/bundle/macos/Monica.app`。
首次完整构建需要联网；`--skip-web-build` 只适用于已经生成最新 Next standalone 输出的情况。
详细采集器接口见 [collectors/README.md](collectors/README.md)，Chatlog 构建与验证见
[patches/chatlog/README.md](patches/chatlog/README.md)。

## 已完成的针对性验证

后端验证使用 bundled Python 3.12 和独立临时 PostgreSQL 16，以下范围合计 **70 passed**：
新空库初始化、独立账户 Profile、重复导入和解除关联、loopback HTTP 边界、唤醒、
快照与退出、LinkedIn 本地媒体、Instagram Story 和 OCR 回归。
采集器本地 fixture 测试 **6 passed**；vendored LinkedIn 的近期修复 fixture 回归 **40 passed**。
后两者不执行真实平台登录或采集。已检查的后端文件通过 Ruff，新增 Python 源码通过 compileall。

下面复现主要测试。先把 `TEST_DATABASE_URL` 设为一个可创建和删除临时数据库的**专用测试 PG16**，
不要指向日常使用的数据库。测试环境只在 bundled Python 之外增加 pytest：

```sh
uv venv --python apps/desktop/resources/runtime/python/bin/python3 \
  --system-site-packages apps/desktop/.build/test-venv
uv pip install --python apps/desktop/.build/test-venv/bin/python pytest
TEST_PYTHON="$PWD/apps/desktop/.build/test-venv/bin/python"

"$TEST_PYTHON" -m pytest apps/desktop/collectors/tests -q
cd services/api
"$TEST_PYTHON" -m pytest tests/test_desktop.py tests/test_release_contract.py \
  tests/test_snapshot_shutdown.py tests/test_linkedin_archive.py \
  tests/test_instagram_stories.py tests/test_image_ocr.py -q
```

## 当前边界

这是面向当前 Apple Silicon Mac、macOS 26+ 的本地原型；当前 bundled PostgreSQL 的最低系统版本为 26.0。
尚未提供 Intel 构建、
经过 Developer ID 签名和公证的正式分发版本，也没有旧 NAS 数据的自动迁移流程。
现有 NAS 部署与桌面数据目录相互独立。每个桌面数据目录目前只连接一个微信账号，
换账号需要独立数据目录，不能混用已解密快照。

## 应用验收（2026-10-26）

已生成约 2.39 GiB 的 `Monica.app`，完成本地 ad-hoc 签名，并通过
`codesign --verify --deep --strict`。复制到 `~/Applications/Monica.app` 后，
从应用自身资源目录执行完整 smoke test；子进程只使用包内运行时和系统 PATH。
验证包括空库初始化、端口冲突、账号创建与登录、HTTP cookie、Origin/CSRF 边界、
暂停期间保存设置、恢复与唤醒、数据库退出后的完整恢复、退出无遗留服务。
内置 Chromium 实际启动渲染成功；本地人脸模型加载和推理成功。

```sh
uv run --no-project --python 3.12.11 python apps/desktop/scripts/smoke_runtime.py \
  --bundle-root "$HOME/Applications/Monica.app/Contents/Resources/runtime" \
  --work-dir "$PWD/.local-validation/new-smoke-run"

apps/desktop/resources/runtime/python/bin/python3 -m unittest discover \
  -s apps/desktop/tests -p 'test_runtime.py' -v
MONICA_TEST_BROWSER_CHANNEL=chrome npm --workspace apps/desktop test
cargo test --release --manifest-path apps/desktop/src-tauri/Cargo.toml --lib
```

`--work-dir` 必须是不存在的新目录。该测试不登录外部平台、不调用模型服务。
运行时边界与状态回归 **12 passed**；设置界面浏览器回归 **5 passed**；
原生设置页恢复回归 **2 passed**；微信媒体预取策略回归 **15 passed**。
界面测试可使用已有 Chrome；不设 `MONICA_TEST_BROWSER_CHANNEL` 时，需要先执行
`npx playwright install chromium` 安装测试浏览器。另扫描 545 个原生可执行文件/库，
未发现实际加载依赖指向 Homebrew 或其他包外的非系统路径。

使用当前 Mac 的真实微信数据，在独立测试目录验证了 Chatlog → 平台库 → CRM：
创建 1,179 个联系人档案、54,557 条朋友圈记录，并在导入 45,000 条消息后
协作暂停并退出。消息是验证停止时的进度，不代表全部历史已导入。
测试数据和现有微信密钥没有写入应用包，日常应用仍从空库开始。

原生 `.app` 已实际启动，数据库/API/Web/workers 就绪。解锁后已通过原生设置页点击验证：
暂停与恢复、Chatlog 文件选择器及取消、数据源/模型/常规页面、在默认浏览器打开登录页，
以及 Cmd+Q 退出和重新启动。锁屏期间启动曾出现设置页空白，现增加前台页面状态检查：
30 秒没有前端响应时原生重载设置页；隐藏或失焦页面保留宽限时间，避免后台定时器节流时丢失表单。
Web 登录页也已改用适合本地与服务器部署的通用文案。Instagram/LinkedIn 实际账号登录、
云端模型调用仍需使用者提供凭据；当前不声称已完成这些外部服务的实网验收。
可核对非私有验收记录 [validation.json](validation.json)。

技术输入字段关闭了系统自动纠错、自动大写和拼写检查，避免 macOS 改写模型名、用户名或路径。
停止请求可以重复调用，但对同一子进程只发送一次协作信号，避免退出清理时重入 Python 信号处理器。

## 2026-10-28：首次导入和进度展示

运行概览现在显示联系人档案、动态和累计导入消息，以及正在执行的步骤。
消息进度只在批次提交后更新；累计消息数量使用持久化游标的成功写入数，
避免为了刷新设置页反复扫描完整消息表。消息总量未知时展示已保存条数，
朋友圈已有明确处理总量时才展示进度条。暂停和过期心跳分别显示“已暂停”和“等待恢复”。

首次导入先解析联系人，再导入朋友圈和消息。联系人映射变化时，朋友圈在同一轮完成更新，
来源未变化时不会在下一轮重复处理。Chatlog 初始快照就绪或生成新快照后会唤醒导入器，
无需一直等待固定扫描周期。主动暂停扫描按可继续的检查点记录，不计为导入失败。

桌面 PostgreSQL 连接统一使用 loopback TCP，避免较长数据目录超过 macOS Unix socket 路径限制。
进度状态只向设置窗口返回阶段和允许的数字计数，不返回消息正文、源文件路径或错误原文。

本轮针对性验证：独立 PostgreSQL 集成测试 **35 passed**，运行时测试 **14 passed**，
设置界面测试 **6 passed**，TypeScript 和变更文件 Ruff 检查通过。
安装后的运行时也已在长路径独立目录完成启动、暂停、恢复、数据库退出恢复和完整关闭验证；
日常数据目录和应用继续保持停止状态。

## 2026-10-29：NAS 导入一致性与采集器性能

以只读方式核对了 NAS 当前 Caddy 实际路由的 API/Web 和后台导入容器，
实际服务版本为 `feb0dcf8e8ed95b1b46474ab8f7db5955e597efb`，与桌面版的核心代码基线一致。
Instagram 帖子解析、LinkedIn 导入与媒体归档、微信联系人、来源时间处理和平台 schema
等七个核心文件与实际 NAS 容器逐文件 SHA-256 一致。
桌面适配保留同一套字段规范化、去重、媒体和增量导入规则；差异是本地路径、
新账户 Profile 初始化、后台调度、进度与唤醒，以及更保守的本地资源预算。
例如微信每轮媒体预取上限在桌面为 1,000、当前 NAS 为 1,000,000；这影响补齐速度，
不改变导入字段或去重规则。没有更改或重启 NAS 服务，也没有迁移 NAS 私有数据。

新增 Instagram 采集产物到本地原子快照、平台库和 CRM 的完整测试，
在服务器模式与桌面模式分别验证作者关联、时间、正文、位置、评论、Story/Highlight 标识、
媒体去重、重复导入和修改后的增量更新。后端相关回归 **185 passed**，
另补充并通过两项正常返回暂停检查点的状态测试（共 187 项不同测试）；
采集器测试 **11 passed**，运行时与打包合约测试 **16 passed**。

Instagram 现在每个采集阶段只扫描该阶段对应的归档，完整一轮从 12 次扫描降至 3 次。
LinkedIn 按内容文件去重校验，并缓存设备、inode、文件大小、mtime 和 ctime；
任意变化都会重新计算 SHA-256。在 256 个媒体地址共用 8 个 1 MiB 文件的本地样例中，
此前算法每轮需读取 256 MiB，当前首次校验读取 8 MiB，未变化的下一轮读取 0 字节媒体内容。
这是发布阶段的磁盘读取量，不能解读为平台网络采集或整体导入耗时的同比提升。
同大小、保留 mtime 的损坏文件仍被拒绝；丢失 CURRENT 时能重建发布快照，失败不推进游标。

构建现在强制核查内置微信 Chatlog、Instagram 帖子/Story/Highlight、LinkedIn 采集器、
对应后端导入器和本地服务组件。还会校验 29 个 vendored 源文件、锁定依赖版本与 Git commit，
执行 9 个入口检查，并实际启动包内 Chromium 渲染本地页面。
缺少文件、包外符号链接或源文件摘要不符会直接中止构建。
可独立复查组装后的运行时：

```sh
npm --workspace apps/desktop run verify:runtime

# 检查安装后的程序，浏览器只渲染本地测试页面。
"$HOME/Applications/Monica.app/Contents/Resources/runtime/python/bin/python3" -B -I \
  apps/desktop/scripts/verify_runtime.py \
  --bundle-root "$HOME/Applications/Monica.app/Contents/Resources/runtime"
```

上述采集与导入测试使用本地 fixture 和独立测试数据库。Instagram/LinkedIn 实际登录、
平台验证和新一轮线上采集仍需使用者账号；当前没有宣称完成这些实网操作。

## 2026-11-01：追加优先的任务通道与异步历史

实时导入与历史采集现在是两个独立通道。桌面自动启用内置 PostgreSQL 16 的
`wal_level=logical`，使用包内 `pgoutput` 解码 WAL；无需 Kafka、Redis 或额外容器。
`ASYNC_PROJECTION_ENABLED` 控制独立导入，`CDC_ENABLED` 控制历史，两者可分别启停。
服务器默认仍关闭这两个实验开关。本轮没有更改 NAS 配置或部署；
NAS 的 PR #36 缓存图片关联修复已纳入桌面源码。

规范化联系人、朋友圈、Instagram 帖子/Story/Highlight 和 LinkedIn API 批次，先写入
对应来源库的不可变 `ingest_batches`，每个投影子批次最多 25 条。
同一事务写入小型 `projection_delivery` 待投递记录。独立 `source-dispatch` 每秒检查，
先向 CRM 提交唯一的 `projection_inbox` 和任务，再确认来源的投递记录。
两个投影 worker 使用既有字段、来源归属和冲突规则更新 CRM，完全不等待 CDC。
批次回执先为 `queued`，全部子批次成功后才变为 `complete` 并汇总数量。
崩溃重放依靠来源批次 ID 和唯一键去重；投递不采用最大序号游标，避免跳过延迟提交的事务。

同一可变对象按接收顺序处理，A→B→A 不会被优先级倒置；同一人的不同帖子可以并行。
近期内容优先于历史补全。微信聊天保留专用分批、游标通道。
微信联系人、朋友圈、聊天、图片下载、图片关联分别运行；Instagram 资料和本地媒体也分开运行，
统计与缓存预热使用独立维护通道。每个通道只防止自身重复运行，不再持有全流程归档锁。

媒体下载、复制和哈希计算尽量在数据库写事务外完成；已存在的相同内容直接读取，
不重写媒体或来源账户记录。去掉媒体目录的全局互斥；修改同一 URL 或同一附件位置时，
才尝试局部非阻塞锁，竞争失败则回滚重试。批量唯一键按稳定顺序处理。
来源账户归属使用共享 KEY SHARE 保护，纯帖子追加使用共享人物 guard；
修改当前字段、合并或解除关联仍保留必要的排他保护。
数据库任务对死锁、序列化冲突和锁超时最多做三次整事务尝试，之后进入持久队列重试。
详细锁清单、保留原因和验证方式见 [LOCKING.md](LOCKING.md)。

下载事件先保存为 `media_dependencies`，再确认文件队列位置。
缺少正文、快照或文件时保留等待状态。同一文件涉及多个朋友圈时合并待关联 ID，
不覆盖旧等待项；不同媒体用途分别保留。每批处理最多 25 个依赖，重复事件不重复登记媒体。
Story/Highlight 和 LinkedIn 只有在内容投影及相应媒体处理完成后才确认归档检查点。

CDC 每轮检查微信、Instagram、LinkedIn 和 CRM 四个数据库，空闲轮询间隔约 30 秒。
有历史待写时以有界批次更快排空，避免大批量首次导入时每批都多保留 30 秒 WAL。
它只用于查看历史：来源库捕获 `accounts`、`account_identifiers`、非消息 `objects`、`media_links`；
CRM 捕获 `profiles`、`identities`、`source_account_links`、`profile_field_current`、
`profile_structured_current`、`activities`、`media_links`。
已存在的追加式消息、内容版本、原始批次和任务状态不再复制进历史日志。
只有观察时间或刷新时间变化的行会被过滤。原有业务审计与版本记录继续保留。

解码后的已提交事务以稳定的来源、LSN、序号写入追加式 `cdc_events`，
成功提交后才推进复制槽；重放不重复写入历史，也不创建业务投影任务。
保存数据库实际提交时间与修改前后值；人物字段详情会解析不可变版本，显示实际字段内容。
它不读取微信客户端本身的 WAL，也不把任意手写来源 SQL 自动解释成业务导入。
历史失败不阻止应用启动、内容导入或媒体关联；设置页单独显示历史接收故障或滞后。

登录 Web 后进入“导入中心 → 历史变化”，可按来源筛选、翻页并查看修改前后内容。
这是启用后新增的历史，不能重建启用前未保留的数据库变化。
暂停时停止采集、投影、关联与分析，轻量历史接收继续，避免长时间保留 WAL；
退出应用会停止全部子进程与 PostgreSQL。每个复制槽的 WAL 保留预算为 512 MiB。
槽丢失、WAL 失效或解码错误会明确报告，不会静默重置并跳过；修复需要明确选择恢复方式。
该预算是磁盘保护，不能保证历史消费者长期故障时仍保有所有变化。

已登录本地账号可使用以下接口：

- `GET /api/v1/pipeline/status`：独立导入、媒体依赖、任务通道与历史心跳。
- `GET /api/v1/cdc/status`：启用 CDC 时的同一状态视图。
- `GET /api/v1/cdc/events?provider=crm&before=<id>&limit=50`：历史元数据，按 ID 翻页。
- `GET /api/v1/cdc/events/{id}`：指定变化的前后记录。
- `POST /api/v1/pipeline/tasks/{id}/retry`：重试失败的投影任务，需要正常会话和 CSRF。

备份时应一起保存四个数据库和 raw/media 目录，批次 manifest 引用内容地址文件。
这些功能不会自动将桌面数据同步到 NAS。当前完整后端回归 **517 passed**，
包括 CDC 事务与崩溃重放、历史槽故障不阻止导入、延迟事务不丢投递、同人不同帖子并行、
当前字段顺序、媒体依赖不同到达顺序，以及持锁情况下的追加读取。
安装包和界面验证结果见 [validation.json](validation.json)。
