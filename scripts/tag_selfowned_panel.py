# -*- coding: utf-8 -*-
"""自有号面板打标降权（双流调度 §6.A/§6.B 落地件）——NRClient 会话走 REST。

动作：对 self_owned_accounts 在册 7 条连接逐条
  PUT /api/providers/<全uuid> {"name": "own:...", "priority": <大数>}
  （9R route.js PUT 白名单解构：只传 name/priority，其余字段零触碰；
   单次 PUT 同时改两字段。禁用状态也改 name 备启用。）

priority 语义修正（2026-10-04 实测 connectionsRepo.js reorderInTx）：
  本构建在 PUT 带 priority 时必触发 reorderInTx——把该 provider 池内所有连接
  按序重编号为连续 1..N。绝对值 200 落库瞬间即被归一化（PUT 返回体里是 200，
  库里已变成池内位次）。因此本脚本的目标状态不是「priority=200」而是其等价
  达成态：**每条 own: 连接在其 provider 池内排在所有产线号之后（池尾）**。
  fill-first 选路按 priority 升序取首个可用——池尾=只在产线耗尽/冷却后兜底，
  与双流设计 §6.B 的意图完全一致。cdeb04f7 已借此从 pos1（fill-first 下最先
  被烧）移到 grok-cli 池 114/114 队尾。

大数取 1_000_000：只要大于池内现最大 priority 即触发 reorder 归一化到队尾，
reorder 随后把它压回 1..N，不会留下巨值。幂等：已达标（name 前缀对 + 池尾）
则跳过不写。**批量导入后需复跑本脚本**：新产线号按 MAX(priority)+1 入池，
会插到自有号之后，复跑即把自有号重新压回池尾。

只动 7 条自有连接，产线号零触碰（PUT 白名单只带 name/priority）。
用法：
    python scripts/tag_selfowned_panel.py            # 实跑（幂等）
    python scripts/tag_selfowned_panel.py --dry-run  # 只读回显计划
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

RETAIL_PRIORITY = 1_000_000   # 任意大于池内最大值即可；reorderInTx 会归一化回 1..N

# own:<平台>-<备注>（docs/双流账号调度设计-20261004.md §6.A 表）
RENAME_PLAN = {
    "cdeb04f7-60a3-4266-a9b6-5a6109fbcb58": "own:grok-主力",
    "0ced424e-9cf0-4004-a570-d8b16984cff4": "own:xai-主力",
    "736dda0b-80b8-4490-8410-2eb7e35d8d88": "own:glm-会员1",
    "d5d232db-7147-4806-84a8-4c8a5dbd1fb7": "own:glm-zcode测试",
    "87fb434b-c63b-4949-b8f6-2cef6df29f4c": "own:qoder-三方",
    "845886e6-3d67-47ea-af50-6d7a6bdf8f16": "own:tokenrouter-三方",
    "1c069e67-0196-4165-8ac3-6dad16abc840": "own:opencode-免费池",
}

# 与台账 self_owned_accounts 对账（conn_id 集合必须一致才动手）
def _ledger_conn_ids() -> set:
    import sqlite3
    from core.db import DEFAULT_DB_PATH

    conn = sqlite3.connect(str(DEFAULT_DB_PATH))
    try:
        return {str(r[0]) for r in conn.execute("SELECT conn_id FROM self_owned_accounts").fetchall()}
    finally:
        conn.close()


def _pool_max_pipeline_priority(conns: list, provider: str, own_ids: set) -> int:
    """该 provider 池内、非自有（产线号+未知来源都算，保守）的最大 priority。"""
    vals = [int(c.get("priority") or 0) for c in conns
            if c.get("provider") == provider and c.get("id") not in own_ids]
    return max(vals) if vals else 0


def _is_tail(conns: list, cid: str) -> bool:
    """own 连接是否已达标：priority 严格大于池内所有非自有产线号。

    注意不与其他 own: 连接比位次——两条自有号之间谁前谁后无所谓（都受保护），
    若要求互压会因 reorderInTx 的 1..N 连续重编号互相翻面、永不收敛。
    """
    me = next((c for c in conns if c.get("id") == cid), None)
    if not me:
        return False
    floor = _pool_max_pipeline_priority(conns, str(me.get("provider")), set(RENAME_PLAN))
    return int(me.get("priority") or 0) > floor


def run(dry_run: bool = False) -> dict:
    from core.nr_client import NRClient

    cli = NRClient()
    conns = cli.list_connections()
    by_id = {c.get("id"): c for c in conns}

    ledger = _ledger_conn_ids()
    missing_in_ledger = sorted(set(RENAME_PLAN) - ledger)
    missing_in_gw = sorted(cid for cid in RENAME_PLAN if cid not in by_id)

    report = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "gateway_conns": len(conns),
        "ledger_rows": len(ledger),
        "plan": RENAME_PLAN,
        "missing_in_ledger": missing_in_ledger,
        "missing_in_gateway": missing_in_gw,
        "applied": [],
        "skipped": [],
        "verify": [],
        "errors": [],
    }

    if missing_in_ledger or missing_in_gw:
        report["errors"].append("conn_id 与台账/网关不一致，中止（对账闸门）")
        return report

    for cid, new_name in RENAME_PLAN.items():
        cur = by_id[cid]
        old_name = str(cur.get("name") or "")
        cur_prio = cur.get("priority")
        name_ok = old_name == new_name
        tail_ok = _is_tail(conns, cid)
        if name_ok and tail_ok:
            report["skipped"].append({"id": cid, "name": old_name, "priority": cur_prio,
                                      "reason": "already-tagged(池尾)"})
            continue
        body = {}
        if not name_ok:
            body["name"] = new_name
        if not tail_ok:
            body["priority"] = RETAIL_PRIORITY
        if dry_run:
            report["applied"].append({"id": cid, "provider": cur.get("provider"),
                                      "old": {"name": old_name, "priority": cur_prio},
                                      "new": {"name": body.get("name", old_name),
                                              "priority": body.get("priority", cur_prio)},
                                      "put_body": body, "dry_run": True})
            continue
        try:
            resp, st = cli._request("PUT", f"/api/providers/{cid}", body)
            got = (resp.get("connection") or {}) if isinstance(resp, dict) else {}
            ok = st == 200 and (not body.get("name") or got.get("name") == new_name)
            report["applied"].append({"id": cid, "provider": cur.get("provider"),
                                      "old": {"name": old_name, "priority": cur_prio},
                                      "new": {"name": body.get("name", old_name),
                                              "priority": body.get("priority", cur_prio)},
                                      "http": st, "ok": ok})
            if not ok:
                report["errors"].append({"id": cid, "error": f"PUT http={st}"})
        except Exception as e:  # noqa: BLE001
            report["errors"].append({"id": cid, "error": f"{type(e).__name__}: {e}"[:160]})

    # 回读核对（GET /api/providers 全量，再逐条比对 name 前缀 + 池尾位次）
    if not dry_run and report["applied"]:
        try:
            conns = cli.list_connections()
            for cid, new_name in RENAME_PLAN.items():
                c = next((x for x in conns if x.get("id") == cid), {}) or {}
                report["verify"].append({
                    "id": cid, "provider": c.get("provider"), "isActive": c.get("isActive"),
                    "name": c.get("name"), "priority": c.get("priority"),
                    "name_ok": str(c.get("name") or "").startswith("own:"),
                    "tail_ok": _is_tail(conns, cid),
                })
        except Exception as e:  # noqa: BLE001
            report["errors"].append({"error": f"verify: {type(e).__name__}: {e}"[:160]})

    return report


def render(rep: dict) -> str:
    mode = "DRY-RUN" if rep.get("dry_run") else "APPLY"
    L = [f"=== tag_selfowned_panel {rep['ts']} [{mode}] "
         f"网关连接 {rep['gateway_conns']} / 台账自有 {rep['ledger_rows']} ==="]
    if rep["missing_in_ledger"] or rep["missing_in_gateway"]:
        L.append(f"!! 对账失败 ledger缺={rep['missing_in_ledger']} gw缺={rep['missing_in_gateway']}")
    for a in rep["applied"]:
        L.append(f"  PUT {a['id'][:8]} [{a.get('provider')}] "
                 f"{a['old'].get('name')!r}(p={a['old'].get('priority')}) → "
                 f"{a['new'].get('name')!r}(p={a['new'].get('priority')}) "
                 f"http={a.get('http')} ok={a.get('ok')}")
    for s in rep["skipped"]:
        L.append(f"  跳过 {s['id'][:8]} {s['name']!r}(p={s['priority']}) — {s['reason']}")
    if rep["verify"]:
        L.append("  回读核对（GET /api/providers）：")
        for v in rep["verify"]:
            mark = "✓" if (v["name_ok"] and v["tail_ok"]) else "✗"
            L.append(f"   {mark} {v['id'][:8]} [{v.get('provider')}] active={v.get('isActive')} "
                     f"name={v.get('name')!r} priority={v.get('priority')} tail_ok={v['tail_ok']}")
    for e in rep["errors"]:
        L.append(f"  !! {e}")
    return "\n".join(L)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    rep = run(dry_run="--dry-run" in sys.argv)
    print(render(rep))
    try:
        scratch = os.path.join(PROJECT_ROOT, "scratch")
        os.makedirs(scratch, exist_ok=True)
        with open(os.path.join(scratch, "tag_selfowned_panel_last.json"), "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
