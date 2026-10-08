#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PellaFree 自动续期（GitHub Actions + patchright 真浏览器过广告链）。

基座模板：../续期脚本参考示例/eooce/Auto-Renew-HidenCloud（照抄其工程骨架：
代理注入、CF 过盾 helpers、反检测浏览器启动、eooce 风 TG 通知、time.txt 保活）。
平台逻辑（Clerk 登录取 JWT、/server/info 拿 renew_links、过 cuty/srnky 广告链、
落 /renew/<id> 自动 claim）为 PellaFree 实测链路，证据见录制：
  agentscribe-bundle-pella.app_-_2026_10_8_11_25_38.json（renew_links 含 id 字段）
  agentscribe-bundle-cuttty.com_-_2026_10_8_11_45_03.json（Continue→Turnstile→倒计时→/go/）

Cloudflare Worker 只做检查+重启（见 ../workers/_worker.js），续期必须走本脚本：
cuty/srnky 广告链 + Turnstile 纯 fetch 无解。
"""

import os, re, sys, time, random
from datetime import datetime, timezone
import requests
try:
    from patchright.sync_api import sync_playwright
    USING_PATCHRIGHT = True
except ImportError:
    from playwright.sync_api import sync_playwright
    USING_PATCHRIGHT = False

# --- 环境变量（单账号，eooce 基座原样） ---
EMAIL        = os.environ.get('EMAIL') or ""
PASSWORD     = os.environ.get('PASSWORD') or ""
COOKIE_VALUE = os.environ.get('COOKIE_VALUE') or ""  # Pella __session JWT，Cookie 优先
TG_CHAT_ID   = os.environ.get('TG_CHAT_ID') or ""
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN') or ""

# --- 代理配置（由工作流 shell 脚本写入 $GITHUB_ENV）---
IS_PROXY      = os.environ.get('IS_PROXY', 'false').lower() == 'true'
PROXY_SERVER  = os.environ.get('PROXY_SERVER') or "http://127.0.0.1:1081"
REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None

# --- Pella 常量 ---
API = "https://api.pella.app"
WEB = "https://www.pella.app"
CLERK_API_VERSION = "2025-11-10"
CLERK_JS_VERSION = "5.125.3"  # Worker 已验证可登录
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'

# Cloudflare 整页挑战 / Turnstile 组件共用的 iframe 选择器（eooce 模板原样）
CF_IFRAME_SELECTOR = 'iframe[src*="challenges.cloudflare.com"]'

if sys.platform == 'win32':
    DEFAULT_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
else:
    DEFAULT_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'

def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)

STEALTH_JS = """
try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
} catch (e) {}
"""

def mask_email(email):
    if '@' in email:
        name, domain = email.split('@', 1)
        if len(name) > 4:
            return f"{name[:2]}****{name[-2:]}@{domain}"
        return f"{name}@{domain}"
    return email[:2] + '****'

def get_current_ip(proxy_server=None):
    proxies = {"http": proxy_server, "https": proxy_server} if (proxy_server and IS_PROXY) else None
    try:
        resp = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
        if resp.status_code == 200:
            return resp.text.strip()
        return "获取失败"
    except Exception as e:
        log(f"❌ 获取出口IP失败: {e}")
        return "获取失败"

def send_telegram_notification(title, status_lines, email, current_ip="未知"):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("⚠️ Telegram 未配置，跳过通知")
        return False
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    text = (
        f"{title}\n\n"
        f"{status_lines}\n"
        f"📧 账号: {mask_email(email)}\n"
        f"🌐 续期使用IP: {current_ip}\n"
        f"🕒 续期时间：{now}"
    )
    try:
        resp = requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                             json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
                             timeout=10, proxies=REQUESTS_PROXIES)
        if resp.status_code == 200:
            log("✅ Telegram 通知发送成功")
            return True
        log(f"❌ Telegram 通知失败: {resp.text[:200]}")
        return False
    except Exception as e:
        log(f"❌ Telegram 通知异常: {e}")
        return False

# ==================== CF 过盾 helpers（eooce 模板原样照抄） ====================

def _get_turnstile_token(page):
    try:
        return page.evaluate(
            """
            () => {
                try {
                    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                        let t = null;
                        try { t = window.turnstile.getResponse(); } catch (e) {}
                        if (t && t.length > 30) return t;
                        for (let i = 0; i < 5; i++) {
                            try { t = window.turnstile.getResponse(String(i)); } catch (e) { t = null; }
                            if (t && t.length > 30) return t;
                        }
                    }
                } catch (e) {}
                const inputs = document.querySelectorAll(
                    'input[name="cf-turnstile-response"], input[id$="_response"]'
                );
                for (const el of inputs) {
                    if (el.value && el.value.length > 30) return el.value;
                }
                return null;
            }
            """
        )
    except Exception:
        return None

def _find_visible_cf_frames(page):
    result = []
    try:
        for handle in page.query_selector_all(CF_IFRAME_SELECTOR):
            try:
                box = handle.bounding_box()
                if box and box['width'] > 10 and box['height'] > 10:
                    result.append(handle)
            except Exception:
                continue
    except Exception:
        pass
    return result

def _cf_checkbox_visible(page):
    for frame in page.frames:
        if 'challenges.cloudflare.com' not in (frame.url or ''):
            continue
        try:
            if frame.locator('input[type="checkbox"]:visible').count() > 0:
                return True
        except Exception:
            continue
    return False

def _click_cf_checkbox(page, frame_el=None):
    for frame in page.frames:
        if 'challenges.cloudflare.com' not in (frame.url or ''):
            continue
        try:
            frame.locator('input[type="checkbox"]').first.click(timeout=2500)
            return True
        except Exception:
            continue
    if frame_el is None:
        return False
    try:
        try:
            frame_el.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        box = frame_el.bounding_box()
        if not box:
            return False
        x = box['x'] + random.uniform(18, 34)
        y = box['y'] + box['height'] / 2 + random.uniform(-4, 4)
        page.mouse.move(x - random.uniform(30, 60), y + random.uniform(-8, 8))
        time.sleep(random.uniform(0.15, 0.4))
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.05, 0.2))
        page.mouse.down()
        time.sleep(random.uniform(0.04, 0.1))
        page.mouse.up()
        return True
    except Exception:
        return False

SECURITY_TITLE_HINTS = ('security verification', 'just a moment', 'attention required',
                        'checking your browser', '请稍候', 'please wait', 'one more step')

def _is_security_check_page(page):
    try:
        title = (page.title() or '').lower()
        if any(h in title for h in SECURITY_TITLE_HINTS):
            return True
    except Exception:
        return True
    try:
        return page.evaluate(
            """
            () => {
                const t = document.body ? document.body.innerText.slice(0, 5000).toLowerCase() : '';
                return t.includes('verify you are human') || t.includes('checking your browser')
                    || t.includes('security verification');
            }
            """
        )
    except Exception:
        return True

def _has_interstitial_iframe(page):
    try:
        for handle in page.query_selector_all(CF_IFRAME_SELECTOR):
            if '/turnstile/' not in (handle.get_attribute('src') or ''):
                return True
    except Exception:
        pass
    return False

def handle_cloudflare(page, timeout=240):
    def challenge_active():
        return _has_interstitial_iframe(page) or _is_security_check_page(page)
    if not challenge_active():
        for _ in range(3):
            time.sleep(1)
            if challenge_active():
                break
        else:
            return True
    log("🔒 检测到 Cloudflare 安全验证...")
    time.sleep(5)
    effective_timeout = timeout + (180 if timeout > 60 else 0)
    start_time = time.time()
    last_click = 0
    clear_rounds = 0
    while time.time() - start_time < effective_timeout:
        if not challenge_active():
            clear_rounds += 1
            if clear_rounds >= 2:
                return True
            time.sleep(1)
            continue
        clear_rounds = 0
        if time.time() - last_click > 6:
            frames = _find_visible_cf_frames(page)
            if frames:
                log("🖱️ 点击 Turnstile 验证......")
                if _click_cf_checkbox(page, frames[-1]):
                    last_click = time.time()
                    time.sleep(random.uniform(3, 5))
                    continue
        time.sleep(1)
    log("❌ 验证超时。")
    try:
        page.screenshot(path="security_timeout.png")
    except Exception:
        pass
    return False

def solve_modal_turnstile(page, timeout=90):
    """弹窗/验证页内嵌 Turnstile 处理（eooce 模板原样）：token 生成即通过，20s 无框视为无需验证。"""
    log("🛡️ 开始处理 Turnstile 验证...")
    start = time.time()
    last_click = 0
    clicks = 0
    no_frame_seconds = 0
    while time.time() - start < timeout:
        if _get_turnstile_token(page):
            log("✅ Turnstile 验证通过！")
            return True
        frames = _find_visible_cf_frames(page)
        if not frames:
            no_frame_seconds += 1
            if no_frame_seconds >= 20 and clicks == 0:
                log("ℹ️ 未检测到 Turnstile 验证框，无需验证。")
                return True
            time.sleep(1)
            continue
        no_frame_seconds = 0
        if clicks > 0 and time.time() - last_click > 4 and not _cf_checkbox_visible(page):
            time.sleep(2)
            if not _cf_checkbox_visible(page):
                log("✅ Turnstile 复选框已消失，视为验证通过！")
                return True
        if time.time() - last_click > 6 and _cf_checkbox_visible(page):
            log(f"🖱️ 点击 Turnstile 复选框（第 {clicks + 1} 次）...")
            if _click_cf_checkbox(page, frames[-1]):
                clicks += 1
                last_click = time.time()
                time.sleep(random.uniform(3, 5))
                continue
        if clicks == 0 and time.time() - start > 20 and time.time() - last_click > 6:
            if _click_cf_checkbox(page, frames[-1]):
                clicks += 1
                last_click = time.time()
                time.sleep(random.uniform(3, 5))
                continue
        time.sleep(1)
    if _get_turnstile_token(page):
        log("✅ Turnstile 验证通过！")
        return True
    log("❌ Turnstile 验证超时。")
    return False

def open_browser(p):
    """启动浏览器（eooce 模板原样）。返回 browser，调用方按账号建 context/page。"""
    proxy_arg = {"server": PROXY_SERVER} if IS_PROXY else None
    if USING_PATCHRIGHT:
        browser = p.chromium.launch(channel="chrome", headless=False,
                                    args=['--disable-infobars'], proxy=proxy_arg)
        return browser
    log("⚠️ 未安装 patchright（建议 pip install patchright），退回原生 playwright，过盾能力较弱")
    browser = p.chromium.launch(channel="chrome", headless=False,
                                args=['--no-sandbox', '--disable-blink-features=AutomationControlled',
                                      '--disable-infobars'], proxy=proxy_arg)
    return browser

def new_account_page(browser):
    ctx = browser.new_context(viewport=None if USING_PATCHRIGHT else {'width': 1920, 'height': 1080},
                              user_agent=DEFAULT_UA if not USING_PATCHRIGHT else None,
                              proxy={"server": PROXY_SERVER} if IS_PROXY else None)
    page = ctx.new_page()
    if not USING_PATCHRIGHT:
        page.add_init_script(STEALTH_JS)
    return ctx, page

# ==================== Pella API（Worker 登录逻辑移植） ====================

def api_login(email, password):
    """Clerk 账密登录，返回 JWT（即 __session cookie 值，60s 有效，用完即换）。
    每次用全新 Session：复用旧 Session 会因残留 __client 被 Clerk 判 session_exists (400)。"""
    session = requests.Session()
    try:
        r = session.post(
            f"https://clerk.pella.app/v1/client/sign_ins?__clerk_api_version={CLERK_API_VERSION}&_clerk_js_version={CLERK_JS_VERSION}",
            headers={'Content-Type': 'application/x-www-form-urlencoded',
                     'Origin': WEB, 'Referer': f'{WEB}/', 'User-Agent': UA},
            data={'locale': 'zh-CN', 'identifier': email, 'password': password, 'strategy': 'password'},
            timeout=20, proxies=REQUESTS_PROXIES)
        if r.status_code != 200:
            # Clerk 已登录态会回 400 session_exists，但包里常带可用 session，直接捡回来
            try:
                err_data = r.json()
            except Exception:
                err_data = {}
            for s in ((err_data.get('client') or {}).get('sessions') or []):
                jwt = (s.get('last_active_token') or {}).get('jwt')
                if jwt:
                    log("✅ 复用已存在会话")
                    return jwt
            log(f"❌ 登录失败: HTTP {r.status_code} {r.text[:200]}")
            return None
        data = r.json()
        sessions = (data.get('client') or {}).get('sessions') or []
        if sessions and sessions[0].get('last_active_token', {}).get('jwt'):
            return sessions[0]['last_active_token']['jwt']
        sid = (data.get('response') or {}).get('created_session_id')
        if not sid and sessions:
            sid = sessions[0].get('id')
        if not sid:
            log("❌ 登录成功但无 session")
            return None
        # touch 兜底
        r2 = session.post(
            f"https://clerk.pella.app/v1/client/sessions/{sid}/touch?__clerk_api_version={CLERK_API_VERSION}&_clerk_js_version={CLERK_JS_VERSION}",
            headers={'Content-Type': 'application/x-www-form-urlencoded',
                     'Origin': WEB, 'Referer': f'{WEB}/', 'User-Agent': UA},
            data={'active_organization_id': ''}, timeout=20, proxies=REQUESTS_PROXIES)
        if r2.status_code == 200:
            d2 = r2.json()
            tok = (d2.get('sessions') or [{}])[0].get('last_active_token', {}).get('jwt') \
                or (d2.get('last_active_token') or {}).get('jwt')
            if tok:
                return tok
        log("❌ 登录成功但无法获取 token")
        return None
    except Exception as e:
        log(f"❌ 登录异常: {e}")
        return None

def api_headers(jwt):
    return {'Authorization': f'Bearer {jwt}', 'Content-Type': 'application/json',
            'Origin': WEB, 'Referer': f'{WEB}/', 'User-Agent': UA}

def api_get_servers(session, jwt):
    try:
        r = session.get(f"{API}/user/servers", headers=api_headers(jwt),
                        timeout=20, proxies=REQUESTS_PROXIES)
        if r.status_code != 200:
            log(f"❌ 获取服务器列表失败: {r.status_code}")
            return []
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get('servers') or []
    except Exception as e:
        log(f"❌ 获取服务器列表异常: {e}")
        return []

def api_refresh_links(session, jwt, sid):
    try:
        session.post(f"{API}/server/renew/update?id={sid}", headers=api_headers(jwt),
                     json={}, timeout=20, proxies=REQUESTS_PROXIES)
    except Exception as e:
        log(f"⚠️ renew/update 异常（继续）: {e}")

def api_get_info(session, jwt, sid):
    try:
        r = session.get(f"{API}/server/info?id={sid}", headers=api_headers(jwt),
                        timeout=20, proxies=REQUESTS_PROXIES)
        if r.status_code != 200:
            return {}
        return r.json()
    except Exception as e:
        log(f"⚠️ 获取 info 异常: {e}")
        return {}

def parse_expiry(s):
    try:
        m = re.match(r'(\d{2}):(\d{2}):(\d{2})\s+(\d{2})/(\d{2})/(\d{4})', s or '')
        if not m:
            return None
        hh, mm, ss, dd, mo, yy = m.groups()
        return datetime(int(yy), int(mo), int(dd), int(hh), int(mm), int(ss),
                        tzinfo=timezone.utc).timestamp()
    except Exception:
        return None

def calc_remaining(expiry):
    ts = parse_expiry(expiry)
    if ts is None:
        return 'N/A'
    diff = ts - time.time()
    tot_h = int(abs(diff) // 3600)
    mins = int((abs(diff) % 3600) // 60)
    if diff <= 0:
        return f"已过期{tot_h}时{mins}分"
    days = int(abs(diff) // 86400)
    hrs = int((abs(diff) % 86400) // 3600)
    if days > 0:
        return f"{days}天{hrs}时{mins}分"
    return f"{tot_h}时{mins}分"

def ensure_pella_auth(ctx, jwt):
    """把新鲜 JWT 注入浏览器（Clerk __session 即 JWT 本体，录制已验证）。"""
    exp = int(time.time()) + 3600
    for name in ('__session', '__session_cZQ0p10T'):
        try:
            ctx.add_cookies([{'name': name, 'value': jwt, 'domain': 'www.pella.app',
                              'path': '/', 'expires': exp, 'httpOnly': False,
                              'secure': True, 'sameSite': 'Lax'}])
        except Exception:
            pass

# ==================== 广告链（录制三幕：Continue→Turnstile→倒计时→/go/） ====================

def click_first_visible(page, selectors, timeout_each=8000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout_each)
            loc.scroll_into_view_if_needed(timeout=3000)
            loc.click(timeout=5000)
            return sel
        except Exception:
            continue
    return None

def pass_one_link(page, ctx, session, email, password, cookie, server_id, link):
    """走完一条广告链并领奖。成功标准：info 里该条 claimed=True。"""
    alias = link.get('link', '')
    lid = link.get('id', '')
    reward = link.get('reward', '?')
    tag = f"{server_id[:8]}-{reward}H"
    log(f"➡ [{tag}] 打开广告链: {alias}")
    try:
        page.goto(alias, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log(f"❌ [{tag}] 广告页打开失败: {e}")
        return False
    handle_cloudflare(page, timeout=120)
    time.sleep(2)

    # 第1页："Please click the button below to continue." → 点 Continue（截图1）
    if not click_first_visible(page, ['button:has-text("Continue"):visible', '#submit-button']):
        page.screenshot(path=f"ad_no_continue_{tag}.png")
        log(f"❌ [{tag}] 第1页找不到 Continue 按钮")
        return False
    log(f"🖱️ [{tag}] 已点 Continue，进验证页")

    # 第2页：CF 盾自动过 + "verify that you are not a robot" → 点 I am not a robot（截图2）
    handle_cloudflare(page, timeout=90)
    try:
        page.wait_for_selector('button:has-text("I am not a robot"):visible', timeout=60000)
    except Exception:
        page.screenshot(path=f"ad_no_robot_{tag}.png")
        log(f"❌ [{tag}] 验证页未出现 I am not a robot")
        return False
    if not click_first_visible(page, ['button:has-text("I am not a robot"):visible', '#submit-button']):
        page.screenshot(path=f"ad_no_robot_{tag}.png")
        log(f"❌ [{tag}] I am not a robot 点击失败")
        return False
    log(f"🖱️ [{tag}] 已点 I am not a robot，进倒计时")
    solve_modal_turnstile(page, timeout=60)

    # 第3页："Please wait while your link is being prepared" 倒计时 → /go/ 跳转链接（截图3）
    go_found = False
    try:
        page.wait_for_selector('a[href*="/go/"]:visible', timeout=120000)
        go_found = True
    except Exception:
        pass
    if not go_found:
        page.screenshot(path=f"ad_no_go_{tag}.png")
        log(f"❌ [{tag}] 倒计时后未出现 /go/ 跳转链接")
        return False
    dest = page.locator('a[href*="/go/"]:visible').first.get_attribute('href') or ''
    if not dest:
        page.screenshot(path=f"ad_no_go_{tag}.png")
        log(f"❌ [{tag}] /go/ 链接无 href")
        return False
    if dest.startswith('/'):
        dest = page.url.rsplit('/', 1)[0] + dest
    log(f"🔗 [{tag}] 终点: {dest}")

    # 临用前换新鲜 JWT（60s 有效），再进领奖页（前端 /renew/:id 落页 2s 自动 claim）
    jwt = api_auth(session, email, password, cookie)
    if not jwt:
        log(f"❌ [{tag}] 领奖前登录失败")
        return False
    ensure_pella_auth(ctx, jwt)
    try:
        page.goto(dest, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log(f"⚠️ [{tag}] 跳转异常（继续观察）: {e}")
    handle_cloudflare(page, timeout=60)
    time.sleep(6)
    try:
        body = page.locator("body").inner_text()[:200].replace('\n', ' ')
        log(f"📝 [{tag}] 领奖页: {body}")
    except Exception:
        pass

    for i in range(4):
        time.sleep(5)
        jwt2 = api_auth(session, email, password, cookie) or jwt
        info = api_get_info(session, jwt2, server_id)
        for l in info.get('renew_links') or []:
            if l.get('id') == lid and l.get('claimed'):
                log(f"✅ [{tag}] 已领取")
                return True
    page.screenshot(path=f"claim_fail_{tag}.png")
    log(f"❌ [{tag}] 领奖未确认（claimed 仍 false）")
    return False

# ==================== 主流程（单账号） ====================

def main():
    log(f"🔍 凭证检测: COOKIE_VALUE={'已配置' if COOKIE_VALUE else '未配置'}, "
        f"EMAIL={'已配置' if EMAIL else '未配置'}, PASSWORD={'已配置' if PASSWORD else '未配置'}")
    if not COOKIE_VALUE and not (EMAIL and PASSWORD):
        log("❌ 缺少登录凭证")
        sys.exit(1)
    log(f"🔍 代理: {'开' if IS_PROXY else '关'}")
    current_ip = get_current_ip(PROXY_SERVER)
    log(f"🎯 当前出口IP: {current_ip}")
    with sync_playwright() as p:
        browser = None
        try:
            log("🚀 启动反检测内核浏览器...")
            browser = open_browser(p)
            email = EMAIL or "cookie账号"
            log(f"===== 账号: {mask_email(email)} =====")
            try:
                ok, status_lines = process_account(browser, EMAIL, PASSWORD, COOKIE_VALUE, current_ip)
            except Exception as e:
                log(f"❌ 账号异常: {e}")
                ok, status_lines = False, f"❌ 错误: {e}"
            send_telegram_notification("🎰 PellaFree 续期报告", status_lines, email, current_ip)
            sys.exit(0 if ok else 1)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
def api_auth(session, email, password, cookie=""):
    """Cookie 优先（eooce 基座策略）：__session JWT 直用，失效回退账密 API 登录。"""
    if cookie:
        log("📇 尝试 Cookie 登录...")
        try:
            r = session.get(f"{API}/user/servers", headers=api_headers(cookie),
                            timeout=20, proxies=REQUESTS_PROXIES)
            if r.status_code == 200:
                log("✅ Cookie 登录成功！")
                return cookie
            log(f"❌ Cookie 失效（HTTP {r.status_code}），回退账密")
        except Exception as e:
            log(f"⚠️ Cookie 登录异常，回退账密: {e}")
    if not password:
        return None
    log("🔑 账密登录...")
    return api_login(email, password)

def process_account(browser, email, password, cookie, current_ip):
    session = requests.Session()
    lines = []
    any_fail = False
    any_succ = False
    jwt = api_auth(session, email, password, cookie)
    if not jwt:
        return False, "❌ 错误: 登录失败"
    servers = api_get_servers(session, jwt)
    if not servers:
        return True, "⏳ 无需续期\n暂无服务器"
    ctx, page = new_account_page(browser)
    try:
        for srv in servers:
            sid = srv.get('id')
            name = srv.get('name') or sid[:8]
            ip = srv.get('ip') or 'N/A'
            jwt = api_auth(session, email, password, cookie) or jwt  # JWT 60s 有效，每台刷新
            api_refresh_links(session, jwt, sid)
            time.sleep(1)
            info = api_get_info(session, jwt, sid)
            before = info.get('expiry') or srv.get('expiry')
            links = info.get('renew_links') or []
            unclaimed = [l for l in links if not l.get('claimed')]
            if not links:
                lines.append(f"{name} | IP: {ip} | 剩余: {calc_remaining(before)} | 无可用广告")
                continue
            if not unclaimed:
                lines.append(f"{name} | IP: {ip} | 剩余: {calc_remaining(before)} | 广告冷却中")
                continue
            succ = 0
            for link in unclaimed:
                if pass_one_link(page, ctx, session, email, password, cookie, sid, link):
                    succ += 1
                time.sleep(1)
            jwt3 = api_auth(session, email, password, cookie) or jwt
            after = api_get_info(session, jwt3, sid).get('expiry') or before
            if succ:
                any_succ = True
                lines.append(f"{name} | IP: {ip} | 剩余: {calc_remaining(before)} → {calc_remaining(after)} | ✅成功({succ}/{len(unclaimed)})")
            else:
                any_fail = True
                lines.append(f"{name} | IP: {ip} | 剩余: {calc_remaining(after)} | ❌失败")
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    if any_succ and not any_fail:
        return True, "✅ 续期成功\n" + "\n".join(lines)
    if any_succ:
        return False, "⚠️ 部分成功\n" + "\n".join(lines)
    if any_fail:
        return False, "❌ 续期失败\n" + "\n".join(lines)
    return True, "⏳ 无需续期\n" + "\n".join(lines)

if __name__ == "__main__":
    main()
