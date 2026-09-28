# 模块5：后端 API 服务 — 详细规划

> **对应总规划**：[`00-总规划框架.md`](plans/00-总规划框架.md) → 模块5

---

## 一、技术选型

| 组件       | 方案           | 说明                            |
| ---------- | -------------- | ------------------------------- |
| Web框架    | FastAPI        | 异步高性能，自动生成Swagger文档 |
| ASGI服务器 | Uvicorn        | 异步支持                        |
| ORM        | SQLAlchemy 2.0 | 异步会话支持                    |
| 数据库迁移 | Alembic        | 版本化管理                      |
| 任务调度   | APScheduler    | 定时触发数据采集和预测Pipeline  |
| 缓存       | Redis          | 加速热点数据查询                |
| 认证       | JWT            | 简单Token认证                   |

---

## 二、API 路由设计

### 2.1 预测榜单接口

| 方法  | 路径                                  | 说明                       | 返回           |
| ----- | ------------------------------------- | -------------------------- | -------------- |
| `GET` | `/api/v1/predictions/today`           | 获取今日推荐榜单           | TOP-30股票列表 |
| `GET` | `/api/v1/predictions/date/{date}`     | 获取指定日期的历史榜单     | 指定日期的榜单 |
| `GET` | `/api/v1/predictions/today/{ts_code}` | 获取某只股票的今日预测详情 | 评分+相似案例  |

**`/predictions/today` 返回示例**：

```json
{
  "date": "2026-07-23",
  "generated_at": "2026-07-23T18:30:00",
  "market_status": "bullish",
  "top_picks": [
    {
      "rank": 1,
      "ts_code": "600036.SH",
      "name": "招商银行",
      "score": 92,
      "similar_case": {
        "stock_name": "招商银行",
        "date": "2020-05-15",
        "similarity": 0.89,
        "return_t1": 3.5,
        "return_t5": 15.2
      },
      "risk_level": "低"
    }
  ],
  "total_scanned": 5023,
  "total_filtered": 4890
}
```

### 2.2 历史数据接口

| 方法  | 路径                                   | 说明                        |
| ----- | -------------------------------------- | --------------------------- |
| `GET` | `/api/v1/history/accuracy?days=30`     | 获取最近N天的预测准确率统计 |
| `GET` | `/api/v1/history/stock/{ts_code}`      | 获取某只股票的历史评分记录  |
| `GET` | `/api/v1/history/pattern/{pattern_id}` | 获取某个历史模式的详情      |

### 2.3 Agent 交互接口

| 方法   | 路径                   | 说明                      |
| ------ | ---------------------- | ------------------------- |
| `POST` | `/api/v1/agent/ask`    | 向Agent提问，获取深度分析 |
| `GET`  | `/api/v1/agent/status` | 查看Agent运行状态         |

**`/agent/ask` 请求示例**：

```json
{
  "question": "为什么今天推荐招商银行？",
  "ts_code": "600036.SH"
}
```

**响应示例**：

```json
{
  "answer": "招商银行当前20天走势与2020年5月的历史短线爆发模式相似度达89%。当时该模式出现后，招商银行在5个交易日内上涨15.2%。主要驱动因素包括：① 放量突破60日均线 ② 主力资金连续3日净流入 ③ 银行板块整体走强。",
  "confidence": 92,
  "similar_cases": [
    { "date": "2020-05-15", "similarity": 0.89, "return_t5": 15.2 },
    { "date": "2022-11-03", "similarity": 0.85, "return_t5": 8.7 }
  ]
}
```

### 2.4 数据管理接口

| 方法   | 路径                              | 说明               |
| ------ | --------------------------------- | ------------------ |
| `POST` | `/api/v1/data/trigger-collection` | 手动触发数据采集   |
| `POST` | `/api/v1/data/trigger-prediction` | 手动触发预测流水线 |
| `GET`  | `/api/v1/data/collection-status`  | 查看数据采集进度   |

### 2.5 系统管理接口

| 方法  | 路径                    | 说明                           |
| ----- | ----------------------- | ------------------------------ |
| `GET` | `/api/v1/system/health` | 健康检查                       |
| `GET` | `/api/v1/system/stats`  | 系统统计（股票数量、数据量等） |
| `GET` | `/docs`                 | Swagger API文档（自动生成）    |

---

## 三、定时任务配置

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()

# 每日18:00 触发数据采集
@scheduler.scheduled_job('cron', hour=18, minute=0)
async def trigger_data_collection():
    """每日收盘后自动采集当日数据"""
    await data_collector.run_daily_update()

# 每日19:00 触发预测流水线
@scheduler.scheduled_job('cron', hour=19, minute=0)
async def trigger_prediction_pipeline():
    """数据采集完成后启动预测"""
    await orchestrator.run_daily_prediction()
```

---

## 四、项目目录结构

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI入口
│   ├── config.py                  # 配置管理
│   ├── api/
│   │   ├── __init__.py
│   │   ├── predictions.py         # 预测榜单路由
│   │   ├── history.py             # 历史数据路由
│   │   ├── agent.py               # Agent交互路由
│   │   ├── data.py                # 数据管理路由
│   │   └── system.py              # 系统管理路由
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── orchestrator.py        # Orchestrator Agent
│   │   ├── pattern_matcher.py     # Pattern Matching Agent
│   │   ├── deepseek_analyzer.py   # DeepSeek Analysis Agent
│   │   ├── risk_filter.py         # Risk Filter Agent
│   │   └── report_generator.py    # Report Agent
│   ├── rag/
│   │   ├── __init__.py
│   │   ├── chroma_client.py       # ChromaDB客户端
│   │   └── vector_store.py        # 向量存储操作
│   ├── data/
│   │   ├── __init__.py
│   │   ├── collector.py           # 数据采集器基类
│   │   ├── tushare_api.py         # Tushare API封装
│   │   ├── rate_limiter.py        # 限流器
│   │   └── validators.py          # 数据校验
│   ├── backtest/
│   │   ├── __init__.py
│   │   ├── pattern_miner.py       # 历史模式挖掘
│   │   └── entry_analyzer.py      # 最佳入场点分析
│   ├── models/
│   │   ├── __init__.py
│   │   └── database.py            # SQLAlchemy模型
│   ├── schemas/
│   │   ├── __init__.py
│   │   └── prediction.py          # Pydantic响应模型
│   └── core/
│       ├── __init__.py
│       ├── config.py              # 配置
│       └── logger.py              # 日志
├── tests/
├── migrations/                    # Alembic迁移
├── requirements.txt
└── Dockerfile
```
