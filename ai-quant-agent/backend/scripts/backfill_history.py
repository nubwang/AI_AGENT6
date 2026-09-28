"""全量历史数据回填脚本"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import time
from tqdm import tqdm
from app.core.logger import logger
from app.data.collector import data_collector
from app.data.tushare_api import tushare_client


def backfill_stage1_basic_info():
    """阶段1：拉取基础信息"""
    logger.info("===== 阶段1：基础信息 =====")
    data_collector.collect_and_store("trade_cal", tushare_client.trade_cal())
    data_collector.collect_and_store("stock_basic", tushare_client.stock_basic())
    data_collector.collect_and_store("concept", tushare_client.concept())
    data_collector.collect_and_store("ths_index", tushare_client.ths_index())
    data_collector.collect_and_store("index_basic", tushare_client.index_basic())
    logger.info("阶段1完成")


def backfill_stage2_daily():
    """阶段2：按日拉取批量数据"""
    logger.info("===== 阶段2：日频批量数据 =====")
    trade_dates = data_collector.get_trade_dates("20100101", time.strftime("%Y%m%d"))

    apis = [
        ("daily", tushare_client.daily),
        ("daily_basic", tushare_client.daily_basic),
        ("moneyflow", tushare_client.moneyflow),
        ("stk_limit", tushare_client.stk_limit),
        ("block_trade", tushare_client.block_trade),
        ("ths_daily", tushare_client.ths_daily),
        ("index_daily", tushare_client.index_daily),
        ("margin", tushare_client.margin),
        ("limit_list", tushare_client.limit_list),
        ("hsgt", tushare_client.hsgt),
    ]

    for date in tqdm(trade_dates, desc="拉取日频数据"):
        for api_name, api_func in apis:
            try:
                df = api_func(trade_date=date)
                data_collector.collect_and_store(api_name, df)
            except Exception as e:
                logger.warning(f"{api_name} {date} 失败: {e}")
        time.sleep(0.3)
    logger.info("阶段2完成")


def backfill_stage3_stocks(only_api: str = "", sleep_sec: float = 0.3):
    """阶段3：按个股循环拉取。

    `only_api`（plans/24 §11.36.15 新增）：只跑指定 API，如 `adj_factor` ——
    用于回补 **2010~2019 缺失的复权因子**（专项核查实测覆盖率仅 91~95%，
    是 16 年样本**唯一的真实缺口**）。

    ⚠️ 两点必须说清：
      1. 走的是**正常采集链路**（`collect_and_store` 的幂等 upsert + `adj_factor` 的唯一键
         `uk_ts_date`）—— **不是**绕过数据门禁的"补数脚本"；
      2. 为什么不能靠 `stage3` 的"补齐缺失"分支：那条分支的判据是「**该股该表完全没数据**才全量补」，
         **不会回补"有数据但部分日期缺失"** ⇒ 只能按股票重采全历史，即本函数做的事。
    """
    logger.info("===== 阶段3：个股维度数据 =====")
    stocks = data_collector.get_all_stocks()

    stock_apis = [
        ("adj_factor", lambda c: tushare_client.adj_factor(ts_code=c)),
        ("dividend", lambda c: tushare_client.dividend(ts_code=c)),
        ("stk_holdernumber", lambda c: tushare_client.stk_holdernumber(ts_code=c)),
        ("fina_indicator", lambda c: tushare_client.fina_indicator(ts_code=c)),
        ("income", lambda c: tushare_client.income(ts_code=c)),
        ("balancesheet", lambda c: tushare_client.balancesheet(ts_code=c)),
        ("cashflow", lambda c: tushare_client.cashflow(ts_code=c)),
    ]
    if only_api:
        stock_apis = [x for x in stock_apis if x[0] == only_api]
        logger.info(f"只跑 API：{only_api}")
    if not stock_apis:
        logger.warning(f"没有匹配的 API：{only_api!r}（可用：adj_factor/dividend/…）")
        return

    ok = fail = 0
    for stock in tqdm(stocks, desc="拉取个股数据"):
        for api_name, api_func in stock_apis:
            try:
                df = api_func(stock)
                data_collector.collect_and_store(api_name, df)
                ok += 1
            except Exception as e:
                fail += 1
                logger.warning(f"{api_name} {stock} 失败: {e}")
        time.sleep(sleep_sec)
    logger.info(f"阶段3完成：成功 {ok} 次 / 失败 {fail} 次")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="全量/定向历史回填（plans/24 §11.36.15）")
    ap.add_argument("--stage", default="all", choices=["all", "1", "2", "3"],
                    help="只跑某阶段（缺省 all ⇒ 与原脚本行为一致）")
    ap.add_argument("--only-api", default="", help="只跑该 API，如 adj_factor")
    ap.add_argument("--sleep", type=float, default=0.3, help="每次调用后的间隔秒（限流）")
    args = ap.parse_args()

    logger.info(f"开始历史回填：stage={args.stage} only_api={args.only_api or '（全部）'}")
    if args.stage in ("all", "1"):
        backfill_stage1_basic_info()
    if args.stage in ("all", "2"):
        backfill_stage2_daily()
    if args.stage in ("all", "3"):
        backfill_stage3_stocks(only_api=args.only_api, sleep_sec=args.sleep)
    logger.info("全量回填完成")
