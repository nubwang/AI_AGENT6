"""生成式代码进化（code_writer）— 对齐 plans/18（用户核心需求 v2）

用户决策（v2）：**放开安全限制**，进化大脑可修改**全部逻辑与参数**，
唯一限制 = **必须提高每日推荐股票之后的上涨概率**（次日大涨 + 后续主升浪）。

保留的工程护栏（这是"正确性/可恢复性"所需，不是安全限制）：
  1. py_compile 编译验证（语法错误必拦，否则系统直接崩）
  2. 版本化备份 + 失败自动回滚（改坏可一键恢复）
  3. 唯一锚点定位（避免改错位置）或整文件替换（full_file 模式）
  4. 待重启标记 + 独立进程重启生效（Tier2/3 生效机制）
  5. 目标校验：提案必须指向"提高每日推荐上涨概率"（唯一宪法，来自用户）

模式（evolution_config.code_evolution.security_level）：
  - full（默认）：无 AST 白名单 / 无保护区限制，可改任意回测/推荐逻辑与参数
  - safe（可选）：保留 AST 白名单 + 进化模块保护区（保守模式）
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import time

from app.core.logger import logger
from app.agents import self_improver
from app.agents.llm_client import deepseek_chat

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
PROJECT_ROOT = os.path.dirname(DATA_DIR)  # backend/
STATE_FILE = os.path.join(DATA_DIR, "code_writer_state.json")
MONITOR_STATE_FILE = os.path.join(DATA_DIR, "monitor_state.json")

# ── 安全白名单（仅 safe 模式使用；full 模式绕过）────────────────
SAFE_MODULES = {"np", "pd", "math"}
SAFE_BUILTINS = {
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter",
    "float", "int", "isinstance", "len", "list", "map", "max", "min", "pow",
    "range", "round", "sorted", "str", "sum", "tuple", "zip",
}
SAFE_CALLS = set(SAFE_BUILTINS)
_FORBIDDEN_TYPES = (
    ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal,
    ast.Await, ast.Yield, ast.While, ast.Raise,
)
for _extra in ("Exec", "Match", "TryStar", "TypeAlias"):
    _t = getattr(ast, _extra, None)
    if _t is not None:
        _FORBIDDEN_TYPES += (_t,)
_DANGEROUS_SUBSTR = (
    "open(", "eval(", "exec(", "__import__", "os.", "sys.", "subprocess",
    "requests", "socket", "shutil", "pathlib", "getattr(", "setattr(",
    "globals(", "locals(", "compile(", "http://", "https://", "import ",
)
_MAX_CODE_LEN = 200000  # 整文件重写上限（放大，允许大文件）


def _security_level() -> str:
    """进化模式：full（无安全限制，默认）/ safe（保守白名单）。"""
    try:
        from app.agents import evolution_config
        ce = evolution_config.get("code_evolution", {}) or {}
        return str(ce.get("security_level", "full") or "full").lower()
    except Exception:  # noqa: BLE001
        return "full"


def ast_safe_scan(code: str, context_vars: set[str] | None = None, strict: bool = True) -> dict:
    """AST 安全扫描（仅 safe 模式调用；full 模式由 py_compile 保证语法正确）。

    strict=True（safe 模式）：只允许数学计算/简单控制流 + 白名单函数；
    strict=False（full 模式）：仅做基础检查（非空/长度），语法由 py_compile 兜底。

    Returns: {"ok": bool, "reason": str}
    """
    if not code or len(code) > _MAX_CODE_LEN:
        return {"ok": False, "reason": "代码为空或超长"}
    if not strict:
        try:
            ast.parse(code)  # 语法正确性（编译阶段 py_compile 还会再验一次）
            return {"ok": True}
        except SyntaxError as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"语法错误: {exc}"}
    low = code.lower()
    for d in _DANGEROUS_SUBSTR:
        if d in low:
            return {"ok": False, "reason": f"含危险片段 '{d.strip()}'"}
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"语法错误: {exc}"}

    locals_: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    locals_.add(t.id)
        elif isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(node.target, ast.Name):
            locals_.add(node.target.id)
        elif isinstance(node, ast.arguments):
            for a in list(node.args) + list(node.kwonlyargs):
                locals_.add(a.arg)
        elif isinstance(node, ast.arg):
            locals_.add(node.arg)

    allowed = locals_ | SAFE_MODULES | SAFE_BUILTINS | {"_"} | (context_vars or set())
    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_TYPES):
            return {"ok": False, "reason": f"禁止语法 {type(node).__name__}"}
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in allowed:
                return {"ok": False, "reason": f"使用了未授权名称 '{node.id}'"}
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                if f.id not in SAFE_CALLS:
                    return {"ok": False, "reason": f"禁止调用 {f.id}()"}
            elif isinstance(f, ast.Attribute):
                if f.attr.startswith("__"):
                    return {"ok": False, "reason": f"禁止 dunder 访问 {f.attr}"}
            else:
                return {"ok": False, "reason": "非法调用目标"}
    return {"ok": True}


def _extract_names(text: str) -> set[str]:
    """从锚点块文本提取出现的变量名（作为 new_code 允许引用的上下文变量）。"""
    names: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:  # noqa: BLE001
        import re
        for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", text):
            names.add(m.group(0))
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
    return names


def _read_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"history": [], "stats": {"applied": 0, "rejected": 0}}


def _save_state(st: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[code_writer] 状态保存失败: {exc}")


def _record(status: str, proposal: dict, note: str, backup: str = "") -> dict:
    """记录一条生成式进化历史，返回更新后的 state（供 _save_state 写回）。"""
    st = _read_state()
    st.setdefault("history", []).append({
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": status, "file": proposal.get("file", ""),
        "target": proposal.get("target", ""), "note": note, "backup": backup,
    })
    st["history"] = st["history"][-200:]
    st["stats"]["applied"] = int(st.get("stats", {}).get("applied", 0)) + (1 if status == "applied" else 0)
    st["stats"]["rejected"] = int(st.get("stats", {}).get("rejected", 0)) + (0 if status == "applied" else 1)
    return st


# ── 唯一宪法（用户唯一限制）：提高每日推荐股票之后上涨概率 ──────
_GOAL_KEYWORDS = ("上涨", "概率", "命中", "收益", "涨", "hit", "up", "t1", "t5",
                  "wave", "主升浪", "大涨", "precision", "accuracy", "return", "prob")


def _goal_check(proposal: dict) -> tuple[bool, str]:
    """唯一限制校验：提案必须指向"提高每日推荐股票之后上涨概率"。

    宽松判定：reason/evidence/target 需非空且含目标相关词（进化依据来自次日/主升浪验证）。
    这**不是安全限制**，而是用户指定的唯一宪法约束。
    """
    txt = f"{proposal.get('reason', '')} {proposal.get('evidence', '')} {proposal.get('target', '')}"
    if not txt.strip():
        return False, "提案必须说明如何提高每日推荐上涨概率（唯一宪法）"
    if any(k in txt.lower() for k in _GOAL_KEYWORDS):
        return True, ""
    return False, "提案未指向唯一目标（提高每日推荐上涨概率），拒绝"


def _read_json_file(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _current_perf(recent_days: int = 20) -> dict:
    """当前每日推荐综合绩效（D+1 买入口径，最近 N 个交易日，monitor_state）。

    用户核心诉求"推荐必须涨、最好主升浪"→ 综合目标分：
      composite = t1_hit × (1 - w) + wave_hit × w
    其中 t1_hit=次日上涨命中率（必须项），wave_hit=主升浪（T+5）命中率（加分项），
    w = daily_target_wave_w（主升浪权重，默认 0.4，可进化）。

    Returns: {"samples", "t1_hit", "wave_hit", "composite", ...}
      - 样本不足/无数据时 t1_hit/composite 为 None
    """
    try:
        import datetime as _dt
        state = _read_json_file(MONITOR_STATE_FILE, {"hits": []})
        hits = state.get("hits", [])
        cutoff = (_dt.date.today() - _dt.timedelta(days=recent_days)).strftime("%Y%m%d")
        recent = [h for h in hits if str(h.get("date", ""))[:8] >= cutoff]
        done1 = [h for h in recent if h.get("t1_ret") is not None]
        done5 = [h for h in recent if h.get("t5_ret") is not None]
        if not done1:
            return {"samples": 0, "t1_hit": None, "wave_hit": None, "composite": None}
        try:
            from app.agents import evolution_config
            t1_thr = float(evolution_config.get_param("t1_hit_threshold", 0.0) or 0.0)
            wave_thr = float(evolution_config.get_param("wave_hit_threshold", 0.05) or 0.05)
            w = float(evolution_config.get_param("daily_target_wave_w", 0.4) or 0.4)
        except Exception:  # noqa: BLE001
            t1_thr, wave_thr, w = 0.0, 0.05, 0.4
        w = max(0.0, min(1.0, w))
        t1_hit = round(sum(1 for h in done1 if float(h.get("t1_ret", 0) or 0) > t1_thr) / len(done1), 4)
        wave_hit = (round(sum(1 for h in done5 if float(h.get("t5_ret", 0) or 0) > wave_thr) / len(done5), 4)
                    if done5 else None)
        composite = round(t1_hit * (1 - w) + (wave_hit if wave_hit is not None else 0.0) * w, 4)
        return {"samples": len(done1), "t1_hit": t1_hit, "wave_hit": wave_hit,
                "composite": composite, "t1_thr": t1_thr, "wave_thr": wave_thr, "wave_w": w}
    except Exception:  # noqa: BLE001
        return {"samples": 0, "t1_hit": None, "wave_hit": None, "composite": None}


def _current_t1_hit(recent_days: int = 20) -> float | None:
    """当前每日推荐 T+1 命中率（D+1 买入口径，最近 N 个交易日，monitor_state）。

    供"效果回滚门"评估：进化补丁是否真提高了命中率（连续下降则自动回滚）。
    兼容旧调用：仅返回 T+1 命中率（综合判定走 _current_perf）。
    """
    return _current_perf(recent_days).get("t1_hit")


def _rollback_by_backup(reg: dict) -> dict:
    """按版本化备份恢复被进化改坏的文件（效果回滚门）。"""
    bak = reg.get("backup", "")
    file_rel = reg.get("file", "")
    if not bak or not os.path.exists(bak) or not file_rel:
        return {"ok": False, "message": "缺少备份/文件"}
    src = os.path.join(PROJECT_ROOT, file_rel)
    try:
        shutil.copy2(bak, src)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"恢复失败: {exc}"}
    try:
        self_improver._audit({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "param": f"code_rollback_effect:{reg.get('target')}",
            "old": "<evolved>", "new": "<restored>",
            "reason": "效果回滚门：命中率连续下降自动回滚",
            "evidence": "", "backup": bak, "action": "code_writer_rollback",
            "status": "rolled_back", "file": file_rel, "tier": 2,
        })
    except Exception:  # noqa: BLE001
        pass
    logger.warning(f"[code_writer] 效果回滚门：回滚 {reg.get('target')}（命中率连续下降）")
    return {"ok": True, "target": reg.get("target"), "restored": src}


def check_regression(limit_low: int = 3, drop: float = 0.15) -> dict:
    """效果回滚门（plans/19）：对比当前综合目标（T+1 次日大涨 + T+5 主升浪）与
    每个进化补丁的基线，连续 limit_low 期综合分下降超过 drop → 自动回滚该补丁
    （恢复 .bak + 写待重启标记）。

    用户核心诉求"推荐必须涨、最好主升浪"→ 回滚判定用综合目标分（T+1 与主升浪加权），
    不再只看 T+1，避免"T+1 微涨但主升浪大降"的坏补丁漏网。

    Returns: {"current", "current_t1", "tracked", "rolled_back", "message"}
    """
    st = _read_state()
    regs = st.get("regression", [])
    perf = _current_perf()
    cur = perf.get("composite")
    if not regs:
        return {"current": cur, "current_t1": perf.get("t1_hit"), "tracked": 0,
                "rolled_back": [], "message": "无待评估的进化补丁"}
    changed = False
    rolled: list[dict] = []
    for r in regs:
        if r.get("rolled_back"):
            continue
        # 兼容旧记录：baseline_composite 优先，缺省回退 baseline_t1（仅 T+1）
        base = r.get("baseline_composite", r.get("baseline_t1"))
        if base is None or cur is None:
            continue
        if cur < base - drop:
            r["low_count"] = r.get("low_count", 0) + 1
            changed = True
            if r["low_count"] >= limit_low:
                res = _rollback_by_backup(r)
                if res.get("ok"):
                    r["rolled_back"] = True
                    r["rolled_back_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    rolled.append({"target": r.get("target"), "file": r.get("file"),
                                   "baseline_composite": base, "current": cur,
                                   "baseline_t1": r.get("baseline_t1"),
                                   "current_t1": perf.get("t1_hit"),
                                   "current_wave": perf.get("wave_hit")})
                    try:
                        self_improver._write_restart_flag(f"code_rollback:{r.get('target')}",
                                                          "restored", r.get("backup", ""))
                    except Exception:  # noqa: BLE001
                        pass
        else:
            if r.get("low_count"):
                r["low_count"] = 0
                changed = True
    if changed:
        _save_state(st)
    return {"current": cur, "current_t1": perf.get("t1_hit"), "tracked": len(regs),
            "rolled_back": rolled,
            "message": f"自动回滚 {len(rolled)} 个效果下降补丁" if rolled else "无效果下降补丁"}


def _find_anchor(content: str, anchor_old: str) -> str | None:
    """定位 anchor_old 在 content 中的唯一匹配（精确优先；失败时按行去首尾空白归一化匹配）。

    返回文件中实际匹配的文本（用于精确替换）；不唯一/不存在返回 None。
    这是"锚点容错"：LLM 生成的 anchor_old 若缩进/空白有细微差异，仍能正确定位，
    避免"没有预置锚点就不能改"的困扰（full 模式本就不需要预置锚点）。
    """
    if not anchor_old or not content:
        return None
    if content.count(anchor_old) == 1:
        return anchor_old
    # 行级归一化：每行去首尾空白后比较（容忍缩进/空白差异）
    alines = [ln.strip() for ln in anchor_old.splitlines() if ln.strip()]
    if not alines:
        return None
    clines = content.splitlines()
    n, m = len(clines), len(alines)
    if n < m:
        return None
    hits: list[int] = []
    for i in range(n - m + 1):
        ok = True
        for j in range(m):
            if clines[i + j].strip() != alines[j]:
                ok = False
                break
        if ok:
            hits.append(i)
    if len(hits) != 1:
        return None
    return "\n".join(clines[hits[0]:hits[0] + m])


def _find_function(content: str, name: str) -> str | None:
    """用 AST 定位 name 函数/类方法的源码块（含 def/装饰器/函数体），返回文件中的实际文本。

    这是"函数级编辑"（ZOO CODE 式，**无需锚点字符串**）：LLM 只需给出函数名 + 新函数代码，
    系统自动定位整个函数体并替换，比"锚点字符串匹配"可靠得多。
    """
    if not name or not content:
        return None
    try:
        import ast
        tree = ast.parse(content)
    except Exception:  # noqa: BLE001
        return None
    lines = content.splitlines(keepends=True)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            start = node.lineno - 1
            end = getattr(node, "end_lineno", None)
            if end is None:
                return None
            decos = getattr(node, "decorator_list", None) or []
            if decos:
                start = min(d.lineno for d in decos) - 1
            return "".join(lines[start:end])
    return None


def _check_function_signature(old_block: str, new_code: str) -> dict:
    """校验 function 模式替换前后**函数签名兼容**（BugFix：进化大脑改签名破坏调用方）。

    进化大脑曾把 classify_current(df, end_idx, ts_code) 改成 classify_current(df, **kwargs)，
    py_compile 语法校验通过但签名不兼容 → daily_scan 用 3 参调用直接抛
    "takes 1 positional argument but 3 were given"，全市场扫描全线失败。
    此处对比新旧函数的**参数个数与参数名**，不兼容则拒绝（保护调用方）。

    Returns: {"ok": bool, "reason": str}
    """
    if not old_block or not new_code:
        return {"ok": True, "reason": ""}
    import ast

    def _sig(code: str):
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = node.args
                    positional = list(args.posonlyargs) + list(args.args)
                    return {
                        "pos_n": len(positional),
                        "names": [a.arg for a in positional],
                        "kwonly": [a.arg for a in args.kwonlyargs],
                        "has_vararg": args.vararg is not None,
                        "has_kwarg": args.kwarg is not None,
                    }
        except Exception:  # noqa: BLE001
            return None
        return None

    old_sig = _sig(old_block)
    new_sig = _sig(new_code)
    if old_sig is None or new_sig is None:
        return {"ok": True, "reason": "签名解析失败，不拦截"}
    # 位置参数个数必须一致（改签名=破坏调用方，禁止）
    if old_sig["pos_n"] != new_sig["pos_n"]:
        return {"ok": False,
                "reason": f"函数签名不兼容：位置参数数 {old_sig['pos_n']} → {new_sig['pos_n']}，"
                          f"禁止改签名（会破坏调用方），请保持原签名"}
    # 位置参数名必须一致（可选参数顺序/命名变更同样破坏调用方）
    if old_sig["names"] != new_sig["names"]:
        return {"ok": False,
                "reason": f"函数签名不兼容：参数名 {old_sig['names']} → {new_sig['names']}，"
                          f"禁止改签名（会破坏调用方），请保持原签名"}
    # 新函数不得把 *args/**kwargs 吞掉明确参数（规避签名绕过）
    if new_sig["has_vararg"] and not old_sig["has_vararg"]:
        return {"ok": False, "reason": "函数签名不兼容：新增 *args 会吞掉明确参数，请保持原签名"}
    return {"ok": True, "reason": ""}


def apply_code_patch(proposal: dict) -> dict:
    """应用生成式代码补丁（进化大脑"自己写代码"）。

    proposal 支持两种模式：
      - anchor 替换：{file, anchor_old, new_code, reason, evidence, target}
      - 整文件重写：{file, full_content, reason, evidence, target}

    流程（full 模式，无安全限制）：
      唯一宪法校验 → 版本化备份 → 应用 → py_compile → 失败自动回滚 → 待重启 → 审计。
    safe 模式额外做 AST 白名单 + 保护区拦截。
    """
    file_rel = str(proposal.get("file", "") or "").replace("\\", "/").lstrip("/")
    level = _security_level()
    full_content = proposal.get("full_content")
    anchor_old = str(proposal.get("anchor_old", "") or "")
    new_code = str(proposal.get("new_code", "") or "")

    if not file_rel:
        return {"ok": False, "action": "reject", "message": "缺 file"}
    # 权限判定（全域可改 + 守护文件拒绝）——用户决策：介入全部，只守住度量
    ok_perm, why_perm = is_editable(file_rel)
    if not ok_perm:
        _save_state(_record("rejected", proposal, f"权限拒绝: {why_perm}"))
        return {"ok": False, "action": "reject", "message": f"权限拒绝: {why_perm}"}
    # 函数级度量核心锁定（命中判定/案例分侧不可替换）
    _fn = str(proposal.get("function", "") or "")
    if _fn and (file_rel, _fn) in METRIC_CORE_FUNCS:
        _save_state(_record("rejected", proposal, f"度量核心函数不可替换: {file_rel}:{_fn}"))
        return {"ok": False, "action": "reject",
                "message": f"拒绝：{file_rel}:{_fn} 是命中/度量判定核心（唯一原则守护），不可替换"}
    if level == "safe" and self_improver._is_protected(file_rel):
        return {"ok": False, "action": "reject", "message": f"{file_rel} 在进化保护区（safe 模式）"}

    # 唯一宪法：提高每日推荐上涨概率（D+1 + 主升浪）
    ok, reason = _goal_check(proposal)
    if not ok:
        _save_state(_record("rejected", proposal, f"唯一宪法拒绝: {reason}"))
        return {"ok": False, "action": "reject", "message": f"唯一宪法拒绝: {reason}"}

    abs_path = os.path.join(PROJECT_ROOT, file_rel)
    exists = os.path.exists(abs_path)
    create = bool(proposal.get("create_file")) or not exists
    if not exists and not create:
        _save_state(_record("rejected", proposal, "目标文件不存在"))
        return {"ok": False, "action": "reject", "message": f"目标文件不存在 {file_rel}"}
    if create:
        content = ""
    else:
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "action": "fail", "message": f"读取 {file_rel} 失败: {exc}"}

    # 模式：新建文件 / 整文件重写 / 函数级替换（ZOO CODE 式）/ 锚点替换
    func = str(proposal.get("function", "") or "")
    if create:
        new_content = str(full_content or new_code or "")
        if not new_content.strip():
            return {"ok": False, "action": "reject", "message": "新建文件需提供 full_content"}
        mode_note = "新建文件"
    elif full_content:
        new_content = str(full_content)
        mode_note = "整文件重写"
    elif func:
        if not new_code:
            return {"ok": False, "action": "reject", "message": "函数模式缺 new_code"}
        block = _find_function(content, func)
        if block is None:
            _save_state(_record("rejected", proposal, f"函数 {func} 不存在或解析失败"))
            return {"ok": False, "action": "reject",
                    "message": f"函数 {func} 不存在或源码解析失败，无法定位"}
        # BugFix：签名一致性校验（进化大脑改签名会破坏调用方，如 classify_current 3参→**kwargs）
        sig = _check_function_signature(block, new_code)
        if not sig["ok"]:
            _save_state(_record("rejected", proposal, f"函数签名不兼容拒绝: {sig['reason']}"))
            return {"ok": False, "action": "reject", "message": f"函数 {func} {sig['reason']}"}
        new_content = content.replace(block, new_code, 1)
        mode_note = f"函数替换:{func}"
    else:
        if not anchor_old or not new_code:
            return {"ok": False, "action": "reject", "message": "缺 anchor_old/new_code 或 full_content"}
        # 锚点容错：精确唯一优先，失败时行级归一化找唯一匹配（容忍 LLM 缩进/空白差异）
        actual_anchor = _find_anchor(content, anchor_old)
        if actual_anchor is None:
            _save_state(_record("rejected", proposal, "锚点不唯一/不存在（精确与行级归一化均失败）"))
            return {"ok": False, "action": "reject",
                    "message": "锚点不唯一/不存在（已尝试精确与行级归一化匹配），拒绝自动改代码"}
        new_content = content.replace(actual_anchor, new_code, 1)
        mode_note = "锚点替换"

    # safe 模式：AST 白名单扫描（full 模式由 py_compile 兜底语法）
    if level == "safe":
        scan = ast_safe_scan(new_content, context_vars=_extract_names(anchor_old), strict=True)
        if not scan["ok"]:
            _save_state(_record("rejected", proposal, f"安全扫描拒绝: {scan['reason']}"))
            return {"ok": False, "action": "reject", "message": f"代码安全扫描拒绝: {scan['reason']}"}
    else:
        scan = ast_safe_scan(new_content, strict=False)
        if not scan["ok"]:
            _save_state(_record("rejected", proposal, f"语法错误: {scan['reason']}"))
            return {"ok": False, "action": "reject", "message": f"生成代码语法错误: {scan['reason']}"}

    # 版本化备份（新建文件无备份，失败时直接删除）
    bak = ""
    if not create:
        bak = self_improver._backup_versions(abs_path)
        if not bak:
            return {"ok": False, "action": "fail", "message": f"备份 {file_rel} 失败"}

    # 应用（新建文件先建目录）
    if create:
        try:
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "action": "fail", "message": f"创建目录失败: {exc}"}
    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as exc:  # noqa: BLE001
        if bak:
            shutil.copy2(bak, abs_path)
        return {"ok": False, "action": "fail", "message": f"写补丁失败（已恢复）: {exc}"}

    # 一级静态验证（失败恢复备份；新建文件则删除）
    if not self_improver._py_compile(file_rel):
        if bak:
            shutil.copy2(bak, abs_path)
            _save_state(_record("rejected", proposal, "编译验证失败已回滚"))
            return {"ok": False, "action": "fail", "message": f"编译验证失败，已恢复 {file_rel}"}
        try:
            os.remove(abs_path)
        except Exception:  # noqa: BLE001
            pass
        _save_state(_record("rejected", proposal, "编译验证失败（新文件已删除）"))
        return {"ok": False, "action": "fail", "message": f"编译验证失败，新文件已删除 {file_rel}"}

    # 审计 + 待重启标记（Tier2/3 代码改动由独立进程重启生效）
    target = str(proposal.get("target", "") or "") or file_rel
    self_improver._audit({
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "param": f"code:{target}", "old": anchor_old or "<full_file>",
        "new": new_code or "<full_content>",
        "reason": proposal.get("reason", ""), "evidence": proposal.get("evidence", ""),
        "backup": bak, "action": "code_writer", "status": "ok",
        "file": file_rel, "tier": 2, "mode": mode_note,
    })
    self_improver._write_restart_flag(f"code:{target}", "code_patch", bak)
    _save_state(_record("applied", proposal, f"已应用（{mode_note}），待重启生效（{file_rel}）", bak))
    # 效果回滚门（plans/19）：记录综合目标基线（T+1 次日大涨 + T+5 主升浪），供每日评估
    try:
        st = _read_state()
        _p = _current_perf()
        st.setdefault("regression", []).append({
            "target": target, "file": file_rel, "backup": bak,
            "baseline_composite": _p.get("composite"),
            "baseline_t1": _p.get("t1_hit"),
            "baseline_wave": _p.get("wave_hit"),
            "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "low_count": 0, "rolled_back": False,
        })
        st["regression"] = st["regression"][-30:]
        _save_state(st)
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"[code_writer] 生成式代码补丁应用({level}): {file_rel} {target}（{mode_note}，待独立重启生效）")
    # plans/21：改动落地 → 知识库台账标记生效（供"改后是否达预期"追踪与结论回填）
    _kb_mark_applied({**proposal, "file": file_rel}, bak)
    return {"ok": True, "action": "code_writer", "file": file_rel, "target": target,
            "backup": bak, "restart_pending": True, "mode": mode_note,
            "backtest_affected": file_rel in BACKTEST_FILES}


# ── 权限域（用户决策：进化大脑介入全部；唯一原则=提高 D+1 与主升浪准确率）──
# 可改：**全部业务代码** —— app/**、scripts/**、frontend/src/**，并且**可新建文件/模块**。
# 不可改（唯一原则的守护：若可改，"提升"就能靠改口径伪造，原则自毁）：
#   ① 守护与留痕本体：evolution_guard / evolution_events
#   ② 参数与宪法读写入口：evolution_config（main_metric/thresholds/protected_files 由守护哈希锁定）
#   ③ 进化执行链本体：self_improver / evolution_agent / evolution_summarizer
#   ④ 度量验证脚本：scripts/verify_metrics_guard.py
PERMISSION_LOCKED = [
    "app/agents/evolution_guard.py",
    "app/agents/evolution_events.py",
    "app/agents/evolution_config.py",
    "app/agents/evolution_agent.py",
    "app/agents/self_improver.py",
    "app/agents/evolution_summarizer.py",
    "scripts/verify_metrics_guard.py",
]

# 函数级锁定：文件整体可改，但这些"命中判定 / 案例分侧"函数不可替换
# （否则可以把"没涨"判成"涨"，命中率凭空上升 —— 同样是伪造度量）
METRIC_CORE_FUNCS = {
    ("app/agents/daily_verify.py", "_calc_multi"),
    ("app/kb/kb_ingest.py", "classify_side"),
}

# 可改根目录（相对 backend 工程根；前端源码同属可改域）
EDITABLE_ROOTS = ("app/", "scripts/", "frontend/src/")


def is_editable(file_rel: str) -> tuple[bool, str]:
    """权限判定：返回 (可改?, 拒绝原因)。新建文件（路径不存在）按同一规则判定。"""
    f = str(file_rel or "").replace("\\", "/").lstrip("/")
    if not f:
        return False, "缺 file"
    if not f.startswith(EDITABLE_ROOTS):
        return False, f"{f} 不在可改域（仅 {', '.join(EDITABLE_ROOTS)}）"
    if f in PERMISSION_LOCKED:
        return False, f"{f} 是唯一原则的守护文件（度量/守护/执行链本体），不可介入"
    return True, ""


def evolvable_files() -> list[str]:
    """全域可改文件清单（动态扫描 app/ 与 scripts/ 下 .py，排除守护文件）。

    用户决策：进化大脑可介入全部，故不再用固定白名单；此处只用于提示与看板展示。
    """
    out: list[str] = []
    for sub in ("app", "scripts"):
        base = os.path.join(PROJECT_ROOT, sub)
        for root, _dirs, files in os.walk(base):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(root, fn), PROJECT_ROOT).replace("\\", "/")
                if is_editable(rel)[0]:
                    out.append(rel)
    return sorted(out)


# 兼容既有引用（system_map/前端看板读它）：导入时快照一次；运行时请用 evolvable_files()
EVOLVABLE_FILES = evolvable_files()

# 回测逻辑相关文件（plans/19）：改动这些文件后应安排一次回测验证（进化大脑"控制回测"）
BACKTEST_FILES = {
    "app/backtest/runner.py", "app/backtest/daily_scan.py", "app/backtest/recommender.py",
    "app/backtest/outcome_tracker.py", "app/backtest/pattern_miner.py",
    "app/backtest/feature_extractor.py", "app/backtest/threshold_analyzer.py",
    "app/backtest/vector_store.py", "app/backtest/surge_scanner.py",
    "app/backtest/form_classifier.py", "app/backtest/rule_miner.py",
    "app/backtest/rule_verifier.py", "app/backtest/entry_analyzer.py",
    "app/backtest/risk_filter.py", "app/backtest/loader.py",
    "app/backtest/extra_feature_loader.py", "app/backtest/monitor.py",
}

_CODE_SYSTEM_FULL = """你是量化系统进化大脑的"代码写手"。唯一的宪法（唯一限制）：
**必须提高 D+1 命中率与主升浪命中率**（次日大涨 = D+1；后续主升浪 = T+5/波段）。
权限（用户决策）：你可以介入**全部业务代码**（app/**、scripts/**、frontend/src/**），
并且可以**新建文件/模块**（mode=new_file）。唯一不可动的是"度量本身"（命中判定 / 守护 / 执行链本体）
—— 改了它就能伪造提升，唯一原则也就无法被证明（系统会直接拒绝这类提案）。
**重要原则（用户决策）**：回测历史有重要研究价值，**不要通过"降低回测产物权重 / 降相似度权重 / 调弱负向抑制"来妥协**——那等于放弃回测成果。
当实盘命中低于回测时，优先**改进回测逻辑本身**（结局分流/特征拟合/模式库/条件表/归一化）及**回测→每日推荐的传导一致性**（daily_scan 加载 probability_table/condition_table/normalizer 口径、特征同源），让更好的回测真正服务于每日推荐。
基于系统近期表现（次日命中率/主升浪命中率/失效归因/**环节归因**/改进方向/**回测健康诊断**），修改回测或每日推荐逻辑。
若"环节归因"显示某个环节（含回测侧：backtest_outcome 结局分流/backtest_feature 特征拟合/backtest_pattern 模式库/backtest_condition 条件表/backtest_normalizer 归一化/backtest_product 产物脱节，以及每日推荐侧：neg_exclude 负向抑制/pattern_match 相似度/condition_score 条件概率/agent_refine 精筛等）问题最大，
**优先优化该环节对应的文件或参数**（环节归因的 optimize 给出了方向）。
若"回测健康诊断"显示 backtest_decouple（回测准但实盘低=产物脱节）或 backtest_failure（回测逻辑失效），
**必须优先处理回测侧问题**：脱节→检查 daily_scan 加载 probability_table/condition_table/normalizer 口径是否与回测一致；失效→检查 runner/outcome_tracker/pattern_miner/feature_extractor/threshold_analyzer 逻辑。
可以修改任何逻辑与参数，也可以新建文件/模块；系统只拒绝"改度量/改守护"这一类提案。
输出 JSON 对象（不要任何其他文字）：
{{
  "file": "要修改或新建的文件路径（全域可写：app/**、scripts/**、frontend/src/**；mode=param 时可为空字符串）",
  "mode": "function（推荐，改一个函数/方法，系统自动定位函数体，无需锚点字符串）或 anchor（改一个代码块）或 full（整个文件重写）或 new_file（新建文件/模块，需给 full_content）或 param（只调 evolution_config 可进化参数）",
  "function": "mode=function 时填：要替换的函数/方法名（如 classify_current）",
  "new_code": "mode=function/anchor 时填：替换后的代码（function 模式必须是完整的新函数，含 def 行）",
  "anchor_old": "mode=anchor 时填：文件中要替换的代码片段（系统会容错定位唯一位置，近似即可，含原缩进更稳）",
  "full_content": "mode=full 时填：整个文件的新内容（谨慎，必须完整且语法正确）",
  "param": "mode=param 时填：参数名（evolution_config 可进化参数，如 daily_lambda_exclude/daily_top_k/hit_threshold 等）",
  "value": "mode=param 时填：新参数值（如 0.65）",
  "reason": "改动如何提高每日推荐上涨概率（≤80字）",
  "evidence": "数据依据（引用次日命中率/主升浪命中率/归因类型，≤100字）"
}}
说明：改单个函数/方法优先用 function 模式（无需锚点字符串，系统用 AST 自动定位函数体替换，像 ZOO CODE 一样）；调可热生效参数（daily_lambda_exclude 等 tier1）用 param 模式（无需改代码）；只有跨多处大改才用 full 整文件重写。
注意：优先用 anchor（最小改动），只有确需大改才用 full。"""

# safe 模式：预置可安全替换的代码锚点（LLM 只生成 new_code）
CODE_ANCHORS: dict[str, dict] = {
    "daily_scan_final_prob": {
        "file": "app/backtest/daily_scan.py",
        "anchor_old": 'final_prob = prob * (1.0 - _evolve_param("daily_lambda_exclude", LAMBDA_EXCLUDE) * neg_sim)',
        "desc": "每日推荐'综合上涨概率'计算（条件概率经负向抑制），可替换为更优的概率融合公式",
        "context_vars": "prob(条件概率), pos_sim(正模式相似度), neg_sim(负模式相似度), code, df, extra, LAMBDA_EXCLUDE",
        "output": "final_prob（必须给 final_prob 赋值）",
    },
}


def _files_text(limit: int = 80) -> str:
    """可改范围说明（全域，不列全部文件以免撑爆 prompt）。"""
    files = evolvable_files()
    focus = [f for f in files if f.startswith(("app/backtest/", "app/agents/", "app/kb/"))]
    return (f"可改范围：**全部业务代码**（{', '.join(EDITABLE_ROOTS)}），并且**可新建文件/模块**"
            f"（mode=new_file：file 填新路径如 app/backtest/new_factor.py，full_content 填完整内容）。\n"
            f"当前可改 .py 共 {len(files)} 个，重点文件例举：\n"
            + "\n".join(f"- {f}" for f in focus[:limit]))


def _kb_exp_id(kind: str, target: str, old, new) -> str:
    """实验台账 id（确定性：同一改动重复提案/应用 → 同一 id，幂等可追溯）。"""
    import hashlib
    h = hashlib.sha1(f"{kind}|{target}|{old}|{str(new)[:400]}".encode("utf-8")).hexdigest()[:10]
    return f"experiment:{time.strftime('%Y%m%d')}:{target}:{h}"


def _kb_record_proposal(proposal: dict) -> dict:
    """把提案写入私有域知识库（plans/21 §七：提案 → 实验台账 + 改动因果链）。

    返回 {"exp_id":..., "chg_id":...}（失败返回空 dict，绝不影响提案主流程）。
    """
    try:
        from app.kb import kb_ingest
        p = proposal or {}
        is_param = str(p.get("mode") or "") == "param"
        target = str(p.get("param") or p.get("file") or p.get("target") or "unknown")
        old = p.get("old") if is_param else (p.get("anchor_old") or "<full_file>")
        new = p.get("value") if is_param else (p.get("new_code") or p.get("full_content") or "")
        eid = _kb_exp_id("param" if is_param else "code", target, old, new)
        cid = f"change:{time.strftime('%Y%m%d')}:{target}:{eid[-10:]}"
        kb_ingest.record_experiment(
            eid,
            kind="param" if is_param else "code",
            target={"param": p.get("param"), "file": p.get("file"), "mode": p.get("mode")},
            change={"old": old, "new": str(new)[:4000],
                    "scope": p.get("function") or p.get("mode") or "anchor"},
            hypothesis=str(p.get("reason") or "")[:200],
            evidence_in=str(p.get("evidence") or "")[:200],
            status="proposed",
        )
        kb_ingest.record_change(
            cid, why=str(p.get("reason") or "进化大脑提案")[:200],
            what={"file": p.get("file"), "function": p.get("function"),
                  "scope": p.get("mode") or "anchor", "diff_summary": str(new)[:300]},
            who="code_writer", expectation=str(p.get("evidence") or "")[:200],
        )
        return {"exp_id": eid, "chg_id": cid}
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[code_writer] 知识库记录提案跳过: {exc}")
        return {}


def _kb_mark_applied(proposal: dict, backup: str = "") -> None:
    """改动落地：更新实验台账/因果链状态（含备份与生效时间），供"改后是否达预期"追踪。"""
    try:
        from app.kb import kb_writer
        p = proposal or {}
        is_param = str(p.get("mode") or "") == "param"
        target = str(p.get("param") or p.get("file") or p.get("target") or "unknown")
        old = p.get("old") if is_param else (p.get("anchor_old") or "<full_file>")
        new = p.get("value") if is_param else (p.get("new_code") or p.get("full_content") or "")
        eid = _kb_exp_id("param" if is_param else "code", target, old, new)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        kb_writer.update_experiment(eid, {"status": "applied", "verdict": None,
                                          "verdict_reason": ""})
        cid = f"change:{time.strftime('%Y%m%d')}:{target}:{eid[-10:]}"
        kb_writer.update_change(cid, {
            "effective_at": ts,
            "what": {"file": p.get("file"), "function": p.get("function"),
                     "scope": p.get("mode") or "anchor", "backup": backup,
                     "diff_summary": str(new)[:300]},
        })
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[code_writer] 知识库标记生效跳过: {exc}")


def propose_code_evolution(summary: str = "", mode: str = "manual", anchor_key: str | None = None,
                           instruction: str = "") -> dict:
    """进化大脑"自己写代码"：基于近期表现生成代码改进提案。

    Args:
        summary: L0 摘要文本（进化依据：次日验证/主升浪/命中率）
        mode: manual → 存 pending_patches 待人工确认；auto → 直接应用（调用方须保证系统空闲）
        anchor_key: 仅 safe 模式使用（预置锚点）

    Returns: {"ok", "message", "proposal"|...}

    plans/21：① 生成前注入私有域知识库（历史同类改动结论 + 否证清单 + 高置信教训）；
             ② 生成后过"否证硬约束"（同类改动已被证明无效 ≥N 次 → 拒绝同向）；
             ③ 提案写入实验台账与改动因果链。
    """
    level = _security_level()
    if level == "safe":
        res = _propose_safe(summary=summary, mode=mode, anchor_key=anchor_key)
    else:
        res = _propose_full(summary=summary, mode=mode, instruction=instruction)
    # ② 否证硬约束（防重复无效改动）
    try:
        from app.kb import kb_context as _kctx
        p = (res or {}).get("proposal") or {}
        is_param = str(p.get("mode") or "") == "param"
        v = _kctx.veto_check(param=p.get("param") if is_param else None,
                            file=None if is_param else p.get("file"))
        if (res or {}).get("ok") and v.get("blocked"):
            logger.info(f"[code_writer] 提案被否证清单拒绝: {v.get('reason')}")
            return {**res, "ok": False, "message": v.get("reason"), "veto": v}
        if v.get("count"):
            res = {**(res or {}), "veto_note": v.get("reason") or ""}
    except Exception:  # noqa: BLE001
        pass
    # ③ 提案入库（实验台账 + 改动因果链）
    if (res or {}).get("ok") and (res or {}).get("proposal"):
        try:
            res["kb"] = _kb_record_proposal(res["proposal"])
        except Exception:  # noqa: BLE001
            pass
    return res


def _propose_param(result: dict, mode: str) -> dict:
    """param 模式：生成/应用 evolution_config 热生效参数提案（无需代码锚点/重启）。"""
    param = str(result.get("param", "") or "").strip()
    value = result.get("value")
    if not param or value is None:
        return {"ok": False, "message": "param 模式缺 param/value"}
    try:
        from app.agents import evolution_config
        meta = evolution_config.params_table().get(param)
    except Exception:  # noqa: BLE001
        meta = None
    if not meta:
        return {"ok": False, "message": f"参数 {param} 不在可进化参数表（evolution_config.params）"}
    try:
        mn, mx = meta.get("min"), meta.get("max")
        v = float(value)
        if mn is not None and v < float(mn):
            return {"ok": False, "message": f"{param} 需 ≥{mn}"}
        if mx is not None and v > float(mx):
            return {"ok": False, "message": f"{param} 需 ≤{mx}"}
    except (TypeError, ValueError):
        pass
    proposal = {
        "mode": "param", "param": param, "value": value,
        "reason": str(result.get("reason", "") or ""),
        "evidence": str(result.get("evidence", "") or ""),
        "target": f"param:{param}",
        "file": meta.get("file", ""),
    }
    if mode == "auto":
        res = apply_param_proposal(proposal)
        res["mode"] = "auto"
        return res
    st = _read_state()
    pid = len(st.get("pending_patches", [])) + 1
    st.setdefault("pending_patches", []).append({**proposal, "id": pid,
                                                 "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    st["pending_patches"] = st["pending_patches"][-20:]
    _save_state(st)
    logger.info(f"[code_writer] 参数提案已生成（{param}={value}，待确认）")
    return {"ok": True, "mode": "manual", "message": f"参数提案已生成（{param}={value}，待确认，热生效）",
            "proposal": {**proposal, "id": pid}}


def apply_param_proposal(proposal: dict) -> dict:
    """应用参数提案（tier=1 热生效参数直接 evolution_config.set_param，无需改代码/重启）。"""
    param = str(proposal.get("param", "") or "")
    value = proposal.get("value")
    if not param or value is None:
        return {"ok": False, "message": "缺 param/value"}
    try:
        from app.agents import evolution_config
        if not evolution_config.set_param(param, value):
            return {"ok": False, "message": f"参数写入失败 {param}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"参数写入异常: {exc}"}
    try:
        self_improver._audit({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "param": f"param:{param}", "old": "", "new": str(value),
            "reason": proposal.get("reason", ""), "evidence": proposal.get("evidence", ""),
            "backup": "", "action": "code_writer_param", "status": "ok",
            "file": proposal.get("file", ""), "tier": 1,
        })
    except Exception:  # noqa: BLE001
        pass
    _save_state(_record("applied", proposal, f"参数热生效 {param}={value}"))
    # plans/21：参数热生效 → 知识库台账标记生效
    _kb_mark_applied({**proposal, "mode": "param", "old": "",
                      "param": param, "value": value})
    logger.info(f"[code_writer] 参数提案应用: {param}={value}（热生效）")
    return {"ok": True, "action": "param", "param": param, "value": value,
            "hot_reload": True, "message": f"参数 {param} 已热生效为 {value}（无需重启）"}


def _propose_full(summary: str, mode: str, instruction: str = "") -> dict:
    """full 模式：LLM 自由生成（anchor/full/param），唯一宪法=提高每日推荐上涨概率。

    instruction: 用户自然语言修改指令（对话驱动时传入）。LLM 据此**自动定位**要改的
    函数/文件/参数，无需用户提供函数名或代码片段（ZOO CODE 式：你说意图，它找代码）。
    """
    try:
        # 系统图谱（plans/18）：当前回测与每日推荐的全部逻辑+参数，随进化动态更新
        try:
            from app.agents import system_map
            map_txt = system_map.build_map_txt(short=True)
        except Exception:  # noqa: BLE001
            map_txt = "（图谱生成失败）"
        # plans/19：回测健康诊断（进化大脑要考虑"是不是回测的问题"）
        try:
            from app.agents import backtest_ctl
            bt_diag = backtest_ctl.health_text()
        except Exception:  # noqa: BLE001
            bt_diag = ""
        # plans/21：私有域知识库综合分析（高置信教训 + 成功/失败对照 + 历史同类改动 + 否证清单）
        try:
            from app.kb import kb_context as _kctx
            kb_txt = _kctx.knowledge_txt(query=(instruction or (summary or ""))[:120])
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[code_writer] 知识库上下文跳过: {exc}")
            kb_txt = ""
        instr = (f"主人的修改指令（严格按此意图执行；自动定位要改的函数/文件/参数，"
                 f"无需主人提供函数名或代码片段）:\n{instruction}\n\n" if instruction else "")
        user = (instr +
                f"系统近期表现（进化依据）:\n{summary or '（暂无）'}\n\n"
                + (f"{kb_txt}\n\n" if kb_txt else "") +
                f"系统图谱（当前回测与每日推荐的全部逻辑与参数，据此定位要优化的环节）:\n{map_txt}\n\n"
                f"回测健康诊断（考虑是不是回测逻辑的问题）:\n{bt_diag or '（暂无）'}\n\n"
                f"可改范围（全域，可新建文件/模块）:\n{_files_text()}\n\n"
                f"请生成代码改进提案（JSON）。")
        result = deepseek_chat([{"role": "system", "content": _CODE_SYSTEM_FULL},
                                {"role": "user", "content": user}])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"LLM 生成代码失败: {exc}"}
    if not isinstance(result, dict):
        return {"ok": False, "message": "LLM 未返回有效提案"}
    rmode = str(result.get("mode", "anchor") or "anchor").lower()
    # ── param 模式：只调 evolution_config 热生效参数（无需代码锚点/重启）──
    if rmode == "param":
        return _propose_param(result, mode)
    if not result.get("file"):
        return {"ok": False, "message": "LLM 未返回有效提案（缺 file）"}

    file_rel = str(result.get("file", "")).strip().replace("\\", "/").lstrip("/")
    ok_perm, why_perm = is_editable(file_rel)
    if not ok_perm:
        return {"ok": False, "message": f"权限拒绝: {why_perm}"}
    _proj = os.path.join(PROJECT_ROOT, file_rel)
    _create = bool(result.get("new_file")) or rmode == "new_file" or not os.path.exists(_proj)
    proposal = {
        "file": file_rel,
        "reason": result.get("reason", ""), "evidence": result.get("evidence", ""),
        "target": f"code_evolve:{file_rel}",
        **({"create_file": True} if _create else {}),
    }
    if _create and not (result.get("full_content") or result.get("new_code")):
        return {"ok": False, "message": "新建文件模式需提供 full_content（完整文件内容）"}
    _fn_chk = str(result.get("function", "") or "")
    if _fn_chk and (file_rel, _fn_chk) in METRIC_CORE_FUNCS:
        return {"ok": False, "message": f"拒绝：{file_rel}:{_fn_chk} 是命中/度量判定核心，不可替换"}
    if rmode == "full" and result.get("full_content"):
        proposal["full_content"] = str(result["full_content"])
        proposal["target"] = f"code_evolve_full:{file_rel}"
    elif rmode == "function" and result.get("function"):
        proposal["function"] = str(result["function"]).strip()
        proposal["new_code"] = str(result.get("new_code", "") or "")
        proposal["target"] = f"code_evolve_func:{file_rel}:{proposal['function']}"
    else:
        proposal["anchor_old"] = str(result.get("anchor_old", "") or "")
        proposal["new_code"] = str(result.get("new_code", "") or "")
        proposal["target"] = f"code_evolve:{file_rel}"
    # 安全落地前先在本函数内做一次语法/目标预检（不写盘）
    ok, reason = _goal_check(proposal)
    if not ok:
        return {"ok": False, "message": f"唯一宪法拒绝: {reason}"}
    # 预检目标定位（避免应用阶段才发现）：full 跳过；function 用 AST 定位；anchor 用容错匹配
    abs_path = os.path.join(PROJECT_ROOT, file_rel)
    if not os.path.exists(abs_path) and not proposal.get("create_file"):
        return {"ok": False, "message": f"文件不存在 {file_rel}（新建请用 mode=new_file 并提供 full_content）"}
    if not proposal.get("full_content") and not proposal.get("create_file"):
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                c = f.read()
        except Exception:  # noqa: BLE001
            c = ""
        if proposal.get("function"):
            if _find_function(c, proposal["function"]) is None:
                return {"ok": False, "message": f"函数 {proposal['function']} 不存在或解析失败，请确认函数名"}
        elif _find_anchor(c, str(proposal.get("anchor_old", "") or "")) is None:
            return {"ok": False, "message": "锚点不唯一/不存在（已尝试精确与行级归一化匹配），请改为精确锚点"}

    if mode == "auto":
        res = apply_code_patch(proposal)
        res["mode"] = "auto"
        return res
    # manual → 存 pending 待人工确认
    st = _read_state()
    pid = len(st.get("pending_patches", [])) + 1
    st.setdefault("pending_patches", []).append({**proposal, "id": pid,
                                                 "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    st["pending_patches"] = st["pending_patches"][-20:]
    _save_state(st)
    logger.info(f"[code_writer] 生成式代码提案已生成（{file_rel}，待人工确认）")
    return {"ok": True, "mode": "manual", "message": "代码提案已生成（待人工确认）",
            "proposal": {**proposal, "id": pid}}


def _propose_safe(summary: str, mode: str, anchor_key: str | None) -> dict:
    """safe 模式：从预置锚点生成替换代码（保留 AST 白名单）。"""
    anchor_key = anchor_key or "daily_scan_final_prob"
    anchor = CODE_ANCHORS.get(anchor_key)
    if not anchor:
        return {"ok": False, "message": f"未知锚点 {anchor_key}"}
    try:
        with open(os.path.join(PROJECT_ROOT, anchor["file"]), "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"读取锚点文件失败: {exc}"}
    if anchor["anchor_old"] not in content:
        return {"ok": False, "message": "锚点当前代码与登记不一致（文件已漂移），跳过生成"}
    _sys = ("你是量化系统进化大脑的'代码写手'。唯一宪法：让每日推荐的股票之后上涨概率高。"
            "基于近期表现，为指定锚点生成替换代码 new_code。输出 JSON："
            '{"anchor_key": "...", "new_code": "...", "reason": "...", "evidence": "..."}')
    try:
        user = (f"目标锚点:\n{anchor['desc']}\n当前代码(anchor_old):\n{anchor['anchor_old']}\n"
                f"上下文变量: {anchor['context_vars']}\n输出要求: {anchor['output']}\n"
                f"系统近期表现:\n{summary or '（暂无）'}\n请生成 new_code。")
        result = deepseek_chat([{"role": "system", "content": _sys},
                                {"role": "user", "content": user}])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"LLM 生成代码失败: {exc}"}
    if not isinstance(result, dict) or result.get("anchor_key") != anchor_key or not result.get("new_code"):
        return {"ok": False, "message": "LLM 未返回有效代码提案"}
    new_code = str(result["new_code"]).strip()
    scan = ast_safe_scan(new_code, context_vars=_extract_names(anchor["anchor_old"]), strict=True)
    if not scan["ok"]:
        return {"ok": False, "message": f"生成代码未通过安全扫描: {scan['reason']}", "new_code": new_code}
    proposal = {
        "file": anchor["file"], "anchor_old": anchor["anchor_old"], "new_code": new_code,
        "reason": result.get("reason", ""), "evidence": result.get("evidence", ""),
        "target": f"code_anchor:{anchor_key}",
    }
    if mode == "auto":
        res = apply_code_patch(proposal)
        res["mode"] = "auto"
        return res
    st = _read_state()
    pid = len(st.get("pending_patches", [])) + 1
    st.setdefault("pending_patches", []).append({**proposal, "id": pid,
                                                 "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    st["pending_patches"] = st["pending_patches"][-20:]
    _save_state(st)
    logger.info(f"[code_writer] 生成式代码提案已生成（{anchor_key}，待人工确认）")
    return {"ok": True, "mode": "manual", "message": "代码提案已生成（待人工确认）",
            "proposal": {**proposal, "id": pid}}


def status() -> dict:
    """生成式代码进化状态（前端进化中心 getEvolveCodeStatus 调用）。

    组合：闲时检测（is_idle）+ 状态文件（state 含 pending_patches/history/stats）。
    前端 Evolution.vue 依赖 codeStatus.idle.{idle,busy} 与 codeStatus.state.{pending_patches,history}。
    """
    return {
        "idle": is_idle(),
        "state": _read_state(),
    }


def pending_patches() -> list[dict]:
    """待人工确认的生成式代码提案列表。"""
    return _read_state().get("pending_patches", [])


def apply_pending(patch_id: int) -> dict:
    """人工确认应用一条 pending 代码提案（走 apply_code_patch 完整工程护栏）。"""
    st = _read_state()
    patches = st.get("pending_patches", [])
    for i, p in enumerate(patches):
        if p.get("id") == patch_id:
            if p.get("mode") == "param":
                res = apply_param_proposal(
                    {k: p.get(k) for k in ("param", "value", "reason", "evidence", "file")})
            else:
                res = apply_code_patch(
                    {k: p.get(k) for k in ("file", "anchor_old", "new_code",
                                           "full_content", "reason", "evidence", "target")})
            if res.get("ok"):
                st["pending_patches"].pop(i)
                _save_state(st)
            return res
    return {"ok": False, "message": f"待确认提案 {patch_id} 不存在"}


def is_idle() -> dict:
    """闲时检测：无回测/每日扫描/Agent精筛/反思/数据采集在跑，才允许自动写代码。

    Returns: {"idle": bool, "busy": [来源]}
    """
    busy: list[str] = []
    try:
        from app.api import backtest as _bt, predictions as _pr
        if _bt._STATE.get("status") == "running":
            busy.append("backtest")
        if _pr._SCAN_STATE.get("status") == "running":
            busy.append("daily_scan")
        if _pr._AGENT_STATE.get("status") == "running":
            busy.append("agent_refine")
        if getattr(_pr, "_REFLECT_STATE", {}).get("status") == "running":
            busy.append("reflection")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[code_writer] 任务状态检查跳过: {exc}")
    try:
        from app.api import ws
        # 采集忙判定用"正在执行(pending/running)"而非"存在任何历史未完成记录(含 error)"：
        # error 残留(如部分股票某接口永久无数据)不应永久阻塞进化/回测/定时任务
        if int(ws.active_collection_tasks() or 0) > 0:
            busy.append("collection")
    except Exception:  # noqa: BLE001
        pass
    return {"idle": not busy, "busy": busy}


# ── 报错监控 → 修复 bug（用户新需求：进化大脑监控后台接口报错并修改 bug）──
_BUG_SYSTEM = """你是量化系统的进化大脑"bug 修复者"。核心使命：
监控后台接口/任务的报错日志（ERROR/WARNING），定位报错根因并**修复代码 bug**，
确保系统稳定运行，最终服务于"高概率上涨的每日推荐"。

输入：近期报错日志列表（含来源 source / 报错消息 / 重复次数）。
任务：分析每个报错，判断是否为**可修代码 bug**（涉及系统代码文件），若是则生成修复补丁。
规则：
- 可修改全部业务代码（app/**、scripts/**、frontend/src/**），也可新建文件；但**不可改度量/守护**
  （命中判定、evolution_guard/events、evolution_config 的宪法键）—— 那是"唯一原则"的证明基础。
- 优先用 function 模式（替换一个函数/方法，系统自动定位函数体，**严禁改变函数签名**）或 anchor 模式。
- 若报错是**外部环境问题**（网络/限流/数据缺失/模型加载 OOM 等，非代码 bug）→ 输出空修复（"fixable": false），
  不要硬改代码。
- 每次只修一个最关键的 bug（G3 最小改动），修复必须能消除报错且不引入新问题。
- 修复后系统会自动 py_compile 编译校验 + 函数签名一致性校验 + 版本化备份 + 效果回滚门，可回退。
输出 JSON 对象（不要任何其他文字）：
{{
  "fixable": true/false,
  "file": "要修改（或新建）的文件路径（fixable=true 时填；全域可写，不含度量/守护文件）",
  "mode": "function 或 anchor",
  "function": "mode=function 时填：要替换的函数/方法名",
  "new_code": "mode=function/anchor 时填：修复后的完整代码（function 模式必须含 def 行，签名与原函数一致）",
  "anchor_old": "mode=anchor 时填：要替换的代码片段",
  "reason": "修复的 bug 根因与改动（≤80字）",
  "evidence": "依据的报错日志（引用具体报错，≤100字）"
}}"""


def propose_bug_fix(hours: float = 24.0, mode: str = "auto", max_errors: int = 15) -> dict:
    """进化大脑监控后台报错并修复 bug（用户新需求）。

    流程：读取近期 ERROR/WARNING 报错 → LLM 定位根因 → 生成修复补丁
    （走 apply_code_patch 完整安全链：唯一宪法→签名校验→备份→py_compile→回滚门）。
    无报错/报错为外部环境问题 → noop（不硬改代码）。

    Args:
        hours: 回溯窗口（小时）
        mode: auto 直接应用 / manual 存待确认
        max_errors: 最多送分析的报错条数

    Returns: {"ok", "action", "message", "errors_n", ...}
    """
    try:
        from app.agents import evolution_events
        errors = evolution_events.recent_errors(hours=hours, limit=max_errors)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "action": "fail", "message": f"读取报错失败: {exc}", "errors_n": 0}
    if not errors:
        return {"ok": True, "action": "noop", "message": f"近 {hours:.0f}h 无 ERROR/WARNING 报错", "errors_n": 0}

    # 报错清单文本
    lines = []
    for i, e in enumerate(errors, 1):
        lines.append(
            f"{i}. [{e.get('level')}] 来源={e.get('source','')} 次数={e.get('count',1)} "
            f"时间={e.get('ts','')}\n   消息: {str(e.get('message',''))[:300]}"
        )
    errors_txt = "\n".join(lines)

    # 系统图谱（供 LLM 定位代码）
    try:
        from app.agents import system_map
        map_txt = system_map.build_map_txt(short=True)
    except Exception:  # noqa: BLE001
        map_txt = "（图谱生成失败）"

    user = (f"近期报错日志（近 {hours:.0f}h，按重复次数排序）:\n{errors_txt}\n\n"
            f"系统图谱（当前回测与每日推荐逻辑，据此定位报错对应代码）:\n{map_txt}\n\n"
            f"可改范围（全域；只改业务代码，不要改度量/守护）:\n{_files_text()}\n\n"
            f"请分析并输出修复提案（JSON）。")
    try:
        result = deepseek_chat([{"role": "system", "content": _BUG_SYSTEM},
                                {"role": "user", "content": user}])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "action": "fail", "message": f"LLM 分析报错失败: {exc}", "errors_n": len(errors)}

    # 不可修/外部环境问题
    if not isinstance(result, dict) or not result.get("fixable"):
        reason = result.get("reason", "") if isinstance(result, dict) else ""
        return {"ok": True, "action": "noop", "errors_n": len(errors),
                "message": f"检测到 {len(errors)} 条报错，判定为外部环境/不可修问题（{reason or '无修复提案'}）",
                "errors": errors[:max_errors]}

    proposal = {
        "file": str(result.get("file", "") or "").strip(),
        "reason": str(result.get("reason", "") or ""),
        "evidence": str(result.get("evidence", "") or ""),
        "target": f"code_bugfix:{str(result.get('file', '') or '').strip()}",
    }
    rmode = str(result.get("mode", "function") or "function").lower()
    if rmode == "function" and result.get("function"):
        proposal["function"] = str(result.get("function", "")).strip()
        proposal["new_code"] = str(result.get("new_code", "") or "")
        proposal["target"] = f"code_bugfix_func:{proposal['file']}:{proposal['function']}"
    else:
        proposal["anchor_old"] = str(result.get("anchor_old", "") or "")
        proposal["new_code"] = str(result.get("new_code", "") or "")
        proposal["target"] = f"code_bugfix:{proposal['file']}"

    if not proposal["file"] or proposal["file"] not in EVOLVABLE_FILES:
        return {"ok": True, "action": "noop", "errors_n": len(errors),
                "message": f"报错对应文件 {proposal['file']} 不在可修清单或缺失，跳过", "errors": errors[:max_errors]}

    if mode == "auto":
        res = apply_code_patch(proposal)
        res["mode"] = "auto"
        res["bugfix"] = True
        res["errors_n"] = len(errors)
        return res
    # manual → 存待确认
    st = _read_state()
    pid = len(st.get("pending_patches", [])) + 1
    st.setdefault("pending_patches", []).append({**proposal, "id": pid, "bugfix": True,
                                                 "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    st["pending_patches"] = st["pending_patches"][-20:]
    _save_state(st)
    logger.info(f"[code_writer] bug 修复提案已生成（{proposal['file']}，待确认）")
    return {"ok": True, "mode": "manual", "message": f"bug 修复提案已生成（{proposal['file']}，待确认）",
            "proposal": {**proposal, "id": pid}, "errors_n": len(errors)}


def rollback_last_code(nth: int = 1) -> dict:
    """回滚最近第 nth 次生成式代码补丁（恢复版本化备份 + 清待重启标记）。"""
    if nth < 1:
        nth = 1
    records = self_improver._load_audit()
    seen = 0
    for r in reversed(records):
        if r.get("action") == "code_writer" and r.get("status") == "ok" and r.get("backup"):
            seen += 1
            if seen != nth:
                continue
            bak = r.get("backup")
            if os.path.exists(bak):
                src = os.path.join(PROJECT_ROOT, r.get("file", "")) if r.get("file") else bak
                try:
                    shutil.copy2(bak, src)
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "message": f"恢复代码备份失败: {exc}"}
                r["status"] = "rolled_back"
                r["rolled_back_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self_improver._audit(r)
                self_improver.clear_restart_flag()
                logger.info(f"[code_writer] 回滚生成式代码补丁 {r.get('param')} -> {src}")
                return {"ok": True, "param": r.get("param"), "restored": src, "nth": nth}
    return {"ok": False, "message": f"无可回滚的第 {nth} 次生成式代码补丁"}


__all__ = ["ast_safe_scan", "apply_code_patch", "apply_param_proposal", "is_idle", "status",
           "rollback_last_code", "propose_code_evolution", "pending_patches", "apply_pending",
           "check_regression", "propose_bug_fix", "CODE_ANCHORS", "EVOLVABLE_FILES",
           "PERMISSION_LOCKED", "METRIC_CORE_FUNCS", "EDITABLE_ROOTS",
           "is_editable", "evolvable_files"]
