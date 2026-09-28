"""迁移脚本：把 plans/23 的回测可信度/链路阈值注册为进化大脑可进化参数（幂等）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/add_plan23_params.py

背景：这些阈值原先散落在各模块的硬编码常量里（改一次要重启、没有影子验证）。
注册到 evolution_config 后，进化大脑可统一感知（L0 health 神经元）并统一调优（Tier1 热生效）。

幂等：已存在的参数**只补描述字段，不覆盖 current**（避免把进化大脑调过的值改回去）。
安全：写入前自动备份 evolution_config.json → .bak.<时间戳>。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
CONFIG = BACKEND / "data" / "evolution_config.json"

NEW_PARAMS: dict[str, dict] = {
    "bt_c_target_coverage": {
        "file": "app/backtest/runner.py", "default": 0.3, "current": 0.3,
        "min": 0.05, "max": 0.5, "step": 0.05, "tier": 1, "hot_reload": True,
        "shadow_eligible": True,
        "desc": "A/B 方案 C（采样条件表）选取覆盖率：按综合概率取前 N 比例，与线上『排序取 TOP』口径一致（plans/23 T2）",
    },
    "bt_min_prob_gap": {
        "file": "app/backtest/runner.py", "default": 0.02, "current": 0.02,
        "min": 0.0, "max": 0.2, "step": 0.005, "tier": 1, "hot_reload": True,
        "shadow_eligible": True,
        "desc": "A/B 有效性下限：C 组与其余样本平均概率差低于此值 → 判『概率无区分度』、A/B 无效（防假的『提升 0.0』）",
    },
    "auto_scan_after_collect": {
        "file": "app/api/predictions.py", "default": "confirm", "current": "confirm",
        "tier": 1, "hot_reload": True, "shadow_eligible": False,
        "choices": ["confirm", "auto", "off"],
        "desc": "采集完成后的每日推荐行为：confirm=弹窗询问（默认，用户控制）/ auto=旧行为直接自动扫 / off=不自动扫（纯手动）",
    },
    "bt_cond_min_edge": {
        "file": "app/backtest/threshold_analyzer.py", "default": 0.05, "current": 0.05,
        "min": 0.0, "max": 0.3, "step": 0.01, "tier": 1, "hot_reload": True,
        "shadow_eligible": True,
        "desc": "条件表规则准入：up_prob 必须比基线高出的幅度（低于此不算『高胜率条件』，防伪规则）",
    },
    "bt_cond_max_coverage": {
        "file": "app/backtest/threshold_analyzer.py", "default": 0.5, "current": 0.5,
        "min": 0.1, "max": 1.0, "step": 0.05, "tier": 1, "hot_reload": True,
        "shadow_eligible": True,
        "desc": "条件表规则覆盖率上限（超过即『宽规则』无筛选力，会让 A/B 的 C ≡ A）",
    },
    "bt_key_auc": {
        "file": "app/backtest/attribution.py", "default": 0.55, "current": 0.55,
        "min": 0.5, "max": 0.9, "step": 0.01, "tier": 1, "hot_reload": True,
        "shadow_eligible": True,
        "desc": "归因『关键特征』AUC 门槛（另需置换检验 p<0.05、方差非零），防常数列/噪音污染 LLM 精筛",
    },
    "bt_discrimination_min_gap": {
        "file": "app/backtest/metrics.py", "default": 0.08, "current": 0.08,
        "min": 0.02, "max": 0.5, "step": 0.01, "tier": 1, "hot_reload": True,
        "shadow_eligible": True,
        "desc": "正负样本区分度门槛：success 与 failure 的 T+5 正收益率差 ≥ 此值且 p<0.05 才算『标签有效』（plans/23 T3）",
    },
    "data_gate_strict": {
        "file": "app/backtest/runner.py", "default": True, "current": True,
        "min": 0, "max": 1, "step": 1, "tier": 1, "hot_reload": True,
        "shadow_eligible": False,
        "desc": "数据门禁：核心表（daily/adj_factor/stk_limit/daily_basic）日期不一致时是否中止回测（1=中止，0=仅告警）",
    },
    "collect_retry_max": {
        "file": "app/main.py", "default": 3, "current": 3,
        "min": 0, "max": 8, "step": 1, "tier": 1, "hot_reload": True,
        "shadow_eligible": False,
        "desc": "每日采集『数据未就绪』自动重试次数上限（配合重试间隔 + 21:00 兜底任务）",
    },
    "collect_retry_delay_min": {
        "file": "app/main.py", "default": 30, "current": 30,
        "min": 5, "max": 90, "step": 5, "tier": 1, "hot_reload": True,
        "shadow_eligible": False,
        "desc": "每日采集就绪重试间隔（分钟）：Tushare 日频通常 17:00 后就绪，未就绪则按此间隔重试",
    },
    "bt_control_sample_n": {
        "file": "app/backtest/control_sampler.py", "default": 400, "current": 400,
        "min": 100, "max": 1500, "step": 100, "tier": 1, "hot_reload": True,
        "shadow_eligible": True,
        "desc": "T4 第二步同口径对照组：每个交易日抽样候选数（抽完再过滤『有启动迹象』者），越大对照越稳但越慢",
    },
    "bt_control_warmup_pct": {
        "file": "app/backtest/control_sampler.py", "default": 0.15, "current": 0.15,
        "min": 0.0, "max": 0.5, "step": 0.01, "tier": 1, "hot_reload": True,
        "shadow_eligible": True,
        "desc": "『有启动迹象』判定阈值：t0 前 20 日累计涨幅超过此值即从对照组剔除（保证对照组确实未启动）",
    },
    "data_gate_max_lag_td": {
        "file": "app/backtest/runner.py", "default": 0, "current": 0,
        "min": 0, "max": 5, "step": 1, "tier": 1, "hot_reload": True,
        "shadow_eligible": False,
        "desc": "数据门禁：锚定表（daily/adj_factor/daily_basic）允许落后『最近交易日』多少个**交易日**（0=必须是最新交易日；周末/节假日不计入）",
    },
    "data_gate_allow_ahead_td": {
        "file": "app/backtest/runner.py", "default": 1, "current": 1,
        "min": 0, "max": 3, "step": 1, "tier": 1, "hot_reload": True,
        "shadow_eligible": False,
        "desc": "数据门禁：stk_limit 允许领先锚定表多少个交易日（涨跌停价按次日披露 → 默认 1）",
    },
    "extra_mirror_enabled": {
        "file": "app/backtest/mirror_cache.py", "default": 1, "current": 1,
        "min": 0, "max": 1, "step": 1, "tier": 1, "hot_reload": True,
        "shadow_eligible": False,
        "desc": "回测提速开关：逐股查大表（daily_basic/moneyflow）走本地 SQLite 镜像（1=开，0=回退 MySQL 冷读）；实测单股 ~4s → ~20ms",
    },
    "verify_evidence_enabled": {
        "file": "app/agents/failure_context.py", "default": 1, "current": 1,
        "min": 0, "max": 1, "step": 1, "tier": 1, "hot_reload": True,
        "shadow_eligible": False,
        "desc": "失败归因是否附带**外部证据包**（大盘指数/个股日线/个股资料）——像回测一样去历史数据里找原因（1=开，0=关）",
    },
    "verify_evidence_days": {
        "file": "app/agents/failure_context.py", "default": 5, "current": 5,
        "min": 1, "max": 20, "step": 1, "tier": 1, "hot_reload": True,
        "shadow_eligible": True,
        "desc": "失败归因证据包的复盘窗口（交易日）：推荐后个股/大盘走多少天纳入证据（窗口未走完会在证据里显式标注，不假装完整）",
    },
    # ── 洗盘识别（plans/23 §3.2 / P1 第 5 项）────────────────────
    "washout_score_threshold": {
        "file": "app/backtest/washout_detector.py", "default": 0.60, "current": 0.60,
        "min": 0.40, "max": 0.90, "step": 0.01, "tier": 1, "hot_reload": True,
        "shadow_eligible": True,
        "desc": "洗盘判定分阈值（九维加权分 ≥ 此值判为洗盘；越高越保守，需与命中率+样本量一起权衡）",
    },
    "washout_callback_threshold": {
        "file": "app/backtest/washout_detector.py", "default": 0.35, "current": 0.35,
        "min": 0.10, "max": 0.60, "step": 0.01, "tier": 1, "hot_reload": True,
        "shadow_eligible": True,
        "desc": "最低回调分阈值（≥ 此值算『回调中/洗盘中』，低于则判无洗盘特征）",
    },
    "washout_vol_shrink_best": {
        "file": "app/backtest/washout_detector.py", "default": 0.50, "current": 0.50,
        "min": 0.20, "max": 0.90, "step": 0.01, "tier": 1, "hot_reload": True,
        "shadow_eligible": False,
        "desc": "量能收缩满分线：回调期均量 / 起涨前均量 ≤ 此值给满分（越小越严格）",
    },
    "washout_vol_shrink_worst": {
        "file": "app/backtest/washout_detector.py", "default": 1.30, "current": 1.30,
        "min": 0.80, "max": 2.50, "step": 0.05, "tier": 1, "hot_reload": True,
        "shadow_eligible": False,
        "desc": "量能零分线：回调期量比 ≥ 此值判『放量』= 出货特征（0 分 + 可能触发硬否决）",
    },
    "washout_retrace_good": {
        "file": "app/backtest/washout_detector.py", "default": 0.34, "current": 0.34,
        "min": 0.10, "max": 0.60, "step": 0.01, "tier": 1, "hot_reload": True,
        "shadow_eligible": False,
        "desc": "回撤满分线：回撤 ≤ 前一波涨幅的此比例给满分（规划建议 1/3）",
    },
    "washout_retrace_bad": {
        "file": "app/backtest/washout_detector.py", "default": 0.67, "current": 0.67,
        "min": 0.40, "max": 1.00, "step": 0.01, "tier": 1, "hot_reload": True,
        "shadow_eligible": True,
        "desc": "回撤红线：回撤 > 此比例（2/3）判结构已破 → 硬否决为『疑似出货』",
    },
    "washout_end_vol_ratio": {
        "file": "app/backtest/washout_detector.py", "default": 0.60, "current": 0.60,
        "min": 0.20, "max": 1.00, "step": 0.01, "tier": 1, "hot_reload": True,
        "shadow_eligible": True,
        "desc": "washout_end 确认日『地量』口径：当日量 ≤ 回调期均量 × 此值 + 收阳 + 站上 MA5",
    },
    "rule_fdr_mode": {
        "file": "app/backtest/rule_verifier.py", "default": "observe", "current": "observe",
        "tier": 1, "hot_reload": True,
        "shadow_eligible": False,
        "desc": "规则验证的多重检验校正模式：off / observe（默认，仅记录 q 值不改 verified 集合）/ enforce（真正降级未过 FDR 的规则）——默认 observe 是为了避免静默削弱每日推荐规则信号（verified_count<3 会触发回退）（plans/23 §4.3）",
    },
    "rule_fdr_alpha": {
        "file": "app/backtest/rule_verifier.py", "default": 0.05, "current": 0.05,
        "min": 0.0, "max": 0.2, "step": 0.01, "tier": 1, "hot_reload": True,
        "shadow_eligible": False,
        "desc": "规则验证的 FDR 目标（Benjamini-Hochberg q 阈值；≤0 视为关闭校正）（plans/23 §4.3）",
    },
    "plan_tp_basis": {
        "file": "app/backtest/target_price.py", "default": "legacy", "current": "legacy",
        "tier": 1, "hot_reload": True,
        "shadow_eligible": False,
        "desc": "止盈目标位口径：legacy=沿用 entry_guidance 的成功组均值（§15.4 实测命中率仅 9.1%）/ atr=按波动率 ATR 倍数的可达性口径（默认 legacy，验证通过再切）（plans/23 §15.4）",
    },
    "plan_tp_atr_mult": {
        "file": "app/backtest/target_price.py", "default": 1.5, "current": 1.5,
        "min": 0.5, "max": 4.0, "step": 0.25, "tier": 1, "hot_reload": True,
        "shadow_eligible": True,
        "desc": "止盈目标位（atr 口径）：tp = T0×（1 + 倍数 × ATR%）。倍数越大目标越远、命中率越低（plans/23 §15.4）",
    },
}


def main() -> int:
    if not CONFIG.exists():
        print(f"❌ 配置不存在: {CONFIG}")
        return 1

    with open(CONFIG, encoding="utf-8") as f:
        data = json.load(f)

    params = data.setdefault("params", {})
    added, kept = [], []
    for name, meta in NEW_PARAMS.items():
        if name in params:
            # 幂等：保留既有 current（可能已被进化大脑调过），只补缺失的元数据字段
            for k, v in meta.items():
                if k not in params[name] and k != "current":
                    params[name][k] = v
            kept.append(name)
        else:
            params[name] = dict(meta)
            added.append(name)

    if not added:
        print(f"ℹ️ 无新增（已全部存在 {len(kept)} 个）：{kept}")
        return 0

    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = CONFIG.with_suffix(f".json.bak.{ts}")
    shutil.copy2(CONFIG, bak)

    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # 写后自检（防止"以为写成功其实没落盘"）
    with open(CONFIG, encoding="utf-8") as f:
        check = json.load(f)
    missing = [n for n in NEW_PARAMS if n not in check.get("params", {})]
    print(f"备份: {bak.name}")
    print(f"新增 {len(added)} 个: {added}")
    if kept:
        print(f"已存在 {len(kept)} 个（保留其 current）: {kept}")
    print(f"写后自检: params 总数={len(check.get('params', {}))}, 缺失={missing}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
