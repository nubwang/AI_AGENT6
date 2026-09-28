"""Tushare API 封装（42个接口全覆盖，参数名与Tushare官方对齐）"""
import tushare as ts
from app.core.config import settings
from app.core.logger import logger


class TushareClient:
    """Tushare API 客户端封装"""

    def __init__(self):
        self.pro = ts.pro_api(settings.tushare_token)

    # ========== 基础信息 ==========
    def stock_basic(self):
        return self.pro.stock_basic()

    def trade_cal(self, exchange="SSE", start_date="20100101", end_date="20261231"):
        return self.pro.trade_cal(exchange=exchange, start_date=start_date, end_date=end_date)

    def stock_company(self, ts_code: str):
        return self.pro.stock_company(ts_code=ts_code)

    def namechange(self, ts_code: str):
        return self.pro.namechange(ts_code=ts_code)

    def new_share(self):
        return self.pro.new_share()

    # ========== 行情数据 ==========
    def daily(self, trade_date: str = "", ts_code: str = "", start_date: str = "", end_date: str = ""):
        return self.pro.daily(trade_date=trade_date or None, ts_code=ts_code or None,
                              start_date=start_date or None, end_date=end_date or None)

    def weekly(self, ts_code: str = "", start_date: str = "", end_date: str = ""):
        return self.pro.weekly(ts_code=ts_code, start_date=start_date, end_date=end_date)

    def monthly(self, ts_code: str = "", start_date: str = "", end_date: str = ""):
        return self.pro.monthly(ts_code=ts_code, start_date=start_date, end_date=end_date)

    def adj_factor(self, ts_code: str = "", trade_date: str = "", start_date: str = "", end_date: str = ""):
        return self.pro.adj_factor(ts_code=ts_code or None, trade_date=trade_date or None,
                                   start_date=start_date or None, end_date=end_date or None)

    def stk_limit(self, trade_date: str = "", ts_code: str = ""):
        return self.pro.stk_limit(trade_date=trade_date or None, ts_code=ts_code or None)

    def suspend_d(self, trade_date: str = "", ts_code: str = ""):
        return self.pro.suspend_d(trade_date=trade_date or None, ts_code=ts_code or None)

    # ========== 每日指标与资金 ==========
    def daily_basic(self, trade_date: str = "", ts_code: str = ""):
        return self.pro.daily_basic(trade_date=trade_date or None, ts_code=ts_code or None)

    def moneyflow(self, trade_date: str = "", ts_code: str = ""):
        return self.pro.moneyflow(trade_date=trade_date or None, ts_code=ts_code or None)

    def block_trade(self, trade_date: str = "", ts_code: str = ""):
        return self.pro.block_trade(trade_date=trade_date or None, ts_code=ts_code or None)

    # ========== 财务数据 ==========
    def income(self, ts_code: str, start_date: str = "", end_date: str = ""):
        return self.pro.income(ts_code=ts_code, start_date=start_date or None, end_date=end_date or None)

    def balancesheet(self, ts_code: str, start_date: str = "", end_date: str = ""):
        return self.pro.balancesheet(ts_code=ts_code, start_date=start_date or None, end_date=end_date or None)

    def cashflow(self, ts_code: str, start_date: str = "", end_date: str = ""):
        return self.pro.cashflow(ts_code=ts_code, start_date=start_date or None, end_date=end_date or None)

    def fina_indicator(self, ts_code: str = "", start_date: str = "", end_date: str = ""):
        return self.pro.fina_indicator(ts_code=ts_code or None, start_date=start_date or None, end_date=end_date or None)

    def fina_mainbz(self, ts_code: str, end_date: str = "", period: str = ""):
        # period=报告期(如20260630), end_date=公告日期范围. 增量按 period 精确拉取单期
        return self.pro.fina_mainbz(ts_code=ts_code, end_date=end_date or None, period=period or None)

    def disclosure_date(self, end_date: str = "", ts_code: str = ""):
        """业绩披露计划: 预约披露日(pre_date) + 实际披露日(actual_date).
        用于按"披露日期"精确判断某股票某报告期是否已披露, 避免未披露期反复空转请求。"""
        return self.pro.disclosure_date(end_date=end_date or None, ts_code=ts_code or None)

    # ⚠️ forecast 使用 ann_date 参数，不是 trade_date
    def forecast(self, ann_date: str = "", ts_code: str = ""):
        return self.pro.forecast(ann_date=ann_date or None, ts_code=ts_code or None)

    def express(self, ann_date: str = "", ts_code: str = ""):
        # 业绩快报按公告日(ann_date)拉取, 与 Tushare 官方参数对齐
        return self.pro.express(ann_date=ann_date or None, ts_code=ts_code or None)

    # ========== 股东与股本 ==========
    def stk_holdernumber(self, ts_code: str = ""):
        return self.pro.stk_holdernumber(ts_code=ts_code or None)

    def top10_holders(self, ts_code: str, start_date: str = "", end_date: str = ""):
        return self.pro.top10_holders(ts_code=ts_code, start_date=start_date or None, end_date=end_date or None)

    def top10_floatholders(self, ts_code: str, start_date: str = "", end_date: str = ""):
        return self.pro.top10_floatholders(ts_code=ts_code, start_date=start_date or None, end_date=end_date or None)

    def stk_holdertrade(self, ts_code: str = ""):
        return self.pro.stk_holdertrade(ts_code=ts_code or None)

    def repurchase(self, ts_code: str = ""):
        return self.pro.repurchase(ts_code=ts_code or None)

    def stk_rewards(self, ts_code: str = ""):
        return self.pro.stk_rewards(ts_code=ts_code or None)

    # ========== 分红与IPO ==========
    def dividend(self, ts_code: str = ""):
        return self.pro.dividend(ts_code=ts_code or None)

    # ========== 板块概念 ==========
    def concept(self):
        return self.pro.concept()

    def concept_detail(self, code: str = ""):
        return self.pro.concept_detail(code=code or None)

    def ths_index(self):
        return self.pro.ths_index()

    def ths_daily(self, trade_date: str = "", ts_code: str = ""):
        return self.pro.ths_daily(trade_date=trade_date or None, ts_code=ts_code or None)

    def ths_member(self, ts_code: str = ""):
        return self.pro.ths_member(ts_code=ts_code or None)

    # ========== 市场参考 ==========
    def margin(self, trade_date: str = ""):
        return self.pro.margin(trade_date=trade_date or None)

    # ⚠️ 龙虎榜接口名是 limit_list_d，Python SDK方法也是 pro.limit_list_d()
    def limit_list_d(self, trade_date: str = ""):
        return self.pro.limit_list_d(trade_date=trade_date or None)

    def hsgt(self, trade_date: str = ""):
        return self.pro.hsgt(trade_date=trade_date or None)

    def pledge_stat(self, ts_code: str = ""):
        return self.pro.pledge_stat(ts_code=ts_code or None)

    # ========== 指数 ==========
    def index_basic(self):
        return self.pro.index_basic()

    def index_daily(self, trade_date: str = "", ts_code: str = ""):
        return self.pro.index_daily(trade_date=trade_date or None, ts_code=ts_code or None)

    def index_weight(self, index_code: str):
        return self.pro.index_weight(index_code=index_code)

    def index_dailybasic(self, trade_date: str = ""):
        return self.pro.index_dailybasic(trade_date=trade_date or None)


tushare_client = TushareClient()
