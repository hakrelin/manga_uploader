# 一键漫画多平台发布器

把一部漫画（图片 + 简介）一次发布到 **B站（专栏文章）**、**百度贴吧（图帖）**、
**e-hentai（图库）**、**再漫画（投稿）**、**小黑盒（图文/文章）** 等多个平台。
提供命令行与图形界面（Web / tkinter）两种用法，基于各平台网页登录态（Cookie）
操作，不做验证码绕过、不采集他人作品。

> 只发布自己拥有版权、或已获得作者授权转载的作品，并遵守各平台内容规范。
> e-hentai 部分内容仅面向成年人；平台接口随时可能调整，遇到失败请查看
> `output/debug/` 下的响应转储再排查。

## 功能

- 一个命令扫描目录里的所有「话」，发布到多个平台
- 图形界面：分字段填 Cookie（或粘贴整段自动拆分）→ 检查登录 → 选择目录或
  “上传漫画”导入（文件夹 / ZIP、CBZ / 图片多选）→ 点“全文预览”核对格式与
  页序 → 一键发布；B站支持扫码登录
- 自动读取 `manga.json`：标题、作者、简介、标签、封面，及各平台专属配置
- 上传前自动压缩/缩放图片（Pillow）：超过大小上限（默认 10MB，可配）的图片
  自动压小，无需处理的图片零拷贝直传；全程只等比缩放，**不会裁剪画面**
- **多平台共享图片预处理**：同一章同时发多个平台时，相同规格的图片只解码/压缩
  一次，结果各平台共享，整批发布结束后统一清理
- B站默认把每话发成**一篇专栏文章**（超上限自动拆篇）；贴吧每楼最多 9 张、首楼
  固定只放封面；小黑盒 ≤30 页发图文、>30 页发文章（文章超 100 页继续拆篇）
- 发布前全文预览：本地真实跑一遍图片处理，展示各平台将提交的标题/正文/字段/
  HTML 结构与逐张图片顺序，自动检查重复页与文件名漏号，全程不联网
- `check` 校验登录态；`--dry-run` 只打印计划不联网；每次发布生成 JSON 报告，
  失败响应自动转存 `output/debug/`
- 支持系统代理 / 手动代理（海外站连不上时启用，国内平台默认直连）
- e-hentai 上传页表单动态解析、字段可配置，站点改版不易写死失效
- 小黑盒：网页创作中心同款签名接口 + COS 直传，可先存草稿核对再发布

## 安装

项目不依赖系统全局 Python 环境即可运行：仓库根目录的启动脚本会准备独立
虚拟环境并自动安装依赖（首次联网下载，之后离线可跑）。

```powershell
# 在项目根目录执行
python -m pip install -r requirements.txt
```

Windows 直接双击 `start.bat`（Web）或 `start-gui.bat`（tkinter GUI）；
macOS / Linux 用 `./start.sh`。

## 快速开始

### 浏览器前端（推荐）

一键启动（首次运行会在项目目录创建 `.venv` 并从镜像安装依赖）：

```bat
:: 浏览器前端（推荐）：双击 start.bat，或
::   .\start-web.ps1 -Lan    # -Lan = 局域网可访问(0.0.0.0)
:: 桌面 GUI（tkinter 备用）：双击 start-gui.bat，或 .\start-gui.ps1
```

```bash
./start.sh               # Linux / macOS，同样支持 --lan
```

也可以手动启动：

```powershell
# 启动本地服务并自动拉起浏览器（默认只监听本机 127.0.0.1）
python -m manga_uploader --web

# 局域网可访问时显式指定监听地址
python -m manga_uploader --web --host 0.0.0.0 --port 8970
```

浏览器界面覆盖全部功能：平台账号（Cookie 分字段 / 粘贴整段 / B站扫码 /
e-hentai 上传表单配置 / 代理）、漫画与发布（路径输入 / 浏览目录 /
拖入 ZIP·CBZ·图片导入 / 压缩与通用设置 / 章节多选）、发布与日志
（检查登录 / 预览计划 / 全文预览 / 一键发布 / 实时日志）。

> 命令行方式（`check` / `publish` / `scaffold`）与旧 tkinter 界面
> （`--gui`，双击 `gui.pyw`）保留可用。

### 命令行

```powershell
# 1. 生成一份示例漫画，熟悉目录结构
python -m manga_uploader scaffold examples\my_comic

# 2. 复制配置并填入各平台 Cookie（config.yaml 已被 .gitignore 忽略）
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

## 运行测试

仓库自带 70+ 个不联网的单元/模拟测试，覆盖目录扫描、配置加载、图片预处理与
自动压缩、多平台共享预处理、代理识别、GUI/Web 配置解析，以及 B站/贴吧/
e-hentai/再漫画/小黑盒发布器的请求链路（含 B站专栏发布与小黑盒签名）：

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

也可以把**全部是图片**的目录当单本/单话发布；子目录里可放 `chapter.json`
（或 `chapter.yaml`）单独覆盖该话标题/简介。

GUI 的“上传漫画…”支持直接选 **文件夹**、**ZIP/CBZ 压缩包**或**多选图片**：
多话目录原样使用；单本（整目录图片 / 压缩包 / 图片多选）会先弹窗填标题、
作者、简介，再复制到系统临时目录的导入缓存（`%TEMP%\mangaupload_imports`）
并自动生成 `manga.json`。RAR / 7z 请先解压成文件夹再导入。

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

优先级：`chapter.json` > `manga.json` 中 `chapters` 对应条目 > 全局字段；
平台配置里没写的字段用 `config.yaml` 中的默认值。

### 日文 → 罗马音自动转换

“漫画信息”页提供“展会/作者/社团 → 罗马音”和“日文标题 → 罗马音标题”：

- 装了 `pykakasi`（`requirements.txt` 已包含）时自动读取汉字读音并按语义分词，
  如 `例大祭 → Reitaisai`；未安装则回退基础假名表，无法识别的汉字保留原样
  （不丢字）。
- 同人专有读法（例大祭、紅楼夢、博麗、輝針城 等）放在
  `manga_uploader/data/romaji_overrides.json`，GUI 有“编辑罗马音词典”按钮，
  格式 `"原文": "假名读音"`；结果仍可在文本框手动微调。
- 本地引擎质量不足时可接任意 OpenAI 兼容接口（DeepSeek / OpenAI / Kimi 等）：
  GUI 里填 Base URL、API Key、模型并启用，转换失败自动回退本地引擎。

## Cookie 获取

打开对应网站并登录，F12 → Network → 刷新页面 → 点任意请求 → 复制 Request
Headers 里的 `Cookie:`，按下面表格填入 `config.yaml`（GUI 支持分字段填入，
也支持“粘贴整段 Cookie”自动拆分）。

| 平台 | 需要的 Cookie | 备注 |
| --- | --- | --- |
| B站 | `SESSDATA`、`bili_jct`（建议 `buvid3`） | 发专栏/动态需绑定手机；`bili_jct` 是 CSRF 令牌 |
| 贴吧 | `BDUSS` | 发帖权限受账号/吧等级限制 |
| e-hentai | `ipb_member_id`、`ipb_pass_hash` | 需站方上传资格；通常还需能直连外网 |
| 再漫画 | `token`（建议 `clientId`） | 登录后取 `token`；投稿入口见平台网页 |
| 小黑盒 | 整段 `Cookie` | 复制请求头 `Cookie` **整段**填入 `cookies.cookie` |

Cookie 会过期（B站 SESSDATA 常见数月），`check` 会提示失效，重新复制即可。
小黑盒比较特殊：粘贴整段 Cookie 时**整段原样保存**（含 HttpOnly 登录态），
不做字段拆分。B站可用“扫码登录”直接获取（需 `pip install qrcode[pil]`）。

## 界面上的发布设置

平台常用设置大多可以在界面直接调节并保存到 `config.yaml`，不需要手改文件：

- **小黑盒卡片 / 工作台“预览与发布”**：「先存草稿」开关、「发布形式」下拉
  （自动 / 强制图文 / 强制文章）、图文分界页数、文章单帖上限、关联社区
  （默认 431327,477625）、关联话题（默认 东方project,东方同人）、站外转载来源。
- **B站**：发布方式（专栏/图文动态）、单篇最多图、原创/转载声明、话题、分类。
- **贴吧**：每楼最多图、图片上传间隔。
- **e-hentai**：上传方式（整包 zip / 逐张）、上传后自动发布、附加标签；
  上传表单字段映射点卡片内“上传表单填写…”。
- **再漫画**：单章最多图、传图失败重试次数。

## 各平台发布逻辑

### B站（专栏文章，默认）

`publish_mode: article`（默认）：

1. `POST /x/article/creative/article/upimage|upcover` 逐张上传正文图片
   （仅 jpg/png、单张 ≤5MB，程序先压缩到 5MB 内；字段/接口版本差异自动回退）；
2. `POST /x/article/creative/draft/addupdate` 先保存草稿取回 `aid`；
3. `POST /x/article/creative/article/submit` 正式发布，输出
   `https://www.bilibili.com/read/cv{aid}`；
4. 单篇最多 `max_article_pages`（默认 100）张，超出按顺序自动拆多篇。

旧版图文动态（`publish_mode: dynamic`，参照
[bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect)）：
上传 `/x/dynamic/feed/draw/upload_bfs` → 发布 `/x/dynamic/feed/create/dyn`，
单条最多 9 张，输出 `https://t.bilibili.com/{id}`。

### 百度贴吧

1. 取 `tbs` 防伪令牌，解析/配置吧 `fid`；
2. 逐张上传图片（间隔可配防频控）；
3. 每楼最多 9 张、首楼固定只放封面，正文 + 图片组合发主题帖。

贴吧反爬严格：遇到验证码/风控直接报错停止该帖，不做绕过；新账号建议先养号、
控制发布频率。

### e-hentai

1. 打开 `https://upload.e-hentai.org/managegallery?act=new` 并动态解析上传表单；
2. 按选项文本匹配分类/语言（`langtag`），汉化默认中文 + `langtype=1`
   （Translated），自动勾选“由专业翻译者翻译”（`langctl`），避免被标机翻；
3. multipart 上传全部页面（zip 整包或逐张，可在界面切换）；
4. 站点先返回“草稿画廊”管理页，程序识别成功后按 `publish_after_upload`
   自动执行 Publish Gallery（设为 false 则只建草稿，由你在 My Uploads 发布）。

上传页常同时有中文/日文等多个标题框，自动匹配容易填错。程序默认只填明确
认识的字段（主标题/简介/标签/分类/语言/评分），并支持 GUI 里按“页面字段名 +
内容来源”逐行自定义映射，结果保存到 `config.yaml` 的
`platforms.ehentai.settings.field_map`。

### 小黑盒（图文 / 文章自动选择）

对应网页创作中心（关联社区 / 话题 / 内容声明 / 转载来源与标题正文）：

1. 整段 Cookie 鉴权，请求走网页端同款签名（hkey/_time/nonce）；
2. 图片经平台 COS 直传，正文按网页编辑器结构提交（text 为 JSON 数组，
   图片自动转站内图床）；
3. 默认按页数自动选择发布形式：≤30 页 → 图文，>30 页 → 文章
   （文章单帖上限 100 张，超出继续拆篇；也可在界面强制图文或文章）；
4. 默认关联社区 东方夜雀食堂 + 东方冰之勇者记，话题 东方project + 东方同人，
   内容声明默认 转载 / 已授权 / 站外来源 bilibili；
5. `publish_draft` 开启时只保存到网页创作中心草稿箱，核对后再手动发布。

### 再漫画（投稿）

对应网页“发布漫画”入口（先看免责声明再点“同意并开始发布”）：

1. `POST v4api.zaimanhua.com/api/v1/comic2/upload/upload/img` 逐张传图
   （multipart 字段 `file`，单张 ≤10MB、单次 ≤500 张）；
2. `POST v4api.zaimanhua.com/api/v1/comic2/upload/submit/chapter` 提交
   `{name, chapter, introduction, downloadUrl, cate, pageUrls}`；
3. 请求头带 `Authorization: Bearer <token>`、`Platform: pc`、可选
   `X-Client-ID`；提交后进入平台人工审核。

作品类型 `cate`：1 原创作品 / 2 原创汉化 / 3 个人扫漫 / 4 转载作品；
同一作品多话使用相同 `name` 即可连载到同一部作品下。

## 多平台图片共享预处理

同一章节同时发布到多个平台时，图片按“允许格式 + 单张上限 + 缩放/质量”规格
统一预处理并做进程级缓存：相同规格的平台共享同一批压缩结果（同一张图只
解码/压缩一次），不再由每个平台各自重复压缩；整批发布结束后统一清理缓存
与临时文件。无需处理的图片保持零拷贝直传。

## 故障排查

- `check` 某平台失败：Cookie 过期/缺失，或账号权限不足。
- 上传返回 412/验证码：平台风控，降低频率、等待或先手动发一帖。
- 发布接口报非预期响应：接口可能更新，把 `output/debug/*.html` 响应保存好
  再调整对应 `publishers/*.py` 的请求参数。
- 图片被跳过：检查格式与单张大小上限（B站专栏仅 jpg/png 且 ≤5MB；图文动态
  与贴吧收 jpg/png/gif；再漫画 ≤10MB）。
- 国内站报网络错误而开了系统代理：GUI「设置」里取消勾选系统代理，或把
  `use_system_proxy` 设为 false；也可在 `platforms.<平台>.settings` 单独配置
  `use_system_proxy: false` 和 `proxy_url: ""` 让该平台直连。

## 项目结构

```
manga_uploader/
├─ cli.py             # 命令行入口
├─ gui.py             # tkinter 图形界面（备用）
├─ web.py / webui.py  # Web 前端服务与共享 UI 逻辑
├─ web/               # Vue3 零构建前端页面
├─ runner.py          # 调度：计划/确认/发布/报告/统一清理
├─ comic.py           # 目录扫描与元数据
├─ composer.py        # 标题/正文组合、罗马音/AI 转换
├─ config.py          # 配置加载与平台默认值
├─ http_client.py     # Cookie/重试/调试转储
├─ util.py            # 图片预处理（共享缓存）/日志/排序
├─ scaffold.py        # 示例目录生成
├─ models.py          # 数据模型
└─ publishers/
   ├─ base.py         # 发布器基类：统一图片预处理与共享缓存
   ├─ bilibili.py
   ├─ tieba.py
   ├─ ehentai.py      # 表单动态解析
   ├─ xiaoheihe.py    # 小黑盒（签名 + COS 直传）
   └─ zaimanhua.py    # 再漫画投稿

gui.pyw               # Windows 双击启动 tkinter GUI
tests/                # 不联网的单元与本地模拟测试
```

新增平台时，实现 `publishers/base.py` 的 `check/plan/publish` 三个方法并在
`runner.py` 的 `PLATFORM_CLASSES` 注册；前端平台卡片在 `webui.py` 的
`PLATFORM_CARDS` 增加即可。
