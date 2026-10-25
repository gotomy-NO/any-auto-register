"""独立浏览器打开指定网址（headed 可见，独立 user-data-dir 数据隔离，用户指导用）
用法: python open_browser.py <网址>   （网址不写死——每次执行从参数传入）
"""
import sys
import time

from playwright.sync_api import sync_playwright

PROFILE_DIR = r"E:\AI\zcode_browser_profile"  # 独立 profile（与其他浏览器数据隔离）
URL = sys.argv[1] if len(sys.argv) > 1 else "https://zcode.z.ai"  # URL 从参数读，不写死

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        proxy={"server": "http://127.0.0.1:7897"},
        args=["--window-size=1280,900", "--window-position=80,60"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    # 监听新标签页（登录可能在新 tab 打开）
    new_pages = []

    def on_page(newp):
        new_pages.append(newp)

    ctx.on("page", on_page)
    try:
        print(f">>> 打开 {URL} ...")
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3500)
        print("URL:", page.url[:120])
        print("TITLE:", page.title()[:80])
        btns = page.eval_on_selector_all(
            "button", "els => els.map(e => e.innerText.trim()).filter(t => t)"
        )
        print("按钮:", btns[:25])

        # 用户指导：点击右上角"登录"（选可见的按钮——页面可能有多个，排除隐藏的）
        login_btn = page.locator("button:visible", has_text="登录")
        if login_btn.count() > 0:
            try:
                login_btn.first.click(timeout=10000)
                print(">>> 已点击右上角登录（可见按钮）")
            except Exception as e:
                print("点击失败:", str(e)[:80])
            page.wait_for_timeout(4000)
            # 检测新标签页（登录可能新 tab 打开）
            if new_pages:
                page = new_pages[-1]
                print(">>> 登录在新标签页打开")
            print("登录后 URL:", page.url[:160])
            print("登录后 TITLE:", page.title()[:80])
            btns2 = page.eval_on_selector_all(
                "button", "els => els.map(e => e.innerText.trim()).filter(t => t)"
            )
            print("登录页按钮:", btns2[:25])
            inputs2 = page.eval_on_selector_all(
                "input",
                "els => els.map(e => (e.type || '') + '|' + (e.placeholder || '')).filter(t => t)",
            )
            print("登录页输入框:", inputs2[:12])
            links2 = page.eval_on_selector_all(
                "a",
                "els => els.map(e => (e.innerText.trim() || '') + '→' + (e.href || '')).filter(t => t.split('→')[0])",
            )
            print("登录页链接:", [l[:100] for l in links2[:15]])

            # 用户指导：登录页点击"注册"按钮（正中间下面）→ 看注册页结构
            reg_btn = page.locator("button:visible", has_text="注册")
            if reg_btn.count() > 0:
                try:
                    reg_btn.first.click(timeout=10000)
                    print(">>> 已点击注册按钮")
                except Exception as e:
                    print("注册按钮点击失败:", str(e)[:80])
                page.wait_for_timeout(4500)
                print("点击注册后 URL:", page.url[:160])
                print("注册页 TITLE:", page.title()[:80])
                btns3 = page.eval_on_selector_all(
                    "button", "els => els.map(e => e.innerText.trim()).filter(t => t)"
                )
                print("注册页按钮:", btns3[:25])
                inputs3 = page.eval_on_selector_all(
                    "input",
                    "els => els.map(e => (e.type || '') + '|' + (e.placeholder || '')).filter(t => t)",
                )
                print("注册页输入框:", inputs3[:15])
            else:
                print("登录页未找到注册按钮")
        else:
            print("未找到登录按钮")
        print(">>> 浏览器保持打开中（用户指导用）...")
        time.sleep(240)  # 保持浏览器可见（用户指导窗口期）
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(60)
    print(">>> 浏览器保持结束")
