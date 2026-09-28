"""把自证测试（plans/25 §8.2）的可进化参数注入 evolution_config.json

与 `scripts/add_plan23_params.py` 同法：**幂等**（已存在则保持 current 不动、只补缺失字段），
JSON 结构损坏直接报错退出（不覆盖用户数据）。

跑法：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/add_selfproof_params.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(BACKEND, "data", "evolution_config.json")

# 结构：name -> (default, min, max, step, tier, hot_reload, shadow_eligible, desc)
PARAMS: dict[str, tuple] = {
    "selfproof_default_step": (1, 1, 20, 1, 1, True, True,
                               "自证回放默认步长（交易日）：1=逐日，N=每 N 日取一个 as_of 点"),
    "selfproof_universe_sample": (0, 0, 5000, 50, 1, True, True,
                                  "回放 universe 抽样只数（0=全市场；>0 随机抽样，主要省钱包）"),
    "selfproof_deep_top_n": (50, 10, 200, 10, 1, True, True,
                             "回放时 L2 逐只精筛只数（成本次旋钮）"),
    "selfproof_max_days": (250, 1, 2000, 10, 1, True, True,
                           "单次自证任务最大回放交易日数"),
    "selfproof_budget_default_cny": (5.0, 0.5, 1000.0, 0.5, 1, True, True,
                                     "自证任务默认预算上限（元）；默认对齐账户余额量级"),
    "selfproof_workers": (1, 1, 16, 1, 1, True, True,
                          "【实验性】回放并行度（按日并行；**默认 1=串行**）。"
                          "实测并行尚未达标：多进程 6 天规模 0.91×（根因=ChromaDB/sqlite 并读争锁），"
                          "线程池更糟（慢一个数量级，GIL 互踩）；LLM 档一律强制串行。"
                          "未达标前不许默认开启，见 plans/25 §15.4"),
    "selfproof_regime_topk": (5, 1, 20, 1, 1, True, True,
                              "大盘相似波段返回 Top-K 条数（K 固定，禁人工挑选）"),
    "selfproof_similarity_metric": ("zscore_euclid_corr", None, None, None, 1, True, True,
                                    "相似波段相似度口径（zscore_euclid_corr / dtw）"),
    "regime_sim_window": (60, 20, 250, 5, 1, True, True,
                          "相似波段窗口长度（交易日）"),
    "selfproof_tavily_enabled": (False, None, None, None, 1, True, False,
                                 "回放是否允许 Tavily 搜索：**必须 False**（实时搜索会穿越，见 plans/25 G5）"),
    # ── 护栏参数：tier=0 表示"不可由进化大脑自动改"，shadow_eligible=False ──
    "selfproof_is_ratio": (0.8, 0.5, 0.95, 0.05, 0, False, False,
                           "【护栏】自证集占比；IS/OOS 切分比例，改动需人工确认（plans/25 F4）"),
    "selfproof_oos_mode": ("year_kfold", None, None, None, 0, False, False,
                           "【护栏】OOS 切分模式：year_kfold（年份滚动留出，默认）/ time_tail（旧口径）"),
    "selfproof_oos_years": ([], None, None, None, 0, False, False,
                            "【护栏】冻结的留出年份集合，如 [2019, 2022]；空=由 year_kfold 轮换"),
}


def main() -> int:
    if not os.path.exists(CFG):
        print(f"❌ 找不到 {CFG}")
        return 1
    try:
        with open(CFG, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 配置解析失败，拒绝写入（避免覆盖用户数据）: {exc}")
        return 1

    bak = f"{CFG}.bak.{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(CFG, bak)

    params = data.setdefault("params", {})
    added, skipped = [], []
    for name, (dflt, lo, hi, step, tier, hot, shadow, desc) in PARAMS.items():
        if name in params:
            skipped.append(name)
            continue
        row = {
            "file": "app/backtest/selfproof_policy.py",
            "anchor_old": f'"{name}"',
            "default": dflt,
            "current": dflt,
            "tier": tier,
            "hot_reload": hot,
            "shadow_eligible": shadow,
            "desc": desc,
        }
        if lo is not None:
            row["min"] = lo
        if hi is not None:
            row["max"] = hi
        if step is not None:
            row["step"] = step
        params[name] = row
        added.append(name)

    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"备份: {bak}")
    print(f"新增参数 {len(added)}: {', '.join(added) if added else '（无）'}")
    print(f"已存在跳过 {len(skipped)}: {', '.join(skipped) if skipped else '（无）'}")
    print(f"params 总数: {len(params)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
