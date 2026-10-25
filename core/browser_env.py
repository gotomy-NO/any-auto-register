"""统一反检测浏览器环境工厂（register-8000）。

目标：所有注册平台开出来的浏览器像真人 —— 环境痕迹不再成为用户诉求里的"总被拦"。

四块能力：
  1. make_context(profile_key, backend=...) 统一入口：每账号独立持久化 profile +
     反检测启动 + 地理一致性 + 指纹快照落盘。backend 可插拔（camoufox|chrome_real）。
  2. geo_for_country(code)            出口国 → 时区/locale/经纬度 对齐表。
  3. fingerprint_snapshot(...)        本次生效指纹抓取 + 落盘（排查 / 回归对照）。
  4. human_* 行为助手                 打字/鼠标轨迹/滚动/延迟（滑块、点选复用）。

设计约束（历史实坑，勿回退）：
  - Camoufox `geoip` 只放【启动参数】；`timezone_id`/`geolocation` 是【context 参数】
    —— 放错层会炸启动或静默失效（xai/zcode 踩过）。
  - GeoIP 库首启可能联网下载，失败会炸启动 → 降级链，降级必须显式记录在
    meta['geoip_active']=False，绝不报假成功（xai_browser._camoufox_start 同思路）。
  - Camoufox 必须在创建它的线程内创建并使用；批量线程复用会撞
    'Sync API inside the asyncio loop'（见 zcode core.register 的一次性线程壳）。
  - 排除 UBO 默认插件：其 addon 路径无效会 InvalidAddonPath 启动失败。
  - 【2026-10-05 用户实测】Camoufox 开 github.com 登录页渲染不出（被风控）。
    所以 backend 可插拔：github 线换 chrome_real（系统真实 Chrome + 独立持久化
    profile + CDP 接管），不要把自己焊死在 Camoufox 上。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from typing import Any, Callable, Mapping

__all__ = [
    "BrowserEnvError",
    "GEO_ALIGNS",
    "SUPPORTED_COUNTRIES",
    "build_camoufox_kwargs",
    "build_chrome_real_kwargs",
    "browser_selfcheck",
    "build_clean_chromium_ua",
    "chromium_ua_major",
    "client_hints_major",
    "filter_preset_antipatterns",
    "find_chrome",
    "fingerprint_snapshot",
    "geo_for_country",
    "geoip_available",
    "human_move_click",
    "human_path",
    "human_scroll",
    "human_type",
    "installed_chromium_major",
    "kill_orphan_camoufox",
    "make_context",
    "parse_proxy",
    "profile_dir",
    "random_delay",
    "repo_root",
    "resolve_profile_key",
    "ua_consistency_check",
    "save_page_evidence",
    "select_preset_for_profile",
]

# --------------------------------------------------------------------------- #
# 地理一致性：出口国 → 时区 / locale / 经纬度
# --------------------------------------------------------------------------- #

#: JS IANA 时区 + BCP47 locale + 首府经纬度。
#: Camoufox geoip=True 时，启动层会按出口 IP 实测覆盖这些值；geoip 不可用
#: （离线 / GeoIP 库下载失败）时按本表兜底 —— 有表兜底好过无 geoip 还挂矛盾 locale。
GEO_ALIGNS: dict[str, dict[str, Any]] = {
    "US": {"timezone_id": "America/Los_Angeles", "locale": "en-US",
           "geolocation": {"latitude": 34.05, "longitude": -118.24}},
    "UK": {"timezone_id": "Europe/London", "locale": "en-GB",
           "geolocation": {"latitude": 51.51, "longitude": -0.13}},
    "DE": {"timezone_id": "Europe/Berlin", "locale": "de-DE",
           "geolocation": {"latitude": 52.52, "longitude": 13.40}},
    "FR": {"timezone_id": "Europe/Paris", "locale": "fr-FR",
           "geolocation": {"latitude": 48.86, "longitude": 2.35}},
    "JP": {"timezone_id": "Asia/Tokyo", "locale": "ja-JP",
           "geolocation": {"latitude": 35.68, "longitude": 139.69}},
    "SG": {"timezone_id": "Asia/Singapore", "locale": "en-SG",
           "geolocation": {"latitude": 1.35, "longitude": 103.82}},
    "HK": {"timezone_id": "Asia/Hong_Kong", "locale": "en-HK",
           "geolocation": {"latitude": 22.32, "longitude": 114.17}},
    "CN": {"timezone_id": "Asia/Shanghai", "locale": "zh-CN",
           "geolocation": {"latitude": 31.23, "longitude": 121.47}},
    "NL": {"timezone_id": "Europe/Amsterdam", "locale": "nl-NL",
           "geolocation": {"latitude": 52.37, "longitude": 4.90}},
    "KR": {"timezone_id": "Asia/Seoul", "locale": "en-US",
           "geolocation": {"latitude": 37.57, "longitude": 126.98}},
}

#: 支持的出口国代码（GEO_ALIGNS 键）。
SUPPORTED_COUNTRIES = tuple(sorted(GEO_ALIGNS))


class BrowserEnvError(RuntimeError):
    """浏览器环境工厂无法给出可用环境。"""


# --------------------------------------------------------------------------- #
# profile 目录：每账号独立持久化（cookies / 指纹稳定）
# --------------------------------------------------------------------------- #

_INVALID_KEY_CHARS = re.compile(r"[^A-Za-z0-9_\-\.]+")


def repo_root() -> str:
    """register-8000 仓库根（core/browser_env.py 的上级目录）。"""
    cand = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.isdir(os.path.join(cand, "core")):
        return cand
    return os.getcwd()


def resolve_profile_key(profile_key: str) -> str:
    """任意字符串 → 安全目录名。非法字符折叠 '_'，限长 80。

    >>> resolve_profile_key('grok/a@b.com::sess-1')
    'grok_a_b.com__sess-1'
    """
    if not isinstance(profile_key, str) or not profile_key.strip():
        raise BrowserEnvError("profile_key 必须是非空字符串")
    cleaned = _INVALID_KEY_CHARS.sub("_", profile_key.strip())[:80].rstrip("._ ")
    if not cleaned:
        raise BrowserEnvError(f"profile_key 清洗后为空: {profile_key!r}")
    return cleaned


def profile_dir(profile_key: str, *, create: bool = False) -> str:
    """profile_key → data/browser_profiles/<safe_key>/（create=True 幂等建目录）。"""
    safe = resolve_profile_key(profile_key)
    path = os.path.join(repo_root(), "data", "browser_profiles", safe)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def parse_proxy(proxy: str | Mapping | None) -> dict | None:
    """统一代理入参：URL 字符串 / mapping / None → Playwright `proxy=` 参数。

    - URL 内嵌认证拆独立 username/password（Firefox/Chromium 都拒绝 URL 内嵌认证，
      语义同 xai_browser.split_proxy）。
    - 自动加 bypass：127.0.0.1/localhost 直连，避免本地回调被代理吃掉。
    """
    if proxy is None:
        return None
    if isinstance(proxy, Mapping):
        return dict(proxy) if proxy else None
    proxy = str(proxy).strip()
    if not proxy:
        return None
    from urllib.parse import urlparse

    p = urlparse(proxy)
    scheme = (p.scheme or "http").lower()
    netloc = p.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[-1]
    out: dict[str, str] = {"server": f"{scheme}://{netloc}",
                           "bypass": "127.0.0.1,localhost,::1"}
    if p.username:
        out["username"] = p.username
    if p.password:
        out["password"] = p.password
    return out


def _country_norm(geo_country: str | None) -> str | None:
    """'uk'/'gb' → 'UK'；None/空 → None。"""
    if not geo_country:
        return None
    code = str(geo_country).strip().upper()
    if not code:
        return None
    return "UK" if code in ("UK", "GB") else code


def geo_for_country(geo_country: str | None) -> dict:
    """出口国 → {timezone_id, locale, geolocation}；未知国 → {}（调用方决定回退）。"""
    code = _country_norm(geo_country)
    if code is None:
        return {}
    align = GEO_ALIGNS.get(code)
    if align is None:
        return {}
    return {
        "timezone_id": align["timezone_id"],
        "locale": align["locale"],
        "geolocation": dict(align["geolocation"]),
    }


def geoip_available() -> bool:
    """GeoIP extra 可用性（不联网、不下载；只查库导入与 geoip_allowed）。

    历史坑：Camoufox geoip=True 首启会联网下载 mmdb，失败直接炸启动。
    这里前置探测：库缺失 → False；geoip_allowed 抛 NotInstalledGeoIPExtra → False。
    """
    try:
        from camoufox.geolocation import geoip_allowed
    except Exception:
        return False
    try:
        geoip_allowed()
    except Exception:
        return False
    return True


def _probe_exit_geo(proxy: dict | None, timeout: float = 8.0) -> dict:
    """经代理探测出口 IP 的国家/时区（ip-api.com）。失败返回 {}，绝不抛。"""
    try:
        import urllib.request

        if proxy:
            server = str(proxy.get("server", "")).replace("socks5://", "socks5h://")
            handler = urllib.request.ProxyHandler({"http": server, "https": server})
        else:
            handler = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(handler)
        with opener.open(
            "http://ip-api.com/json?fields=query,countryCode,timezone", timeout=timeout
        ) as resp:
            data = json.loads(resp.read())
        return {
            "ip": data.get("query") or None,
            "country": (data.get("countryCode") or "").upper() or None,
            "timezone": data.get("timezone") or None,
        }
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# backend=chrome_real：系统真实 Chrome（github 线用；Camoufox 被 github 风控）
# --------------------------------------------------------------------------- #

def find_chrome() -> str | None:
    """探测系统已装 Chrome/Edge 的可执行文件路径（Windows 优先注册表常见位置）。"""
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for c in candidates:
        try:
            if c and os.path.isfile(c):
                return c
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- #
# N7 修复（2026-10-08）：无头 UA 去 'HeadlessChrome' 保留字
#   实测：本机 Edge 154 下 --headless=new 仍让 UA/请求头带 HeadlessChrome，
#   且与干净的 Sec-CH-UA（154）自相矛盾。修法=无头时显式给 context 一个干净 UA。
#   _N7_HEADLESS_UA_PROBE=True  → 优先"抓内核真实无头 UA → HeadlessChrome 字节级
#     替换成 Chrome"（最稳、零版本猜测；代价：每进程首次多一次轻量启动，按 exe 缓存）。
#   False → 只按安装目录版本号重建 UA（不额外启动）。
#   环境变量 N7_NO_UA_PROBE=1 可临时关掉探针。
# --------------------------------------------------------------------------- #

_CHROMIUM_VER_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
_N7_HEADLESS_UA_PROBE = True
_KERNEL_UA_CACHE: dict[str, str] = {}


def installed_chromium_major(exe: str) -> str | None:
    """返回浏览器主版本（如 '154'）。Windows 扫安装目录的版本子目录
    （Edge/Chrome 均为 Application/<x.y.z.w>/），否则回退 `exe --version`。"""
    try:
        d = os.path.dirname(os.path.abspath(exe))
        best = None
        for name in os.listdir(d):
            if os.path.isdir(os.path.join(d, name)) and _CHROMIUM_VER_RE.match(name):
                t = tuple(int(x) for x in name.split("."))
                if best is None or t > best[0]:
                    best = (t, name)
        if best:
            return best[1].split(".")[0]
    except Exception:
        pass
    try:
        out = subprocess.run([exe, "--version"], capture_output=True,
                             text=True, timeout=10).stdout
        m = re.search(r"(\d+)\.\d+", out or "")
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def build_clean_chromium_ua(exe: str, headless_ua: str | None = None) -> str | None:
    """无头干净 UA：优先把内核自报 UA 里的 HeadlessChrome→Chrome（字节级等价有头
    真 Edge）；否则按真实主版本重建（平台按 OS；Edge 补 Edg 段）。"""
    if headless_ua and "HeadlessChrome" in headless_ua:
        return headless_ua.replace("HeadlessChrome", "Chrome")
    ver = installed_chromium_major(exe)
    if not ver:
        return None
    if sys.platform.startswith("win"):
        plat = "Windows NT 10.0; Win64; x64"
    elif sys.platform == "darwin":
        plat = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        plat = "X11; Linux x86_64"
    ua = (f"Mozilla/5.0 ({plat}) AppleWebKit/537.36 (KHTML, like Gecko) "
          f"Chrome/{ver}.0.0.0 Safari/537.36")
    base = os.path.basename(exe).lower()
    if "msedge" in base or "edge" in exe.lower():
        ua += f" Edg/{ver}.0.0.0"
    return ua


def chromium_ua_major(ua: str) -> str | None:
    """从 UA 串取 Chrome/HeadlessChrome 主版本号。"""
    m = re.search(r"(?:Headless)?Chrome/(\d+)\.", ua or "")
    return m.group(1) if m else None


def client_hints_major(brands: Any) -> str | None:
    """从 Sec-CH-UA / navigator.userAgentData.brands 取主版本（Chromium/Edge 取最大）。"""
    best = None
    for b in brands or []:
        if isinstance(b, dict):
            name, ver = str(b.get("brand", "")), str(b.get("version", ""))
        else:
            name, _, ver = str(b).partition("/")
        if "Chromium" in name or "Edge" in name or "Chrome" in name:
            m = re.match(r"(\d+)", ver)
            if m:
                best = max(best or 0, int(m.group(1)))
    return str(best) if best is not None else None


def ua_consistency_check(ua: str, brands: Any = None) -> dict:
    """只读自检：UA 不含 HeadlessChrome，且主版本与 Client Hints 主版本自洽。"""
    major = chromium_ua_major(ua)
    has_hl = "HeadlessChrome" in (ua or "")
    ch = client_hints_major(brands) if brands is not None else None
    reason = None
    if has_hl:
        reason = "headless_token"
    elif not major:
        reason = "no_major"
    elif ch is not None and ch != major:
        reason = f"major_mismatch:{major}!={ch}"
    return {"ok": reason is None, "reason": reason,
            "ua_major": major, "ch_major": ch, "has_headless": has_hl}


def _probe_kernel_headless_ua(pw: Any, exe: str,
                              log: Callable[[str], None] | None = None) -> str | None:
    """轻量无头探针：取内核自己报的 UA（供字节级 token 替换）。按 exe 缓存一次。"""
    if not exe or os.environ.get("N7_NO_UA_PROBE"):
        return None
    key = exe.lower()
    if key in _KERNEL_UA_CACHE:
        return _KERNEL_UA_CACHE[key]
    ua = None
    try:
        b = pw.chromium.launch(executable_path=exe, headless=False,
                               args=["--headless=new"], timeout=20000)
        try:
            ua = b.new_page().evaluate("navigator.userAgent")
        finally:
            try:
                b.close()
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"browser_env: headless UA 探测失败（{type(exc).__name__}）→ 回退版本重建")
        ua = None
    if ua:
        _KERNEL_UA_CACHE[key] = ua
    return ua


def build_chrome_real_kwargs(
    *,
    profile_key: str,
    proxy: str | Mapping | None = None,
    headless: bool = False,
    geo_country: str | None = None,
    persist: bool = True,
    window: tuple[int, int] = (1440, 900),
    extra_launch: Mapping | None = None,
    extra_context: Mapping | None = None,
) -> dict:
    """chrome_real 后端的启动/上下文参数（真 Chrome + 独立持久 profile + 反自动化）。

    与 Camoufox 的差异：没有指纹伪装（本来就真），关键是
    --disable-blink-features=AutomationControlled + 排除 enable-automation 开关
    + 真实用户数据目录（cookies/登录态稳定）。

    无头坑：Playwright/内核无头 UA 会在产品位输出 'HeadlessChrome'（一眼假）。实测
    本机 Edge 154 下 `--headless=new` 仍带该保留字（旧注释假设「新无头 UA 与有头
    一致」被证伪，见 docs/N7-UA泄漏方案-20261008.md）——停用 Playwright 自带 headless、
    改传 --headless=new，并在此处不设 UA；真正的干净化由 make_context 在 headless 时
    注入 `context["user_agent"]`（去 HeadlessChrome）兜底。geo 兜底：geoip 无 Camoufox
    那套能力，走出口 IP 探测对齐时区（geo_country 未指定且给了代理时）。
    """
    headless = bool(headless)
    launch: dict[str, Any] = {
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            f"--window-size={window[0]},{window[1]}",
        ],
        "ignore_default_args": ["--enable-automation"],
    }
    if headless:
        # 不走 playwright 自带 headless（老无头 UA 泄 HeadlessChrome）
        launch["headless"] = False
        launch["args"].append("--headless=new")
    else:
        launch["headless"] = False
    context: dict[str, Any] = {
        "viewport": {"width": window[0], "height": window[1]},
        "ignore_https_errors": True,
    }
    align = geo_for_country(geo_country)
    if not align and proxy and geo_country is None:
        # 未指定国家：按出口 IP 实测对齐（反"中国时区 + 美国出口"矛盾）
        probe = _probe_exit_geo(parse_proxy(proxy))
        if probe:
            align = geo_for_country(probe.get("country"))  # 表内含 locale+经纬度
            if not align and probe.get("timezone"):
                align = {"timezone_id": probe["timezone"]}
    if align:
        context.update(align)

    proxy_cfg = parse_proxy(proxy)
    if proxy_cfg:
        launch["proxy"] = proxy_cfg

    meta: dict[str, Any] = {
        "profile_key": profile_key,
        "profile_path": None,
        "geoip_active": False,
        "geo_mode": "table" if align else "off",
        "geo_country": _country_norm(geo_country),
        "fallbacks": [],
    }
    if persist:
        pdir = profile_dir(profile_key, create=True)
        meta["profile_path"] = pdir
        launch["user_data_dir"] = pdir  # playwright chromium.launch_persistent_context

    if extra_launch:
        launch.update(extra_launch)
    if extra_context:
        context.update(extra_context)
    meta["context_overrides"] = dict(context)
    return {"launch": launch, "context": context, "meta": meta}


# --------------------------------------------------------------------------- #
# backend=camoufox：启动/上下文参数计算
# --------------------------------------------------------------------------- #

def build_camoufox_kwargs(
    *,
    profile_key: str,
    proxy: str | Mapping | None = None,
    headless: bool = False,
    geo_country: str | None = None,
    persist: bool = True,
    geoip: str | bool = "auto",
    os_name: str = "windows",
    window: tuple[int, int] = (1440, 900),
    extra_launch: Mapping | None = None,
    extra_context: Mapping | None = None,
    log: Callable[[str], None] | None = None,
) -> dict:
    """Camoufox 后端的完整启动/上下文参数（纯函数，离线可测）。

    返回 dict：
      launch   Camoufox(...) 启动参数 —— `geoip` 只在这一层
      context  context 层参数 —— `timezone_id`/`geolocation`/`locale` 在这一层
      meta     决策记录（geoip 生效值、降级原因、出口探测、profile 路径）

    meta['geoip_active'] 是唯一权威：True 才代表 geo 按出口 IP 实测对齐了。
    geoip 入参：'auto'（GeoIP 库可用则开）/ True / False。
    """
    log = log or (lambda _msg: None)
    launch: dict[str, Any] = {
        "headless": bool(headless),
        "humanize": True,
        "os": str(os_name).lower(),
        "window": tuple(window),
        "exclude_addons": ["UBO"],
        "args": ["--no-sandbox"],
    }
    context: dict[str, Any] = {}

    proxy_cfg = parse_proxy(proxy)
    if proxy_cfg:
        launch["proxy"] = proxy_cfg

    # ---- geoip：只放启动参数；不可用则按对齐表兜底到 context 层 ----
    geo_active = False
    geo_mode = "off"
    if geoip is True or (geoip == "auto" and geoip_available()):
        launch["geoip"] = True
        geo_mode = "launch"
        geo_active = True
    else:
        log("browser_env: geoip 不可用/关闭 —— 用地理对齐表兜底")
        align = geo_for_country(geo_country)
        if align:
            context.update(align)
            geo_mode = "table"
            # 【2026-10-06 修复】persistent_context=True 时 make_context 走
            # `context_obj = browser`，不再 new_context(**context_kw) ——
            # context 层的 timezone_id/locale 被【整体丢弃】，浏览器 Intl/Date
            # 时区因而回落到宿主（实测 Asia/Shanghai），与 KR 出口矛盾。
            # Camoufox 的 Intl/Date 时区由【启动层】config["timezone"] 驱动，
            # 故这里必须把对齐值同时写进 launch，才能真正生效。
            cfg = launch.setdefault("config", {})
            cfg.setdefault("timezone", align["timezone_id"])
            if align.get("locale"):
                launch.setdefault("locale", align["locale"])

    meta: dict[str, Any] = {
        "profile_key": profile_key,
        "profile_path": None,
        "geoip_active": geo_active,
        "geo_mode": geo_mode,
        "geo_country": _country_norm(geo_country),
        "fallbacks": [],
    }

    # ---- persist：每账号独立持久化 profile（cookies/指纹稳定） ----
    if persist:
        pdir = profile_dir(profile_key, create=True)
        meta["profile_path"] = pdir
        # Camoufox 原生支持 persistent_context + user_data_dir（直通 launch_options）
        launch["persistent_context"] = True
        launch["user_data_dir"] = pdir

    # ---- 出口探测：仅落盘记录 + geoip 关且未指定国家时对齐时区 ----
    if proxy_cfg:
        probe = _probe_exit_geo(proxy_cfg)
        if probe:
            meta["exit_geo"] = probe
            if not geo_active and not geo_country and probe.get("timezone"):
                context.setdefault("timezone_id", probe["timezone"])
                # 同上：持久化 context 会丢弃 context 层参数，必须落到启动层。
                launch.setdefault("config", {}).setdefault("timezone", probe["timezone"])

    if extra_launch:
        prev_cfg = launch.get("config")
        launch.update(extra_launch)
        # 若调用方也传了 config，合并而非整体覆盖，避免丢掉 timezone/locale 对齐值。
        new_cfg = launch.get("config")
        if isinstance(prev_cfg, dict) and isinstance(new_cfg, dict) and new_cfg is not prev_cfg:
            launch["config"] = {**prev_cfg, **new_cfg}
    if extra_context:
        context.update(extra_context)

    meta["context_overrides"] = dict(context)
    return {"launch": launch, "context": context, "meta": meta}


# --------------------------------------------------------------------------- #
# make_context：统一入口（backend 可插拔）
# --------------------------------------------------------------------------- #

def make_context(
    profile_key: str,
    *,
    proxy: str | Mapping | None = None,
    headless: bool = False,
    geo_country: str | None = None,
    persist: bool = True,
    backend: str = "camoufox",
    geoip: str | bool = "auto",
    os_name: str = "windows",
    window: tuple[int, int] = (1440, 900),
    fingerprint_stable: bool = True,
    chrome_path: str | None = None,
    extra_launch: Mapping | None = None,
    extra_context: Mapping | None = None,
    log: Callable[[str], None] | None = None,
) -> dict:
    """统一入口：开一个"像真人"的浏览器环境。

    backend：
      camoufox    Firefox 反指纹内核（xai/zcode/trae 默认）
      chrome_real 系统 Chrome/Edge + 独立持久 profile（github 线用；
                  用户实测 Camoufox 开 github 登录页被风控渲染不出）

    返回 dict（生命周期归调用方，结束调 env['close']()）：
      browser        启动产物（persistent 时是 BrowserContext，否则 Browser）
      context        真 BrowserContext 对象（persistent 时与 browser 同对象）
      context_obj    context 的显式别名（与 context 同对象；便于区别于 kwargs）
      context_kwargs 构建该 context 的 kwargs 字典（时区/locale/viewport 等）
      page           已开的第一个标签页
      backend / is_persistent / launch / meta
      fingerprint    固定指纹 dict（仅 camoufox；chrome_real 为 None）
      close          无参闭包

    【2026-10-05 修复】此前 return 字典里 `"context"` 键重复定义：先放 BrowserContext
    对象、后又被 `context_kw` 字典覆盖 → env["context"] 实际是 kwargs 字典，真对象只在
    env["browser"]。现按文档语义让 env["context"] 返回真 BrowserContext，并把 kwargs
    挪到 env["context_kwargs"]（旧调用方若靠 env["browser"] 取 context 不受影响）。

    必须在专用线程内使用（Playwright sync API 线程亲和，勿与批量线程复用）。
    """
    log = log or (lambda _msg: None)
    backend = str(backend or "camoufox").strip().lower()
    if backend == "camoufox":
        built = build_camoufox_kwargs(
            profile_key=profile_key, proxy=proxy, headless=headless,
            geo_country=geo_country, persist=persist, geoip=geoip,
            os_name=os_name, window=window,
            extra_launch=extra_launch, extra_context=extra_context, log=log,
        )
    elif backend == "chrome_real":
        built = build_chrome_real_kwargs(
            profile_key=profile_key, proxy=proxy, headless=headless,
            geo_country=geo_country, persist=persist, window=window,
            extra_launch=extra_launch, extra_context=extra_context,
        )
    else:
        raise BrowserEnvError(f"未知 backend: {backend!r}（可选 camoufox/chrome_real）")

    launch, context_kw, meta = built["launch"], built["context"], built["meta"]
    meta["backend"] = backend
    fingerprint = None

    if backend == "camoufox":
        # ---- 指纹决策（坑1+坑3）----
        # 优先级：fingerprint.json 快照复用 > 反模式过滤后的确定性 preset
        #        > 按 profile_key 确定性回退 > （最后手段）随机指纹。
        # 快照复用保证"同 profile 永远同指纹"；后两级保证没有快照时也
        # 不再是纯随机（DataDome 看 profile 连续性）。
        fp_snapshot = _load_fingerprint_snapshot(
            profile_key, meta, persist, os_name) if fingerprint_stable else None
        if fp_snapshot is not None:
            fingerprint = fp_snapshot
            meta["fingerprint_source"] = "snapshot"
            try:
                from camoufox.utils import installed_verstr

                launch["ff_version"] = int(installed_verstr().split(".", 1)[0])
            except Exception:  # noqa: BLE001
                pass
            # 快照 UA 可能带旧 rv（内核升级/旧版落盘）→ 同一实现重写
            rewrite_fingerprint_ua(fingerprint, launch.get("ff_version"), log=log)
            log("browser_env: 复用 fingerprint.json 快照（同 profile 同指纹）")
        if fingerprint is None and fingerprint_stable:
            try:
                from camoufox.utils import installed_verstr

                ff_ver = int(installed_verstr().split(".", 1)[0])
                fingerprint = select_preset_for_profile(
                    profile_key, os_name, ff_ver, log=log)
                if fingerprint is not None:
                    launch["ff_version"] = ff_ver  # 启动层按安装版重写 preset UA
                    meta["fingerprint_source"] = "preset"
                    rewrite_fingerprint_ua(fingerprint, ff_ver, log=log)
            except Exception as exc:  # noqa: BLE001
                meta["fallbacks"].append(f"fingerprint:{type(exc).__name__}")
                log(f"browser_env: preset 选择失败（{type(exc).__name__}）")
            if fingerprint is None:
                # 确定性回退（坑3）：按 profile_key 种子生成，同 key 同指纹；
                # 不再退回纯随机 —— 随机指纹跨会话不一致会被风控扣分。
                try:
                    from camoufox.utils import installed_verstr

                    ff_ver2 = int(installed_verstr().split(".", 1)[0])
                except Exception:  # noqa: BLE001
                    ff_ver2 = None
                fingerprint = _fallback_fingerprint_for(
                    profile_key, os_name, window, ff_ver2, log=log)
                if fingerprint is not None:
                    if ff_ver2:
                        launch["ff_version"] = ff_ver2
                    meta["fingerprint_source"] = "deterministic_fallback"
                    log("browser_env: preset 不可用 → 按 profile_key 确定性回退指纹")
        from camoufox.sync_api import Camoufox

        launch_args = dict(launch)
        if fingerprint is not None:
            launch_args["fingerprint_preset"] = fingerprint
            # ff_version 重写与 Camoufox 内部 preset 默认路径完全一致（按安装版
            # 内核重写 UA），显式传参才触发 LeakWarning —— 压掉它。
            launch_args["i_know_what_im_doing"] = True
        # 坑1兜底：preset 若仍炸启动（如 WebGl 数据缺失类），自动换下一个
        # 候选 preset 重试一次（确定性候选环，不引入随机）。
        try:
            browser = Camoufox(**launch_args).start()
        except Exception as first_exc:  # noqa: BLE001
            if fingerprint is None or not fingerprint_stable:
                raise
            meta["fallbacks"].append(f"launch_retry:{type(first_exc).__name__}")
            log(f"browser_env: 启动失败（{type(first_exc).__name__}: "
                f"{str(first_exc)[:120]}）→ 换候选 preset 重试一次")
            fingerprint = _next_candidate_preset(
                profile_key, os_name, launch.get("ff_version"), log=log)
            if fingerprint is None:
                # 候选环只剩一个/查询失败 → 直接确定性回退
                fingerprint = _fallback_fingerprint_for(
                    profile_key, os_name, window,
                    launch.get("ff_version"), log=log)
                if fingerprint is not None:
                    launch_args["fingerprint_preset"] = fingerprint
                    meta["fingerprint_source"] = "deterministic_fallback"
            else:
                launch_args["fingerprint_preset"] = fingerprint
                launch_args["i_know_what_im_doing"] = True
                meta["fingerprint_source"] = "preset_retry"
                rewrite_fingerprint_ua(fingerprint, launch.get("ff_version"), log=log)
            try:
                browser = Camoufox(**launch_args).start()
            except Exception as second_exc:  # noqa: BLE001
                if fingerprint is None:  # 回退指纹也炸 → 如实上报
                    raise second_exc
                log("browser_env: 重试仍失败 → 确定性回退指纹（最后一搏）")
                fingerprint = _fallback_fingerprint_for(
                    profile_key, os_name, window,
                    launch.get("ff_version"), log=log)
                if fingerprint is not None:
                    launch_args["fingerprint_preset"] = fingerprint
                    meta["fingerprint_source"] = "deterministic_fallback"
                browser = Camoufox(**launch_args).start()
        is_persistent = bool(launch_args.get("persistent_context"))
    else:  # chrome_real
        exe = chrome_path or find_chrome()
        if not exe:
            raise BrowserEnvError("chrome_real 后端未找到系统 Chrome/Edge，请先安装或传 chrome_path")
        meta["chrome_path"] = exe
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        # ---- N7 修复：无头 UA 去 'HeadlessChrome' 保留字（仅 headless 且调用方未
        # 显式给 user_agent 时注入；持久化 merged 与非持久化 new_context 两条路径
        # 都吃 context_kw，一处注入全局生效）。有头分支零影响。 ----
        if headless and "user_agent" not in context_kw:
            _kua = (_probe_kernel_headless_ua(pw, exe, log=log)
                    if _N7_HEADLESS_UA_PROBE else None)
            _ua = build_clean_chromium_ua(exe, headless_ua=_kua)
            if _ua and "HeadlessChrome" not in _ua:
                context_kw["user_agent"] = _ua
                meta["ua_headless_scrubbed"] = True
                meta["ua_source"] = "kernel_token_replace" if _kua else "version_rebuild"
                log(f"browser_env: headless UA 去 HeadlessChrome → {_ua[:90]}")
            else:
                meta["fallbacks"].append("ua_headless_scrub:ver_unknown")
                log("browser_env: headless UA 干净化失败（版本未知），保留 HeadlessChrome 风险")
        launch_args = dict(launch)
        launch_args["executable_path"] = exe
        try:
            udd = launch_args.pop("user_data_dir", None)
            if udd:
                # persistent context：context 层参数（时区/locale/viewport）必须
                # 一并塞进 launch_persistent_context —— 它没有独立 new_context 步骤
                is_persistent = True
                merged = {**launch_args, **context_kw}
                browser = pw.chromium.launch_persistent_context(udd, **merged)
            else:
                is_persistent = False
                browser = pw.chromium.launch(**launch_args)
        except BaseException:
            try:
                pw.stop()
            except Exception:
                pass
            raise
        meta["_playwright"] = pw  # close 时需要 stop()

    try:
        context_obj = browser if is_persistent else browser.new_context(**context_kw)
        page = context_obj.new_page()
        if backend == "chrome_real":
            # 反自动化痕迹：CDP 层注入（真 Chrome 仍会带 navigator.webdriver=true）
            try:
                context_obj.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                )
            except Exception:
                pass
        # N7 兜底自检（只读、不阻断，仅 chrome_real+headless）：注入后 UA 应无
        # HeadlessChrome 且主版本与 Client Hints 自洽；异常/不一致只记 meta + warn。
        if backend == "chrome_real" and headless:
            try:
                _ua_txt = page.evaluate("navigator.userAgent") or ""
                _brands = page.evaluate(
                    "(() => { try { return navigator.userAgentData ? "
                    "navigator.userAgentData.brands.map(b => b.brand + '/' + b.version) : null; } "
                    "catch (e) { return null; } })()")
                _chk = ua_consistency_check(_ua_txt, _brands)
                meta["ua_check"] = _chk
                if not _chk["ok"]:
                    meta["fallbacks"].append(f"ua_check:{_chk['reason']}")
                    log(f"browser_env: [warn] headless UA 自检未通过 → {_chk}")
            except Exception as _exc:  # noqa: BLE001
                meta["fallbacks"].append(f"ua_check_probe:{type(_exc).__name__}")
        fp_file = _write_snapshot_files(profile_key, meta, fingerprint, context_kw)
        meta["fingerprint_file"] = fp_file
        return {
            "browser": browser,
            "context": context_obj,
            "context_obj": context_obj,  # 显式别名：真 BrowserContext 对象
            "page": page,
            "backend": backend,
            "is_persistent": is_persistent,
            "launch": launch_args,
            "context_kwargs": context_kw,  # 原 kwargs（旧实现被 "context" 重复键覆盖）
            "meta": meta,
            "fingerprint": fingerprint,
            "close": _make_closer(browser, context_obj, is_persistent, meta),
        }
    except BaseException:
        try:
            browser.close()
        except Exception:
            pass
        raise


def _make_closer(browser: Any, context_obj: Any, is_persistent: bool,
                 meta: dict | None = None) -> Callable[[], None]:
    def close() -> None:
        try:
            (context_obj if is_persistent else browser).close()
        except Exception:
            pass
        if not is_persistent:
            try:
                browser.close()
            except Exception:
                pass
        pw = (meta or {}).pop("_playwright", None)
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        # Camoufox 主进程偶发在 close() 后存活（exitCode=0 的假活），锁死 profile
        # 导致下一轮同 profile 秒退。优雅关闭失败 → 兜底杀自家 profile 的孤儿。
        profile_path = (meta or {}).get("profile_path")
        if profile_path:
            try:
                kill_orphan_camoufox(profile_path, timeout=8.0, log=lambda _m: None)
            except Exception:
                pass
    return close


# --------------------------------------------------------------------------- #
# 坑2修复：孤儿 camoufox 进程兜杀（只杀命令行含本 profile 路径的进程）
# 借鉴 scratch/gh_ab_probe.py 的 kill_orphans（A/B 实测已验证有效），
# 从"硬编码 profile 名"泛化为"按 profile 路径/目录名匹配 + psutil 优先"。
# --------------------------------------------------------------------------- #

def _iter_camoufox_processes() -> list[dict]:
    """枚举本机 camoufox 进程 → [{pid, name, cmdline(str)}]，失败返回 []。

    优先 psutil（无则回退 PowerShell Get-CimInstance Win32_Process）。
    """
    try:
        import psutil  # type: ignore

        out = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                info = proc.info or {}
                name = str(info.get("name") or "")
                if "camoufox" not in name.lower():
                    continue
                cmd = info.get("cmdline") or []
                if isinstance(cmd, (list, tuple)):
                    cmd = subprocess.list2cmdline(list(cmd))
                out.append({"pid": int(info.get("pid") or 0),
                            "name": name, "cmdline": str(cmd)})
            except Exception:  # noqa: BLE001  # 进程已退出/权限不足
                continue
        return out
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        pass
    # psutil 不可用：PowerShell 兜底（gh_ab_probe.py 同款，泛化匹配串）
    if sys.platform.startswith("win"):
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='camoufox.exe'\" | "
                 "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
                capture_output=True, timeout=20)
            data = json.loads(r.stdout.decode("gbk", "replace") or "[]")
            if isinstance(data, dict):  # 单条结果时 ConvertTo-Json 不给数组
                data = [data]
            return [{"pid": int(d.get("ProcessId") or 0),
                     "name": str(d.get("Name") or "camoufox.exe"),
                     "cmdline": str(d.get("CommandLine") or "")}
                    for d in data if isinstance(d, dict) and d.get("ProcessId")]
        except Exception:  # noqa: BLE001
            return []
    return []


def kill_orphan_camoufox(profile_dir_or_key: str, *, timeout: float = 10.0,
                         log: Callable[[str], None] | None = None) -> int:
    """杀掉"命令行里含该 profile 路径"的孤儿 camoufox 进程，返回杀掉数。

    安全边界：只匹配命令行里出现 profile 路径（或其安全目录名）的 camoufox
    进程 —— 绝不波及用户自己的 Edge/Chrome、其他子代理的 profile。匹配串
    统一小写比较；路径归一成原生分隔符 + 正斜杠两种形态都试。
    timeout：优雅先等 timeout/2，再强杀剩余。
    """
    log = log or (lambda _m: None)
    if not profile_dir_or_key:
        return 0
    raw = str(profile_dir_or_key)
    safe_key = None
    try:
        safe_key = resolve_profile_key(os.path.basename(raw.rstrip("/\\")))
    except Exception:  # noqa: BLE001
        safe_key = None
    # 匹配串：完整路径（两分隔形态）+ 安全目录名（防命令行只带相对路径）
    needles = set()
    norm = raw.replace("\\\\", "\\")
    for cand in (norm, norm.replace("\\", "/")):
        if len(cand) >= 8:  # 过短串误伤风险高，不参与匹配
            needles.add(cand.lower())
    if safe_key and len(safe_key) >= 8:
        needles.add(safe_key.lower())
    if not needles:
        log(f"kill_orphan_camoufox: 匹配串过短，跳过 ({raw!r})")
        return 0

    try:
        procs = _iter_camoufox_processes()
    except Exception as exc:  # noqa: BLE001
        log(f"kill_orphan_camoufox: 进程枚举失败 {type(exc).__name__}: {exc}")
        return 0
    # 双重防线：进程名必须含 camoufox（防用户 Edge/Chrome 恰好挂着本 profile
    # 路径启动被误杀）+ 命令行含 profile 匹配串。
    targets = [p for p in procs
               if p.get("pid")
               and "camoufox" in str(p.get("name", "")).lower()
               and any(n in str(p.get("cmdline", "")).lower()
                       for n in needles)]
    if not targets:
        return 0

    killed = 0
    deadline = time.monotonic() + max(1.0, float(timeout))
    for p in targets:
        pid = int(p["pid"])
        try:
            try:
                import psutil  # type: ignore

                proc = psutil.Process(pid)
                proc.terminate()  # SIGTERM 语义：给主人 close 流程一个机会
                grace = max(0.5, (deadline - time.monotonic()) / 2)
                try:
                    proc.wait(timeout=grace)
                    killed += 1
                    log(f"kill_orphan_camoufox: terminated pid={pid}")
                    continue
                except Exception:  # noqa: BLE001  # 超时 → 强杀
                    proc.kill()
                    proc.wait(timeout=max(0.5, deadline - time.monotonic()))
                    killed += 1
                    log(f"kill_orphan_camoufox: killed pid={pid}")
                    continue
            except ImportError:
                pass
            # PowerShell/taskkill 路径（gh_ab_probe.py 同款：/T 连子进程）
            grace = max(0.5, (deadline - time.monotonic()) / 2)
            subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True,
                           timeout=grace)
            time.sleep(0.3)
            if _pid_alive(pid):
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=max(1.0, deadline - time.monotonic()))
            killed += 1
            log(f"kill_orphan_camoufox: taskkill pid={pid}")
        except Exception as exc:  # noqa: BLE001
            log(f"kill_orphan_camoufox: pid={pid} kill失败 {type(exc).__name__}: {exc}")
    return killed


def _pid_alive(pid: int) -> bool:
    """进程是否仍存活（psutil 优先，PowerShell 兜底；判定不了按活算）。"""
    try:
        import psutil  # type: ignore

        return psutil.pid_exists(pid)
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        pass
    if sys.platform.startswith("win"):
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"if (Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue) "
                 f"{{ 'alive' }} else {{ 'dead' }}"],
                capture_output=True, timeout=10)
            return "alive" in r.stdout.decode("gbk", "replace")
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 坑1+坑3修复：preset 反模式过滤 + 按 profile_key 确定性选择 / 确定性回退
# --------------------------------------------------------------------------- #

#: WebGL 反模式 token（大小写不敏感）：命中即跳过该 preset。
#: 实测依据（docs/浏览器要求与Camoufox-AB对照-20261005.md）：
#: v150 bundle 180 个 windows preset 中 4 个 "Win 桌面 UA + Adreno650/Generic"
#: 组合，Camoufox 启动层 sample_webgl 炸 `No WebGL data found` → 直接炸启动。
#: - Adreno/Mali：移动 GPU 出现在桌面 OS preset（库缺该组合数据）
#: - Unknown：ANGLE Generic Renderer（同样查库必炸）
#: - SwiftShader/llvmpipe/software：软件渲染（一眼假 + 库常缺数据）
_WEBGL_ANTIPATTERN_TOKENS = (
    "adreno", "mali", "unknown", "swiftshader", "llvmpipe", "softpipe",
    "software", "angle (unknown",
)

#: 与 os_name 明显不匹配的 GPU 语境（webgl 字段文本命中即跳过）。
_OS_MISMATCH_TOKENS = {
    "windows": ("apple m1", "apple m2", "apple m3", "apple gpu"),
    "macos": ("direct3d",),
    "linux": ("apple m1", "apple m2", "apple m3", "apple gpu", "direct3d"),
}

#: filter_preset_antipatterns(webgl_check=...) 的哨兵：未显式传参。
_UNSET = object()

#: sample_webgl 的 OS 键映射（camoufox 内部用 win/mac/lin）。
_WEBGL_OS_MAP = {"windows": "win", "macos": "mac", "linux": "lin"}


def _default_webgl_check(os_name: str) -> Callable[[str | None, str | None], None] | None:
    """构造"该 (vendor, renderer) 在本 OS 下 WebGl 库有数据"的精确预检函数。

    直接复用 camoufox 启动层同一把尺子（camoufox.webgl.sample.sample_webgl，
    纯 sqlite 查库、无浏览器无网络）：preset 过了它 = 启动层 sample_webgl
    必不炸 `No WebGL data found`。库不可用时返回 None（只靠 token 快筛）。
    """
    os_key = _WEBGL_OS_MAP.get(str(os_name or "").lower())
    if not os_key:
        return None

    def check(vendor: str | None, renderer: str | None) -> None:
        from camoufox.webgl.sample import sample_webgl

        sample_webgl(os_key, vendor, renderer)  # ValueError → 视为坏 preset

    return check


def _preset_webgl_text(preset: Mapping) -> str:
    """preset 的 webgl vendor+renderer 拼接文本（小写，容缺字段）。"""
    wg = (preset or {}).get("webgl") or {}
    vend = str(wg.get("unmaskedVendor") or "")
    rend = str(wg.get("unmaskedRenderer") or "")
    return f"{vend} {rend}".lower()


def filter_preset_antipatterns(
    presets: list | None,
    os_name: str = "windows",
    log: Callable[[str], None] | None = None,
    *,
    webgl_check: Callable[[str | None, str | None], None] | None | object = _UNSET,
) -> list:
    """过滤"启动必炸/一眼假"的 preset（WebGL 反模式 + 平台错配），返回新列表。

    两级防线：
      1. token 快筛（Adreno/Mali/Unknown/SwiftShader/软件渲染/平台错配）——
         免费，但实测漏抓正常名字却库里没数据的组合（如 Intel Arc A750）；
      2. webgl_check 精确预检（缺省=camoufox 自己的 sample_webgl 查库，
         与启动层同一把尺子）——查库抛异常即判坏。带 (vendor,renderer)
         结果缓存，180 个 preset 预检 <0.2s；webgl_check=None 可关掉（离线单测
         用 mock 注入）。
    不改输入列表；全部被滤掉时返回 []（调用方走确定性回退）。
    纯函数（不 load_presets、不起浏览器）——离线单测目标。
    """
    log = log or (lambda _m: None)
    if not presets:
        return []
    if webgl_check is _UNSET:
        webgl_check = _default_webgl_check(os_name)
    os_key = str(os_name or "windows").lower()
    mismatch = _OS_MISMATCH_TOKENS.get(os_key, ())
    kept: list = []
    check_cache: dict[tuple[str | None, str | None], bool] = {}
    for i, preset in enumerate(presets):
        wg = (preset or {}).get("webgl") or {}
        vend = wg.get("unmaskedVendor")
        rend = wg.get("unmaskedRenderer")
        text = f"{vend or ''} {rend or ''}".lower()
        hit = next((tok for tok in _WEBGL_ANTIPATTERN_TOKENS if tok in text), None)
        if hit is None:
            hit = next((tok for tok in mismatch if tok in text), None)
        if hit is not None:
            log(f"browser_env: preset[{i}] 命中反模式 {hit!r}，跳过: "
                f"{text[:90]}")
            continue
        if webgl_check is not None and wg:
            key = (vend, rend)
            ok = check_cache.get(key)
            if ok is None:
                try:
                    webgl_check(vend, rend)
                    ok = True
                except Exception as exc:  # noqa: BLE001  # ValueError 等 → 坏
                    ok = False
                    log(f"browser_env: preset[{i}] WebGl 库预检失败，跳过: "
                        f"{str(exc)[:100]}")
                check_cache[key] = ok
            if not ok:
                continue
        kept.append(preset)
    return kept


def _deterministic_preset_index(safe_key: str, n: int) -> int:
    """profile_key 安全名 → [0, n) 确定性索引（sha256 前 4 字节，稳定跨进程）。"""
    digest = hashlib.sha256(f"preset:{safe_key}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % n


def select_preset_for_profile(
    profile_key: str,
    os_name: str = "windows",
    ff_version: int | str | None = None,
    *,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    """按 profile_key 确定性挑一个"过了反模式过滤"的官方 preset。

    确定性语义：同 profile_key + 同 preset 库 → 永远同一个 preset（不因过滤
    而变成随机）；过滤只是把坏 preset 从候选环里摘掉，索引仍在剩余列表上
    按 key 哈希取。无可用 preset 时返回 None（调用方走确定性指纹回退）。
    纯查表 + 过滤 + 哈希，不起浏览器 —— 离线可测。
    """
    from camoufox.fingerprints import load_presets

    log = log or (lambda _m: None)
    try:
        db = load_presets(ff_version) if ff_version else load_presets()
    except Exception as exc:  # noqa: BLE001
        log(f"browser_env: load_presets 失败 {type(exc).__name__}: {exc}")
        return None
    plist = ((db or {}).get("presets") or {}).get(str(os_name).lower()) or []
    if not plist:
        return None
    safe = resolve_profile_key(profile_key)
    kept = filter_preset_antipatterns(plist, os_name, log=log)
    if not kept:
        log(f"browser_env: {os_name} preset 全部命中反模式（{len(plist)} 个），走确定性回退")
        return None
    idx = _deterministic_preset_index(safe, len(kept))
    chosen = kept[idx]
    # 原始索引只用于日志排查（反模式滤掉的序号不再参与选择）
    orig_idx = plist.index(chosen) if chosen in plist else -1
    log(f"browser_env: preset for {safe!r}: filtered_idx={idx}/{len(kept)} "
        f"(raw_idx={orig_idx}/{len(plist)})")
    return dict(chosen)


def _ff_version_str(ff_version: int | str | None) -> str | None:
    """ff_version 归一：None → 查安装内核主版本；查不到 → None。"""
    if ff_version is None:
        try:
            from camoufox.utils import installed_verstr

            ff_version = str(installed_verstr().split(".", 1)[0])
        except Exception:  # noqa: BLE001
            return None
    return str(ff_version).split(".", 1)[0]


def rewrite_fingerprint_ua(fingerprint: dict, ff_version: int | str | None = None,
                           log: Callable[[str], None] | None = None) -> dict:
    """把 preset 形状指纹的 UA rv/Firefox 版本重写成安装内核版本（原地修改）。

    【UA-内核一致性，单一实现】preset/快照/回退生成的 UA 可能带旧 rv（如
    browserforge 按 150 采样、真内核是 152）。DataDome 交叉比对 UA rv 与
    内核行为是经典检测项（9 月十几轮全败的根因，已修过一次）——三条指纹
    路径（preset / snapshot / deterministic_fallback）都必须过这里。
    与 camoufox from_preset 同一正则（Firefox/\\d+\\.0 与 rv:\\d+\\.0）。
    """
    log = log or (lambda _m: None)
    ver = _ff_version_str(ff_version)
    nav = (fingerprint or {}).get("navigator")
    ua = (nav or {}).get("userAgent")
    if not ver or not isinstance(nav, dict) or not isinstance(ua, str):
        return fingerprint
    headers = fingerprint.get("headers") if isinstance(fingerprint.get("headers"), dict) else None
    new_ua = re.sub(r"Firefox/\d+\.0", f"Firefox/{ver}.0", ua)
    new_ua = re.sub(r"rv:\d+\.0", f"rv:{ver}.0", new_ua)
    if new_ua != ua:
        nav["userAgent"] = new_ua
        if headers and isinstance(headers.get("useragent"), str):
            headers["useragent"] = re.sub(r"Firefox/\d+\.0", f"Firefox/{ver}.0",
                                          headers["useragent"])
        log(f"browser_env: UA 按内核 v{ver} 重写: {ua[:40]} -> {new_ua[:40]}")
    return fingerprint


def _fallback_fingerprint_for(
    profile_key: str,
    os_name: str = "windows",
    window: tuple[int, int] = (1440, 900),
    ff_version: int | str | None = None,
    *,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    """坑3修复：随机指纹回退改为"按 profile_key 确定性"。

    机制（已实测验证）：camoufox 的 BrowserForge 采样器（贝叶斯网络）走
    **全局 random 模块**——`random.seed(profile_key 派生种子)` 后
    generate_fingerprint 每次产出完全一致（同进程与跨进程均验证）。

    GPU/WebGL 三道保险（历史坑：browserforge dict 的 GPU 在 `videoCard`，
    直接丢掉会让启动层走 numpy 随机 sample_webgl_for_screen → 每次启动
    GPU 都变，等于把坑3换个地方复发）：
      1. videoCard → webgl 映射；
      2. sample_webgl 查库验证该 pair 本 OS 有数据（与启动层同尺子）；
      3. 验证不过 → 从 get_possible_pairs() 里按 key 确定性挑一个有效 pair
         （sample_webgl 给定 pair 后是纯 SQL 查询，零随机）。
    最后统一走 rewrite_fingerprint_ua（UA-内核一致，与 preset 路径同一实现）。
    """
    from camoufox.fingerprints import generate_fingerprint

    log = log or (lambda _m: None)
    ver = _ff_version_str(ff_version)
    safe = resolve_profile_key(profile_key)
    seed = int(hashlib.sha256(f"fp-fallback:{safe}".encode("utf-8")).hexdigest()[:16], 16)
    state = random.getstate()  # 保护全局 random：只短暂接管，不污染调用方随机流
    try:
        random.seed(seed)
        fp = generate_fingerprint(window=tuple(window), os=str(os_name).lower())
    except Exception as exc:  # noqa: BLE001
        log(f"browser_env: 确定性回退生成失败 {type(exc).__name__}: {exc}")
        return None
    finally:
        random.setstate(state)
    try:
        obj = json.loads(fp.dumps())
    except Exception as exc:  # noqa: BLE001
        log(f"browser_env: 确定性回退序列化失败 {type(exc).__name__}: {exc}")
        return None
    vc = obj.get("videoCard") if isinstance(obj.get("videoCard"), dict) else {}
    webgl = {
        "unmaskedVendor": vc.get("vendor") or "",
        "unmaskedRenderer": vc.get("renderer") or "",
    }
    # ---- WebGL 数据有效性：库验证，失败则确定性换有效 pair ----
    os_key = _WEBGL_OS_MAP.get(str(os_name).lower())
    if os_key and (webgl["unmaskedVendor"] or webgl["unmaskedRenderer"]):
        try:
            from camoufox.webgl.sample import sample_webgl

            sample_webgl(os_key, webgl["unmaskedVendor"], webgl["unmaskedRenderer"])
        except Exception as exc:  # noqa: BLE001
            log(f"browser_env: 回退 GPU 库验证失败({str(exc)[:60]}) → 按确定性选有效 pair")
            _use_deterministic_webgl_pair(safe, os_key, webgl, log=log)
    elif os_key:
        _use_deterministic_webgl_pair(safe, os_key, webgl, log=log)
    out = {
        "navigator": obj.get("navigator") or {},
        "screen": obj.get("screen") or {},
        "webgl": webgl,
    }
    rewrite_fingerprint_ua(out, ver, log=log)
    log(f"browser_env: 确定性回退指纹(seed={seed % 10**8}) UA="
        f"{(out['navigator'].get('userAgent') or '')[:60]} "
        f"GPU={(out['webgl'].get('unmaskedRenderer') or '')[:44]}")
    return out


def _use_deterministic_webgl_pair(safe_key: str, os_key: str,
                                  webgl: dict, *,
                                  log: Callable[[str], None] | None = None) -> None:
    """从 WebGl 库本 OS 的有效 pair 里按 profile_key 确定性挑一个填入 webgl。

    有效 pair 列表按库名排序（库更新会插行，排序键仍稳定；排序变更才可能
    换 pair —— 可接受，好过随机或空 GPU）。sample_webgl 给定 pair 后是纯
    SQL 查询，注入后启动层零随机。
    """
    from camoufox.webgl.sample import get_possible_pairs, sample_webgl

    log = log or (lambda _m: None)
    try:
        pairs = sorted(get_possible_pairs().get(os_key) or [])
    except Exception as exc:  # noqa: BLE001
        log(f"browser_env: get_possible_pairs 失败 {type(exc).__name__}: {exc}")
        return
    if not pairs:
        return
    digest = hashlib.sha256(f"webgl-pair:{safe_key}".encode("utf-8")).digest()
    vend, rend = pairs[int.from_bytes(digest[:4], "big") % len(pairs)]
    webgl["unmaskedVendor"] = vend
    webgl["unmaskedRenderer"] = rend
    log(f"browser_env: 确定性 WebGl pair({os_key}): {rend[:60]}")


def _load_fingerprint_snapshot(profile_key: str, meta: dict, persist: bool,
                               os_name: str = "windows") -> dict | None:
    """坑3落盘复用：读回 fingerprint.json 里的 camoufox_fingerprint 快照。

    快照必须是含 navigator/screen/webgl 的 preset 形状 dict 才有效（防旧版
    / 损坏文件注入炸启动）。persist=False 时 profile 目录根本没落盘，直接 None。
    """
    if not persist:
        return None
    base = meta.get("profile_path") or profile_dir(profile_key)
    fp_file = os.path.join(base, "fingerprint.json")
    try:
        with open(fp_file, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:  # noqa: BLE001  # 不存在/损坏 → 当无快照
        return None
    snap = payload.get("camoufox_fingerprint") if isinstance(payload, dict) else None
    if not isinstance(snap, dict):
        return None
    nav = snap.get("navigator") or {}
    if not isinstance(nav, dict) or not nav.get("userAgent"):
        return None
    if not (isinstance(snap.get("screen"), dict) and snap["screen"]
            and isinstance(snap.get("webgl"), dict)):
        return None
    # 反模式防线也适用于快照：快照本身若来自坏 preset，弃用
    if not filter_preset_antipatterns([snap], os_name, log=lambda _m: None):
        return None
    return snap


def _next_candidate_preset(profile_key: str, os_name: str,
                           ff_version, *,
                           log: Callable[[str], None] | None = None) -> dict | None:
    """启动重试用：确定性换"下一个"候选 preset（同 key 同顺序 → 确定性）。

    与 select_preset_for_profile 同一候选环，只是索引 +1（环形回绕）。
    """
    from camoufox.fingerprints import load_presets

    log = log or (lambda _m: None)
    try:
        db = load_presets(ff_version) if ff_version else load_presets()
    except Exception as exc:  # noqa: BLE001
        log(f"browser_env: load_presets 失败 {type(exc).__name__}: {exc}")
        return None
    plist = ((db or {}).get("presets") or {}).get(str(os_name).lower()) or []
    if not plist:
        return None
    kept = filter_preset_antipatterns(plist, os_name, log=log)
    if len(kept) < 2:
        return None
    safe = resolve_profile_key(profile_key)
    idx = (_deterministic_preset_index(safe, len(kept)) + 1) % len(kept)
    log(f"browser_env: 重试候选 preset filtered_idx={idx}/{len(kept)}")
    return dict(kept[idx])


def _stable_fingerprint_for(profile_key: str, os_name: str = "windows",
                            ff_version: int | str | None = None) -> dict | None:
    """（已由 select_preset_for_profile 取代；保留供旧调用兼容）

    profile_key 派生固定指纹 preset dict（同 key → 同 UA/屏幕/WebGL 噪声）。

    用 Camoufox 官方真实指纹 preset（load_presets），按 profile_key 哈希确定性
    挑选：preset 是纯 dict、可 json 落盘、每次启动注入完全一致 —— 比
    BrowserForge 随机生成 + fingerprint 对象透传更稳（对象路径要求嵌套
    dataclass，dict 会在 check_custom_fingerprint 炸 'no attribute navigator'）。
    必须配 ff_version（安装版内核主版本）一起传给 launch，否则 preset UA 的
    rv 与真实内核不一致（被 UA-内核版本交叉检测抓包）。
    无可用 preset 时返回 None（调用方退回 Camoufox 随机指纹，不炸流程）。

    ff_version：安装版 Firefox 主版本号；传入后选 v150 bundle 且启动层会把
    preset UA 重写成该版本（否则 preset rv 与真内核 rv 不一致 —— UA 漏洞）。
    """
    from camoufox.fingerprints import load_presets

    db = load_presets(ff_version) if ff_version else load_presets()
    plist = (db or {}).get("presets") or {}
    plist = plist.get(str(os_name).lower()) or []
    if not plist:
        return None
    digest = hashlib.sha256(resolve_profile_key(profile_key).encode("utf-8")).digest()
    idx = (digest[0] << 8 | digest[1]) % len(plist)
    return dict(plist[idx])


def fingerprint_snapshot(env: dict, page: Any | None = None) -> dict:
    """从活页面抓"实际生效"指纹（navigator/webdriver/时区/屏幕/WebGL）。

    env 是 make_context 返回值；page 缺省取 env['page']。结果合并回
    env['meta']['fingerprint_file'] 指向的 JSON（live_page 字段）。
    """
    if page is None:
        page = env["page"]
    js = r"""
    () => {
      const nav = navigator;
      let webglVendor = null, webglRenderer = null, webglHash = null;
      try {
        const c = document.createElement('canvas');
        const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
        if (gl) {
          const dbg = gl.getExtension('WEBGL_debug_renderer_info');
          webglVendor = dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL)
                            : gl.getParameter(gl.VENDOR);
          webglRenderer = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)
                              : gl.getParameter(gl.RENDERER);
          const g2 = c.getContext('2d');
          if (g2) {
            g2.textBaseline = 'top'; g2.font = "15px 'Arial'";
            g2.fillStyle = '#f60'; g2.fillRect(0, 0, 100, 20);
            g2.fillStyle = '#069'; g2.fillText('fp-snap', 2, 2);
            webglHash = c.toDataURL().slice(-48);
          }
        }
      } catch (e) {}
      return {
        userAgent: nav.userAgent,
        webdriver: nav.webdriver,
        platform: nav.platform,
        language: nav.language,
        languages: (nav.languages || []).slice(0, 6),
        timezone: (Intl.DateTimeFormat().resolvedOptions().timeZone) || null,
        timezoneOffset: new Date().getTimezoneOffset(),
        hardwareConcurrency: nav.hardwareConcurrency,
        screen: {w: screen.width, h: screen.height,
                 availW: screen.availWidth, availH: screen.availHeight,
                 dpr: window.devicePixelRatio || 1},
        webglVendor: webglVendor, webglRenderer: webglRenderer, webglHash: webglHash,
        cdpArtifact: (typeof window.cdc_adoQpoasnfa76pfcZLmcfl_Array !== 'undefined'
                      || typeof window.cdc_adoQpoasnfa76pfcZLmcfl_Promise !== 'undefined'),
      };
    }
    """
    try:
        live = page.evaluate(js)
        if not isinstance(live, dict):
            live = {"error": f"unexpected evaluate result: {type(live).__name__}"}
    except Exception as exc:  # noqa: BLE001
        live = {"error": f"page.evaluate failed: {type(exc).__name__}: {exc}"}
    meta = env.get("meta") or {}
    fp = {
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "profile_key": meta.get("profile_key"),
        "live": live,
    }
    fp_file = meta.get("fingerprint_file")
    if fp_file:
        try:
            with open(fp_file, "r", encoding="utf-8") as fh:
                prior = json.load(fh)
        except Exception:
            prior = {}
        prior["live_page"] = live
        prior["live_captured_at"] = fp["captured_at"]
        try:
            with open(fp_file, "w", encoding="utf-8") as fh:
                json.dump(prior, fh, ensure_ascii=False, indent=1, default=str)
        except Exception:
            pass
    return fp


def _write_snapshot_files(profile_key: str, meta: dict, fingerprint: Any,
                          context_kw: dict) -> str:
    """指纹快照 + meta 落盘（browser_profiles/<key>/fingerprint.json + env_meta.json）。

    preset 的 UA 原样落盘的是"重写前"的值；再补一份按 ff_version 重写后的
    effective UA（与 launch_options.from_preset 同一正则），排查时直接对得上。
    """
    base = meta.get("profile_path") or profile_dir(profile_key, create=True)
    os.makedirs(base, exist_ok=True)
    fp_file = os.path.join(base, "fingerprint.json")
    payload = {
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "profile_key": meta.get("profile_key"),
        "camoufox_fingerprint": _jsonable(fingerprint),
        "context_kwargs": context_kw,
    }
    if isinstance(fingerprint, dict):
        ff_ver = None
        try:
            from camoufox.utils import installed_verstr

            ff_ver = str(installed_verstr().split(".", 1)[0])
        except Exception:
            pass
        nav = fingerprint.get("navigator") or {}
        ua = nav.get("userAgent")
        if ua and ff_ver:
            payload["effective_user_agent"] = (
                re.sub(r"Firefox/\d+\.0", f"Firefox/{ff_ver}.0", ua)
            )
            payload["effective_user_agent"] = re.sub(
                r"rv:\d+\.0", f"rv:{ff_ver}.0", payload["effective_user_agent"]
            )
    try:
        with open(fp_file, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1, default=str)
    except Exception:
        fp_file = ""
    try:
        with open(os.path.join(base, "env_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=1, default=str)
    except Exception:
        pass
    return fp_file


def _jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, Mapping):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "dumps"):
        try:
            return json.loads(obj.dumps())
        except Exception:
            pass
    return str(obj)


# --------------------------------------------------------------------------- #
# 类人行为助手（滑块/点选/表单复用；轨迹生成不需要浏览器，可离线测）
# --------------------------------------------------------------------------- #

def random_delay(a: float = 0.35, b: float = 1.2) -> float:
    """随机停顿秒数（默认 0.35-1.2s）。真睡；关键步骤别省这一步。"""
    lo, hi = float(a), float(b)
    if lo < 0 or hi < lo:
        raise BrowserEnvError(f"random_delay 需 0 <= a <= b: {a!r}, {b!r}")
    secs = random.uniform(lo, hi)
    time.sleep(secs)
    return secs


def _bezier(p0: tuple, p1: tuple, p2: tuple, t: float) -> tuple:
    x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
    y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
    return (x, y)


def human_path(x0: float, y0: float, x1: float, y1: float,
               steps: int = 0, jitter: float = 1.6) -> list:
    """两点间类人轨迹：二次贝塞尔 + 垂直抖动 + 首尾收敛。

    纯函数 —— 不需要浏览器即可测（steps/jitter 校验、点数、首尾点）。
    steps=0 时按距离自动取 8-48 段。
    """
    if not isinstance(steps, int) or isinstance(steps, bool) or steps < 0:
        raise BrowserEnvError(f"steps 必须是非负整数: {steps!r}")
    if jitter < 0:
        raise BrowserEnvError(f"jitter 不能为负: {jitter!r}")
    dist = math.hypot(x1 - x0, y1 - y0)
    if steps == 0:
        steps = max(8, min(48, int(dist / 12)))
    if dist < 0.5:
        return [(float(x1), float(y1))]
    side = random.choice((-1, 1))
    off = side * min(dist * 0.22, 55.0) * (0.55 + random.random() * 0.6)
    mx = (x0 + x1) / 2 - (y1 - y0) / dist * off
    my = (y0 + y1) / 2 + (x1 - x0) / dist * off
    pts = []
    for i in range(steps + 1):
        t = i / steps
        bx, by = _bezier((x0, y0), (mx, my), (x1, y1), t)
        edge = math.sin(math.pi * t)  # 首尾收敛：起落点不抖
        bx += random.uniform(-jitter, jitter) * edge
        by += random.uniform(-jitter, jitter) * edge
        pts.append((bx, by))
    pts[-1] = (float(x1), float(y1))
    return pts


def human_move_click(page: Any, x: float, y: float, *,
                     steps: int = 0, jitter: float = 1.6,
                     pre_delay: tuple = (0.06, 0.22),
                     settle_delay: tuple = (0.08, 0.30),
                     start: tuple | None = None) -> None:
    """类人移动到 (x, y) 并点击：贝塞尔轨迹 + 分段变速 + 点击前后微停顿。

    page 只需暴露 mouse.move/down/up（Playwright/Camoufox 原生兼容）。
    start=None 时从 (0,0) 起步（Playwright mouse 初始位置即视口原点）。
    """
    if page is None or not hasattr(page, "mouse"):
        raise BrowserEnvError("human_move_click 需要 page.mouse（Playwright page）")
    x0, y0 = (float(start[0]), float(start[1])) if start else (0.0, 0.0)
    pts = human_path(x0, y0, float(x), float(y), steps=steps, jitter=jitter)
    time.sleep(random.uniform(*pre_delay))
    n = len(pts)
    for i, (px, py) in enumerate(pts):
        page.mouse.move(px, py, steps=1)
        # 分段变速：中段快、首尾慢（人类手部加减速特征）
        ease = math.sin(math.pi * (i / max(1, n - 1)))
        time.sleep(0.004 + 0.018 * (1 - ease))
    page.mouse.down()
    time.sleep(random.uniform(*pre_delay))
    page.mouse.up()
    time.sleep(random.uniform(*settle_delay))


def human_type(page: Any, selector_or_el: Any, text: str, *,
               cps: float = 9.0, typo_rate: float = 0.045,
               click_first: bool = True) -> None:
    """类人打字：点击聚焦 → 清空 → 逐字符随机延迟 + 按概率打错再退格修正。

    selector_or_el：locator / ElementHandle / selector 字符串均可。
    cps=每秒字符数基线（键间延迟 ≈ 1/cps 高斯抖动）；typo_rate=每字符打错概率。
    """
    if page is None or not hasattr(page, "keyboard"):
        raise BrowserEnvError("human_type 需要 page.keyboard（Playwright page）")
    if cps <= 0:
        raise BrowserEnvError(f"cps 必须为正: {cps!r}")
    if not 0 <= typo_rate <= 1:
        raise BrowserEnvError(f"typo_rate 需在 [0,1]: {typo_rate!r}")
    if not isinstance(text, str):
        raise BrowserEnvError("text 必须是字符串")
    el = selector_or_el
    if isinstance(el, str):
        el = page.locator(el).first
    if click_first and el is not None:
        try:
            box = el.bounding_box()
            if box:
                human_move_click(page, box["x"] + box["width"] / 2.0,
                                 box["y"] + box["height"] / 2.0, steps=12)
            else:
                el.click(timeout=3000)
        except Exception:
            try:
                el.click(timeout=3000)
            except Exception:
                pass
    if el is not None:
        try:
            el.fill("")
        except Exception:
            pass
    for ch in text:
        page.keyboard.type(ch, delay=0)
        if ch != " " and random.random() < typo_rate:
            wrong = random.choice("qwertyuiopasdfghjklzxcvbnm".replace(ch, ""))
            page.keyboard.type(wrong, delay=0)
            time.sleep(random.uniform(0.12, 0.34))
            page.keyboard.press("Backspace")
            time.sleep(random.uniform(0.04, 0.12))
        time.sleep(max(0.0, random.gauss(1.0 / cps, 0.35 / cps)))


def human_scroll(page: Any, *, total: int = 600, direction: int = 1,
                 step_min: int = 80, step_max: int = 260,
                 pause: tuple = (0.12, 0.45)) -> None:
    """类人滚动：随机步长 + 步间停顿（Playwright mouse.wheel）。"""
    if page is None or not hasattr(page, "mouse"):
        raise BrowserEnvError("human_scroll 需要 page.mouse（Playwright page）")
    if direction not in (1, -1):
        raise BrowserEnvError(f"direction 只能 1/-1: {direction!r}")
    if total <= 0:
        raise BrowserEnvError(f"total 必须为正: {total!r}")
    moved = 0
    while moved < total:
        chunk = random.randint(max(20, step_min), max(step_min, step_max))
        chunk = min(chunk, total - moved)
        page.mouse.wheel(0, direction * chunk)
        moved += chunk
        time.sleep(random.uniform(*pause))


# --------------------------------------------------------------------------- #
# 页面证据 + github 可用性探测 + 真实冒烟自检
# --------------------------------------------------------------------------- #

def save_page_evidence(page: Any, out_dir: str, name: str) -> dict:
    """页面证据落盘：截图 + HTML + 标题/URL。失败字段带 error，不抛。"""
    os.makedirs(out_dir, exist_ok=True)
    ev: dict[str, Any] = {}
    try:
        ev["url"] = page.url
    except Exception:
        ev["url"] = None
    try:
        ev["title"] = page.title()
    except Exception:
        ev["title"] = None
    shot = os.path.join(out_dir, f"{name}.png")
    try:
        page.screenshot(path=shot, full_page=True)
        ev["screenshot"] = shot
    except Exception as exc:  # noqa: BLE001
        ev["screenshot_error"] = f"{type(exc).__name__}: {exc}"
    html = os.path.join(out_dir, f"{name}.html")
    try:
        with open(html, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(page.content())
        ev["html"] = html
    except Exception as exc:  # noqa: BLE001
        ev["html_error"] = f"{type(exc).__name__}: {exc}"
    return ev


def _github_probe(page: Any, out_dir: str) -> dict:
    """github.com/login 可用性探测（用户实测 Camoufox 在此被风控）。

    判定渲染成功 = 出现登录表单输入框（#login_field / input[name=login]）。
    记录：HTTP 状态、标题、表单可见性、挑战页迹象（captcha-delivery/
    x-dd-b/PerimeterX/白屏）。如实记录，不美化。截图+HTML 落盘。
    """
    out: dict[str, Any] = {"url": "https://github.com/login"}
    status = None
    try:
        resp = page.goto("https://github.com/login",
                         wait_until="domcontentloaded", timeout=45000)
        status = resp.status if resp is not None else None
    except Exception as exc:  # noqa: BLE001
        out["goto_error"] = f"{type(exc).__name__}: {exc}"
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(2000)
    out["http_status"] = status
    try:
        out["final_url"] = page.url
    except Exception:
        out["final_url"] = None
    try:
        out["title"] = page.title()
    except Exception:
        out["title"] = None

    # 登录表单是否真的渲染出来
    form_found = False
    form_selector = None
    for sel in ("#login_field", "input[name='login']", "input[autocomplete='username']"):
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                form_found = True
                form_selector = sel
                break
        except Exception:
            continue
    out["login_form_visible"] = form_found
    out["login_form_selector"] = form_selector

    body_text = ""
    try:
        body_text = (page.locator("body").inner_text(timeout=3000) or "")[:600]
    except Exception:
        pass
    low = body_text.lower()
    markers = {
        "captcha_delivery": "captcha-delivery" in (page.content()[:8000].lower()),
        "dd_breadcrumb": "x-dd-b" in low or "datadome" in low,
        "perimeterx": "perimeterx" in low or "px-captcha" in low,
        "white_screen": len(body_text.strip()) < 20,
        "challenge_word": ("verify" in low and "human" in low) or "are you a robot" in low,
    }
    out["challenge_markers"] = {k: v for k, v in markers.items() if v}
    out["body_text_head"] = body_text[:200]
    ev = save_page_evidence(page, out_dir, "github_login")
    out["evidence"] = ev
    out["render_ok"] = bool(form_found)
    return out


def browser_selfcheck(url: str = "https://bot.sannysoft.com/",
                      *, out_dir: str | None = None,
                      profile_key: str = "selfcheck",
                      proxy: str | Mapping | None = None,
                      headless: bool = True,
                      geo_country: str | None = None,
                      backend: str = "camoufox",
                      check_github: bool = True) -> dict:
    """真实冒烟：make_context → 反检测自检站点 → github 可用性 → 全部证据落盘。

    起不来/抓不到时 result['ok']=False 且带 'error' —— 绝不报假成功。
    落盘：out_dir/{selfcheck_<key>.json, selfcheck_<key>.png, github_login.*}。
    """
    result: dict[str, Any] = {"ok": False, "url": url, "profile_key": profile_key,
                              "backend": backend}
    if out_dir is None:
        out_dir = os.path.join(
            os.path.expanduser("~"), "AppData", "Local", "hermes",
            "cache", "scratch", "browserenv",
        )
    os.makedirs(out_dir, exist_ok=True)
    env = None
    try:
        env = make_context(profile_key=f"selfcheck/{profile_key}",
                           proxy=proxy, headless=headless,
                           geo_country=geo_country, persist=True, backend=backend)
        page = env["page"]
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        fp = fingerprint_snapshot(env, page)
        result["fingerprint"] = fp
        shot = os.path.join(out_dir, f"selfcheck_{profile_key}.png")
        try:
            page.screenshot(path=shot, full_page=True)
            result["screenshot"] = shot
        except Exception as exc:  # noqa: BLE001
            result["screenshot_error"] = f"{type(exc).__name__}: {exc}"
        live = fp.get("live") or {}
        result["ok"] = ("error" not in live) and bool(live.get("userAgent"))
        if not result["ok"]:
            result["error"] = live.get("error") or "live fingerprint incomplete"
        result["out_dir"] = out_dir

        # github 可用性（用户实测 Camoufox 被拦——这里如实记录）
        if check_github:
            try:
                result["github"] = _github_probe(page, out_dir)
            except Exception as exc:  # noqa: BLE001
                result["github"] = {"render_ok": False,
                                    "error": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if env is not None:
            try:
                env["close"]()
            except Exception:
                pass
    try:
        with open(os.path.join(out_dir, f"selfcheck_{profile_key}.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=1, default=str)
    except Exception:
        pass
    return result
