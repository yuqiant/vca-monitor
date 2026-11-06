#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Van Cleef & Arpels 补货监控（Python + Playwright）

判定规则（页面 HTML 里两个按钮一直同时存在，所以只看“真正可见”的那个）：
  - ADD TO BAG 可见            -> 有货：Bark 紧急来电推送 + Gmail 邮件，退出码 0
  - 只有 ORDER BY PHONE 可见   -> 没货：退出码 0
  - 两个都看不到               -> 页面异常（被拦截/改版）：保存截图，退出码 1

用法：
  python monitor.py                 # 正常检测
  python monitor.py --test-email    # 只发一封测试邮件，不打开网页
  python monitor.py --test-bark     # 只发一条测试推送（紧急+来电），不打开网页
  python monitor.py --save-screenshot   # 正常检测，并且无论结果都存一张截图

需要的环境变量（在 GitHub 里是 Secrets，代码里不写死任何密码）：
  BARK_KEY            Bark 的设备 Key
  GMAIL_USER          发件 Gmail 地址
  GMAIL_APP_PASSWORD  Gmail 应用专用密码（16 位）
可选：
  MAIL_TO             收件邮箱（默认 yuqiant07@gmail.com）
  BARK_SERVER         自建 Bark 服务器地址（默认 https://api.day.app）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path

# ----------------------------- 配置区 -----------------------------
PRODUCT_URL = (
    "https://www.vancleefarpels.com/us/en/collections/jewelry/alhambra/"
    "vcard35600---vintage-alhambra-bracelet-5-motifs.html"
  
)
PRODUCT_NAME = "Vintage Alhambra bracelet, 5 motifs (VCARD35600)"
IN_STOCK_TEXT = "ADD TO BAG"          # 有货按钮文字
OUT_OF_STOCK_TEXT = "ORDER BY PHONE"  # 没货按钮文字
DEFAULT_MAIL_TO = "yuqiant07@gmail.com"

PAGE_TIMEOUT_MS = 60_000   # 打开网页最长等待
VERDICT_WAIT_S = 30        # 等按钮出现的最长秒数
MAX_ATTEMPTS = 3           # 页面没出现任何按钮时，最多重试几次
SCREENSHOT_DIR = Path("screenshots")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

IN_STOCK, OUT_OF_STOCK, UNKNOWN = "IN_STOCK", "OUT_OF_STOCK", "UNKNOWN"

# 候选元素：按钮 / 链接 / role=button
CLICKABLE = "button, a, [role='button'], input[type='submit'], input[type='button']"

# 在浏览器里判断一个元素“人眼真的能看到吗”
JS_REALLY_VISIBLE = """
el => {
  if ((el.textContent || '').trim().length > 60) return false;   // 排除包着大段内容的外层容器
  const r = el.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return false;                  // 没有实际尺寸
  if (r.right + window.scrollX <= 0 || r.bottom + window.scrollY <= 0) return false; // 被挪到屏幕外
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    const s = getComputedStyle(n);
    if (s.display === 'none') return false;
    if (s.visibility === 'hidden' || s.visibility === 'collapse') return false;
    if (parseFloat(s.opacity) === 0) return false;
    if (n.hasAttribute('hidden')) return false;
    if (n !== document.body && n !== document.documentElement) {
      const clip = s.overflow !== 'visible' || s.overflowX !== 'visible' || s.overflowY !== 'visible';
      if (clip) {                                                  // 被折叠成 0 高/0 宽并裁剪
        const a = n.getBoundingClientRect();
        if (a.width < 2 || a.height < 2) return false;
      }
    }
  }
  return true;
}
"""


# ----------------------------- 小工具 -----------------------------
def log(msg: str) -> None:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}Z] {msg}", flush=True)


def local_time_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        t = dt.datetime.now(ZoneInfo("America/Los_Angeles"))
        return t.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def need_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量/Secret：{name}（请在 GitHub Secrets 里添加）")
    return value


def write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass


def text_pattern(text: str) -> re.Pattern:
    """'ADD TO BAG' -> 不区分大小写、空白宽松匹配的正则"""
    return re.compile(r"\s+".join(re.escape(w) for w in text.split()), re.I)


# ----------------------------- 通知 -----------------------------
def send_bark(title: str, body: str) -> None:
    """Bark 紧急推送：level=critical（突破静音/勿扰/专注）+ call=1（持续响铃约 30 秒）"""
    key = need_env("BARK_KEY").rstrip("/").split("/")[-1]  # 兼容误贴整条 URL
    server = os.environ.get("BARK_SERVER", "").strip() or "https://api.day.app"
    payload = {
        "device_key": key,
        "title": title,
        "body": body,
        "level": "critical",
        "call": "1",
        "volume": 10,
        "sound": "alarm",
        "group": "restock",
        "url": PRODUCT_URL,
    }
    req = urllib.request.Request(
        server.rstrip("/") + "/push",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Bark 返回 HTTP {e.code}：{e.read().decode('utf-8', 'replace')[:200]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Bark 网络错误：{e.reason}")
    try:
        code = json.loads(raw).get("code")
    except ValueError:
        code = None
    if code != 200:
        raise RuntimeError(f"Bark 推送失败：{raw[:200]}")


def send_email(subject: str, text_body: str, html_body: str | None = None) -> None:
    user = need_env("GMAIL_USER")
    password = need_env("GMAIL_APP_PASSWORD").replace(" ", "")
    to_addr = os.environ.get("MAIL_TO", "").strip() or DEFAULT_MAIL_TO

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


def build_alert_email(test: bool = False) -> tuple[str, str, str]:
    prefix = "【测试】" if test else "🔔 有货了！"
    subject = f"{prefix}Van Cleef & Arpels {PRODUCT_NAME}"
    when = local_time_str()
    text = (
        f"{'这是一封测试邮件，邮件通道工作正常。' if test else '检测到“' + IN_STOCK_TEXT + '”按钮已出现，商品可能已补货！'}\n\n"
        f"商品：{PRODUCT_NAME}\n"
        f"链接：{PRODUCT_URL}\n"
        f"检测时间：{when}\n"
    )
    html = (
        f"<p>{'这是一封测试邮件，邮件通道工作正常。' if test else '检测到“<b>' + IN_STOCK_TEXT + '</b>”按钮已出现，商品可能已补货！'}</p>"
        f"<p>商品：{PRODUCT_NAME}<br>检测时间：{when}</p>"
        f'<p><a href="{PRODUCT_URL}" style="display:inline-block;padding:12px 20px;'
        f'background:#111;color:#fff;text-decoration:none;border-radius:4px">立即打开商品页</a></p>'
    )
    return subject, text, html


def notify_in_stock() -> int:
    """有货：Bark + 邮件各发一次，返回成功的通道数。两个通道互不影响。"""
    ok = 0
    try:
        send_bark("🔔 Van Cleef 有货了！", f"{PRODUCT_NAME} 出现 {IN_STOCK_TEXT}，马上去抢！")
        log("Bark 紧急推送已发送")
        ok += 1
    except Exception as e:  # noqa: BLE001
        log(f"❌ Bark 推送失败：{e}")
    try:
        subject, text, html = build_alert_email()
        send_email(subject, text, html)
        log("邮件已发送")
        ok += 1
    except Exception as e:  # noqa: BLE001
        log(f"❌ 邮件发送失败：{e}")
    return ok


# ----------------------------- 检测 -----------------------------
def count_visible(page, text: str) -> tuple[int, int]:
    """返回 (真正可见的数量, 页面里文字匹配的总数)"""
    cands = page.locator(CLICKABLE).filter(has_text=text_pattern(text))
    total = cands.count()
    visible = 0
    for i in range(total):
        el = cands.nth(i)
        try:
            if el.is_visible() and el.evaluate(JS_REALLY_VISIBLE):
                visible += 1
        except Exception:  # 元素在检查过程中消失，忽略
            pass
    return visible, total


def judge(page) -> str:
    from playwright.sync_api import Error as PWError

    try:
        add_vis, add_tot = count_visible(page, IN_STOCK_TEXT)
        out_vis, out_tot = count_visible(page, OUT_OF_STOCK_TEXT)
    except PWError as e:
        log(f"检测时页面正在跳转/刷新：{str(e)[:80]}")
        return UNKNOWN
    log(f"{IN_STOCK_TEXT}: 可见 {add_vis}/{add_tot} | {OUT_OF_STOCK_TEXT}: 可见 {out_vis}/{out_tot}")
    if add_vis > 0:
        return IN_STOCK          # 有货按钮可见就算有货
    if out_vis > 0:
        return OUT_OF_STOCK
    return UNKNOWN


def wait_for_verdict(page, timeout_s: int) -> str:
    from playwright.sync_api import Error as PWError

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        first = judge(page)
        if first != UNKNOWN:
            # 页面脚本可能还在切换按钮：等网络安静 + 2 秒后再确认一次，以后者为准
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PWError:
                pass
            page.wait_for_timeout(2000)
            second = judge(page)
            return second if second != UNKNOWN else first
        page.wait_for_timeout(1000)
    return UNKNOWN


def dismiss_cookie_banner(page) -> None:
    for name in ("Accept all", "Accept All", "Accept", "I accept", "Agree", "Allow all"):
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(name)}\s*$", re.I)).first
            if btn.is_visible(timeout=800):
                btn.click(timeout=2000)
                log(f"已关闭 Cookie 弹窗（{name}）")
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def save_debug(page, tag: str) -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    png = SCREENSHOT_DIR / f"{tag}-{stamp}.png"
    try:
        page.screenshot(path=str(png), full_page=True)
        log(f"截图已保存：{png}")
    except Exception as e:  # noqa: BLE001
        log(f"截图失败：{e}")
    try:
        (SCREENSHOT_DIR / f"{tag}-{stamp}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        log(f"页面标题：{page.title()!r}  当前地址：{page.url}")
    except Exception:
        pass


def check_stock(always_screenshot: bool) -> str:
    from playwright.sync_api import Error as PWError, sync_playwright

    verdict = UNKNOWN
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="en-GB",
            timezone_id="Europe/Rome",
        )
        context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = context.new_page()

        for attempt in range(1, MAX_ATTEMPTS + 1):
            log(f"第 {attempt}/{MAX_ATTEMPTS} 次打开页面")
            try:
                page.goto(PRODUCT_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            except PWError as e:
                log(f"打开页面出错：{str(e)[:120]}")
            dismiss_cookie_banner(page)
            verdict = wait_for_verdict(page, VERDICT_WAIT_S)
            if verdict != UNKNOWN:
                break
            if attempt < MAX_ATTEMPTS:
                time.sleep(5)

        if verdict == UNKNOWN:
            save_debug(page, "fail")
        elif always_screenshot:
            save_debug(page, verdict.lower())
        browser.close()
    return verdict


# ----------------------------- 入口 -----------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Van Cleef & Arpels 补货监控")
    ap.add_argument("--test-email", action="store_true", help="只发一封测试邮件，不检测网页")
    ap.add_argument("--test-bark", action="store_true", help="只发一条测试推送（紧急+来电），不检测网页")
    ap.add_argument("--save-screenshot", action="store_true", help="无论结果如何都保存截图")
    args = ap.parse_args()

    # ---- 测试模式 ----
    if args.test_email or args.test_bark:
        failed = False
        if args.test_email:
            try:
                subject, text, html = build_alert_email(test=True)
                send_email(subject, text, html)
                log("✅ 测试邮件已发送，请查收（也看看垃圾邮件箱）")
            except Exception as e:  # noqa: BLE001
                log(f"❌ 测试邮件失败：{e}")
                failed = True
        if args.test_bark:
            try:
                send_bark("【测试】Van Cleef 监控", "这是测试推送：如果手机响铃了，说明紧急通知配置成功。")
                log("✅ 测试推送已发送，手机应该会响铃")
            except Exception as e:  # noqa: BLE001
                log(f"❌ 测试推送失败：{e}")
                failed = True
        return 3 if failed else 0

    # ---- 正常检测 ----
    log(f"开始检测：{PRODUCT_NAME}")
    verdict = check_stock(args.save_screenshot)

    if verdict == IN_STOCK:
        log("🔔 结果：有货！")
        write_summary(f"### 🔔 有货！\n检测时间：{local_time_str()}\n\n{PRODUCT_URL}")
        sent = notify_in_stock()
        if sent == 0:
            log("❌ 所有通知通道都失败了，请检查 Secrets")
            return 3
        return 0

    if verdict == OUT_OF_STOCK:
        log("结果：暂时无货（仅 ORDER BY PHONE 可见）")
        write_summary(f"无货（{local_time_str()}）")
        return 0

    log("❌ 结果：两个按钮都看不到（可能被网站拦截或页面改版），已保存截图")
    write_summary("### ⚠️ 两个按钮都不可见\n请下载本次运行的 Artifacts 查看截图。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
