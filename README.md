# 一键漫画多平台发布器

把一部漫画（图片 + 简介）一次发布到 **B站（专栏文章）**、**百度贴吧（图帖）**、
**e-hentai（图库）**、**再漫画（投稿）**、**小黑盒（图文）** 等多个平台。提供命令行与**图形界面
（GUI）**两种用法，基于各平台网页登录态
（Cookie）操作，不做验证码绕过，不采集他人作品。

> 只发布你自己拥有版权、或已获得作者授权转载的作品，并遵守各平台内容规范。
> e-hentai 部分内容仅面向成年人；平台接口随时可能调整，遇到失败请查看
> `output/debug/` 下的响应转储再排查。

## 功能

- 一个命令扫描目录里的所有「话」，发布到多个平台
- 图形界面：分字段填 Cookie（或粘贴整段自动拆分）→ 检查登录 →
  选择目录或“上传漫画”导入（文件夹 / ZIP、CBZ / 图片多选）→
  点“全文预览”核对格式与页序 → 一键发布；B站支持扫码登录
- 自动读取 `manga.json`：标题、作者、简介、标签、封面，及各平台专属配置
- 上传前自动压缩/缩放图片（Pillow），**超过 10MB（可配）的图片自动压小**，
  保留格式时零拷贝；全程只等比缩放，**不会裁剪画面**
- B站默认把每话发成**一篇专栏文章**（超过单篇上限自动拆多篇），贴吧超过
  50 张自动拆帖；旧版图文动态模式可配置保留
- 支持系统代理/手动代理（e-hentai 等海外站连不上时启用）
- `check` 校验各平台登录态；`--dry-run` 只打印计划不联网
- **发布前全文预览**（GUI“发布与日志”→“全文预览”）：本地真实跑一遍图片
  处理，展示每平台将提交的标题/正文/字段/HTML 结构与逐张图片顺序，自动检查
  重复页与文件名漏号，防止格式错误和缺页，全程不联网
- 每次发布生成 JSON 报告，失败响应自动转存 `output/debug/`
- e-hentai 上传页表单动态解析，站点改版不易写死失效
- 小黑盒：网页创作中心同款签名接口 + COS 直传，单帖 30 张自动拆帖，可先存草稿

## 安装

```powershell
cd C:\Users\wangx\Documents\mangaupload
python -m pip install -r requirements.txt
```

## 快速开始

### 浏览器前端（推荐）

**一键启动**（首次运行会在程序目录自动创建 `.venv` 虚拟环境并从清华镜像安装依赖）：

```bat
:: 浏览器前端（推荐）：双击 start.bat，或
::   .\start-web.ps1 -Lan    # -Lan = 局域网可访问(0.0.0.0)
:: 桌面 GUI（tkinter 版）：双击 start-gui.bat，或 .\start-gui.ps1
:: 两者共享同一个本地 .venv（绿色 Python 3.12），首次运行自动准备
```

```bash
./start.sh               # Linux / macOS，同样支持 --lan
```

也可以手动启动：

```powershell
# 启动本地服务并自动拉起浏览器
python -m manga_uploader --web

# 只在本机监听（默认 127.0.0.1）；需要局域网访问时加 --host 0.0.0.0
python -m manga_uploader --web --host 0.0.0.0 --port 8970
```

浏览器界面覆盖全部功能：平台账号（Cookie 分字段 / 粘贴整段 / B站扫码 / e-hentai 上传表单配置 / 代理）、漫画与发布（路径输入 / 浏览目录 / 拖入 ZIP·CBZ·图片导入 / 压缩与通用设置 / 章节多选）、发布与日志（检查登录 / 预览计划 / 全文预览 / 一键发布 / 实时日志）。

> 命令行方式（`check` / `publish` / `scaffold`）与旧 tkinter 界面（`--gui`，双击 `gui.pyw`）保留可用。

### 命令行

```powershell
# 1. 生成一份示例漫画，熟悉目录结构
python -m manga_uploader scaffold examples\my_comic

# 2. 复制配置并填入各平台 Cookie
copy config.example.yaml config.yaml
notepad config.yaml

# 3. 检查登录状态（不发布任何东西）
python -m manga_uploader check

# 4. 干跑：只看将要发布的计划
python -m manga_uploader publish examples\my_comic --dry-run

# 5. 真发布（发布前会二次确认；加 --yes 跳过）
python -m manga_uploader publish examples\my_comic

# 常用选项
python -m manga_uploader publish examples\my_comic --platform bilibili,tieba   # 只发指定平台
python -m manga_uploader publish examples\my_comic --chapter ch01 --chapter ch02
python -m manga_uploader publish examples\my_comic --parallel                  # 章节并行
```

> 双击 `gui.pyw` 没反应时，多半是系统里 .pyw 文件关联被旧版 Python
> （例如 ArcGIS 自带的 Python 2.7）占用。新版 `gui.pyw` 会自动检测并改用
> 本机 Python 3 重新启动；仍失败就用命令行或 `--web` 方式启动。

## ⚠ 状态声明

浏览器前端（Web UI）与本次对齐 GUI 能力的改动（漫画信息全字段、罗马音/AI、各平台发布内容编辑）**尚未经过完整的人工测试**：
自动化测试全部通过；B站/贴吧/e-hentai/再漫画/小黑盒已在 2026-09 实测发布
（小黑盒默认 `publish_draft=true` 只存草稿，核对无误后再改 false 公开发布）。
使用中遇到问题请把界面提示与 `output/debug/` 下的转储发出来排查。

## 运行测试

仓库自带 20+ 个不联网的单元/模拟测试（覆盖目录扫描、配置加载、图片预处理与
10MB 自动压缩、代理识别、GUI Cookie 解析，以及 B站/贴吧/e-hentai/再漫画
四家发布器的完整请求链路，含 B站专栏发布）：

```powershell
python -m unittest discover -s tests -v
```

## 漫画目录约定

```
examples\my_comic\
├─ manga.json          # 元数据（必须）
├─ cover.png           # 封面（可选，仅作记录不参与上传）
├─ ch01\               # 每一话一个子目录
│  ├─ 001.jpg
│  ├─ 002.jpg
│  └─ …
└─ ch02\
   └─ …
```

也可以把一个**全部是图片**的目录当单本/单话发布。子目录里也可以放
`chapter.json`（或 `chapter.yaml`）单独覆盖该话标题/简介。

GUI 的“上传漫画…”按钮支持直接选 **文件夹**、**ZIP/CBZ 压缩包**或
**多选图片**导入：多话目录会被原样使用；单本（整目录图片 / 压缩包 /
图片多选）会先弹窗填标题、作者、简介，再复制到系统临时目录的导入缓存
（`%TEMP%\mangaupload_imports`）并自动生成 `manga.json` 后加载。
RAR / 7z 请先解压成文件夹再导入。

### manga.json 字段

```json
{
  "title": "我的漫画",
  "author": "作者名",
  "description": "一句话简介，会作为各平台正文/简介",
  "tags": ["原创漫画", "日常"],
  "cover": "cover.png",
  "chapters": [
    { "folder": "ch01", "title": "我的漫画 第01话", "description": "第一话简介" }
  ],
  "platforms": {
    "bilibili": {
      "publish_mode": "article",
      "reprint": 0,
      "topics": ["原创漫画"]
    },
    "tieba": { "forum": "目标吧名" },
    "ehentai": {
      "category": "Manga",
      "language": "Chinese (Simplified)",
      "rating": "Safe",
      "extra_tags": ["parody:original"]
    }
  }
}
```

优先级：`chapter.json` > `manga.json` 中 `chapters` 对应条目 > 全局字段。
平台配置里没写的字段会用 `config.yaml` 中的默认值。

### 日文 → 罗马音自动转换

“漫画信息”页的“展会/作者/社团 → 罗马音”和“日文标题 → 罗马音标题”
会把日文/假名自动转成 ASCII 罗马音（首字母大写，供 e-hentai 英文标题等使用）：

- 装了 `pykakasi`（`requirements.txt` 已包含）时会自动读取汉字读音并按语义分词，
  如 `例大祭 → Reitaisai`、`万能型天才肌美少女主人公の憂鬱 → Bannougata Tensai
  Hada Bishoujo Shujinkou No Yuuutsu`；未安装则回退基础假名表，汉字保留原样。
- 同人专有读法（例大祭、紅楼夢、博麗、輝針城 等）放在
  `manga_uploader/data/romaji_overrides.json`，GUI 里有“编辑罗马音词典”按钮，
  格式为 `"原文": "假名读音"`，可自行增补；转换结果仍可在文本框里手动微调。
- 本地引擎整体不够准时可接 **AI 接口**（任意 OpenAI 兼容 Chat Completions，如
  DeepSeek / OpenAI / Kimi）：GUI“平台账号”页最下方填 Base URL、API Key、模型，
  勾选启用后点漫画信息页的“… → 罗马音（AI）”按钮；接口返回格式不稳定时可在
  “展开自定义提示词…”里补充约束。AI 失败会自动回退本地引擎，不会阻塞发布。

## Cookie 获取

打开对应网站并登录，按 F12 → Network（网络）→ 刷新页面 → 点任意请求 →
Request Headers（请求标头）里的 `Cookie:` 复制对应键值填入 `config.yaml`。

| 平台 | 需要的 Cookie | 备注 |
| --- | --- | --- |
| B站 | `SESSDATA`、`bili_jct`（建议 `buvid3`） | 发布专栏/动态需要账号绑定手机；`bili_jct` 是 CSRF 令牌 |
| 贴吧 | `BDUSS` | 发帖权限受账号/吧等级限制 |
| e-hentai | `ipb_member_id`、`ipb_pass_hash` | 账号需满足站方上传资格；通常还需能直连外网 |
| 再漫画 | `token`（建议 `clientId`） | 登录 www.zaimanhua.com 后从 Cookie 取 `token`；投稿页 manhua.zaimanhua.com/uploadShows |
| 小黑盒 | 整段 `Cookie` | 登录 www.xiaoheihe.cn 后复制任意请求的 Cookie 头**整段**填入 `cookies.cookie`（需含 `pkey`、`heybox_id`、`user_pkey` 等登录态） |

Cookie 会过期（B站 SESSDATA 常见数月），失效时 `check` 会提示，重新复制即可。
GUI 里每个 Cookie（如 `SESSDATA`、`bili_jct`、`BDUSS`、`token`）都单独一行，
也可以在“粘贴整段 Cookie（自动拆分）”窗口里粘整段（`k=v; k2=v2`）自动填到
各字段。**小黑盒比较特殊：在“粘贴整段 Cookie”里粘入后整段原样保存**（不做
字段拆分），再点“检查登录”确认。B站可用“扫码登录”直接获取（需
`pip install qrcode[pil]`）。

### 小黑盒（图文）

- 登录态：整段 Cookie；关联社区默认 东方夜雀食堂 + 东方冰之勇者记
  （`topic_ids=431327,477625`，最多 2 个），话题默认 东方project + 东方同人。
- 图文正文与图片按网页编辑器结构提交（text 为 JSON 数组，图片自动转站内图床）。
- 单帖最多 30 张（站点硬上限），漫画页数更多会自动拆成多条；`common.interval_seconds`
  可调大以降低“发帖频率过快”被拒概率。
- `publish_draft=true` 时只保存草稿，可在 xiaoheihe.cn 创作中心检查后再手动发布；
  确认无误后改为 `false` 即为直接公开（也可在界面“平台账号 → 小黑盒”里修改）。
- 界面可调：工作台“预览与发布”与“平台账号 → 小黑盒”卡片提供
  「先存草稿」开关与「发布形式」下拉（自动 ≤30 页图文 / >30 页文章、强制图文、强制文章），
  其它平台卡片也新增发布方式/单帖上限等常用控件，保存配置即生效，无需手改文件。

### 多平台图片共享预处理

同一本漫画同时发多个平台时，图片按“允许格式 + 单张上限 + 缩放/质量”规格统一
预处理并缓存：相同规格的平台共享同一批压缩结果（同一张图只解码/压缩一次），
整批发布结束后统一清理缓存与临时文件。

## 各平台发布逻辑

### B站（专栏文章，默认）

专栏发布流程（`publish_mode: article`，默认）：

1. `POST /x/article/creative/article/upimage|upcover` 逐张上传正文图片
   （仅 jpg/png、单张 ≤5MB，程序先统一压缩到 5MB 内；上传字段/接口版本
   差异会自动回退尝试）；
2. `POST /x/article/creative/draft/addupdate` 先保存草稿，取回 `aid`；
3. `POST /x/article/creative/article/submit` 正式发布，输出专栏链接
   （`https://www.bilibili.com/read/cv{aid}`）；
4. 单篇最多 `max_article_pages`（默认 100）张图，超出自动按顺序拆成多篇专栏。

标题/简介进入专栏正文；`original`/`reprint` 控制原创/转载标记（授权搬运请
按 B站规范自行选择）。正文图片在 `manga.json` 的 `platforms.bilibili` 或
`config.yaml` 的 `platforms.bilibili.settings` 里可以覆盖
`publish_mode`、`tid`、`category`、`max_article_pages` 等。

旧版图文动态（`publish_mode: dynamic`，接口参照
[bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect)）：
`/x/dynamic/feed/draw/upload_bfs` 上传 → `/x/dynamic/feed/create/dyn`
发布，单条最多 9 张，超出自动拆条，输出 `https://t.bilibili.com/{id}`。

### 百度贴吧

1. 取 `tbs` 防伪令牌，解析/配置吧 `fid`；
2. 逐张上传图片（间隔可配，防频控）；
3. 以简介 + `<img>` 组合成正文发主题帖。

贴吧反爬严格：遇到验证码/风控时程序**直接报错停止该帖**，不会尝试绕过；
建议新账号先养号、控制发布频率。

### e-hentai

1. 打开 `https://upload.e-hentai.org/managegallery?act=new` 并解析上传表单
   （字段名随页面走）；
2. 按选项文本匹配分类、语言（`langtag`），汉化默认中文 +
   `langtype=1`（Translated）；
3. multipart 一次上传全部页面（自动勾选服务条款、汉化 `langtype=1` 与
   专业翻译者 `langctl`）；
4. 站点会先返回“草稿画廊”管理页（`ulgid=…`，状态 Unpublished），程序会
   识别为上传成功并**自动执行 Publish Gallery**；若在 config 里把
   `publish_after_upload` 设为 false，则只建草稿，由你在 My Uploads 手动发布。

e-hentai 对账号上传资格与内容分类要求较多，失败时先读页面提示
（程序会保留在 `output/debug/`）。

**表单字段填写可自定义**：上传页常同时有中文/日文等多个标题框，
自动匹配容易填错，程序默认只填明确认识的字段（主标题/简介/标签/
分类/语言/评分），不再乱填未知输入框。已按真实上传页
（`managegallery?act=new`）核对默认字段：`gname_en`（英文/罗马字标题）、
`gname_jp`（日文原标题）、`ulcomment`（上传者评论），并自动勾选 `tos`
服务条款、保留页面默认分类/语言/文件夹与语言类型单选。GUI 在 e-hentai
平台卡片点
“上传表单填写…”，按一行一个输入框配置：

- 页面字段名：填页面上输入框的 `name`（F12 查看），可留空自动识别；
- 内容来源：章节标题 / 系列名 / 作者 / 简介 / 标签 /
  manga.json 字段 / 固定文本，下拉框可选手动“选项匹配”；
- manga.json 字段示例：在 `platforms.ehentai` 写
  `"title_jpn": "エロ漫画 第1話"`，来源选“manga.json/config 字段”
  并填 `title_jpn`（默认已映射到 `gname_jp`）。

画廊语言 `langtag`、语言类型 `langtype`（0=官方/无字、1=汉化、2=改写）、
分类等也提供了独立的 config/GUI 字段（`language_label`、`langtype`、
`category_label`）。汉化上传默认：语言=Chinese、`langtype=1`（Translated），
并自动勾选 `langctl`（“由专业翻译者翻译”），避免被标成机翻/渣翻；
上传原版/无字内容时把 `langtype` 改成 0、语言改成 Japanese / No Text 即可。

配置会保存到 `config.yaml` 的 `platforms.ehentai.settings.field_map`，
也可直接手改（见 `config.example.yaml` 注释示例）。

### 再漫画（投稿）

对应网页“发布漫画”入口（先看免责声明再点“同意并开始发布”）：

1. `POST v4api.zaimanhua.com/api/v1/comic2/upload/upload/img` 逐张传图
   （multipart 字段 `file`，单张 ≤10MB、单次 ≤500 张）；
2. `POST v4api.zaimanhua.com/api/v1/comic2/upload/submit/chapter` 提交
   `{name, chapter, introduction, downloadUrl, cate, pageUrls}`；
3. 请求头带 `Authorization: Bearer <token>`、`Platform: pc`，
   可选 `X-Client-ID: <clientId>`；提交后进入平台人工审核。

作品类型 `cate`：1 原创作品 / 2 原创汉化 / 3 个人扫漫 / 4 转载作品，
可在 config.yaml 或 manga.json 的 `platforms.zaimanhua` 里配置；
同一作品的多话使用相同 `name`（作品名）即可连载到同一部作品下。

## 故障排查

- `check` 某平台失败：Cookie 过期/缺失，或账号权限不足。
- 上传返回 412/验证码：平台风控，降低频率、等待或先手动发一帖。
- 发布接口报非预期响应：接口可能更新，把 `output/debug/*.html` 中响应保存好
  再调整对应 `publishers/*.py` 里的请求参数。
- 图片被跳过：检查格式与单张大小上限（B站专栏仅 jpg/png 且 ≤5MB；
  图文动态与贴吧收 jpg/png/gif；再漫画 ≤10MB）。
- 国内站报网络错误而你有系统代理：GUI「通用」里取消勾选系统代理，或把
  `use_system_proxy` 设为 false（国内平台走代理易触发风控/SSL 断连）。
  也可以在 `platforms.<平台>.settings` 里单独写 `use_system_proxy: false`
  和 `proxy_url: ""` 让该平台直连（B站/贴吧/再漫画默认已按此处理，
  e-hentai 等海外站继续走全局代理）。

## 项目结构

```
manga_uploader/
├─ cli.py            # 命令行入口
├─ gui.py            # tkinter 图形界面（账号 Cookie 分字段、漫画导入、压缩/代理、一键发布）
├─ runner.py         # 调度：计划/确认/发布/报告
├─ comic.py          # 目录扫描与元数据
├─ config.py         # 配置加载
├─ http_client.py    # Cookie/重试/调试转储
├─ scaffold.py       # 示例目录生成
└─ publishers/
   ├─ bilibili.py
   ├─ tieba.py
   ├─ ehentai.py     # 表单动态解析
   └─ zaimanhua.py   # 再漫画投稿
gui.pyw              # Windows 双击启动 GUI

tests/               # 不联网的单元与本地模拟测试
```

新增平台时，实现 `publishers/base.py` 的 `check/plan/publish` 三个方法并在
`runner.py` 的 `PLATFORM_CLASSES` 注册即可。
