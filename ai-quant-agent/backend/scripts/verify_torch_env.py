"""本地推理环境自检（torch / sentence-transformers / 模型缓存 / 重排可用性）

## 为什么需要这个脚本

2026-09-23 10:45 线上日志：
    [WARNING] [policy_kb] 重排序模型加载失败（跳过）: dlopen(.../torch/_C.cpython-310-darwin.so):
    Library not loaded: @loader_path/libtorch_cpu.dylib

真相：venv 里 **torch 只缺了一个文件** `torch/lib/libtorch_cpu.dylib`（449MB），
其余 1000+ 文件齐全（目录 mtime 比同目录其他文件新 → 该文件是被单独删掉的，
多半是"清磁盘大文件"顺手误删）。但后果是**整条本地模型链路静默降级**：

  - `import torch` 直接 dlopen 失败 → 所有依赖 torch 的代码都不可用；
  - policy_kb 混合召回退化成**纯 BM25**（语义召回丢失）；
  - EKB 向量检索、bge-reranker 重排（BAAI/bge-reranker-v2-m3）全部跳过。

而日志里只有一句 WARNING（而且"（跳过）"看起来像是正常降级）→ 极易被忽略。
本脚本把"环境到底完不完整 + 缺什么 + 怎么修"一次讲清楚。

## 验什么

  ① torch 能否 import（版本、MPS 可用性）
  ② torch 关键 dylib 是否齐全（对照 wheel 的 RECORD 清单，缺哪个说得出来）
  ③ 已装 dylib 的 sha256 是否与 RECORD 一致（防"文件在但被截断/替换"）
  ④ sentence_transformers / transformers 能否 import
  ⑤ 本地 HF 模型缓存是否具备（bge-large-zh-v1.5 向量 / bge-reranker-v2-m3 重排）
  ⑥ `--deep` 时真加载 policy_kb 重排模型并做一次相关性打分（相关应显著高于无关）

跑法：
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_torch_env.py
    cd ai-quant-agent/backend && ./venv/bin/python scripts/verify_torch_env.py --deep
"""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILS: list[str] = []
WARNS: list[str] = []

# 关键动态库（macOS x86_64 的 torch wheel 里这几个是"缺一个就整包不可用"的）
CRITICAL_LIBS = ("libtorch_cpu.dylib", "libtorch_python.dylib", "libc10.dylib")

# 本地链路依赖的 HF 模型缓存（目录名即 huggingface hub 的 models--<org>--<name>）
REQUIRED_MODELS = {
    "向量 embedding": "models--BAAI--bge-large-zh-v1.5",
    "重排 reranker": "models--BAAI--bge-reranker-v2-m3",
}

TORCH_VER = "2.2.2"          # 与 venv 内实际版本保持一致的"修复默认值"（macOS 最后一个 x86_64 wheel）
PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"


def ck(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label} {detail}")
        FAILS.append(label)


def warn(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ⚠️  {label} {detail}")
        WARNS.append(label)


def _sp() -> str:
    """site-packages 路径（由本文件位置反推，避免依赖当前工作目录）。"""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "venv", "lib", "python3.10", "site-packages")


def check_torch_libs(sp: str) -> None:
    print("\n② torch 关键动态库（缺 1 个就整包不可用）")
    lib_dir = os.path.join(sp, "torch", "lib")
    for name in CRITICAL_LIBS:
        p = os.path.join(lib_dir, name)
        ck(os.path.exists(p), f"{name} 存在",
           f"缺失 → {REPAIR_HINT}")

    # 对照 RECORD 全量清单（能把"缺哪个文件"精确列出来）
    rec = os.path.join(sp, f"torch-{TORCH_VER}.dist-info", "RECORD")
    if not os.path.exists(rec):
        warn(False, "找到 torch 的 RECORD 清单（跳过全量比对）", rec)
        return
    rows: list[tuple[str, str, str]] = []
    with open(rec, newline="") as f:
        for row in csv.reader(f):
            if len(row) >= 3 and row[0].startswith("torch/"):
                rows.append((row[0], row[1], row[2]))
    missing = [r[0] for r in rows if not os.path.exists(os.path.join(sp, r[0]))]
    ck(not missing, f"RECORD 声明的 {len(rows)} 个文件全部在位",
       f"缺失 {len(missing)} 个: {missing[:5]}")
    # 抽查已存在的关键库是否被截断/替换（sha256 与 RECORD 一致）
    # ★ RECORD 里的哈希是 **URL-safe base64 且去掉 '=' 填充**（`-`/`_` 而非 `+`/`/`）。
    #   用标准 base64 比对会误报"内容不一致"（第一版就踩了：只有恰好不含 +/ 的文件才通过）。
    for name in CRITICAL_LIBS:
        rel = f"torch/lib/{name}"
        want = next((h.split("=", 1)[1] for p, h, _ in rows if p == rel and h.startswith("sha256=")), "")
        if not want:
            continue
        path = os.path.join(sp, rel)
        if not os.path.exists(path):
            continue
        got = base64.urlsafe_b64encode(hashlib.sha256(open(path, "rb").read()).digest()).decode().rstrip("=")
        ck(got == want, f"{name} 内容与官方 wheel 一致（sha256）", f"got={got[:16]}… want={want[:16]}…")


REPAIR_HINT = "跑下面「修复命令」"


def check_python_deps() -> None:
    print("\n①④ torch / sentence-transformers / transformers")
    try:
        import torch
        ck(True, f"import torch 成功（{torch.__version__}）")
        print(f"     设备: mps={torch.backends.mps.is_available()} "
              f"cuda={torch.cuda.is_available()} threads={torch.get_num_threads()}")
    except Exception as exc:  # noqa: BLE001
        ck(False, "import torch 成功", f"{type(exc).__name__}: {exc} → {REPAIR_HINT}")
        return
    for mod in ("sentence_transformers", "transformers"):
        try:
            m = __import__(mod)
            ck(True, f"import {mod} 成功（{getattr(m, '__version__', '?')}）")
        except Exception as exc:  # noqa: BLE001
            ck(False, f"import {mod} 成功", f"{type(exc).__name__}: {exc}")


def check_model_cache() -> None:
    print("\n⑤ 本地 HF 模型缓存（policy_kb 向量召回 / reranker 重排）")
    hub = os.path.expanduser("~/.cache/huggingface/hub")
    names = set(os.listdir(hub)) if os.path.isdir(hub) else set()
    for label, d in REQUIRED_MODELS.items():
        warn(d in names, f"{label} 已缓存（{d}）",
             f"未缓存 → 首次调用会去 HF 下载（{hub}）")


def check_deep() -> None:
    print("\n⑥ 真加载 policy_kb 重排模型 + 相关性打分（--deep）")
    try:
        import time
        from app.data import policy_kb
        t0 = time.time()
        rr = policy_kb._load_reranker()
        ck(rr is not None, f"重排模型加载成功（{time.time() - t0:.1f}s）")
        if not rr:
            return
        import numpy as np
        scores = rr.predict([
            ("半导体 政策 支持", "国家出台集成电路产业扶持政策，加大税收优惠与研发补贴。"),
            ("半导体 政策 支持", "今天天气不错，适合出去散步。"),
        ])
        hi, lo = float(np.ravel(scores)[0]), float(np.ravel(scores)[1])
        ck(hi > lo + 0.2, f"相关性打分合理（相关 {hi:.3f} > 无关 {lo:.3f}）",
           f"hi={hi} lo={lo}")
    except Exception as exc:  # noqa: BLE001
        ck(False, "重排模型加载与打分", f"{type(exc).__name__}: {exc}")


def repair_commands() -> str:
    return f"""
────────────────────────────────────────────────────────────────
修复命令（只补缺失文件，不动其他依赖；本次实测就是这么修好的）
────────────────────────────────────────────────────────────────
cd ai-quant-agent/backend

# 1) 下载官方 wheel（清华镜像；macOS Intel 用 macosx_10_9_x86_64，Apple Silicon 换成 arm64）
./venv/bin/pip download torch=={TORCH_VER} --no-deps -d /tmp/torch_whl -i {PIP_INDEX}

# 2) 只解出需要的那几个库文件
mkdir -p /tmp/torch_x && unzip -o /tmp/torch_whl/torch-{TORCH_VER}-*.whl 'torch/lib/*.dylib' -d /tmp/torch_x

# 3) 补回 venv（先备份可省；下列 cp 不覆盖已有文件之外的任何东西）
cp -n /tmp/torch_x/torch/lib/*.dylib venv/lib/python3.10/site-packages/torch/lib/

# 4) 自检（应全部 ✅；加 --deep 会真加载 reranker 并打分）
./venv/bin/python scripts/verify_torch_env.py --deep
"""


def main() -> int:
    deep = "--deep" in sys.argv
    sp = _sp()
    print("本地推理环境自检（torch / 向量 / 重排）")
    print(f"  venv site-packages: {sp}")
    check_python_deps()
    check_torch_libs(sp)
    check_model_cache()
    if deep:
        check_deep()
    else:
        print("\n⑥ 重排真加载：未跑（加 --deep 可跑，约 15s）")

    print("\n" + "=" * 60)
    if FAILS:
        print(f"❌ 环境不完整（{len(FAILS)} 项）：这些会让 policy_kb 静默降级为纯 BM25、"
              f"EKB 向量检索与重排全部跳过")
        for f in FAILS:
            print(f"   - {f}")
        print(repair_commands())
        return 1
    if WARNS:
        print(f"⚠️  通过但有 {len(WARNS)} 项提醒：{WARNS}")
    print("✅ 本地模型链路环境完好（torch / embedding / reranker 均可用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
