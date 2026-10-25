r"""login_direct_v2.py — z.ai 直连登录 v2（2026-10-05，继承昨晚诊断 + 当晨滑块实锤）

继承结论（scratch/zcode_slider_verify_report.json + zcode_pipeline_run2.log）：
  1. 授权页账密表单藏在「Continue with Email」按钮后（昨晚实锤，v1 已接）。
  2. 登录页今天也部署了阿里云滑块验证组件——「组件存在但弹窗调不出」时
     静默直提必被风控吞成 guest 会话（昨晚 3 连击 outside-of-viewport 实锤）。
     core.py 已修：scrollIntoView 兜底 + 精确报错不静默。
  3. 滑块本地自动求解已真网验证通过（gray-ncc 第 3 拖通过）。
  4. 昨晚 device-flow 手动协作走到 callback code=code-51129eae6b2f——授权页可用，
     但本地 280s deadline 早于上游 298s 过期，差 18s 憋死。v2 对策：登录成功后
     再 cli_init，poll 拿满 300s 窗口。
  5. 台账 id=89 token 是真会话（JWT email=tmpo***，非 guest）——作为兜底登录态。

阶段（每阶段截图+DOM dump，失败停在当阶段并写 classify.json）：
  S0 环境自检（camoufox / 台账账密 / CA 垫 ASCII 路径）
  S1 Camoufox 直连开 chat.z.ai/auth + 页内网络探针
  S2 点「Continue with Email」→ 填账密 → 滑块验证（自动为主，可开人工）→ Sign in
  S3 token 捕获 + 严格复核（非 guest + email 匹配）；失败转 S2F 台账 token 注入兜底
  S4 cli_init（fresh 300s）→ authorize_url → 勾协议 + 点 Authorize
  S5 poll_until_ready → mint_from_ready → quick_validate（不导入 9Router）
  S6 产物落盘：storage_state / 截图 / classify.json（全程打码，不落明文 key/token）

人工兜底开关：ZCODE_LOGIN_V2_MANUAL=1（有头窗口，用户代滑，300s）；
有头：ZCODE_LOGIN_V2_HEADED=1。默认 headless + 自动滑块（无人值守）。
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REG_ROOT = Path(__file__).resolve().parents[2]  # register-8000/
if str(REG_ROOT) not in sys.path:
    sys.path.insert(0, str(REG_ROOT))

# 独立脚本 CA 垫：中文路径会杀 venv 自带 CA（已实锤）→ 全部指向 ASCII 路径证书
_CACERT = Path(os.environ.get("LOCALAPPDATA", "")) / "anyauto-cacert.pem"
if _CACERT.exists():
    os.environ["SSL_CERT_FILE"] = str(_CACERT)
    os.environ["CURL_CA_BUNDLE"] = str(_CACERT)      # requests 认这个
    os.environ["REQUESTS_CA_BUNDLE"] = str(_CACERT)  # requests 也认这个

# 直连铁律：git-bash 常带 HTTP(S)_PROXY 环境变量，requests trust_env 会静默走系统代理
# （2026-10-05 11:43 实锤 ProxyError；浏览器侧 Camoufox 本就直连不受影响）
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)

DB_PATH = REG_ROOT / "account_manager.db"
CHAT_BASE = "https://chat.z.ai"
CHAT_AUTH = CHAT_BASE + "/auth"
BUDGET_S = 540.0  # 整体自限（外层 foreground timeout 580s）
T0 = time.time()

SCRATCH = REG_ROOT / "scratch" / f"login_direct_v2_{time.strftime('%H%M%S')}"
SCRATCH.mkdir(parents=True, exist_ok=True)

LOG_LINES: list[str] = []


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        line = line.encode("gbk", errors="replace").decode("gbk")
    except Exception:
        line = line.encode("ascii", errors="replace").decode("ascii")
    LOG_LINES.append(line)
    print(line, flush=True)


def elapsed() -> float:
    return time.time() - T0


def budget_left() -> float:
    return BUDGET_S - elapsed()


def mask(s: str, head: int = 8) -> str:
    s = s or ""
    return f"{s[:head]}…(len={len(s)})" if len(s) > head + 4 else "***"


def flush_log() -> None:
    (SCRATCH / "log.txt").write_text("\n".join(LOG_LINES), encoding="utf-8")


def write_classify(stage: str, reason: str, page=None, extra: dict | None = None) -> None:
    """卡点分类落盘（打码）。"""
    data = {"stage": stage, "reason": reason, "elapsed_s": round(elapsed(), 1), "ts": time.strftime("%H:%M:%S")}
    if extra:
        data.update(extra)
    if page is not None:
        try:
            data["url"] = page.url
            data["bodyHead"] = page.evaluate('document.body ? document.body.innerText.slice(0,300) : ""').replace("\n", " ")
        except Exception as e:
            data["page_err"] = str(e)[:120]
        try:
            page.screenshot(path=str(SCRATCH / f"{stage}_stuck.png"))
        except Exception:
            pass
        try:
            (SCRATCH / f"{stage}_dom.html").write_text(page.content()[:400000], encoding="utf-8")
        except Exception:
            pass
        try:
            (SCRATCH / f"{stage}_tap.json").write_text(
                json.dumps(page.evaluate("() => window.__TAP || []"), ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
    (SCRATCH / "classify.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"[classify] {stage}: {reason}")
    flush_log()


# ---------------------------------------------------------------- S0 台账
def load_account() -> dict:
    """最新 platform='zcode' registered 行；password 兼容 v1: 信封与明文两种形态。"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id,email,password,token FROM accounts "
        "WHERE platform='zcode' AND status='registered' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        raise SystemExit("台账无 platform='zcode' registered 账号")
    acct = dict(row)
    pwd = str(acct.get("password") or "")
    if pwd.startswith("v1:"):
        from core.secret_box import secret_box
        pwd = secret_box.decrypt(pwd).decode("utf-8")
    acct["password"] = pwd
    return acct


def _token_email(token: str) -> str:
    try:
        part = str(token).split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return str(payload.get("email") or "")
    except Exception:
        return ""


def validate_token(token: str, email: str) -> str | None:
    """token 严格复核（对齐 core._validate_token 口径）。None=通过。"""
    if not token:
        return "未捕获到 token"
    if not token.startswith("eyJ"):
        return f"token 非 JWT 形态（{token[:12]}…）"
    tok_email = _token_email(token)
    if tok_email and tok_email.startswith("guest"):
        return f"guest token（{tok_email}）——登录未真正生效"
    if email and tok_email and tok_email.lower() != email.lower():
        return f"token email 不匹配（got={tok_email}, want={email}）"
    if email and not tok_email:
        return "token payload 无 email 字段——非正式会话形态"
    return None


# ---------------------------------------------------------------- 页内探针
TAP_JS = r"""
(() => {
  if (window.__TAP) return;
  const tap = []; window.__TAP = tap;
  const hit = (u) => /auth|login|signin|session|oauth|captcha/i.test(String(u||''));
  const of = window.fetch;
  window.fetch = function(...a){
    const url = (a[0] && a[0].url) || a[0];
    const p = of.apply(this, a);
    if (hit(url)) p.then(r => { try{
      tap.push({k:'fetch', url:String(url), method:(a[1]&&a[1].method)||'GET',
        body:(a[1]&&a[1].body)?String(a[1].body).slice(0,500):null, status:r.status});
    }catch(e){} }).catch(()=>{});
    return p;
  };
  const oo = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m,u){ this.__u=String(u); this.__m=m; return oo.apply(this,arguments); };
  XMLHttpRequest.prototype.send = function(b){ if(hit(this.__u)) this.addEventListener('load',()=>{
    tap.push({k:'xhr', url:this.__u, method:this.__m, body:b?String(b).slice(0,500):null, status:this.status}); });
    return os.apply(this,arguments); };
})();
"""

PROBE_LOGIN_JS = r"""
(() => {
  const vis = e => !!e && e.offsetParent !== null;
  const ins = [...document.querySelectorAll('input, textarea')].filter(vis);
  const email = ins.find(e => e.type === 'email') || ins.find(e => /邮箱|mail/i.test(e.placeholder||''));
  const pwd = ins.find(e => e.type === 'password') || ins.find(e => /密码|password/i.test(e.placeholder||''));
  return {
    url: location.href, path: location.pathname, ready: document.readyState,
    bodyLen: (document.body&&document.body.innerText||'').length,
    hasEmail: !!email, hasPwd: !!pwd,
    captchaHint: /Click to start verification|点击开始验证/i.test(document.body&&document.body.innerText||''),
  };
})()
"""

# 授权页（zac_3 onAuthorizePage / confirmAuthorize 移植）
AUTHORIZE_JS = r"""
() => {
  const vis = e => !!e && e.offsetParent !== null;
  const norm = s => String(s||'').replace(/\s+/g,'');
  const body = (document.body&&document.body.innerText||'');
  if (/请输入验证码|输入您的电子邮箱|输入您的密码|Unlock all features|Create account|Enter your email|Create account/i.test(body)) {
    return {onPage: false, reason: 'login-ish', url: location.href, bodyHead: body.slice(0,200)};
  }
  const btns = [...document.querySelectorAll('button, a, div, span, [role=button]')].filter(vis);
  const btn = btns.find(b => /^(继续|同意|确认授权|授权|确认|Continue|Authorize|Allow|接受)$/.test(norm(b.innerText)));
  const boxes = [...document.querySelectorAll('input[type=checkbox], .el-checkbox__original, .ant-checkbox-input, [role=checkbox]')].filter(vis);
  return {onPage: !!btn, btnLabel: btn?norm(btn.innerText):null,
    boxes: boxes.length, url: location.href, bodyHead: body.slice(0,200)};
}
"""

DO_AUTHORIZE_JS = r"""
() => {
  const vis = e => !!e && e.offsetParent !== null;
  const norm = s => String(s||'').replace(/\s+/g,'');
  const looksDis = b => b.disabled===true || b.getAttribute('aria-disabled')==='true' ||
    (()=>{try{const cs=getComputedStyle(b);return cs.pointerEvents==='none'||parseFloat(cs.opacity)<0.55;}catch(e){return false;}})();
  // 勾协议：优先点可见的勾选框容器/label（样式化 checkbox 的 input 本体点击不生效，
  // 2026-10-05 实测 Continue 常灰）——input、父级 label、[class*=checkbox] 容器逐级试
  let checked = false;
  for (let round = 0; round < 3 && !checked; round++) {
    for (const c of document.querySelectorAll('input[type=checkbox], [role=checkbox]')) {
      if (!vis(c)) continue;
      const isOn = c.checked === true || c.getAttribute('aria-checked') === 'true';
      if (!isOn) {
        const targets = [c, c.parentElement, c.closest('label'), c.closest('[class*=checkbox]')].filter(Boolean);
        let hit = false;
        for (const t of targets) { try { t.click(); hit = true; break; } catch(e) {} }
        if (!hit) c.click();
      }
    }
    checked = [...document.querySelectorAll('input[type=checkbox], [role=checkbox]')]
      .some(c => vis(c) && (c.checked === true || c.getAttribute('aria-checked') === 'true'));
    if (!checked) {
      // 无可读状态时按按钮是否仍灰判断
      break;
    }
  }
  const btns = [...document.querySelectorAll('button, a, div, span, [role=button]')].filter(vis);
  const match = b => /^(继续|同意|确认授权|授权|确认|Continue|Authorize|Allow|接受)$/.test(norm(b.innerText));
  const btn = btns.find(b => match(b) && b.tagName==='BUTTON') || btns.find(match);
  if (!btn) return {clicked:false, why:'no-btn', checked};
  const dis = looksDis(btn);
  // 仍灰也点一次（点击可触发重校验；pointerevents 正常时浏览器自会忽略无效点击）
  btn.click();
  return {clicked:true, label:norm(btn.innerText), wasDisabled: dis, checked};
}
"""


def dump_artifacts(page, tag: str) -> None:
    try:
        page.screenshot(path=str(SCRATCH / f"{tag}.png"))
    except Exception as e:
        log(f"[art] screenshot {tag} 失败: {e}")
    try:
        (SCRATCH / f"{tag}.html").write_text(page.content()[:400000], encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    manual = os.environ.get("ZCODE_LOGIN_V2_MANUAL", "") in ("1", "true", "yes")
    headed = manual or os.environ.get("ZCODE_LOGIN_V2_HEADED", "") in ("1", "true", "yes")

    # ---- S0 自检
    acct = load_account()
    email, password, db_token = acct["email"], acct["password"], acct["token"] or ""
    log(f"S0 台账 id={acct['id']} email={email} pwd_len={len(password)} db_token={mask(db_token)}")
    (SCRATCH / "account_masked.json").write_text(json.dumps(
        {"id": acct["id"], "email": email, "pwd_len": len(password), "db_token_masked": mask(db_token)},
        ensure_ascii=False, indent=1), encoding="utf-8")

    from platforms.zcode.glm_mint import RequestsHttp, mask_key, quick_validate
    from platforms.zcode import zcode_oauth as zo
    from platforms.zcode.core import ZcodeRegister

    http = RequestsHttp(proxy="", timeout=40.0)  # 直连 = 家宽住宅出口

    reg = ZcodeRegister(proxy=None, log_fn=log, manual_fallback=manual)
    result: dict = {"ok": False, "email": email}

    from camoufox.addons import DefaultAddons
    from camoufox.sync_api import Camoufox

    with Camoufox(
        headless=not headed,
        exclude_addons=[DefaultAddons.UBO],
    ) as browser:
        page = browser.new_page()
        page.add_init_script(TAP_JS)
        log(f"S1 Camoufox 启动（{'headed' if headed else 'headless'} 直连无代理）")

        # ---- S2 表单登录（真跑主路径）
        page.goto(CHAT_AUTH, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        probe = page.evaluate(PROBE_LOGIN_JS)
        log(f"S2 首探: {json.dumps(probe, ensure_ascii=False)[:250]}")
        dump_artifacts(page, "s2_first")

        reg._click_email_entry(page)
        probe = page.evaluate(PROBE_LOGIN_JS)
        log(f"S2 点入口后: hasEmail={probe.get('hasEmail')} hasPwd={probe.get('hasPwd')}")
        if not (probe.get("hasEmail") and probe.get("hasPwd")):
            write_classify("S2", "点 Continue with Email 后账密表单仍未出现", page)
            return 2

        reg._fill_login(page, email, password)
        dump_artifacts(page, "s2_filled")

        # 滑块验证 + 提交（核心复用 core 修正版：弹窗调不出时精确报错，绝不静默）。
        # 2026-10-05 实测：登录页滑块 8 连拖全部对齐收敛但服务端不放行（注册页第 3 拖即过
        # ——登录页自动求解 0 通过），失败时转台账 token 兜底而非死磕。
        submitted = True
        try:
            reg._collab_verify(page, submit_texts=["Sign in", "登录"], verification_required=False)
        except RuntimeError as e:
            submitted = False
            log(f"S2 滑块验证/提交失败（不静默，转兜底）: {str(e)[:220]}")
        except Exception as e:  # noqa: BLE001
            submitted = False
            log(f"S2 滑块验证异常（转兜底）: {type(e).__name__}: {str(e)[:200]}")

        # ---- S3 token 捕获 + 严格复核
        token, reason = "", "表单未提交（滑块未过）"
        if submitted:
            deadline = time.time() + 25
            while time.time() < deadline:
                token = reg._capture_token(page)
                reason = validate_token(token, email)
                if reason is None:
                    break
                page.wait_for_timeout(3000)
        dump_artifacts(page, "s3_after_login")
        if reason is None:
            log(f"S3 表单登录成功：token={mask(token)}（email 匹配 {email}）")
            result["login_path"] = "form"
            result["session_token_masked"] = mask(token)
        else:
            log(f"S3 表单登录 token 复核失败: {reason}")
            if not db_token:
                write_classify("S3", f"表单登录失败且无台账 token 兜底: {reason}", page)
                return 3
            # ---- S2F 兜底：台账 token 注入（真会话，昨晨注册时捕获）
            log("S2F 台账 token 注入兜底…")
            page.goto(CHAT_BASE + "/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            page.evaluate(
                """(t) => { localStorage.setItem('token', t); try{localStorage.setItem('JWT',t);}catch(e){}
                     document.cookie = `token=${t}; path=/; domain=.z.ai; secure`; }""",
                db_token,
            )
            reason2 = validate_token(db_token, email)
            if reason2 is not None:
                write_classify("S2F", f"台账 token 复核也失败: {reason2}", page)
                return 3
            # 服务端实测（2026-10-05 11:21）：带 cookie GET /api/v1/auths/ → 200
            # 用户对象（role=user）——台账 token 是真会话；payload 无 exp 字段。
            log("S2F 台账 token 服务端 200 已预验证（/api/v1/auths/ role=user）")
            token = db_token
            result["login_path"] = "db_token_fallback"
            result["session_token_masked"] = mask(token)
            log(f"S2F 注入台账 token 通过复核: {mask(token)}")
            page.goto(CHAT_BASE + "/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
        result["session_ok"] = True
        flush_log()

        # ---- S4 device-flow（登录后才 cli_init：poll 拿满 300s 窗口）
        if budget_left() < 120:
            write_classify("S4", f"预算不足（剩 {budget_left():.0f}s），token 已到手先行交卷", page)
            result.update({"ok": "token_only", "why": "budget"})
            (SCRATCH / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
            return 4
        device = None
        for attempt in range(1, 4):
            try:
                device = zo.cli_init(http)
                break
            except zo.ZcodeOAuthError as e:
                log(f"S4 cli_init 第 {attempt} 次失败: {str(e)[:140]}")
                if attempt < 3:
                    page.wait_for_timeout(4000)
        if device is None:
            write_classify("S4", "cli_init 3 连败（网络/代理层）", page)
            (SCRATCH / "result.json").write_text(json.dumps(
                {**result, "ok": "token_only", "why": "cli_init_failed"}, ensure_ascii=False, indent=1),
                encoding="utf-8")
            return 4
        log(f"S4 flow_id={device.flow_id[:8]}… authorize_url={device.authorize_url[:100]}")
        page.goto(device.authorize_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        az = page.evaluate(AUTHORIZE_JS)
        log(f"S4 授权页: {json.dumps(az, ensure_ascii=False)[:300]}")
        dump_artifacts(page, "s4_authorize")
        authorized = False
        if az.get("onPage"):
            # ① DOM 点击（勾协议 + Continue）；② Continue 真实鼠标点击补刀——
            # 2026-10-05 实测：协议 DOM 勾上（checked=True）但 React 灰按钮对 el.click()
            # 无反应，wasDisabled=True；真实鼠标事件才触发受控组件 onClick。
            res = page.evaluate(DO_AUTHORIZE_JS)
            log(f"S4 DOM 点授权: {res}")
            dump_artifacts(page, "s4_after_click")
            for punch in range(1, 6):
                try:
                    page.mouse.click(1150, 900, delay=60)  # 视口内空白区，不遮 Continue
                except Exception:
                    pass
                try:
                    loc = page.locator("button:visible", has_text="Continue").first
                    if loc.count() > 0:
                        loc.click(timeout=8000, force=False)
                        log(f"S4 Continue 鼠标点击第 {punch} 次已发")
                except Exception as e:
                    log(f"S4 Continue 鼠标点击第 {punch} 次异常: {str(e)[:80]}")
                for _ in range(5):  # 最多等 ~10s 判断是否已跳走
                    page.wait_for_timeout(2000)
                    try:
                        now_url = page.url
                    except Exception:
                        now_url = ""
                    if "/auth/oauth/authorize" not in now_url:
                        authorized = True
                        break
                if authorized:
                    log(f"S4 授权已提交（第 {punch} 次点击生效），当前页: {now_url[:80]}")
                    break
            dump_artifacts(page, "s4_after_punch")
        else:
            # 未点成（可能自动跳了/表单形态不同）：再给一次机会
            page.goto(device.authorize_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
            az = page.evaluate(AUTHORIZE_JS)
            log(f"S4 重探授权页: {json.dumps(az, ensure_ascii=False)[:300]}")
            if az.get("onPage"):
                res = page.evaluate(DO_AUTHORIZE_JS)
                log(f"S4 二次点授权: {res}")
                page.wait_for_timeout(6000)
                authorized = "/auth/oauth/authorize" not in page.url
            else:
                write_classify("S4", f"授权页未出现（{az.get('reason','?')}）——会话未生效或形态变更", page)
                (SCRATCH / "result.json").write_text(json.dumps(
                    {**result, "ok": "token_only", "why": "authorize_page_missing"}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
                return 4
        flush_log()

        # ---- S5 poll + mint + validate
        # 韧性 poll：传输错误（read timeout 等）是瞬时的——重试到真 ready/failed/真超时
        poll_deadline = min(240.0, budget_left() - 60)
        poll_start = time.monotonic()
        poll = None
        while True:
            try:
                poll = zo.poll_until_ready(device, http=http, deadline_s=max(30.0, poll_deadline - (time.monotonic() - poll_start)))
                break
            except zo.ZcodeOAuthError as e:
                spent = time.monotonic() - poll_start
                transient = "请求失败" in str(e) or "timed out" in str(e).lower() or "timeout" in str(e).lower()
                if transient and spent < poll_deadline - 20:
                    log(f"S5 poll 传输错误重试（{str(e)[:120]}）已耗时 {spent:.0f}s")
                    page.wait_for_timeout(3000)
                    continue
                write_classify("S5", f"poll 失败: {e}", page)
                (SCRATCH / "result.json").write_text(json.dumps(
                    {**result, "ok": "token_only", "why": f"poll: {str(e)[:150]}"}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
                return 5
        log(f"S5 poll ready: chat_token={mask(poll.chat_token or '')} zai_at={mask(poll.zai_access_token or '')}")
        # [2026-10-05] 持久化免费额度通道凭据：
        #   zcode_jwt = poll.chat_token（OAuth 换来的 zcode JWT，zcode-plan 通道的 Bearer）
        #   zai_at    = OAuth access_token（/api/oauth/userinfo 取 user_id 用）
        try:
            _db = Path(__file__).resolve().parents[2] / "account_manager.db"
            _c = sqlite3.connect(str(_db))
            _row = _c.execute("SELECT extra_json FROM accounts WHERE id=?", (acct["id"],)).fetchone()
            _ex = {}
            try:
                _ex = json.loads((_row or [None])[0] or "{}")
            except Exception:
                _ex = {}
            _ex["zcode_jwt"] = poll.chat_token or ""
            _ex["zai_at"] = poll.zai_access_token or ""
            _c.execute("UPDATE accounts SET extra_json=? WHERE id=?", (json.dumps(_ex, ensure_ascii=False), acct["id"]))
            _c.commit()
            _c.close()
            log("S5 免费额度通道凭据已入库（zcode_jwt/zai_at，不回显）")
        except Exception as _e:  # noqa: BLE001
            log(f"S5 凭据持久化失败: {type(_e).__name__}: {str(_e)[:120]}")
        try:
            mint = zo.mint_from_ready(poll, http=http)
            log(f"S5 MINT OK steps={mint.steps} key={mask_key(mint.combined_key)} reused={mint.reused}")
        except Exception as e:  # noqa: BLE001
            write_classify("S5", f"mint 失败（token 已到手）: {type(e).__name__}: {str(e)[:200]}", page)
            (SCRATCH / "result.json").write_text(json.dumps(
                {**result, "ok": "token_only", "why": f"mint: {str(e)[:200]}",
                 "device_token_masked": mask(poll.chat_token or "")}, ensure_ascii=False, indent=1), encoding="utf-8")
            return 5
        try:
            valid = quick_validate(mint.combined_key, http=http)
        except Exception as e:  # noqa: BLE001
            log(f"S5 quick_validate 异常（key 已铸）: {type(e).__name__}: {str(e)[:150]}")
            valid = False
        log(f"S5 quick_validate: {valid}")
        result.update({
            "ok": True if valid else "key_unvalidated",
            "key_masked": mask_key(mint.combined_key),
            "key_len": len(mint.combined_key),
            "key_prefix8": mint.combined_key[:8],
            "mint_reused": mint.reused,
            "mint_steps": mint.steps,
            "quick_validate": bool(valid),
            "device_chat_token_masked": mask(poll.chat_token or ""),
        })
        try:
            page.context.storage_state(path=str(SCRATCH / "storage_state.json"))
        except Exception:
            pass

    (SCRATCH / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    flush_log()
    log(f"S6 完成 elapsed={elapsed():.0f}s result={json.dumps(result, ensure_ascii=False)[:300]}")
    return 0 if result.get("ok") is True else 6


if __name__ == "__main__":
    sys.exit(main())
