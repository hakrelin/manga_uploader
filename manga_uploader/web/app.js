/* 漫画多平台发布器 — 前端逻辑（Vue 3,零构建） */
const { createApp, ref, reactive, computed, nextTick, onMounted, watch } = Vue;

const CSRF = window.CSRF_TOKEN || "";

const SOURCE_CHOICES = [
  ["章节标题", "title"],
  ["系列名", "series"],
  ["作者", "author"],
  ["简介", "description"],
  ["标签", "tags"],
  ["manga.json/config 字段", "meta:"],
  ["固定文本", "text:"],
  ["下拉框：分类（按选项文本匹配）", "category"],
  ["下拉框：语言（按选项文本匹配）", "language"],
  ["下拉框：评分（按选项文本匹配）", "rating"],
];

const CATE_OPTIONS = { "1": "原创作品", "2": "原创汉化", "3": "个人扫漫", "4": "转载作品" };

// 漫画信息 · 次级字段（展会/社团/汉化组/日英标题/系列/标签等，对齐 composer）
const META_EXTRA = [
  { key: "event", label: "展会（如 C105）" },
  { key: "event_en", label: "展会罗马音" },
  { key: "author_en", label: "作者罗马音" },
  { key: "circle", label: "社团" },
  { key: "circle_en", label: "社团罗马音" },
  { key: "group", label: "汉化组（如 茶与金平糖汉化组）" },
  { key: "title_jp", label: "日文原标题" },
  { key: "title_en", label: "英文/罗马音标题" },
  { key: "series", label: "系列/tag 中文（如 东方）" },
  { key: "series_en", label: "系列英文（如 Touhou Project）" },
  { key: "series_jp", label: "系列日文（如 東方Project）" },
  { key: "language", label: "语言（Chinese）" },
  { key: "tags", label: "标签（逗号分隔，如 东方,汉化）" },
  { key: "chapter_name", label: "章节名（默认短篇）" },
];

// 次要/自动生成字段：基本信息（中文标题/日文标题/作者/社团/简介）已在
// 上方单独陈列，这里放罗马音、展会、汉化组、系列、标签等补充项
const META_EXTRA_EXTRA = [
  { key: "event", label: "展会（如 C105）" },
  { key: "event_en", label: "展会罗马音（自动）" },
  { key: "author_en", label: "作者罗马音（自动）" },
  { key: "circle_en", label: "社团罗马音（自动）" },
  { key: "group", label: "汉化组（如 茶与金平糖汉化组）" },
  { key: "title_en", label: "英文/罗马音标题（自动）" },
  { key: "series", label: "系列/tag 中文（如 东方）" },
  { key: "series_en", label: "系列英文（如 Touhou Project）" },
  { key: "series_jp", label: "系列日文（如 東方Project）" },
  { key: "language", label: "语言（Chinese）" },
  { key: "tags", label: "标签（逗号分隔，如 东方,汉化）" },
  { key: "chapter_name", label: "章节名（默认短篇）" },
];

// 各平台发布内容（对齐 composer.PLATFORM_SCHEMA；留空 = 自动组合）
const PLATFORM_CONTENT_SCHEMA = {
  bilibili: [
    { key: "title", label: "标题（默认【汉化组】中文标题）", kind: "text" },
    { key: "description", label: "正文（默认 作者/社团/简介）", kind: "textarea" },
  ],
  tieba: [
    { key: "forum", label: "目标吧名", kind: "text" },
    { key: "title", label: "标题", kind: "text" },
    { key: "description", label: "正文", kind: "textarea" },
  ],
  ehentai: [
    { key: "category", label: "画廊类型（如 Manga）", kind: "text" },
    { key: "language", label: "画廊语言（如 Chinese）", kind: "text" },
    { key: "langtype", label: "语言类型（0官方/1汉化/2改写）", kind: "text" },
    { key: "gname_en", label: "英文标题", kind: "text" },
    { key: "gname_jp", label: "日文标题", kind: "text" },
    { key: "comment", label: "上传者评论", kind: "textarea" },
  ],
  zaimanhua: [
    { key: "work_name", label: "作品名", kind: "text" },
    { key: "chapter_name", label: "章节名（默认短篇）", kind: "text" },
    { key: "introduction", label: "简介", kind: "textarea" },
    { key: "cate", label: "作品类型（1原创/2汉化/3扫漫/4转载）", kind: "text" },
  ],
  xiaoheihe: [
    { key: "title", label: "标题（≤30 字）", kind: "text" },
    { key: "description", label: "正文（默认 作者/社团/简介）", kind: "textarea" },
  ],
};

const EXTRA_LABELS = {
  forum: "目标吧名",
  category_label: "默认分类",
  cate: "作品类型",
  language_label: "画廊语言",
  langtype: "语言类型",
  title_jpn: "默认日文标题",
  max_pages_per_post: "单帖图片上限（默认 30）",
  publish_draft: "只存草稿（true/false）",
  topic_id: "发布社区 id（默认 1=PC游戏）",
};

const NAV_ITEMS = [
  { key: "workbench", label: "工作台", icon: "◈" },
  { key: "accounts", label: "平台账号", icon: "⇄" },
  { key: "settings", label: "设置", icon: "⚙" },
];

const PLAT_LABELS = {
  bilibili: "B站",
  tieba: "贴吧",
  ehentai: "e-hentai",
  xiaoheihe: "小黑盒",
  zaimanhua: "再漫画",
};

function parseCookies(text) {
  text = (text || "").trim();
  if (!text) return {};
  try {
    if (text.startsWith("{")) {
      const obj = JSON.parse(text);
      if (obj && typeof obj === "object" && !Array.isArray(obj)) {
        const out = {};
        for (const [k, v] of Object.entries(obj)) out[String(k)] = String(v);
        return out;
      }
    }
  } catch (e) { /* 非 JSON，走 k=v 拆分 */ }
  const out = {};
  text.split(/[;,\n]/).forEach((chunk) => {
    chunk = chunk.trim();
    if (!chunk || !chunk.includes("=")) return;
    const i = chunk.indexOf("=");
    const k = chunk.slice(0, i).trim();
    let v = chunk.slice(i + 1).trim().replace(/^['"]|['"]$/g, "");
    if (k) out[k] = v;
  });
  return out;
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (opts.json) headers["Content-Type"] = "application/json";
  headers["X-CSRF-Token"] = CSRF;
  const resp = await fetch(path, Object.assign({}, opts, { headers }));
  if (!resp.ok) {
    let msg = "HTTP " + resp.status;
    try { const j = await resp.json(); if (j && j.error) msg = j.error; } catch (e) {}
    throw new Error(msg);
  }
  const ct = resp.headers.get("content-type") || "";
  return ct.includes("application/json") ? resp.json() : resp;
}

createApp({
  setup() {
    // 支持 ?page=accounts|settings|workbench 直达（可收藏/调试）
    const pageParam = new URLSearchParams(location.search).get("page");
    const nav = ref(NAV_ITEMS.some((n) => n.key === pageParam) ? pageParam : "workbench");
    const navItems = NAV_ITEMS;
    const version = ref("");
    const note = ref("");
    const running = ref(false);
    const busy = ref(false);
    const cards = ref([]);
    const config = reactive({ common: {}, platforms: {} });
    const statuses = reactive({});
    const expanded = reactive({});

    const comicDir = ref("");
    const summary = ref(null);
    const metaForm = reactive({ title: "", author: "", description: "", language: "Chinese" });
    META_EXTRA.forEach((f) => { metaForm[f.key] = ""; });
    metaForm.language = metaForm.language || "Chinese";
    const platformContent = reactive({});
    const platformTouched = reactive({}); // 用户手写过的平台字段（组合刷新不覆盖）
    Object.keys(PLATFORM_CONTENT_SCHEMA).forEach((plat) => {
      platformContent[plat] = {};
      platformTouched[plat] = {};
      PLATFORM_CONTENT_SCHEMA[plat].forEach((f) => { platformContent[plat][f.key] = ""; });
    });
    const dragOver = ref(false);
    const previewText = ref("");
    const previewChapters = ref([]);
    const previewMode = ref("card");

    const logLines = ref([]);
    const logBox = ref(null);
    const logOpen = ref(false);
    const logNew = ref(0);
    const toast = ref("");

    const modal = ref(null);
    let composeTimer = null;
    let composing = false;

    function markPlatformTouched(plat, field) {
      platformTouched[plat][field] = true;
    }

    // 按当前漫画信息实时组合各平台发布内容（不写盘），并回填留空的罗马音
    async function refreshCompose(forceNonTouched = false) {
      if (!comicDir.value.trim() || composing) return;
      composing = true;
      try {
        const r = await api("/api/compose", {
          method: "POST", json: true,
          body: JSON.stringify({ dir: comicDir.value.trim(), book: metaBook() }),
        });
        const romaji = r.romaji || {};
        if (r.language && !metaForm.language) metaForm.language = r.language;
        ["event_en", "author_en", "circle_en"].forEach((k) => {
          if (romaji[k] && !metaForm[k]) metaForm[k] = romaji[k];
        });
        if (romaji.title_en && !metaForm.title_en) metaForm.title_en = romaji.title_en;
        const pc = r.platforms_content || {};
        Object.keys(pc).forEach((plat) => {
          const values = pc[plat] || {};
          Object.keys(values).forEach((field) => {
            // 未手写字段跟随当前漫画信息自动组合；手写/已保存覆盖字段保留
            if (values[field] && !platformTouched[plat][field]
                && (forceNonTouched || !platformContent[plat][field])) {
              platformContent[plat][field] = values[field];
            }
          });
        });
      } catch (e) {
        // 组合失败不阻塞编辑（可能是漫画目录未加载完整）
      } finally {
        composing = false;
      }
    }

    function scheduleCompose() {
      clearTimeout(composeTimer);
      composeTimer = setTimeout(refreshCompose, 500);
    }

    // ---------------- AI 罗马音设置（config.yaml 顶层 ai 段） ----------------

    const aiForm = reactive({ enabled: false, base_url: "", api_key: "", model: "", timeout: 60 });
    const aiStatus = ref("");

    async function loadAi() {
      try {
        const r = await api("/api/ai");
        const ai = r.ai || {};
        aiForm.enabled = !!ai.enabled;
        aiForm.base_url = ai.base_url || "";
        aiForm.api_key = ai.api_key || "";
        aiForm.model = ai.model || "";
        aiForm.timeout = Number(ai.timeout) || 60;
      } catch (e) { /* 无 config.yaml 时静默用默认 */ }
    }

    async function aiSave() {
      busy.value = true;
      try {
        await api("/api/ai", { method: "POST", json: true, body: JSON.stringify({ ai: { ...aiForm } }) });
        aiStatus.value = "已保存" + (aiForm.enabled ? "（AI 转换已开启）" : "（AI 转换未开启）");
        toastMsg("AI 设置已保存");
      } catch (e) {
        toastMsg("保存失败：" + e.message);
      } finally {
        busy.value = false;
      }
    }

    async function aiTest() {
      busy.value = true;
      aiStatus.value = "测试中…";
      try {
        const r = await api("/api/ai/test", { method: "POST", json: true, body: JSON.stringify({ ai: { ...aiForm } }) });
        aiStatus.value = r.ok ? `测试通过：例大祭 → ${r.result}` : "测试失败：" + (r.error || "未知");
      } catch (e) {
        aiStatus.value = "测试失败：" + e.message;
      } finally {
        busy.value = false;
      }
    }

    function dictOpen() {
      api("/api/dict").then((r) => {
        const text = (r.rows || []).map(([k, v]) => `${k}=${v}`).join("\n");
        modal.value = { kind: "dict", title: "罗马音覆盖词典", text };
      }).catch((e) => toastMsg("读取词典失败：" + e.message));
    }

    const lanAddr = window.location.host;

    // ---------------- 主题（跟随系统/浅色/深色，localStorage 记忆） ----------------

    const theme = ref((() => {
      try { return localStorage.getItem("mu-theme") || "system"; } catch (e) { return "system"; }
    })());
    const themeLabel = computed(() =>
      theme.value === "system" ? "跟随系统" : theme.value === "light" ? "浅色" : "深色");
    const themeIcon = computed(() =>
      theme.value === "dark" ? "🌙" : theme.value === "light" ? "☀" : "◐");

    function applyTheme() {
      if (theme.value === "light" || theme.value === "dark") {
        document.documentElement.setAttribute("data-theme", theme.value);
      } else {
        document.documentElement.removeAttribute("data-theme");
      }
    }
    function cycleTheme() {
      const order = ["system", "light", "dark"];
      theme.value = order[(order.indexOf(theme.value) + 1) % order.length];
      try { localStorage.setItem("mu-theme", theme.value); } catch (e) {}
      applyTheme();
    }

    // ---------------- 工具 ----------------

    function toastMsg(msg) {
      toast.value = msg;
      setTimeout(() => { toast.value = ""; }, 3200);
    }

    function requiredCookies(card) {
      return (card.cookie_fields || []).filter((f) => f.required).map((f) => f.name);
    }

    function connected(card) {
      const p = config.platforms[card.key];
      if (!p || !p.enabled) return false;
      return requiredCookies(card).every((n) => (p.cookies || {})[n]);
    }

    const anyUnconfigured = computed(() =>
      cards.value.some((c) => !connected(c)));
    const publishTargetsText = computed(() => {
      const names = cards.value.filter(connected).map((c) => c.label.split("（")[0]);
      return "发布到：" + (names.length ? names.join("、") : "（未配置，去「平台账号」连接）");
    });

    function platShort(card) { return card.label.split("（")[0]; }

    function platStatus(card) {
      if (connected(card)) return "已连接";
      const p = config.platforms[card.key];
      if (!p || !p.enabled) return "未启用";
      const missing = requiredCookies(card).filter((n) => !(p.cookies || {})[n]);
      return missing.length ? "缺少 " + missing.join("/") : "已启用";
    }

    function extrasOf(card) {
      const out = {};
      (card.extras || []).forEach(([k, v]) => { out[k] = v; });
      return out;
    }
    function extraLabel(k) { return EXTRA_LABELS[k] || k; }

    function setStatus(key, ok, message) {
      statuses[key] = [{ ok, message }];
    }

    // ---------------- 初始化 ----------------

    async function loadState() {
      try {
        const st = await api("/api/state");
        version.value = st.version || "";
        note.value = st.note || "";
        cards.value = st.cards || [];
        Object.keys(config.common).forEach((k) => delete config.common[k]);
        Object.keys(config.platforms).forEach((k) => delete config.platforms[k]);
        Object.assign(config.common, st.config.common);
        for (const [k, v] of Object.entries(st.config.platforms || {})) {
          config.platforms[k] = v;
        }
        running.value = !!st.running;
      } catch (e) {
        toastMsg("加载配置失败：" + e.message);
      }
    }

    // ---------------- 日志（SSE） ----------------

    function connectLog() {
      // ?nosse：调试旁路（headless 截图等场景跳过长连接）
      if (new URLSearchParams(location.search).has("nosse")) return;
      const es = new EventSource("/api/events?token=" + encodeURIComponent(CSRF) + "&since=0");
      es.onmessage = (e) => {
        let d = null;
        try { d = JSON.parse(e.data); } catch (err) { return; }
        if (!d || typeof d.seq !== "number") return;
        logLines.value.push(d);
        if (logLines.value.length > 2000) logLines.value.splice(0, logLines.value.length - 2000);
        if (d.msg && /开始发布到/.test(d.msg)) running.value = true;
        if (d.msg && /发布完成：/.test(d.msg)) running.value = false;
        if (!logOpen.value) logNew.value += 1;
        scrollLog();
      };
      es.onerror = () => { /* EventSource 自动重连 */ };
    }

    function scrollLog() {
      nextTick(() => {
        if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight;
      });
    }
    function clearLog() { logLines.value = []; }

    // ---------------- 保存配置 ----------------

    function payload() {
      const out = { common: Object.assign({}, config.common), platforms: {} };
      for (const [k, v] of Object.entries(config.platforms)) {
        out.platforms[k] = {
          enabled: !!v.enabled,
          cookies: Object.assign({}, v.cookies),
          settings: Object.assign({}, v.settings),
        };
      }
      return out;
    }

    async function saveConfig() {
      busy.value = true;
      try {
        const r = await api("/api/config", { method: "POST", json: true, body: JSON.stringify({ config: payload() }) });
        toastMsg("已保存配置");
      } catch (e) {
        toastMsg("保存失败：" + e.message);
      } finally {
        busy.value = false;
      }
    }

    // ---------------- 平台账号 ----------------

    function openAccount(key) {
      expanded[key] = true;
      nav.value = "accounts";
    }
    function toggleExpand(key) {
      expanded[key] = !expanded[key];
    }
    function openLogin(card) { window.open(card.login_url, "_blank"); }

    function checkOne(key) { runCheck([key]); }
    function checkAll() { runCheck(null); }

    async function runCheck(names) {
      busy.value = true;
      try {
        const r = await api("/api/check", {
          method: "POST", json: true,
          body: JSON.stringify({ config: payload(), platforms: names || undefined }),
        });
        (r.results || []).forEach((res) => setStatus(res.platform, res.ok, res.message));
      } catch (e) {
        toastMsg("检查失败：" + e.message);
      } finally {
        busy.value = false;
      }
    }

    function pasteCookie(card) {
      modal.value = { kind: "paste", title: "粘贴整段 Cookie · " + card.label, key: card.key, text: "" };
    }

    // ---------------- B站扫码 ----------------

    function qrLogin(card) {
      modal.value = {
        kind: "qr", title: "B站扫码登录", key: card.key,
        qrUrl: "/api/qr/start?" + Date.now(), qrMsg: "等待扫码…",
      };
      pollQr();
    }

    function pollQr() {
      const timer = setInterval(async () => {
        if (!modal.value || modal.value.kind !== "qr") { clearInterval(timer); return; }
        try {
          const r = await api("/api/qr/status");
          if (r.status === "ok" && r.cookies) {
            const p = config.platforms.bilibili;
            for (const [k, v] of Object.entries(r.cookies)) p.cookies[k] = v;
            toastMsg("B站扫码登录成功");
            modal.value = null;
            clearInterval(timer);
          } else if (r.status === "expired") {
            modal.value.qrMsg = "二维码已失效，请重新打开";
            modal.value.qrUrl = "";
            clearInterval(timer);
          } else if (r.status === "confirmed") {
            modal.value.qrMsg = "已扫码，请在手机上确认";
          } else if (r.status === "idle") {
            clearInterval(timer);
          }
        } catch (e) { /* 网络抖动忽略 */ }
      }, 2000);
    }

    // ---------------- 代理 ----------------

    async function detectProxy() {
      try {
        const r = await api("/api/proxy/detect");
        if (r.url) {
          config.common.proxy_url = r.url;
          toastMsg("已填入检测到的代理：" + r.url);
        } else {
          toastMsg("未检测到系统代理");
        }
      } catch (e) {
        toastMsg("检测代理失败：" + e.message);
      }
    }

    // ---------------- e-hentai 上传表单字段 ----------------

    function fieldMapOpen(key) {
      const settings = config.platforms[key].settings || {};
      const rows = (settings.field_map || []).map((row) => splitSource(row));
      modal.value = {
        kind: "fieldmap", title: "上传表单填写配置 · e-hentai", key,
        rows: rows.length ? rows : [{ label: "", field: "", base: "title", param: "" }],
      };
    }

    function splitSource(row) {
      let source = row.source || "title";
      for (const [, token] of SOURCE_CHOICES) {
        if (source === token) return { label: row.label || "", field: row.field || "", base: token, param: "" };
        if (token.endsWith(":") && source.startsWith(token)) {
          return { label: row.label || "", field: row.field || "", base: token, param: source.slice(token.length) };
        }
      }
      return { label: row.label || "", field: row.field || "", base: "title", param: "" };
    }

    function onSourceChange(row) {
      if (row.base && !row.base.endsWith(":")) row.param = "";
    }

    // ---------------- 漫画 ----------------

    function pickDir() {
      api("/api/pick").then((r) => {
        if (r.ok && r.picked) { comicDir.value = r.picked; return loadComic(); }
        if (r.error) toastMsg(r.error);
      }).catch((e) => toastMsg("浏览失败：" + e.message));
    }

    function pickZip() {
      api("/api/pick?kind=file").then((r) => {
        if (r.ok && r.dir) { comicDir.value = r.dir; return loadComic(); }
        if (r.error) toastMsg(r.error);
      }).catch((e) => toastMsg("导入失败：" + e.message));
    }

    async function loadComic() {
      let raw = comicDir.value.trim();
      if (!raw) { toastMsg("请先填写漫画目录路径"); return; }
      busy.value = true;
      try {
        if (/\.(zip|cbz)$/i.test(raw)) {
          // 路径是压缩包 → 先自动导入（解压到导入缓存）再按目录加载
          const imp = await api("/api/import-path", {
            method: "POST", json: true, body: JSON.stringify({ path: raw }),
          });
          comicDir.value = imp.dir || raw;
          raw = comicDir.value.trim();
        }
        const r = await api("/api/load", {
          method: "POST", json: true, body: JSON.stringify({ dir: raw }),
        });
        summary.value = r;
        const m = (r.meta || {});
        metaForm.title = m.title || "";
        metaForm.author = m.author || "";
        metaForm.description = m.description || "";
        META_EXTRA.forEach((f) => { metaForm[f.key] = m[f.key] || ""; });
        if (!metaForm.language) metaForm.language = "Chinese";
        // 预填充：系列三字段留空时默认按东方系列填（仅表单，保存才写盘）
        if (!metaForm.series) metaForm.series = "东方";
        if (!metaForm.series_en) metaForm.series_en = "Touhou Project";
        if (!metaForm.series_jp) metaForm.series_jp = "東方Project";
        const pc = r.platforms_content || {};
        Object.keys(PLATFORM_CONTENT_SCHEMA).forEach((plat) => {
          const saved = pc[plat] || {};
          PLATFORM_CONTENT_SCHEMA[plat].forEach((f) => {
            platformContent[plat][f.key] = saved[f.key] || "";
            platformTouched[plat][f.key] = !!saved[f.key]; // 已存覆盖视为“用户意图”
          });
        });
        await refreshCompose(); // 各平台栏目立即显示自动组合结果
        await previewFull(); // 加载后自动弹出发布预览
      } catch (e) {
        summary.value = null;
        toastMsg("加载失败：" + e.message);
      } finally {
        busy.value = false;
      }
    }

    function resetPick() {
      summary.value = null;
      comicDir.value = "";
      previewChapters.value = [];
      previewText.value = "";
    }

    function metaBook() {
      // 只收集已知漫画信息字段（空串照传，后端负责删除）
      const book = { title: metaForm.title, author: metaForm.author, description: metaForm.description };
      META_EXTRA.forEach((f) => { book[f.key] = metaForm[f.key] || ""; });
      return book;
    }

    async function fillRomajiNames() {
      busy.value = true;
      try {
        const r = await api("/api/romaji", {
          method: "POST", json: true,
          body: JSON.stringify({ values: { event: metaForm.event, author: metaForm.author, circle: metaForm.circle } }),
        });
        const map = r.romaji || {};
        ["event_en", "author_en", "circle_en"].forEach((k) => { if (map[k]) metaForm[k] = map[k]; });
        toastMsg("已转罗马音：展会/作者/社团");
      } catch (e) {
        toastMsg("罗马音转换失败：" + e.message);
      } finally {
        busy.value = false;
      }
    }

    async function fillRomajiTitle() {
      busy.value = true;
      try {
        const r = await api("/api/romaji", {
          method: "POST", json: true,
          body: JSON.stringify({ values: { title_jp: metaForm.title_jp } }),
        });
        if (r.romaji && r.romaji.title_en) metaForm.title_en = r.romaji.title_en;
        toastMsg("已转罗马音标题");
      } catch (e) {
        toastMsg("罗马音转换失败：" + e.message);
      } finally {
        busy.value = false;
      }
    }

    // 预填系列（东方）：只填表单，需点「保存内容」写入 manga.json
    function prefillTouhouSeries() {
      metaForm.series = "东方";
      metaForm.series_en = "Touhou Project";
      metaForm.series_jp = "東方Project";
      toastMsg("已预填系列：东方 / Touhou Project / 東方Project（点“保存内容”写入）");
    }

    async function saveMeta() {
      if (!comicDir.value.trim()) return;
      busy.value = true;
      try {
        // 1) 先按当前漫画信息保存顶层字段
        const r = await api("/api/meta", {
          method: "POST", json: true,
          body: JSON.stringify({
            dir: comicDir.value.trim(),
            book: metaBook(),
          }),
        });
        // 2) 用最新漫画信息重新组合各平台发布内容并展示
        //    （未手写字段覆盖为新组合；用户手写的平台覆盖保留）
        await refreshCompose(true);
        // 3) 平台栏（含组合结果与手写覆盖）一并写回 manga.json
        await api("/api/meta", {
          method: "POST", json: true,
          body: JSON.stringify({
            dir: comicDir.value.trim(),
            book: {},
            platforms: JSON.parse(JSON.stringify(platformContent)),
          }),
        });
        toastMsg("内容已保存：漫画信息 + 各平台发布内容已更新");
        await loadComic();
      } catch (e) {
        toastMsg("保存失败：" + e.message);
      } finally {
        busy.value = false;
      }
    }

    function onDrop(e) {
      dragOver.value = false;
      if (busy.value) { toastMsg("正在导入中，请稍候…"); return; }
      const files = Array.from((e && e.dataTransfer && e.dataTransfer.files) || []);
      if (!files.length) return;
      const zips = files.filter((f) => /\.(zip|cbz)$/i.test(f.name));
      const imgs = files.filter((f) => /\.(jpg|jpeg|png|gif|webp)$/i.test(f.name));
      if (zips.length) {
        importUpload([zips[0]], null);
      } else if (imgs.length) {
        modal.value = {
          kind: "meta", title: "单本漫画信息",
          pending: imgs, title: "", author: "", desc: "",
        };
      } else {
        toastMsg("只支持 ZIP / CBZ 压缩包或 jpg/png/gif/webp 图片");
      }
    }

    async function importUpload(files, meta) {
      busy.value = true;
      try {
        const fd = new FormData();
        files.forEach((f) => fd.append("file", f));
        if (meta) {
          fd.append("meta_title", meta.title || "");
          fd.append("meta_author", meta.author || "");
          fd.append("meta_desc", meta.desc || "");
        }
        const r = await api("/api/import", { method: "POST", body: fd });
        comicDir.value = r.dir;
        await loadComic();
        toastMsg("已导入：请检查章节列表");
      } catch (e) {
        toastMsg("导入失败：" + e.message);
      } finally {
        busy.value = false;
      }
    }

    // ---------------- 预览 / 发布 ----------------

    async function previewPlan() { await runPreview("/api/plan", "生成计划失败"); }
    async function previewFull() { await runPreview("/api/preview", "全文预览失败"); }

    function pageUrl(chapterKey, index) {
      // max=0 → 后端原图直发（流式，不压缩不裁剪），逐页原样展示供核对
      return "/api/page?dir=" + encodeURIComponent(comicDir.value.trim()) +
        "&chapter=" + encodeURIComponent(chapterKey) +
        "&index=" + index + "&max=0";
    }

    async function runPreview(endpoint, errPrefix) {
      if (!comicDir.value.trim()) { toastMsg("请先加载漫画目录"); return; }
      busy.value = true;
      try {
        const r = await api(endpoint, {
          method: "POST", json: true,
          body: JSON.stringify({ dir: comicDir.value.trim(), config: payload() }),
        });
        previewText.value = r.text || "";
        previewChapters.value = r.chapters || [];
        previewMode.value = r.chapters && r.chapters.length ? "card" : "text";
        nav.value = "workbench";
      } catch (e) {
        toastMsg(errPrefix + "：" + e.message);
      } finally {
        busy.value = false;
      }
    }

    async function publish() {
      if (!comicDir.value.trim()) { toastMsg("请先加载漫画目录"); return; }
      const names = cards.value.filter(connected).map((c) => c.label.split("（")[0]);
      if (!names.length) { toastMsg("没有已连接的平台，请先到「平台账号」配置 Cookie"); return; }
      // 先把当前编辑内容落盘，保证发布的标题/正文与预览一致
      try {
        await api("/api/meta", {
          method: "POST", json: true,
          body: JSON.stringify({
            dir: comicDir.value.trim(),
            book: metaBook(),
          }),
        });
      } catch (e) {
        toastMsg("保存内容失败：" + e.message);
        return;
      }
      if (!window.confirm("确认发布到：" + names.join("、") + "？\n\n发布后不可撤销。")) return;
      busy.value = true;
      try {
        await api("/api/publish", {
          method: "POST", json: true,
          body: JSON.stringify({ dir: comicDir.value.trim(), config: payload() }),
        });
        running.value = true;
        nav.value = "workbench";
      } catch (e) {
        toastMsg("发布失败：" + e.message);
      } finally {
        busy.value = false;
      }
    }

    // ---------------- Modal 确定 ----------------

    async function modalOk() {
      const m = modal.value;
      if (!m) return;
      if (m.kind === "dict") {
        const rows = m.text.split("\n")
          .map((line) => line.trim())
          .filter(Boolean)
          .map((line) => {
            const i = line.indexOf("=");
            return i > 0 ? [line.slice(0, i).trim(), line.slice(i + 1).trim()] : null;
          })
          .filter(Boolean);
        try {
          await api("/api/dict", { method: "POST", json: true, body: JSON.stringify({ rows }) });
          toastMsg(`词典已保存：${rows.length} 条`);
          modal.value = null;
        } catch (e) {
          toastMsg("词典保存失败：" + e.message);
        }
        return;
      }
      if (m.kind === "paste") {
        const parsed = parseCookies(m.text);
        const keys = Object.keys(parsed);
        if (!keys.length) { toastMsg("没有解析到任何 Cookie（格式：k=v; k2=v2）"); return; }
        const p = config.platforms[m.key].cookies;
        // 小黑盒：整段 Cookie 原样保存（含登录态与设备标识），不做字段拆分
        if (m.key === "xiaoheihe") {
          const only = m.text.replace(/^[\s;]+|[\s;]+$/g, "").trim();
          if (!only) { toastMsg("Cookie 内容为空"); return; }
          p.cookie = only;
          // 顺带解析 heybox_id（可选，便于诊断）
          if (parsed.heybox_id && !("heybox_id" in p)) p.heybox_id = parsed.heybox_id;
          toastMsg("已保存小黑盒整段 Cookie（长度 " + only.length + "）");
          modal.value = null;
          return;
        }
        let filled = 0;
        keys.forEach((k) => { if (k in p) { p[k] = parsed[k]; filled++; } });
        toastMsg(`已填 ${filled} 个 Cookie 到 ${m.key}`);
        modal.value = null;
      } else if (m.kind === "meta") {
        if (!m.title.trim()) { toastMsg("标题不能为空"); return; }
        importUpload(m.pending, { title: m.title, author: m.author, desc: m.desc });
        modal.value = null;
      } else if (m.kind === "fieldmap") {
        const rows = m.rows
          .filter((r) => r.label || r.field || r.base)
          .map((r) => {
            let source = r.base || "title";
            if (source.endsWith(":")) source = source + (r.param || "");
            return { label: (r.label || "").trim(), field: (r.field || "").trim(), source };
          });
        config.platforms[m.key].settings.field_map = rows;
        toastMsg(`已保存 ${m.key} 的上传表单字段映射：${rows.length} 行`);
        modal.value = null;
        saveConfig();
      } else {
        modal.value = null;
      }
    }

    // ---------------- 启动 ----------------

    async function autoPreview() {
      // ?autopreview：调试/回归用，自动加载示例漫画并打开全文预览
      comicDir.value = "examples/my_comic";
      await loadComic();
      await runPreview("/api/preview", "全文预览失败");
    }

    onMounted(() => {
      applyTheme();
      loadState().then(() => {
        if (new URLSearchParams(location.search).has("autopreview")) autoPreview();
      });
      loadAi();
      connectLog();
      // 漫画信息变化 → 自动组合各平台发布内容并回填罗马音（本地引擎）。
      // 只监听“源字段”；代码回填 *_en 不会再次触发，避免循环请求。
      ["event", "author", "circle", "group", "title", "title_jp",
       "series", "series_en", "series_jp", "language", "tags",
       "chapter_name", "description"].forEach((key) => {
        watch(() => metaForm[key], scheduleCompose);
      });
      watch(logLines, scrollLog);
      watch(logOpen, (v) => { if (v) logNew.value = 0; });
    });

    return {
      nav, navItems, version, note, running, busy, cards, config, statuses, expanded,
      comicDir, summary, metaForm, dragOver, previewText, previewChapters, previewMode,
      resetPick, saveMeta,
      logLines, logBox, logOpen, logNew, clearLog, toast, modal, lanAddr,
      theme, themeLabel, themeIcon, cycleTheme,
      aiForm, aiStatus, aiSave, aiTest, dictOpen,
      PLAT_LABELS, pageUrl, META_EXTRA, META_EXTRA_EXTRA, PLATFORM_CONTENT_SCHEMA, platformContent,
      SOURCE_CHOICES, CATE_OPTIONS,
      markPlatformTouched,
      anyUnconfigured, publishTargetsText,
      platShort, platStatus, connected, extrasOf, extraLabel,
      saveConfig, openAccount, toggleExpand, openLogin,
      checkOne, checkAll, pasteCookie, qrLogin, detectProxy,
      fieldMapOpen, onSourceChange, pickDir, pickZip, loadComic, onDrop,
      fillRomajiNames, fillRomajiTitle, prefillTouhouSeries,
      previewPlan, previewFull, publish, modalOk,
    };
  },
}).mount("#app");
