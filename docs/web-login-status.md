# Web Login Status（kol.dwpc.com.cn 应用内登录取代 Nginx Basic Auth）

设计：`docs/plans/2026-09-13-web-login-design.md`（唯一真相）。
被部分取代的旧方案：`docs/plans/2026-09-12-kol-domain-access-design.md` 的 Basic Auth 一节。
本文件是这次上线的进度与证据真相。

## 交接摘要（2026-09-13 收口时写，下一个会话先读这里）

**状态：三步全部 completed。生产 `ef1688c5`，登录已启用，kol vhost 的 Basic Auth 已移除。**

用户现在这样访问站点：打开 `https://kol.dwpc.com.cn/` → 302 到 `/login` → 填用户名密码（浏览器
密码管理器会提示保存，下次自动填充）→ 拿到 30 天 Cookie；此后每次访问只要剩余寿命不足一半就
自动续期，因此持续使用不会掉线。

| 谁 | 走哪条路 | 结果 |
|---|---|---|
| 浏览器导航（GET + `Accept: text/html`）无 Cookie | 中间件 | 302 `/login?next=<path>` |
| fetch / SSE 无 Cookie | 中间件 | 401 JSON `{"detail":"authentication required"}` |
| `/login`、`/logout`、`/static/*` | 中间件豁免 | 照常 |
| 服务器本机 `127.0.0.1` 直连且无 `X-Forwarded-For` | 中间件豁免 | 照常（monitor / 诊断脚本靠这条） |
| 经 Nginx 来的任何请求 | 一定带 `X-Forwarded-For` | 一定要 Cookie |

**安全前提**：kol vhost 里的 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`
必须存在。没有它，代理来的请求会被当成本机直连而免登录。切换前已确认存在（原文件里本来就有，
本次未改动该行）。**同一前提对所有指向 `127.0.0.1:8000` 的 vhost 成立**：切换前已逐个审计
`/etc/nginx/conf.d/` 与 `nginx.conf`，只有 `kol-dwpc.conf` 和 `telegram-kol.conf` 代理到 8000，
两者都设了 `X-Forwarded-For`；`nginx.conf` 的默认 server 只发静态文件，`codex-api-http.conf`
代理到 3466，都不是绕过口。

**`telegram-kol.conf`（按 IP 的 80 端口 vhost）按设计未动**，它仍有自己的
`auth_basic` + `/etc/nginx/.telegram-kol.htpasswd`。从 `http://43.167.220.225/` 进来现在要过
两道：Basic Auth，然后应用登录。这是设计里"原来按 IP 访问的 80 端口 vhost 不动"的直接后果，
不是遗漏。

**凭据存放**：明文只在服务器 `/root/kol-web-login.txt`（0600 root:root）和交给用户本人的一次
转告里；仓库、日志、Telegram、命令行参数中都没有。hash 与 session secret 在
`/etc/telegram-kol-web.env`（0600 root:root）。

**回滚**（三层，各自独立，从外往里）：

```bash
# 1. 只回 Nginx —— 回到 Basic Auth + 应用登录同时生效
cp /etc/nginx/conf.d/kol-dwpc.conf.bak-20260913 /etc/nginx/conf.d/kol-dwpc.conf
nginx -t && systemctl reload nginx

# 2. 再回登录 —— 登录关闭，回到"部署了代码但未启用"的状态
cp /etc/telegram-kol-web.env.bak-20260913 /etc/telegram-kol-web.env
systemctl restart telegram-kol-web

# 3. 再回代码 —— 回到今天以前
/usr/local/bin/tg-deploy d386cbba8501a769a97fbd5edc395bd938dd2354
```

**未决 / 待办**：
1. htpasswd 文件 `/etc/nginx/.kol-dwpc.htpasswd` 按设计保留未删（第 1 层回滚需要）。它现在不被
   任何 vhost 引用；确认不再回滚后可由所有者删除。
2. 换密码：`web-login-password-hash` 生成新 hash → 改 env → `systemctl restart telegram-kol-web`。
   注意**换密码不会踢掉已登录会话**（旧 Cookie 仍有效到过期）；要立即踢下线必须同时换
   `TELEGRAM_KOL_WEB_SESSION_SECRET`。
3. 只有一个账号，没有账号管理界面；设计里也没有。
4. 切换瞬间用户浏览器里已打开的页面会开始收到 401（它每分钟轮询 `/api/monitor-status`）。
   刷新一次、登录即可，无需其它处理。

```yaml
project: web-login
integration_branch: codex/deepcoin-auto-trading-v1
deploy: tg-deploy <sha>（AGENTS.md 部署一节，四步）
risk_level: L1        # web 进程内新增认证层，不碰交易 / 识别 / worker / ingest
production_head_at_start: d386cbba8501a769a97fbd5edc395bd938dd2354   # 回滚 sha
deployed_sha: ef1688c5b4c4f291bafd10e57993502028a5f3f1               # 代码 + 用例，同一个 commit
candidate_branch: origin/claude/web-login
step_1_code_and_deploy: completed    # 2026-09-13T20:01Z
step_2_enable_login: completed       # 2026-09-13T20:02Z
step_3_nginx_cutover: completed      # 2026-09-13T20:05Z
env_backup: /etc/telegram-kol-web.env.bak-20260913
nginx_backup: /etc/nginx/conf.d/kol-dwpc.conf.bak-20260913
credential_file: /root/kol-web-login.txt        # 0600 root:root，明文只在这里
htpasswd_kept: /etc/nginx/.kol-dwpc.htpasswd    # 未删，回滚用
full_suite: 8689 passed / 0 failed              # 见"本地验证"一节
```

## 步骤总览

| 步 | 名称 | 状态 |
|---|---|---|
| 1 | 实现 + 用例 + tg-deploy（env 未设＝登录关闭，行为与今天相同） | **completed**：`ef1688c5`，回滚 `d386cbba` |
| 2 | 服务器写入三个 env 变量，重启 web，本机 curl 验证 | **completed**：web PID 1162482→1162819，ingest/worker PID 不变 |
| 3 | kol vhost 去掉 `auth_basic` 两行，reload，从本机外部验证 | **completed**：只删两行，`nginx -t` ok，reload |

## 本地验证（2026-09-13，工作树 `.claude/worktrees/agent-a4878a81e490e9bae`）

聚焦用例（最终候选 `ef1688c5` 的树）：

```
pytest tests/test_web_login.py tests/test_web_app.py tests/test_web_page_render.py \
       tests/test_web_assets_smoke.py tests/test_web_chat_api.py \
       tests/test_web_strategy_records.py tests/test_web_cli.py -q
→ 583 passed, 2 warnings in 88.80s
```

新增 `tests/test_web_login.py` 共 50 例，覆盖：登录关闭时一切照旧、部分配置抛 `ValueError`、
未登录导航 302 `/login?next=…`、未登录 fetch 401、错密码/错用户名重渲染且无 Set-Cookie、
失败日志带 IP 不带密码、对密码 302 + Set-Cookie（属性逐项断言）、HTTPS 下带 `Secure`、
带 Cookie 200、篡改 / 换密钥签名 / 过期 / 畸形 Cookie 被拒、过半寿命滑动续期、未过半不续期、
回环无 XFF 豁免（`TestClient(app, client=("127.0.0.1", 50000))`）、回环带 XFF 不豁免、
非回环不豁免、`next` 的 9 种取值（含 `//`、`/\`、绝对 URL）、已登录访问 `/login` 直接跳转、
logout 清 Cookie 后重新上锁、`/static` 缓存头未变、hash 助手往返与畸形 hash 不抛异常。

全量：

```
pytest -q -p no:randomly
→ 15 failed, 8674 passed, 4 skipped in 652.19s
```

15 个失败**全部**是同一个工作树环境原因，与本次改动无关：
`tests/test_minimal_server_updater.py` 与 `tests/test_server_update_scripts.py`
会 shell 出 `<repo>/.venv/bin/python`，而 git worktree 里没有 `.venv`，报
`FileNotFoundError: .../.claude/worktrees/agent-.../.venv/bin/python`。
把主检出的 `.venv` 软链进工作树后重跑这两个文件：

```
pytest tests/test_minimal_server_updater.py tests/test_server_update_scripts.py -q -p no:randomly
→ 46 passed, 1 skipped in 6.14s
```

因此最终候选的实际结果是 **8689 passed / 0 failed / 4 skipped**。
（这两个文件测的是 AGENTS.md 已注明退役的 stage/activate 流程，本次未改动其中任何文件。）

## 第 1 步证据：部署（2026-09-13T20:01Z 前后）

```
$ ssh tecent 'git -C /opt/telegram-kol-analyzer rev-parse HEAD'
d386cbba8501a769a97fbd5edc395bd938dd2354          # 回滚 sha

$ git merge-base --is-ancestor d386cbba ef1688c5 ; echo $?
0                                                  # 候选是生产的后代

$ git diff d386cbba ef1688c5 --name-only | sed -E '/^docs\//d; /\.md$/d'
src/telegram_kol_research/cli.py
src/telegram_kol_research/templates/login.html
src/telegram_kol_research/web_app.py
tests/test_web_login.py                            # 只有本任务的四个文件

$ git push origin HEAD:refs/heads/claude/web-login
 * [new branch]        HEAD -> claude/web-login

$ ssh tecent '/usr/local/bin/tg-deploy ef1688c5b4c4f291bafd10e57993502028a5f3f1'
HEAD: ef1688c5b4c4f291bafd10e57993502028a5f3f1
worker   MainPID=1162471 ActiveState=active
web      MainPID=1162482 ActiveState=active
ingest   MainPID=1162495 ActiveState=active

$ git push origin ef1688c5...:refs/heads/codex/deepcoin-auto-trading-v1
   b5dd6a61..ef1688c5                              # fast-forward，非 force

$ ssh tecent 'systemctl is-active telegram-kol-web telegram-kol-ingest telegram-kol-worker'
active / active / active
$ ssh tecent 'git -C /opt/telegram-kol-analyzer rev-parse HEAD'
ef1688c5b4c4f291bafd10e57993502028a5f3f1
```

部署后 env 未设，登录仍关闭，行为与部署前相同（本机 curl，模拟代理头）：

```
site_root_via_proxy_headers=200
monitor_status=200
login_page=302        # 登录关闭时 /login 直接跳回 next，不渲染表单
```

web journal 无 Traceback，`Application startup complete` / `Uvicorn running on http://127.0.0.1:8000`。

## 第 2 步证据：启用登录（2026-09-13T20:02Z）

```
$ ssh tecent 'cut -d: -f1 /etc/nginx/.kol-dwpc.htpasswd'
kol-admin                                          # 沿用用户已熟悉的用户名

$ ssh tecent 'cp -p /etc/telegram-kol-web.env /etc/telegram-kol-web.env.bak-20260913'
$ ssh tecent 'sha256sum /etc/telegram-kol-web.env /etc/telegram-kol-web.env.bak-20260913'
049bf207...cceee  /etc/telegram-kol-web.env        # 备份与原文件一致
049bf207...cceee  /etc/telegram-kol-web.env.bak-20260913
```

密码在服务器上用 `python3 -c 'import secrets;print(secrets.token_urlsafe(16))'` 生成，
只写进 `/root/kol-web-login.txt`（0600）。hash 由
`/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research web-login-password-hash`
从该文件经 **stdin 管道**读取生成（`PYTHONDONTWRITEBYTECODE=1`，未在发布目录留 bytecode），
明文从未出现在命令行参数里。session secret 用 `secrets.token_hex(32)`。三个变量**追加**到
env 文件末尾，原有的那一行注释未动：

```
$ ssh tecent 'sed -E "s/=.*/=<redacted>/" /etc/telegram-kol-web.env'
# Phase 6 role-scoped environment: web
TELEGRAM_KOL_WEB_LOGIN_USERNAME=<redacted>
TELEGRAM_KOL_WEB_LOGIN_PASSWORD_HASH=<redacted>
TELEGRAM_KOL_WEB_SESSION_SECRET=<redacted>
$ ssh tecent 'ls -l /etc/telegram-kol-web.env'
-rw------- 1 root root 302
```

（`scrypt$...` 里的 `$` 经 systemd `EnvironmentFile` 未被展开或改写 —— 由下面"对密码 → 302”
这条功能验证反证。）

```
$ ssh tecent 'systemctl restart telegram-kol-web'
$ ssh tecent 'systemctl is-active telegram-kol-web telegram-kol-ingest telegram-kol-worker'
active / active / active
$ ssh tecent 'systemctl show -p MainPID --value telegram-kol-{web,ingest,worker}'
web 1162482 → 1162819       # 只有 web 重启
ingest  1162495 → 1162495   # 不变
worker  1162471 → 1162471   # 不变
$ ssh tecent 'journalctl -u telegram-kol-web -n 30 | grep -ci traceback'
0
```

本机 curl 验证（`-H 'X-Forwarded-For: 203.0.113.1' -H 'X-Forwarded-Proto: https'` 模拟代理）：

| 请求 | 期望 | 实得 |
|---|---|---|
| `GET /`（Accept: text/html） | 302 `/login?next=/` | **302**，`Location: .../login?next=%2F` |
| `GET /api/monitor-status` | 401 | **401** |
| `GET /login` | 200 | **200** |
| `POST /login` 错密码 | 200 登录页，无 Set-Cookie | **200**，`set-cookie` 计数 0，页面含"用户名或密码错误" |
| `POST /login` 对密码 | 302 + Set-Cookie | **302** `Location: /`，`telegram_kol_web_session=v1.…` |
| 该 Cookie 的属性 | HttpOnly / Secure / SameSite=Lax / Path=/ / 30 天 | **HttpOnly, Secure, SameSite=lax, Path=/, Max-Age=2592000** |
| 带 Cookie `GET /` | 200 HTML | **200**，`text/html; charset=utf-8`，`<!DOCTYPE html>` |
| 带 Cookie `GET /api/monitor-status`、`GET /logs` | 200 | **200 / 200** |
| **不带 XFF 的本机** `GET /api/monitor-status` | 200（豁免生效） | **200** |

密码全程从 `/root/kol-web-login.txt` 读入 shell 变量再经 `--data-urlencode` 传入，未出现在
命令行明文里。

## 第 3 步证据：Nginx 切换（2026-09-13T20:05Z）

```
$ ssh tecent 'cp -p /etc/nginx/conf.d/kol-dwpc.conf /etc/nginx/conf.d/kol-dwpc.conf.bak-20260913'
$ ssh tecent 'grep -c "proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;" ...'
1                                                  # 安全前提，原本就有，未改
$ ssh tecent 'grep -c "proxy_set_header X-Forwarded-Proto \$scheme;" ...'
1

$ ssh tecent 'sed -i -E "/^[[:space:]]*auth_basic(_user_file)?[[:space:]]/d" /etc/nginx/conf.d/kol-dwpc.conf'
$ ssh tecent 'diff kol-dwpc.conf.bak-20260913 kol-dwpc.conf'
24,25d23
<     auth_basic "Telegram KOL";
<     auth_basic_user_file /etc/nginx/.kol-dwpc.htpasswd;
                                                   # 恰好只删这两行，别的一字未动

$ ssh tecent 'nginx -t'
syntax is ok / test is successful                  # 只有既有的 http2 deprecation warning
$ ssh tecent 'systemctl reload nginx'              # reload，不是 restart
$ ssh tecent 'systemctl is-active nginx codex-proxy telegram-kol-web telegram-kol-ingest telegram-kol-worker'
active / active / active / active / active
```

备份文件名后缀 `.bak-20260913` 不匹配 `include /etc/nginx/conf.d/*.conf`，不会被加载。

从本机（不是服务器）经公网验证：

| 请求 | 切换前 | 期望 | 实得 |
|---|---|---|---|
| `GET https://kol.dwpc.com.cn/` | 401（Basic） | 302 `/login` | **302** `→ .../login?next=%2F` |
| `GET https://kol.dwpc.com.cn/api/monitor-status` | 401（Basic） | 401 | **401**，且 `WWW-Authenticate` 头计数 **0**（Basic 挑战确已消失） |
| `GET https://kol.dwpc.com.cn/login` | 401（Basic） | 200 | **200** |
| `POST /login` 错密码 | — | 200 无 Cookie | **200**，`set-cookie` 计数 0，含"用户名或密码错误" |
| `POST /login` 对密码 | — | 302 + Set-Cookie | **302** `→ https://kol.dwpc.com.cn/`；`HttpOnly; Max-Age=2592000; Path=/; SameSite=lax; Secure` |
| 带 Cookie `GET /` | — | 200 HTML | **200**，`text/html; charset=utf-8` |
| 带 Cookie `GET /api/monitor-status` | — | 200 | **200** |
| `GET https://codex-api.dwpc.com.cn/v1/models` | 401 | 与切换前一致 | **401**（一致） |

未触发任何回滚。
