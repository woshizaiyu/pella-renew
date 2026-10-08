# PellaFree 自动续期（GitHub Actions + 真浏览器）

> 基座模板：`../续期脚本参考示例/eooce/Auto-Renew-HidenCloud`
> （代理注入、CF 过盾、TG 通知、time.txt 保活照抄 eooce；平台逻辑按录制重写）
>
> Pella 续期必须走浏览器过广告链（cuty 24H + srnky 32H → Turnstile → 倒计时 → `/go/` → `/renew/<id>` 自动 claim），
> Cloudflare Worker 纯 `fetch` 无解。Worker 仅保留**状态检查 + 重启**（见 `../workers/`）。

## Secrets（仓库 Settings → Secrets and variables → Actions）

| Secret | 必填 | 说明 | 示例 |
|---|---|---|---|
| `EMAIL` | ✅（单账号） | Pella 邮箱（优先，配了就用它） | `a@gmail.com` |
| `PASSWORD` | ✅（单账号） | Pella 密码 | |
| `COOKIE_VALUE` | ❌ | `__session` 值，Cookie 优先直用，失效回退账密 | `eyJhbGci…` |
| `ACCOUNT` | ✅（无 EMAIL 时） | 多账号兼容，每行 `邮箱-----密码[-----cookie]` | `a@gmail.com-----p1` |
| `TG_BOT_TOKEN` | ❌ | Telegram Bot Token | |
| `TG_CHAT_ID` | ❌ | Telegram Chat ID | |
| `NODE_LINK` | ❌ | 代理节点（vless/vmess/trojan/…完整分享链接，不配即直连） | |

## 定时

`10 */12 * * *`（广告链接 24h 一换，一天两遍兜底）。手动触发：Actions → Auto Renew PellaFree → Run workflow。

## Cookie 获取（可选，eooce 基座 Cookie 优先策略）

登录 `www.pella.app` 后按 F12（或右键检查）→ 应用程序/存储 → Cookie → 复制 `__session` 的值
（即 Clerk JWT），拼到 `ACCOUNT` 行尾：`邮箱-----密码-----__session值`。
不配也行，脚本自动走账密 API 登录。

## 行为

- 每账号：Clerk API 登录拿 JWT → `renew/update` 刷新 → `server/info` 取 `renew_links`（未领取优先，双链全试）→ 浏览器过广告链 → 落 `/renew/<id>` 等待自动 claim → 复查 `claimed` + 到期时间 → TG 报告。
- 状态三态：`✅ 续期成功` / `广告冷却中`（全 claimed，exit 0）/ `❌ 续期失败`（exit 1）。

## 本地演练

```bash
pip install patchright requests
python -m patchright install chrome
set ACCOUNT=a@gmail.com-----p1 && python app.py
```
