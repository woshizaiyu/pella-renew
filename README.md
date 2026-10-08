# PellaFree 自动续期（GitHub Actions + 真浏览器）

> 基座模板：`../续期脚本参考示例/eooce/Auto-Renew-HidenCloud`
> （代理注入、CF 过盾、TG 通知、time.txt 保活照抄 eooce；平台逻辑按录制重写）
>
> Pella 续期必须走浏览器过广告链（cuty 24H + srnky 32H → Turnstile → 倒计时 → `/go/` → `/renew/<id>` 自动 claim），
> Cloudflare Worker 纯 `fetch` 无解。Worker 仅保留**状态检查 + 重启**（见 `../workers/`）。

## Secrets（仓库 Settings → Secrets and variables → Actions）

| Secret | 必填 | 说明 | 示例 |
|---|---|---|---|
| `EMAIL` | ✅ | Pella 邮箱 | `a@gmail.com` |
| `PASSWORD` | ✅（无 COOKIE 时） | Pella 密码 | |
| `COOKIE_VALUE` | ❌（一般不用配） | `__session` 值，先试用，401 自动回退账密；JWT 本体 60 秒命，定时任务基本都会回退 | `eyJhbGci…` |
| `TG_BOT_TOKEN` | ❌ | Telegram Bot Token | |
| `TG_CHAT_ID` | ❌ | Telegram Chat ID | |
| `NODE_LINK` | ❌ | 代理节点（vless/vmess/trojan/…完整分享链接，不配即直连） | |

## 定时

`10 */12 * * *`（广告链接 24h 一换，一天两遍兜底）。手动触发：Actions → Auto Renew PellaFree → Run workflow。

## Cookie 获取（可选，一般不用配）

Pella（Clerk）没有静态长效 Cookie：`__session` 壳 1 年、里面 JWT 60 秒。
定时任务每次都会过期回退到账密，所以有密码的话可直接不配。
真要配：登录 `www.pella.app` 后 F12 → 应用程序/存储 → Cookie → 复制 `__session` 的值填入 `COOKIE_VALUE`。

## 行为

- 单账号：Clerk API 登录拿 JWT → `renew/update` 刷新 → `server/info` 取 `renew_links`（未领取优先，双链全试）→ 浏览器过广告链 → 落 `/renew/<id>` 等待自动 claim → 复查 `claimed` + 到期时间 → TG 报告。
- 状态：`✅ 续期成功` / `⚠️ 部分成功` / `⏳ 无需续期`（全冷却，exit 0）/ `❌ 续期失败`（exit 1）。

## 本地演练

```bash
pip install patchright requests
python -m patchright install chrome
set EMAIL=a@gmail.com && set PASSWORD=p1 && python app.py
```
