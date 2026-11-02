> 这些是共享后端与可选服务器模式的开发说明。公开桌面仓库的 CI 只做源码验证，不发布生产镜像或部署 NAS。

# Monica Next CRM

这是一个面向个人关系管理的前后端分离 monorepo，用于替代现有 Monica 运行时。前端是 Next.js/React PWA，后端是 FastAPI，结构化查询数据进入 PostgreSQL，原始 JSON、SQLite、聊天归档和媒体留在 NAS 的受控目录。

项目已经实现了可运行的第一版纵向切片：认证、Profile、字段版本、内容寻址、增量导入、社交内容 Timeline、关系/标签、媒体断点上传、异步聚合和 Docker Compose 都可以在本地启动。导入 adapter 已经统一成 Import API contract；真实 WeChat、Instagram、LinkedIn 和 Monica 归档仍要在切换阶段从现有采集目录生成生产批次，仓库里的 smoke 数据只用于验证链路。

## 目录

```text
apps/web                  Next.js + TypeScript + responsive PWA
services/api              FastAPI HTTP API and authentication (feature routers in app/routers)
services/worker           PostgreSQL queue worker and metric refresh
packages/contracts        shared TypeScript import/API contracts
importers                 WeChat, Instagram, LinkedIn, Monica adapters
migrations                ordered PostgreSQL schema migrations
infra                     Compose, Caddy, NAS data and backup scripts
```

## 本地启动

```sh
cp .env.example .env
./infra/init-data.sh
npm install

docker compose -f infra/docker-compose.yml up -d --build
```

本地入口是 `http://localhost:8080`。第一次使用时调用下面的 JSON 请求创建单用户账号，然后访问 `/login`（密码不会放进 URL）：

```sh
curl -X POST http://localhost:8080/api/v1/auth/bootstrap \
  -H 'content-type: application/json' \
  -d '{"email":"owner@example.com","password":"replace-with-a-long-password"}'
```

API 文档通过同一个入口的 `/docs` 或 `/openapi.json` 提供。

如果不使用 Compose，也可以按 `services/api/pyproject.toml` 安装 `uv` 环境并运行 `uv run uvicorn app.main:app --reload --port 8000`，前端的 `next.config.mjs` 会把 `/api/*` 转发到 `API_INTERNAL_URL`（默认 `http://localhost:8000`）。

## 增量和版本规则

- `content_objects` 按 `(content_kind, sha256)` 唯一；字段值、原始 JSON、帖子正文、消息正文和媒体都可以复用同一个内容对象。
- `profile_field_revisions` 是不可变历史，`profile_field_current` 是首屏读取 projection。手工值拥有当前优先级，provider 值在冲突时保留为候选历史。
- 同一外部对象在下一批次 hash 不变时只产生 `source_observations`；变化时才进入 revision 或 social post/story revision。
- batch 的 `idempotency_key`、HMAC 时间戳/nonce、source cursor 和原始 manifest 支持重试与 replay。cursor 只在整个批次事务提交后推进。
- 媒体先写 NAS staging，流式计算 SHA-256 后原子移动到 `media/sha256/aa/bb/<sha256>`；断点上传接口会保留 `next_chunk` 和 `received_bytes`。

Import batch 的最小形状：

```json
{
  "source": "instagram",
  "stream": "posts",
  "schema_version": 1,
  "batch_id": "stable-id",
  "idempotency_key": "instagram:posts:stable-id",
  "cursor_before": "...",
  "cursor_after": "...",
  "observed_at": "2026-09-18T00:00:00Z",
  "records": [
    {
      "external_id": "post-1",
      "profile_id": "00000000-0000-0000-0000-000000000000",
      "entity_type": "post",
      "occurred_at": "2026-09-17T18:14:00Z",
      "content": {"caption": "...", "media_hashes": []}
    }
  ],
  "raw_objects": [],
  "media_manifest": []
}
```

post/story record 会写入 `social_posts`/`social_stories` 及其 revision；`messages`/`chat` stream 会写入按时间分区的 `message_events`，但消息正文只在 Profile/Group 的按需接口读取，不进入全局搜索索引。

## 性能、缓存与重负载任务

NAS 是两块机械盘 RAID1、5.6 GB 内存。以下约定让导入不会拖垮浏览：

- **重负载任务串行 + 空闲 I/O 优先级**（长回填按批持锁，批间让出给导入）：`archive-sync`、`linkedin-sync`、`instagram-story-sync`、`full_migration`、`import_messages`、图片尺寸回填、`platform_trim` 都持有同一个锁 `STAGING_ROOT/.heavy-io.lock`，并以 `ionice` idle + `nice 10` 运行。手工回填必须走同一入口，例如 `python -m app.jobs app.archive_media --instagram-root …` 或 `python -m app.jobs -c "…"`，不要直接 `docker exec … python -c`。诊断时用 `media_assets.byte_length` 统计，不要对媒体目录跑 `du`/`find`。
- **健康检查**：`/health` 与 `/api/v1/health` 不访问数据库、不占线程池；`/api/v1/ready` 带 2 秒查询超时。API 连接默认 `statement_timeout=15s`（`API_STATEMENT_TIMEOUT_MS`），批处理连接不受限。
- **增量导入**：只备份签名（含 `-wal`）变化的 SQLite 文件；消息按文件和表游标增量写入，不再全表 `count(*)`；朋友圈按 `(tid, 原始 XML hash, 作者映射)` 指纹只解析变化的帖子；无法匹配的媒体队列记录写入 `continuous-snapshots/media-import-unmatched.jsonl` 后继续推进检查点；预取对持续失败的资源做指数退避（`prefetch-failures.json`）。
- **数据版本与聚合缓存**：迁移 046 为 `profiles/links/tags/messages/posts/media/contact_events` 各建一个序列，语句级触发器只在真的改了行时推进，所以幂等重放不会让缓存失效。`aggregate_cache.cached_endpoint` 按“命名空间 + 参数 + 相关域版本”缓存到 `STAGING_ROOT/api-cache/aggregates`（多个 API 版本共享），版本变化即重算；同一版本超过 TTL 时先返回旧值并在后台刷新。响应带版本 ETag，`If-None-Match` 返回 304。每个导入周期结束会刷新统计并预热首页、目录、仪表盘、地图等全局聚合（`app.cache_warmup`）。
- **消息统计**：迁移 047 维护 `conversation_message_months`，新消息的会话由触发器标记为 dirty，`contact_stats.refresh` 只重算这些会话；`profile_message_months` / `profile_contact_stats` 是视图，账号改链后自动跟随。`conversations` 带最近消息时间、80 字预览和消息数。迁移 063 再按“会话 × 发言人”维护 `conversation_sender_stats`，所以「最近联系」（`last_contact_at`）取私聊和 TA 自己在群里发言中较晚的一次，另有 `last_direct_at`、`last_group_at`、`group_message_count`；目录、洞察、关系星图、个人主页和「该联系了」都用它。在两个以上私聊里作为对方之外的发言人出现的账号就是自己（`own_message_accounts`），它和它链接的人物不计入群里发言。
- **客户端目录**：`/api/v1/directory` 返回紧凑的列式联系人快照（标签/公司/学校用下标编码），前端 `apps/web/lib/directory.ts` 存入 IndexedDB，用 MiniSearch + pinyin-pro 在本地做姓名、别名、拼音、首字母搜索、筛选和排序；帖子文本索引 `/api/v1/directory/posts` 只在搜索页按需加载。消息全文检索仍在服务端。
- **图片原图直出**：页面直接加载原图，不生成缩放副本。迁移 049/052 在图片入库时排队 `media.dimensions`，worker 只读文件头记录宽高（按 EXIF 方向），供瀑布流预留版面。缺尺寸的历史图片：`python -m app.media_dimensions`，加 `--remove-thumbnails` 删除早期版本在 `media/thumbnails/` 留下的缩放副本。媒体响应 `immutable`、hash ETag、支持 Range。
- **平台库去重**：消息正文只在 CRM `content_objects` 保存一份，`crm_wechat.payloads` 对消息只存 hash 与 `crm_content_kind`；读取用 `platform_store.payload_values`。存量转换：`python -m app.platform_trim`，维护窗口再加 `--vacuum`。
- **PostgreSQL 调参**：`infra/postgres-api-tuning.sql`（`work_mem=16MB`、`jit=off`、`pg_stat_statements`，需重启一次）。
- **历史图片上下文索引**：大型资料库的 Memory 查询可在维护会话中以 autocommit 执行 `infra/ai-media-context-index.sql`，并发创建 `media_links` 的社交媒体覆盖索引。它支持从已识别人脸／图片关联集合读取动态 ID；执行后核对索引有效性与实际查询计划。该索引不改变识别内容，应用镜像可按已验证的构建输入复用。

## 前端结构与视觉

- **视觉**：一本用一支藤黄荧光笔划过重点的通讯录。界面是纸面 `#F3F4F6` 上的墨色 `#1B2230`；唯一的强调色藤黄 `#F5B83D` 只标“现在值得注意”的东西（今天要记得、高峰月、生日当天）。来源色只描述数据从哪来：微信 `#07C160`、Instagram `#C13584`、LinkedIn `#0A66C2`。中文正文用系统苹方栈，标题用 `next/font` 自托管的 Noto Serif SC。浅色 / 深色 / 跟随系统三种主题，首屏前由内联脚本恢复，不闪烁。所有值都在 `apps/web/app/styles/tokens.css`（调色板 → 语义变量两层），关系图和地图的颜色也读这些 CSS 变量。
- **外壳**：桌面是可收起的侧栏，手机是底部 5 个标签加「更多」抽屉。主导航六项：首页、动态、联系人、聊天、关系与足迹、洞察；「数据与设置」收纳账号关联、导入、人脸、设置。旧的 `/dashboard#timeline` 和 `?event=` 链接会转到 `/timeline`。`⌘K` / `Ctrl K` 打开命令面板（页面、人物拼音搜索、最近查看、动态），`/` 聚焦当前页的搜索框；最近查看的人存本地，侧栏和面板都能直达。
- **组件**：通用组件在 `components/ui/`（`Button`、`Tabs`、`Segmented`、`Dialog`、`Menu`、`PhotoViewer`、`LoadMore`、`PersonPicker`、`primitives.tsx` 里的 `Section` / `Panel` / `Avatar` / `SourceMark` / `Stat`），页面组件按功能分目录，各自一个 CSS Module。`Section` 没有数据时不渲染。旧的全局样式表已删除。
- **单人计数**：一个人的私聊、群里发言、通话等数字 1 万以内照常显示，往上用 `formatCount` 简写为「9.2 万」，悬停看精确值；导入核对和聊天明细页仍显示精确数字。
- **动效**：路由与资料分区切换用 View Transitions，动态卡片点开时照片从缩略图放大成详情，对话框、抽屉、提示和周报条形图有短促的进入动画；`prefers-reduced-motion` 下全部关闭。
- **首页**：问候与关键数字 → 周报卡片 → 「今天要记得」（两周内的重要日子、该联系的人和模型给出的理由）→ 最近动态瀑布流。点动态在原地打开详情，只有姓名 / 头像进入个人主页。
- **动态 / 联系人 / 聊天**：动态页按来源和月份筛选，`?event=` 可直达某条；联系人列表是本地目录的虚拟列表，带拼音字母索引条、可折叠的多标签筛选（同时满足）、公司 / 学校筛选和键盘上下选择；聊天页左会话右消息，支持会话内搜索。
- **个人主页**：头部是头像、各来源头像版本、身份标签、外链和操作；下方「往来概况」给出私聊总数、最近联系、通话、共同群、群里发言数和首次联系，以及全历史的「关系丝带」（每月我发 / 对方发、通话、朋友圈互动、重要日子标记，可键盘逐月查看、回车打开该月聊天）。分区：概览 / 动态 / 照片 / 聊天 / 笔记与记录 / 通话与收支 / 关系（群为「成员」）/ 历史，状态写在 `?tab=`。概览左栏是资料（平台简介、重要日子、标签、笔记与资料、照片与文件、微信资料、通话、关系、所属群），右栏是近况、互动统计、足迹地图、LinkedIn 与 Instagram。标签可直接添加和移除，移除的导入标签记在 `tag_membership_removals`，之后的导入不再加回。手写的记录正文和手工资料按 Markdown 显示（GFM 表格和待办，单个换行保留；不渲染 HTML，图片只显示为链接），超过约 16 行先折叠；编辑框随内容变高，可切换预览。导入的通话、收支仍是纯文本。「笔记与资料」一个名称只占一行：手填字段、旧 Monica 打包在 `monica.custom_fields` 里的条目、以及常见别名（Phone / 手机 / 电话，Email / 邮箱，company / 公司 等）合并显示，每行可直接编辑、改名或删除，「添加」时输入已有名称会合并到那一行。保存时由 `PUT /api/v1/profiles/{id}/details` 在一个事务里写入新值并退役旧副本（改名前的字段、打包对象里的同名条目），删除走 `DELETE` 同一路径；都是手工修订，导入不会覆盖或加回，历史里显示「已删除」。顶部「编辑」只改姓名和简介。「照片与文件」和笔记与记录的附件都接受任意文件（每个最大 500 MB）：照片和视频按内容校验并内联显示，其他文件保留原文件名、列为可下载项；HTML、SVG 等文档只以附件形式下载，PDF 可在新标签页打开；时间线照片流只取照片和视频。
- **关系与足迹**：三个视图——亲疏圈（按最近私聊与通话排成 5 / 15 / 50 / 150 四层，社群按 Louvain 划分并可命名）、关系图（朋友圈互动、共同群、确认的合照）、足迹地图（可按月回放）。
- **洞察**：联系日历（每天和谁聊过）、很久没联系、重要日子、最近发动态的人、最近联系、公司与学校排行、全局社交统计。
- **地图**：MapLibre GL（`lib/map.ts`，接近视口才加载），底图是 OpenFreeMap 矢量样式并随主题切换；连不上时退回本地 `vendor/world.geojson` 世界轮廓。
- **数据**：组件只通过 `lib/queries.ts`（react-query）、`lib/directory.ts`（本地目录）和各功能的 `lib/*.ts` 读数据；拼音词典和搜索引擎在第一次输入时才按需加载。
- **原则**：空区块不渲染，不出现 0 值卡片、说明性段落或占位图；截图含真实联系人，只保存在本机的 `.local-validation/`。

## 模型功能（LiteLLM）

`AI_ENABLED=true` 时，API 和 worker 通过 NAS 上的 LiteLLM（`LITELLM_BASE_URL`、`LITELLM_API_KEY`）调用模型；关闭时不调用任何模型，相关区块也不显示。批量任务用 `AI_BATCH_MODEL`，周报及资料建议用 `AI_WRITER_MODEL`，人物 Memory 和每周近况用 `AI_PERSON_MODEL`（默认 `gemini-3.8-flash`）；所用模型须在 NAS 的 LiteLLM `config.yml` 中登记。

- **能发给模型的**：人物资料、Monica 已存的私聊双方文字、此人在群聊中的发言、朋友圈 / Instagram 等动态文字及已存图片描述和 OCR。Memory 与个人周近况按人隔离输入；聊天文本集中在 `app/ai/chat_excerpts.py` 读取。其他批量 AI 功能继续通过 `app/ai/payloads.py` 的 `read` 避免读取聊天正文。
- **人物 Memory 与资料建议**（每周日 01:00）：有实际资料、聊天或动态的人生成独立的长期 Memory，取代原 AI 概览，介绍身份、关系、稳定背景和有依据的近况，并列出已知要点与资料缺口。Memory 分批读取完整的相关资料、聊天和动态，长内容分片；输入不变时使用缓存。资料建议仍读取近两年动态和近 90 天聊天，给出待确认的字段或见面建议；接受、修改和忽略流程不变。每人一个 `ai.profile` 任务，间隔 30 秒排队；只有建议候选而无 Memory 内容的人带 `memory: false`。
- **个人每周近况**（每周一 08:00）：仅对上一个完整周有动态、聊天、OCR 新结果或资料变化的人排 `ai.person_week`，每人一条 2–3 句的简短分析，最多附 4 个可点击来源。分析的每个分片都含该人的 Memory，普通朋友圈和 Instagram 分享按主题概括；本周才导入的旧内容标为「新发现的历史资料」。首页随机显示该周最多 4 人，可通过「查看全部近况」进入每周回顾的「个人近况」；按周分页浏览所有已生成的分析，每次 12 人，附人物和来源链接。周选择同时包含整体回顾与个人近况的记录，某周没有整体回顾也能查看个人近况。个人页仍可查看近况和来源。首次上线只生成最近一个完整周，历史周不自动回填。
- **社群命名**（每天 03:00）：给亲疏圈 / 关系图里的社群起名，可在图例里改名或恢复（`PUT/DELETE /api/v1/communities/{id}/name`）。
- **该联系了**：候选是某一年私聊至少 100 条、且 60 天以上没有私聊、通话或群里发言的人。每次请求都从全部候选里重新加权抽签，返回 30 人；首页每次打开都是新的一批，一次显示 5 人，「换一批」翻到后面 5 人，翻完再重新抽签。权重是 √(当年最高私聊数) × 沉寂程度（一年内逐渐升高，之后缓慢回落，七年后仍保留 0.4）；近 90 天发过动态的人再 ×1.5。每天 06:00 为缺理由或输入已变的候选各排一个 `ai.reconnect_one` 任务（每天最多 120 个，间隔 30 秒），生成理由、开场白和引用的动态。可标记已联系、稍后（2 周 / 1 个月）或不再提醒，均可撤销，撤销后放回原位，其余卡片不重新洗牌（`GET /api/v1/reconnect`，`POST/DELETE /api/v1/reconnect/{profile_id}`）。
- **动态与 Story 图片识别**：新导入 Story 的媒体入库时，在同一事务中排入后台识别队列，导入请求不调用模型；新 Story 优先于历史补扫，每小时 :20 补扫遗漏和变化的媒体。用 `AI_VISION_MODEL`（默认 `gemini-3.5-flash-lite`，多模态）读 `AI_POST_MEDIA_SINCE`（默认 2026-06-01）以来的朋友圈 / Instagram / LinkedIn 动态和 Instagram Story，结合配文，每张图记类别、图里有意义的文字（照抄，忽略水印和界面）和一句短描述，整条动态一句总结；不猜人名、身份、关系和情绪。视频读封面（包含只有封面的 Story），没有封面就截第 1 秒的帧（PyAV）；Live Photo 的动态部分跳过。图片缩到长边 1280，每条最多 10 张。结果分别存在 `social_post_media_readings` 和 `social_story_media_readings`，时间线详情的正文下方显示默认折叠的「图片识别」。每条一个 `ai.post_media_one` / `ai.story_media_one` 任务，间隔 12 秒排队；配文、媒体和模型没变就不再调用，媒体变更重读，补扫对失败任务冷却一周。聊天图片不发送。
- **完整图片 OCR 补扫**：独立 `image-ocr` worker 使用 `gemini-3.5-flash-lite`，按 `media_id` 去重覆盖全部历史帖子 / Story 图片（含视频封面）；聊天图片须另行明确开启 `include_chat`，纯头像跳过。图片长边最多 2048，完整 OCR 保留原语言和换行，写入 `image_ocr_readings`，与旧的简短图片识别分开。按所属帖子、Story、消息的最新时间从新到旧处理；缺少来源日期才回退图片拍摄 / 入库日期。首次快照通过 `python -m app.jobs` 持共享 I/O 锁生成。2026-10-16 已确认包含聊天、跳过头像，当前共 211,173 张，目标约 40 天（不设截止日），日限额 5,600 次图片尝试。`image_ocr_daily_usage` 按 America/Chicago 日历日原子计数，失败尝试也计入，重启、恢复不清零，积压不能突破限额，以约 15 秒间隔处理。空文字同样完成，错误最多尝试 3 次；15 分钟租约及令牌保护防止过期结果覆盖。图片读取持共享 I/O 锁，调用模型时释放；每六小时补入新增图片，挂钩也可即时加入。设置页显示进度、失败和当日额度，支持修改每日数量与暂停 / 继续；`PATCH /api/v1/ai/image-ocr` 调整配置，`GET /api/v1/media/{id}/ocr` 读取单图结果。社交图片 OCR 提供给后续 AI 和详情，聊天 OCR 不加入通用人物分析。配置和进度持久化于数据库。
- **后续 AI 的视觉上下文**：Memory、个人每周近况、资料建议、重新联系理由和总周报会读取已有的帖子 / Story 图片总结、逐图描述、OCR 和人脸信息，包括没有配文的动态。只读取数据库里的识别结果，不重新上传图片。Memory 和个人每周近况分批保留全部相关文字与 OCR；其他模型输入仍采用原有的张数和文字长度限制。人工确认与候选明确区分；候选不能作为身份事实，合影不能直接推断家人、恋爱或工作。
- **资料与关系上下文**：Memory、资料建议与重新联系读取当前备注、昵称、群内显示名、手工字段、笔记及档案简介。Monica 自定义字段的字符串列表按行保留。资料建议可引用这些字段与笔记，出处链接回联系人资料。总周报按批读取最多 12 位动态作者的精简资料；人物的简化互动网络只列最近 365 天最多 8 位对象和已有关系记录，同群和互动频繁不等同于现实关系。生成文案统一用「我」称呼资料所有者，原文引用保持原样。
- **周报**（每周一 08:00）：上周的往来、动态和值得记下的事，`/digest` 查看历史（`GET /api/v1/digests[/{week_start}]`）。
- 设置页「模型服务」显示开关、模型、各任务上次运行与结果，并可立即运行（`GET /api/v1/ai/status`，`POST /api/v1/ai/jobs/{job_type}`）。

## 照片里的人（本地人脸识别）

人脸检测和比对在家里那台常开的 Mac 上运行（`importers/faces`，InsightFace `buffalo_l`），NAS 只保存人脸框、分组和人工确认结果，不保存特征向量。流程与安装见 `importers/faces/README.md`。

- Mac 用签名请求（`X-Import-*` HMAC，来源 `faces`，必须带有效的 `faces` 采集器令牌，nonce 防重放）读取 `GET /api/v1/faces/device/work`、`/media/{id}`、`/anchors`，并 `POST /api/v1/faces/device/batch` 上传人脸框与建议。只有头像和社交帖子里的图片可读，聊天图片不会发出。
- `/faces` 按建议的人物或未知分组列出待确认的人脸，可「是 TA / 是别人 / 不是 TA / 忽略」，每一步都能撤销（`/api/v1/faces/summary|groups|decide|restore`）。
- Profile 独立「照片」Tab 展示已有候选与人工确认，默认 24 张游标分页、下滑自动加载，可按时间或匹配分数排序。默认「人脸」视图显示裁剪后的人脸缩略图，可切换「照片」视图查看原照片与选框；两个视图共享已加载页面，分别保留浏览位置，并记住当前视图。人脸视图的计数仍按去重照片统计；点击具体人脸会在照片详情中选中该条标注。仅打开 Tab 时请求，在当前会话中保留已加载列表与浏览位置。`GET /api/v1/faces/people/{id}/photos` 返回去重媒体、人脸、来源及计数；`/photos/status` 检测变化，提示刷新而不插入正在浏览的列表。
- 相册支持动态、Story、已有聊天媒体记录与头像的出处，按微信账号标识排除公众号来源。聊天出处通过 `?conversation=…&message=…` 定位原消息；缺失出处仍可查看本地照片。本次不扩展扫描器来源、不触发历史重新识别。
- 人脸缩略图使用 `/api/v1/media/{id}/preview?face_id=…`：根据已保存选框裁剪并缓存为 192px WebP，沿用预览的并发限制和原图大小限制，先处理 EXIF 方向；缓存随图片内容或选框变化。不会在列表里下载整批原图或调用识别模型。
- 两个视图默认保持图片浏览，点击「批量管理」才显示多选控件；选择框位于图片下方，操作栏不悬浮遮图，点击图片仍打开详情，进出管理模式不改变当前视图、图片尺寸或裁剪。人脸视图逐张选择，照片视图选择其中归属当前联系人的候选，支持半选提示、跨视图保留选择和「选择已加载候选」。每批最多 200 张人脸，复用 `/faces/decide` 和 `/faces/restore`，成功后更新列表与计数并可撤销本批，退出管理后仍保留撤销入口；失败保留选择。不会处理同照其他联系人的人脸。
- 照片详情可对选中人脸确认、否定、改归属、忽略及撤销，复用现有决定接口和后台反馈读取流程。候选不会因浏览自动变为确认；匹配分数不表示身份正确概率。
- 相册网格使用按需缓存的 480px WebP 预览（`GET /api/v1/media/{id}/preview`），最多同时解码两张，打开详情才加载原图；原始文件保持不变。预览缓存位于 staging 的 `photo-previews-v1`，无需全量预生成。
- 同照人物保留在照片详情中；合照不再产生关系图边或亲疏圈权重。账号关联候选仍显示已有头像人脸相似度。
- 表结构在迁移 `059_faces.sql`（`faces`、`face_scans`，数据版本域 `faces`）。

## 认证和安全

浏览器使用 HttpOnly、Secure、SameSite session cookie，并对浏览器写请求使用 `crm_csrf` cookie + `X-CSRF-Token` 双提交校验；密码使用 Argon2id，登录失败按账号/IP 在 PostgreSQL 中限流并写入审计。TOTP setup/verify 已实现，WebAuthn credential/challenge 表和身份候选队列已经预留。导入请求用时间戳、nonce、batch ID 和 HMAC 签名，默认使用 `IMPORT_HMAC_SECRET`，也可以通过 `IMPORT_HMAC_SECRETS=wechat=...,instagram=...,linkedin=...,monica=...,faces=...` 为每个来源配置独立 secret。导入中心的「采集器令牌」可为每个来源创建（只显示一次）和吊销令牌。生产环境必须把 `APP_ENV=production`、随机 session/import secret、数据库密码和 Tailscale/Caddy 域名写入 NAS 的私有 `.env`，不能提交 Git。

## NAS 目录和备份

`DATA_ROOT` 默认挂载为：

```text
raw/{wechat,instagram,linkedin,monica}
media/sha256
staging
exports
backups
```

`infra/backup.sh` 会导出 PostgreSQL custom dump，并打包 raw/staging/exports；生产环境还应对 `media` 做 NAS 快照或增量备份。Caddy 的 `/api/*` 和 `/health` 直达 FastAPI，其余请求转发到 Next.js；设置 `CRM_SCHEME=https` 与 `CRM_HOST` 后由 Caddy 管理 HTTPS 入口。

## 现有数据接入边界

`importers/` 不读取第三方网站，也不把采集器凭据放进仓库；它只把现有 Chatlog Alpha、WeChat SQLite/SNS、Instagram archive、LinkedIn snapshot 和 Monica export 变成可签名的 batch。现有源工程与字段规则位于 `/path/to/local/cache/monica-wechat-import/README.md`。切换时应先停止 Monica 写入、保存离线快照，再从这些源目录生成全量 batch，人工确认 identity candidates 后运行完整聚合。Instagram Story schema 和 Timeline 已实现，但没有历史归档的对象必须保持 `partial`/`unavailable`，不能解释为“从未发布”。

## 跨平台人物关联

`/linking` 分两个标签页：

- **已关联**：列出所有同时挂着两个以上平台账号的人物，按人物一行、微信 / Instagram / LinkedIn 各一列展示，每页 50 人。人物下方显示备注、微信号、群内名等身份信号，每个账号标出关联来源（旧 Monica 匹配、账号关联页确认及其依据等）。支持按姓名、用户名、备注或群内名搜索和平台组合筛选（`GET /api/v1/link-review/linked`）。
- **待关联**：用姓名、拼音、用户名、邮箱、电话、微信备注和群内名做启发式排序，列出微信、Instagram、LinkedIn 之间尚未挂在同一人上的账号，每页 50 个（`GET /api/v1/link-review?page=&page_size=`）。来源账号和候选人物下方都列出用于匹配的身份信号。勾选账号、改选或搜索候选人物、标记「不是同一人」/「跳过」都只写进浏览器本地的待提交列表（`localStorage`），不会立即保存。

候选计算只给共享索引键（完整名字、名字与用户名词、相邻拼音音节、账号、邮箱、电话）的账号对打分，被很多账号共享的键（单独的姓氏等）不参与。完整队列按数据版本缓存（`link-review-queue`），生产数据首次计算约十几秒，之后筛选和翻页只读缓存。互为候选的两个账号只保留移向有微信或账号更多的人物那一方向，避免批量提交时互换。

底部待提交栏点「统一提交」后，前端每 10 项调用一次 `POST /api/v1/link-review/batch`（单次最多 25 项）。每一项在独立 savepoint 中走现有 `set_link` / 决定记录，失败项回滚并带原因留在待提交栏里重试，不影响同批其他项；不会按同名自动合并 Profile。

关联前的归属检查是实时计算的（`app/link_ownership.py`）：账号当前人物上存在未标来源的头像/封面、平台标签或平台关系边，或旧身份已隔离时，不能把账号挂到其他人物，但仍可解除关联。`source_accounts.migrated_at` 不再作为关联闸门；迁移 `056_wechat_link_ready.sql` 把通过同样检查的微信账号一次性标为已迁移。

## 验证

```sh
npm run lint:web
npm run build:web
npm run test:api
cd services/api && uv run ruff check app tests

docker compose -f infra/docker-compose.yml config
docker compose -f infra/docker-compose.yml ps
```

浏览器回归用 Playwright（`tests/e2e/`），针对真实数据运行，桌面 1440px 和手机 390px 各跑一遍。会话 cookie 只从环境变量读取：

```sh
CRM_BASE_URL=http://localhost:3100 CRM_SESSION_TOKEN=... npx playwright test
```

`local-media.spec.ts` 全部使用模拟接口；`account-links.spec.ts` 会在真实数据上解除再恢复一个 LinkedIn 关联，`afterEach` 保证恢复。截图和结果写入被忽略的 `.local-validation/`。

API smoke 验证了相同 batch 重试、unchanged observation、changed revision、Timeline post/story、媒体 hash 去重、断点上传、TOTP 和 nonce replay guard。下一阶段接入真实归档时应增加批量导入对账、完整聊天/媒体覆盖率和 Playwright 桌面/移动端回归数据。


### LinkedIn 图片与采集器

`linkedin-sync` 每分钟检查 NAS 的
`raw/linkedin/linkedin_enrichment.incoming.json`，只在导出内容变化时导入。
头像 URL、帖子 `image_urls` 会创建 `media.download` 任务，worker 下载到
`media/sha256/`；相同 URL 不再请求，相同内容只保存一个文件。
每个任务最多重试 5 次，可在导入中心查看失败；过期 URL 要由采集器刷新。
只允许 HTTPS LinkedIn CDN 图片，下载有大小、超时和重定向检查。
Timeline 列表和详情优先读取本地媒体。LinkedIn 头像也单独显示在账号链接旁。

已有独立采集器的升级文件位于 `infra/linkedin/`：将 `profile_media.py` 安装到
`src/linkedin_enrichment/`，`publish-crm.sh` 安装到 `scripts/`，运行
`python3 configure_collector.py <collector-root>`。脚本保留原文件备份，新增
目标 Profile 的头像解析、归档导出字段和 NAS 发布步骤，不改变采集配额或解除认证暂停。
`CRM_NAS_HOST` 默认 `linkedin-nas`，`CRM_LINKEDIN_PATH` 默认 NAS 的上述 raw 文件。
保持现有 Mac Chromium 登录；遇到认证暂停需在该 Mac 的原浏览器配置中重新登录。
