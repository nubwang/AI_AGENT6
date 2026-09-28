"""进化配置读取（evolution_config）— 对齐 plans/11 进化Agent §8.3/E4/G5/J1

MVP 能力：
  - Tier1 可进化参数"热生效"：模块每次调用读 evolution_config.json（进程内缓存 + mtime 检查 G5）
  - 读配置失败回退默认值 + 告警（J1 防御：config 损坏不崩精筛）
  - 提供 get_param / get / params_table / set_param（供 self_improver 写新值）

进化模块自身在保护区（H1）：本文件不可被进化 Agent 修改（由 evolution_config.json 的 protected_files 登记）。
"""
from __future__ import annotations

import json
import os

from app.core.logger import logger

CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "evolution_config.json",
)

# 进程内缓存（热路径：mtime 变化才重读，避免每次 IO + 写读并发不一致 G5）
_cache = {"mtime": None, "data": None}


def _load() -> dict:
    """读取配置（mtime 缓存）。失败回退缓存/空 dict（J1：不崩调用方）。"""
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
        if _cache["data"] is not None and _cache["mtime"] == mtime:
            return _cache["data"]
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _cache["mtime"] = mtime
        _cache["data"] = data
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[evolution_config] 读取失败（回退缓存/空）: {exc}")
        return _cache["data"] or {}


def get_param(name: str, default=None):
    """获取可进化参数当前值（Tier1 热生效）。current 优先，其次 default。失败回退 default（J1）。"""
    data = _load()
    p = data.get("params", {}).get(name)
    if not p:
        return default
    return p.get("current", p.get("default", default))


def get_num(name: str, default):
    """读"数值/布尔型"可进化参数（带类型转换与失败回退，plans/23 §十二）。

    供回测可信度阈值（A/B 覆盖率、条件表规则门槛、归因准入、数据门禁、采集重试）
    统一从 evolution_config 取值 —— 这样这些阈值就成了**进化大脑可直接调优的对象**，
    而不是散落在各模块里的硬编码常量（改一次要重启、还没有影子验证）。

    失败一律回退 default（J1：不崩调用方）。
    """
    v = get_param(name, default)
    if isinstance(default, bool):
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)
    try:
        return float(v) if isinstance(default, float) else int(v)
    except (TypeError, ValueError):
        return default


def get(name: str, default=None):
    """获取配置顶层字段。"""
    return _load().get(name, default)


def params_table() -> dict:
    """返回全部参数定义表（含锚点/min/max/step/tier/hot_reload/shadow_eligible）。"""
    return _load().get("params", {})


def set_param(name: str, value) -> bool:
    """写入参数当前值（self_improver 应用提案用）。写后刷新缓存。返回是否成功。"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        params = data.setdefault("params", {})
        if name not in params:
            logger.warning(f"[evolution_config] 未知参数 {name}，拒绝写入")
            return False
        params[name]["current"] = value
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _cache["data"] = data
        _cache["mtime"] = os.path.getmtime(CONFIG_FILE)
        logger.info(f"[evolution_config] 参数 {name} 热生效 -> {value}")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[evolution_config] 写入参数 {name} 失败: {exc}")
        return False


def protected_files() -> list:
    """进化模块保护区清单（H1）：这些文件禁止被进化 Agent 修改。"""
    return _load().get("protected_files", [])


__all__ = ["get_param", "get", "params_table", "set_param", "protected_files", "CONFIG_FILE"]
