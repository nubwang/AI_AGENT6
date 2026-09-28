"""Agent交互接口 — 进化大脑对话（plans/20：你与进化大脑对话，像 ZOO CODE 一样对话后修改代码）

- POST /api/v1/agent/ask       与进化大脑对话（注入 L0/回测诊断/系统图谱；可建议改代码）
- GET  /api/v1/agent/status     进化大脑状态
"""
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/agent", tags=["Agent交互"])


class AskRequest(BaseModel):
    question: str
    ts_code: str | None = None


# 进化大脑系统提示：结合系统近期表现回答主人，可建议改代码（唯一宪法=提高每日推荐上涨概率）
_ASK_SYSTEM = """你是本系统（A股每日推荐量化系统）的"进化大脑"。
你在与系统主人对话，像 ZOO CODE 一样既能回答问题、也能动手修改代码。
职责：结合系统近期表现（次日验证命中率/失效归因/环节归因/回测健康诊断/系统图谱/当前参数），
回答主人的问题，并为提高"每日推荐股票的上涨概率"（D+1 买入后上涨 + 后续主升浪）给出可操作建议，
建议要指向具体文件或参数（如 daily_scan.py 的负向抑制、form_classifier 的形态识别、threshold_analyzer 的条件表等）。
你拥有修改回测与每日推荐代码的能力；若主人要求改代码、或你判断某个环节问题最大需要改代码，
就说明你的改动方案并设置 suggest_code_change=true（主人确认后由系统生成代码提案并走备份/编译/回滚护栏应用）。
重要原则（用户决策）：回测历史有重要研究价值，**不要通过"降低相似度权重/调弱负向抑制"来妥协**——那等于放弃回测成果；
当实盘命中低于回测时，优先**改进回测逻辑本身**（结局分流/特征拟合/模式库/条件表/归一化）及**回测→每日推荐的传导一致性**（daily_scan 加载口径、特征同源），让更好的回测服务于每日推荐。
输出 JSON 对象（不要其他文字）：
{
  "answer": "对话回复（中文，专业、具体、简洁；必要时引用次日命中率/回测诊断/环节归因数据）",
  "suggest_code_change": true 或 false,
  "code_target": "若建议改代码，填目标方向描述（如'负向抑制参数'、'形态识别'、'回测结局分流'），否则空字符串"
}"""


@router.post("/ask")
async def ask_agent(req: AskRequest):
    """与进化大脑对话：注入 L0 / 回测诊断 / 系统图谱，返回回答与是否建议改代码。"""
    from app.agents import evolution_summarizer, backtest_ctl, llm_client
    try:
        l0 = evolution_summarizer.build_l0_text()
    except Exception as exc:  # noqa: BLE001
        l0 = f"（L0 摘要生成失败: {exc}）"
    try:
        bt = backtest_ctl.health_text()
    except Exception:  # noqa: BLE001
        bt = ""
    try:
        from app.agents import system_map
        mp = system_map.build_map_txt(short=True)
    except Exception:  # noqa: BLE001
        mp = ""
    # plans/21 §8.2：对话必须结合私有域知识库（历史同类案例/教训/已试改动与否证清单），禁止单点分析
    try:
        from app.kb import kb_context as _kctx
        kb_txt = _kctx.knowledge_txt(query=req.question)
    except Exception:  # noqa: BLE001
        kb_txt = ""
    # 价格化操作计划（用户诉求："入场建议要标明什么价格买入、什么价格卖出"）：
    # 提问里带 6 位股票代码（或显式传 ts_code）时，直接把该票的买卖价位注入上下文
    plan_txt = ""
    try:
        import re as _re
        code6 = str(req.ts_code or "")
        if not code6:
            m = _re.search(r"(\d{6})", req.question or "")
            code6 = m.group(1) if m else ""
        if code6:
            from app.api import predictions as _pred
            today = _pred._data_date()
            rep = _pred._load_agent_report(today) or _pred.load_report(today) or {}
            rep = _pred._with_plans(rep, today)
            pick = next((p for p in (rep.get("top_picks") or [])
                         if str(p.get("ts_code") or "").startswith(code6)), None)
            if pick and pick.get("entry_plan"):
                pl = pick["entry_plan"]
                b = pl.get("basis") or {}
                plan_txt = (
                    f"[操作计划] {pick.get('ts_code')} {pick.get('name')}（形态 {pick.get('form_type')}）："
                    f"{pl.get('text')}；依据={b.get('source')}，样本 {b.get('samples')}，"
                    f"T+5 胜率 {b.get('win_rate_t5')}"
                )
            elif pick:
                plan_txt = f"[操作计划] {pick.get('ts_code')} 暂无价格计划（缺 T0 收盘价或形态统计）"
    except Exception:  # noqa: BLE001
        plan_txt = ""
    user = (f"系统近期表现:\n{l0}\n\n回测健康诊断:\n{bt}\n\n系统图谱(简化):\n{mp}\n\n"
            + (f"{kb_txt}\n\n" if kb_txt else "")
            + (f"{plan_txt}\n\n" if plan_txt else "")
            + f"主人：{req.question}")
    try:
        result = llm_client.deepseek_chat([
            {"role": "system", "content": _ASK_SYSTEM},
            {"role": "user", "content": user},
        ])
    except Exception as exc:  # noqa: BLE001
        return {"answer": f"进化大脑暂时无法回复：{exc}", "suggest_code_change": False,
                "code_target": ""}
    if not isinstance(result, dict):
        return {"answer": "进化大脑暂无回复（LLM 解析失败，请稍后再试）",
                "suggest_code_change": False, "code_target": ""}
    answer = str(result.get("answer", "")).strip() or "（无回复）"
    suggest = bool(result.get("suggest_code_change"))
    code_target = str(result.get("code_target", "") or "")
    # ZOO CODE 式：进化大脑判定需改代码 → 后端自动生成代码提案（manual 存待确认），
    # 前端随即展示提案卡片，你只需确认"应用"即可，无需再手动点"生成提案"。
    proposal_info = {}
    if suggest:
        try:
            from app.agents import code_writer
            # 把用户自然语言指令传给提案生成 → LLM 自动定位要改的函数/文件/参数（无需用户报函数名）
            pr = code_writer.propose_code_evolution(
                summary=l0, mode="manual",
                instruction=f"{req.question}（进化大脑建议方向：{code_target}）" if code_target else req.question)
            proposal_info = {
                "proposal_generated": bool(pr.get("ok")),
                "proposal_message": pr.get("message", ""),
                "proposal": pr.get("proposal"),
            }
            if not pr.get("ok"):
                answer += f"\n\n（代码提案生成失败：{pr.get('message', '')}）"
        except Exception as exc:  # noqa: BLE001
            proposal_info = {"proposal_generated": False,
                             "proposal_message": f"提案生成异常: {exc}"}
    return {
        "answer": answer,
        "suggest_code_change": suggest,
        "code_target": code_target,
        **proposal_info,
    }


@router.get("/status")
async def agent_status():
    """查看进化大脑状态。"""
    return {"status": "idle", "last_run": None}
