# PellaFree 自动续期（GitHub Actions + 真浏览器）

> 基座模板：`../续期脚本参考示例/eooce/Auto-Renew-HidenCloud`
> （代理注入、CF 过盾、TG 通知、time.txt 保活照抄 eooce；平台逻辑按录制重写）
>
> Pella 续期必须走浏览器过广告链（cuty 24H + srnky 32H → Turnstile → 倒计时 → `/go/` → `/renew/<id>` 自动 claim），
> Cloudflare Worker 纯 `fetch` 无解。Worker 仅保留**状态检查 + 重启**（见 `../workers/`）。

## Secrets（仓库 Settings → Secrets and variables → Actions）

| Secret | 必填 | 说明 | 示例 |
|---|---|---|---|
| `ACCOUNT` | ✅ | 多账号，每行 `邮箱-----密码` | `a@gmail.com-----p1`<br>`b@gmail.com-----p2` |
| `TG_BOT_TOKEN` | ❌ | Telegram Bot Token | |
| `TG_CHAT_ID` | ❌ | Telegram Chat ID | |
| `NODE_LINK` | ❌ | 代理节点（vless/vmess/trojan/…完整分享链接，不配即直连） | |

## 定时

`10 */12 * * *`（广告链接 24h 一换，一天两遍兜底）。手动触发：Actions → Auto Renew PellaFree → Run workflow。

## 行为

- 每账号：Clerk API 登录拿 JWT → `renew/update` 刷新 → `server/info` 取 `renew_links`（未领取优先，双链全试）→ 浏览器过广告链 → 落 `/renew/<id>` 等待自动 claim → 复查 `claimed` + 到期时间 → TG 报告。
- 状态三态：`✅ 续期成功` / `广告冷却中`（全 claimed，exit 0）/ `❌ 续期失败`（exit 1）。

## 本地演练

```bash
pip install patchright requests
python -m patchright install chrome
set ACCOUNT=a@gmail.com-----p1 && python app.py
```
