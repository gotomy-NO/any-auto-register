"""抓 chat.z.ai 前端真实对话 API 路径（token 注入 + 网络拦截 + 发一条消息）
流程：登录页注入 token（跳过登录）→ 进主界面 → 拦截所有 XHR/fetch → 发一条测试消息 → 打印对话请求 URL/method/headers
用法: python probe_chat_api.py [token]
"""
import json
import sys
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

TOKEN_PATH = r"E:\AI\openAI\AIClient2API-main\configs\zcode\zai_session.json"

with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = json.load(f)["token"]

OAUTH_URL = (
    "https://chat.z.ai/auth?response_type=code&client_id=client_P8X5CMWmlaRO9gyO-KSqtg"
    "&redirect_uri=https%3A%2F%2Fzcode.z.ai%2Flogin"
)

captured = []


def on_response(resp):
    try:
        req = resp.request
        url = req.url
        if any(k in url for k in ("completions", "chat", "message", "api/v1", "/v1/", "generate", "inference", "stream")):
            captured.append({
                "method": req.method,
                "url": url[:200],
                "status": resp.status,
                "req_headers": {k: v[:60] for k, v in req.headers.items() if k.lower() in ("authorization", "content-type", "x-api-key")},
            })
    except Exception:
        pass


with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],
) as browser:
    page = browser.new_page()
    page.on("response", on_response)
    try:
        # 主应用根域注入 token（localStorage 'token'——/auth 是 OAuth 登录页，token 存主应用域）
        page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        page.evaluate(
            """(token) => {
                localStorage.setItem('token', token);
            }""",
            TOKEN,
        )
        print("已在 chat.z.ai 根域注入 token——刷新加载登录态")
        page.reload(wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(10000)
        # 若仍在登录页——点 Skip for now 跳过（游客态也能进主界面测对话路径）
        if "/auth" in page.url or "Welcome to Z.ai" in page.evaluate(
            'document.body ? document.body.innerText.slice(0, 200) : ""'
        ):
            skip_btn = page.locator("button:visible", has_text="Skip for now")
            if skip_btn.count() > 0:
                skip_btn.first.click()
                print("token 未生效——点 Skip for now 进主界面（游客态抓路径）")
                page.wait_for_timeout(8000)
        print("URL:", page.url[:120])
        body = page.evaluate('document.body ? document.body.innerText.slice(0, 300) : ""')
        print("页面:", body.replace("\n", " ")[:250])

        # 发送测试消息（找输入框）
        textarea = page.locator("textarea:visible").first
        input_el = textarea if textarea.count() > 0 else page.locator("input[placeholder]:visible").first
        if input_el.count() > 0:
            input_el.click()
            input_el.type("hi", delay=100)
            print("已输入 hi——找发送按钮")
            send_btn = page.locator("button:visible").filter(has_text="").last
            # 发送按钮通常是纸飞机图标——直接 Enter
            input_el.press("Enter")
            print("已发送（Enter）")
            # 等待响应流
            for i in range(20):
                time.sleep(2)
                if captured:
                    break
            print("=== 捕获的请求 ===")
            for c in captured[:15]:
                print(json.dumps(c, ensure_ascii=False))
        else:
            print("未找到输入框——当前 URL:", page.url[:140])
            print("页面按钮:", page.eval_on_selector_all(
                "button:visible", "els => els.map(e => (e.innerText.trim() || e.getAttribute('aria-label') || 'icon').slice(0, 30))"
            )[:15])
        print(">>> 保持浏览器 60s 供手动验证/操作...")
        time.sleep(60)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(20)
    print(">>> 结束")
