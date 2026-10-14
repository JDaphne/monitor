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

FULL_PATTERN = re.compile(r"已满|满员|无余位|余位[:：]?\s*0(?!\d)|剩余[:：]?\s*0(?!\d)|不可预约|Full", re.I)
BOOK_BTN_PATTERN = re.compile(r"^\s*(预约|立即预约|选择|选定|确认预约|报名)\s*$")
CONFIRM_BTN_PATTERN = re.compile(r"^\s*(确定|确认|确 定|确认预约|提交|是|OK|Confirm)\s*$", re.I)
ENTRY_PATTERN = re.compile(r"考位预约|预约考位|考试预约|去预约|立即预约|选择考点|考点预约")

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


def looks_like_booking_page(page, keywords, home_url):
    if page.url == home_url:
        return False
    if page_has_keyword(page, keywords):
        return True
    try:
        if page.locator("select:visible, .el-select:visible, .ant-select:visible").count() > 0:
            return True
        body = page.inner_text("body", timeout=3000)
    except PWError:
        return False
    return bool(re.search(r"考点|考场|场次|考试城市", body))


def open_booking_page(ctx, booking_url, keywords, timeout_s):
    page = active_page(ctx)
    if booking_url:
        page.goto(booking_url, wait_until="domcontentloaded")
        return page
    page.wait_for_timeout(1500)
    home_url = page.url
    entry = page.locator("a, button, span, div").filter(has_text=ENTRY_PATTERN)
    for i in range(min(entry.count(), 20)):
        el = entry.nth(i)
        try:
            txt = el.inner_text(timeout=500).strip()
            if el.is_visible() and len(txt) <= 12:
                log(f"点击入口：{txt}")
                el.click()
                time.sleep(3)
                if looks_like_booking_page(active_page(ctx), keywords, home_url):
                    return active_page(ctx)
                break
        except PWError:
            continue
    log("没找到预约入口。请在浏览器中手动进入考位预约页面（选择考点/考位的那一页），脚本会自动检测……")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(2)
        page = active_page(ctx)
        try:
            if looks_like_booking_page(page, keywords, home_url):
                time.sleep(2)  # 等页面加载完
                return active_page(ctx)
        except PWError:
            continue  # 页面正在跳转
    raise RuntimeError("等待进入预约页超时。")


def page_has_keyword(page, keywords):
    try:
        body = page.inner_text("body", timeout=3000)
    except PWError:
        return False
    return any(k.lower() in body.lower() for k in keywords)


# ---------------------------------------------------------------- 查找并预约

def pick_city_in_selects(page, keywords):
    """如果城市/考点是 <select> 下拉框，选中包含关键字的选项。"""
    picked = False
    selects = page.locator("select:visible")
    for i in range(selects.count()):
        sel = selects.nth(i)
        try:
            opts = sel.locator("option").all_inner_texts()
        except PWError:
            continue
        for opt in opts:
            if any(k.lower() in opt.lower() for k in keywords):
                sel.select_option(label=opt)
                page.wait_for_timeout(1200)
                picked = True
                break
    return picked


def pick_city_in_custom_dropdowns(page, keywords):
    """Element-UI / Ant Design 等自定义下拉：点击包含关键字的下拉项。"""
    pattern = re.compile("|".join(re.escape(k) for k in keywords), re.I)
    items = page.locator(
        ".el-select-dropdown__item, .ant-select-item-option, li[role=option], .dropdown-item"
    ).filter(has_text=pattern)
    if items.count() == 0:
        # 先展开所有下拉框，让选项渲染出来
        for trig in page.locator(".el-select:visible, .ant-select:visible").all()[:5]:
            try:
                trig.click()
                page.wait_for_timeout(500)
                items = page.locator(
                    ".el-select-dropdown__item:visible, .ant-select-item-option:visible, li[role=option]:visible"
                ).filter(has_text=pattern)
                if items.count() > 0:
                    break
                page.keyboard.press("Escape")
            except PWError:
                continue
    for i in range(items.count()):
        it = items.nth(i)
        try:
            if it.is_visible() and "disabled" not in (it.get_attribute("class") or ""):
                it.click()
                page.wait_for_timeout(1200)
                return True
        except PWError:
            continue
    return False


def find_available_rows(page, keywords):
    """返回包含关键字、未标记为已满、且带有可点击“预约/选择”按钮的行/卡片中的按钮。"""
    pattern = re.compile("|".join(re.escape(k) for k in keywords), re.I)
    rows = page.locator("tr, li, .card, .el-card, .ant-card, .item, [class*=row], [class*=item]") \
        .filter(has_text=pattern)
    found = []
    for i in range(min(rows.count(), 200)):
        row = rows.nth(i)
        try:
            if not row.is_visible():
                continue
            text = row.inner_text(timeout=1000)
            if len(text) > 400:  # 太大，多半是整个容器而非单行
                continue
            if FULL_PATTERN.search(text):
                continue
            btns = row.locator("button, a, span, div[role=button], input[type=button]") \
                .filter(has_text=BOOK_BTN_PATTERN)
            for j in range(btns.count()):
                b = btns.nth(j)
                if b.is_visible() and b.is_enabled() and not is_disabled_like(b):
                    found.append((text.replace("\n", " | ")[:150], b))
                    break
            else:
                # 有的系统用单选框/复选框选场次
                radio = row.locator("input[type=radio]:not([disabled]), .el-radio:not(.is-disabled)")
                if radio.count() > 0:
                    found.append((text.replace("\n", " | ")[:150], radio.first))
        except PWError:
            continue
    return found


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


def try_book(page, keywords, dry_run):
    pick_city_in_selects(page, keywords) or pick_city_in_custom_dropdowns(page, keywords)
    candidates = find_available_rows(page, keywords)
    if not candidates:
        return False
    for desc, btn in candidates:
        log(f"发现可约考位：{desc}")
        if dry_run:
            return True
        try:
            btn.click(timeout=3000)
        except PWError as e:
            log(f"点击失败：{e}")
            continue
        click_confirms(page)
        page.wait_for_timeout(1500)
        dump(page, "after_book")
        if booking_succeeded(page):
            return True
        log("未检测到“预约成功”字样，请查看浏览器/截图确认。")
        return True
    return False


# ---------------------------------------------------------------- 主循环

def main():
    ap = argparse.ArgumentParser(description="中行校招考试纽约考位监控/自动预约")
    ap.add_argument("--user", default=os.getenv("BOC_USER"), help="证件号码（或环境变量 BOC_USER）")
    ap.add_argument("--password", default=os.getenv("BOC_PASS"), help="手机号后 6 位（或环境变量 BOC_PASS）")
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
        page = open_booking_page(ctx, args.booking_url, keywords, timeout_s=600)
        booking_url = args.booking_url or page.url
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
                if try_book(page, keywords, args.dry_run):
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
