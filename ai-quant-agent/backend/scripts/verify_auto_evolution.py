"""自证：自动生效 + 午间自进化（用户决策：不要人工重启；每天中午 DeepSeek 闲时自行进化）

需求原文："进化大脑要自己重启项目，自动生效，不要让我重启再生效，进化大脑每天中午
deepseek 闲时自动自行。"

本脚本做**零副作用**验证（绝不真的触发重启；重启判定用纯函数真值表覆盖）：

  A. 交易窗口口径与 scripts/restart_backend.sh **一致**（避免两套窗口判断打架）
  B. DeepSeek 空闲时段口径（午间 12:00-14:00 = off_peak，半价）
  C. 重启守护纯决策真值表（有改动 + 非窗口 + 未暂停 + 非防抖 → 必须重启）
  D. 配置与调度已就绪（noon_auto_evolve / auto_restart / require_off_peak；两个 job 已注册）
  E. 真实现状可见（pending / 窗口 / 时段 / 判定原因），便于人工核对

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_auto_evolution.py
"""
from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents import evolution_config, self_improver  # noqa: E402

PASS, FAIL = [], []
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def ts_at(y: int, m: int, d: int, hh: int, mm: int) -> float:
    """构造某北京时刻的时间戳（本机时区即 Asia/Shanghai）。"""
    return time.mktime((y, m, d, hh, mm, 0, 0, 0, -1))


WED = (2026, 9, 23)      # 周三
SAT = (2026, 9, 26)      # 周六

print("\n[A] 交易窗口口径与重启脚本一致")
sh = os.path.join(PROJECT, "..", "scripts", "restart_backend.sh")
sh_txt = ""
try:
    with open(sh, "r", encoding="utf-8") as f:
        sh_txt = f.read()
except Exception as exc:  # noqa: BLE001
    print(f"  （读不到 restart_backend.sh: {exc}）")
for token in ("0915", "1130", "1300", "1500"):
    check(f"脚本避让窗口含 {token}", token in sh_txt)
check("窗口常量与脚本一致（09:15-11:30 / 13:00-15:00）",
      [list(w) for w in self_improver.TRADE_WINDOWS] == [["09:15", "11:30"], ["13:00", "15:00"]],
      str(self_improver.TRADE_WINDOWS))
check("工作日 10:00 → 在交易窗口", self_improver.in_trade_window(ts_at(*WED, 10, 0)) is True)
check("工作日 14:00 → 在交易窗口", self_improver.in_trade_window(ts_at(*WED, 14, 0)) is True)
check("工作日 12:30 → 非交易窗口（午间可重启）",
      self_improver.in_trade_window(ts_at(*WED, 12, 30)) is False)
check("工作日 16:00 → 非交易窗口", self_improver.in_trade_window(ts_at(*WED, 16, 0)) is False)
check("周六 10:00 → 非交易窗口", self_improver.in_trade_window(ts_at(*SAT, 10, 0)) is False)

print("\n[B] DeepSeek 空闲时段（中午即半价）")
check("工作日 12:30 → off_peak", self_improver.llm_period(ts_at(*WED, 12, 30)) == "off_peak")
check("工作日 13:30 → off_peak", self_improver.llm_period(ts_at(*WED, 13, 30)) == "off_peak")
check("工作日 10:00 → peak", self_improver.llm_period(ts_at(*WED, 10, 0)) == "peak")
check("工作日 19:00 → off_peak", self_improver.llm_period(ts_at(*WED, 19, 0)) == "off_peak")
check("周末全天 → off_peak", self_improver.llm_period(ts_at(*SAT, 10, 0)) == "off_peak")

print("\n[C] 重启守护纯决策真值表（无副作用）")
cases = [
    (True, False, False, False, True, "有改动+非窗口 → 自动重启"),
    (True, True, False, False, False, "交易窗口内 → 不重启（等窗口后）"),
    (True, False, True, False, False, "进化暂停 → 不重启"),
    (True, False, False, True, False, "防抖中 → 不重启"),
    (False, False, False, False, False, "无待重启改动 → 不重启"),
]
for pend, trade, paused, cool, expect, desc in cases:
    got, reason = self_improver.decide_restart(pend, trade, paused, cool)
    check(f"{desc}", got is expect, reason)

print("\n[D] 配置与调度就绪")
ce = evolution_config.get("code_evolution", {}) or {}
check("午间自动进化已开启", bool(ce.get("noon_auto_evolve", False)),
      f"noon={ce.get('noon_hour')}:{ce.get('noon_minute')}")
check("仅空闲时段执行（省 50%）", bool(ce.get("require_off_peak", False)))
check("自动重启已开启", bool(ce.get("auto_restart", False)))
nh, nm = int(ce.get("noon_hour", 12)), int(ce.get("noon_minute", 5))
check("午间执行时刻落在 off_peak 内（12:00-14:00）",
      12 <= nh < 14 and 0 <= nm < 60,
      f"{nh:02d}:{nm:02d} → {self_improver.llm_period(ts_at(*WED, nh, nm))}")
main_txt = ""
try:
    with open(os.path.join(PROJECT, "app", "main.py"), "r", encoding="utf-8") as f:
        main_txt = f.read()
except Exception as exc:  # noqa: BLE001
    print(f"  （读不到 main.py: {exc}）")
check("已注册午间进化 job", "code_evolution_noon" in main_txt)
check("已注册重启守护 job", '"restart_guard"' in main_txt or "'restart_guard'" in main_txt)
check("午间 job 为工作日触发", re.search(r"day_of_week=\"mon-fri\", hour=12", main_txt) is not None)
check("守护每 10 分钟检查", 'minute="*/10"' in main_txt)

print("\n[D2] 全部任务落在用户在线窗口（19:00 关机前；进化类集中在午间）")
_jobs = []
for _blk in re.split(r"scheduler\.add_job\(", main_txt)[1:]:
    _b = _blk[:700]
    _id = re.search(r'id="([^"]+)"', _b)
    _h = re.search(r"hour=(\d+)", _b)
    _jobs.append({"id": _id.group(1) if _id else "?", "hour": int(_h.group(1)) if _h else None})
_night = [j for j in _jobs if (j["hour"] or 0) >= 19]
print("  任务时间表：" + "、".join(f"{j['id']}@{j['hour']}" for j in _jobs if j["hour"] is not None))
# 允许的例外：数据采集（依赖 Tushare 当日数据到达，且自带重试）
_allow_night = {"daily_data_collection", "daily_data_collection_night",
                "backtest_monthly_monitor"}
_bad_night = [j for j in _night if j["id"] not in _allow_night]
check("无进化/学习类任务排在 19:00 后（用户已关机）",
      not _bad_night, "违规：" + "、".join(f"{j['id']}@{j['hour']}" for j in _bad_night) or "无")
_evo_ids = ("evolution", "code_evolution", "bugfix", "kb_maintain", "daily_learning",
            "restart_guard")
_evo_noon = [j for j in _jobs if j["hour"] is not None
             and any(k in j["id"] for k in _evo_ids)]
_bad_noon = [j for j in _evo_noon if not (12 <= j["hour"] < 14 or j["hour"] <= 18)]
check("进化类任务集中在午间 12:00-14:00（或下班前 ≤18:00）",
      not _bad_noon, "违规：" + "、".join(f"{j['id']}@{j['hour']}" for j in _bad_noon) or "无")
check("午间任务数量 ≥ 5（学习/进化/修复/维护/清理）", len(_evo_noon) >= 5, f"n={len(_evo_noon)}")

print("\n[E] 当前真实状态（人工核对用）")
d = self_improver.auto_restart_decision()
print(f"  pending_restart = {bool(d.get('pending'))}")
print(f"  in_trade_window = {d.get('in_trade_window')}")
print(f"  llm_period      = {d.get('llm_period')}")
print(f"  should_restart  = {d.get('should_restart')}（{d.get('reason')}）")

print(f"\n{'=' * 62}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + ", ".join(FAIL))
    sys.exit(1)
print("全部通过 ✅ —— 改动会自己重启生效，每天中午 DeepSeek 空闲时段自行进化")
