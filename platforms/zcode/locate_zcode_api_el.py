"""定位 ZCode API 元素真实结构（tag/href/parent 链接）"""
import json
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

TOKEN_PATH = r"E:\AI\openAI\AIClient2API-main\configs\zcode\zai_session.json"
with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = json.load(f)["token"]

with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],
) as browser:
    page = browser.new_page()
    try:
        page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        page.evaluate("(t) => localStorage.setItem('token', t)", TOKEN)
        page.reload(wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(10000)
        info = page.evaluate(
            """() => {
                const out = [];
                const els = document.querySelectorAll('a, button, div, span');
                for (const e of els) {
                    const t = (e.innerText || '').trim();
                    if (t === 'ZCode API' || (t.includes('ZCode API') && t.length < 30)) {
                        out.push({
                            tag: e.tagName,
                            text: t.slice(0, 40),
                            href: e.getAttribute('href') || '',
                            onclick: !!e.onclick,
                            target: e.getAttribute('target') || '',
                            cls: (e.className || '').toString().slice(0, 80),
                            parentTag: e.parentElement ? e.parentElement.tagName : '',
                            parentHref: e.parentElement && e.parentElement.tagName === 'A' ? e.parentElement.getAttribute('href') : '',
                        });
                    }
                }
                return out;
            }"""
        )
        print("ZCode API 元素:")
        print(json.dumps(info, ensure_ascii=False, indent=2))
        # 全页 a 标签 href 中含 api/zcode 的
        links = page.eval_on_selector_all(
            "a", "els => els.map(e => (e.innerText.trim()||'') + '=>' + (e.href||'')).filter(t => t)"
        )
        api_links = [l for l in links if "api" in l.lower() or "zcode" in l.lower() or "docs" in l.lower()]
        print("相关链接:", [l[:160] for l in api_links[:15]])
        time.sleep(15)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(15)
    print(">>> 结束")
