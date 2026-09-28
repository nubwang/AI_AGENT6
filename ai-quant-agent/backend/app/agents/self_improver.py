"""自我改进执行器（self_improver）— 对齐 plans/11 进化Agent §7.4/§8/H1/J2

MVP 能力：
  - 应用进化提案：Tier1 热生效参数（改 evolution_config.json，set_param 即热生效）
  - 保护区拦截（H1）：目标文件在 protected_files → 拒绝
  - 范围校验 / 备份 / 审计日志 / 回滚
  - 锚点代码补丁（非热生效参数）：MVP 暂拒绝，扩展 1 接入

安全：所有改动必须经 apply_proposal（备份+审计），禁止绕过；失败即回滚。
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import time

from app.core.logger import logger
from app.agents import evolution_config

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
AUDIT_FILE = os.path.join(DATA_DIR, "self_improve_log.json")
RESTART_FLAG_FILE = os.path.join(DATA_DIR, "evolution_restart_pending.json")
PROJECT_ROOT = os.path.dirname(DATA_DIR)  # backend/


def _is_protected(file_rel: str) -> bool:
    return file_rel in evolution_config.protected_files()


def _py_compile(file_rel: str) -> bool:
    """静态验证：py_compile 编译（J2：锚点/文件异常优雅降级）。"""
    abs_path = os.path.join(PROJECT_ROOT, file_rel)
    try:
        import py_compile
        py_compile.compile(abs_path, doraise=True)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[self_improver] 静态验证失败 {file_rel}: {exc}")
        return False


def _backup_versions(path: str, keep: int = 30) -> str:
    """写前版本化备份：保留时间戳独立副本 + 最新 .bak。返回时间戳副本路径；失败返回 ''。
    每次进化留独立版本 → rollback_last 可回滚任意历史（用户要求：保存好每次进化的备份版本）。"""
    # datetime.strftime 支持 %f 微秒（macOS 的 time.strftime 不支持 %f），避免同秒多次进化互覆盖丢版本
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    ver = f"{path}.bak.{ts}"
    try:
        shutil.copy2(path, ver)
        shutil.copy2(path, path + ".bak")  # 最新副本
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[self_improver] 版本化备份失败 {path}: {exc}")
        return ""
    # 清理旧版本（保留 keep 个最新，防无限增长）
    try:
        import glob
        vers = sorted(glob.glob(f"{path}.bak.*"), reverse=True)
        for v in vers[keep:]:
            try:
                os.remove(v)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return ver


def _audit(entry: dict) -> None:
    """写审计日志 self_improve_log.json（含前后值/依据/结果）。"""
    records: list = []
    if os.path.exists(AUDIT_FILE):
        try:
            with open(AUDIT_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)
        except Exception:  # noqa: BLE001
            records = []
    records.append(entry)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(AUDIT_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[self_improver] 审计写入失败: {exc}")


def _load_audit() -> list:
    try:
        with open(AUDIT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return []


def apply_proposal(proposal: dict) -> dict:
    """应用进化提案（MVP：Tier1 热生效参数）。

    proposal: {param, new, reason, evidence}
    返回 {ok, action, param, old, new, message}
    """
    param = proposal.get("param")
    new = proposal.get("new")
    if not param:
        return {"ok": False, "action": "reject", "message": "缺少参数名"}
    params = evolution_config.params_table()
    p = params.get(param)
    if not p:
        return {"ok": False, "action": "reject", "message": f"未知参数 {param}"}

    # 保护区拦截（H1）+ 记录越权拦截
    if _is_protected(p.get("file", "")):
        try:
            from app.agents import evolution_guard
            evolution_guard.record_intercept()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "action": "reject", "message": f"目标 {p.get('file')} 在进化保护区，禁止自动修改"}

    # Bug9：Tier1 也检查 H3 冻结（影子期参数禁止触碰）
    try:
        from app.agents import evolution_shadow
        if evolution_shadow.is_frozen(param):
            return {"ok": False, "action": "reject", "message": f"{param} 正在影子期冻结（H3），禁止改动"}
    except Exception:  # noqa: BLE001
        pass

    # 范围校验
    try:
        nv = float(new)
    except (TypeError, ValueError):
        return {"ok": False, "action": "reject", "message": f"新值 {new} 非数值"}
    if "min" in p and nv < float(p["min"]):
        return {"ok": False, "action": "reject", "message": f"{param} 低于下限 {p['min']}"}
    if "max" in p and nv > float(p["max"]):
        return {"ok": False, "action": "reject", "message": f"{param} 高于上限 {p['max']}"}

    old = p.get("current", p.get("default"))

    # 应用：热生效参数 → set_param；非热生效（Tier2/3 代码锚点）→ apply_patch + 待重启标记（扩展 2 E2/G9）
    if not p.get("hot_reload"):
        return apply_patch(proposal, p, nv)

    # 备份配置（写前，版本化：每次进化独立副本 + 最新 .bak，供任意历史回滚）
    bak = _backup_versions(evolution_config.CONFIG_FILE) \
        if os.path.exists(evolution_config.CONFIG_FILE) else None
    if not evolution_config.set_param(param, nv):
        return {"ok": False, "action": "fail", "message": f"{param} 写入失败"}

    # 静态验证（目标文件编译，J2 优雅降级：失败不致命，但告警）
    if not _py_compile(p.get("file", "")):
        logger.warning(f"[self_improver] {param} 生效后目标文件编译告警（建议人工核查）")

    _audit({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "param": param, "old": old, "new": nv,
            "reason": proposal.get("reason", ""), "evidence": proposal.get("evidence", ""),
            "backup": bak, "action": "apply_config", "status": "ok"})
    # Bug3/Bug4：直接应用路径开账 + 守护记录
    try:
        from app.agents import evolution_ledger, evolution_guard
        evolution_ledger.open_entry(param, old, nv)
        evolution_guard.record_apply(param, "ok")
    except Exception:  # noqa: BLE001
        pass
    # plans/21 §七：参数热生效 → 知识库实验台账标记 applied（不存在则补建，改动必留痕）
    try:
        from app.kb import kb_ingest
        kb_ingest.mark_param_applied(param, old, nv,
                                     note=proposal.get("reason", ""),
                                     file_hint=p.get("file", ""))
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"[self_improver] 应用提案: {param} {old} -> {nv}（热生效）")
    return {"ok": True, "action": "apply_config", "param": param, "old": old, "new": nv}


def apply_patch(proposal: dict, p: dict, nv) -> dict:
    """Tier2/3 代码锚点补丁（E2/G9）：备份 → 锚点替换 → 静态验证 → 失败恢复 → 写待重启标记。

    非热生效参数（hot_reload=False，Tier2/3 业务逻辑/核心框架）无法进程内热生效，
    需改代码锚点后由**独立进程重启**（scripts/restart_backend.sh）生效——本函数只负责
    改代码 + 登记待重启标记，重启由 evolution_apply 触发（E2：进化 Agent 不 kill 自己）。
    """
    param = proposal.get("param")
    if not param:
        return {"ok": False, "action": "reject", "message": "缺少参数名"}
    # 参数冻结（H3）：影子期参数禁止触碰（含人工应用/代码补丁）
    try:
        from app.agents import evolution_shadow
        if evolution_shadow.is_frozen(param):
            return {"ok": False, "action": "reject", "message": f"{param} 正在影子期冻结（H3），禁止改动"}
    except Exception:  # noqa: BLE001
        pass
    file_rel = p.get("file", "")
    anchor_old = p.get("anchor_old", "")
    anchor_tmpl = p.get("anchor_tmpl", "")
    if not file_rel or not anchor_old or not anchor_tmpl:
        return {"ok": False, "action": "reject",
                "message": f"{param} 缺代码锚点配置（file/anchor_old/anchor_tmpl），拒绝自动改代码"}

    abs_path = os.path.join(PROJECT_ROOT, file_rel)
    if not os.path.exists(abs_path):
        return {"ok": False, "action": "reject", "message": f"目标文件不存在 {file_rel}"}
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "action": "fail", "message": f"读取 {file_rel} 失败: {exc}"}

    # 锚点唯一性校验（G3 最小改动：只改唯一锚点，避免误替换多处）
    if content.count(anchor_old) != 1:
        return {"ok": False, "action": "reject",
                "message": f"锚点不唯一/不存在（出现 {content.count(anchor_old)} 次），拒绝自动改代码"}

    try:
        anchor_new = anchor_tmpl.format(value=nv)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "action": "reject", "message": f"anchor_tmpl 格式化失败: {exc}"}

    # 写前备份（版本化：每次补丁独立副本 + 最新 .bak，供任意历史回滚）
    bak = _backup_versions(abs_path)
    if not bak:
        return {"ok": False, "action": "fail", "message": f"备份 {file_rel} 失败"}

    # 应用锚点补丁
    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content.replace(anchor_old, anchor_new, 1))
    except Exception as exc:  # noqa: BLE001
        shutil.copy2(bak, abs_path)  # 写失败 → 恢复
        return {"ok": False, "action": "fail", "message": f"写补丁失败（已恢复）: {exc}"}

    # 一级静态验证（J2）：失败 → 恢复备份
    if not _py_compile(file_rel):
        shutil.copy2(bak, abs_path)
        return {"ok": False, "action": "fail", "message": f"静态验证失败，已恢复 {file_rel}"}

    # 同步 config 记录期望值（Tier2 实际生效靠代码 + 独立重启；config.current 供摘要/记账展示）
    old_cfg = p.get("current", p.get("default"))
    evolution_config.set_param(param, nv)
    _audit({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "param": param, "old": old_cfg,
            "new": nv, "anchor_old": anchor_old, "anchor_new": anchor_new,
            "reason": proposal.get("reason", ""),
            "evidence": proposal.get("evidence", ""), "backup": bak,
            "action": "apply_patch", "status": "ok", "file": file_rel, "tier": p.get("tier", 2)})
    _write_restart_flag(param, nv, bak)
    # plans/21 §七：Tier2/3 补丁应用 → 知识库实验台账标记 applied（待重启生效）
    try:
        from app.kb import kb_ingest
        kb_ingest.mark_param_applied(param, anchor_old, anchor_new,
                                     note=proposal.get("reason", ""), file_hint=file_rel)
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"[self_improver] 代码补丁应用: {param} {anchor_old} -> {anchor_new}（{file_rel}，待独立重启生效）")
    return {"ok": True, "action": "apply_patch", "param": param, "old": anchor_old, "new": anchor_new,
            "file": file_rel, "restart_pending": True}


# ── Tier2/3 待重启标记 + 独立进程重启触发（E2/G9）─────────────
def _write_restart_flag(param: str, nv, backup: str) -> None:
    """写"待重启标记"：代码已改，进程重启后才生效。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(RESTART_FLAG_FILE, "w", encoding="utf-8") as f:
            json.dump({"param": param, "new": nv, "backup": backup,
                       "ts": time.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[self_improver] 待重启标记写入失败: {exc}")


def pending_restart() -> dict | None:
    """读取待重启标记（有未生效的 Tier2/3 代码改动）。"""
    try:
        with open(RESTART_FLAG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def clear_restart_flag() -> bool:
    """重启成功后清除待重启标记。"""
    try:
        if os.path.exists(RESTART_FLAG_FILE):
            os.remove(RESTART_FLAG_FILE)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[self_improver] 清除待重启标记失败: {exc}")
        return False


def evolution_apply() -> dict:
    """触发 Tier2/3 生效：调用**独立进程**重启脚本（E2：进化 Agent 不 kill 自己）。

    由 system API / 人工在前端点击触发；脚本在避让窗口执行 → 健康检查 → 失败回滚 .bak。
    """
    flag = pending_restart()
    if not flag:
        return {"ok": False, "action": "noop", "message": "无待重启改动（Tier1 参数已热生效）"}
    script = os.path.join(os.path.dirname(PROJECT_ROOT), "scripts", "restart_backend.sh")
    if not os.path.exists(script):
        return {"ok": False, "action": "fail", "message": f"重启脚本不存在 {script}"}
    try:
        # BugFix：脚本输出不再 DEVNULL 丢弃——交易窗口被拒/失败原因会丢失且不可排查。
        # 重定向到 logs/restart_backend.log 便于审计（脚本自身含健康检查 + 失败回滚）。
        logf = os.path.join(os.path.dirname(PROJECT_ROOT), "logs", "restart_backend.log")
        try:
            os.makedirs(os.path.dirname(logf), exist_ok=True)
            f = open(logf, "a", encoding="utf-8")
        except Exception:  # noqa: BLE001
            f = None
        subprocess.Popen(["bash", script], stdout=f, stderr=f or subprocess.STDOUT)
        logger.info(f"[self_improver] 已触发独立进程重启（{flag.get('param')}，脚本输出见 {logf}）")
        return {"ok": True, "action": "restart_triggered", "param": flag.get("param"),
                "script": script, "log": logf,
                "message": "已触发独立进程重启（避让窗口 + 健康检查 + 失败回滚；结果见日志）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "action": "fail", "message": f"触发重启失败: {exc}"}


# ── 自动重启（用户决策：不要人工重启，改动自己生效）──────────────
# 与 scripts/restart_backend.sh 的避让窗口同一口径：窗口内不重启（不打断盘中推荐），
# 等 12:00-13:00（午间，也是 DeepSeek 空闲半价时段）或 15:00 之后的第一个安全窗口。
TRADE_WINDOWS = (("09:15", "11:30"), ("13:00", "15:00"))
_LAST_AUTO_RESTART = os.path.join(DATA_DIR, "auto_restart_last.json")


def in_trade_window(ts: float | None = None) -> bool:
    """是否处于 A 股交易窗口（工作日 09:15-11:30 / 13:00-15:00）。"""
    t = time.localtime(ts if ts is not None else time.time())
    if t.tm_wday >= 5:
        return False
    hhmm = f"{t.tm_hour:02d}:{t.tm_min:02d}"
    return any(lo <= hhmm <= hi for lo, hi in TRADE_WINDOWS)


def llm_period(ts: float | None = None) -> str:
    """当前 DeepSeek 计费时段（peak/off_peak）。午间 12:00-14:00 与夜间为空闲（半价）。"""
    try:
        from app.agents import llm_metering
        return str(llm_metering.current_period(ts))
    except Exception:  # noqa: BLE001
        return "unknown"


def _last_auto_restart_epoch() -> float:
    try:
        with open(_LAST_AUTO_RESTART, "r", encoding="utf-8") as f:
            return float(json.load(f).get("ts_epoch") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def decide_restart(has_pending: bool, trade: bool, paused: bool,
                   cooldown: bool) -> tuple[bool, str]:
    """重启守护的**纯决策**（无副作用，便于验证与审计）：返回 (是否重启, 原因)。"""
    if not has_pending:
        return False, "无待重启改动（Tier1 参数已热生效）"
    if trade:
        return False, "交易窗口内（等 12:00-13:00 或 15:00 后自动重启）"
    if paused:
        return False, "进化已暂停（守护/人工）"
    if cooldown:
        return False, "刚触发过重启（3 分钟防抖）"
    return True, "允许自动重启"


def auto_restart_decision(ts: float | None = None) -> dict:
    """是否应立刻自动重启（**纯决策，不执行**）。

    条件：有待重启改动 + 非交易窗口 + 进化未暂停 + 距上次触发 > 3 分钟（防抖）。
    """
    flag = pending_restart()
    try:
        from app.agents import evolution_guard
        paused = bool(evolution_guard.is_paused())
    except Exception:  # noqa: BLE001
        paused = False
    trade = in_trade_window(ts)
    period = llm_period(ts)
    last = _last_auto_restart_epoch()
    now = float(ts if ts is not None else time.time())
    cooldown = (now - last) < 180
    should, reason = decide_restart(bool(flag), trade, paused, cooldown)
    return {"should_restart": should, "reason": reason, "pending": flag,
            "in_trade_window": trade, "llm_period": period,
            "last_auto_restart_epoch": last}


def restart_now(reason: str = "auto") -> dict:
    """立即触发独立进程重启（带防抖与窗口判断），并记录触发时间。"""
    d = auto_restart_decision()
    if not d.get("should_restart"):
        return {"ok": False, "action": "skip", "reason": d.get("reason"), "decision": d}
    res = evolution_apply()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_LAST_AUTO_RESTART, "w", encoding="utf-8") as f:
            json.dump({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "ts_epoch": time.time(), "reason": reason,
                       "param": (d.get("pending") or {}).get("param")}, f,
                      ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[self_improver] 自动重启时间戳写入失败: {exc}")
    res["decision"] = d
    logger.info(f"[self_improver] 自动重启触发（{reason}）：{res.get('action')} {res.get('param') or ''}")
    return res


def rollback_last(nth: int = 1) -> dict:
    """回滚最近第 nth 次成功应用（nth=1 最近一次；规划 §8.5 可回退任意历史版本）。

    配置恢复旧值 / 代码补丁恢复版本化备份 .bak.YYYYMMDD_HHMMSS（+ 清待重启标记）。
    """
    if nth < 1:
        nth = 1
    records = _load_audit()
    seen = 0
    for r in reversed(records):
        if r.get("action") == "apply_config" and r.get("status") == "ok" and r.get("param"):
            seen += 1
            if seen != nth:
                continue
            if evolution_config.set_param(r["param"], r["old"]):
                # Bug3：回滚配置 → 终身记账结账
                try:
                    from app.agents import evolution_ledger
                    evolution_ledger.close_entry(r["param"], "rolled_back")
                except Exception:  # noqa: BLE001
                    pass
                # plans/21 §七：回滚 → 知识库实验台账判 rolled_back（含未达预期原因）
                try:
                    from app.kb import kb_ingest
                    kb_ingest.finalize_param_experiments(
                        str(r["param"]), "rolled_back", f"人工/守护回滚（第 {nth} 次，回滚到 {r['old']}）")
                except Exception:  # noqa: BLE001
                    pass
                r["status"] = "rolled_back"
                r["rolled_back_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _audit(r)
                logger.info(f"[self_improver] 回滚 {r['param']} -> {r['old']}（第 {nth} 次）")
                return {"ok": True, "param": r["param"], "old": r["new"], "restored": r["old"], "nth": nth}
        if r.get("action") == "apply_patch" and r.get("status") == "ok" and r.get("backup"):
            seen += 1
            if seen != nth:
                continue
            bak = r.get("backup")
            if os.path.exists(bak):
                # 恢复目标：审计记录的 file 优先（版本化备份 .bak.YYYYMMDD_HHMMSS 无法反推）；
                # 旧式 .bak 记录按后缀反推
                src = os.path.join(PROJECT_ROOT, r["file"]) if r.get("file") else bak
                if not r.get("file"):
                    src = bak[:-4] if bak.endswith(".bak") else bak
                try:
                    shutil.copy2(bak, src)
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "message": f"恢复代码备份失败: {exc}"}
                # 同步恢复 config.current（代码已恢复，config 记录也回退）
                if r.get("old") is not None and r.get("param"):
                    evolution_config.set_param(r["param"], r["old"])
                # plans/21 §七：代码补丁回滚 → 知识库实验台账判 rolled_back
                try:
                    from app.kb import kb_ingest
                    kb_ingest.finalize_param_experiments(
                        str(r.get("param") or ""), "rolled_back",
                        f"代码补丁回滚（第 {nth} 次，恢复 {src}）")
                except Exception:  # noqa: BLE001
                    pass
                r["status"] = "rolled_back"
                r["rolled_back_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _audit(r)
                clear_restart_flag()
                logger.info(f"[self_improver] 回滚代码补丁 {r.get('param')} -> 恢复 {src}（第 {nth} 次）")
                return {"ok": True, "action": "apply_patch", "param": r.get("param"),
                        "old": r.get("new"), "restored": src, "nth": nth}
    return {"ok": False, "message": f"无可回滚的第 {nth} 次应用记录"}


__all__ = ["apply_proposal", "apply_patch", "rollback_last", "evolution_apply",
           "pending_restart", "clear_restart_flag", "_load_audit",
           "in_trade_window", "llm_period", "decide_restart",
           "auto_restart_decision", "restart_now", "TRADE_WINDOWS"]
