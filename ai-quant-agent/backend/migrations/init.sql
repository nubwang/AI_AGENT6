-- ============================================
-- AI Quant Agent - 数据库初始化脚本
-- 引擎: InnoDB | 字符集: utf8mb4
-- ============================================

CREATE DATABASE IF NOT EXISTS quant_db
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE quant_db;


CREATE TABLE stock_basic (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL COMMENT '股票代码', 
	symbol VARCHAR(10) NOT NULL COMMENT '数字代码', 
	name VARCHAR(50) NOT NULL COMMENT '股票名称', 
	area VARCHAR(20) COMMENT '地区', 
	industry VARCHAR(50) COMMENT '所属行业', 
	market VARCHAR(20) COMMENT '市场', 
	list_date DATE COMMENT '上市日期', 
	delist_date DATE COMMENT '退市日期', 
	is_hs VARCHAR(10) COMMENT '沪深港通标的', 
	created_at DATETIME DEFAULT now(), 
	PRIMARY KEY (id), 
	UNIQUE (ts_code)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='stock_basic';


CREATE TABLE trade_cal (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	exchange VARCHAR(10) NOT NULL COMMENT '交易所', 
	cal_date DATE NOT NULL COMMENT '日历日期', 
	is_open INTEGER NOT NULL COMMENT '是否交易日', 
	pretrade_date DATE COMMENT '前一个交易日', 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='trade_cal';


CREATE TABLE stock_company (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	chairman VARCHAR(50), 
	manager VARCHAR(50), 
	secretary VARCHAR(50), 
	reg_capital DECIMAL(21, 4), 
	setup_date DATE, 
	province VARCHAR(20), 
	city VARCHAR(30), 
	introduction TEXT, 
	website VARCHAR(100), 
	employees INTEGER, 
	main_business TEXT, 
	PRIMARY KEY (id), 
	UNIQUE (ts_code)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='stock_company';


CREATE TABLE namechange (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	name VARCHAR(50) NOT NULL, 
	start_date DATE, 
	end_date DATE, 
	ann_date DATE, 
	change_reason VARCHAR(50), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='namechange';


CREATE TABLE new_share (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	sub_code VARCHAR(20), 
	name VARCHAR(50), 
	ipo_date DATE, 
	issue_date DATE, 
	amount DECIMAL(21, 4), 
	market_amount DECIMAL(21, 4), 
	price DECIMAL(12, 4), 
	pe DECIMAL(10, 4), 
	limit_amount DECIMAL(12, 4), 
	fund DECIMAL(21, 4), 
	PRIMARY KEY (id), 
	UNIQUE (ts_code)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='new_share';


CREATE TABLE daily (
	ts_code VARCHAR(20) NOT NULL, 
	trade_date DATE NOT NULL, 
	open DECIMAL(12, 4), 
	high DECIMAL(12, 4), 
	low DECIMAL(12, 4), 
	close DECIMAL(12, 4), 
	pre_close DECIMAL(12, 4), 
	`change` DECIMAL(12, 4), 
	pct_chg DECIMAL(10, 4), 
	vol BIGINT, 
	amount DECIMAL(24, 4), 
	PRIMARY KEY (ts_code, trade_date)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='daily';


CREATE TABLE weekly (
	ts_code VARCHAR(20) NOT NULL, 
	trade_date DATE NOT NULL, 
	open DECIMAL(12, 4), 
	high DECIMAL(12, 4), 
	low DECIMAL(12, 4), 
	close DECIMAL(12, 4), 
	vol BIGINT, 
	amount DECIMAL(24, 4), 
	PRIMARY KEY (ts_code, trade_date)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='weekly';


CREATE TABLE monthly (
	ts_code VARCHAR(20) NOT NULL, 
	trade_date DATE NOT NULL, 
	open DECIMAL(12, 4), 
	high DECIMAL(12, 4), 
	low DECIMAL(12, 4), 
	close DECIMAL(12, 4), 
	vol BIGINT, 
	amount DECIMAL(24, 4), 
	PRIMARY KEY (ts_code, trade_date)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='monthly';


CREATE TABLE adj_factor (
	ts_code VARCHAR(20) NOT NULL, 
	trade_date DATE NOT NULL, 
	adj_factor DECIMAL(21, 6) NOT NULL, 
	PRIMARY KEY (ts_code, trade_date)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='adj_factor';


CREATE TABLE stk_limit (
	ts_code VARCHAR(20) NOT NULL,
	trade_date DATE NOT NULL,
	up_limit DECIMAL(12, 4),
	down_limit DECIMAL(12, 4),
	PRIMARY KEY (ts_code, trade_date),
	KEY idx_stk_limit_date (trade_date)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='stk_limit';


CREATE TABLE suspend_d (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	trade_date DATE NOT NULL, 
	suspend_flg VARCHAR(10) NOT NULL, 
	suspend_type VARCHAR(30), 
	ann_date DATE, 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='suspend_d';


CREATE TABLE daily_basic (
	ts_code VARCHAR(20) NOT NULL, 
	trade_date DATE NOT NULL, 
	turnover_rate DECIMAL(10, 4), 
	turnover_rate_f DECIMAL(10, 4), 
	volume_ratio DECIMAL(10, 4), 
	pe DECIMAL(12, 4), 
	pe_ttm DECIMAL(12, 4), 
	pb DECIMAL(12, 4), 
	ps DECIMAL(12, 4), 
	ps_ttm DECIMAL(12, 4), 
	dv_ratio DECIMAL(10, 4), 
	dv_ttm DECIMAL(10, 4), 
	total_share BIGINT, 
	float_share BIGINT, 
	free_share BIGINT, 
	total_mv DECIMAL(24, 4), 
	circ_mv DECIMAL(24, 4), 
	PRIMARY KEY (ts_code, trade_date)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='daily_basic';


CREATE TABLE moneyflow (
	ts_code VARCHAR(20) NOT NULL, 
	trade_date DATE NOT NULL, 
	buy_amount DECIMAL(24, 4), 
	buy_vol BIGINT, 
	sell_amount DECIMAL(24, 4), 
	sell_vol BIGINT, 
	net_amount DECIMAL(24, 4), 
	net_vol BIGINT, 
	amount_lt DECIMAL(24, 4), 
	vol_lt BIGINT, 
	net_amount_lt DECIMAL(24, 4), 
	amount_mt DECIMAL(24, 4), 
	vol_mt BIGINT, 
	net_amount_mt DECIMAL(24, 4), 
	amount_st DECIMAL(24, 4), 
	vol_st BIGINT, 
	net_amount_st DECIMAL(24, 4), 
	PRIMARY KEY (ts_code, trade_date)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='moneyflow';


CREATE TABLE block_trade (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	trade_date DATE NOT NULL, 
	price DECIMAL(12, 4), 
	vol DECIMAL(21, 4), 
	amount DECIMAL(24, 4), 
	buyer VARCHAR(100), 
	seller VARCHAR(100), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='block_trade';


CREATE TABLE income (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	ann_date DATE, 
	f_ann_date DATE, 
	end_date DATE NOT NULL, 
	report_type INTEGER, 
	comp_type INTEGER, 
	revenue DECIMAL(24, 4), 
	operate_profit DECIMAL(24, 4), 
	total_profit DECIMAL(24, 4), 
	n_income DECIMAL(24, 4), 
	n_income_attr_p DECIMAL(24, 4), 
	basic_eps DECIMAL(12, 4), 
	diluted_eps DECIMAL(12, 4), 
	update_flag VARCHAR(10), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='income';


CREATE TABLE balancesheet (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	end_date DATE NOT NULL, 
	report_type INTEGER, 
	comp_type INTEGER, 
	total_assets DECIMAL(24, 4), 
	total_liab DECIMAL(24, 4), 
	total_hldr_eqy DECIMAL(24, 4), 
	total_lse DECIMAL(24, 4), 
	total_equity DECIMAL(24, 4), 
	update_flag VARCHAR(10), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='balancesheet';


CREATE TABLE cashflow (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	end_date DATE NOT NULL, 
	report_type INTEGER, 
	comp_type INTEGER, 
	n_inc_oper_act DECIMAL(24, 4), 
	n_inc_inves_act DECIMAL(24, 4), 
	n_inc_fnc_act DECIMAL(24, 4), 
	net_cash_flows DECIMAL(24, 4), 
	update_flag VARCHAR(10), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='cashflow';


CREATE TABLE fina_indicator (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	ann_date DATE, 
	end_date DATE NOT NULL, 
	roe DECIMAL(10, 4), 
	roe_dt DECIMAL(10, 4), 
	roa DECIMAL(10, 4), 
	debt_to_assets DECIMAL(10, 4), 
	eps DECIMAL(12, 4), 
	bps DECIMAL(12, 4), 
	ocfps DECIMAL(12, 4), 
	profit_dedt DECIMAL(24, 4), 
	gross_margin DECIMAL(10, 4), 
	profit_margin DECIMAL(10, 4), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='fina_indicator';


CREATE TABLE fina_mainbz (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	end_date DATE NOT NULL, 
	bz_item VARCHAR(100), 
	bz_sales DECIMAL(24, 4), 
	bz_cost DECIMAL(24, 4), 
	bz_profit DECIMAL(24, 4), 
	bz_sales_ratio DECIMAL(10, 4), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='fina_mainbz';


CREATE TABLE forecast (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	ann_date DATE NOT NULL, 
	end_date DATE, 
	type VARCHAR(20), 
	p_change_min DECIMAL(10, 4), 
	p_change_max DECIMAL(10, 4), 
	net_profit_min DECIMAL(24, 4), 
	net_profit_max DECIMAL(24, 4), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='forecast';


CREATE TABLE express (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	ann_date DATE NOT NULL, 
	end_date DATE, 
	revenue DECIMAL(24, 4), 
	operate_profit DECIMAL(24, 4), 
	total_profit DECIMAL(24, 4), 
	n_income DECIMAL(24, 4), 
	total_assets DECIMAL(24, 4), 
	basic_eps DECIMAL(12, 4), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='express';


CREATE TABLE stk_holdernumber (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	ann_date DATE NOT NULL, 
	end_date DATE, 
	holder_num BIGINT, 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='stk_holdernumber';


CREATE TABLE top10_holders (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	ann_date DATE, 
	end_date DATE NOT NULL, 
	holder_name VARCHAR(100) NOT NULL, 
	hold_amount BIGINT, 
	hold_ratio DECIMAL(10, 4), 
	hold_float_ratio DECIMAL(10, 4), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='top10_holders';


CREATE TABLE top10_floatholders (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	ann_date DATE, 
	end_date DATE NOT NULL, 
	holder_name VARCHAR(100) NOT NULL, 
	hold_amount BIGINT, 
	hold_ratio DECIMAL(10, 4), 
	hold_float_ratio DECIMAL(10, 4), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='top10_floatholders';


CREATE TABLE stk_holdertrade (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	ann_date DATE NOT NULL, 
	holder_name VARCHAR(100) NOT NULL, 
	holder_type VARCHAR(20), 
	trade_type VARCHAR(4) NOT NULL, 
	change_vol BIGINT, 
	change_ratio DECIMAL(10, 6), 
	begin_date DATE, 
	end_date DATE, 
	price_avg DECIMAL(12, 4), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='stk_holdertrade';


CREATE TABLE repurchase (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	ann_date DATE NOT NULL, 
	end_date DATE, 
	progress VARCHAR(20), 
	exp_date DATE, 
	amount_min DECIMAL(24, 4), 
	amount_max DECIMAL(24, 4), 
	vol_min BIGINT, 
	vol_max BIGINT, 
	price_min DECIMAL(12, 4), 
	price_max DECIMAL(12, 4), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='repurchase';


CREATE TABLE stk_rewards (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	ann_date DATE, 
	end_date DATE, 
	exe_mode VARCHAR(50), 
	rewards_type VARCHAR(50), 
	gprice DECIMAL(12, 4), 
	volume BIGINT, 
	stk_name VARCHAR(100), 
	mark TEXT, 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='stk_rewards';


CREATE TABLE dividend (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	ann_date DATE, 
	imp_ann_date DATE, 
	record_date DATE, 
	ex_date DATE, 
	pay_date DATE, 
	div_proc VARCHAR(20), 
	cash_div DECIMAL(12, 4), 
	cash_div_tax DECIMAL(12, 4), 
	stk_div DECIMAL(12, 4), 
	stk_bo DECIMAL(12, 4), 
	share_tran DECIMAL(12, 4), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='dividend';


CREATE TABLE concept (
	code VARCHAR(20) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	src VARCHAR(20), 
	PRIMARY KEY (code)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='concept';


CREATE TABLE concept_detail (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	code VARCHAR(20) NOT NULL, 
	ts_code VARCHAR(20) NOT NULL, 
	name VARCHAR(50), 
	in_date DATE, 
	out_date DATE, 
	is_new VARCHAR(10), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='concept_detail';


CREATE TABLE ths_index (
	ts_code VARCHAR(20) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	count INTEGER, 
	index_intro TEXT, 
	release_date DATE, 
	PRIMARY KEY (ts_code)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='ths_index';


CREATE TABLE ths_daily (
	ts_code VARCHAR(20) NOT NULL, 
	trade_date DATE NOT NULL, 
	close DECIMAL(12, 4), 
	pct_change DECIMAL(10, 4), 
	vol BIGINT, 
	amount DECIMAL(24, 4), 
	PRIMARY KEY (ts_code, trade_date)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='ths_daily';


CREATE TABLE ths_member (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	con_code VARCHAR(20) NOT NULL, 
	name VARCHAR(50), 
	weight DECIMAL(10, 4), 
	in_date DATE, 
	is_new VARCHAR(10), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='ths_member';


CREATE TABLE margin (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	trade_date DATE NOT NULL, 
	exchange_id VARCHAR(10) NOT NULL, 
	rzye DECIMAL(24, 4), 
	rzmre DECIMAL(24, 4), 
	rzche DECIMAL(24, 4), 
	rqye DECIMAL(24, 4), 
	rzrqye DECIMAL(24, 4), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='margin';


CREATE TABLE limit_list (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	trade_date DATE NOT NULL, 
	ts_code VARCHAR(20) NOT NULL, 
	name VARCHAR(50), 
	close DECIMAL(12, 4), 
	pct_chg DECIMAL(10, 4), 
	buy_amount DECIMAL(24, 4), 
	sell_amount DECIMAL(24, 4), 
	net_amount DECIMAL(24, 4), 
	buy_times INTEGER, 
	reason VARCHAR(200), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='limit_list';


CREATE TABLE hsgt (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	trade_date DATE NOT NULL, 
	hg_sh_amount DECIMAL(24, 4), 
	hg_sz_amount DECIMAL(24, 4), 
	close DECIMAL(12, 4), 
	percent DECIMAL(10, 4), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='hsgt';


CREATE TABLE pledge_stat (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	ts_code VARCHAR(20) NOT NULL, 
	end_date DATE NOT NULL, 
	pledge_count INTEGER, 
	pledge_vol BIGINT, 
	pledge_ratio DECIMAL(10, 4), 
	market_cap DECIMAL(24, 4), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='pledge_stat';


CREATE TABLE index_basic (
	ts_code VARCHAR(20) NOT NULL, 
	name VARCHAR(50) NOT NULL, 
	market VARCHAR(10), 
	publisher VARCHAR(50), 
	index_type VARCHAR(20), 
	list_date DATE, 
	PRIMARY KEY (ts_code)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='index_basic';


CREATE TABLE index_daily (
	ts_code VARCHAR(20) NOT NULL, 
	trade_date DATE NOT NULL, 
	close DECIMAL(12, 4), 
	open DECIMAL(12, 4), 
	high DECIMAL(12, 4), 
	low DECIMAL(12, 4), 
	pre_close DECIMAL(12, 4), 
	pct_chg DECIMAL(10, 4), 
	vol BIGINT, 
	amount DECIMAL(24, 4), 
	PRIMARY KEY (ts_code, trade_date)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='index_daily';


CREATE TABLE index_weight (
	id BIGINT NOT NULL AUTO_INCREMENT, 
	index_code VARCHAR(20) NOT NULL, 
	con_code VARCHAR(20) NOT NULL, 
	weight DECIMAL(10, 4), 
	PRIMARY KEY (id)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='index_weight';


CREATE TABLE index_dailybasic (
	ts_code VARCHAR(20) NOT NULL, 
	trade_date DATE NOT NULL, 
	pe DECIMAL(12, 4), 
	pb DECIMAL(12, 4), 
	turnover DECIMAL(10, 4), 
	PRIMARY KEY (ts_code, trade_date)
)

,
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
) COMMENT='index_dailybasic';

-- ============================================
-- 验证
-- ============================================
SELECT CONCAT('初始化完成: ', COUNT(*), ' 张表') AS result
FROM information_schema.tables
WHERE table_schema = 'quant_db';
