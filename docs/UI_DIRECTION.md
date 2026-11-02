# 界面设计与检查依据

使用技能：anthropics/skills frontend-design、vercel-labs/agent-skills web-design-guidelines、wshobson/agents responsive-design。

面向自己的关系手册：真实人物、照片、社交近况、工作教育、地点和互动。
主视觉是照片和可探索的关系。禁止用插图、占位人物、假进度或假关系替代真实数据。

隐喻：一本用一支藤黄荧光笔划过重点的通讯录。界面是纸上的墨，藤黄只标“现在值得注意”的东西，来源色只描述数据从哪来，从不表示界面状态。
配色（`apps/web/app/styles/tokens.css`）：纸面 #F3F4F6、卡片 #FFFFFF、墨 #1B2230、次要 #545D6B、淡 #8B93A1、藤黄 #F5B83D（文字用 #8A5A00）；微信 #07C160、Instagram #C13584、LinkedIn #0A66C2。深色主题使用同一套语义变量，关系图与地图读取 CSS 变量，随主题切换。
字体：中文正文用系统苹方 / HarmonyOS Sans / 微软雅黑栈，标题用 Noto Serif SC；正文 15px，标题 23–36px（`--text-*`）。
布局：桌面左侧可收起的侧栏，手机底部五个标签加「更多」；个人主页概览左栏资料、右栏近况与照片，手机单栏且资料在前。
主标题左对齐，封面保持真实比例，图片裁切仅用于预览，详情可查看完整图。
所有数据图必须有清楚的分母、时间或来源范围，点击能查看相关联系人或事件；图可用键盘操作并有文字说明。
动效只用于说明空间关系（缩略图放大成详情、视图切换），时长 120–600ms（`--duration-*`），`prefers-reduced-motion` 时关闭。

检查：触控目标 44px，键盘焦点可见、对话框有名称且可用 Esc 关闭、图片固定尺寸且按需加载，
无水平溢出；浅色和深色都检查；不把历史/批次/原始元数据作为首页内容。
desktop 1440px / mobile 390px 的 Playwright 检查（`tests/e2e/`）通过后记录证据，截图只留在本机 `.local-validation/`。
