import axios from "axios";

const api = axios.create({ baseURL: "/api/v1", timeout: 30000 });

export default api;

// 数据采集
export const listApis = () => api.get("/data/apis");
export const getStats = () => api.get("/data/stats");
export const triggerCollection = () => api.post("/data/trigger-collection");
export const triggerPrediction = () => api.post("/data/trigger-prediction");

// 独立阶段采集（新增）
export const collectAll = () => api.post("/data/collect/all");
export const collectStage1 = () => api.post("/data/collect/stage1");
export const collectStage2 = () => api.post("/data/collect/stage2");
export const collectStage3 = () => api.post("/data/collect/stage3");
export const collectStage4 = () => api.post("/data/collect/stage4");
export const getCollectStatus = () => api.get("/data/collect/status");

// 停止采集
export const stopCollectAll = () => api.post("/data/collect/stop/all");
export const stopStage1 = () => api.post("/data/collect/stop/stage1");
export const stopStage2 = () => api.post("/data/collect/stop/stage2");
export const stopStage3 = () => api.post("/data/collect/stop/stage3");
export const stopStage4 = () => api.post("/data/collect/stop/stage4");

// 全市场股票查询
export const getStocks = (params: any) => api.get("/data/stocks", { params });
export const getStockDetail = (ts_code: string) =>
  api.get(`/data/stocks/${ts_code}`);
export const getStockDaily = (ts_code: string, params?: any) =>
  api.get(`/data/stocks/${ts_code}/daily`, { params });
export const getStockTables = (ts_code: string) =>
  api.get(`/data/stocks/${ts_code}/tables`, { timeout: 60000 });

// 预测榜单（每日推荐，对齐新机制：综合上涨概率 + 命中条件）
export const getTodayPicks = () => api.get("/predictions/today");
export const getDatePicks = (date: string) =>
  api.get(`/predictions/date/${date}`);
export const triggerRecommendScan = (max_stocks?: number) =>
  api.post(
    "/predictions/scan",
    {},
    { params: max_stocks ? { max_stocks } : {} },
  );
export const getRecommendScanStatus = () => api.get("/predictions/scan/status");
// 停止扫描（协作式取消：后端在下一只股票/下一批精筛前退出，不落盘半成品榜单）
export const stopRecommendScan = () => api.post("/predictions/scan/stop");
// 采集完成后的"是否运行每日推荐"确认（默认模式 confirm：后端只标记待确认，由前端弹窗决定）
export const confirmRecommendScan = () => api.post("/predictions/scan/confirm");
export const dismissRecommendScan = (reason?: string) =>
  api.post(
    "/predictions/scan/dismiss",
    {},
    { params: reason ? { reason } : {} },
  );
export const getStockRecommend = (ts_code: string) =>
  api.get(`/predictions/today/${ts_code}`);
// 多 Agent 精筛（每日推荐最后一环）
export const triggerAgentRefine = (topK?: number, topN?: number) =>
  api.post(
    "/predictions/agent/refine",
    {},
    { params: { top_k: topK, top_n: topN } },
  );
export const getAgentRefineStatus = () => api.get("/predictions/agent/status");

// 系统
export const getHealth = () => api.get("/system/health");

// 进化 Agent（对齐 plans/11 进化Agent §15.7）
// 进化状态要跑守护巡检 + L0 摘要（含全表 SQL 探针），冷启动可能数秒 → 单独放宽超时，
// 避免默认 30s 把「进化中心」首屏直接判成 timeout（其它进化接口仍用默认 30s）
export const getEvolveStatus = () =>
  api.get("/system/evolve/status", { timeout: 90000 });
export const pauseEvolve = (reason?: string) =>
  api.post("/system/evolve/pause", null, {
    params: { reason: reason || "人工暂停" },
  });
export const resumeEvolve = () => api.post("/system/evolve/resume");
export const getEvolveEvents = (limit?: number) =>
  api.get("/system/evolve/events", { params: { limit: limit || 50 } });
export const reportEvolveEvent = (
  level: string,
  source: string,
  message: string,
) =>
  api.post("/system/evolve/event", null, {
    params: { level, source, message },
  });
export const triggerEvolveEvaluate = () => api.post("/system/evolve/evaluate");
export const confirmEvolveProposal = (proposal_id: number) =>
  api.post("/system/evolve/confirm", null, { params: { proposal_id } });
export const settleEvolveShadow = (param: string) =>
  api.post("/system/evolve/settle", null, { params: { param } });
export const rollbackEvolve = (index: number = 1) =>
  api.post("/system/evolve/rollback", null, { params: { index } });
// 扩展2：Tier2/3 独立进程重启
export const getEvolveRestartStatus = () =>
  api.get("/system/evolve/restart/status");
export const triggerEvolveRestart = () => api.post("/system/evolve/restart");
// 次日自我验证（plans/17：新日线行情出来后，验证"为什么推荐的股票没涨"）
export const getEvolveVerifyStatus = () =>
  api.get("/system/evolve/verify/status");
export const triggerEvolveVerify = (limitDays?: number, maxFailures?: number) =>
  api.post("/system/evolve/verify/run", null, {
    params: { limit_days: limitDays, max_failures: maxFailures },
  });
export const getEvolveVerifyReport = () =>
  api.get("/system/evolve/verify/report");
// 生成式代码进化（plans/18：进化大脑像 ZOO CODE 一样自己写代码）
export const getEvolveCodeStatus = () => api.get("/system/evolve/code/status");
export const triggerEvolveCodePropose = (mode?: string, anchorKey?: string) =>
  api.post("/system/evolve/code/propose", null, {
    params: { mode, anchor_key: anchorKey },
  });
export const applyEvolveCode = (patchId: number) =>
  api.post("/system/evolve/code/apply", null, {
    params: { patch_id: patchId },
  });
export const rollbackEvolveCode = (index: number = 1) =>
  api.post("/system/evolve/code/rollback", null, { params: { index } });

// ── 自证测试（plans/25：用历史数据自证选股准确率 + 大盘相似波段）──
export const getSelfProofGate = () => api.get("/selfproof/data-gate");
export const getSelfProofParams = () => api.get("/selfproof/params");
export const estimateSelfProof = (payload: any) =>
  api.post("/selfproof/estimate", payload, { timeout: 60000 });
export const runSelfProof = (payload: any) =>
  api.post("/selfproof/run", payload);
export const stopSelfProof = () => api.post("/selfproof/stop");
export const getSelfProofStatus = () => api.get("/selfproof/status");
export const listSelfProofRuns = (limit = 30) =>
  api.get("/selfproof/runs", { params: { limit } });
export const getSelfProofReport = (runId: string, rebuild = false) =>
  api.get(`/selfproof/report/${runId}`, {
    params: rebuild ? { rebuild: true } : {},
    timeout: 300000, // 实时结算+组装可能较慢（要算 T+5/T+20 与基准）
  });
export const settleSelfProof = (runId: string) =>
  api.post(`/selfproof/settle/${runId}`, null, { timeout: 300000 });
export const getRegimeCandidates = () =>
  api.get("/selfproof/regime/candidates");
// 系统图谱（plans/18：回测与每日推荐的逻辑+参数，随进化动态更新）
export const getEvolveMap = () => api.get("/system/evolve/map");
// 回测控制与诊断（plans/19：进化大脑控制回测，考虑是不是回测的问题）
export const getEvolveBacktestDiag = () =>
  api.get("/system/evolve/backtest/diag");
export const getEvolveBacktestStatus = () =>
  api.get("/system/evolve/backtest/status");
export const triggerEvolveBacktest = (params?: any) =>
  api.post("/system/evolve/backtest/run", null, { params });
// 进化大脑对话（plans/20：你与进化大脑对话，像 ZOO CODE 一样对话后修改代码）
export const askAgent = (question: string) =>
  api.post("/agent/ask", { question });
// 私有域知识库（plans/21：进化大脑长期记忆——案例/实验台账/教训检索，分析必查）
export const getEvolveKbStats = () => api.get("/system/evolve/kb/stats");
export const searchEvolveKb = (params?: any) =>
  api.get("/system/evolve/kb/search", { params, timeout: 60000 });
export const getEvolveKbCase = (caseId: string) =>
  api.get(`/system/evolve/kb/case/${encodeURIComponent(caseId)}`);
export const getEvolveKbExperiments = (params?: any) =>
  api.get("/system/evolve/kb/experiments", { params });
export const getEvolveKbLessons = (params?: any) =>
  api.get("/system/evolve/kb/lessons", { params });
export const feedbackEvolveKbLesson = (lessonId: string, params: any) =>
  api.post("/system/evolve/kb/lesson/feedback", null, {
    params: { lesson_id: lessonId, ...params },
  });
export const backfillEvolveKb = (periods: number = 60) =>
  api.post("/system/evolve/kb/backfill", null, { params: { periods } });
export const maintainEvolveKb = () =>
  api.post("/system/evolve/kb/maintain", null, { timeout: 120000 });
// 实证结论（plans/23·24·25 的验证产出：洗盘/买点/博弈特征/情绪门控/纪律网格/自证/基准对照…）
// 这些是"被统计检验过的事实"，是核对"新模块是否真的接进了进化大脑"的清单
export const getEvolveKbEvidence = (params?: any) =>
  api.get("/system/evolve/kb/evidence", { params });
// 进化大脑权限域与唯一判据（介入全部业务代码 + 可新建文件；度量/守护不可改）
export const getEvolvePermissions = () => api.get("/system/evolve/permissions");
// 自动生效与午间自进化状态（待重启 / 交易窗口 / DeepSeek 时段 / 重启守护判定）
export const getEvolveAutoApply = () => api.get("/system/evolve/auto_apply");
// 交易纪律（plans/22：纪律进化——计划台账 / 触价回放 / 反事实网格 / 纪律 alpha）
export const getEvolvePlanStats = (days: number = 60) =>
  api.get("/system/evolve/plan/stats", { params: { days } });
export const getEvolvePlanList = (params?: any) =>
  api.get("/system/evolve/plan/list", { params });
export const triggerEvolvePlanReplay = (params?: any) =>
  api.post("/system/evolve/plan/replay", null, { params, timeout: 300000 });
export const triggerEvolvePlanBackfill = (limit: number = 600) =>
  api.post("/system/evolve/plan/backfill", null, {
    params: { limit },
    timeout: 300000,
  });
export const distillEvolvePlanLessons = (days: number = 60) =>
  api.post("/system/evolve/plan/lessons", null, { params: { days } });
export const getEvolvePlanGrid = () => api.get("/system/evolve/plan/grid");
export const buildEvolvePlanGrid = (limit: number = 600) =>
  api.post("/system/evolve/plan/grid/build", null, {
    params: { limit },
    timeout: 600000,
  });

// 回测引擎
// 回测为长任务（全市场可达数十分钟，且首次数据完整性校验可能较慢），
// 相关请求放宽超时，避免默认 30s 触发 "timeout of 30000ms exceeded" 误判失败
const BACKTEST_TIMEOUT = 120000; // 120s
export const triggerBacktest = (params?: any) =>
  api.post("/backtest/run", params || {}, { timeout: BACKTEST_TIMEOUT });
export const getBacktestStatus = () =>
  api.get("/backtest/status", { timeout: BACKTEST_TIMEOUT });
export const getBacktestReport = () =>
  api.get("/backtest/report", { timeout: BACKTEST_TIMEOUT });
export const getBacktestSegments = (label?: string) =>
  api.get("/backtest/segments", { params: { label: label || "all" } });
