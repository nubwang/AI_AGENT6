"""门禁：`deepseek_chat()` 的**每个消费点**都必须先处置返回形状（2026-09-29 事故后新增）

## 为什么需要"门禁"，而不是"再修一次"

2026-09-29 线上事故（前端：`失败Agent 精筛失败: 'list' object has no attribute 'get'`）
的本质不是某个函数写错，而是**契约缺少机械保证**：

  · [`deepseek_chat()`](ai-quant-agent/backend/app/agents/llm_client.py:222) 返回类型由**模型**决定
    （`dict` 或 `list`，其 docstring 与 [`_extract_json()`](ai-quant-agent/backend/app/agents/llm_client.py:179)
    都写明了）；
  · 消费点靠"人记得判形状"防崩 ⇒ 只要**新加一个消费点忘了判**，就再崩一次，
    而且崩点往往在整条链路末端（L3 统一决策外层无 try）⇒ 代价是"整轮精筛 failed、榜单不落盘"。

只修 6 个已知崩点 = "这次记得"；下次照样忘。所以把它变成**可机械检查的规则**：

  规则：凡 `x = deepseek_chat(...)`，在对 `x` 取字段（`x.get(` / `x[`）**之前**，
        必须出现以下之一：
          · 带契约包装：`deepseek_chat_obj(...)` / `deepseek_chat_rows(...)`（**首选**，语法上不可能忘）
          · 规整工具：  `as_dict(x)` / `as_rows(x)`
          · 显式判定：  `isinstance(x, dict)` / `isinstance(x, list)`
        否则判为「裸用」→ 门禁失败，并打印 `文件:行 函数名`。

  ★ 只判 `x is None` **不算**处置 —— 那正是本次事故的写法（None 检查通过后照样 `.get` 崩）。

## 为什么不统一成一种包装

`{"candidates": [...]}` 这类"对象里嵌列表"的返回**必须能按列表语义解释**；
强行统一会吃掉语义（见 [`as_dict()`](ai-quant-agent/backend/app/agents/llm_client.py:205) 的说明）。
故门禁只要求"取值前有处置"，不规定处置方式。

## 判据可信度

脚本带**自检**：把"裸用"和"只判 None"两种反面样例喂给扫描器，必须都被判违规；
把三种正确样例喂进去必须全过 —— 防"门禁永远绿"的假绿。

运行：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_llm_shape_guard.py
"""
from __future__ import annotations

import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(BACKEND, "app")

CHAT_FUNCS = {"deepseek_chat", "deepseek_chat_obj", "deepseek_chat_rows"}
# 带形状契约的包装：用了它们就**结构上**不可能忘（永远返回 dict / list[dict]）
TYPED_FUNCS = {"deepseek_chat_obj", "deepseek_chat_rows"}

PASS: list[str] = []
FAIL: list[str] = []


def ck(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _call_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _assigned_var(node: ast.Assign) -> str:
    for t in node.targets:
        if isinstance(t, ast.Name):
            return t.id
    return ""


def _strip_comments(lines: list[str]) -> str:
    """去掉整行注释与行尾注释（保守：仅当 `#` 前引号成对时才截断）。

    ★ 为什么必须做：注释里天然会写"反面示例代码"（例如解释事故时写
      「原来直接 `raw.get(...)` 就崩」）。不剥掉注释，扫描器就会把**已经修好的代码
      判成裸用**（假红）—— 门禁一旦开始假红，就没人再信它了。
    """
    out: list[str] = []
    for ln in lines:
        if ln.lstrip().startswith("#"):
            out.append("\n")
            continue
        i = ln.find("#")
        if i != -1:
            head = ln[:i]
            if head.count('"') % 2 == 0 and head.count("'") % 2 == 0:
                ln = head + "\n"
        out.append(ln)
    return "".join(out)


def scan_source(name: str, src: str) -> list[str]:
    """扫描单份源码，返回「裸用」明细（`文件:行 函数`）。无违规则返回 []。"""
    try:
        tree = ast.parse(src, filename=name)
    except SyntaxError as exc:  # noqa: BLE001
        return [f"{name}: 语法错误，无法扫描（{exc}）"]
    lines = src.splitlines(keepends=True)
    out: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
                continue
            cname = _call_name(node.value)
            if cname not in CHAT_FUNCS or cname in TYPED_FUNCS:
                continue                      # 带契约包装 ⇒ 结构安全，跳过
            var = _assigned_var(node)
            if not var:
                continue                      # 未落变量（如 `return deepseek_chat(...)`）⇒ 无法裸用
            assign_end = node.end_lineno or node.lineno
            fn_end = fn.end_lineno or len(lines)
            # 赋值之后 → 函数末尾（先剥注释，避免注释里的示例代码造成假红）
            tail = _strip_comments(lines[assign_end:fn_end])
            v = re.escape(var)
            uses = [m.start() for m in re.finditer(rf"\b{v}\s*(?:\.get\(|\[)", tail)]
            if not uses:
                continue                      # 从不取字段 ⇒ 无从崩
            guards = [m.start() for m in re.finditer(
                rf"(?:isinstance\(\s*{v}\s*,"
                rf"|as_dict\(\s*{v}\s*\)"
                rf"|as_rows\(\s*{v}\s*\))", tail)]
            first_use = min(uses)
            if not guards or min(guards) > first_use:
                rel_line = assign_end + tail[:first_use].count("\n")
                out.append(
                    f"{name}:{rel_line} 函数 {fn.name}() 裸用 `{var}` ——"
                    f"取字段前没有形状处置（未用 obj/rows 包装、未 isinstance、未 as_dict/as_rows；"
                    f"只判 `is None` 不算）"
                )
    return out


def scan_tree() -> list[str]:
    bad: list[str] = []
    for root, _dirs, files in os.walk(APP):
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            rel = os.path.relpath(path, BACKEND)
            with open(path, encoding="utf-8") as fh:
                bad.extend(scan_source(rel, fh.read()))
    return bad


def main() -> int:
    print("=" * 100)
    print("门禁：deepseek_chat 消费点必须处置返回形状（防「'list' object has no attribute 'get'」复发）")
    print("=" * 100)

    # ── ① 门禁自检：反面样例必须被判违规，正面样例必须通过 ──
    print("\n① 门禁自检（防「门禁永远绿」的假绿）")
    print("-" * 100)
    bad_plain = (
        "def f():\n"
        "    result = deepseek_chat([{'role': 'user', 'content': 'x'}])\n"
        "    return result.get('a')\n"
    )
    bad_none_only = (
        "def f():\n"
        "    result = deepseek_chat([{'role': 'user', 'content': 'x'}])\n"
        "    if result is None:\n"
        "        return {}\n"
        "    return result.get('a')\n"
    )
    bad_subscript = (
        "def f():\n"
        "    result = deepseek_chat([{'role': 'user', 'content': 'x'}])\n"
        "    result['raw_news'] = []\n"
        "    return result\n"
    )
    good_isinstance = (
        "def f():\n"
        "    result = deepseek_chat([{'role': 'user', 'content': 'x'}])\n"
        "    if not isinstance(result, dict):\n"
        "        return {}\n"
        "    return result.get('a')\n"
    )
    good_inline = (
        "def f():\n"
        "    result = deepseek_chat([{'role': 'user', 'content': 'x'}])\n"
        "    if not isinstance(result, dict) or not result.get('a'):\n"
        "        return {}\n"
        "    return result\n"
    )
    good_wrapper = (
        "def f():\n"
        "    result = deepseek_chat_obj([{'role': 'user', 'content': 'x'}])\n"
        "    return result.get('a')\n"
    )
    good_asdict = (
        "def f():\n"
        "    raw = deepseek_chat([{'role': 'user', 'content': 'x'}])\n"
        "    d = as_dict(raw)\n"
        "    return d.get('a')\n"
    )
    good_return_only = (
        "def f():\n"
        "    result = deepseek_chat([{'role': 'user', 'content': 'x'}])\n"
        "    return result if isinstance(result, dict) else None\n"
    )
    ck("A1 裸用 `.get()` ⇒ 判违规", bool(scan_source("bad_plain.py", bad_plain)))
    ck("A2 **只判 `is None`** ⇒ 仍判违规（这正是本次事故的写法）",
       bool(scan_source("bad_none.py", bad_none_only)))
    ck("A3 裸用下标 `result[...]` ⇒ 判违规", bool(scan_source("bad_sub.py", bad_subscript)))
    ck("A4 isinstance 判定在取值之前 ⇒ 通过",
       not scan_source("good_isinstance.py", good_isinstance))
    ck("A5 同一行内 `isinstance(..., dict) or x.get(...)` ⇒ 通过（短路安全）",
       not scan_source("good_inline.py", good_inline))
    ck("A6 用 deepseek_chat_obj 包装 ⇒ 通过（语法上不可能忘）",
       not scan_source("good_wrapper.py", good_wrapper))
    ck("A7 用 as_dict(raw) 规整 ⇒ 通过", not scan_source("good_asdict.py", good_asdict))
    ck("A8 只 return 不取字段 ⇒ 通过", not scan_source("good_return_only.py", good_return_only))

    # ── ② 契约工具存在且可观测（不再静默降级）──
    print("\n② 契约工具：带形状契约的包装 + 形状不符必须 WARNING")
    print("-" * 100)
    from app.agents import llm_client as LC  # noqa: E402（放在自检之后导入，避免副作用干扰）

    ck("B1 `as_dict()` / `as_rows()` 可用", callable(LC.as_dict) and callable(LC.as_rows))
    ck("B2 `deepseek_chat_obj()` / `deepseek_chat_rows()` 可用（新代码首选）",
       callable(getattr(LC, "deepseek_chat_obj", None))
       and callable(getattr(LC, "deepseek_chat_rows", None)))
    lc_src = open(os.path.join(BACKEND, "app", "agents", "llm_client.py"), encoding="utf-8").read()
    ck("B3 形状不符有 WARNING（`_warn_shape` 被两个包装调用）",
       lc_src.count("_warn_shape(") >= 2, f"命中 {lc_src.count('_warn_shape(')} 处")
    with mock.patch.object(LC, "deepseek_chat", return_value=[{"a": 1}, {"b": 2}]):
        ck("B4 `deepseek_chat_obj()` 对多元素数组返回 {}（不折叠、不崩）",
           LC.deepseek_chat_obj([]) == {})
    with mock.patch.object(LC, "deepseek_chat",
                           return_value={"candidates": [{"ts_code": "1"}, {"ts_code": "2"}]}):
        rows = LC.deepseek_chat_rows([])
    ck("B5 `deepseek_chat_rows()` 能取出对象里嵌的列表（[\"candidates\"]）",
       [r["ts_code"] for r in rows] == ["1", "2"], str(rows))

    # ── ③ 全量扫描 app/：现状必须零裸用 ──
    print("\n③ 全量扫描 `app/`：所有 deepseek_chat 消费点（现状应为 0 违规）")
    print("-" * 100)
    bad = scan_tree()
    ck("C1 无裸用消费点", not bad, "\n      " + "\n      ".join(bad) if bad else "")
    # 覆盖率自证：扫描器至少要看到"足够多"的消费点，否则规则可能形同虚设
    sites = 0
    for root, _dirs, files in os.walk(APP):
        for f in files:
            if f.endswith(".py"):
                with open(os.path.join(root, f), encoding="utf-8") as fh:
                    sites += sum(1 for ln in fh if re.search(r"(?<!def )\bdeepseek_chat\(", ln))
    ck("C2 扫描确实覆盖到调用点（防「扫到 0 处也算通过」的假绿）",
       sites >= 12, f"命中 {sites} 处调用")

    print("\n" + "=" * 100)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("门禁通过：不存在「取值前未处置形状」的 deepseek_chat 消费点。")
    print("新增消费点请用 deepseek_chat_obj()/deepseek_chat_rows()（或 as_dict()/as_rows()+isinstance）；")
    print("本脚本应在每次改动 Agent/LLM 调用后重跑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
