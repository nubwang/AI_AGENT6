"""应用配置（pydantic-settings v2+）"""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env 优先取当前目录, 其次取项目根目录(本地运行时从 backend/ 启动也能找到)
PROJECT_ROOT = Path(__file__).resolve().parents[3]  # backend/app/core/config.py -> ai-quant-agent 项目根


class Settings(BaseSettings):
    # 应用
    app_name: str = "AI Quant Agent"
    app_version: str = "0.1.0"
    debug: bool = False

    # 数据库
    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = "quant_user"
    db_password: str = "quant_pass_2024"
    db_name: str = "quant_db"

    @property
    def database_url(self) -> str:
        return f"mysql+pymysql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379

    # Tushare
    tushare_token: str = ""
    # Tushare 每分钟请求上限(免费档一般200, 更高积分可调大)。
    # 注意: Tushare 限频按"单接口"计, 原实现把全部接口塞进这一个全局桶,
    # 几十个接口互相拖累成一条 200/min 管道 → 采集极慢、大量"触发限流等待"。
    # 现改为每接口独立桶(见 tushare_rate_per_api), 本字段仅作为"未命名调用"的默认桶配额兼容保留。
    tushare_rate_limit: int = 200
    # Tushare 每个接口每分钟请求上限(基础档多数接口 200/min)。各接口独立令牌桶并行满速,
    # 不再互相拖累; 若你某接口配额更高(更高积分), 可在 .env 调大此值。
    tushare_rate_per_api: int = 200
    # 可选"全局总调用保护上限"(每分钟, 0=关闭)。仅当你担心账户级总限频/风控时设置(如 500)。
    # 默认关闭: Tushare 以单接口计频, 设过小反而拖慢整体吞吐。
    tushare_rate_global: int = 0

    # DeepSeek
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    # 思考模式（plans/25 §14.3）：官方**默认开启**且 effort 默认 high，产生的 reasoning_tokens
    # 计入 completion_tokens 并**按输出价计费**（输出单价是输入未命中价的 4 倍）→ 成本会数倍膨胀；
    # 另有副作用：思考模式下 temperature/presence_penalty/frequency_penalty **不生效**（官方明确）。
    # 用户口径（2026-09-22 明确）：**deepseek-flash 就用思考模式** ⇒ 默认 True，
    # 且请求体里**始终显式写入** {"thinking":{"type":"enabled/disabled"}}（不依赖官方默认值，
    # 免得哪天官方改默认被人静默改变成本与行为）。
    # 注意：思考模式下 temperature / presence_penalty / frequency_penalty **不生效**；
    # 若某个抽取类 Agent 需要 temperature 真正生效，请在调用处显式传 `thinking=False`。
    # .env 可覆盖：DEEPSEEK_THINKING=false 关闭；DEEPSEEK_REASONING_EFFORT=low/high/max 调强度。
    deepseek_thinking: bool = True
    deepseek_reasoning_effort: str = "high"
    # LLM 三闸门预算（元）。**首次创建 data/llm_budget.json 时作为初始值**；已有台账则沿用
    # 文件里的值（人工改文件或用 llm_metering.set_limits() 调整），避免"改了 .env 却对不上台账"。
    # ★ 默认 per_day=3.0 元实测**不够一次完整扫描**：2026-09-23 当天 189 次调用就花掉 3.0017 元
    #   （≈1.6 分/次，其中 187 次带思维链），预算一打满 → 后续 `deepseek_chat` 直接返回 None
    #   → RefineAgent 大面积降级为"按规则概率保留"。要跑完整扫描建议 8~15 元/天，
    #   或用 `DEEPSEEK_THINKING=false`（实测约省 3.7×）/ 缩小候选数来降本。
    llm_budget_per_task_cny: float = 5.0
    llm_budget_per_day_cny: float = 10.0
    llm_budget_total_cny: float = 50.0

    # Tavily（消息面验证，多 Agent 精筛）
    tavily_api_key: str = ""
    # ★ 消息面**必需**（用户口径 2026-09-23："必须使用 Tavily 得有消息面，不能失败直接略过了"）：
    # True 时：单次调用内跨轮重试 → 仍失败则把 query 记入"欠账队列"（data/tavily_pending.json）
    # 稍后由 tavily_drain_pending() 补做，并把该失败升级为 ERROR 工单进事件流，
    # 交给进化大脑（code_writer 读 evolution_events.recent_errors）去改代码/改配置。
    # 关闭 = 退回旧的"失败即静默降级为无消息面"。
    tavily_required: bool = True
    # 访问 api.tavily.com 需要稳定 TLS 链路；直连偶发 `SSLError: SSLZeroReturnError`
    # （TLS 被链路上某处中断，表现为"有时成功有时失败"，非代码 bug）。
    # 若失败频繁：填本地/公司代理走更稳的出口，如 http://127.0.0.1:7890。留空则沿用系统
    # HTTPS_PROXY（requests 默认 trust_env）。
    tavily_proxy: str = ""
    # 自定义端点（一般留空；仅当走自建网关/镜像时填完整前缀，如 https://my-gw.example.com）
    tavily_base_url: str = ""
    # 单次请求读取超时（秒）。实测正常响应 2~3s，但该链路偶发"握手成功却长时间无响应"
    # （已实际观测到一次 ReadTimeout）→ 给足 30s，避免把慢响应当故障
    tavily_timeout: int = 30
    # 每轮尝试次数（含首次）。SSL/连接中断与 429/5xx 走指数退避重试；4xx 不重试（重试无意义）
    tavily_max_attempts: int = 3
    # 跨轮重试轮数：瞬时 TLS 抖动常持续几秒，一轮打完就放弃太早 → 轮间等待 tavily_round_gap_s
    # 再打下一轮（默认 3 轮 × 3 次 = 最多 9 次请求，总等待约 17s）
    tavily_rounds: int = 3
    tavily_round_gap_s: float = 5.0
    # 单次调用总时限（秒，硬约束）：跨轮重试不允许把每日扫描拖死
    tavily_deadline_s: int = 60
    # 欠账队列上限（条）：拿不到的消息面 query 排队待补，超上限丢最旧
    tavily_pending_max: int = 200
    # 每轮扫描收尾时最多补做几条欠账（0=不补做）。补做成功的结果会落消息面缓存，
    # 供当天同 query（行业政策/盘前兜底）复用 —— 欠账才有意义，不然只是"记着不用"
    tavily_drain_limit: int = 3
    # 消息面缓存有效期（秒，默认 6 小时）：同 query 在 TTL 内直接命中缓存，
    # 既省额度，也让"补做回来的消息面"真正被后续流程用上
    tavily_cache_ttl_s: int = 21600
    # 连续失败达该次数 → 熔断降温：暂停请求 tavily_circuit_cooldown 秒（期间只打 debug、
    # 并把失败的 query 记入欠账队列）。60s 而非更久：熔断只是"降温"，不是"放弃消息面"
    tavily_fail_threshold: int = 3
    tavily_circuit_cooldown: int = 60
    # 校验 TLS 证书（默认开）。仅当自建网关证书链不完整时才考虑关闭
    tavily_verify_ssl: bool = True

    # Agent 精筛（多 Agent 协作 + 统一决策，对齐 plans/03-Agent系统.md）
    agent_enabled: bool = True          # 总开关
    agent_top_k: int | None = None      # 进入 LLM 精筛的候选数（None = 全部候选池，不限制）
    agent_final_top_n: int | None = None  # 最终榜单条数（None = 全部 select）
    agent_batch: int = 10               # 每次 LLM 调用的候选股数
    # ★ 三层漏斗的规模参数（默认值 = 历史口径；同名常量在 app/agents/__init__.py，可被这里覆盖）。
    #   以前这几个数**硬编码**，想调"每板块几只进深度分析"只能改代码。
    agent_l1_batch_size: int = 25       # L1 批量粗筛：每批候选数
    agent_l1_per_industry: int = 10     # L1 批量粗筛：每行业最多保留数
    # L2 深度精筛：全局 Top-N（按 up_probability）—— **注意是全局 Top-N，不是"每板块前 N"**
    agent_l2_deep_top_n: int = 50
    # L2 每行业保底进深度的只数（**默认 10 = 用户口径"每个板块前 10 都进深度分析"**；
    # 0=关闭，退回"只有全局 Top-N"）。
    # 口径说明：L1 的"每行业前 10"只是候选，只有这里 >0 才**保证**每个板块的头部都被
    # Tavily 消息面 + LLM 精筛看到（行业多则成本线性上升，务必配合 LLM 预算）。
    agent_l2_per_industry_floor: int = 10
    # L2 总量硬上限（防"每行业保底"在行业多时爆量）。
    # 该上限**只裁"每行业保底"**：全局 Top-N 与形态规则保送是硬承诺，不会被裁。
    agent_l2_max_total: int = 200
    agent_policy_max_sectors: int = 5   # 政策解读只对频次最高的 N 个行业做

    # 定时任务
    data_collect_time: str = "18:00"
    prediction_time: str = "19:00"

    model_config = SettingsConfigDict(
        env_file=(".env", str(PROJECT_ROOT / ".env")),
        env_file_encoding="utf-8",
    )


settings = Settings()
