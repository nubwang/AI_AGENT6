"""证据接入 CLI：把各模块的验证产出写进进化大脑（"跑完即入脑"）

## 用途

新模块（plans/23 回测增强 / 24 人心博弈 / 25 自证测试 …）跑完后，其结论原本只落在
`data/*.json` 里，进化大脑看不见。本脚本把这些结论接入私有域知识库（EKB）：

    模块产物 JSON ──提取结论──> lesson（type=`evidence:<module>`，幂等、只读原始文件）

## 用法

    cd backend && ./venv/bin/python scripts/kb_absorb_evidence.py            # 增量接入（幂等）
    cd backend && ./venv/bin/python scripts/kb_absorb_evidence.py --rebuild  # 清空证据层后重建
    cd backend && ./venv/bin/python scripts/kb_absorb_evidence.py --check    # 只体检（不写库）
    cd backend && ./venv/bin/python scripts/kb_absorb_evidence.py --json     # 机器可读输出

## 何时需要跑

  - 手动：模块产物更新后（如 `build_washout_samples.py`、`game_features.py`、自证回放）
  - 自动：每日维护 `kb_context.distill_and_reindex()` 已内置接入，通常无需手动
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.kb import kb_evidence, kb_store  # noqa: E402

_CN = {
    "washout": "洗盘确认信号", "buy_types": "四类买点回测", "game_features": "人心博弈特征",
    "sentiment_gate": "情绪门控", "discipline_grid": "纪律网格择优",
    "entry_guidance": "最佳入场", "condition_table": "条件概率表",
    "form_leaderboard": "形态规则有效性", "cost_basis": "成本与滑点口径",
    "regime": "市场环境分布", "selfproof": "自证回测",
    "pool_filter_oos": "池过滤样本内外", "walk_forward": "滚动前向验证",
    "agent_refine": "LLM 精筛净效果", "lowmom_scheme": "低动量方案判定",
    "turnover_cost": "换手成本重放", "t_overlap": "T 重叠显著性重检",
    "ours_vs_baseline": "我们 vs 基准", "size_baseline": "市值分层基线",
    "data_validation": "数据可信度门", "early_data_quality": "早期数据可比性",
    "form_coverage": "形态覆盖率", "improve_effect": "改动实盘追踪",
    "evolution_trend": "进化快照趋势", "probability_table": "概率表",
    "news_verdict": "新闻判定分布", "policy_impact": "政策影响统计",
}


def check() -> dict:
    """只体检不写库：每个产物的存在性 + 可提取条数 + 缺产物清单。"""
    out: dict = {"ok": [], "missing": [], "empty": []}
    for art in kb_evidence.ARTIFACTS:
        mod = str(art.get("module") or "")
        fname = str(art.get("file") or "")
        path = os.path.join(kb_store.DATA_DIR, fname)
        if not fname or not os.path.exists(path):
            out["missing"].append({"module": mod, "file": fname})
            continue
        try:
            data = kb_evidence._read_json(path, None)  # noqa: SLF001
            items = art["extract"](data) or [] if data is not None else []
        except Exception as exc:  # noqa: BLE001
            out["empty"].append({"module": mod, "file": fname, "error": str(exc)[:80]})
            continue
        if items:
            out["ok"].append({"module": mod, "file": fname, "items": len(items)})
        else:
            out["empty"].append({"module": mod, "file": fname, "reason": "无可提取结论"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="把模块验证产出接入进化大脑（EKB）")
    ap.add_argument("--rebuild", action="store_true", help="清空证据层后重建（id 口径升级用）")
    ap.add_argument("--check", action="store_true", help="只体检，不写库")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    args = ap.parse_args()

    if args.check:
        r = check()
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            print(f"可提取 {len(r['ok'])} 个产物 / 缺产物 {len(r['missing'])} / 空结论 {len(r['empty'])}")
            for it in r["ok"]:
                print(f"  ✅ {_CN.get(it['module'], it['module']):<16} {it['file']:<28} {it['items']} 条")
            for it in r["missing"]:
                print(f"  ⏭️  {_CN.get(it['module'], it['module']):<16} {it['file']:<28} 产物不存在")
            for it in r["empty"]:
                print(f"  ⚠️  {_CN.get(it['module'], it['module']):<16} {it['file']:<28} "
                      f"{it.get('reason') or it.get('error')}")
        return 0

    print("== 初始化知识库 ==")
    print("init_schema:", kb_store.init_schema())
    res = kb_evidence.absorb_all(rebuild=args.rebuild)
    st = kb_evidence.stats()
    if args.json:
        print(json.dumps({"absorb": res, "stats": st}, ensure_ascii=False, indent=2))
    else:
        print("\n== 本次接入 ==")
        for mod, n in res.items():
            if mod == "total":
                continue
            flag = "✅" if n else "⏭️"
            print(f"  {flag} {_CN.get(mod, mod):<16} {n} 条")
        print(f"  合计 {res.get('total', 0)} 条")
        print("\n== 库内证据总览 ==")
        print(f"  {st['n']} 条（active {st['active']}），来源 {len(st['by_module'])} 类")
        for mod, n in sorted(st["by_module"].items(), key=lambda kv: -kv[1]):
            print(f"    {_CN.get(mod, mod):<16} {n} 条")
    return 0 if int(res.get("total", 0) or 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
