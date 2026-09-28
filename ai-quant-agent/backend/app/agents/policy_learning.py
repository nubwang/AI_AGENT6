"""政策自学层（policy_learning）

对齐 plans/03-Agent系统.md §19：让政策 Agent 理解官方书面潜在意思、从"政策→板块验证"中自学。

  - policy_intent_kb.json：官方措辞→潜在意图映射库（内置种子 + 自学校正）
  - policy_impact_kb.json：政策→板块实证库（解读后跟踪板块 T+1/T+5/T+20 回填）
  - deep_intent：政策解读时标注"潜在意图研判"（字面 vs 潜在意图 vs 预期差）
  - backfill_sector_impact()：板块跟踪（申万行业指数 index_daily，T+1/T+5/T+20 涨幅）
  - 自学反馈：板块实际表现 vs 解读强度 → 校正意图库措辞力度

知识边界：仅影响政策判断/风险提示，不改规则概率（防参数漂移）。
"""
from __future__ import annotations

import json
import os
import re
import time

from sqlalchemy import text

from app.core.logger import logger
from app.models import SessionLocal

INTENT_KB_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "policy_intent_kb.json",
)
IMPACT_KB_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "policy_impact_kb.json",
)
# 板块跟踪窗口
TRACK_WINDOWS = (1, 5, 20)

# 官方措辞→潜在意图 内置种子（自学校正会调整 strength/count）
_SEED_INTENTS = [
    {"phrase": "大力发展", "intent": "强利好", "strength": 0.9, "note": "财政+税收+考核多重保障"},
    {"phrase": "加快", "intent": "强利好", "strength": 0.85, "note": "自上而下推动，落地快"},
    {"phrase": "强力支持", "intent": "强利好", "strength": 0.9, "note": "明确资金/资源倾斜"},
    {"phrase": "重点支持", "intent": "利好", "strength": 0.8, "note": "方向明确但力度视细节"},
    {"phrase": "鼓励", "intent": "利好", "strength": 0.7, "note": "引导性支持"},
    {"phrase": "稳妥推进", "intent": "中性偏谨慎", "strength": 0.4, "note": "试点先行，非全面铺开"},
    {"phrase": "有序开展", "intent": "中性", "strength": 0.5, "note": "按节奏推进"},
    {"phrase": "研究制定", "intent": "中性偏谨慎", "strength": 0.3, "note": "尚在酝酿，落地待观察"},
    {"phrase": "适时出台", "intent": "预期管理", "strength": 0.5, "note": "情绪催化，落地待观察"},
    {"phrase": "预期引导", "intent": "预期管理", "strength": 0.5, "note": "政策吹风，先行情绪"},
    {"phrase": "规范发展", "intent": "收紧利空", "strength": 0.25, "note": "行业出清，龙头受益"},
    {"phrase": "严控", "intent": "收紧利空", "strength": 0.15, "note": "限制扩张，利空供给端"},
    {"phrase": "坚决遏制", "intent": "收紧利空", "strength": 0.1, "note": "强监管，重大利空"},
    {"phrase": "防止过度", "intent": "收紧利空", "strength": 0.2, "note": "防过热，边际收紧"},
    {"phrase": "万亿", "intent": "量化资金利好", "strength": 0.95, "note": "体量巨大，景气强"},
    {"phrase": "千亿", "intent": "量化资金利好", "strength": 0.85, "note": "大额资金，明确景气"},
    {"phrase": "中央财政", "intent": "量化资金利好", "strength": 0.8, "note": "中央出资，信用背书"},
    {"phrase": "试点", "intent": "中性偏谨慎", "strength": 0.45, "note": "局部试验，全面推广待验证"},
    {"phrase": "补贴", "intent": "利好", "strength": 0.75, "note": "直接补贴，盈利弹性"},
    {"phrase": "退出机制", "intent": "收紧利空", "strength": 0.2, "note": "补贴退坡，盈利承压"},
]

# 政策行业 → 申万一级行业关键词（优先映射；找不到再用 SQL 模糊匹配）
# 说明：index_daily 仅有一级行业指数（801010-801250，旧申万分类），二级（如 801081 半导体）无行情 → 映射到一级行业
_SECTOR_INDEX_HINTS = {
    "半导体": "电子", "芯片": "电子", "集成电路": "电子", "消费电子": "电子", "面板": "电子", "光学": "电子",
    "通信": "信息设备", "5g": "信息设备", "光模块": "信息设备", "运营商": "信息设备",
    "光伏": "机械设备", "新能源": "机械设备", "储能": "机械设备", "电池": "机械设备", "风电": "机械设备",
    "汽车": "交运设备", "新能源车": "交运设备", "军工": "交运设备", "航空": "交运设备", "航天": "交运设备", "船舶": "交运设备",
    "化工": "基础化工", "化工原料": "基础化工", "化学": "基础化工", "塑料": "基础化工",
    "医药": "医药生物", "生物": "医药生物", "医疗器械": "医药生物", "中药": "医药生物", "疫苗": "医药生物",
    "计算机": "信息服务", "软件": "信息服务", "ai": "信息服务", "互联网": "信息服务", "数据": "信息服务",
    "家电": "家用电器",
    "食品": "食品饮料", "白酒": "食品饮料", "乳制品": "食品饮料", "调味品": "食品饮料",
    "银行": "金融服务", "证券": "金融服务", "保险": "金融服务", "券商": "金融服务", "金融": "金融服务",
    "地产": "房地产", "物业": "房地产",
    "基建": "建筑建材", "建材": "建筑建材", "水泥": "建筑建材", "建筑": "建筑建材",
    "钢铁": "钢铁",
    "煤炭": "采掘", "油气": "采掘", "石油": "采掘",
    "有色": "有色金属", "黄金": "有色金属", "铜": "有色金属", "锂": "有色金属",
    "电力": "公用事业", "环保": "公用事业", "燃气": "公用事业", "水务": "公用事业",
    "农业": "农林牧渔", "养殖": "农林牧渔", "种业": "农林牧渔",
    "机械": "机械设备", "机器人": "机械设备",
    "纺织": "纺织服饰", "服装": "纺织服饰",
    "零售": "商贸零售", "百货": "商贸零售",
    "传媒": "信息服务", "游戏": "信息服务", "影视": "信息服务",
    "旅游": "社会服务", "酒店": "社会服务", "教育": "社会服务",
    "物流": "交通运输", "航运": "交通运输", "港口": "交通运输",
    "轻工": "轻工制造", "造纸": "轻工制造", "包装": "轻工制造",
}


# ── ① 政策措辞-意图库 ─────────────────────────────────────────
def _new_intent_kb() -> dict:
    return {"version": 1, "intents": list(_SEED_INTENTS), "stats": {"calibrated": 0}}


def load_intent_kb(path: str = INTENT_KB_FILE) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and "intents" in d:
            return d
    except Exception:  # noqa: BLE001
        pass
    return _new_intent_kb()


def save_intent_kb(kb: dict, path: str = INTENT_KB_FILE) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(kb, f, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"意图库保存失败: {exc}")
        return False


def detect_intents(text: str, kb: dict | None = None) -> list[dict]:
    """从政策文本检测命中的"官方措辞→潜在意图"信号（按 strength 降序）。"""
    kb = kb or load_intent_kb()
    if not text:
        return []
    hits = []
    for it in kb.get("intents", []):
        phrase = it.get("phrase", "")
        if phrase and phrase in text:
            hits.append({
                "phrase": phrase,
                "intent": it.get("intent", ""),
                "strength": it.get("strength", 0.5),
                "note": it.get("note", ""),
            })
    hits.sort(key=lambda h: -h["strength"])
    return hits


def build_intent_txt(text: str) -> str:
    """把意图检测结果构造为供政策 Agent 参考的"官方措辞潜在意图"文本。"""
    hits = detect_intents(text)
    if not hits:
        return ""
    parts = [f"{h['phrase']}({h['intent']} 力度{h['strength']:.2f}，{h['note']})" for h in hits[:8]]
    return "官方措辞潜在意图信号：" + "；".join(parts)


def calibrate_intent(kb: dict, phrase: str, actual_signal: float) -> None:
    """自学：根据板块实证校正某措辞的实际市场力度（实际表现 vs 预期强度）。

    actual_signal: -1(利空兑现)~+1(利好兑现) 的板块实际反应（T+5 板块涨幅归一化）
    校正逻辑：预期 strength 与实际 signal 偏差 → 向实际靠拢一小步（0.1），并计次。
    """
    for it in kb.get("intents", []):
        if it.get("phrase") == phrase:
            cur = float(it.get("strength", 0.5))
            # 归一化 signal 到 [0,1] 尺度便于与 strength 对齐
            target = max(0.05, min(0.95, (actual_signal + 1) / 2))
            it["strength"] = round(cur + (target - cur) * 0.1, 3)
            it["count"] = int(it.get("count", 0)) + 1
            it["last_actual"] = round(actual_signal, 3)
            break
    kb.setdefault("stats", {})["calibrated"] = int(kb.get("stats", {}).get("calibrated", 0)) + 1


# ── ② 政策-板块实证库 ─────────────────────────────────────────
def _new_impact_kb() -> dict:
    return {"version": 1, "events": [], "stats": {"recorded": 0, "backfilled": 0}}


def load_impact_kb(path: str = IMPACT_KB_FILE) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and "events" in d:
            return d
    except Exception:  # noqa: BLE001
        pass
    return _new_impact_kb()


def save_impact_kb(kb: dict, path: str = IMPACT_KB_FILE) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(kb, f, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"实证库保存失败: {exc}")
        return False


def record_policy_interpretation(
    sector: str,
    grade: str,
    path_txt: str,
    intent_txt: str,
    policy_snippet: str = "",
) -> dict | None:
    """记录一次政策解读事件（待板块跟踪回填）。返回事件或 None。"""
    try:
        kb = load_impact_kb()
        ev = {
            "id": f"{int(time.time())}_{sector}",
            "date": time.strftime("%Y%m%d"),
            "sector": sector,
            "grade": grade or "",
            "intent_txt": (intent_txt or "")[:400],
            "path_txt": (path_txt or "")[:300],
            "policy_snippet": (policy_snippet or "")[:500],
            "outcomes": {},          # {"1": 0.05, "5": -0.02, "20": null}
            "verdict": "pending",    # pending / 准确 / 过乐观 / 过悲观 / 传导偏差
            "backfilled_at": None,
        }
        kb["events"].append(ev)
        if len(kb["events"]) > 2000:
            kb["events"] = kb["events"][-2000:]
        kb["stats"]["recorded"] = int(kb.get("stats", {}).get("recorded", 0)) + 1
        save_impact_kb(kb)
        return ev
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"政策解读记录失败: {exc}")
        return None


# ── ③ 板块指数解析 + 板块跟踪 ─────────────────────────────────
def _index_has_data(ts_code: str) -> bool:
    """判断某指数在 index_daily 是否有行情数据。"""
    db = SessionLocal()
    try:
        r = db.execute(
            text("SELECT 1 FROM index_daily WHERE ts_code=:c LIMIT 1"), {"c": ts_code},
        ).fetchone()
        return r is not None
    except Exception:  # noqa: BLE001
        return False
    finally:
        db.close()


def resolve_sector_index(sector: str) -> str:
    """把政策行业名解析为申万行业指数 ts_code（回退宽基 000001.SH 市场基准）。

    优先内置关键词提示 → SQL 模糊匹配申万指数 → 校验 index_daily 有数据 → 回退上证指数。
    恒返回非空 ts_code。
    """
    sector = (sector or "").strip()
    if not sector:
        return "000001.SH"
    hint = None
    for k, v in _SECTOR_INDEX_HINTS.items():
        if k in sector:
            hint = v
            break
    candidates: list[str] = []
    db = SessionLocal()
    try:
        if hint:
            row = db.execute(
                text("SELECT ts_code FROM index_basic WHERE ts_code LIKE '801%' AND name LIKE :kw LIMIT 1"),
                {"kw": f"%{hint}%"},
            ).fetchone()
            if row:
                candidates.append(str(row[0]))
        # 通用模糊匹配申万指数
        row = db.execute(
            text("SELECT ts_code FROM index_basic WHERE ts_code LIKE '801%' AND name LIKE :kw LIMIT 1"),
            {"kw": f"%{sector}%"},
        ).fetchone()
        if row:
            candidates.append(str(row[0]))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"板块指数解析失败 {sector}: {exc}")
    finally:
        db.close()
    for c in candidates:
        if _index_has_data(c):
            return c
    return "000001.SH"


def sector_recent_gain(sector: str, n: int = 10) -> float | None:
    """板块指数近 n 个交易日累计涨幅（政策利好兑现度：板块已涨过→利好被市场反应，政策"过期"）。

    用 resolve_sector_index 把行业名解析为申万指数，回退上证指数。无数据返回 None。
    """
    ts_code = resolve_sector_index(sector)
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT close FROM index_daily WHERE ts_code=:c ORDER BY trade_date DESC LIMIT :n"),
            {"c": ts_code, "n": n + 1},
        ).fetchall()
        closes = [float(r[0]) for r in rows if r[0] is not None]
        if len(closes) >= 2 and closes[-1]:
            return closes[0] / closes[-1] - 1.0
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"板块涨幅查询失败 {sector}: {exc}")
    finally:
        db.close()
    return None


def _index_outcomes(ts_code: str, date: str) -> dict:
    """某指数在 date（或之后最近交易日）的 T+1/T+5/T+20 前复权涨幅。返回 {"1":..,"5":..,"20":..}。"""
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT trade_date, close FROM index_daily WHERE ts_code=:c ORDER BY trade_date"),
            {"c": ts_code},
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"指数行情查询失败 {ts_code}: {exc}")
        return {}
    finally:
        db.close()
    if not rows:
        return {}
    dates = [str(r[0]) for r in rows]
    closes = [float(r[1]) for r in rows]
    idx = None
    for i, d in enumerate(dates):
        if d >= date:
            idx = i
            break
    if idx is None or closes[idx] <= 0:
        return {}
    base = closes[idx]
    out = {}
    for w in TRACK_WINDOWS:
        j = idx + w
        if j < len(closes) and closes[j] > 0:
            out[str(w)] = round(closes[j] / base - 1.0, 4)
        else:
            out[str(w)] = None
    return out


def _judge_verdict(grade: str, outcomes: dict) -> str:
    """根据解读等级与板块实际 T+5 表现判定解读准确性。"""
    t5 = outcomes.get("5")
    if t5 is None:
        return "pending"
    up = grade in ("S", "A", "利好")
    down = grade in ("利空",)
    if up:
        if t5 > 0.02:
            return "准确"
        if t5 < -0.02:
            return "过乐观"
        return "中性"
    if down:
        if t5 < -0.02:
            return "准确"
        if t5 > 0.02:
            return "过悲观"
        return "中性"
    return "中性"


def backfill_sector_impact() -> dict:
    """板块跟踪 + 自学反馈：回填所有 pending 解读事件的板块表现，校正意图库。"""
    kb = load_impact_kb()
    intents = load_intent_kb()
    backfilled = 0
    for ev in kb.get("events", []):
        if ev.get("verdict") != "pending" or not ev.get("date"):
            continue
        outcomes = _index_outcomes(resolve_sector_index(ev.get("sector", "")), ev["date"])
        if not outcomes or outcomes.get("5") is None:
            continue  # 未到 T+5
        ev["outcomes"] = outcomes
        ev["verdict"] = _judge_verdict(ev.get("grade", ""), outcomes)
        ev["backfilled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        backfilled += 1
        # 自学反馈：命中措辞 → 按板块实际表现校正意图强度
        t5 = outcomes.get("5")
        if t5 is None:
            continue  # 双保险（前面已跳过未到 T+5 的事件）
        signal = max(-1.0, min(1.0, t5 * 5.0))  # ±20% 板块涨幅→±1
        for it in detect_intents(ev.get("intent_txt", "") + ev.get("policy_snippet", ""), intents):
            calibrate_intent(intents, it["phrase"], signal)
    if backfilled:
        save_impact_kb(kb)
        save_intent_kb(intents)
    kb2 = load_impact_kb()
    kb2["stats"]["backfilled"] = int(kb2.get("stats", {}).get("backfilled", 0)) + backfilled
    save_impact_kb(kb2)
    logger.info(
        f"政策板块跟踪: 回填 {backfilled} 条，累计事件 {len(kb2['events'])}，"
        f"意图库校准 {intents.get('stats', {}).get('calibrated', 0)} 次"
    )
    return {"backfilled": backfilled, "events": len(kb2["events"]),
            "calibrated": intents.get("stats", {}).get("calibrated", 0)}


# ── ④ 实证参考（深度研究）─────────────────────────────────────
def build_empirical_txt(sector: str) -> str:
    """构造该行业"历史政策→板块实证"参考文本（深度研究/历史对标用）。"""
    kb = load_impact_kb()
    evs = [e for e in kb.get("events", []) if e.get("sector") == sector and e.get("verdict") != "pending"]
    evs = evs[-8:]
    if not evs:
        return ""
    lines = []
    for e in evs:
        o5 = e.get("outcomes", {}).get("5")
        o5_txt = f"{o5 * 100:.1f}%" if o5 is not None else "N/A"
        lines.append(
            f"- {e['date']} 等级{e.get('grade', '?')} T+5板块{o5_txt} → {e.get('verdict', '')}"
        )
    return f"该行业历史政策-板块实证（仅参考）：\n" + "\n".join(lines)


__all__ = [
    "load_intent_kb", "save_intent_kb", "detect_intents", "build_intent_txt", "calibrate_intent",
    "load_impact_kb", "save_impact_kb", "record_policy_interpretation",
    "resolve_sector_index", "backfill_sector_impact", "build_empirical_txt",
    "INTENT_KB_FILE", "IMPACT_KB_FILE",
]
