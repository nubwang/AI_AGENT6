"""进化大脑私有域知识库（EKB — Evolution Knowledge Base）

对齐规划：plans/21-进化大脑私有域知识库.md

包结构：
  - kb_store.py     存储层（唯一真源 evolution_kb.sqlite，五类档案 schema + 幂等 upsert + 结构化查询）
  - kb_writer.py    写入网关（异步队列 + 批量 + 去重 + 失败静默，绝不阻塞主流程）
  - kb_index.py     检索层（结构化 SQL + BM25 + 向量 RRF 融合 + 时间衰减）
  - kb_distiller.py 蒸馏层（教训生成/升权降权/退役）
  - kb_context.py   综合分析上下文构建（build_knowledge_context，注入四处分析）

设计边界：本包只读/写自己的 evolution_kb.sqlite，不改动任何推荐/回测主流程文件。
"""
from __future__ import annotations

__all__ = [
    "kb_store",
    "kb_writer",
]
