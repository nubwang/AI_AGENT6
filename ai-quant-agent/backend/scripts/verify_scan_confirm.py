"""验收：采集完成后「弹窗询问是否运行每日推荐扫描」（用户诉求）

背景（用户原话）：
    "给数据采集完成之后不是自动运行每日推荐么？这样每次都这样很不方便，
     在采集跑完之后，弹窗显示是否跑每日推荐扫描，这样我可以方便控制"

正确实现必须满足 5 条硬约束（否则会退化回"擅自扫描"或"永远不扫"）：

  A. 默认模式是 confirm（不是 auto）：安全性优先——**不擅自跑**
  B. confirm 模式下"采集完成"只标记待确认，**绝不启动扫描线程**
     （若这里偷偷 start_scan_task，用户就又被抢了控制权）
  C. 用户点「是」(/scan/confirm) 才真启动；点「否/稍后」(/scan/dismiss) 只清标记
  D. off / auto 两种模式仍可用（off=纯手动；auto=旧行为，向后兼容）
  E. 两条采集路径（手动采集 data.py / 定时采集 main.py）都改走同一入口
     —— 只改一条会出现"手动不扫、定时照扫"的不一致

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_scan_confirm.py
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents import evolution_config as EC
from app.api import data as D
from app.api import predictions as P

PASS, FAIL = [], []
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _frontend(path: str) -> str:
    p = os.path.join(ROOT, "frontend", "src", path)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def _backend(path: str) -> str:
    p = os.path.join(BACKEND, path)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def _snapshot() -> dict:
    return {
        "scan_status": P._SCAN_STATE["status"],
        "scan_message": P._SCAN_STATE.get("message", ""),
        "confirm": dict(P._SCAN_CONFIRM),
        "start_scan_task": P.start_scan_task,
        "auto_scan_mode": P.auto_scan_mode,
    }


def _restore(snap: dict) -> None:
    P._SCAN_STATE["status"] = snap["scan_status"]
    P._SCAN_STATE["message"] = snap["scan_message"]
    P._SCAN_CONFIRM.clear()
    P._SCAN_CONFIRM.update(snap["confirm"])
    P.start_scan_task = snap["start_scan_task"]
    P.auto_scan_mode = snap["auto_scan_mode"]


def main() -> int:  # noqa: C901
    snap = _snapshot()
    try:
        print("=== A 默认模式 = confirm（安全优先：不擅自扫）===")
        ck("A1 auto_scan_mode() 存在", callable(getattr(P, "auto_scan_mode", None)))
        cfg = json.load(open(os.path.join(BACKEND, "data", "evolution_config.json"), encoding="utf-8"))
        entry = (cfg.get("params") or {}).get("auto_scan_after_collect") or {}
        ck("A2 参数已注册进进化大脑（可热改）", bool(entry), str(list(entry.keys())))
        ck("A3 参数默认值 = confirm", entry.get("default") == "confirm", str(entry.get("default")))
        ck("A4 参数可选值 = confirm/auto/off（受进化大脑约束，不会写出非法值）",
           sorted(entry.get("choices") or []) == ["auto", "confirm", "off"], str(entry.get("choices")))
        mode_now = P.auto_scan_mode()
        ck("A5 实际读到的模式是三者之一", mode_now in ("confirm", "auto", "off"), mode_now)

        # 非法值回退 confirm（防进化大脑写入脏值 → 变成"永扫"或"永不扫"）
        orig_get = EC.get_param

        def _bad(name, default=None):
            return "banana" if name == "auto_scan_after_collect" else orig_get(name, default)

        EC.get_param = _bad
        try:
            ck("A6 非法值回退 confirm（不崩、不乱扫）", P.auto_scan_mode() == "confirm", P.auto_scan_mode())
        finally:
            EC.get_param = orig_get

        def _upper(name, default=None):
            return "AUTO " if name == "auto_scan_after_collect" else orig_get(name, default)

        EC.get_param = _upper
        try:
            ck("A7 大小写/空格容错（'AUTO ' → auto）", P.auto_scan_mode() == "auto", P.auto_scan_mode())
        finally:
            EC.get_param = orig_get

        print("=== B confirm 模式：只标记，**不启动扫描**（最关键）===")
        P._SCAN_CONFIRM.update({"pending": False, "data_date": "", "last_action": "", "last_action_at": ""})
        P._SCAN_STATE["status"] = "idle"
        before_status = P._SCAN_STATE["status"]
        calls: list = []
        P.start_scan_task = lambda *a, **k: calls.append((a, k)) or {"status": "pending"}  # type: ignore[assignment]
        P.auto_scan_mode = lambda: "confirm"  # type: ignore[assignment]
        res = P.handle_after_collection(source="auto_data", data_date="20260918")
        ck("B1 返回 confirm_pending", res.get("status") == "confirm_pending", str(res))
        ck("B2 标记了待确认 + 数据日期", P._SCAN_CONFIRM["pending"] is True
           and P._SCAN_CONFIRM["data_date"] == "20260918", str(P._SCAN_CONFIRM.get("data_date")))
        ck("B3 **没启动扫描线程**", calls == [], f"start_scan_task 调用次数={len(calls)}")
        ck("B4 扫描状态保持原样（没被置 running/pending）",
           P._SCAN_STATE["status"] == before_status and P._SCAN_STATE["status"] not in ("running", "pending"),
           f"{before_status} → {P._SCAN_STATE['status']}")
        ck("B5 记下来源，便于排查是哪条链路触发的", P._SCAN_CONFIRM.get("source") == "auto_data")
        ck("B6 返回体带数据日期（前端弹窗要显示）", res.get("data_date") == "20260918")

        # 已在扫描中：不再弹窗（弹了也跑不了，只会让用户困惑）
        P._SCAN_STATE["status"] = "running"
        P._SCAN_CONFIRM.update({"pending": False, "data_date": ""})
        res = P.handle_after_collection(source="auto_data", data_date="20260918")
        ck("B7 已在扫描中 → 不弹窗（不置 pending）",
           res.get("status") == "running" and P._SCAN_CONFIRM["pending"] is False, str(res))
        P._SCAN_STATE["status"] = "idle"

        print("=== C off 模式：纯手动（不标记、不扫描）===")
        P._SCAN_CONFIRM.update({"pending": False, "data_date": ""})
        calls.clear()
        P.auto_scan_mode = lambda: "off"  # type: ignore[assignment]
        res = P.handle_after_collection(source="schedule", data_date="20260918")
        ck("C1 返回 off", res.get("status") == "off", str(res))
        ck("C2 不标记待确认", P._SCAN_CONFIRM["pending"] is False)
        ck("C3 不启动扫描", calls == [])

        print("=== D auto 模式：兼容旧行为（采集完成即扫）===")
        P._SCAN_CONFIRM.update({"pending": False, "data_date": ""})
        calls.clear()
        P.auto_scan_mode = lambda: "auto"  # type: ignore[assignment]
        res = P.handle_after_collection(source="schedule", data_date="20260918")
        ck("D1 直接启动扫描（沿用旧行为）", len(calls) == 1, f"调用次数={len(calls)}")
        ck("D2 透传来源，前端状态页能看出是定时触发",
           bool(calls) and calls[0][1].get("source") == "schedule", str(calls[:1]))
        ck("D3 auto 模式不产生「待确认」残留", P._SCAN_CONFIRM["pending"] is False)

        print("=== E 确认/跳过接口 ===")
        routes = [r for r in P.router.routes if str(r.path).endswith("/scan/confirm")]
        droutes = [r for r in P.router.routes if str(r.path).endswith("/scan/dismiss")]
        ck("E1 /scan/confirm 路由存在且是 POST", bool(routes)
           and "POST" in {m for r in routes for m in (r.methods or set())},
           str([str(r.path) for r in routes]))
        ck("E2 /scan/dismiss 路由存在且是 POST", bool(droutes)
           and "POST" in {m for r in droutes for m in (r.methods or set())},
           str([str(r.path) for r in droutes]))

        # 跳过：只清标记，不启动
        P._SCAN_CONFIRM.update({"pending": True, "data_date": "20260918", "source": "auto_data"})
        calls.clear()
        res = asyncio.run(P.dismiss_scan(reason="user_skip"))
        ck("E3 跳过 → 清掉待确认", res.get("status") == "dismissed" and P._SCAN_CONFIRM["pending"] is False, str(res))
        ck("E4 跳过 → 不启动扫描", calls == [])
        ck("E5 跳过 → 留下审计痕迹（last_action=dismissed）",
           P._SCAN_CONFIRM.get("last_action") == "dismissed", str(P._SCAN_CONFIRM.get("last_action")))

        # 确认：清标记 + 启动
        P._SCAN_CONFIRM.update({"pending": True, "data_date": "20260918", "source": "auto_data"})
        P._SCAN_STATE["status"] = "idle"
        calls.clear()
        res = asyncio.run(P.confirm_scan(max_stocks=None, force=False))
        ck("E6 确认 → 真启动扫描", len(calls) == 1, f"调用次数={len(calls)}")
        ck("E7 确认 → 返回 confirmed=True", res.get("confirmed") is True, str(res))
        ck("E8 确认 → 清掉待确认（前端提示条消失）", P._SCAN_CONFIRM["pending"] is False)
        ck("E9 确认 → 留下审计痕迹 + 来源标 confirm（区别于手动/定时）",
           P._SCAN_CONFIRM.get("last_action") == "confirmed"
           and bool(calls) and calls[0][1].get("source") == "confirm",
           str(calls[:1]))

        # 已在跑时点"确认"：不重复触发（防双跑 → 重复落盘/重复命中登记）
        P._SCAN_STATE["status"] = "running"
        calls.clear()
        res = asyncio.run(P.confirm_scan(max_stocks=None, force=False))
        ck("E10 已 running 时点确认 → 不重复启动（防双跑）", calls == [], f"调用次数={len(calls)}")
        ck("E11 此时也清掉待确认（避免提示条一直挂着）", P._SCAN_CONFIRM["pending"] is False, str(res))
        P._SCAN_STATE["status"] = "idle"

        print("=== F 状态接口暴露给前端 ===")
        st = asyncio.run(P.scan_status())
        ck("F1 scan/status 带 confirm_pending", "confirm_pending" in st, str(list(st.keys())))
        ck("F2 confirm_pending 是可读结构（pending/data_date/source）",
           isinstance(st.get("confirm_pending"), dict)
           and {"pending", "data_date", "source"} <= set(st["confirm_pending"]))
        ck("F3 scan/status 带 auto_scan_mode（前端可据此提示「为什么没弹窗」）",
           st.get("auto_scan_mode") in ("confirm", "auto", "off"), str(st.get("auto_scan_mode")))
        ck("F4 停止扫描信号仍在（本次改动未破坏上一功能）", "cancel_requested" in st)

        print("=== G 两条采集路径都改走同一入口（一致性）===")
        dsrc = inspect.getsource(D._auto_scan_after_collection)
        ck("G1 手动采集路径调用 handle_after_collection",
           "handle_after_collection" in dsrc and "source=\"auto_data\"" in dsrc)
        ck("G2 手动采集路径不再直接 start_scan_task（否则又变成擅自扫）",
           "start_scan_task(" not in dsrc)
        msrc = _backend("app/main.py")
        ck("G3 定时采集路径调用 handle_after_collection",
           "handle_after_collection" in msrc and "source=\"schedule\"" in msrc)
        ck("G4 定时采集路径传入最新数据日期（弹窗要显示）",
           "data_date=latest_s" in msrc or "data_date=" in msrc)

        print("=== H 前端接线 ===")
        api_ts = _frontend("api/index.ts")
        ck("H1 api 导出 confirmRecommendScan → /predictions/scan/confirm",
           "export const confirmRecommendScan" in api_ts and "/predictions/scan/confirm" in api_ts)
        ck("H2 api 导出 dismissRecommendScan → /predictions/scan/dismiss 且能带 reason",
           "export const dismissRecommendScan" in api_ts and "/predictions/scan/dismiss" in api_ts
           and "reason" in api_ts)

        dm = _frontend("views/DataManage.vue")
        ck("H3 DataManage 引入 ElMessageBox（用弹窗而非自动跑）", "ElMessageBox" in dm)
        ck("H4 DataManage 实现 maybeAskScan 并真的调用确认/跳过接口",
           "const maybeAskScan" in dm and "await confirmRecommendScan()" in dm
           and "await dismissRecommendScan(" in dm)
        ck("H5 采集结束（progress.status==='done'）即触发询问",
           "maybeAskScan()" in dm and 'data.status === "done"' in dm)
        ck("H6 有兜底轮询 + 卸载清理（定时采集/WS 掉线也能问到）",
           "scanCheckTimer = setInterval" in dm and "clearInterval(scanCheckTimer)" in dm)
        ck("H7 同一数据日期只弹一次（不骚扰）",
           "scanPromptedDate.value === dateKey" in dm and "scanAsking" in dm)

        rv = _frontend("views/Recommend.vue")
        ck("H8 Recommend 也有提示条 + 立即运行/稍后再说",
           "confirmPending?.pending" in rv and "立即运行" in rv and "稍后再说" in rv)
        ck("H9 Recommend 轮询同步待确认标记（跨页面一致）",
           "confirmPending.value = scan?.confirm_pending" in rv)
        ck("H10 Recommend 确认后进入轮询（用户能看到扫描进度）",
           "runConfirmedScan" in rv and "startScanPolling()" in rv)
    finally:
        _restore(snap)

    print(f"\n===== 结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过 =====")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
