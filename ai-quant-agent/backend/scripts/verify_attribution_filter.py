"""验证：归因关键特征准入标准（plans/23 §2.4 T6）

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_attribution_filter.py

背景（T6 实测症状）：
    attribution 排行前列把 `st_risk_flag`（两组均值都 0、info_gain=0）、
    `mainbz_concentration`（两组均值都 1.0）、`event_dividend`（failure 0.676 > success 0.649）
    判为 is_key=true，AUC 仅 0.63~0.66（≈随机）。这份结果会被 load_attribution_kb()
    当作"历史成功规律"喂给 LLM 精筛 → 等于给 LLM 灌噪音。

覆盖：
  A1. 常数列（值全 0 / 全 1）→ 被剔除，原因=方差≈0
  A2. 复现旧症状：st_risk_flag 形态（0/NaN 混合，两组均值都 0）→ 被剔除
  A3. 两组均值相同的特征 → 被剔除
  A4. 纯随机特征 → 不判 is_key（置换检验拦住）
  A5. 真有判别力的特征 → is_key=true 且 p<0.05
  A6. 有效样本不足 → 被剔除
  A7. 关键特征数量上限生效
  A8. 过滤统计与剔除原因已落盘（可审计）
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.backtest.attribution import (  # noqa: E402
    analyze_attribution, MAX_KEY_FEATURES, MIN_VALID_SAMPLES, PERM_ALPHA,
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


rng = np.random.default_rng(2026)
N = 300
labels = np.concatenate([np.ones(N // 2), np.zeros(N // 2)])   # 150 成功 / 150 失败

feat = pd.DataFrame({
    "const_zero": np.zeros(N),                                  # 常数 0
    "const_one": np.ones(N),                                    # 常数 1
    "st_risk_flag": np.where(rng.random(N) < 0.6, 0.0, np.nan), # 0/NaN 混合（复现真实症状）
    "same_mean": np.where(labels == 1, 0.5, 0.5),               # 两组均值相同
    "random": rng.normal(0, 1, N),                              # 纯噪音
    "real_signal": np.where(labels == 1, 1.0, 0.0) + rng.normal(0, 0.5, N),  # 真有判别力
    "too_short": np.where(np.arange(N) < 10, 1.0, np.nan),      # 有效样本不足
})

res = analyze_attribution(feat, labels)
by_name = {r.feature: r for r in res.rankings}
discarded = {d["feature"]: d["reason"] for d in res.discarded}

print("剔除清单：")
for f, why in discarded.items():
    print(f"    {f:15s} → {why}")
print("保留特征：")
for r in res.rankings:
    print(f"    {r.feature:15s} auc={r.auc:.3f} p={r.p_value} is_key={r.is_key} "
          f"n={r.valid_samples}")

# A1 常数列
check("A1 const_zero 被剔除（方差≈0）", "const_zero" in discarded and "方差" in discarded["const_zero"])
check("A1 const_one 被剔除（方差≈0）", "const_one" in discarded and "方差" in discarded["const_one"])

# A2 st_risk_flag 形态
check("A2 st_risk_flag（0/NaN，两组均值都 0）被剔除", "st_risk_flag" in discarded,
      discarded.get("st_risk_flag", "（仍被保留！）"))

# A3 两组均值相同
check("A3 same_mean 被剔除（两组均值相同）", "same_mean" in discarded,
      discarded.get("same_mean", "（仍被保留！）"))

# A4 随机特征不判 key
rand_key = by_name.get("random").is_key if "random" in by_name else False
check("A4 random 不判 is_key（置换检验拦住）", not rand_key,
      f"p={by_name.get('random').p_value if 'random' in by_name else None}")

# A5 真信号判 key
sig = by_name.get("real_signal")
check("A5 real_signal 判 is_key=true", bool(sig and sig.is_key),
      f"auc={sig.auc if sig else None} p={sig.p_value if sig else None}")
check("A5 real_signal p < 0.05", bool(sig and sig.p_value is not None and sig.p_value < PERM_ALPHA),
      f"p={sig.p_value if sig else None}")

# A6 样本不足
check("A6 too_short 被剔除（有效样本不足）",
      "too_short" in discarded and "有效样本" in discarded["too_short"],
      discarded.get("too_short", "（仍被保留！）"))

# A7 key 数量上限
key_n = sum(1 for r in res.rankings if r.is_key)
check("A7 关键特征数 ≤ 上限", key_n <= MAX_KEY_FEATURES, f"key={key_n}, max={MAX_KEY_FEATURES}")

# A8 审计信息
fs = res.filter_stats
check("A8 filter_stats 齐备", bool(fs and "criteria" in fs and fs["total_features"] == len(feat.columns)),
      str(fs.get("total_features") if fs else None))
check("A8 剔除原因可追溯", len(res.discarded) >= 4 and all("reason" in d for d in res.discarded),
      f"剔除 {len(res.discarded)} 个")
check("A8 to_dict 暴露 discarded/filter_stats",
      "discarded" in res.to_dict() and "filter_stats" in res.to_dict())
check("A8 常量被过滤后不再进入成功/失败画像",
      not ({"const_zero", "const_one"} & set(res.success_profile.keys())),
      f"profile keys={list(res.success_profile.keys())}")

print(f"\n{'=' * 60}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：" + ", ".join(FAIL))
    sys.exit(1)
print("全部通过 ✅")
