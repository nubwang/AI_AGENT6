"""接线自证：新增模块（plans/23·24·25）的验证产出是否真的成为"进化大脑的神经"

用户诉求：**"新增的这些都写入进化大脑了么？都成大脑的神经了么？"**
本脚本不靠"应该接了"的口头保证，逐条给出可核对证据：

  1. **接入**：kb_evidence.absorb_all() 写入条目 > 0，且覆盖模块 ≥ 3
  2. **幂等**：连续两次接入，库内证据条数不变（确定性 id，不产生重复事实）
  3. **可检索**：证据型 lesson 能被 kb_index.search_lessons 召回（结构化 + BM25 路径）
  4. **进 L0**：kb_index.l0_text() 含"实证结论"（进化大脑每轮摘要必读）
  5. **进综合分析**：kb_context.knowledge_txt() 含"实证结论"与硬规则⑤
  6. **看板可见**：kb_index.stats() 含 plans / evidence；/evolve/kb/evidence 可从库取数

用法：
  cd backend && ./venv/bin/python scripts/verify_evolution_evidence_wiring.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.kb import kb_evidence, kb_index, kb_store  # noqa: E402


def main() -> int:
    print("== 0. 初始化知识库 ==")
    print("init_schema:", kb_store.init_schema())

    print("\n== 1. 接入新增模块验证产出 ==")
    r1 = kb_evidence.absorb_all()
    print("接入结果:", r1)
    n1 = len(kb_evidence.evidence_lessons(limit=500))
    mods = {str((l.get("scope") or {}).get("module") or "-")
            for l in kb_evidence.evidence_lessons(limit=500)}
    print(f"证据型教训: {n1} 条，覆盖模块 {len(mods)}: {sorted(mods)}")
    # 产物体检：登记表里的文件是否都在（缺产物 = 该模块没跑或路径变了）
    missing = [str(a.get("file")) for a in kb_evidence.ARTIFACTS
               if not os.path.exists(os.path.join(kb_store.DATA_DIR, str(a.get("file"))))]
    print(f"登记产物 {len(kb_evidence.ARTIFACTS)} 个，缺失 {len(missing)} 个"
          + (f": {missing}" if missing else ""))

    print("\n== 2. 幂等校验（二次接入条数不变）==")
    r2 = kb_evidence.absorb_all()
    n2 = len(kb_evidence.evidence_lessons(limit=500))
    print(f"第二次接入: {r2}；条数 {n1} → {n2}")

    print("\n== 3. 检索可达性 ==")
    kw = "买点"
    hits = kb_index.search_lessons(query=kw, limit=5, use_vector=False)
    ev_hits = [h for h in hits if str(h.get("type") or "").startswith("evidence:")]
    print(f"query='{kw}' 命中 {len(hits)} 条，其中证据型 {len(ev_hits)} 条")
    for h in hits[:3]:
        print(f"  - [{h.get('type')}] {(h.get('text') or '')[:60]}")

    print("\n== 4. L0 摘要 ==")
    l0 = kb_index.l0_text()
    print(l0[:1200])

    print("\n== 5. 综合分析上下文 ==")
    from app.kb import kb_context
    ktxt = kb_context.knowledge_txt(query="为什么推荐没涨")
    print(ktxt[:1200])

    print("\n== 6. 看板统计 ==")
    print("index.stats:", kb_index.stats())
    print("evidence.stats:", kb_evidence.stats())

    print("\n== 判定 ==")
    checks = [
        ("接入产出 > 0", int(r1.get("total", 0) or 0) > 0),
        ("覆盖模块 ≥ 20", len(mods) >= 20),
        ("登记产物无缺失", len(missing) == 0),
        ("交易计划台账已入脑", int(kb_index.stats().get("plans") or 0) > 0),
        ("幂等（条数不变）", n1 == n2),
        ("证据可检索", len(ev_hits) > 0),
        ("进 L0（含实证结论）", "实证结论" in l0),
        ("进综合分析（含实证结论）", "实证结论" in ktxt),
        ("硬规则⑤已注入", "必须引用或解释为何不采纳" in ktxt),
        ("看板含 plans/evidence", "plans" in kb_index.stats() and "evidence" in kb_index.stats()),
    ]
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n结果: {'✅ 全部通过' if not failed else '❌ 未通过 ' + '、'.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
