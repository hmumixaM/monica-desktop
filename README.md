# Monica Desktop

一个在 Mac 本地运行的个人关系管理实验项目。用 Tauri 提供菜单栏和最小设置界面，主体继续使用 Next.js Web；FastAPI、PostgreSQL、采集器和后台任务由应用管理。

最终 `.app` 的使用者不需要 Docker、Homebrew、Python、Node.js 或 PostgreSQL。当前目标为 **Apple Silicon / macOS 26+**。本仓库提供源码和构建脚本，尚未发布经过 Developer ID 签名及公证的安装包。

## 功能

- 联系人、资料版本、社交动态、消息归档、媒体、关系与人工身份关联。
- 本地 Chatlog Alpha / 微信数据导入，以及内置 Instagram、Story、Highlight、LinkedIn 采集器。
- 独立采集、资料投影、图片关联和分析通道；支持暂停、恢复、检查点和任务重试。
- 追加优先的输入队列，按对象保持可变资料顺序，不同帖子可并行。
- PostgreSQL 原生 `pgoutput` CDC 异步记录四个数据库的历史；历史接收独立于实时导入。
- Web 导入中心查看历史变化、提交时间和修改前后的实际值。
- 可选兼容模型服务与本地人脸识别；模型功能默认关闭，由用户配置后启用。

## 构建 macOS 应用

构建机需要 Xcode Command Line Tools、Git、Node.js/npm、uv、Rust/Cargo、Go 1.24+、PostgreSQL 16，以及自行准备的 InsightFace `buffalo_l` 模型。运行时下载固定版本，并验证校验和和采集器源码摘要。

```sh
git clone https://github.com/hmumixaM/monica-desktop.git
cd monica-desktop
npm ci
uv python install 3.12.11

# 从公开的固定上游版本加补丁构建 Chatlog。
apps/desktop/patches/chatlog/build.sh "$PWD/apps/desktop/.build/chatlog-alpha"

uv run --no-project --python 3.12.11 python apps/desktop/scripts/bundle_runtime.py \
  --chatlog-binary "$PWD/apps/desktop/.build/chatlog-alpha" \
  --postgres-prefix /opt/homebrew/opt/postgresql@16 \
  --face-models "$HOME/.insightface/models"

npm run build:desktop
```

应用产物位于 `apps/desktop/src-tauri/target/release/bundle/macos/Monica.app`。
`--face-models` 指向包含 `buffalo_l/det_10g.onnx`、`buffalo_l/w600k_r50.onnx` 的目录；模型权重和组装后的运行时不在 Git 中。

## 首次使用与数据

打开应用，创建本地账号，再从设置页打开 Web。微信需要用户提供本机账号目录和已有的 Chatlog 配置或密钥；应用不自动提取密钥。Instagram / LinkedIn 在独立浏览器窗口登录。平台验证、冷却和采集预算保持生效。

Web/API 只监听 loopback。数据默认保存在 `~/Library/Application Support/Monica/`，日志在 `~/Library/Logs/Monica/`。退出应用会停止所属服务，替换 `.app` 不删除用户数据。凭据目前存放在权限为 `0600` 的本地配置中，尚未接入 Keychain。启用模型分析后，相应内容会发送给用户配置的模型服务。

本仓库不包含个人归档、账号映射、登录状态、数据库、模型密钥或私有部署记录。原始输入、不可变版本、媒体和队列通过稳定 ID 与唯一键去重；仅对当前值、账户归属及任务所有权保留局部同步。CDC 从启用后的变更开始记录，不能重建启用前未保留的历史。

## 目录与文档

| 路径 | 内容 |
| --- | --- |
| `apps/desktop/` | Tauri 设置界面、进程管理、采集器与打包脚本 |
| `apps/web/` | Next.js / React Web UI |
| `services/api/` | FastAPI、导入器、历史与 PostgreSQL 队列 |
| `services/worker/` | 投影、媒体、分析与统计 worker |
| `migrations/` | 有序数据库迁移 |
| `importers/` | 来源适配器与本地人脸识别 |
| `infra/` | 共享快照工具与可选服务器工具 |

- [桌面设置、运行时与完整构建说明](apps/desktop/README.md)
- [锁清单与一致性设计](apps/desktop/LOCKING.md)
- [采集器接口与发布协议](apps/desktop/collectors/README.md)
- [Chatlog 上游、补丁与构建](apps/desktop/patches/chatlog/README.md)
- [最近一次本机验证记录](apps/desktop/validation.json)
- [共享后端与可选服务器开发](docs/SERVER.md)
- [第三方组件声明](THIRD_PARTY_NOTICES.md)

最近一次本机验证：517 项后端测试、17 项运行时与打包合约测试、11 项采集器测试、7 项设置界面测试，以及安装包独立启动、Web 历史详情、暂停恢复和退出清理。测试使用独立数据库与本地样例，不代表真实平台的采集吞吐量。

CI 只运行源码检查和测试。它不部署 NAS，也不发布生产镜像。
