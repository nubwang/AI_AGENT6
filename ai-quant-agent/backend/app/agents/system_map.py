"""系统图谱（system_map）— 对齐 plans/18（用户：把回测与每日推荐的逻辑+参数总结给进化大脑，查漏补缺）

给进化大脑一张"**会随进化修改而动态更新**"的**详细系统图谱**：
  - 每日推荐流水线（11 环节：逻辑 + 关键函数/常量/公式 + 关键参数当前值 + 文件 + 可调方向）
  - 回测流水线（9 步：逻辑 + 关键函数 + 关键参数当前值 + 文件）
  - 全部可进化参数（evolution_config 实时读取）
  - 形态定义（A-E/U）
  - 特征体系（59+ 维：33 形态向量 + ~30 条件参数）
  - 关键常量（含可进化映射）
  - 知识库/软知识（Agent 精筛与进化依据）
  - 回测产物（含存在性）
  - 运行时序（采集→扫描→精筛→验证→学习→进化）

动态性：每次 build_map() 实时读 evolution_config 参数当前值 + 产物文件状态，
进化大脑改参数/代码后图谱自动反映最新状态（图谱随进化而变）。

供：进化大脑 L0 摘要、code_writer 写代码 prompt（告诉它能改什么/在哪/现状）、前端/API 查看。
"""
from __future__ import annotations

import json
import os
import time

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
MAP_FILE = os.path.join(DATA_DIR, "evolution_map.json")

GOAL = "唯一宪法：提高每日推荐股票之后的上涨概率（次日大涨 + 后续主升浪）"

# ════════════ 每日推荐流水线（11 环节，含关键实现细节）════════════
DAILY_PIPELINE = [
    {"stage": "stock_pool", "name": "股票池", "file": "app/backtest/daily_scan.py / loader.py",
     "logic": "加载全市场股票池，粗筛剔除科创板/北交所；逐股 prepare_stock_data（前复权 adj_close），停牌检测跳过",
     "detail": "load_stock_pool() → 剔除 688/689.SH 与 .BJ；prepare_stock_data() 前复权+涨跌停标记；停牌 gap>5 自然日跳过；次新过滤 MIN_LIST_TRADING_DAYS=60",
     "key_params": [], "tunable": "可改：股票池过滤规则 / 停牌阈值 / 复权口径 / 次新天数"},
    {"stage": "form_identify", "name": "形态识别", "file": "app/backtest/form_classifier.py / surge_scanner.py",
     "logic": "classify_current 识别当前启动形态（A/B/C/D），须 confirmed 才进候选；E 连板（难买入）剔除",
     "detail": "判定优先级 E→C→B→A→D；A平台突破/B V型反转/C中继加速/D直接拉升/E连板/U无法归类；阈值见 FORM_TYPES",
     "key_params": [], "tunable": "可改：形态判定阈值（平台窗口/波动/缩量/突破/放量/前涨/前跌）"},
    {"stage": "feature_extract", "name": "特征提取", "file": "app/backtest/feature_extractor.py",
     "logic": "提取 59+ 维特征；VECTOR_FEATURES(33 维) 用固化 normalizer 归一化；CONDITION_FEATURES(~30 维) 保留原始值供条件打分",
     "detail": "extract_features(df, end_idx, ts_code, form_type, extra)；FEATURE_WINDOW=20；extra 由 build_extra_map() 从 21 张关联表加载 25 个增强特征（bps/股东人数/质押/事件/周月动量/大盘等，严格 t0 前已披露，无未来函数）",
     "key_params": [], "tunable": "可改：特征集 / 归一化 / 窗口"},
    {"stage": "pattern_match", "name": "正模式库相似度", "file": "app/backtest/vector_store.py (ChromaDB)",
     "logic": "归一化向量 query 同形态正模式库 top1 → pos_sim（主排序依据 similarity）",
     "detail": "store.query('success', form_type, vec_vec, top_k=1) → pos_sim；排序 key=-similarity；模式库集合 short_term_patterns_success/failure_A~D，33 维余弦相似度",
     "key_params": [], "tunable": "可改：相似度计算 / 模式库构建 / 召回 top_k"},
    {"stage": "neg_exclude", "name": "负向抑制", "file": "app/backtest/daily_scan.py",
     "logic": "query 失败模式库 top1 → neg_sim；综合概率 final_prob = prob * (1 - lambda * neg_sim)",
     "detail": "LAMBDA_EXCLUDE=daily_lambda_exclude(0.5)（可进化）；risk_level 按 neg_sim 分档",
     "key_params": ["daily_lambda_exclude"], "tunable": "可改：负向抑制强度 daily_lambda_exclude"},
    {"stage": "condition_score", "name": "条件概率打分", "file": "app/backtest/threshold_analyzer.py / condition_table.json",
     "logic": "CONDITION_FEATURES 原始值查条件概率表 → prob（历史统计参考分）+ 命中条件 hit_conditions",
     "detail": "score_by_probability_table：单特征命中(high/low方向) → 组合概率 1-Π(1-p_i)（独立条件近似，取前10条命中）→ 组合规则修正(取更高 up_prob) → 上限 min(prob,0.99)；未命中任何高胜率条件返回中性基线 NEUTRAL_BASELINE=0.5；条件表由 threshold_analyzer 按 MIN_SAMPLE=30 / 分位 / 信息增益切分构建",
     "key_params": [], "tunable": "可改：条件表重算 / 分位切分 / 命中阈值"},
    {"stage": "rule_signal", "name": "规则信号", "file": "app/backtest/rule_verifier.py / form_leaderboard.json",
     "logic": "detect_all 命中已验证看涨规则 → rule_prob（按 lift 加权 + 看跌抑制），hit_rules 供 Agent 精筛保送",
     "detail": "rule_pos = Σ(w_i*hit_rate_i)/Σw_i（w=lift）；看跌抑制 (1 - daily_rule_lambda*fail)；baseline=form_leaderboard.baseline_t1；23 条看涨规则见 RULE_FORMS（验证门槛：≥45% 且 ≥段内基线）",
     "key_params": ["daily_rule_lambda"], "tunable": "可改：规则验证门槛 / 看跌抑制 daily_rule_lambda"},
    {"stage": "risk_filter", "name": "风险过滤", "file": "app/backtest/risk_filter.py",
     "logic": "check_risk：ST / 涨停封死 / 高质押(>risk_pledge_max) / 流动性不足 / 次新 / 即将*ST → 剔除",
     "detail": "MIN_AVG_AMOUNT=50000千元(5000万)；MIN_LIST_TRADING_DAYS=60；PLEDGE_RISK_RATIO=risk_pledge_max(0.5)；st_risk_flag 剔除",
     "key_params": ["risk_pledge_max"], "tunable": "可改：质押上限 risk_pledge_max / 流动性下限 / 涨停剔除规则"},
    {"stage": "rank_select", "name": "排序TOP-K", "file": "app/backtest/daily_scan.py",
     "logic": "候选按 综合排序分（上涨概率为主，相似度加权）降序 → 取 TOP-K（daily_top_k）落盘 + 命中登记",
     "detail": "rank_score = up_probability×(1-α) + similarity×α（α=daily_rank_alpha，默认 0.3）；落盘 daily_YYYYMMDD_vN.json + record_hits(pred_prob, t5=None)",
     "key_params": ["daily_top_k", "daily_rank_alpha"], "tunable": "可改：榜单条数 daily_top_k / 上涨概率与相似度权重 daily_rank_alpha"},
    {"stage": "agent_refine", "name": "Agent精筛", "file": "app/agents/refine_agent.py / decision_agent.py",
     "logic": "三层漏斗（批量/深度/select）精筛最终榜单，结合政策/消息面/历史教训，可改序/删股",
     "detail": "run_agent_refine() 三层漏斗参数：批量粗筛 BATCH_SIZE=25(每批)/PER_INDUSTRY=10(每行业最多保留)→ 深度精筛(RefineAgent select/skip+score_adjust；含形态规则保送 locked)→ decision_agent 排序→ build_final_report；政策只解读频次最高 MAX_POLICY_SECTORS=5 行业；硬性兜底(利好出货→skip score_adjust=-20 / 烟雾弹→≤-5)；最终 decision_agent 排序（LLM final_ranks 或确定性降级 final = up_probability × (1 + score_adjust%)，select 优先，截取 Top-N）；三大信号：①量化画像 candidate_profiler(纯规则, 30+条件参数中文名+板块概念 concept/ths_member) ②政策催化 policy_agent(4子Agent: 要素抽取/产业链映射/利好分级/历史对标; Tavily 搜索板块政策 + policy_history 历史案例) ③消息面 news_agent(Tavily 实时搜索个股新闻/公告; 识别利好出货/烟雾弹/超跌反抽, news_gain_window 近N日涨幅) + attribution_kb / reflect_kb / condition_stat；产出 daily_YYYYMMDD_agent_vN.json",
     "key_params": [], "tunable": "可改：精筛逻辑 / 评分权重 / Prompt / 候选保送"},
    {"stage": "market", "name": "市场环境", "file": "-",
     "logic": "系统性风险（大盘普跌）导致推荐普跌，需对照指数过滤 / 择时",
     "detail": "环节归因 'market' 提示大盘拖累；当前无指数过滤，可新增",
     "key_params": [], "tunable": "可改：大盘过滤 / 择时 / 指数对照"},
]

# ════════════ 回测流水线（9 步，含关键实现细节）════════════
BACKTEST_PIPELINE = [
    {"step": "data_clean", "name": "数据清洗", "file": "app/backtest/loader.py / data_validator.py",
     "logic": "数据完整性校验 + 股票池 / 前复权 / 涨跌停标记 / 停牌处理；prepare_stock_data 供回测与每日推荐共用",
     "detail": "run_data_validation()；前复权 adj_close；SEAL_RATIO=0.997 封板判定(is_sealed_up/down)；停牌容忍 MAX_SUSPEND_RATIO=0.20；次新 MIN_LIST_TRADING_DAYS=60；流动性 MIN_AVG_AMOUNT=50000千元",
     "key_params": []},
    {"step": "form_surge", "name": "形态识别+启动段", "file": "app/backtest/surge_scanner.py / form_classifier.py / pattern_miner.py / failure_miner.py",
     "logic": "识别 3 倍启动段（与每日推荐同源），形态 A-E 分类；挖掘正/负模式；条件概率表在此采样",
     "detail": "scan_market（CANDIDATE_GAIN=2.5 预标记 / MAX_WINDOW=90 / NEIGHBOR=3 波谷）→ 3倍启动段；classify_batch → A-E；mine_positive_patterns / mine_failure_patterns",
     "key_params": []},
    {"step": "outcome_split", "name": "结局分流", "file": "app/backtest/outcome_tracker.py",
     "logic": "success = 启动后最高涨幅 ≥ outcome_success_gain 倍；failure_A/B、near、discard；过滤涨跌停/结局不完整等噪音",
     "detail": "SUCCESS_GAIN=outcome_success_gain(3.0)；NEAR=2.5；failure_A∈[0.5,1.5)且回撤>30%；failure_B=T+20<20%；filter_noisy_samples 噪音剔除：⑲T0封死不可买 / ⑬结局窗口截断未达3倍 / ⑥长期停牌后复牌跳空(>30自然日) / ⑨停牌过多(>20自然日) / ⑧复权异常(单日|涨跌幅|>25%)",
     "key_params": ["outcome_success_gain"]},
    {"step": "feature_fit", "name": "特征提取拟合", "file": "app/backtest/feature_extractor.py",
     "logic": "59+ 维特征；拟合 FeatureNormalizer（固化 feature_normalizer.json）供每日推荐归一化",
     "detail": "extract_features + FeatureNormalizer(zscore/分位)；save_normalizer → feature_normalizer.json",
     "key_params": []},
    {"step": "pattern_lib", "name": "模式库构建", "file": "app/backtest/vector_store.py",
     "logic": "正/负模式向量写入 ChromaDB（short_term_patterns），每日推荐召回相似度",
     "detail": "build_vector_store()：正样本(3倍成功)与同形态失败样本分别建库 short_term_patterns_success/failure_A~D（33 维余弦）",
     "key_params": []},
    {"step": "condition_table", "name": "条件概率表", "file": "app/backtest/threshold_analyzer.py",
     "logic": "条件参数按分位切分统计 → T+5 上涨率，产出 probability_table.json / condition_table.json",
     "detail": "build_probability_table / build_condition_table（未归一化原始值分位 / 信息增益切分；MIN_SAMPLE=30；未命中中性基线 NEUTRAL_BASELINE=0.5；UP_THRESHOLD=0.05 上涨判定）",
     "key_params": []},
    {"step": "backtest_ab", "name": "回测打分与A/B", "file": "app/backtest/runner.py / recommender.py / validator.py / metrics.py / attribution.py / cluster.py",
     "logic": "组合打分、T+5 收益与命中、A/B 决策门、归因、聚类；产出 baseline 供 monitor 月度漂移",
     "detail": "run_backtest 完整编排：run_data_validation → scan_market(3倍段) → classify_batch(A-E) → outcome 分流 → _build_feature_matrix(向量归一化 + 条件原始值) → build_condition_table / build_vector_store → mine_positive_patterns(POSITIVE_LABELS=success) → analyze_attribution(同形态簇内成功vs失败对比：连续特征 AUC/均值差异、离散特征信息增益；MIN_CLUSTER_SAMPLES=10；产出 attribution_kb: 特征判别力排行/成功画像/失败画像/组合规则) → compute_metrics(T+5命中/收益/盈亏比) → run_ab_validation(Walk-Forward 样本外 + A/B/C 三方案[A基线/B负模式库/C完整] + McNemar/配对t 显著性 + Go/No-Go 上线决策门；指标 hit_rate[T+5>0]/accuracy[T+5>5%]/t20_cont/double_confirm/avg_t5/max_drawdown) → cluster_by_form → entry_analyze；产出 BacktestResult(含 data_validation / ab_validation / clusters / entry_analysis)",
     "key_params": []},
    {"step": "rule_verify", "name": "规则形态验证", "file": "app/backtest/rule_verifier.py / rule_forms.py",
     "logic": "3 倍启动段内验证看涨形态规则 → form_leaderboard.json（每日推荐规则信号用）",
     "detail": "run_rule_verification(sample_mode='surge')；段内基线 T+1≈0.56~0.64；看涨规则需 ≥45% 且 ≥段内基线；规则清单见 RULE_FORMS（23 条网络经典形态）",
     "key_params": []},
    {"step": "entry_guide", "name": "入场指引", "file": "app/backtest/entry_analyzer.py",
     "logic": "成功组各形态最佳入场方式 → entry_guidance.json",
     "detail": "analyze_batch / aggregate_entry_results / save_entry_guidance",
     "key_params": []},
]

# ════════════ 形态定义（A-E/U）════════════
FORM_TYPES = [
    {"type": "A", "name": "平台突破型", "desc": "底部横盘 20~60 日后放量突破（核心形态）",
     "thresholds": "平台波动<25% 且 缩量<80%均量；突破 close>平台高点*1.03 且 放量>20日均量*1.5"},
    {"type": "B", "name": "V型反转型", "desc": "快速下跌后 V 型反转（无平台）",
     "thresholds": "前 20 日快速下跌>25%（PRIOR_DROP=0.25）"},
    {"type": "C", "name": "中继加速型", "desc": "第一波上涨后平台整理再加速",
     "thresholds": "前一波上涨>50%（PRIOR_RISE=0.5）"},
    {"type": "D", "name": "直接拉升型", "desc": "兜底形态，无明显平台/下跌/前涨，放量大阳拉升",
     "thresholds": "10 日内涨幅>30%（DIRECT_RISE_10D=0.3）"},
    {"type": "E", "name": "连板型", "desc": "连续涨停/一字板启动（难买入，每日推荐剔除）",
     "thresholds": "涨停日占比>50%（LIMIT_UP_RATIO=0.5）"},
    {"type": "U", "name": "无法归类", "desc": "未知形态，进样本库但不参与同形态归因/聚类", "thresholds": "-"},
]

# ════════════ 特征体系（59+ 维）════════════
FEATURE_LAYOUT = {
    "total": "59+ 维（回测与每日推荐同源）",
    "vector": {
        "n": 33,
        "desc": "形态向量（归一化 + 相似度匹配）",
        "groups": "20 价格图形(pct_1..pct_20) + 8 技术(ma5_above_ma10 / ma10_above_ma20 / macd_hist / rsi_14 / kdj_k / boll_pos / atr_pct / adx) + 5 量能(volume_ratio / vol_ma_change / vol_surge_days / turnover_rate / vol_pct_20)",
    },
    "condition": {
        "n": "~30",
        "desc": "条件参数（原始值，分位统计，不归一化，供条件概率打分）",
        "groups": "资金(net_amount_ratio / large_order_ratio / amount_ratio) + 基本(roe / gross_margin / profit_growth / pe_percentile / pb_percentile / revenue_growth / debt_ratio / ocf_ps / mainbz_concentration) + 筹码(holder_change / top10_ratio / pledge_ratio / bps每股净资产 / avg_hold_amount人均持股 / holder_concentration筹码集中度) + ST风险(st_risk_flag) + 事件(event_forecast业绩预告 / event_express快报 / event_dividend分红 / event_repurchase回购 / event_rewards股权激励 / event_holdertrade股东增减持 / event_block大宗折溢价) + 市场(market_env / wk_trend周动量 / mo_trend月动量 / is_ever_st历史ST)",
    },
}

# ════════════ 关键常量（含可进化映射）════════════
KEY_CONSTANTS = [
    {"name": "FEATURE_WINDOW", "value": 20, "file": "feature_extractor.py", "desc": "特征窗口（前 20 交易日）"},
    {"name": "MIN_LIST_TRADING_DAYS", "value": 60, "file": "loader.py", "desc": "次新过滤（上市<60 交易日剔除）"},
    {"name": "MIN_AVG_AMOUNT", "value": "50000 千元(5000万)", "file": "loader.py", "desc": "流动性下限"},
    {"name": "BREAKOUT_PCT", "value": 1.03, "file": "form_classifier.py", "desc": "有效突破阈值"},
    {"name": "BREAKOUT_VOL_RATIO", "value": 1.5, "file": "form_classifier.py", "desc": "突破放量倍数"},
    {"name": "PRIOR_DROP / PRIOR_RISE", "value": "0.25 / 0.50", "file": "form_classifier.py", "desc": "V反转前跌 / 中继前涨"},
    {"name": "LAMBDA_EXCLUDE", "value": "daily_lambda_exclude(0.5)", "file": "daily_scan.py", "desc": "负向抑制强度（可进化）"},
    {"name": "DEFAULT_TOP_K", "value": "daily_top_k(30)", "file": "daily_scan.py", "desc": "榜单条数（可进化）"},
    {"name": "LAMBDA_NEG_RULE", "value": "daily_rule_lambda(0.3)", "file": "daily_scan.py", "desc": "看跌规则抑制（可进化）"},
    {"name": "SUCCESS_GAIN", "value": "outcome_success_gain(3.0)", "file": "outcome_tracker.py", "desc": "回测成功=3倍（可进化）"},
    {"name": "HIT_RET_THRESHOLD", "value": "hit_threshold(0.05)", "file": "monitor.py", "desc": "T+5 命中阈值（可进化）"},
    {"name": "PLEDGE_RISK_RATIO", "value": "risk_pledge_max(0.5)", "file": "risk_filter.py", "desc": "高质押上限（可进化）"},
    {"name": "SEAL_RATIO", "value": 0.997, "file": "loader.py", "desc": "封板判定（触及涨停价 99.7% 视为封死不可买）"},
    {"name": "MAX_SUSPEND_RATIO", "value": 0.20, "file": "loader.py", "desc": "停牌容忍（特征/结局窗口内停牌占比>20% 剔除）"},
    {"name": "CANDIDATE_GAIN", "value": 2.5, "file": "surge_scanner.py", "desc": "3倍段预标记候选（≥2.5 倍，防复权误差漏检）"},
    {"name": "MAX_WINDOW", "value": 90, "file": "surge_scanner.py", "desc": "启动段最大跨度（交易日）"},
    {"name": "NEIGHBOR", "value": 3, "file": "surge_scanner.py", "desc": "有效局部低点前后验证日数"},
    {"name": "MIN_SAMPLE", "value": 30, "file": "threshold_analyzer.py", "desc": "条件概率表最小样本量（防小样本偶然）"},
    {"name": "UP_THRESHOLD", "value": 0.05, "file": "threshold_analyzer.py", "desc": "条件表上涨判定（T+5 涨幅>5%）"},
    {"name": "NEUTRAL_BASELINE", "value": 0.5, "file": "threshold_analyzer.py", "desc": "未命中条件的中性基线（防虚高）"},
    {"name": "NEG_HIGH_THRESHOLD", "value": 0.75, "file": "recommender.py", "desc": "负模式相似度高风险分档（>0.75 高 / 0.6~0.75 中）"},
]

# ════════════ 规则形态库（23 条网络经典看涨规则，rule_forms.py，每日推荐规则信号）════════
RULE_FORMS = [
    "老鸭头(laoyatou)", "仙人指路(xianren)", "出水芙蓉(chushuifurong)", "红三兵(hongsanbing)",
    "多方炮(duofangpao)", "早晨之星(zaochenzhixing)", "金针探底(jinzhentandi)", "双底/W底(shuangdi)",
    "头肩底(toujianbe)", "均线多头排列(duotoupailie)", "缩量回踩(suolianghuicai)", "塔形底(taxingdi)",
    "三重底(sanchongdi)", "圆弧底(yuanhudi)", "放量突破(fangliangtupo)", "量价齐升(liangjiaqisheng)",
    "金山谷(jinshangu)", "海底捞月(haidilaoyue)", "倒锤头线(daochuizhui)", "曙光初现(shuguang)",
    "看涨吞没(kantunmo)", "三阳开泰(sanyangguan)", "上升三法(shangsanshi)",
]

# ════════════ 可改锚点清单（进化大脑直接改代码的精确定位，供 code_writer 参考）════════
CHANGE_ANCHORS = [
    {"key": "neg_exclude_formula", "file": "app/backtest/daily_scan.py",
     "anchor_old": 'final_prob = prob * (1.0 - _evolve_param("daily_lambda_exclude", LAMBDA_EXCLUDE) * neg_sim)',
     "desc": "负向抑制综合概率公式（负模式相似度压低）", "how": "可改概率融合权重/引入 pos_sim/加动量因子"},
    {"key": "rank_sort", "file": "app/backtest/daily_scan.py",
     "anchor_old": 'candidates.sort(key=_rank_key, reverse=True)',
     "desc": "每日推荐主排序（综合排序分=上涨概率为主、相似度加权，α=daily_rank_alpha）",
     "how": "可改排序键/权重：如调高 daily_rank_alpha 更重形态相似度，调低更重上涨概率"},
    {"key": "top_k", "file": "app/backtest/daily_scan.py",
     "anchor_old": 'top_k = int(_evolve_param("daily_top_k", DEFAULT_TOP_K) or DEFAULT_TOP_K)',
     "desc": "榜单条数 TOP-K", "how": "可改条数/按命中率动态"},
    {"key": "breakout_pct", "file": "app/backtest/form_classifier.py",
     "anchor_old": "BREAKOUT_PCT = 1.03",
     "desc": "A 形态有效突破阈值", "how": "调高=更严格（减少假突破）/调低=更宽松"},
    {"key": "success_gain", "file": "app/backtest/outcome_tracker.py",
     "anchor_old": "SUCCESS_GAIN = 3.0",
     "desc": "回测'成功=启动后最高涨幅≥3 倍'", "how": "已登记 outcome_success_gain 锚点补丁，可进化"},
    {"key": "neutral_baseline", "file": "app/backtest/threshold_analyzer.py",
     "anchor_old": "NEUTRAL_BASELINE = 0.5",
     "desc": "未命中条件的中性基线（防虚高）", "how": "调高=更严格（未命中条件普遍不给高分）"},
    {"key": "min_sample", "file": "app/backtest/threshold_analyzer.py",
     "anchor_old": "MIN_SAMPLE = 30",
     "desc": "条件概率表最小样本量", "how": "调高=更稳防偶然/调低=覆盖更多条件"},
    {"key": "seal_ratio", "file": "app/backtest/loader.py",
     "anchor_old": "SEAL_RATIO = 0.997",
     "desc": "涨停封板判定阈值", "how": "越接近 1 越严格（更多涨停视为不可买）"},
    {"key": "hit_threshold", "file": "app/backtest/monitor.py",
     "anchor_old": "HIT_RET_THRESHOLD = 0.05",
     "desc": "T+5 命中阈值", "how": "已登记 hit_threshold 热生效，可进化"},
    {"key": "rule_lambda", "file": "app/backtest/daily_scan.py",
     "anchor_old": 'lambda_neg=_evolve_param("daily_rule_lambda", LAMBDA_NEG_RULE),',
     "desc": "看跌规则负向抑制强度", "how": "已登记 daily_rule_lambda 热生效，可进化"},
]

# ════════════ 绩效指标（metrics.py，回测评估口径）════════════
PERFORMANCE_METRICS = (
    "win_rate(胜率) / hit_rate(T+5>0%) / accuracy(T+5>5%) / t20_continuation(T+20>0) / "
    "double_confirm(T+5>5% 且 T+20>0) / avg_t5_return / profit_loss_ratio(盈亏比) / "
    "cum_return(累计) / annual_return(年化) / max_drawdown(最大回撤) / sharpe(夏普) / "
    "exclude_effectiveness(负模式排除有效度)；TRADING_DAYS=252 / RISK_FREE=0.02"
)

# ════════════ 知识库/软知识（Agent 精筛与进化依据）════════════
KNOWLEDGE_BASES = [
    {"file": "data/decision_kb.json", "desc": "决策层教训（最终榜单失败反思）"},
    {"file": "data/reflect_kb.json", "desc": "个股反思教训（T+5 失败）"},
    {"file": "data/news_lesson_kb.json", "desc": "消息面反思教训"},
    {"file": "data/policy_impact_kb.json", "desc": "政策板块实证（T+1/T+5/T+20）"},
    {"file": "data/policy_intent_kb.json", "desc": "政策意图库（自学校正）"},
    {"file": "data/policy_history.json", "desc": "历史政策→板块案例库（policy_agent 历史对标分析）"},
    {"file": "data/attribution_kb.json", "desc": "回测归因知识"},
    {"file": "data/verify_kb.json", "desc": "次日验证 + 环节归因"},
    {"file": "data/policy_kb.sqlite", "desc": "政策 RAG 索引"},
    {"file": "data/evolution_events.db", "desc": "全项目事件流（进化依据）"},
    {"file": "data/monitor_state.json", "desc": "推荐命中 + 月度漂移"},
    {"file": "data/evolution_ledger.json", "desc": "进化终身记账"},
]

# ════════════ 回测产物（每日推荐依赖的固化资产）════════════
PRODUCTS = [
    {"file": "data/feature_normalizer.json", "desc": "59维特征归一化参数（回测拟合，每日推荐固化使用）"},
    {"file": "data/probability_table.json", "desc": "条件概率表（上涨概率）"},
    {"file": "data/condition_table.json", "desc": "采样条件参数分位统计（条件打分）"},
    {"file": "data/form_leaderboard.json", "desc": "已验证看涨形态规则排行榜（规则信号）"},
    {"file": "data/entry_guidance.json", "desc": "各形态入场指引"},
    {"file": "data/evolution_config.json", "desc": "可进化参数表（进化大脑可改，热生效/锚点补丁）"},
    {"file": "data/monitor_state.json", "desc": "推荐命中与月度漂移监控"},
    {"file": "data/verify_kb.json", "desc": "次日自我验证与环节归因知识库"},
    {"file": "data/code_writer_state.json", "desc": "生成式代码进化历史/待确认提案"},
    {"file": "data/evolution_map.json", "desc": "本系统图谱快照"},
    {"file": "data/daily_recommend/", "desc": "每日推荐榜单目录（daily_YYYYMMDD_vN.json 规则版 + _agent_vN.json Agent 最终版）"},
    {"file": "data/backtest_results/", "desc": "回测结果目录（backtest_YYYYMMDD_HHMMSS.json + backtest_latest.json）"},
]

# ════════════ 运行时序（每日自动任务）════════════
# 信号滞后口径（plans/19）：每日推荐基于 D 日收盘晚上生成，实际 D+1 日买入；
# 验证/反思收益一律按"买入后持有 N 日"（t1=D+2 vs D+1、t5=D+6 vs D+1、t20=D+21 vs D+1）。
RUN_SEQUENCE = [
    {"time": "12:05~13:35", "task": "进化任务窗口（守护/影子/惰性/代码进化/审批/每周应用/统一学习）",
     "note": "全部经 _evolution_idle_check，后台有 回测/扫描/采集/反思 时跳过不运行"},
    {"time": "12:10", "task": "次日自我验证（daily_verify，次日验证前天）",
     "note": "T+1/T+5/T+20 回填（D+1 买入口径）+ 失效归因 + 环节归因（含回测侧 backtest_outcome/feature/pattern/condition/normalizer/product）→ verify_kb"},
    {"time": "16:30", "task": "数据采集（阶段1-4）+ 自动每日推荐扫描（daily_scan → TOP-K 落盘）",
     "note": "后台忙则不触发；扫描完成后自动 Agent 精筛 → 最终榜单 daily_YYYYMMDD_agent_vN.json"},
]


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def build_map() -> dict:
    """实时构建详细系统图谱（随进化参数/产物变化而更新）。"""
    from app.agents import evolution_config
    from app.agents import daily_verify

    params = evolution_config.params_table()

    def _pv(name, default=None):
        p = params.get(name, {})
        return p.get("current", p.get("default", default))

    daily = []
    for p in DAILY_PIPELINE:
        daily.append({**p, "params_now": {k: _pv(k) for k in p.get("key_params", [])}})
    backtest = []
    for b in BACKTEST_PIPELINE:
        backtest.append({**b, "params_now": {k: _pv(k) for k in b.get("key_params", [])}})

    evo_params = [
        {"name": k, "current": v.get("current", v.get("default")),
         "min": v.get("min"), "max": v.get("max"), "step": v.get("step"),
         "tier": v.get("tier"), "hot_reload": v.get("hot_reload"),
         "file": v.get("file"), "anchor_old": v.get("anchor_old"),
         "desc": v.get("desc", "")}
        for k, v in params.items()
    ]

    stage_map = dict(getattr(daily_verify, "STAGE_MAP", {}))
    evolvable: list = []
    try:
        from app.agents import code_writer
        evolvable = list(getattr(code_writer, "EVOLVABLE_FILES", []))
    except Exception:  # noqa: BLE001
        pass

    products = []
    for pr in PRODUCTS:
        path = os.path.join(DATA_DIR, os.path.basename(pr["file"]))
        products.append({**pr, "exists": os.path.exists(path)})
    kbs = []
    for kb in KNOWLEDGE_BASES:
        path = os.path.join(DATA_DIR, os.path.basename(kb["file"]))
        kbs.append({**kb, "exists": os.path.exists(path)})

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "goal": GOAL,
        "daily_recommend_pipeline": daily,
        "backtest_pipeline": backtest,
        "evolution_params": evo_params,
        "stage_file_map": stage_map,
        "evolvable_files": evolvable,
        "form_types": FORM_TYPES,
        "rule_forms": RULE_FORMS,
        "change_anchors": CHANGE_ANCHORS,
        "performance_metrics": PERFORMANCE_METRICS,
        "feature_layout": FEATURE_LAYOUT,
        "key_constants": KEY_CONSTANTS,
        "knowledge_bases": kbs,
        "products": products,
        "run_sequence": RUN_SEQUENCE,
    }


def build_map_txt(short: bool = True) -> str:
    """生成进化大脑可读的系统图谱文本。

    short=True：精简（流水线逻辑 + 参数当前值 + 形态/特征/常量概要）；
    short=False：完整（含 detail/知识库/产物/时序）。
    """
    m = build_map()
    lines = [f"【系统图谱 {m['generated_at']}】目标={m['goal']}"]

    lines.append("\n【每日推荐流水线】")
    for p in m["daily_recommend_pipeline"]:
        pn = " ".join(f"{k}={v}" for k, v in (p.get("params_now") or {}).items())
        seg = (f"- {p['name']}({p['stage']}): {p['logic'][:70]}"
               f"；文件:{p['file']}" + (f"；参数:{pn}" if pn else ""))
        if not short and p.get("detail"):
            seg += f"\n    ↳ 实现: {p['detail']}；可调:{p['tunable']}"
        lines.append(seg)

    lines.append("\n【回测流水线】")
    for b in m["backtest_pipeline"]:
        pn = " ".join(f"{k}={v}" for k, v in (b.get("params_now") or {}).items())
        seg = (f"- {b['name']}({b['step']}): {b['logic'][:70]}"
               f"；文件:{b['file']}" + (f"；参数:{pn}" if pn else ""))
        if not short and b.get("detail"):
            seg += f"\n    ↳ 实现: {b['detail']}"
        lines.append(seg)

    lines.append("\n【可进化参数】")
    for e in m["evolution_params"]:
        lines.append(f"- {e['name']}={e['current']} range=[{e['min']},{e['max']}]"
                     f" tier{e.get('tier','?')} hot={e.get('hot_reload')} — {e['desc']}")

    lines.append("\n【形态定义】")
    for f_ in m["form_types"]:
        lines.append(f"- {f_['type']} {f_['name']}: {f_['desc']}；判定: {f_['thresholds']}")

    v = m["feature_layout"]["vector"]
    c = m["feature_layout"]["condition"]
    lines.append(f"\n【特征体系】形态向量 {v['n']} 维: {v['groups']}")
    lines.append(f"条件参数 {c['n']} 维: {c['groups']}")

    lines.append(f"\n【规则形态库】{len(m['rule_forms'])} 条看涨规则: " + "、".join(m["rule_forms"]))

    lines.append(f"\n【可改锚点（code_writer 直接修改代码用）】{len(m['change_anchors'])} 处:")
    for ca in m["change_anchors"]:
        lines.append(f"- {ca['desc']} ({ca['file']}) 锚点: {ca['anchor_old'][:70]}… → 改法: {ca['how']}")

    lines.append(f"\n【绩效指标】{m['performance_metrics']}")

    if not short:
        lines.append("\n【关键常量】")
        for kc in m["key_constants"]:
            lines.append(f"- {kc['name']}={kc['value']} ({kc['file']}): {kc['desc']}")
        lines.append("\n【知识库】")
        for kb in m["knowledge_bases"]:
            lines.append(f"- {kb['file']}: {kb['desc']}（{'存在' if kb['exists'] else '缺失'}）")
        lines.append("\n【回测产物】")
        for pr in m["products"]:
            lines.append(f"- {pr['file']}: {pr['desc']}（{'存在' if pr['exists'] else '缺失'}）")
        lines.append("\n【运行时序】")
        for rs in m["run_sequence"]:
            lines.append(f"- {rs['time']}: {rs['task']}（{rs['note']}）")
    return "\n".join(lines)


def save_map() -> dict:
    """写图谱快照 evolution_map.json（供前端/审计查看当前图谱）。"""
    m = build_map()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger("quant").warning(f"[system_map] 图谱快照写入失败: {exc}")
    return m


__all__ = ["build_map", "build_map_txt", "save_map", "MAP_FILE", "GOAL"]
