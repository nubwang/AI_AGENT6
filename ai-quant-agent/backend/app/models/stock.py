"""股票数据 SQLAlchemy 模型（42张表）"""
from sqlalchemy import (Column, String, Date, BigInteger, Integer, Text, DateTime,
                        Index, func)
from sqlalchemy.types import DECIMAL
Decimal = DECIMAL
from app.models import Base


class StockBasic(Base):
    """股票基本信息"""
    __tablename__ = "stock_basic"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), unique=True, nullable=False, comment="股票代码")
    symbol = Column(String(10), nullable=False, comment="数字代码")
    name = Column(String(50), nullable=False, comment="股票名称")
    area = Column(String(20), comment="地区")
    industry = Column(String(50), comment="所属行业")
    market = Column(String(20), comment="市场")
    list_date = Column(Date, comment="上市日期")
    delist_date = Column(Date, comment="退市日期")
    is_hs = Column(String(10), comment="沪深港通标的")
    created_at = Column(DateTime, server_default=func.now())


class TradeCal(Base):
    """交易日历"""
    __tablename__ = "trade_cal"
    id = Column(Integer, primary_key=True, autoincrement=True)
    exchange = Column(String(10), nullable=False, comment="交易所")
    cal_date = Column(Date, nullable=False, comment="日历日期")
    is_open = Column(Integer, nullable=False, comment="是否交易日")
    pretrade_date = Column(Date, comment="前一个交易日")


class StockCompany(Base):
    """上市公司信息"""
    __tablename__ = "stock_company"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), unique=True, nullable=False)
    chairman = Column(String(50))
    manager = Column(String(50))
    secretary = Column(String(50))
    reg_capital = Column(Decimal(21, 4))
    setup_date = Column(Date)
    province = Column(String(20))
    city = Column(String(30))
    introduction = Column(Text)
    website = Column(String(100))
    employees = Column(Integer)
    main_business = Column(Text)


class NameChange(Base):
    """股票曾用名"""
    __tablename__ = "namechange"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    name = Column(String(50), nullable=False)
    start_date = Column(Date)
    end_date = Column(Date)
    ann_date = Column(Date)
    change_reason = Column(String(50))


class NewShare(Base):
    """新股上市"""
    __tablename__ = "new_share"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), unique=True, nullable=False)
    sub_code = Column(String(20))
    name = Column(String(50))
    ipo_date = Column(Date)
    issue_date = Column(Date)
    amount = Column(Decimal(21, 4))
    market_amount = Column(Decimal(21, 4))
    price = Column(Decimal(12, 4))
    pe = Column(Decimal(10, 4))
    limit_amount = Column(Decimal(12, 4))
    fund = Column(Decimal(21, 4))


class Daily(Base):
    """日线行情"""
    __tablename__ = "daily"
    ts_code = Column(String(20), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    open = Column(Decimal(12, 4))
    high = Column(Decimal(12, 4))
    low = Column(Decimal(12, 4))
    close = Column(Decimal(12, 4))
    pre_close = Column(Decimal(12, 4))
    change = Column(Decimal(12, 4))
    pct_chg = Column(Decimal(10, 4))
    vol = Column(BigInteger)
    amount = Column(Decimal(24, 4))


class Weekly(Base):
    """周线行情"""
    __tablename__ = "weekly"
    ts_code = Column(String(20), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    open = Column(Decimal(12, 4))
    high = Column(Decimal(12, 4))
    low = Column(Decimal(12, 4))
    close = Column(Decimal(12, 4))
    vol = Column(BigInteger)
    amount = Column(Decimal(24, 4))


class Monthly(Base):
    """月线行情"""
    __tablename__ = "monthly"
    ts_code = Column(String(20), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    open = Column(Decimal(12, 4))
    high = Column(Decimal(12, 4))
    low = Column(Decimal(12, 4))
    close = Column(Decimal(12, 4))
    vol = Column(BigInteger)
    amount = Column(Decimal(24, 4))


class AdjFactor(Base):
    """复权因子

    ⚠️ **2026-09-21 与真实表逐项核对 —— 本模型曾与表不符，并因此导致过一次真实事故**：

      · `trade_date` 真实类型是 **`varchar(10)` 且存 `'YYYYMMDD'`（不带横线）**，
        **不是 `Date`** —— 按 DATE 语义传 `'2026-09-17'` 会写进**格式不同的行**，
        而 `= '20260918'` 这类查询又**看不到**它们（事故经过见 plans/24 §11.30）；
      · 真实 PRIMARY KEY 是**自增 `id`**，不是 `(ts_code, trade_date)`；
        但自 2026-09-21 起 `(ts_code, trade_date)` 上有 **UNIQUE `uk_ts_date`**，
        所以 `INSERT ... ON DUPLICATE KEY UPDATE` 现在**才真正幂等**。

    ⇒ 写库请一律用 **`'YYYYMMDD'` 字符串**（`app/data/adjust.py` 就是这么做的）。
    ⚠️ 遗留（未修，另行登记）：本模型仍声明复合主键、缺 `id` 列，
       因此 `scripts/generate_init_sql.py` 生成的 DDL 与线上表**仍不一致**（既有问题）。
    """
    __tablename__ = "adj_factor"
    # ★ 补上真实存在的自增主键 `id`（2026-09-21）：线上表的 PRIMARY KEY 是它，
    #   `(ts_code, trade_date)` 只是 UNIQUE `uk_ts_date`。之前模型缺这一列，
    #   会让 `generate_init_sql.py` 产出的 DDL 与线上表不一致（§11.30 登记的遗留项，已修）。
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False)
    # ★ 曾是 Column(Date)：与线上 varchar(10)('YYYYMMDD') 不符，已修正为字符串
    trade_date = Column(String(10), nullable=False)
    adj_factor = Column(Decimal(21, 6), nullable=False)


class StkLimit(Base):
    """每日涨跌停"""
    __tablename__ = "stk_limit"
    # ★ 2026-09-23：必须保留以 trade_date 为**前导**的索引。
    #   其它索引（uk_stk_limit_ts_date / idx_stk_limit_fixed_code_date）都以 ts_code 打头，
    #   缺这个索引时 `SELECT MAX(trade_date) FROM stk_limit` 只能全索引扫描 1690 万行（实测 43s），
    #   而它正是进化中心 L0 数据链路探针的查询 → `/system/evolve/status` 冷启动 38~73s
    #   → 前端整页 axios 30s 超时。线上已补建（scripts/migrate_stk_limit_date_index.py）。
    __table_args__ = (Index("idx_stk_limit_date", "trade_date"),)
    ts_code = Column(String(20), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    up_limit = Column(Decimal(12, 4))
    down_limit = Column(Decimal(12, 4))


class SuspendD(Base):
    """停复牌"""
    __tablename__ = "suspend_d"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    trade_date = Column(Date, nullable=False)
    suspend_flg = Column(String(10), nullable=False)
    suspend_type = Column(String(30))
    ann_date = Column(Date)


class DailyBasic(Base):
    """每日指标"""
    __tablename__ = "daily_basic"
    ts_code = Column(String(20), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    turnover_rate = Column(Decimal(10, 4))
    turnover_rate_f = Column(Decimal(10, 4))
    volume_ratio = Column(Decimal(10, 4))
    pe = Column(Decimal(12, 4))
    pe_ttm = Column(Decimal(12, 4))
    pb = Column(Decimal(12, 4))
    ps = Column(Decimal(12, 4))
    ps_ttm = Column(Decimal(12, 4))
    dv_ratio = Column(Decimal(10, 4))
    dv_ttm = Column(Decimal(10, 4))
    total_share = Column(BigInteger)
    float_share = Column(BigInteger)
    free_share = Column(BigInteger)
    total_mv = Column(Decimal(24, 4))
    circ_mv = Column(Decimal(24, 4))


class Moneyflow(Base):
    """资金流向"""
    __tablename__ = "moneyflow"
    ts_code = Column(String(20), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    buy_amount = Column(Decimal(24, 4))
    buy_vol = Column(BigInteger)
    sell_amount = Column(Decimal(24, 4))
    sell_vol = Column(BigInteger)
    net_amount = Column(Decimal(24, 4))
    net_vol = Column(BigInteger)
    amount_lt = Column(Decimal(24, 4))
    vol_lt = Column(BigInteger)
    net_amount_lt = Column(Decimal(24, 4))
    amount_mt = Column(Decimal(24, 4))
    vol_mt = Column(BigInteger)
    net_amount_mt = Column(Decimal(24, 4))
    amount_st = Column(Decimal(24, 4))
    vol_st = Column(BigInteger)
    net_amount_st = Column(Decimal(24, 4))


class BlockTrade(Base):
    """大宗交易"""
    __tablename__ = "block_trade"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    trade_date = Column(Date, nullable=False)
    price = Column(Decimal(12, 4))
    vol = Column(Decimal(21, 4))
    amount = Column(Decimal(24, 4))
    buyer = Column(String(100))
    seller = Column(String(100))


class Income(Base):
    """利润表"""
    __tablename__ = "income"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    ann_date = Column(Date)
    f_ann_date = Column(Date)
    end_date = Column(Date, nullable=False)
    report_type = Column(Integer)
    comp_type = Column(Integer)
    revenue = Column(Decimal(24, 4))
    operate_profit = Column(Decimal(24, 4))
    total_profit = Column(Decimal(24, 4))
    n_income = Column(Decimal(24, 4))
    n_income_attr_p = Column(Decimal(24, 4))
    basic_eps = Column(Decimal(12, 4))
    diluted_eps = Column(Decimal(12, 4))
    update_flag = Column(String(10))


class Balancesheet(Base):
    """资产负债表"""
    __tablename__ = "balancesheet"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    end_date = Column(Date, nullable=False)
    report_type = Column(Integer)
    comp_type = Column(Integer)
    total_assets = Column(Decimal(24, 4))
    total_liab = Column(Decimal(24, 4))
    total_hldr_eqy = Column(Decimal(24, 4))
    total_lse = Column(Decimal(24, 4))
    total_equity = Column(Decimal(24, 4))
    update_flag = Column(String(10))


class Cashflow(Base):
    """现金流量表"""
    __tablename__ = "cashflow"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    end_date = Column(Date, nullable=False)
    report_type = Column(Integer)
    comp_type = Column(Integer)
    n_inc_oper_act = Column(Decimal(24, 4))
    n_inc_inves_act = Column(Decimal(24, 4))
    n_inc_fnc_act = Column(Decimal(24, 4))
    net_cash_flows = Column(Decimal(24, 4))
    update_flag = Column(String(10))


class FinaIndicator(Base):
    """财务指标"""
    __tablename__ = "fina_indicator"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    ann_date = Column(Date)
    end_date = Column(Date, nullable=False)
    roe = Column(Decimal(10, 4))
    roe_dt = Column(Decimal(10, 4))
    roa = Column(Decimal(10, 4))
    debt_to_assets = Column(Decimal(10, 4))
    eps = Column(Decimal(12, 4))
    bps = Column(Decimal(12, 4))
    ocfps = Column(Decimal(12, 4))
    profit_dedt = Column(Decimal(24, 4))
    gross_margin = Column(Decimal(10, 4))
    profit_margin = Column(Decimal(10, 4))


class FinaMainbz(Base):
    """主营业务构成"""
    __tablename__ = "fina_mainbz"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    end_date = Column(Date, nullable=False)
    bz_item = Column(String(100))
    bz_sales = Column(Decimal(24, 4))
    bz_cost = Column(Decimal(24, 4))
    bz_profit = Column(Decimal(24, 4))
    bz_sales_ratio = Column(Decimal(10, 4))


class Forecast(Base):
    """业绩预告"""
    __tablename__ = "forecast"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    ann_date = Column(Date, nullable=False)
    end_date = Column(Date)
    type = Column(String(20))
    p_change_min = Column(Decimal(10, 4))
    p_change_max = Column(Decimal(10, 4))
    net_profit_min = Column(Decimal(24, 4))
    net_profit_max = Column(Decimal(24, 4))


class Express(Base):
    """业绩快报"""
    __tablename__ = "express"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    ann_date = Column(Date, nullable=False)
    end_date = Column(Date)
    revenue = Column(Decimal(24, 4))
    operate_profit = Column(Decimal(24, 4))
    total_profit = Column(Decimal(24, 4))
    n_income = Column(Decimal(24, 4))
    total_assets = Column(Decimal(24, 4))
    basic_eps = Column(Decimal(12, 4))


class StkHoldernumber(Base):
    """股东人数"""
    __tablename__ = "stk_holdernumber"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    ann_date = Column(Date, nullable=False)
    end_date = Column(Date)
    holder_num = Column(BigInteger)


class Top10Holders(Base):
    """前十大股东"""
    __tablename__ = "top10_holders"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    ann_date = Column(Date)
    end_date = Column(Date, nullable=False)
    holder_name = Column(String(100), nullable=False)
    hold_amount = Column(BigInteger)
    hold_ratio = Column(Decimal(10, 4))
    hold_float_ratio = Column(Decimal(10, 4))


class Top10Floatholders(Base):
    """前十大流通股东"""
    __tablename__ = "top10_floatholders"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    ann_date = Column(Date)
    end_date = Column(Date, nullable=False)
    holder_name = Column(String(100), nullable=False)
    hold_amount = Column(BigInteger)
    hold_ratio = Column(Decimal(10, 4))
    hold_float_ratio = Column(Decimal(10, 4))


class StkHoldertrade(Base):
    """股东增减持"""
    __tablename__ = "stk_holdertrade"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    ann_date = Column(Date, nullable=False)
    holder_name = Column(String(100), nullable=False)
    holder_type = Column(String(20))
    trade_type = Column(String(4), nullable=False)
    change_vol = Column(BigInteger)
    change_ratio = Column(Decimal(10, 6))
    begin_date = Column(Date)
    end_date = Column(Date)
    price_avg = Column(Decimal(12, 4))


class Repurchase(Base):
    """股票回购"""
    __tablename__ = "repurchase"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    ann_date = Column(Date, nullable=False)
    end_date = Column(Date)
    progress = Column(String(20))
    exp_date = Column(Date)
    amount_min = Column(Decimal(24, 4))
    amount_max = Column(Decimal(24, 4))
    vol_min = Column(BigInteger)
    vol_max = Column(BigInteger)
    price_min = Column(Decimal(12, 4))
    price_max = Column(Decimal(12, 4))


class StkRewards(Base):
    """股权激励"""
    __tablename__ = "stk_rewards"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    ann_date = Column(Date)
    end_date = Column(Date)
    exe_mode = Column(String(50))
    rewards_type = Column(String(50))
    gprice = Column(Decimal(12, 4))
    volume = Column(BigInteger)
    stk_name = Column(String(100))
    mark = Column(Text)


class Dividend(Base):
    """分红送配"""
    __tablename__ = "dividend"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    ann_date = Column(Date)
    imp_ann_date = Column(Date)
    record_date = Column(Date)
    ex_date = Column(Date, index=True)
    pay_date = Column(Date)
    div_proc = Column(String(20))
    cash_div = Column(Decimal(12, 4))
    cash_div_tax = Column(Decimal(12, 4))
    stk_div = Column(Decimal(12, 4))
    stk_bo = Column(Decimal(12, 4))
    share_tran = Column(Decimal(12, 4))


class Concept(Base):
    """概念板块"""
    __tablename__ = "concept"
    code = Column(String(20), primary_key=True)
    name = Column(String(100), nullable=False)
    src = Column(String(20))


class ConceptDetail(Base):
    """概念成分股"""
    __tablename__ = "concept_detail"
    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(20), nullable=False, index=True)
    ts_code = Column(String(20), nullable=False, index=True)
    name = Column(String(50))
    in_date = Column(Date)
    out_date = Column(Date)
    is_new = Column(String(10))


class ThsIndex(Base):
    """同花顺概念"""
    __tablename__ = "ths_index"
    ts_code = Column(String(20), primary_key=True)
    name = Column(String(100), nullable=False)
    count = Column(Integer)
    index_intro = Column(Text)
    release_date = Column(Date)


class ThsDaily(Base):
    """同花顺日线"""
    __tablename__ = "ths_daily"
    ts_code = Column(String(20), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    close = Column(Decimal(12, 4))
    pct_change = Column(Decimal(10, 4))
    vol = Column(BigInteger)
    amount = Column(Decimal(24, 4))


class ThsMember(Base):
    """同花顺成分"""
    __tablename__ = "ths_member"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    con_code = Column(String(20), nullable=False, index=True)
    name = Column(String(50))
    weight = Column(Decimal(10, 4))
    in_date = Column(Date)
    is_new = Column(String(10))


class Margin(Base):
    """融资融券汇总"""
    __tablename__ = "margin"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    trade_date = Column(Date, nullable=False, index=True)
    exchange_id = Column(String(10), nullable=False)
    rzye = Column(Decimal(24, 4))
    rzmre = Column(Decimal(24, 4))
    rzche = Column(Decimal(24, 4))
    rqye = Column(Decimal(24, 4))
    rzrqye = Column(Decimal(24, 4))


class LimitList(Base):
    """龙虎榜"""
    __tablename__ = "limit_list"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    trade_date = Column(Date, nullable=False, index=True)
    ts_code = Column(String(20), nullable=False, index=True)
    name = Column(String(50))
    close = Column(Decimal(12, 4))
    pct_chg = Column(Decimal(10, 4))
    buy_amount = Column(Decimal(24, 4))
    sell_amount = Column(Decimal(24, 4))
    net_amount = Column(Decimal(24, 4))
    buy_times = Column(Integer)
    reason = Column(String(200))


class Hsgt(Base):
    """沪深港通持股"""
    __tablename__ = "hsgt"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    trade_date = Column(Date, nullable=False)
    hg_sh_amount = Column(Decimal(24, 4))
    hg_sz_amount = Column(Decimal(24, 4))
    close = Column(Decimal(12, 4))
    percent = Column(Decimal(10, 4))


class PledgeStat(Base):
    """股权质押"""
    __tablename__ = "pledge_stat"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, index=True)
    end_date = Column(Date, nullable=False)
    pledge_count = Column(Integer)
    pledge_vol = Column(BigInteger)
    pledge_ratio = Column(Decimal(10, 4))
    market_cap = Column(Decimal(24, 4))


class IndexBasic(Base):
    """指数基本信息"""
    __tablename__ = "index_basic"
    ts_code = Column(String(20), primary_key=True)
    name = Column(String(50), nullable=False)
    market = Column(String(10))
    publisher = Column(String(50))
    index_type = Column(String(20))
    list_date = Column(Date)


class IndexDaily(Base):
    """指数日线"""
    __tablename__ = "index_daily"
    ts_code = Column(String(20), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    close = Column(Decimal(12, 4))
    open = Column(Decimal(12, 4))
    high = Column(Decimal(12, 4))
    low = Column(Decimal(12, 4))
    pre_close = Column(Decimal(12, 4))
    pct_chg = Column(Decimal(10, 4))
    vol = Column(BigInteger)
    amount = Column(Decimal(24, 4))


class IndexWeight(Base):
    """指数成分和权重"""
    __tablename__ = "index_weight"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    index_code = Column(String(20), nullable=False, index=True)
    con_code = Column(String(20), nullable=False, index=True)
    weight = Column(Decimal(10, 4))


class IndexDailybasic(Base):
    """大盘指数每日指标"""
    __tablename__ = "index_dailybasic"
    ts_code = Column(String(20), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    pe = Column(Decimal(12, 4))
    pb = Column(Decimal(12, 4))
    turnover = Column(Decimal(10, 4))
