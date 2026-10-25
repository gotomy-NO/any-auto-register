"""诊断：复现 core.py 的 OAuth → Continue with Email → Sign up 流程，dump 页面结构
不填表、不提交——只观察 Sign up 点击后的 URL 和表单 DOM。
用法: python diagnose_signup.py
"""
import time
from urllib.parse import quote

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

OAUTH_URL = (
    "https://chat.z.ai/auth?response_type=code&client_id=client_P8X5CMWmlaRO9gyO-KSqtg"
    "&redirect_uri=" + quote("https://zcode.z.ai/login", safe="")
)


def dump_inputs(page, tag):
    try:
        inputs = page.eval_on_selector_all(
            "input:visible",
            "els => els.map(e => (e.type || '') + '|' + (e.placeholder || '')).filter(t => t)",
        )
        print(f"[{tag}] 输入框: {inputs}")
    except Exception as e:
        print(f"[{tag}] 输入框读取失败: {e}")


def dump_text_links(page, tag):
    try:
        links = page.eval_on_selector_all(
            "a:visible",
            "els => els.map(e => (e.innerText.trim() || '') + '|' + (e.href || '')).filter(t => t && t[0])",
        )
        print(f"[{tag}] 链接: {[l[:90] for l in links[:12]]}")
    except Exception as e:
        print(f"[{tag}] 链接读取失败: {e}")


def dump_buttons(page, tag):
    try:
        btns = page.eval_on_selector_all(
            "button:visible", "els => els.map(e => e.innerText.trim()).filter(t => t)"
        )
        print(f"[{tag}] 按钮: {btns[:12]}")
    except Exception as e:
        print(f"[{tag}] 按钮读取失败: {e}")


def dump_iframe(page, tag):
    try:
        for f in page.frames:
            if f == page.main_frame:
                continue
            try:
                fi = f.eval_on_selector_all(
                    "input:visible",
                    "els => els.map(e => (e.type || '') + '|' + (e.placeholder || '')).filter(t => t)",
                )
                fb = f.eval_on_selector_all(
                    "button:visible", "els => els.map(e => e.innerText.trim()).filter(t => t)"
                )
                print(f"[{tag}] iframe {f.url[:70]}: inputs={fi} btns={fb[:8]}")
            except Exception:
                continue
    except Exception as e:
        print(f"[{tag}] iframe 读取失败: {e}")


with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],
) as browser:
    page = browser.new_page()
    page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    print("STEP1 URL:", page.url[:120])
    dump_inputs(page, "STEP1")
    dump_text_links(page, "STEP1")
    dump_buttons(page, "STEP1")

    # Continue with Email
    btn = page.locator("button:visible", has_text="Continue with Email")
    if btn.count() == 0:
        btn = page.locator("button:visible", has_text="Email")
    if btn.count() > 0:
        btn.first.click()
        print("STEP2 已点 Continue with Email")
        page.wait_for_timeout(3000)
    else:
        print("STEP2 未见 Continue with Email 按钮")
    print("STEP2 URL:", page.url[:120])
    dump_inputs(page, "STEP2")
    dump_text_links(page, "STEP2")
    dump_buttons(page, "STEP2")
    dump_iframe(page, "STEP2")

    # Sign up
    target = None
    for loc in (
        page.locator("a:visible", has_text="Sign up"),
        page.locator("button:visible", has_text="Sign up"),
        page.locator("text=Sign up").last,
    ):
        try:
            if loc.count() > 0:
                target = loc.first
                break
        except Exception:
            continue
    if target is None:
        print("STEP3 未见 Sign up 入口")
    else:
        href = None
        try:
            href = target.get_attribute("href")
        except Exception:
            href = None
        print(f"STEP3 Sign up 目标: tag={target.evaluate('el => el.tagName')} href={href}")
        if href and href.startswith("http"):
            page.goto(href, wait_until="domcontentloaded", timeout=30000)
            print("STEP3 方式=goto href")
        else:
            try:
                target.click(force=True, timeout=5000)
                print("STEP3 方式=click")
            except Exception as e:
                target.press("Enter")
                print(f"STEP3 方式=Enter (click 失败: {str(e)[:80]})")
        page.wait_for_timeout(3000)
    print("STEP3 URL:", page.url[:120])
    dump_inputs(page, "STEP3")
    dump_text_links(page, "STEP3")
    dump_buttons(page, "STEP3")
    dump_iframe(page, "STEP3")
    try:
        body = page.evaluate('document.body ? document.body.innerText.slice(0, 500) : ""')
        print("STEP3 页面文本:", body.replace("\n", " ")[:400])
    except Exception as e:
        print("STEP3 文本读取失败:", e)

    print(">>> 浏览器保持 120s，可人工观察")
    time.sleep(120)
print(">>> 结束")
