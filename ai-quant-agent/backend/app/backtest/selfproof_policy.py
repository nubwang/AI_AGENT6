"""自证测试的护栏与口径（selfproof_policy）— 对齐 plans/25 §五 / §十三（F1/F2/F4/F8）

## 为什么必须单独一个模块

自证测试最容易"自欺欺人"：用未来数据、用含未来知识的提示词、用被反复调过的区间去"证明"自己准。
本模块把这些**不可协商的口径**集中到一处，供回放内核、报告生成、进化验收**共同调用**，
避免"每个模块各自记得"这种必然失守的做法：

  1. `REAL_ASSETS` / `sandbox_path()` / `assert_not_real_asset()`
     —— 回放**禁止**写真实资产（plans/25 G3：污染 monitor_state/daily_recommend 会让进化学错）
  2. `FORWARD_LOOK_ITEMS` / `forward_look_flags()`
     —— 逐项判定"注入给 LLM 的知识能否 as-of 过滤"（F2：这是**真正的未来函数**，比模式库前视更严重）
  3. `TERMINOLOGY` / `main_metric()`
     —— 术语与主指标口径（F1：回放是"用今天的系统重演当年数据"，不是"当年那套系统的成绩"；
        F8：主指标 = 超额 excess_vs_market，避免被市场 beta 主导）
  4. `split_ratio()` / `is_oos_year()` / `frozen_oos_years()`
     —— IS/OOS 分离，验收只看 OOS，且 OOS 与实盘反馈解耦（F4）
  5. `report_flags()`
     —— 报告/UI **必带**的声明（前视徽标、口径、样本量、数据门、幸存者偏差、费用）

## 参数来源

`evolution_config.json` 的 `selfproof_*` 参数（由 `scripts/add_selfproof_params.py` 注入），
读不到时回退本模块默认值（J1：绝不因配置缺失而崩回放）。
"""
from __future__ import annotations

import json
import os

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data",
)

# 沙箱根目录：自证回放的所有产物只允许落在这里
SANDBOX_ROOT_NAME = "selfproof"
SANDBOX_ROOT = os.path.join(DATA_DIR, SANDBOX_ROOT_NAME)

# ── ① 保护区：回放**禁止**写的真实资产 ──────────────────────────
REAL_ASSETS: list[dict] = [
    {"path": os.path.join(DATA_DIR, "monitor_state.json"), "kind": "file",
     "why": "真实命中台账：被回放写入会污染命中率与进化基线"},
    {"path": os.path.join(DATA_DIR, "daily_recommend"), "kind": "dir",
     "why": "真实每日榜单落盘目录：回放榜单必须落沙箱"},
    {"path": os.path.join(DATA_DIR, "decision_records.json"), "kind": "file",
     "why": "决策台账：T+5 反思与统一学习消费它"},
    {"path": os.path.join(DATA_DIR, "verify_kb.json"), "kind": "file",
     "why": "次日验证知识库主序列：回放结论必须写沙箱分区"},
    {"path": os.path.join(DATA_DIR, "reflect_kb.json"), "kind": "file",
     "why": "反思教训库：回放不得往里写'未来教训'"},
    {"path": os.path.join(DATA_DIR, "evolution_config.json"), "kind": "file",
     "why": "可进化参数：回放不得修改参数（否则等于自我作弊）"},
    {"path": os.path.join(DATA_DIR, "evolution_ledger.json"), "kind": "file",
     "why": "进化记账：只有真正生效的变更才记账"},
    {"path": os.path.join(DATA_DIR, "learning_state.json"), "kind": "file",
     "why": "学习游标：回放不得推进真实学习进度"},
]

# 允许写入、但必须**显式带来源标签**的共享资产（事件流是审计用，允许追加）
SHARED_ASSETS_WITH_SOURCE = [
    {"path": os.path.join(DATA_DIR, "evolution_events.db"), "require_source": "selfproof",
     "why": "进化事件流：允许追加，但 source 必须为 selfproof，便于与真实事件区分"},
]

# ── ② 前视清单：逐项判定注入知识可否 as-of 过滤 ─────────────────
# filterable=True  → 必须按 field 过滤到 <= as_of
# filterable=False → **不可过滤**：要么回放禁用（tavily），要么打徽标（模式库）
FORWARD_LOOK_ITEMS: list[dict] = [
    {"key": "tavily", "name": "Tavily 实时搜索（消息面）", "filterable": False, "field": "",
     "action": "forbid",
     "note": "返回的是**今天**的新闻；历史回放必须关闭，否则消息面静默穿越（原规划 G5）"},
    {"key": "policy_kb", "name": "政策库 policy_kb（含时效衰减）", "filterable": True,
     "field": "发布日", "action": "filter",
     "note": "按 发布日 <= as_of 裁剪；否则会引用未来政策"},
    {"key": "reflect_kb", "name": "反思教训库 reflect_kb", "filterable": True,
     "field": "教训产生日", "action": "filter",
     "note": "F2 核心：把 as_of 之后的教训注入 prompt = 真未来函数"},
    {"key": "verify_kb", "name": "次日验证结论 verify_kb", "filterable": True,
     "field": "验证日期", "action": "filter",
     "note": "同上；回放按 as_of 过滤"},
    {"key": "ekb", "name": "进化私有域知识库 EKB（案例/教训/实验卡）", "filterable": True,
     "field": "date / created_at", "action": "filter",
     "note": "EKB 是长期记忆，含未来案例，必须按日期切"},
    {"key": "news_kb", "name": "消息面教训库 news_lesson_kb / news_verdicts", "filterable": True,
     "field": "日期", "action": "filter", "note": "同上"},
    {"key": "attribution_kb", "name": "回测归因知识 attribution_kb", "filterable": False, "field": "",
     "action": "badge",
     "note": "由整段回测产出，含全样本信息 → 报告打徽标（后续可做 as-of 重建）"},
    {"key": "condition_table", "name": "条件概率表 condition_table", "filterable": True,
     "field": "统计样本区间", "action": "asof_rebuild",
     "note": "纯统计产物，**可低成本 as-of 重建**（优先做）"},
    {"key": "probability_table", "name": "条件概率表 probability_table", "filterable": True,
     "field": "统计样本区间", "action": "asof_rebuild", "note": "同上"},
    {"key": "feature_normalizer", "name": "归一化参数 feature_normalizer", "filterable": True,
     "field": "拟合样本区间", "action": "asof_rebuild",
     "note": "尺度参数，前视影响相对小，但可重建"},
    {"key": "form_leaderboard", "name": "规则形态排行榜 form_leaderboard", "filterable": True,
     "field": "验证区间", "action": "filter", "note": "按验证区间 <= as_of 过滤"},
    {"key": "entry_guidance", "name": "入场指引 entry_guidance", "filterable": False, "field": "",
     "action": "badge", "note": "成功组统计产物 → 徽标"},
    {"key": "pattern_library", "name": "ChromaDB 形态模式库", "filterable": False, "field": "",
     "action": "badge",
     "note": "全样本构建，回放历史时含样本外成分 → 徽标；后续可选'时间切分重建'（贵）"},
]


def _selfproof_param(name: str, default):
    """读 selfproof_* 可进化参数（失败回退默认，J1）。"""
    try:
        from app.agents import evolution_config
        v = evolution_config.get_param(name, default)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


# ── ③ 术语与口径（F1 / F8）────────────────────────────────────
TERMINOLOGY = {
    "replay_metric_name": "重演口径胜率",
    "forbidden_claims": [
        "完全复刻每日推荐",       # F1：回放用的是今天的模型/提示词/知识库
        "当年那套系统的成绩",
        "证明系统准确",           # 只能说"重演口径下表现如何"
    ],
    "canonical_statement": (
        "本次结果为『用今天的模型与知识库，在当年数据上重演每日推荐』的表现，"
        "**不等于**当年那套系统的历史成绩；亦非实盘承诺。"
    ),
}


def main_metric() -> dict:
    """主指标口径：**继承** evolution_config.main_metric（宪法口径，禁止自证自改，F8）。"""
    try:
        from app.agents import evolution_config
        mm = evolution_config.get("main_metric") or {}
    except Exception:  # noqa: BLE001
        mm = {}
    return {
        "hit_window": int(mm.get("hit_window") or _selfproof_param("hit_window", 5) or 5),
        "hit_definition": str(mm.get("hit_definition") or "excess_vs_market"),
        "immutable": bool(mm.get("immutable", True)),
        "note": "主指标=超额（vs 市场基准），绝对胜率仅作辅 —— 防止被市场 beta 主导",
    }


# ── ④ IS/OOS 分离（F4）─────────────────────────────────────────
def split_ratio() -> float:
    """自证集占比（默认 0.8）——**护栏参数**，改动需人工确认。"""
    try:
        v = float(_selfproof_param("selfproof_is_ratio", 0.8))
    except (TypeError, ValueError):
        v = 0.8
    return min(max(v, 0.5), 0.95)


def oos_mode() -> str:
    """OOS 切分模式：`year_kfold`（默认，按年份滚动留出）/ `time_tail`（旧方案，仅兼容）。"""
    return str(_selfproof_param("selfproof_oos_mode", "year_kfold") or "year_kfold")


def frozen_oos_years() -> list[int]:
    """冻结的留出年份集合（如 [2019, 2022]）；空列表表示由 year_kfold 轮换决定。"""
    try:
        raw = _selfproof_param("selfproof_oos_years", []) or []
    except Exception:  # noqa: BLE001
        raw = []
    out: list[int] = []
    for x in raw if isinstance(raw, (list, tuple)) else []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def is_oos_year(year: int, fold: int | None = None) -> bool:
    """某年是否为 OOS（验收集）。

    - 有冻结集合 → 只看冻结集合；
    - 否则按 `year_kfold`：用 (year + fold) % 5 轮换留出（fold 由调用方按轮次传入，保证可复现）；
    - `time_tail` 模式返回 False（由调用方按时间轴自行切分，兼容旧口径）。
    """
    frozen = frozen_oos_years()
    if frozen:
        return int(year) in frozen
    if oos_mode() == "year_kfold":
        return ((int(year) + int(fold or 0)) % 5) == 0
    return False


def oos_note() -> str:
    return ("验收集（OOS）与实盘反馈**解耦**：实盘结果只做监控存证，不进自证验收判据 —— "
            "否则'进化影响实盘、实盘又当验收'会形成污染闭环（F4）")


# ── ⑤ 沙箱路径与写入守卫（G3）─────────────────────────────────
def sandbox_path(*parts: str) -> str:
    """拼沙箱路径（data/selfproof/...）。"""
    return os.path.join(SANDBOX_ROOT, *parts)


class RealAssetWriteError(RuntimeError):
    """回放试图写真实资产（护栏拦截）。"""


def _norm(p: str) -> str:
    return os.path.normpath(os.path.abspath(str(p)))


def hit_real_asset(path: str) -> str:
    """返回命中的保护区路径（未命中返回空串）。"""
    target = _norm(path)
    for item in REAL_ASSETS:
        p = _norm(item["path"])
        if target == p or target.startswith(p + os.sep):
            return item["path"]
    return ""


def assert_not_real_asset(path: str, *, action: str = "write") -> None:
    """写入前守卫：命中保护区 → 抛错（回放只能在沙箱里写）。"""
    hit = hit_real_asset(path)
    if hit:
        why = next((i["why"] for i in REAL_ASSETS if i["path"] == hit), "")
        raise RealAssetWriteError(
            f"自证回放禁止{action}真实资产 {hit}（{why}）——请改写沙箱 {SANDBOX_ROOT}"
        )


def is_sandboxed(path: str) -> bool:
    return _norm(path).startswith(_norm(SANDBOX_ROOT) + os.sep)


# ── ⑥ 前视徽标与报告声明 ───────────────────────────────────────
def forward_look_flags(as_of: str = "", disabled: list[str] | None = None) -> dict:
    """构造前视清单的处置结果（供报告/UI 徽标与审计）。

    Returns: {"as_of", "items": [...], "filtered": [...], "badged": [...],
              "forbidden": [...], "unresolved": [...], "ok": bool}
    """
    disabled = [str(x) for x in (disabled or [])]
    items, filtered, badged, forbidden, unresolved = [], [], [], [], []
    for it in FORWARD_LOOK_ITEMS:
        row = dict(it)
        if it["key"] in disabled:
            row["effective"] = "forbidden"
            forbidden.append(it["key"])
        elif it["action"] == "forbid":
            row["effective"] = "forbidden"
            forbidden.append(it["key"])
        elif it["action"] in ("filter", "asof_rebuild"):
            row["effective"] = it["action"]
            filtered.append(it["key"])
        else:
            row["effective"] = "badge"
            badged.append(it["key"])
            unresolved.append(it["key"])
        items.append(row)
    return {
        "as_of": str(as_of or ""),
        "items": items,
        "filtered": filtered,
        "badged": badged,
        "forbidden": forbidden,
        "unresolved": unresolved,
        "ok": len(unresolved) == 0,
        "note": "unresolved = 无法 as-of 过滤、只能打徽标的项（报告必须显式声明）",
    }


def report_flags(as_of: str = "", *, model: str = "", knowledge_cutoff: str | None = None,
                 data_gate: dict | None = None, survivorship: dict | None = None,
                 usage: dict | None = None, extra: dict | None = None) -> dict:
    """报告/UI **必带**的声明块（F1/F2/F7/F10/F13 的落地物）。"""
    mm = main_metric()
    out = {
        "as_of": str(as_of or ""),
        "metric_name": TERMINOLOGY["replay_metric_name"],
        "main_metric": mm,
        "forward_look": forward_look_flags(as_of),
        "model": str(model or ""),
        "knowledge_cutoff": str(knowledge_cutoff or as_of or ""),
        "statement": TERMINOLOGY["canonical_statement"],
        "history_note": "历史数据边界与幸存者偏差见 data_gate / survivorship（P0.5 产出）",
        "is_oos": {"mode": oos_mode(), "ratio": split_ratio(),
                   "frozen_oos_years": frozen_oos_years(), "note": oos_note()},
        "data_gate": data_gate or {},
        "survivorship": survivorship or {},
        "usage": usage or {},
    }
    if extra:
        out.update(extra)
    return out


def write_json_guarded(path: str, data) -> bool:
    """**带护栏**的 JSON 落盘：回放内核统一走这个函数写盘。

    契约：命中真实资产 → **抛 `RealAssetWriteError`**（不静默返回 False）——
    静默失败会掩盖 bug，让"以为写进沙箱了、其实没写"这种错误变成一个月的排查。
    只有"沙箱路径 + IO 失败"才返回 False。
    """
    assert_not_real_asset(path, action="write")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "DATA_DIR", "SANDBOX_ROOT", "SANDBOX_ROOT_NAME", "REAL_ASSETS",
    "SHARED_ASSETS_WITH_SOURCE", "FORWARD_LOOK_ITEMS", "TERMINOLOGY",
    "main_metric", "split_ratio", "oos_mode", "frozen_oos_years", "is_oos_year", "oos_note",
    "sandbox_path", "hit_real_asset", "assert_not_real_asset", "is_sandboxed",
    "RealAssetWriteError", "forward_look_flags", "report_flags", "write_json_guarded",
]
