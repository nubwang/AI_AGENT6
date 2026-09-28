"""验收：每日推荐「停止扫描」（前后端协作式取消）

要求（用户诉求）：前端能停止扫描。
正确实现必须满足 4 条硬约束，否则"停止"会制造更脏的数据：

  A. daily_scan 支持 should_stop：逐股循环在下一次迭代前退出
  B. **停止时不落盘、不登记命中**（半成品榜单若落盘，会被次日自我验证/命中登记当成正式推荐）
  C. 状态可区分：cancelled ≠ failed ≠ success（前端要能显示"已停止"）
  D. Agent 精筛阶段同样能被停止（扫描最后一环，耗时最长）

另检查接线：POST /predictions/scan/stop 路由存在、scan/status 暴露 cancel_requested、
前端 api 有 stopRecommendScan、Recommend.vue 有按钮与 cancelled 处理。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_scan_stop.py
"""
from __future__ import annotations

import inspect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest import daily_scan as DS
from app.api import predictions as P
from app.agents import run_agent_refine
from app.agents.refine_agent import RefineAgent

PASS, FAIL = [], []


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _frontend(path: str) -> str:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    p = os.path.join(root, "frontend", "src", path)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def main() -> int:
    print("=== A 后端支持 should_stop ===")
    ck("A1 run_daily_scan 接受 should_stop",
       "should_stop" in inspect.signature(DS.run_daily_scan).parameters)
    ck("A2 _run_daily_scan_impl 接受 should_stop",
       "should_stop" in inspect.signature(DS._run_daily_scan_impl).parameters)
    ck("A3 逐股循环里有取消检查", "should_stop is not None and should_stop()" in inspect.getsource(DS._run_daily_scan_impl))

    print("=== B 停止 → 不落盘、不登记命中（实测真跑一次）===")
    files_before = sorted(os.listdir(DS.REPORT_DIR)) if os.path.isdir(DS.REPORT_DIR) else []
    try:
        rep = DS.run_daily_scan(max_stocks=20, should_stop=lambda: True)
    except Exception as exc:  # noqa: BLE001
        ck("B1 立即停止的扫描不抛异常", False, str(exc))
        rep = {}
    ck("B1 立即停止的扫描不抛异常", bool(rep))
    ck("B2 返回 cancelled=True", bool(rep.get("cancelled")))
    ck("B3 停止时不产出任何榜单条目", rep.get("top_picks") == [])
    ck("B4 停止时有可读的中断说明", bool(rep.get("message")))
    files_after = sorted(os.listdir(DS.REPORT_DIR)) if os.path.isdir(DS.REPORT_DIR) else []
    ck("B5 停止时**没有新增落盘文件**", files_before == files_after,
       f"before={len(files_before)} after={len(files_after)}")
    ck("B6 停止时扫描计数为 0（第一只前就退出）", rep.get("total_scanned") == 0,
       f"total_scanned={rep.get('total_scanned')}")

    print("=== C 状态语义（cancelled 可区分）===")
    src = inspect.getsource(P._scan_worker)
    ck("C1 worker 识别 report.cancelled", 'report.get("cancelled")' in src)
    ck("C2 worker 置状态 cancelled（不是 failed/success）", '"cancelled"' in src and '_SCAN_STATE["status"] = "cancelled"' in src)
    ck("C3 停止时不进入 Agent 精筛（提前 return）",
       re.search(r'status"\] = "cancelled"[\s\S]{0,400}?return', src) is not None)
    ck("C4 停止时 report 置空（不把半成品当榜单）", '_SCAN_STATE["report"] = None' in src)
    ck("C5 扫描取消信号存在且每次启动前清空",
       isinstance(P._SCAN_CANCEL, type(__import__("threading").Event())) and "_SCAN_CANCEL.clear()" in inspect.getsource(P.start_scan_task))

    print("=== D 停止接口与状态暴露 ===")
    routes = {r.path for r in P.router.routes}
    stop_routes = [r for r in P.router.routes if str(r.path).endswith("/scan/stop")]
    ck("D1 /scan/stop 路由存在", bool(stop_routes), str(sorted(routes)))
    ck("D1b /scan/stop 是 POST（写操作，不该是 GET）",
       bool(stop_routes) and "POST" in {m for r in stop_routes for m in (r.methods or set())},
       str({m for r in stop_routes for m in (r.methods or set())}))
    stop_src = inspect.getsource(P.stop_scan)
    ck("D2 停止接口会置位取消信号", "_SCAN_CANCEL.set()" in stop_src)
    ck("D3 停止接口对'没在跑'有明确反馈", "没有正在运行的" in stop_src)
    ck("D4 停止接口也覆盖手动 Agent 精筛", "agent_running" in stop_src)
    st_src = inspect.getsource(P.scan_status)
    ck("D5 scan/status 暴露 cancel_requested", "cancel_requested" in st_src)
    ck("D6 worker 把取消信号传给 daily_scan", "should_stop=_SCAN_CANCEL.is_set" in src)

    print("=== E Agent 精筛阶段同样可停 ===")
    ck("E1 run_agent_refine 接受 should_stop",
       "should_stop" in inspect.signature(run_agent_refine).parameters)
    ck("E2 RefineAgent.batch_refine 接受 should_stop",
       "should_stop" in inspect.signature(RefineAgent.batch_refine).parameters)
    ck("E3 batch_refine 每批前检查", "should_stop is not None and should_stop()" in inspect.getsource(RefineAgent.batch_refine))
    res = run_agent_refine([{"ts_code": "000001.SZ", "up_probability": 0.5}], should_stop=lambda: True)
    ck("E4 立即停止的精筛返回 cancelled（且不发 LLM 请求）", bool(res.get("cancelled")), str(res.get("message")))
    ck("E5 精筛 cancelled 时不返回榜单（由调用方决定不落盘）", res.get("top_picks") == [])
    aw = inspect.getsource(P._agent_worker)
    ck("E6 手动精筛 worker 也识别 cancelled 且不落盘",
       'result.get("cancelled")' in aw and aw.count('_save_agent_report') == 1)

    print("=== F 前端接线 ===")
    api_ts = _frontend("api/index.ts")
    ck("F1 api 导出 stopRecommendScan", "export const stopRecommendScan" in api_ts
       and "/predictions/scan/stop" in api_ts)
    vue = _frontend("views/Recommend.vue")
    ck("F2 Recommend.vue 导入并调用停止接口",
       "stopRecommendScan" in vue and "await stopRecommendScan()" in vue)
    ck("F3 存在停止按钮", "停止扫描" in vue and 'v-if="scanning"' in vue)
    ck("F4 轮询把 cancelled 视为结束", 'scan?.status === "cancelled"' in vue)
    ck("F5 界面能显示「已停止」", "已停止" in vue)

    print(f"\n===== 结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过 =====")
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
