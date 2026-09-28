# AI Agent 量化交易系统

基于 **历史短线爆发模式匹配** 的 A 股智能量化交易系统。每日全市场扫描，通过匹配历史3倍以上短线爆发行情的起爆前走势特征，预测次日上涨概率，输出TOP-30推荐榜单。

---

## 项目状态

> 🚧 项目搭建中 — 已完成后端骨架（FastAPI v0.139+ / Pydantic v2 / SQLAlchemy 2.0+）和数据采集层

---

## 技术栈（全部使用最新版）

| 类别       | 技术                                            | 版本                     |
| ---------- | ----------------------------------------------- | ------------------------ |
| 后端框架   | **FastAPI** + Uvicorn                           | v0.139+                  |
| 数据校验   | **Pydantic**                                    | v2.13+                   |
| ORM        | **SQLAlchemy**                                  | v2.0+（DeclarativeBase） |
| 数据库     | **MySQL 8.0** + PyMySQL                         | -                        |
| 缓存       | **Redis**                                       | v8.0+                    |
| 向量库     | **ChromaDB**                                    | v1.5+                    |
| AI 模型    | **DeepSeek V4 Pro**                             | API                      |
| Agent 框架 | **LangChain** + **LangGraph**                   | v1.3+ / v1.2+            |
| 数据源     | **Tushare Pro**（2100积分）                     | v1.4+（42个API）         |
| 任务调度   | **APScheduler**                                 | v3.11+                   |
| 前端       | **Vue 3** + TypeScript + Element Plus + ECharts | -                        |
| 部署       | **Docker** + Docker Compose                     | -                        |

---

## 已实现功能

### ✅ 已完成

**1. 后端骨架**

- FastAPI 应用入口（`lifespan` 生命周期管理）
- 4个路由模块：预测榜单 / 系统管理 / Agent交互 / 数据管理
- SQLAlchemy 2.0+（`DeclarativeBase` 声明式模型）
- Pydantic v2（`Field` 正则/范围校验）
- CORS 跨域支持

**2. 数据采集层**

- Tushare API 封装：daily / daily_basic / moneyflow / stk_limit / block_trade / stock_basic / trade_cal
- 令牌桶限流器：200次/分钟，线程安全
- 数据采集器：支持按单日/按日期批量采集

**3. 部署配置**

- Docker Compose：MySQL 8.0 + Redis 8 + API 服务
- 健康检查、数据持久化、自动重启

**4. 完整规划文档（`/plans` 目录，10份文档）**

- 总规划框架、42个Tushare API全览（积分已核实）、MySQL 41张表DDL
- ChromaDB设计、AI Agent设计、API路由设计、前端设计、部署方案
- 短线爆发模式匹配算法推演、最佳入场点分析方法

### 🚧 开发中

| 模块             | 说明                               |
| ---------------- | ---------------------------------- |
| MySQL 建表脚本   | 41张表的初始化SQL                  |
| 历史模式挖掘引擎 | 回测2010-2024，提取3倍以上爆发行情 |
| AI Agent 系统    | LangGraph编排5个Agent              |
| 前端 Vue 3       | 每日推荐榜单 / 个股详情 / 历史复盘 |
| ChromaDB 模式库  | 向量化存储 + 相似度检索            |

---

## AI Agent 技术架构

基于 **LangGraph（v1.2+）** 构建有向图编排的5个Agent：

| Agent                   | 框架                  | 职责                       |
| ----------------------- | --------------------- | -------------------------- |
| Orchestrator Agent      | LangGraph StateGraph  | 编排每日全市场扫描流水线   |
| Pattern Matching Agent  | ChromaDB + 余弦相似度 | 特征提取 → 模式检索 → 评分 |
| DeepSeek Analysis Agent | DeepSeek V4 Pro API   | 归因分析                   |
| Risk Filter Agent       | 规则引擎              | 剔除ST/涨停/高质押标的     |
| Report Agent            | 模板引擎              | 生成推荐榜单               |

---

## 快速开始

### 方式一：一键启动（推荐）

```bash
cd ai-quant-agent
bash scripts/start_all.sh
```

脚本会自动完成：检查环境变量 → Docker Compose 启动 → 验证服务。

### 方式二：分步启动

```bash
# 1. 配置环境变量
cd ai-quant-agent
cp .env.example .env
# 编辑 .env，填入 TUSHARE_TOKEN 和 DEEPSEEK_API_KEY

# 2. 启动所有服务（MySQL + Redis + API）
docker-compose up -d

# 3. 查看 API 文档
open http://localhost:8000/docs
```

### 方式三：本地开发启动

**后端：**

```bash
cd ai-quant-agent/backend
python -m venv venv
source venv/bin/activate          # macOS/Linux
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

**前端：**

```bash
cd ai-quant-agent/frontend
npm install
npm run dev
```

### 验证

```bash
curl http://localhost:8000/api/v1/system/health
# {"status":"ok","app":"AI Quant Agent","version":"0.1.0"}
```

---

## API 接口

| 方法   | 路径                                  | 说明         |
| ------ | ------------------------------------- | ------------ |
| `GET`  | `/api/v1/predictions/today`           | 今日推荐榜单 |
| `GET`  | `/api/v1/predictions/date/{date}`     | 历史榜单     |
| `GET`  | `/api/v1/predictions/today/{ts_code}` | 个股预测详情 |
| `POST` | `/api/v1/agent/ask`                   | Agent提问    |
| `GET`  | `/api/v1/system/health`               | 健康检查     |
| `POST` | `/api/v1/data/trigger-collection`     | 触发数据采集 |
| `GET`  | `/docs`                               | Swagger文档  |

---

## 项目结构

```
ai-quant-agent/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口（lifespan）
│   │   ├── core/config.py       # pydantic-settings v2
│   │   ├── core/logger.py       # 日志
│   │   ├── models/              # SQLAlchemy 2.0+ ORM
│   │   ├── api/                 # FastAPI 路由（v0.139+）
│   │   ├── data/                # 数据采集（限流器+Tushare封装）
│   │   ├── agents/              # LangGraph Agent（待开发）
│   │   ├── rag/                 # ChromaDB（待开发）
│   │   └── backtest/            # 模式挖掘（待开发）
│   ├── requirements.txt         # 全部最新版依赖
│   └── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
└── plans/                       # 10份规划文档
```
