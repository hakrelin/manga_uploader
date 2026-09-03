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

const EXTRA_LABELS = {
  forum: "目标吧名",
  category_label: "默认分类",
  cate: "作品类型",
  language_label: "画廊语言",
  langtype: "语言类型",
  title_jpn: "默认日文标题",
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
    const selectedChapters = ref([]);
    const dragOver = ref(false);
    const previewText = ref("");
    const previewChapters = ref([]);
    const previewMode = ref("card");

    const logLines = ref([]);
    const logBox = ref(null);
    const toast = ref("");

    const modal = ref(null);

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
      return names.length ? names.join("、") : "（无，请先到「平台账号」启用并填 Cookie）";
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
        scrollLog();
      };
      es.onerror = () => { /* EventSource 自动重连 */ };
    }

    function scrollLog() {
      nextTick(() => {
        if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight;
      });
    }

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

    async function loadComic() {
      if (!comicDir.value.trim()) { toastMsg("请先填写漫画目录路径"); return; }
      busy.value = true;
      try {
        const r = await api("/api/load", {
          method: "POST", json: true, body: JSON.stringify({ dir: comicDir.value.trim() }),
        });
        summary.value = r;
        selectedChapters.value = [];
      } catch (e) {
        summary.value = null;
        toastMsg("加载失败：" + e.message);
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
      return "/api/page?dir=" + encodeURIComponent(comicDir.value.trim()) +
        "&chapter=" + encodeURIComponent(chapterKey) +
        "&index=" + index + "&max=360";
    }

    async function runPreview(endpoint, errPrefix) {
      if (!comicDir.value.trim()) { toastMsg("请先加载漫画目录"); return; }
      busy.value = true;
      try {
        const r = await api(endpoint, {
          method: "POST", json: true,
          body: JSON.stringify({
            dir: comicDir.value.trim(),
            config: payload(),
            chapters: selectedChapters.value.length ? selectedChapters.value : undefined,
          }),
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
      if (!window.confirm("确认发布到：" + names.join("、") + "？\n\n发布后不可撤销。")) return;
      busy.value = true;
      try {
        await api("/api/publish", {
          method: "POST", json: true,
          body: JSON.stringify({
            dir: comicDir.value.trim(),
            config: payload(),
            chapters: selectedChapters.value.length ? selectedChapters.value : undefined,
          }),
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
      if (m.kind === "paste") {
        const parsed = parseCookies(m.text);
        const keys = Object.keys(parsed);
        if (!keys.length) { toastMsg("没有解析到任何 Cookie（格式：k=v; k2=v2）"); return; }
        const p = config.platforms[m.key].cookies;
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

    onMounted(() => {
      applyTheme();
      loadState();
      connectLog();
      watch(logLines, scrollLog);
    });

    return {
      nav, navItems, version, note, running, busy, cards, config, statuses, expanded,
      comicDir, summary, selectedChapters, dragOver, previewText, previewChapters, previewMode,
      logLines, logBox, toast, modal, lanAddr,
      theme, themeLabel, themeIcon, cycleTheme,
      PLAT_LABELS, pageUrl,
      SOURCE_CHOICES, CATE_OPTIONS,
      anyUnconfigured, publishTargetsText,
      platShort, platStatus, connected, extrasOf, extraLabel,
      saveConfig, openAccount, toggleExpand, openLogin,
      checkOne, checkAll, pasteCookie, qrLogin, detectProxy,
      fieldMapOpen, onSourceChange, pickDir, loadComic, onDrop,
      previewPlan, previewFull, publish, modalOk,
    };
  },
}).mount("#app");
