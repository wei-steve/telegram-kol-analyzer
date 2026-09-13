# Web 登录：应用内登录页 + 长效签名 Cookie（替换 kol.dwpc.com.cn 的 Nginx Basic Auth）

## 目标

用户在 `https://kol.dwpc.com.cn` 上希望：浏览器能记住账号密码，并且登录状态长期保持，不用每次重新输入。

现状：站点由 Nginx Basic Auth 保护（`docs/plans/2026-09-12-kol-domain-access-design.md`），
应用本身没有登录。Basic Auth 由浏览器弹窗输入，每个浏览器会话都会再问一次，
移动端尤其烦；密码管理器对它的支持也参差不齐。

## 方案

把认证边界从 Nginx 移到应用：

1. **登录页 `GET /login`**：标准 HTML 表单，`<input name="username" autocomplete="username">` 和
   `<input type="password" name="password" autocomplete="current-password">`，浏览器密码管理器
   因此能保存并自动填充账号密码（这就是"记住账号密码"）。页面独立、不加载 `app.js`。
2. **`POST /login`**：校验用户名（`hmac.compare_digest`）和密码（`hashlib.scrypt`，盐随机，
   比较用 `compare_digest`）。成功则签发 Cookie 并 302 到 `next`（只允许站内相对路径，默认 `/`）。
   失败：`asyncio.sleep(1)` 后重渲染登录页并写一行 warning 日志（带客户端 IP，不带密码）。
3. **会话 Cookie**：名 `telegram_kol_web_session`，值 `v1.<exp_unix>.<nonce>.<hmac_sha256_hex>`，
   签名密钥来自环境变量。`HttpOnly; SameSite=Lax; Path=/; Max-Age=30 天`；请求经 HTTPS
   （`X-Forwarded-Proto: https` 或 `request.url.scheme == "https"`）时加 `Secure`。
   **滑动续期**：Cookie 有效且剩余寿命不足一半时，在响应里重新签发。这就是"登录状态长期保持"。
4. **中间件**：登录启用时，除下列豁免外一律要求有效 Cookie：
   - `/login`、`/logout`、`/static/*`；
   - 客户端是回环地址（`127.0.0.1`/`::1`）**且**没有 `X-Forwarded-For` 头 —— 这是仓库里
     `require_monitor_capture_auth` 等本地工具接口已有的"本机直连"判定，服务器上的 monitor /
     诊断脚本靠它继续工作。经 Nginx 代理来的请求带 `X-Forwarded-For`，因此永远要登录。
   未通过时：浏览器导航（`GET` 且 `Accept` 含 `text/html`）302 到 `/login?next=<path>`；
   其他（fetch / SSE）返回 `401` JSON。
5. **`POST /logout`**：清 Cookie，302 到 `/login`。
6. **配置**（都放 `/etc/telegram-kol-web.env`，随 `EnvironmentFile` 进入 web 进程）：
   - `TELEGRAM_KOL_WEB_LOGIN_USERNAME`
   - `TELEGRAM_KOL_WEB_LOGIN_PASSWORD_HASH` —— 格式 `scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`
   - `TELEGRAM_KOL_WEB_SESSION_SECRET` —— 至少 32 字节随机 hex
   - `TELEGRAM_KOL_WEB_SESSION_DAYS` —— 可选，默认 30
   三个必填项**全部缺席**＝登录关闭（今天的行为，本地开发和全部现有测试不受影响）；
   **部分缺席**＝配置错误，`create_app` 直接抛 `ValueError`，进程不起来，`systemctl status` 可见。
7. **CLI 助手** `telegram-kol-research web-login-password-hash`：从 stdin/getpass 读密码，
   输出上面格式的 hash，不回显密码。用于初始化和以后换密码。
8. **Nginx**：`kol.dwpc.com.cn` 的 vhost 去掉 `auth_basic` / `auth_basic_user_file`；确认
   `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` 和 `X-Forwarded-Proto $scheme;`
   存在（缺就补）。原来按 IP 访问的 80 端口 vhost 不动。

无新依赖：全部用标准库（`hashlib.scrypt`、`hmac`、`secrets`、`urllib.parse`）。
**表单体用 `urllib.parse.parse_qs(await request.body())` 解析，不要用 `fastapi.Form` /
`request.form()`**：那需要 `python-multipart`，仓库没装，`tg-deploy` 也不装依赖。

## 上线顺序（每步都可回退）

1. 代码合入并 `tg-deploy`。此时 env 未设，登录关闭，站点仍由 Basic Auth 保护，行为与今天相同。
2. 服务器上写入三个 env 变量（hash 由 CLI 助手生成；明文密码只交给用户本人，
   不进仓库、日志、Telegram），`systemctl restart telegram-kol-web`。
   验证（仍带 Basic Auth）：`GET /` → 302 `/login`；错密码 → 重渲染；对密码 → Set-Cookie；
   带 Cookie `GET /` → 200。
3. Nginx 去掉 Basic Auth，`nginx -t` 后 `systemctl reload nginx`。
   验证（不带任何凭据）：`GET https://kol.dwpc.com.cn/` → 302 `/login`；
   `GET https://kol.dwpc.com.cn/api/monitor-status` → 401；带 Cookie → 200。
   `codex-api.dwpc.com.cn` 行为不变；ingest / worker 进程 PID 不变。
4. 回退：把 `auth_basic` 两行加回去并 reload，即回到第 1 步之后的状态；再回退 tg-deploy 即回到今天。

## 验收

- 浏览器第一次访问看到登录页，浏览器提示保存密码；下次访问自动填充。
- 登录后 30 天内再访问不再要求登录；期间每次访问都把有效期往后推。
- 服务器上的 monitor / 本地诊断脚本（`127.0.0.1` 直连，带 token）不受影响。
- `pytest tests/test_web_*.py` 与新增 `tests/test_web_login.py` 全绿。
