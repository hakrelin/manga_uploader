# 贴吧发帖修复 —— 排查笔记（2026-09-02/03）

## 最终结论（2026-09-03 已实现并真实验证）

用户反馈的“贴吧要求验证码或触发风控”是**误报**：
旧发帖 CGI 的真实响应是 `no:2000 / err_code:232000`（内容不合法），
`data.vcode.need_vcode=0`。旧代码用“响应里出现 vcode 字样”当风控，
把 232000 错报成验证码。

真正原因有两层，均已修复：
1. **表单编码**：旧 CGI 把表单正文按 GBK 解码，直接 POST UTF-8 会把
   吧名/正文变成乱码（fname 回显 “涓滄柟鍚” 即“东方吧”的 UTF-8 被
   当 GBK 读）。GBK 编码后中文正常，但 232000 仍在 —— 说明还有第二层。
2. **正文格式**：旧 CGI 已不再接受 `<img class="BDE_Image">` HTML，
   只认新版纯文本格式（含图片标记）。

因此发帖/回帖整体迁移到网页端新版 /c/ 接口（2026-09-03 真实验证成功）：
- 传图：`POST /c/s/uploadPicture_pc`（multipart + sign），
  响应含 `picId`，图片宽高取自 `picInfo.originPic`。
- 发帖：`POST /c/c/thread/add_pc`，成功返回 `{"error_code":"0","tid":...}`。
- 回帖：`POST /c/c/post/add_pc`（加楼层），成功返回 pid。
- 正文：简介按行 `\r\n` 连接，每张图追加
  `#(pic,<picId>,<宽>,<高>)`；请求带 `is_pictxt=1`、
  `ext={"is_hide":null,"needImage":"1"}`，表单 UTF-8。
- 签名：参数按 key 升序拼 `k=v`，末尾加 PC 密钥
  `36770b1f34c9bbf2e7d1a99d2b82fa9e` 再 MD5；带上
  `tbs/subapp_type=pc/_client_type=20`。
- Acs-Token/jt：实测不带（或带假值）也能发帖；只有响应
  `info.need_vcode=1` 时才需要浏览器内滑动验证码，报错文案改为
  “请在浏览器完成一次验证后重试”，不再误报风控。

## 已验证

- 真实发帖成功：1 图 + 简介 → `tid=10993427351`，`msg=发送成功`，
  移动端页面可见标题与文本（该测试帖已由用户手动删除）。
- mock 单测 6 项（帖吧）+ 全量 51 项通过。
- 回帖 `/c/c/post/add_pc` 未做真实帖验证（测试帖被删后无目标），
  下次真实发布超过 `max_pages_per_post`（默认 50 页）时顺带确认。

## 遗留说明

- 调试脚本 `_dbg_*` 已清理，不再提交。
- 若以后出现 `info.need_vcode=1`：贴吧是滑动验证码，GUI 无法直接
  “渲染图片让用户输入”，更合适的方式是让用户在浏览器登录态下完成
  一次验证后重试（报告文案已按此提示）。

---

## 结论：旧传图接口已死，网页端已迁移到新接口

- 旧接口 `POST https://tieba.baidu.com/cgi-bin/upload_image?from=tiebapc&tbs=...`
  始终被重定向到 gsp0 错误页（“贴吧404 / 不支持IE8以下浏览器”）。
  已排除：UA、`X-Requested-With`/`Accept` 头、Chrome TLS 指纹（curl_cffi）、
  Cookie 域（`.baidu.com`）、完整网页会话（TIEBA_SID）等，均可复现 → 该接口对脚本已不可用。

- 网页编辑器现在的上传接口（从 base.js / publish.js / pb.js 逆向确认）：
  - 上传图片：`POST https://tieba.baidu.com/c/s/uploadPicture_pc`（multipart）
  - 新发帖：`POST /c/c/thread/add_pc`（需要 banti 的 Acs-Token + jt，纯脚本成本高）
  - 旧发帖 CGI `https://tieba.baidu.com/f/commit/thread/add` 与
    `https://tieba.baidu.com/f/commit/post/add` 仍活着（空参 POST 返回 JSON 权限错误 230308），可继续使用。

## 已破译的关键细节

1. 所有 `/c/...` 请求会自动带上 `tbs`、`subapp_type=pc`、`_client_type=20` 并计算 `sign`。
2. sign 算法（PC 端）：
   - 参数按 key 升序（JS 默认字典序）拼接成 `k=v` 无分隔符的字符串；
   - 末尾追加 PC 密钥：`36770b1f34c9bbf2e7d1a99d2b82fa9e`；
   - 对整串取 MD5 hex，作为 `sign` 字段一起提交。
3. 上传字段（完整复刻网页端）：
   - `resourceId = md5("[object File]")` ← 网页 JS 直接对 File 对象做 md5，恒为常量，
     实测 `709d1d31dc47636e4f5ccbfd07601c19`（用真实文件内容 md5 也能过，字段按网页端复刻最稳）
   - `isFinish=1`, `saveOrigin=1`, `size=文件字节数`,
     `width=120`, `height=120`, `chunkNo=1`, `pic_water_type=3`,
     `chunk=<文件二进制>`（multipart 字段名就是 chunk）
   - 再带上 `tbs`、`subapp_type=pc`、`_client_type=20`、`sign`
4. 成功响应：
   ```json
   {"resourceId":"...","chunkNo":"1","picId":"301522372501",
    "picInfo":{"originPic":{"width":"1","height":"1","picUrl":"http://tiebapic.baidu.com/forum/pic/item/xxx.jpg?tbpicau=..."},
               "bigPic":{...},"smallPic":{...}},
    "error_code":"0","error_msg":"sucess"}
   ```
   发帖内容里的图用 `picInfo.originPic.picUrl`（或 bigPic）。
5. Cookie 坑：BDUSS 只能以 host-only 方式挂在 tieba.baidu.com（requests 默认行为即可）；
   不要手动指定 domain `.baidu.com`（curl_cffi 下会失效，tbs 的 is_login 变 0）。
6. 实测：用上述字段+签名上传合法 1px PNG 成功（error_code=0，不产生帖子）。

## 明天要做

1. 改 `manga_uploader/publishers/tieba.py`：
   - `_upload_image` 换成 `uploadPicture_pc`（multipart + 上述字段 + sign）；
   - 解析 `error_code==0`，返回 `picInfo.originPic.picUrl`；
   - 失败时把 `error_msg` 原样抛给用户（如格式不合法 2230211、系统错误 210009）；
   - 发帖/回帖沿用旧 commit CGI（POST 直接 https，不要被 301 带成 GET）。
2. 更新 `tests/test_tieba_mock.py`：mock `/uploadPicture_pc`，校验 multipart 字段与 sign 存在。
3. 跑全量测试（pytest / unittest）。
4. 需要用户在场时真实发一帖验证全链路（会真实产生帖子）。
5. 若旧 CGI 发帖遇风控（230308 权限 / 验证码），再评估新 `/c/c/thread/add_pc`
   （需实现 banti paris：`parisInstance.getAcsTokenWithBantiSend` + `sendBantiReport`，复杂，优先提示手动）。
6. 清理 `_dbg_tieba_*.py` / `_dbg_tieba_browser.js` 等临时脚本。
7. git 提交推送（config.yaml 在 .gitignore，严禁提交；工作区还有未提交的
   bilibili/ehentai/再漫画修复，可一并测试后推送）。

## 调试脚本清单（清理前保留）

`_dbg_tieba_upload.py`、`_dbg_tieba_probe.py`、`_dbg_tieba_domain.py`、
`_dbg_tieba_cffi.py`、`_dbg_tieba_follow.py`、`_dbg_tieba_js.py`、
`_dbg_tieba_uploadurl.py`、`_dbg_tieba_indexjs.py`、`_dbg_tieba_ctx.py`、
`_dbg_tieba_module.py`、`_dbg_tieba_consumers.py`、`_dbg_tieba_sign.py`、
`_dbg_tieba_findmod.py`、`_dbg_tieba_sign2.py`、`_dbg_tieba_xt.py`、
`_dbg_tieba_carm.py`、`_dbg_tieba_publishjs.py`、`_dbg_tieba_fields.py`、
`_dbg_tieba_resource.py`、`_dbg_tieba_find1232.py`、`_dbg_tieba_newupload.py`、
`_dbg_tieba_exact.py`（这个可直接改造成回归验证脚本）、`_dbg_tieba_addpc.py`、
`_dbg_tieba_addchunks.py`、`_dbg_tieba_cgi.py`、`_dbg_tieba_postprobe.py`、
`_dbg_tieba_browser.js`
