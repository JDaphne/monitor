#!/usr/bin/env python3
"""
中国银行 2027 校招统一考试 —— 纽约考位监控 & 自动预约

用法（详见 README.md）:
    export BOC_USER="E12345678"      # 网申证件号码（字母大写、去括号）
    export BOC_PASS="123456"         # 网申联系电话后 6 位
    python boc_monitor.py

脚本以「有界面」模式打开浏览器：
  1. 自动填写用户名/密码；如有验证码，请在弹出的浏览器里手动完成并点登录，脚本会自动等待。
  2. 登录后进入考位预约页（可用 --booking-url 指定；否则自动点击含“预约”字样的入口，
     若找不到，请手动点进预约页面，脚本会等待你）。
  3. 每隔 --interval 秒刷新一次，查找包含关键字（默认“纽约”/“New York”）且未满的考位，
     一旦发现可约，立即点击“预约/选择”并点击后续“确定/确认/提交”按钮，然后截图并报警。

每轮的页面 HTML/截图会保存在 debug/ 目录；如果页面结构与脚本假设不符，
把 debug/ 里的文件发给维护者即可据此调整选择器。
"""

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

BASE_URL = "https://boc2027xzks.campustest.cn/"

FULL_PATTERN = re.compile(r"预约已满|已满|满员|无余位|余位[:：]?\s*0(?!\d)|剩余[:：]?\s*0(?!\d)|不可预约|Full", re.I)
SUBMIT_BTN_PATTERN = re.compile(r"^\s*(确认预约|提交预约|确定预约|立即预约|预约|提交|确认|确定|下一步)\s*$")
CONFIRM_BTN_PATTERN = re.compile(r"^\s*(确定|确认|确 定|确认预约|提交|是|OK|Confirm)\s*$", re.I)
ENTRY_PATTERN = re.compile(r"马上预约|考位预约|预约考位|考试预约|去预约|立即预约|选择考点|考点预约")

DEBUG_DIR = Path(__file__).with_name("debug")


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def notify(msg, url):
    """终端响铃 + 可选推送（Bark / Server 酱 / 任意 GET webhook，{msg} 会被替换）。"""
    for _ in range(5):
        sys.stdout.write("\a")
        sys.stdout.flush()
        time.sleep(0.2)
    log(f"*** {msg} ***")
    if url:
        try:
            full = url.replace("{msg}", urllib.parse.quote(msg))
            urllib.request.urlopen(full, timeout=10).read()
        except Exception as e:  # 推送失败不影响主流程
            log(f"推送失败: {e}")


def wait_enter(prompt, fallback_s):
    """等待回车；在 Jupyter 的 !python 等无法输入的环境下改为等待 fallback_s 秒。"""
    try:
        input(prompt)
    except EOFError:
        log(f"（无法读取键盘输入，{fallback_s} 秒后继续）")
        time.sleep(fallback_s)


def dump(page, tag):
    DEBUG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%m%d_%H%M%S")
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{stamp}_{tag}.png"), full_page=True)
        (DEBUG_DIR / f"{stamp}_{tag}.html").write_text(page.content(), encoding="utf-8")
    except PWError as e:
        log(f"保存调试文件失败: {e}")


# ---------------------------------------------------------------- 登录

def login_form_visible(page):
    try:
        return page.locator("input[type=password]:visible").count() > 0
    except PWError:
        return False


def login(page, user, pwd, timeout_s):
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    if not login_form_visible(page):
        log("未看到登录表单，可能已处于登录状态。")
        return

    pwd_input = page.locator("input[type=password]:visible").first
    # 用户名框：密码框之前最近的一个文本输入框
    user_input = page.locator(
        "input:visible:not([type=password]):not([type=hidden]):not([type=checkbox])"
        ":not([type=radio]):not([type=submit]):not([type=button])"
    ).first
    user_input.fill(user)
    pwd_input.fill(pwd)
    log("已填写用户名和密码。")

    captcha = page.locator(
        "input[placeholder*=验证码]:visible, input[name*=code i]:visible, input[id*=code i]:visible"
    )
    if captcha.count() == 0:
        btn = page.get_by_role("button", name=re.compile(r"登\s*录|Login", re.I))
        if btn.count() == 0:
            btn = page.locator("text=/^\\s*登\\s*录\\s*$/")
        if btn.count() > 0:
            btn.first.click()
    else:
        log("检测到验证码：请在浏览器中输入验证码并点击登录。")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        if not login_form_visible(page):
            log("登录成功。")
            return
    raise RuntimeError("登录超时（请检查账号密码或手动完成验证码）。")


# ---------------------------------------------------------------- 进入预约页

def active_page(ctx):
    """返回最新打开且未关闭的标签页（网站可能在新标签/新窗口中打开预约页）。"""
    pages = [pg for pg in ctx.pages if not pg.is_closed()]
    if not pages:
        raise RuntimeError("浏览器窗口已被关闭，请重新运行脚本（运行期间不要关闭它）。")
    return pages[-1]


DEFAULT_BOOKING_URL = "https://c.exam-sp.com/t/default/order/exam"


def is_booking_page(page):
    """预约页的标志：有“选择考区”输入框。"""
    try:
        return page.locator("input[placeholder*=考区]:visible").count() > 0
    except PWError:
        return False


def wait_booking_page(ctx, timeout_s):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        page = active_page(ctx)
        if is_booking_page(page):
            return page
        time.sleep(1)
    return None


def open_booking_page(ctx, booking_url, timeout_s):
    # 1) 登录后可能已经直接在预约页
    page = wait_booking_page(ctx, 3)
    if page:
        return page
    page = active_page(ctx)
    # 2) 点“马上预约”等入口
    entry = page.locator("a, button, span, li").filter(has_text=ENTRY_PATTERN)
    for i in range(min(entry.count(), 20)):
        el = entry.nth(i)
        try:
            txt = el.inner_text(timeout=500).strip()
            if el.is_visible() and len(txt) <= 12:
                log(f"点击入口：{txt}")
                el.click()
                page = wait_booking_page(ctx, 8)
                if page:
                    return page
                break
        except PWError:
            continue
    # 3) 直接打开预约页地址
    try:
        active_page(ctx).goto(booking_url or DEFAULT_BOOKING_URL, wait_until="domcontentloaded")
        page = wait_booking_page(ctx, 10)
        if page:
            return page
    except PWError:
        pass
    # 4) 让用户手动进入
    log("没能自动进入预约页。请在浏览器中点“马上预约”，进入有“选择考区”的那一页，脚本会自动检测……")
    page = wait_booking_page(ctx, timeout_s)
    if page:
        return page
    raise RuntimeError("等待进入预约页超时。")


def page_has_keyword(page, keywords):
    try:
        body = page.inner_text("body", timeout=3000)
    except PWError:
        return False
    return any(k.lower() in body.lower() for k in keywords)


# ---------------------------------------------------------------- 查找并预约

def section(page, title_re):
    """预约页由若干 .info 区块组成，每块有 .title（如“选择考区”“选择考点”）。"""
    return page.locator(".reservation .info").filter(
        has=page.locator(".title", has_text=re.compile(title_re)))


def select_city(page, city):
    inp = page.locator("input[placeholder*=考区]").first
    inp.wait_for(state="visible", timeout=15000)
    if inp.input_value().strip() == city:
        return
    opt = page.locator(".regionList > div").filter(has_text=re.compile(f"^\\s*{re.escape(city)}\\s*$"))
    if not opt.first.is_visible():
        inp.click()
        page.wait_for_timeout(500)
    opt.first.click()
    page.wait_for_timeout(1500)


def session_items(page):
    """考区下面的场次（如“北京时间10月19日00:00场-纽约”）。"""
    sec = page.locator(".reservation .info").filter(has=page.locator(".title", has_text="金融综合类"))
    return sec.locator(".list > div")


def venue_cards(page):
    """“选择考点”区块中的考点卡片。"""
    sec = section(page, "选择考点")
    cards = sec.locator(".list > div")
    if cards.count() == 0:
        cards = sec.locator(":scope > div:not(.title) > div")
    return cards


def is_disabled_like(locator):
    try:
        cls = (locator.get_attribute("class") or "").lower()
        return "disabled" in cls or locator.get_attribute("disabled") is not None
    except PWError:
        return True


def click_confirms(page, rounds=4):
    """连续点击弹出的“确定/确认/提交”按钮。"""
    for _ in range(rounds):
        page.wait_for_timeout(800)
        btns = page.locator("button:visible, a:visible, .el-button:visible, .ant-btn:visible") \
            .filter(has_text=CONFIRM_BTN_PATTERN)
        clicked = False
        for i in range(btns.count() - 1, -1, -1):  # 弹窗一般在 DOM 末尾
            b = btns.nth(i)
            try:
                if b.is_enabled() and not is_disabled_like(b):
                    log(f"点击：{b.inner_text(timeout=500).strip()}")
                    b.click(timeout=3000)
                    clicked = True
                    break
            except PWError:
                continue
        if not clicked:
            return


def booking_succeeded(page):
    try:
        body = page.inner_text("body", timeout=3000)
    except PWError:
        return False
    return bool(re.search(r"预约成功|已预约|预约结果|准考证|Success", body, re.I))


def ensure_on_booking_page(page, booking_url):
    """刷新后如果不在预约页（如被跳回选批次页），尝试回到预约页；必要时等用户手动点进去。"""
    inp = page.locator("input[placeholder*=考区]")
    try:
        inp.first.wait_for(state="visible", timeout=10000)
        return True
    except PWTimeout:
        pass
    log("当前不在预约页，尝试重新进入……")
    dump(page, "lost_booking_page")
    for target in (booking_url, "https://c.exam-sp.com/t/default/order/chooseBatch"):
        try:
            page.goto(target, wait_until="domcontentloaded")
            inp.first.wait_for(state="visible", timeout=10000)
            return True
        except (PWError, PWTimeout):
            continue
    log("请在浏览器中手动点进“马上预约”→ 选考区的那一页（最多等 5 分钟）……")
    try:
        inp.first.wait_for(state="visible", timeout=300000)
        return True
    except PWTimeout:
        return False


def card_is_full(card):
    try:
        text = card.inner_text(timeout=2000)
    except PWError:
        return True
    return bool(FULL_PATTERN.search(text)) or is_disabled_like(card)


def click_submit(page):
    """选中考点后点击页面上的提交/确认预约按钮（只点可见的按钮，跳过隐藏弹窗里的）。"""
    btns = page.locator(".reservation button:visible, .right_container button:visible, "
                        ".el-dialog__wrapper:visible button:visible") \
        .filter(has_text=SUBMIT_BTN_PATTERN)
    for i in range(btns.count() - 1, -1, -1):
        b = btns.nth(i)
        try:
            if b.is_enabled() and not is_disabled_like(b):
                log(f"点击：{b.inner_text(timeout=500).strip()}")
                b.click(timeout=3000)
                return True
        except PWError:
            continue
    return False


def try_book(page, city, dry_run, verbose):
    select_city(page, city)
    sessions = session_items(page)
    try:
        sessions.first.wait_for(state="visible", timeout=10000)
    except PWTimeout:
        log("选了考区但没有出现场次。")
        return False
    for si in range(sessions.count()):
        sess = sessions.nth(si)
        sess_text = sess.inner_text().replace("\n", "")
        if "selected" not in (sess.get_attribute("class") or ""):
            sess.click()
            page.wait_for_timeout(1500)
        cards = venue_cards(page)
        try:
            cards.first.wait_for(state="visible", timeout=10000)
        except PWTimeout:
            log(f"场次 {sess_text} 下没有出现考点。")
            continue
        for ci in range(cards.count()):
            card = cards.nth(ci)
            lines = [ln.strip() for ln in card.inner_text().split("\n") if ln.strip()]
            name = next((ln for ln in lines if not FULL_PATTERN.search(ln)), "考点")
            if card_is_full(card):
                if verbose:
                    log(f"  {sess_text} / {name}：已满")
                continue
            log(f"发现可约考位：{sess_text} / {name}")
            if dry_run:
                return True
            card.click()
            page.wait_for_timeout(800)
            if not click_submit(page):
                log("没找到提交按钮，请立即在浏览器中手动点击提交！")
            click_confirms(page)
            page.wait_for_timeout(1500)
            dump(page, "after_book")
            if not booking_succeeded(page):
                log("未检测到“预约成功”字样，请查看浏览器确认。")
            return True
    return False


# ---------------------------------------------------------------- 主循环

def main():
    ap = argparse.ArgumentParser(description="中行校招考试纽约考位监控/自动预约")
    ap.add_argument("--user", default=os.getenv("BOC_USER"), help="证件号码（或环境变量 BOC_USER）")
    ap.add_argument("--password", default=os.getenv("BOC_PASS"), help="手机号后 6 位（或环境变量 BOC_PASS）")
    ap.add_argument("--city", default="纽约", help="考区名称（与页面上显示的一致），默认 纽约")
    ap.add_argument("--keywords", default="纽约,New York,NewYork,NYC", help="考点关键字，逗号分隔")
    ap.add_argument("--booking-url", default=None, help="预约页面完整 URL（可选）")
    ap.add_argument("--interval", type=float, default=20, help="刷新间隔秒数（会加随机抖动），默认 20")
    ap.add_argument("--notify-url", default=os.getenv("BOC_NOTIFY_URL"),
                    help="推送 URL，{msg} 替换为消息，例如 https://api.day.app/<key>/{msg}")
    ap.add_argument("--dry-run", action="store_true", help="只监控提醒，不自动点击预约")
    ap.add_argument("--headless", action="store_true", help="无界面运行（有验证码时不要用）")
    ap.add_argument("--end", default="2026-10-16 11:00", help="北京时间截止时间，默认 2026-10-16 11:00")
    args = ap.parse_args()

    if not args.user or not args.password:
        ap.error("请提供 --user/--password 或设置环境变量 BOC_USER/BOC_PASS")
    user = re.sub(r"[()（）\s]", "", args.user).upper()
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    end_ts = datetime.strptime(args.end, "%Y-%m-%d %H:%M").timestamp()  # 按本机时区解析
    if os.getenv("TZ") is None and time.strftime("%z") != "+0800":
        log("提示：本机不是北京时间，--end 会按本机时区解释，可设置 TZ=Asia/Shanghai。")

    with sync_playwright() as p:
        launch_kw = dict(
            user_data_dir=str(Path(__file__).with_name(".browser_profile")),
            headless=args.headless,
            viewport={"width": 1366, "height": 900},
            locale="zh-CN",
        )
        try:
            # 优先使用电脑上已安装的 Google Chrome，无需 playwright install
            ctx = p.chromium.launch_persistent_context(channel="chrome", **launch_kw)
        except PWError:
            log("未找到 Google Chrome，改用 Playwright 自带的 Chromium。")
            ctx = p.chromium.launch_persistent_context(**launch_kw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # 把含关键字的接口响应记下来，便于后续改成直接调接口
        def on_response(resp):
            if resp.request.resource_type in ("xhr", "fetch"):
                try:
                    body = resp.text()
                except Exception:
                    return
                if any(k in body for k in keywords):
                    DEBUG_DIR.mkdir(exist_ok=True)
                    with open(DEBUG_DIR / "api_log.jsonl", "a", encoding="utf-8") as f:
                        f.write(json.dumps({"t": time.time(), "url": resp.url,
                                            "method": resp.request.method,
                                            "post": resp.request.post_data,
                                            "body": body[:5000]}, ensure_ascii=False) + "\n")
        def setup_page(pg):
            pg.on("dialog", lambda d: (log(f"弹窗：{d.message}"), d.accept()))
            pg.on("response", on_response)

        for pg in ctx.pages:
            setup_page(pg)
        ctx.on("page", setup_page)

        login(page, user, args.password, timeout_s=300)
        page = open_booking_page(ctx, args.booking_url, timeout_s=600)
        booking_url = page.url
        log(f"预约页：{booking_url}")
        dump(page, "booking_page")

        n = 0
        while time.time() < end_ts:
            n += 1
            try:
                page = active_page(ctx)
                if login_form_visible(page):
                    log("登录已失效，重新登录……")
                    login(page, user, args.password, timeout_s=300)
                    page.goto(booking_url, wait_until="domcontentloaded")
                if not ensure_on_booking_page(page, booking_url):
                    continue
                if try_book(page, args.city, args.dry_run, verbose=(n % 10 == 1)):
                    msg = "纽约考位有空位！" + ("（仅提醒，请立即手动预约）" if args.dry_run else "已尝试自动预约，请立即核对！")
                    notify(msg, args.notify_url)
                    dump(page, "found")
                    if args.dry_run:
                        wait_enter("按回车继续监控……", 60)
                    else:
                        wait_enter("请在浏览器中确认预约结果，按回车退出。", 1800)
                        return
                else:
                    log(f"第 {n} 次检查：暂无可约纽约考位")
                if n % 30 == 1:
                    dump(page, f"check{n}")
            except (PWError, PWTimeout) as e:
                log(f"出错：{e}")
                dump(page, "error")

            time.sleep(args.interval * random.uniform(0.8, 1.3))
            try:
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
            except PWError as e:
                log(f"刷新失败：{e}")
                try:
                    page.goto(booking_url, wait_until="domcontentloaded")
                except PWError:
                    pass
        log("已过预约截止时间，退出。")
        ctx.close()


if __name__ == "__main__":
    main()
