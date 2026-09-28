"""从 SQLAlchemy 模型生成 MySQL DDL（不依赖数据库连接）"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.schema import CreateTable
from app.models.stock import Base

lines = [
    "-- ============================================",
    "-- AI Quant Agent - 数据库初始化脚本",
    "-- 引擎: InnoDB | 字符集: utf8mb4",
    "-- ============================================",
    "",
    "CREATE DATABASE IF NOT EXISTS quant_db",
    "    DEFAULT CHARACTER SET utf8mb4",
    "    DEFAULT COLLATE utf8mb4_unicode_ci;",
    "",
    "USE quant_db;",
    "",
]

# 按建表顺序排列
table_order = [
    "stock_basic", "trade_cal", "stock_company", "namechange", "new_share",
    "daily", "weekly", "monthly", "adj_factor", "stk_limit", "suspend_d",
    "daily_basic", "moneyflow", "block_trade",
    "income", "balancesheet", "cashflow", "fina_indicator", "fina_mainbz",
    "forecast", "express",
    "stk_holdernumber", "top10_holders", "top10_floatholders",
    "stk_holdertrade", "repurchase", "stk_rewards", "dividend",
    "concept", "concept_detail", "ths_index", "ths_daily", "ths_member",
    "margin", "limit_list", "hsgt", "pledge_stat",
    "index_basic", "index_daily", "index_weight", "index_dailybasic",
]

tables = {t.name: t for t in Base.metadata.sorted_tables}

for name in table_order:
    if name not in tables:
        continue
    table = tables[name]
    # 生成 MySQL DDL
    create_sql = str(CreateTable(table).compile(dialect=__import__("sqlalchemy").dialects.mysql.dialect()))
    # 添加 ENGINE/CHARSET
    create_sql = create_sql.rstrip(")") + ","
    create_sql += "\n    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
    create_sql += f"\n) COMMENT='{name}';"
    lines.append(create_sql)
    lines.append("")

lines.append("-- ============================================")
lines.append("-- 验证")
lines.append("-- ============================================")
lines.append("SELECT CONCAT('初始化完成: ', COUNT(*), ' 张表') AS result")
lines.append("FROM information_schema.tables")
lines.append("WHERE table_schema = 'quant_db';")
lines.append("")

output_path = Path(__file__).parent.parent / "migrations" / "init.sql"
output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text("\n".join(lines), encoding="utf-8")
print(f"✅ init.sql 已生成: {output_path} ({output_path.stat().st_size} 字节)")
