"""前端静态检查：Vue 模板能否编译 + app.js 语法（有 node 才跑）。

改了 index.html 很容易把模板写挂（少个闭合标签、v-else 顺序不对），
而这类错误只有打开页面才会暴露。这里用仓库里自带的 vue.global.prod.js
直接在 node 里编译模板，提前拦住。
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "manga_uploader" / "web"
NODE = shutil.which("node")

CHECK_JS = r"""
const fs = require("fs");
const vm = require("vm");
const [vuePath, htmlPath] = process.argv.slice(2);

function fakeElement() {
  return {
    style: {},
    _html: "",
    _attr: "",
    set innerHTML(value) {
      this._html = String(value);
      const m = /foo="([\s\S]*)"/.exec(this._html);
      this._attr = m ? m[1] : "";
    },
    get innerHTML() { return this._html; },
    get textContent() { return this._html; },
    get children() { return [{ getAttribute: () => this._attr }]; },
    setAttribute() {},
    appendChild() {},
  };
}

const sandbox = {
  console, setTimeout, clearTimeout,
  document: {
    querySelector: () => null,
    createElement: () => fakeElement(),
    head: { appendChild() {} },
    body: {},
  },
  navigator: { userAgent: "node" },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(vuePath, "utf8"), sandbox, { filename: "vue.js" });

const Vue = sandbox.Vue;
if (!Vue || typeof Vue.compile !== "function") {
  console.error("没有 Vue.compile");
  process.exit(2);
}

const html = fs.readFileSync(htmlPath, "utf8");
const start = html.indexOf('<div id="app"');
if (start < 0) {
  console.error("找不到 #app 根节点");
  process.exit(2);
}
const scriptStart = html.indexOf("<script>", start);
const template = html.slice(start, scriptStart > 0 ? scriptStart : html.length).trim();
try {
  Vue.compile(template);
} catch (err) {
  console.error("模板编译失败：" + err.message);
  process.exit(1);
}
console.log("ok");
"""


@unittest.skipUnless(NODE, "需要 node 才能跑前端静态检查")
class TestWebFrontend(unittest.TestCase):
    def test_vue_template_compiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "check.js"
            script.write_text(CHECK_JS, encoding="utf-8")
            proc = subprocess.run(
                [NODE, str(script), str(WEB / "vendor" / "vue.global.prod.js"), str(WEB / "index.html")],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        self.assertEqual(proc.returncode, 0, f"模板编译失败：{proc.stdout}{proc.stderr}")
        self.assertIn("ok", proc.stdout)

    def test_app_js_syntax(self):
        proc = subprocess.run(
            [NODE, "--check", str(WEB / "app.js")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(proc.returncode, 0, f"app.js 语法错误：{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
