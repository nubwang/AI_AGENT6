<script setup lang="ts">
import { ref, onMounted, computed } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  getEvolveStatus,
  getEvolveEvents,
  reportEvolveEvent,
  triggerEvolveEvaluate,
  confirmEvolveProposal,
  settleEvolveShadow,
  rollbackEvolve,
  getEvolveRestartStatus,
  triggerEvolveRestart,
  pauseEvolve,
  resumeEvolve,
  getEvolveVerifyStatus,
  triggerEvolveVerify,
  getEvolveVerifyReport,
  getEvolveCodeStatus,
  triggerEvolveCodePropose,
  applyEvolveCode,
  rollbackEvolveCode,
  getEvolveMap,
  getEvolveBacktestDiag,
  getEvolveKbStats,
  getEvolveKbExperiments,
  getEvolveKbLessons,
  searchEvolveKb,
  backfillEvolveKb,
  maintainEvolveKb,
  feedbackEvolveKbLesson,
  getEvolvePlanStats,
  getEvolvePlanList,
  triggerEvolvePlanReplay,
  triggerEvolvePlanBackfill,
  distillEvolvePlanLessons,
  getEvolvePlanGrid,
  buildEvolvePlanGrid,
  getEvolveKbEvidence,
  getEvolvePermissions,
  getEvolveAutoApply,
} from "../api";

const loading = ref(false);
const status = ref<any>({});
const restartStatus = ref<any>({ pending: null });
const events = ref<any[]>([]);
const error = ref("");
const verifyStatus = ref<any>({});
const verifyReport = ref<any>({});
const codeStatus = ref<any>({});
const systemMap = ref<any>({});
const mapOpen = ref(["daily"]);
const btDiag = ref<any>({});

const pending = computed(() => status.value?.pending || []);
const shadow = computed(() => status.value?.shadow || {});
const guard = computed(() => status.value?.guard || {});
const ledger = computed(() => status.value?.ledger || {});
const paused = computed(() => !!status.value?.paused);
const approvalMode = computed(() => status.value?.approval_mode || "manual");
const autoMode = computed(() => approvalMode.value === "auto");
const recentProposals = computed(() => status.value?.recent_proposals || []);

const shadowEntries = computed(() => {
  const st = shadow.value;
  if (!st || typeof st !== "object") return [];
  return Object.entries(st).map(([param, rec]: any) => ({ param, ...rec }));
});

const restartPending = computed(() => restartStatus.value?.pending || null);
const verifyLatest = computed(
  () => verifyStatus.value?.report || verifyReport.value?.latest || null,
);
const verifyRunning = computed(() => verifyStatus.value?.status === "running");
const codeIdle = computed(() => !!codeStatus.value?.idle?.idle);
const codeBusy = computed(() => codeStatus.value?.idle?.busy || []);
const codePending = computed(
  () => codeStatus.value?.state?.pending_patches || [],
);
const codeHistory = computed(() => codeStatus.value?.state?.history || []);
const codeEnabled = computed(() => !!codeStatus.value?.code_evolution?.enabled);
const codeApproval = computed(
  () => codeStatus.value?.code_evolution?.approval || "manual",
);
// 系统图谱（plans/18）
const mapDaily = computed(
  () => systemMap.value?.daily_recommend_pipeline || [],
);
const mapBacktest = computed(() => systemMap.value?.backtest_pipeline || []);
const mapParams = computed(() => systemMap.value?.evolution_params || []);
const mapProducts = computed(() => systemMap.value?.products || []);
const mapForms = computed(() => systemMap.value?.form_types || []);
const mapFeatures = computed(() => systemMap.value?.feature_layout || {});
const mapConstants = computed(() => systemMap.value?.key_constants || []);
const mapAnchors = computed(() => systemMap.value?.change_anchors || []);
const mapKbs = computed(() => systemMap.value?.knowledge_bases || []);
const mapSequence = computed(() => systemMap.value?.run_sequence || []);

// ── 私有域知识库（plans/21：进化大脑长期记忆）──
const kbStats = ref<any>({});
const kbExperiments = ref<any[]>([]);
const kbChanges = ref<any[]>([]);
const kbVetoCounts = ref<any>({});
const kbLessons = ref<any[]>([]);
const kbCases = ref<any[]>([]);
const kbPairs = ref<any[]>([]);
const kbQuery = ref("");
const kbForm = ref("");
const kbSide = ref("");
const kbLessonFilter = ref("active");
// 实证结论（plans/23·24·25 的模块验证产出）：洗盘 A/B、四类买点、博弈特征 FDR、
// 情绪门控、纪律网格、自证样本外超额、我们 vs 基准、数据可信度…（进化大脑必读）
const kbEvidence = ref<any[]>([]);
const kbEvidenceStats = ref<any>({});
const kbEvidenceModule = ref("");
// 进化大脑权限域与唯一判据（用户决策：介入全部；唯一原则=D+1 + 主升浪准确率）
const permissions = ref<any>({});
// 自动生效状态：待重启 / 交易窗口 / DeepSeek 时段 / 午间自进化计划
const autoApply = ref<any>({});

async function loadKb() {
  try {
    // 教训筛选：active（高置信）/ candidate（观察中）/ reference（外部软知识挂引用）/ 全部
    const lp: any = { limit: 30 };
    if (kbLessonFilter.value) lp.status = kbLessonFilter.value;
    lp.min_n = kbLessonFilter.value === "reference" ? 1 : 2;
    const [ks, ke, kl, kev, perm, aa] = await Promise.all([
      getEvolveKbStats(),
      getEvolveKbExperiments({ limit: 40 }),
      getEvolveKbLessons(lp),
      getEvolveKbEvidence({ limit: 60 }),
      getEvolvePermissions(),
      getEvolveAutoApply(),
    ]);
    permissions.value = perm.data || {};
    autoApply.value = aa.data || {};
    kbStats.value = ks.data || {};
    kbExperiments.value = ke.data?.experiments || [];
    kbChanges.value = ke.data?.changes || [];
    kbVetoCounts.value = ke.data?.veto_counts || {};
    kbLessons.value = kl.data?.lessons || [];
    kbEvidence.value = kev.data?.items || [];
    kbEvidenceStats.value = kev.data?.stats || {};
  } catch (e: any) {
    // 知识库面板失败不影响进化中心其它面板
    console.warn("知识库加载失败", e?.message);
  }
}

async function runKbSearch() {
  try {
    const res = await searchEvolveKb({
      q: kbQuery.value,
      form: kbForm.value || undefined,
      side: kbSide.value || undefined,
      limit: 10,
    });
    kbCases.value = res.data?.cases || [];
    kbPairs.value = res.data?.control_pairs?.pairs || [];
    kbLessons.value = res.data?.lessons?.length
      ? res.data.lessons
      : kbLessons.value;
  } catch (e: any) {
    ElMessage.error(
      e?.response?.data?.detail || e?.message || "知识库检索失败",
    );
  }
}

async function runKbBackfill() {
  try {
    await ElMessageBox.confirm(
      "从既有 次日验证/反思/回测/改动记录 回填历史到私有域知识库？（幂等，可重复执行）",
      "回填历史知识",
      { type: "warning" },
    );
  } catch {
    return;
  }
  try {
    const res = await backfillEvolveKb(60);
    ElMessage.success(res.data?.message || "回填已提交");
    await loadKb();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || "回填失败");
  }
}

async function runKbMaintain() {
  try {
    const res = await maintainEvolveKb();
    ElMessage.success(
      `维护完成：衰减 ${res.data?.decayed ?? 0} 条教训，索引 ${res.data?.indexed ?? 0} 条`,
    );
    await loadKb();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || "维护失败");
  }
}

async function retireKbLesson(lesson: any) {
  try {
    await ElMessageBox.confirm(
      `确认废弃该教训（防错误归因被反复继承）？\n${lesson.text}`,
      "人工纠正教训",
      { type: "warning" },
    );
  } catch {
    return;
  }
  try {
    await feedbackEvolveKbLesson(lesson.id, {
      status: "retired",
      note: "人工判定为错误/失效教训",
    });
    ElMessage.success("已废弃（保留历史记录）");
    await loadKb();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || "操作失败");
  }
}

// 首屏拆两段：① 快的元信息并行取（页面立刻可用）；
// ② 进化状态（守护巡检 + L0 摘要，含全表 SQL 探针，冷启动可能数秒）单独请求，
//    失败/超时只影响状态卡片，不阻断其它面板。
// 2026-09-23 故障复盘：/evolve/status 冷启动 38~73s → 整页 17 个请求排队 → axios 30s 超时。
async function load() {
  loading.value = true;
  error.value = "";
  try {
    const [r, ev, vs, vr, cs, mp, bd] = await Promise.all([
      getEvolveRestartStatus(),
      getEvolveEvents(30),
      getEvolveVerifyStatus(),
      getEvolveVerifyReport(),
      getEvolveCodeStatus(),
      getEvolveMap(),
      getEvolveBacktestDiag(),
    ]);
    restartStatus.value = r.data;
    events.value = ev.data?.events || [];
    verifyStatus.value = vs.data;
    verifyReport.value = vr.data;
    codeStatus.value = cs.data;
    systemMap.value = mp.data;
    btDiag.value = bd.data;
  } catch (e: any) {
    error.value =
      e?.response?.data?.detail || e?.message || "加载进化元信息失败";
  } finally {
    loading.value = false;
  }
  await Promise.all([loadStatus(), loadKb(), loadPlan()]);
}

async function loadStatus() {
  try {
    const s = await getEvolveStatus();
    status.value = s.data;
  } catch (e: any) {
    // 慢接口独立失败：仅提示 + 该卡片留空，其它面板照常展示
    console.warn("进化状态加载失败", e?.message || e);
    ElMessage.warning(
      e?.code === "ECONNABORTED"
        ? "进化状态加载超时（守护巡检较慢），请稍后点『刷新』重试"
        : e?.response?.data?.detail || e?.message || "进化状态加载失败",
    );
  }
}

const kbLessonFiltered = computed(() =>
  kbLessons.value.filter(
    (l: any) => !kbLessonFilter.value || l.status === kbLessonFilter.value,
  ),
);
// 实证结论按模块筛选（模块清单来自后端 stats.by_module）
const kbEvidenceFiltered = computed(() =>
  kbEvidence.value.filter(
    (e: any) => !kbEvidenceModule.value || e.module === kbEvidenceModule.value,
  ),
);
const kbCounts = computed(() => ({
  cases: kbStats.value?.index?.cases ?? 0,
  lessons: kbStats.value?.index?.lessons ?? 0,
  experiments: kbStats.value?.index?.experiments ?? 0,
  changes: kbStats.value?.index?.changes ?? 0,
  vectors: kbStats.value?.index?.vectors ?? 0,
  plans: kbStats.value?.index?.plans ?? 0,
  evidence: kbStats.value?.index?.evidence ?? kbEvidenceStats.value?.n ?? 0,
}));

// ── 交易纪律（plans/22：纪律进化——计划台账 / 触价回放 / 反事实最优纪律）──
const planStats = ref<any>({});
const planRows = ref<any[]>([]);
const planGrid = ref<any>({});
const planLoading = ref(false);
const planFormFilter = ref("");
const planFailureFilter = ref("");

const FAILURE_CN: Record<string, string> = {
  missed_gap_up: "高开未追错失大涨",
  missed_fill: "高开超上限未成交",
  chased_high: "追高被套",
  stop_whipsaw: "止损后反弹(假摔)",
  take_profit_early: "止盈过早",
  expired_no_gain: "到期不达标",
  no_stop_loss: "未止损扩大亏损",
};
const failureCn = (k: string) => FAILURE_CN[k] || k || "-";
const fmtPctN = (v: any, digits = 2) =>
  v === null || v === undefined || v === ""
    ? "-"
    : `${(Number(v) * 100).toFixed(digits)}%`;
const fmtSigned = (v: any, digits = 2) =>
  v === null || v === undefined || v === ""
    ? "-"
    : `${Number(v) >= 0 ? "+" : ""}${(Number(v) * 100).toFixed(digits)}%`;

async function loadPlan() {
  planLoading.value = true;
  try {
    const [st, ls, gd] = await Promise.all([
      getEvolvePlanStats(60),
      getEvolvePlanList({
        limit: 60,
        form: planFormFilter.value || undefined,
        failure: planFailureFilter.value || undefined,
      }),
      getEvolvePlanGrid(),
    ]);
    planStats.value = st.data || {};
    planRows.value = ls.data?.rows || [];
    planGrid.value = gd.data || {};
  } catch (e: any) {
    // 静默（后端可能尚未重启加载新接口），仅记录，避免首屏刷错误提示
    console.warn("加载交易纪律数据失败", e?.message || e);
  } finally {
    planLoading.value = false;
  }
}

const planFailureOptions = computed(() =>
  Object.entries(planStats.value?.failure_type_pct || {})
    .sort((a: any, b: any) => Number(b[1]) - Number(a[1]))
    .map(([k, v]) => ({
      key: k,
      label: `${failureCn(k)}（${fmtPctN(v, 0)}）`,
    })),
);

const planGridRows = computed(() =>
  Object.entries(planGrid.value?.grid?.by_form || {}).map(([form, v]: any) => ({
    form,
    ...(v || {}),
  })),
);

const planAdoptedForms = computed(() => {
  const a = planGrid.value?.adopted || {};
  return Object.keys(a).filter((k) => a[k] && a[k].plan_stop_loss_pct);
});

async function runPlanReplay(force = false) {
  try {
    const res = await triggerEvolvePlanReplay({ force, limit: 300 });
    ElMessage.success(res.data?.message || "回放已提交");
    setTimeout(loadPlan, 5000);
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || "触发回放失败");
  }
}

async function runPlanBackfill() {
  try {
    await ElMessageBox.confirm(
      "用历史 EKB 案例重建操作计划并做触价回放？（基准价取该案例推荐日收盘价，耗时 1~3 分钟）",
      "纪律样本回填",
      { type: "warning" },
    );
  } catch {
    return;
  }
  try {
    const res = await triggerEvolvePlanBackfill(600);
    ElMessage.success(res.data?.message || "回填已提交");
    setTimeout(loadPlan, 10000);
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || "回填失败");
  }
}

async function runPlanGrid() {
  try {
    const res = await buildEvolvePlanGrid(600);
    ElMessage.success(res.data?.message || "纪律网格重建已提交");
    setTimeout(loadPlan, 10000);
  } catch (e: any) {
    ElMessage.error(
      e?.response?.data?.detail || e?.message || "重建纪律网格失败",
    );
  }
}

async function runPlanLessons() {
  try {
    const res = await distillEvolvePlanLessons(60);
    ElMessage.success(`已沉淀纪律教训 ${res.data?.lessons ?? 0} 条`);
    await loadPlan();
  } catch (e: any) {
    ElMessage.error(
      e?.response?.data?.detail || e?.message || "沉淀纪律教训失败",
    );
  }
}

async function runVerify() {
  try {
    const res = await triggerEvolveVerify();
    ElMessage.success(res.data?.message || "次日自我验证已提交");
    await load();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || "触发验证失败");
  }
}

async function proposeCode() {
  try {
    await ElMessageBox.confirm(
      "让进化大脑基于近期表现生成一段替换每日推荐逻辑的代码提案？（AST 白名单安全扫描 + 待确认）",
      "生成式代码进化",
      { type: "warning" },
    );
  } catch {
    return;
  }
  try {
    const res = await triggerEvolveCodePropose();
    ElMessage.success(res.data?.message || "代码提案已生成");
    await load();
  } catch (e: any) {
    ElMessage.error(
      e?.response?.data?.detail || e?.message || "生成代码提案失败",
    );
  }
}

async function applyCode(id: number) {
  try {
    await ElMessageBox.confirm(
      "确认应用该代码提案？（备份 → AST 白名单 → 编译验证 → 失败自动回滚，需独立重启生效）",
      "应用代码提案",
      { type: "warning" },
    );
  } catch {
    return;
  }
  try {
    const res = await applyEvolveCode(id);
    if (res.data?.ok) {
      ElMessage.success(res.data?.message || "代码提案已应用（待重启生效）");
    } else {
      ElMessage.warning(res.data?.message || "应用失败");
    }
    await load();
  } catch (e: any) {
    ElMessage.error(
      e?.response?.data?.detail || e?.message || "应用代码提案失败",
    );
  }
}

async function rollbackCode() {
  try {
    await ElMessageBox.confirm(
      "回滚最近一次生成式代码补丁（恢复版本化备份）？",
      "回滚代码补丁",
      { type: "warning" },
    );
  } catch {
    return;
  }
  try {
    const res = await rollbackEvolveCode();
    if (res.data?.ok) {
      ElMessage.success(`已回滚代码补丁（${res.data?.restored || ""}）`);
    } else {
      ElMessage.warning(res.data?.message || "无可回滚记录");
    }
    await load();
  } catch (e: any) {
    ElMessage.error(
      e?.response?.data?.detail || e?.message || "回滚代码补丁失败",
    );
  }
}

async function runEvaluate() {
  try {
    const res = await triggerEvolveEvaluate();
    const n = res.data?.proposals?.length ?? 0;
    const msg = res.data?.auto
      ? `评估完成：提案 ${n} 个，自动进化 ${res.data?.auto_confirmed?.length ?? 0} 个（拒绝 ${res.data?.rejected ?? 0}）`
      : `评估完成：提案 ${n} 个，拒绝 ${res.data?.rejected ?? 0} 个`;
    ElMessage.success(msg);
    await load();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || "评估失败");
  }
}

async function confirm(id: number) {
  try {
    await ElMessageBox.confirm(
      "确认应用该改进提案？（Tier1 确定性参数将先进影子期验证）",
      "人工确认",
      {
        type: "warning",
      },
    );
  } catch {
    return;
  }
  try {
    const res = await confirmEvolveProposal(id);
    ElMessage.success(res.data?.message || "已确认");
    await load();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || "确认失败");
  }
}

async function settle(param: string) {
  try {
    const res = await settleEvolveShadow(param);
    if (res.data?.ok) {
      ElMessage.success(`结算：${res.data?.status || "applied"}`);
    } else {
      ElMessage.warning(res.data?.reason || `样本不足继续`);
    }
    await load();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || "结算失败");
  }
}

async function rollback() {
  try {
    await ElMessageBox.confirm(
      "回滚最近一次成功应用（配置旧值 / 代码补丁恢复 .bak）？",
      "回滚",
      {
        type: "warning",
      },
    );
  } catch {
    return;
  }
  try {
    const res = await rollbackEvolve();
    if (res.data?.ok) {
      ElMessage.success(`已回滚 ${res.data?.param || ""}`);
    } else {
      ElMessage.warning(res.data?.message || "无可回滚记录");
    }
    await load();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || "回滚失败");
  }
}

async function triggerRestart() {
  try {
    await ElMessageBox.confirm(
      "触发独立进程重启以生效 Tier2/3 代码改动？脚本将自动健康检查并在失败时回滚 .bak。",
      "独立进程重启（E2/G9）",
      { type: "warning" },
    );
  } catch {
    return;
  }
  try {
    const res = await triggerEvolveRestart();
    ElMessage.success(res.data?.message || "已触发重启");
    await load();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || "触发重启失败");
  }
}

async function doPause() {
  try {
    const res = await pauseEvolve("人工暂停");
    ElMessage.success(res.data?.ok ? "进化已暂停" : "暂停失败");
    await load();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || "暂停失败");
  }
}

async function doResume() {
  try {
    const res = await resumeEvolve();
    ElMessage.success(res.data?.ok ? "进化已恢复" : "恢复失败");
    await load();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || "恢复失败");
  }
}

const fmtPct = (v: number | undefined) =>
  v === undefined || v === null ? "-" : `${(v * 100).toFixed(1)}%`;

const statusTag = (st: string) => {
  const map: Record<string, string> = {
    pending: "warning",
    shadowing: "primary",
    running: "primary",
    applied: "success",
    rolled_back: "danger",
    failed: "danger",
  };
  return map[st] || "info";
};

function eventTag(level: string) {
  const map: Record<string, string> = {
    ERROR: "danger",
    WARNING: "warning",
    INFO: "info",
    DEBUG: "info",
  };
  return map[level] || "info";
}

onMounted(() => {
  load();
  // 前端事件上报（§15.2）：进化中心打开时上报一条 INFO 事件，验证端到端链路
  reportEvolveEvent("INFO", "frontend", "进化中心页面已打开").catch(() => {});
});
</script>

<template>
  <div style="padding: 20px">
    <el-card shadow="never">
      <template #header>
        <div
          style="
            display: flex;
            justify-content: space-between;
            align-items: center;
          "
        >
          <span>🧬 进化中心（Evolution Agent）</span>
          <div>
            <el-tag v-if="paused" type="danger" style="margin-right: 12px"
              >⚠️ 已暂停（守护）</el-tag
            >
            <el-tag
              :type="autoMode ? 'success' : 'info'"
              style="margin-right: 12px"
              >{{
                autoMode ? "🤖 自动进化（无需人工确认）" : "👤 人工确认模式"
              }}</el-tag
            >
            <el-button size="small" @click="load" :loading="loading"
              >刷新</el-button
            >
            <el-button
              size="small"
              type="primary"
              @click="runEvaluate"
              :loading="loading"
              >手工评估（生成提案）</el-button
            >
            <el-button size="small" type="warning" @click="rollback"
              >回滚最近改进</el-button
            >
            <el-button
              size="small"
              type="danger"
              @click="triggerRestart"
              :disabled="!restartPending"
            >
              重启生效 Tier2/3
            </el-button>
            <el-button
              size="small"
              :type="paused ? 'success' : 'warning'"
              @click="paused ? doResume() : doPause()"
              :loading="loading"
              style="margin-left: 12px"
            >
              {{ paused ? "恢复进化" : "暂停进化" }}
            </el-button>
          </div>
        </div>
      </template>

      <el-alert
        v-if="error"
        :title="error"
        type="error"
        show-icon
        closable
        style="margin-bottom: 16px"
      />
      <el-alert
        v-if="restartPending"
        :title="`待重启：${restartPending.param} → ${restartPending.new}（${restartPending.ts}）——Tier2/3 代码已补丁，重启后才生效`"
        type="warning"
        show-icon
        style="margin-bottom: 16px"
      />
    </el-card>

    <!-- 状态总览 -->
    <el-row :gutter="16" style="margin-top: 16px">
      <el-col :span="6">
        <el-card shadow="never">
          <div style="font-size: 26px; font-weight: bold">
            {{ pending.length }}
          </div>
          <div style="color: #909399; margin-top: 4px">待确认提案</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div style="font-size: 26px; font-weight: bold">
            {{ shadowEntries.length }}
          </div>
          <div style="color: #909399; margin-top: 4px">影子实验运行中</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div style="font-size: 26px; font-weight: bold">
            {{ ledger.closed_total || 0 }}
          </div>
          <div style="color: #909399; margin-top: 4px">
            已结账改进（{{ ledger.positive_closed || 0 }} 项提升）
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div style="font-size: 26px; font-weight: bold">
            {{ restartPending ? "有" : "无" }}
          </div>
          <div style="color: #909399; margin-top: 4px">待重启代码改动</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 次日自我验证（plans/17：新日线行情出来后，验证"为什么推荐的股票没涨"） -->
    <el-card shadow="never" style="margin-top: 16px">
      <template #header>
        <div
          style="
            display: flex;
            justify-content: space-between;
            align-items: center;
          "
        >
          <span>🔁 次日自我验证（推荐后第 2 天新日线 → 回答"为什么没涨"）</span>
          <el-button
            size="small"
            type="primary"
            @click="runVerify"
            :loading="verifyRunning"
            :disabled="verifyRunning"
          >
            {{ verifyRunning ? "验证中…" : "触发次日验证" }}
          </el-button>
        </div>
      </template>

      <template v-if="verifyLatest">
        <el-row :gutter="16">
          <el-col :span="4">
            <div style="font-size: 26px; font-weight: bold; color: #409eff">
              {{ ((verifyLatest.t1_hit_rate || 0) * 100).toFixed(1) }}%
            </div>
            <div style="color: #909399; margin-top: 4px">T+1 次日命中率</div>
          </el-col>
          <el-col :span="4">
            <div style="font-size: 26px; font-weight: bold; color: #67c23a">
              {{ verifyLatest.up_n || 0 }}
            </div>
            <div style="color: #909399; margin-top: 4px">上涨</div>
          </el-col>
          <el-col :span="4">
            <div style="font-size: 26px; font-weight: bold; color: #909399">
              {{ verifyLatest.flat_n || 0 }}
            </div>
            <div style="color: #909399; margin-top: 4px">平盘</div>
          </el-col>
          <el-col :span="4">
            <div style="font-size: 26px; font-weight: bold; color: #f56c6c">
              {{ verifyLatest.down_n || 0 }}
            </div>
            <div style="color: #909399; margin-top: 4px">下跌（失败）</div>
          </el-col>
          <el-col :span="4">
            <div style="font-size: 26px; font-weight: bold">
              {{ ((verifyLatest.avg_t1_ret || 0) * 100).toFixed(2) }}%
            </div>
            <div style="color: #909399; margin-top: 4px">平均 T+1 收益</div>
          </el-col>
          <el-col :span="4">
            <div style="font-weight: bold">推荐日 {{ verifyLatest.date }}</div>
            <div style="color: #909399; margin-top: 4px; font-size: 12px">
              {{ verifyLatest.verified_at }}
            </div>
          </el-col>
        </el-row>

        <el-alert
          v-if="verifyLatest.composite != null"
          type="success"
          :title="`综合目标分 ${((verifyLatest.composite || 0) * 100).toFixed(1)}%（必须涨+主升浪：T+1×${(1 - (verifyLatest.wave_w ?? 0.4)).toFixed(1)} + 主升浪×${(verifyLatest.wave_w ?? 0.4).toFixed(1)}）`"
          show-icon
          style="margin-top: 12px"
        />

        <el-alert
          v-if="(verifyLatest.t1_hit_rate || 0) < 0.5"
          type="warning"
          :title="`次日命中率 ${((verifyLatest.t1_hit_rate || 0) * 100).toFixed(1)}% 偏低，进化中心将据此调整每日推荐/回测逻辑`"
          show-icon
          style="margin-top: 12px"
        />

        <div v-if="verifyLatest.improvements?.length" style="margin-top: 12px">
          <span style="font-weight: bold; margin-right: 6px">改进方向：</span>
          <el-tag
            v-for="imp in verifyLatest.improvements"
            :key="imp"
            size="small"
            type="danger"
            style="margin-right: 6px"
            >{{ imp }}</el-tag
          >
        </div>
        <div v-if="verifyLatest.attributions?.length" style="margin-top: 8px">
          <span style="font-weight: bold; margin-right: 6px">失效归因：</span>
          <el-tag
            v-for="a in verifyLatest.attributions"
            :key="a.error_type"
            size="small"
            :type="a.count >= 3 ? 'warning' : 'info'"
            style="margin-right: 6px"
            >{{ a.error_type }}（{{ a.count }} 例）</el-tag
          >
        </div>
        <!-- 环节归因（plans/18：定位回测/每日推荐哪个环节出问题 → 优化方向） -->
        <div v-if="verifyLatest.stage_report?.length" style="margin-top: 8px">
          <span style="font-weight: bold; margin-right: 6px"
            >🔧 环节归因（哪环节出问题）：</span
          >
          <el-tag
            v-for="s in verifyLatest.stage_report"
            :key="s.stage"
            size="small"
            :type="s.count >= 3 ? 'danger' : 'warning'"
            style="margin-right: 6px"
            >{{ s.stage_name }}（{{ s.count }} 例）</el-tag
          >
          <div
            v-if="verifyLatest.stage_report[0]"
            style="
              margin-top: 6px;
              font-size: 12px;
              color: #606266;
              line-height: 1.6;
            "
          >
            ⚠️ 最需优化：{{ verifyLatest.stage_report[0].stage_name }} —
            {{ verifyLatest.stage_report[0].stage_hint }}<br />💡 优化方向：{{
              verifyLatest.stage_report[0].optimize
            }}
          </div>
        </div>

        <!-- 外因/内因分离（plans/17 §十一）：从大盘/个股日线/个股资料找原因 -->
        <div
          v-if="verifyLatest.external?.n || verifyLatest.external?.unknown_n"
          style="margin-top: 8px"
        >
          <span style="font-weight: bold; margin-right: 6px"
            >🌐 外因归因：</span
          >
          <el-tag size="small" type="info" style="margin-right: 6px">
            {{ Math.round((verifyLatest.external?.pct || 0) * 100) }}%
            属外因（{{ verifyLatest.external?.n || 0 }}
            例）
          </el-tag>
          <span style="font-size: 12px; color: #909399">
            大盘拖累/个股利空/技术破位 →
            已排除在「改进方向」之外（不拿外因改形态/阈值）
          </span>
          <div
            v-for="(e, i) in verifyLatest.external?.examples || []"
            :key="i"
            style="
              font-size: 12px;
              color: #606266;
              line-height: 1.6;
              margin-top: 4px;
            "
          >
            · {{ e.ts_code }} — {{ e.cause }}：{{ e.basis }}
          </div>
        </div>

        <el-table
          :data="verifyLatest.failures || []"
          size="small"
          style="margin-top: 12px"
          max-height="320"
          empty-text="本次无失败（推荐次日全部上涨）"
        >
          <el-table-column prop="ts_code" label="代码" width="110" />
          <el-table-column prop="name" label="名称" width="120" />
          <el-table-column label="次日涨幅" width="100">
            <template #default="{ row }">
              <span
                :style="{
                  color: (row.t1_ret || 0) >= 0 ? '#67c23a' : '#f56c6c',
                }"
                >{{ ((row.t1_ret || 0) * 100).toFixed(2) }}%</span
              >
            </template>
          </el-table-column>
          <el-table-column prop="form_type" label="形态" width="70" />
          <el-table-column label="相似度" width="90">
            <template #default="{ row }">{{
              row.similarity?.toFixed(3) ?? "-"
            }}</template>
          </el-table-column>
          <el-table-column prop="stage" label="环节" width="110" />
          <el-table-column prop="error_type" label="失效归因" width="140" />
          <el-table-column label="外部证据（外因）" min-width="220">
            <template #default="{ row }">
              <div v-if="row.external_cause" style="line-height: 1.5">
                <el-tag
                  size="small"
                  :type="
                    String(row.external_cause).includes('无（内因）')
                      ? 'info'
                      : 'warning'
                  "
                >
                  {{ row.external_cause }}
                </el-tag>
                <div
                  v-if="row.evidence_basis"
                  style="font-size: 12px; color: #909399; margin-top: 2px"
                >
                  {{ row.evidence_basis }}
                </div>
              </div>
              <span v-else style="color: #c0c4cc">—</span>
            </template>
          </el-table-column>
          <el-table-column
            prop="lesson"
            label="教训"
            min-width="160"
            show-overflow-tooltip
          />
          <el-table-column
            prop="fix"
            label="修正建议"
            min-width="160"
            show-overflow-tooltip
          />
          <el-table-column
            prop="param_hint"
            label="参数方向"
            width="160"
            show-overflow-tooltip
          />
        </el-table>
      </template>
      <el-empty
        v-else
        description="暂无次日验证结果（每日 17:30 自动执行，或点击右上角手动触发）"
      />
    </el-card>

    <!-- 私有域知识库（plans/21：失败/成功原因 + 回测结果 + 改动因果 + 已试无效方案，沉淀供综合分析） -->
    <el-card shadow="never" style="margin-top: 16px">
      <template #header>
        <div
          style="
            display: flex;
            align-items: center;
            justify-content: space-between;
          "
        >
          <span
            >🧠 私有域知识库（为什么涨 / 为什么没涨 / 试过什么 /
            结果如何）</span
          >
          <div>
            <el-button size="small" @click="runKbBackfill"
              >回填历史知识</el-button
            >
            <el-button size="small" type="primary" @click="runKbMaintain">
              维护与索引
            </el-button>
          </div>
        </div>
      </template>

      <el-row :gutter="16">
        <el-col :span="4">
          <div style="font-size: 24px; font-weight: bold">
            {{ kbCounts.cases }}
          </div>
          <div style="color: #909399">案例（成功+失败）</div>
        </el-col>
        <el-col :span="4">
          <div style="font-size: 24px; font-weight: bold">
            {{ kbCounts.lessons }}
          </div>
          <div style="color: #909399">教训/规律</div>
        </el-col>
        <el-col :span="4">
          <div style="font-size: 24px; font-weight: bold">
            {{ kbCounts.experiments }}
          </div>
          <div style="color: #909399">实验台账</div>
        </el-col>
        <el-col :span="4">
          <div style="font-size: 24px; font-weight: bold">
            {{ kbCounts.changes }}
          </div>
          <div style="color: #909399">改动因果链</div>
        </el-col>
        <el-col :span="4">
          <div style="font-size: 24px; font-weight: bold">
            {{ kbCounts.vectors }}
          </div>
          <div style="color: #909399">语义索引</div>
        </el-col>
        <el-col :span="4">
          <div style="font-size: 24px; font-weight: bold">
            {{ kbStats?.lessons?.by_status?.active ?? 0 }}
          </div>
          <div style="color: #909399">高置信教训</div>
        </el-col>
        <el-col :span="4">
          <div style="font-size: 24px; font-weight: bold">
            {{ kbCounts.evidence }}
          </div>
          <div style="color: #909399">实证结论</div>
        </el-col>
        <el-col :span="4">
          <div style="font-size: 24px; font-weight: bold">
            {{ kbCounts.plans }}
          </div>
          <div style="color: #909399">交易计划台账</div>
        </el-col>
      </el-row>

      <el-alert
        style="margin-top: 12px"
        type="info"
        show-icon
        :closable="false"
        title="进化大脑每次分析都会先检索本知识库（历史同类失败/成功对照、已试改动结论、否证清单）；同类改动被证明无效达阈值将被硬拒，避免重复试错。"
      />

      <el-alert
        style="margin-top: 8px"
        type="warning"
        show-icon
        :closable="false"
        title="进化大脑权限域：可介入全部业务代码（app/**、scripts/**、frontend/src/**）并可新建文件/模块；唯一不可动的是「度量本身」——否则可改口径伪造提升，唯一原则无法被证明。"
      >
        <div style="font-size: 12px; line-height: 1.7">
          <b>唯一判据</b>：
          {{
            (permissions?.metric?.primary || []).join(" + ") ||
            "t1_hit_rate + wave_hit_rate"
          }}
          （D+1 命中率 + 主升浪命中率；口径 immutable，守护哈希
          {{
            permissions?.constitution_ok ? "校验通过 ✅" : "校验异常 ⚠️"
          }}）<br />
          <b>可改</b>：{{ permissions?.scope?.editable_files_n ?? 0 }} 个文件（
          {{ (permissions?.scope?.editable_roots || []).join("、") }}），支持
          {{ (permissions?.scope?.modes || []).join(" / ") }} 模式<br />
          <b>锁定</b>：{{ (permissions?.locked_files || []).length }} 个文件 +
          {{ (permissions?.locked_funcs || []).length }}
          个度量核心函数（命中判定不可替换）
        </div>
      </el-alert>

      <div
        style="
          margin-top: 14px;
          font-weight: bold;
          display: flex;
          align-items: center;
          gap: 8px;
        "
      >
        <span>🔬 实证结论（回测/检验过的事实，进化大脑每次分析必读）</span>
        <el-select
          v-model="kbEvidenceModule"
          size="small"
          style="width: 220px"
          clearable
          placeholder="全部模块"
        >
          <el-option
            v-for="(n, m) in kbEvidenceStats?.by_module || {}"
            :key="m"
            :label="`${m}（${n}）`"
            :value="m"
          />
        </el-select>
      </div>
      <el-table
        :data="kbEvidenceFiltered"
        size="small"
        max-height="280"
        style="margin-top: 6px"
      >
        <el-table-column label="模块" width="150">
          <template #default="{ row }">{{ row.module || "-" }}</template>
        </el-table-column>
        <el-table-column prop="text" label="实证结论" show-overflow-tooltip />
        <el-table-column label="样本" width="90">
          <template #default="{ row }">{{ row.n ?? "-" }}</template>
        </el-table-column>
        <el-table-column label="置信度" width="90">
          <template #default="{ row }">
            {{ ((row.confidence ?? 0) * 100).toFixed(0) }}%
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.status === 'active' ? 'success' : 'info'"
            >
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="hint" label="可操作建议" show-overflow-tooltip />
      </el-table>

      <div style="margin-top: 14px; font-weight: bold">
        📋 实验台账（改了什么 / 预期依据 / 结论 / 未达预期原因）
      </div>
      <el-table
        :data="kbExperiments"
        size="small"
        max-height="280"
        style="margin-top: 6px"
      >
        <el-table-column prop="ts" label="时间" width="150" />
        <el-table-column label="目标" width="200">
          <template #default="{ row }">
            {{
              row.target?.param || row.target?.file || row.target?.source || "-"
            }}
          </template>
        </el-table-column>
        <el-table-column label="改动" width="150">
          <template #default="{ row }">
            {{ row.change?.old }} →
            {{ row.change?.new ?? row.change?.kind ?? "" }}
          </template>
        </el-table-column>
        <el-table-column
          prop="hypothesis"
          label="预期/依据"
          show-overflow-tooltip
        />
        <el-table-column label="结论" width="110">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="
                row.verdict === 'improved'
                  ? 'success'
                  : row.verdict === 'invalid'
                    ? 'danger'
                    : row.verdict === 'worse' || row.verdict === 'rolled_back'
                      ? 'warning'
                      : 'info'
              "
            >
              {{ row.verdict || row.status || "未决" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          prop="verdict_reason"
          label="未达预期原因"
          show-overflow-tooltip
        />
      </el-table>

      <div
        style="
          margin-top: 14px;
          font-weight: bold;
          display: flex;
          align-items: center;
          gap: 8px;
        "
      >
        <span>📚 教训/规律（综合分析必查；n<10 仅作观察）</span>
        <el-select
          v-model="kbLessonFilter"
          size="small"
          style="width: 170px"
          @change="loadKb"
        >
          <el-option label="高置信（active）" value="active" />
          <el-option label="观察中（candidate）" value="candidate" />
          <el-option label="外部知识（reference）" value="reference" />
          <el-option label="全部" value="" />
        </el-select>
      </div>
      <el-table
        :data="kbLessonFiltered"
        size="small"
        max-height="260"
        style="margin-top: 6px"
      >
        <el-table-column prop="text" label="教训" show-overflow-tooltip />
        <el-table-column prop="type" label="类型" width="120" />
        <el-table-column label="环节" width="130">
          <template #default="{ row }">{{ row.scope?.stage || "-" }}</template>
        </el-table-column>
        <el-table-column label="样本" width="80">
          <template #default="{ row }">{{ row.support?.n ?? 0 }}</template>
        </el-table-column>
        <el-table-column label="置信度" width="90">
          <template #default="{ row }">
            {{ ((row.confidence ?? 0) * 100).toFixed(0) }}%
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.status === 'active' ? 'success' : 'info'"
            >
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button
              v-if="row.status !== 'retired'"
              size="small"
              text
              type="danger"
              @click="retireKbLesson(row)"
            >
              废弃
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div style="margin-top: 14px; font-weight: bold">
        🔍 案例检索（结构化 + 语义混合，检索结果即分析的"历史对照"）
      </div>
      <div
        style="margin-top: 6px; display: flex; gap: 8px; align-items: center"
      >
        <el-input
          v-model="kbQuery"
          placeholder="关键词，如：推荐未上涨 条件概率 市场环境"
          style="width: 320px"
          clearable
          @keyup.enter="runKbSearch"
        />
        <el-select
          v-model="kbSide"
          placeholder="方向"
          style="width: 120px"
          clearable
        >
          <el-option label="失败" value="failure" />
          <el-option label="成功" value="success" />
        </el-select>
        <el-input
          v-model="kbForm"
          placeholder="形态 A/B/C/D"
          style="width: 120px"
          clearable
        />
        <el-button type="primary" size="small" @click="runKbSearch"
          >检索</el-button
        >
      </div>
      <el-table
        :data="kbCases"
        size="small"
        max-height="260"
        style="margin-top: 6px"
      >
        <el-table-column prop="date" label="日期" width="100" />
        <el-table-column prop="ts_code" label="代码" width="110" />
        <el-table-column prop="form_type" label="形态" width="70" />
        <el-table-column label="方向" width="80">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.side === 'success' ? 'success' : 'danger'"
            >
              {{
                row.side === "success"
                  ? "上涨"
                  : row.side === "failure"
                    ? "未涨"
                    : "中性"
              }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="T+1" width="90">
          <template #default="{ row }">
            {{ ((row.outcome?.t1_ret ?? 0) * 100).toFixed(2) }}%
          </template>
        </el-table-column>
        <el-table-column label="超额" width="90">
          <template #default="{ row }">
            {{
              row.outcome?.excess_t1 == null
                ? "-"
                : (row.outcome.excess_t1 * 100).toFixed(2) + "%"
            }}
          </template>
        </el-table-column>
        <el-table-column label="归因" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.cause?.error_type || row.cause?.success_type || "未归因" }}
            {{ row.cause?.lesson ? "：" + row.cause.lesson : "" }}
          </template>
        </el-table-column>
      </el-table>
      <div v-if="kbPairs.length" style="margin-top: 8px; color: #606266">
        <div style="font-weight: bold">成功/失败对照（同日同形态）：</div>
        <div v-for="(p, i) in kbPairs" :key="i" style="font-size: 13px">
          · {{ p.success?.date }} {{ p.success?.ts_code }} 成功（T+1
          {{ ((p.success?.outcome?.t1_ret ?? 0) * 100).toFixed(2) }}%） vs
          <span v-for="(c, j) in p.controls" :key="j">
            {{ c.ts_code }} 未涨（{{ c.cause?.error_type || "未归因" }}）{{
              Number(j) < p.controls.length - 1 ? "、" : ""
            }}
          </span>
        </div>
      </div>
    </el-card>

    <!-- 生成式代码进化（plans/18：进化大脑像 ZOO CODE 一样自己写代码，闲时自动改回测/推荐逻辑） -->
    <el-card shadow="never" style="margin-top: 16px">
      <template #header>
        <div
          style="
            display: flex;
            justify-content: space-between;
            align-items: center;
          "
        >
          <span>🤖 生成式代码进化（进化大脑像 ZOO CODE 一样自己写代码）</span>
          <div>
            <el-button
              size="small"
              type="primary"
              @click="proposeCode"
              :loading="loading"
              >生成代码提案</el-button
            >
            <el-button size="small" type="warning" @click="rollbackCode"
              >回滚代码补丁</el-button
            >
          </div>
        </div>
      </template>
      <el-alert
        :title="
          codeEnabled
            ? `已开启：${codeApproval === 'auto' ? '自动' : '人工确认'}模式，进化目标=次日大涨+后续主升浪（每日 04:20 闲时自动执行）`
            : '生成式代码进化未开启（evolution_config.code_evolution.enabled=false）'
        "
        :type="codeEnabled ? (codeIdle ? 'success' : 'warning') : 'info'"
        show-icon
        :closable="false"
        style="margin-bottom: 12px"
      />
      <div v-if="!codeIdle" style="color: #e6a23c; margin-bottom: 12px">
        ⏳ 系统当前忙（{{ codeBusy.join("、") }}），生成式代码进化将等待闲时执行
      </div>

      <template v-if="codePending.length">
        <div style="font-weight: bold; margin-bottom: 8px">
          待确认代码提案（{{ codePending.length }}）
        </div>
        <el-table :data="codePending" size="small">
          <el-table-column prop="id" label="ID" width="60" />
          <el-table-column prop="ts" label="生成时间" width="150" />
          <el-table-column prop="target" label="目标锚点" width="200" />
          <el-table-column
            prop="reason"
            label="原因"
            min-width="160"
            show-overflow-tooltip
          />
          <el-table-column
            prop="evidence"
            label="证据"
            min-width="140"
            show-overflow-tooltip
          />
          <el-table-column label="新代码预览" min-width="200">
            <template #default="{ row }"
              ><code
                style="font-size: 12px; color: #409eff; white-space: pre-wrap"
                >{{ row.new_code }}</code
              ></template
            >
          </el-table-column>
          <el-table-column label="操作" width="110" align="center">
            <template #default="{ row }">
              <el-button size="small" type="success" @click="applyCode(row.id)"
                >确认应用</el-button
              >
            </template>
          </el-table-column>
        </el-table>
      </template>

      <el-table
        v-if="codeHistory.length"
        :data="codeHistory"
        size="small"
        style="margin-top: 12px"
      >
        <el-table-column prop="ts" label="时间" width="150" />
        <el-table-column prop="target" label="目标" width="200" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="
                row.status === 'applied'
                  ? 'success'
                  : row.status === 'rejected'
                    ? 'danger'
                    : 'info'
              "
              >{{ row.status }}</el-tag
            >
          </template>
        </el-table-column>
        <el-table-column
          prop="note"
          label="说明"
          min-width="260"
          show-overflow-tooltip
        />
      </el-table>
      <el-empty
        v-if="!codePending.length && !codeHistory.length"
        description="暂无代码提案（点击右上角'生成代码提案'，或等每日 04:20 闲时自动执行）"
      />
    </el-card>

    <!-- 回测健康诊断（plans/19：进化大脑控制回测，考虑是不是回测的问题） -->
    <el-card shadow="never" style="margin-top: 16px">
      <template #header
        ><span>🔬 回测健康诊断（是不是回测的问题）</span></template
      >
      <div
        v-if="btDiag?.ok"
        style="display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 12px"
      >
        <div>
          <div style="font-size: 12px; color: #909399">诊断结论</div>
          <el-tag
            :type="
              btDiag.verdict === 'normal'
                ? 'success'
                : btDiag.verdict === 'backtest_decouple' ||
                    btDiag.verdict === 'backtest_failure'
                  ? 'danger'
                  : 'warning'
            "
          >
            {{ btDiag.verdict }}
          </el-tag>
        </div>
        <div>
          <div style="font-size: 12px; color: #909399">回测 accuracy</div>
          <div style="font-weight: bold">
            {{ ((btDiag.backtest?.accuracy || 0) * 100).toFixed(1) }}%
          </div>
        </div>
        <div>
          <div style="font-size: 12px; color: #909399">回测 T+5 均收益</div>
          <div style="font-weight: bold">
            {{ ((btDiag.backtest?.avg_t5_return || 0) * 100).toFixed(2) }}%
          </div>
        </div>
        <div>
          <div style="font-size: 12px; color: #909399">
            实盘次日 T+1 命中（D+1 买入）
          </div>
          <div style="font-weight: bold">
            {{
              btDiag.live?.t1_hit_rate != null
                ? (btDiag.live.t1_hit_rate * 100).toFixed(1) + "%"
                : "-"
            }}
          </div>
        </div>
        <div>
          <div style="font-size: 12px; color: #909399">实盘 T+5 命中</div>
          <div style="font-weight: bold">
            {{
              btDiag.live?.t5_hit_rate != null
                ? (btDiag.live.t5_hit_rate * 100).toFixed(1) + "%"
                : "-"
            }}
          </div>
        </div>
        <div>
          <div style="font-size: 12px; color: #909399">回测-实盘差值</div>
          <div style="font-weight: bold">{{ btDiag.gap ?? "-" }}</div>
        </div>
      </div>
      <el-alert
        v-if="btDiag?.hypotheses?.length"
        :type="btDiag.verdict === 'normal' ? 'info' : 'warning'"
        :title="
          btDiag.verdict === 'normal'
            ? '系统健康：回测与实盘口径一致（D+1 买入）'
            : '进化大脑据此定位回测侧问题'
        "
        :description="btDiag.hypotheses.join('；')"
        show-icon
        :closable="false"
        style="margin-bottom: 8px"
      />
      <div v-if="btDiag?.suggestions?.length" style="margin-bottom: 8px">
        <span style="font-weight: bold; margin-right: 6px">优化建议：</span>
        <el-tag
          v-for="(sg, i) in btDiag.suggestions"
          :key="i"
          style="margin: 2px 4px 2px 0"
          type="warning"
          effect="plain"
        >
          {{ sg }}
        </el-tag>
      </div>
      <div
        v-if="btDiag?.note"
        style="font-size: 12px; color: #909399; margin-bottom: 8px"
      >
        {{ btDiag.note }}
      </div>
      <!-- 环节归因 TOP 变化观察（建议"持续观察环节归因 TOP 是否变化"的落地展示） -->
      <div
        v-if="btDiag?.stage_trend?.ok"
        style="
          margin-top: 8px;
          font-size: 12px;
          color: #606266;
          line-height: 1.7;
        "
      >
        <div style="font-weight: bold; margin-bottom: 4px">
          👁 环节归因 TOP 变化观察
          <el-tag
            size="small"
            :type="btDiag.stage_trend.changed ? 'warning' : 'success'"
            style="margin-left: 6px"
            >{{ btDiag.stage_trend.changed ? "TOP 漂移" : "TOP 稳定" }}</el-tag
          >
        </div>
        <div>
          最新（{{ btDiag.stage_trend.latest_date }}）：
          <el-tag
            v-for="(s, i) in btDiag.stage_trend.latest_top"
            :key="i"
            size="small"
            style="margin: 0 4px 2px 0"
            :type="i === 0 ? 'danger' : 'info'"
            >{{ s }}</el-tag
          >
          <span
            v-if="btDiag.stage_trend.prev_top?.length"
            style="margin-left: 4px; color: #909399"
          >
            ← 上一期（{{ btDiag.stage_trend.prev_date }}）：
            {{ btDiag.stage_trend.prev_top.join(" / ") }}
          </span>
        </div>
        <div style="margin-top: 4px; color: #909399">
          {{ btDiag.stage_trend.note }}
          <span v-if="btDiag.stage_trend.new_in?.length" style="color: #e6a23c">
            （新进：{{ btDiag.stage_trend.new_in.join("、") }}）
          </span>
          <span v-if="btDiag.stage_trend.gone?.length">
            （退出：{{ btDiag.stage_trend.gone.join("、") }}）
          </span>
        </div>
      </div>
      <el-empty
        v-if="!btDiag?.ok"
        description="暂无回测健康诊断数据"
        :image-size="60"
      />
    </el-card>

    <!-- 系统图谱（plans/18：回测与每日推荐的逻辑+参数，随进化修改动态更新） -->
    <el-card shadow="never" style="margin-top: 16px">
      <template #header
        ><span
          >🗺️ 系统图谱（回测与每日推荐的逻辑 +
          参数，随进化修改动态更新，进化大脑据此优化）</span
        ></template
      >
      <el-collapse v-model="mapOpen">
        <el-collapse-item
          name="daily"
          :title="`📋 每日推荐流水线（${mapDaily.length} 环节）`"
        >
          <el-table :data="mapDaily" size="small" max-height="360">
            <el-table-column prop="name" label="环节" width="110" />
            <el-table-column prop="stage" label="stage" width="130" />
            <el-table-column label="当前逻辑" min-width="220">
              <template #default="{ row }">{{ row.logic }}</template>
            </el-table-column>
            <el-table-column label="关键参数当前值" width="170">
              <template #default="{ row }">
                <template v-if="Object.keys(row.params_now || {}).length">
                  <el-tag
                    v-for="(v, k) in row.params_now"
                    :key="k"
                    size="small"
                    style="margin-right: 4px"
                    >{{ k }}={{ v }}</el-tag
                  >
                </template>
                <span v-else style="color: #909399">-</span>
              </template>
            </el-table-column>
            <el-table-column
              prop="file"
              label="文件"
              min-width="170"
              show-overflow-tooltip
            />
            <el-table-column
              prop="tunable"
              label="可调方向"
              min-width="180"
              show-overflow-tooltip
            />
          </el-table>
        </el-collapse-item>
        <el-collapse-item
          name="bt"
          :title="`🔧 回测流水线（${mapBacktest.length} 步）`"
        >
          <el-table :data="mapBacktest" size="small" max-height="320">
            <el-table-column prop="name" label="步骤" width="130" />
            <el-table-column prop="step" label="step" width="130" />
            <el-table-column label="逻辑" min-width="220">
              <template #default="{ row }">{{ row.logic }}</template>
            </el-table-column>
            <el-table-column label="参数当前值" width="150">
              <template #default="{ row }">
                <template v-if="Object.keys(row.params_now || {}).length">
                  <el-tag
                    v-for="(v, k) in row.params_now"
                    :key="k"
                    size="small"
                    style="margin-right: 4px"
                    >{{ k }}={{ v }}</el-tag
                  >
                </template>
                <span v-else style="color: #909399">-</span>
              </template>
            </el-table-column>
            <el-table-column
              prop="file"
              label="文件"
              min-width="180"
              show-overflow-tooltip
            />
          </el-table>
        </el-collapse-item>
        <el-collapse-item
          name="params"
          :title="`⚙️ 可进化参数（${mapParams.length} 个，进化大脑可改）`"
        >
          <el-table :data="mapParams" size="small" max-height="360">
            <el-table-column prop="name" label="参数" width="190" />
            <el-table-column label="当前值" width="90">
              <template #default="{ row }"
                ><b>{{ row.current }}</b></template
              >
            </el-table-column>
            <el-table-column label="范围" width="120">
              <template #default="{ row }"
                >[{{ row.min }}, {{ row.max }}]</template
              >
            </el-table-column>
            <el-table-column label="热生效" width="80">
              <template #default="{ row }">{{
                row.hot_reload ? "✓" : "需重启"
              }}</template>
            </el-table-column>
            <el-table-column
              prop="desc"
              label="作用"
              min-width="220"
              show-overflow-tooltip
            />
            <el-table-column
              prop="file"
              label="文件"
              min-width="160"
              show-overflow-tooltip
            />
          </el-table>
        </el-collapse-item>
        <el-collapse-item
          name="prod"
          :title="`📦 回测产物（每日推荐依赖的固化资产，${mapProducts.length} 项）`"
        >
          <el-table :data="mapProducts" size="small" max-height="260">
            <el-table-column prop="file" label="文件" width="280" />
            <el-table-column
              prop="desc"
              label="说明"
              min-width="240"
              show-overflow-tooltip
            />
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag
                  size="small"
                  :type="row.exists ? 'success' : 'danger'"
                  >{{ row.exists ? "存在" : "缺失" }}</el-tag
                >
              </template>
            </el-table-column>
          </el-table>
        </el-collapse-item>
        <el-collapse-item
          name="form"
          :title="`🧬 形态定义（${mapForms.length} 种）+ 特征体系（59+ 维）`"
        >
          <el-table :data="mapForms" size="small" max-height="260">
            <el-table-column prop="type" label="形态" width="60" />
            <el-table-column prop="name" label="名称" width="130" />
            <el-table-column
              prop="desc"
              label="说明"
              min-width="220"
              show-overflow-tooltip
            />
            <el-table-column
              prop="thresholds"
              label="判定阈值"
              min-width="260"
              show-overflow-tooltip
            />
          </el-table>
          <div style="margin-top: 8px; font-size: 12px; line-height: 1.8">
            <b
              >形态向量（VECTOR_FEATURES，{{
                mapFeatures.vector?.n
              }}
              维，归一化+相似度）</b
            >：{{ mapFeatures.vector?.groups }} <br /><b
              >条件参数（CONDITION_FEATURES，{{
                mapFeatures.condition?.n
              }}
              维，原始值分位统计）</b
            >：{{ mapFeatures.condition?.groups }}
          </div>
        </el-collapse-item>
        <el-collapse-item
          name="const"
          :title="`⚡ 关键常量（${mapConstants.length}，含可进化映射）+ 知识库（${mapKbs.length}）`"
        >
          <el-table :data="mapConstants" size="small" max-height="220">
            <el-table-column prop="name" label="常量" width="180" />
            <el-table-column prop="value" label="值" width="180" />
            <el-table-column prop="file" label="文件" width="180" />
            <el-table-column
              prop="desc"
              label="说明"
              min-width="200"
              show-overflow-tooltip
            />
          </el-table>
          <el-table
            :data="mapKbs"
            size="small"
            max-height="220"
            style="margin-top: 8px"
          >
            <el-table-column prop="file" label="知识库文件" width="260" />
            <el-table-column
              prop="desc"
              label="说明（Agent 精筛/进化依据）"
              min-width="240"
              show-overflow-tooltip
            />
            <el-table-column label="状态" width="80">
              <template #default="{ row }">
                <el-tag
                  size="small"
                  :type="row.exists ? 'success' : 'danger'"
                  >{{ row.exists ? "有" : "缺" }}</el-tag
                >
              </template>
            </el-table-column>
          </el-table>
        </el-collapse-item>
        <el-collapse-item
          name="anchor"
          :title="`✏️ 可改锚点（${mapAnchors.length} 处，code_writer 直接改代码的精确定位）`"
        >
          <el-table :data="mapAnchors" size="small" max-height="360">
            <el-table-column prop="desc" label="可改点" width="200" />
            <el-table-column prop="file" label="文件" width="200" />
            <el-table-column label="当前代码锚点（anchor_old）" min-width="260">
              <template #default="{ row }"
                ><code
                  style="font-size: 12px; color: #409eff; white-space: pre-wrap"
                  >{{ row.anchor_old }}</code
                ></template
              >
            </el-table-column>
            <el-table-column
              prop="how"
              label="改法建议"
              min-width="200"
              show-overflow-tooltip
            />
          </el-table>
        </el-collapse-item>
        <el-collapse-item
          name="seq"
          :title="`🕒 运行时序（${mapSequence.length} 个自动任务）`"
        >
          <el-table :data="mapSequence" size="small">
            <el-table-column prop="time" label="时间" width="90" />
            <el-table-column prop="task" label="任务" width="240" />
            <el-table-column
              prop="note"
              label="说明"
              min-width="260"
              show-overflow-tooltip
            />
          </el-table>
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <!-- 候选池（待确认提案） -->
    <el-card shadow="never" style="margin-top: 16px">
      <template #header
        ><span
          >📋
          {{
            autoMode
              ? "进化提案（自动确认 + 版本备份）"
              : "候选池（待人工确认）"
          }}</span
        ></template
      >
      <el-table
        :data="autoMode ? recentProposals : pending"
        size="small"
        v-loading="loading"
        :empty-text="autoMode ? '暂无进化提案' : '暂无待确认提案'"
      >
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="ts" label="生成时间" width="150" />
        <el-table-column prop="param" label="参数" width="180" />
        <el-table-column label="调整" width="140">
          <template #default="{ row }"
            >{{ row.old }} → <b>{{ row.new }}</b></template
          >
        </el-table-column>
        <el-table-column
          prop="reason"
          label="原因"
          min-width="180"
          show-overflow-tooltip
        />
        <el-table-column
          prop="evidence"
          label="证据"
          min-width="160"
          show-overflow-tooltip
        />
        <el-table-column label="提案卡片（E8）" min-width="200">
          <template #default="{ row }">
            <div v-if="row.card" style="font-size: 12px; line-height: 1.6">
              <el-tag
                size="small"
                :type="row.card.direction === '上调' ? 'danger' : 'success'"
                style="margin-right: 4px"
              >
                {{ row.card.direction }}
              </el-tag>
              <span style="color: #909399"
                >基线命中 {{ fmtPct(row.card.baseline_hit_rate) }}</span
              >
              <div style="color: #606266; margin-top: 2px">
                {{ row.card.minimal_change }}
              </div>
            </div>
            <span v-else style="color: #909399">-</span>
          </template>
        </el-table-column>
        <el-table-column label="回测模拟" width="120">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="(row.backtest_sim?.delta || 0) < 0 ? 'danger' : 'success'"
            >
              {{ row.backtest_sim?.direction || "-" }}
              {{ row.backtest_sim?.delta ?? "" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          :label="autoMode ? '自动结果' : '操作'"
          :width="autoMode ? 120 : 110"
          align="center"
        >
          <template #default="{ row }">
            <el-tag
              v-if="autoMode"
              size="small"
              :type="statusTag(row.status)"
              >{{ row.status }}</el-tag
            >
            <el-button
              v-else
              size="small"
              type="primary"
              @click="confirm(row.id)"
              >确认</el-button
            >
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 影子实验看板 -->
    <el-card shadow="never" style="margin-top: 16px">
      <template #header><span>🧪 影子实验（先验证后生效 F1）</span></template>
      <el-table :data="shadowEntries" size="small" empty-text="暂无影子实验">
        <el-table-column prop="param" label="参数" width="200" />
        <el-table-column label="old → new" width="150">
          <template #default="{ row }"
            >{{ row.old }} → <b>{{ row.new }}</b></template
          >
        </el-table-column>
        <el-table-column prop="start_date" label="开始" width="110" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTag(row.status)">{{
              row.status
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="旁路判定样本" width="130">
          <template #default="{ row }">{{
            row.verdicts?.length || 0
          }}</template>
        </el-table-column>
        <el-table-column label="操作" width="110" align="center">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'running'"
              size="small"
              @click="settle(row.param)"
              >结算</el-button
            >
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 守护报告 -->
    <el-card shadow="never" style="margin-top: 16px">
      <template #header
        ><span
          >🛡️ 独立守护（Constitution / 越权 / 频率 / 主指标 / 自身健康）</span
        ></template
      >
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="宪法哈希">
          <el-tag
            size="small"
            :type="guard.constitution?.ok ? 'success' : 'danger'"
          >
            {{ guard.constitution?.ok ? "完整" : "⚠️ 被篡改" }}
          </el-tag>
          <span
            v-if="!guard.constitution?.ok"
            style="margin-left: 6px; color: #f56c6c"
          >
            {{ guard.constitution?.reason }}
          </span>
        </el-descriptions-item>
        <el-descriptions-item label="频率监控">
          <el-tag
            size="small"
            :type="guard.frequency?.cooldown_suggested ? 'warning' : 'success'"
          >
            {{ guard.frequency?.cooldown_suggested ? "建议冷却" : "正常" }}
          </el-tag>
          <span style="margin-left: 6px; color: #909399">
            本周应用 {{ guard.frequency?.week_applies || 0 }} / 近期失败
            {{ guard.frequency?.recent_fails || 0 }}
          </span>
        </el-descriptions-item>
        <el-descriptions-item label="主指标">
          <el-tag
            size="small"
            :type="guard.main_metric?.pause_suggested ? 'warning' : 'success'"
          >
            {{ guard.main_metric?.pause_suggested ? "建议暂停" : "正常" }}
          </el-tag>
          <span
            v-if="guard.main_metric?.drop_pp !== undefined"
            style="margin-left: 6px; color: #909399"
          >
            命中率下降 {{ guard.main_metric.drop_pp }}pp
          </span>
        </el-descriptions-item>
        <el-descriptions-item label="自身健康（I2）">
          <el-tag
            size="small"
            :type="guard.health?.events_over_cap ? 'warning' : 'success'"
          >
            {{ guard.health?.events_over_cap ? "事件库超容" : "正常" }}
          </el-tag>
          <span style="margin-left: 6px; color: #909399">
            事件 {{ guard.health?.events_rows || 0 }} / 影子
            {{ guard.health?.shadow_running || 0 }}
          </span>
        </el-descriptions-item>
        <el-descriptions-item label="越权拦截"
          ><b>{{ guard.health?.intercepts || 0 }}</b> 次</el-descriptions-item
        >
        <el-descriptions-item label="巡检时间">{{
          guard.ts || "-"
        }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 终身记账 -->
    <el-card shadow="never" style="margin-top: 16px">
      <template #header
        ><span
          >📒 终身记账（G4：改进生命周期效果）—
          {{ ledger.summary || "暂无" }}</span
        ></template
      >
      <el-table
        :data="[...(ledger.closed || []), ...(ledger.active || [])]"
        size="small"
        empty-text="暂无记账记录"
      >
        <el-table-column prop="param" label="参数" width="200" />
        <el-table-column label="old → new" width="150">
          <template #default="{ row }"
            >{{ row.old }} → <b>{{ row.new }}</b></template
          >
        </el-table-column>
        <el-table-column label="基线命中率" width="110">
          <template #default="{ row }">{{
            fmtPct(row.baseline_hit_rate)
          }}</template>
        </el-table-column>
        <el-table-column label="当前/最终命中率" width="120">
          <template #default="{ row }">{{
            fmtPct(row.final_hit_rate ?? row.current_hit_rate)
          }}</template>
        </el-table-column>
        <el-table-column label="变化" width="100">
          <template #default="{ row }">
            <span
              :style="{
                color:
                  (row.delta_hit_rate ?? row.final_delta ?? 0) >= 0
                    ? '#67c23a'
                    : '#f56c6c',
              }"
            >
              {{ fmtPct(row.delta_hit_rate ?? row.final_delta) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="opened_at" label="开始" width="150" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.status === 'active' ? 'primary' : 'info'"
            >
              {{ row.status === "active" ? "生效中" : "已结账" }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 交易纪律（plans/22：纪律进化——操作计划是否可执行、按计划执行到底赚不赚） -->
    <el-card shadow="never" style="margin-top: 16px" v-loading="planLoading">
      <template #header>
        <span
          >🎯 交易纪律（买卖点位可执行性 + 纪律 alpha + 反事实最优纪律）</span
        >
        <div style="float: right">
          <el-button size="small" @click="runPlanReplay(false)"
            >回放未结算</el-button
          >
          <el-button size="small" @click="runPlanReplay(true)"
            >全部重放</el-button
          >
          <el-button size="small" @click="runPlanBackfill"
            >回填历史样本</el-button
          >
          <el-button size="small" @click="runPlanGrid">重建纪律网格</el-button>
          <el-button size="small" @click="runPlanLessons"
            >沉淀纪律教训</el-button
          >
        </div>
      </template>

      <el-row :gutter="12" style="margin-bottom: 12px">
        <el-col :span="3">
          <div class="plan-stat">
            <div class="plan-stat-v">{{ planStats?.plans ?? 0 }}</div>
            <div class="plan-stat-l">计划台账</div>
          </div>
        </el-col>
        <el-col :span="3">
          <div class="plan-stat">
            <div class="plan-stat-v">{{ planStats?.replayed ?? 0 }}</div>
            <div class="plan-stat-l">已回放</div>
          </div>
        </el-col>
        <el-col :span="3">
          <div class="plan-stat">
            <div class="plan-stat-v">
              {{ fmtPctN(planStats?.filled_rate, 1) }}
            </div>
            <div class="plan-stat-l">
              成交率（未成交 {{ fmtPctN(planStats?.missed_rate, 1) }}）
            </div>
          </div>
        </el-col>
        <el-col :span="3">
          <div class="plan-stat">
            <div
              class="plan-stat-v"
              :style="{
                color:
                  (planStats?.avg_plan_ret_net ?? 0) >= 0
                    ? '#67c23a'
                    : '#f56c6c',
              }"
            >
              {{ fmtSigned(planStats?.avg_plan_ret_net) }}
            </div>
            <div class="plan-stat-l">
              计划收益(含成本，胜率
              {{ fmtPctN(planStats?.plan_win_rate, 0) }})
            </div>
          </div>
        </el-col>
        <el-col :span="3">
          <div class="plan-stat">
            <div
              class="plan-stat-v"
              :style="{
                color:
                  (planStats?.avg_alpha_vs_hold_t5 ?? 0) >= 0
                    ? '#67c23a'
                    : '#f56c6c',
              }"
            >
              {{ fmtSigned(planStats?.avg_alpha_vs_hold_t5) }}
            </div>
            <div class="plan-stat-l">纪律alpha(vs无脑持有T+5)</div>
          </div>
        </el-col>
        <el-col :span="3">
          <div class="plan-stat">
            <div class="plan-stat-v">
              {{ fmtPctN(planStats?.alpha_positive_rate, 0) }}
            </div>
            <div class="plan-stat-l">跑赢基准的单子占比</div>
          </div>
        </el-col>
        <el-col :span="3">
          <div class="plan-stat">
            <div class="plan-stat-v">
              {{ fmtPctN(planStats?.failure_type_pct?.missed_fill, 1) }}
            </div>
            <div class="plan-stat-l">高开未成交（追高上限太紧?）</div>
          </div>
        </el-col>
        <el-col :span="3">
          <div class="plan-stat">
            <div class="plan-stat-v">
              {{ fmtPctN(planStats?.failure_type_pct?.stop_whipsaw, 1) }}
            </div>
            <div class="plan-stat-l">止损后反弹（止损太紧?）</div>
          </div>
        </el-col>
      </el-row>

      <div style="margin-bottom: 10px">
        <span style="font-size: 13px; color: #606266">纪律失效分布：</span>
        <el-tag
          v-for="f in planFailureOptions"
          :key="f.key"
          size="small"
          style="margin-right: 6px"
          type="warning"
          >{{ f.label }}</el-tag
        >
        <span v-if="!planFailureOptions.length" style="color: #909399"
          >暂无纪律样本（先"回填历史样本"或等每日榜单落台账后回放）</span
        >
      </div>

      <el-divider content-position="left">反事实最优纪律</el-divider>
      <div style="font-size: 13px; line-height: 1.9; color: #606266">
        <div>
          样本 {{ planGrid?.grid?.samples ?? 0 }}（样本内
          {{ planGrid?.grid?.is_samples ?? 0 }} / 样本外
          {{ planGrid?.grid?.oos_samples ?? 0 }}，分界
          {{ planGrid?.grid?.is_cut || "-" }}）；网格 止损
          {{ planGrid?.grid?.grid?.stop?.join("/") }} × 止盈折扣
          {{ planGrid?.grid?.grid?.tp1_ratio?.join("/") }} × 持有
          {{ planGrid?.grid?.grid?.hold_days?.join("/") }} 日 × 追高上限
          {{ planGrid?.grid?.grid?.chase_pct?.join("/") }}
        </div>
        <div v-if="planGrid?.grid?.global">
          <b>全局最优</b>：止损
          {{ fmtPctN(planGrid.grid.global.stop_loss_pct, 0) }} / 止盈折扣
          {{ fmtPctN(planGrid.grid.global.tp1_ratio, 0) }} / 持有
          {{ planGrid.grid.global.hold_days }} 日 / 追高上限
          {{ fmtPctN(planGrid.grid.global.chase_pct, 0) }} → 可部署期望
          <b>{{ fmtSigned(planGrid.grid.global.utility) }}</b>
          （成交率
          {{ fmtPctN(planGrid.grid.global.filled_rate, 0) }}、被止损洗出
          {{ fmtPctN(planGrid.grid.global.washout_rate, 0) }}、纪律alpha
          {{ fmtSigned(planGrid.grid.global.alpha_utility) }}、样本外
          {{ fmtSigned(planGrid.grid.global.oos_utility) }}
          {{ planGrid.grid.global.oos_pass ? "稳健" : "未通过" }}）
        </div>
        <div v-if="planGrid?.grid?.baseline">
          <b>当前参数</b>：止损
          {{ fmtPctN(planGrid.grid.baseline.stop_loss_pct, 0) }} / 折扣
          {{ fmtPctN(planGrid.grid.baseline.tp1_ratio, 0) }} / 持有
          {{ planGrid.grid.baseline.hold_days }} 日 / 追高
          {{ fmtPctN(planGrid.grid.baseline.chase_pct, 0) }} → 期望
          {{ fmtSigned(planGrid.grid.baseline.utility) }}
          （纪律alpha
          {{ fmtSigned(planGrid.grid.baseline.alpha_utility) }}、样本外
          {{ fmtSigned(planGrid.grid.baseline.oos_utility) }}）→ 改进空间
          {{
            fmtSigned(
              (planGrid.grid.global?.utility ?? 0) -
                (planGrid.grid.baseline?.utility ?? 0),
            )
          }}
        </div>
        <div>
          是否采纳网格（相对当前参数有正改进且样本外稳健才采纳）：
          <el-tag
            size="small"
            :type="planAdoptedForms.length ? 'success' : 'info'"
          >
            {{
              planAdoptedForms.length
                ? planAdoptedForms.join("、")
                : "未采纳（保持当前参数）"
            }}
          </el-tag>
        </div>
        <div
          v-if="(planGrid?.grid?.global?.utility ?? 0) < 0"
          style="color: #e6a23c"
        >
          ⚠ 全部纪律组合期望为负 →
          问题在<b>选股端</b>（纪律的价值是止损减亏）；应优先提高入选质量/收紧信号
        </div>
      </div>

      <el-table
        :data="planGridRows"
        size="small"
        style="margin-top: 10px"
        empty-text="暂无纪律网格（点上方“重建纪律网格”，约 10~30 秒）"
      >
        <el-table-column prop="form" label="形态" width="80" />
        <el-table-column label="最优止损" width="110">
          <template #default="{ row }">{{
            fmtPctN(row.stop_loss_pct, 0)
          }}</template>
        </el-table-column>
        <el-table-column label="止盈折扣" width="100">
          <template #default="{ row }">{{
            fmtPctN(row.tp1_ratio, 0)
          }}</template>
        </el-table-column>
        <el-table-column prop="hold_days" label="持有(日)" width="90" />
        <el-table-column label="追高上限" width="100">
          <template #default="{ row }">{{
            fmtPctN(row.chase_pct, 0)
          }}</template>
        </el-table-column>
        <el-table-column label="可部署期望" width="110">
          <template #default="{ row }">
            <span
              :style="{
                color: (row.utility ?? 0) >= 0 ? '#67c23a' : '#f56c6c',
              }"
              >{{ fmtSigned(row.utility) }}</span
            >
          </template>
        </el-table-column>
        <el-table-column label="纪律alpha" width="110">
          <template #default="{ row }">{{
            fmtSigned(row.alpha_utility)
          }}</template>
        </el-table-column>
        <el-table-column label="样本外" width="110">
          <template #default="{ row }">
            {{ fmtSigned(row.oos_utility) }}
            <el-tag size="small" :type="row.oos_pass ? 'success' : 'warning'">{{
              row.oos_pass ? "过" : "未过"
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="成交率" width="90">
          <template #default="{ row }">{{
            fmtPctN(row.filled_rate, 0)
          }}</template>
        </el-table-column>
        <el-table-column label="洗出率" width="90">
          <template #default="{ row }">{{
            fmtPctN(row.washout_rate, 0)
          }}</template>
        </el-table-column>
        <el-table-column label="样本" width="90">
          <template #default="{ row }">
            {{ row.samples }}
            <el-tag v-if="row.fallback_global" size="small" type="info"
              >回退全局</el-tag
            >
          </template>
        </el-table-column>
        <el-table-column label="相对当前参数" min-width="120">
          <template #default="{ row }">{{
            fmtSigned(row.improvement)
          }}</template>
        </el-table-column>
      </el-table>

      <el-divider content-position="left"
        >计划台账明细（最近 60 条）</el-divider
      >
      <div style="margin-bottom: 8px">
        <el-select
          v-model="planFormFilter"
          placeholder="形态"
          clearable
          size="small"
          style="width: 110px"
          @change="loadPlan"
        >
          <el-option label="A" value="A" />
          <el-option label="B" value="B" />
          <el-option label="C" value="C" />
          <el-option label="D" value="D" />
          <el-option label="E" value="E" />
          <el-option label="U" value="U" />
        </el-select>
        <el-select
          v-model="planFailureFilter"
          placeholder="失效类型"
          clearable
          size="small"
          style="width: 190px; margin-left: 8px"
          @change="loadPlan"
        >
          <el-option
            v-for="f in planFailureOptions"
            :key="f.key"
            :label="f.label"
            :value="f.key"
          />
        </el-select>
      </div>
      <el-table
        :data="planRows"
        size="small"
        empty-text="暂无计划台账（每日精筛榜单落盘后自动写入）"
      >
        <el-table-column prop="date" label="推荐日" width="100" />
        <el-table-column label="标的" width="150">
          <template #default="{ row }"
            >{{ row.ts_code }} {{ row.name }}</template
          >
        </el-table-column>
        <el-table-column prop="form_type" label="形态" width="70" />
        <el-table-column label="买入区间" width="130">
          <template #default="{ row }">
            {{ row.plan?.buy_low ?? "-" }} ~ {{ row.plan?.buy_high ?? "-" }}
          </template>
        </el-table-column>
        <el-table-column label="止损/止盈" width="150">
          <template #default="{ row }">
            {{ row.plan?.stop_loss ?? "-" }} /
            {{ row.plan?.take_profit?.[0]?.price ?? "-" }}
          </template>
        </el-table-column>
        <el-table-column label="持有" width="70">
          <template #default="{ row }">{{
            row.plan?.hold_days ?? "-"
          }}</template>
        </el-table-column>
        <el-table-column label="回放结果" width="150">
          <template #default="{ row }">
            <template v-if="row.execution?.exit_reason">
              {{ row.execution.fill_price ?? "-" }} →
              {{ row.execution.exit_price ?? "-" }}
              <el-tag size="small" type="info">{{
                row.execution.exit_reason
              }}</el-tag>
            </template>
            <span v-else style="color: #909399">待回放</span>
          </template>
        </el-table-column>
        <el-table-column label="净收益" width="100">
          <template #default="{ row }">
            <span
              v-if="
                row.execution?.plan_ret_net !== null &&
                row.execution?.plan_ret_net !== undefined
              "
              :style="{
                color: row.execution.plan_ret_net >= 0 ? '#67c23a' : '#f56c6c',
              }"
              >{{ fmtSigned(row.execution.plan_ret_net) }}</span
            >
            <span v-else style="color: #909399">-</span>
          </template>
        </el-table-column>
        <el-table-column label="纪律alpha" width="100">
          <template #default="{ row }">{{
            fmtSigned(row.alpha?.vs_hold_t5)
          }}</template>
        </el-table-column>
        <el-table-column label="失效类型" width="150">
          <template #default="{ row }">{{
            row.failure_type ? failureCn(row.failure_type) : "-"
          }}</template>
        </el-table-column>
        <el-table-column
          prop="params_version"
          label="参数版本"
          min-width="130"
        />
      </el-table>
    </el-card>

    <!-- 事件流（全项目错误/告警/结果汇聚，§15.2） -->
    <el-card shadow="never" style="margin-top: 16px">
      <template #header
        ><span>📡 事件流（全项目错误/告警/结果汇聚）</span></template
      >
      <el-table :data="events" size="small" empty-text="暂无事件">
        <el-table-column prop="ts" label="时间" width="150" />
        <el-table-column label="级别" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="eventTag(row.level)">{{
              row.level
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="source" label="来源" width="140" />
        <el-table-column
          prop="message"
          label="内容"
          min-width="280"
          show-overflow-tooltip
        />
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.plan-stat {
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 6px 8px;
  text-align: center;
  background: #fafafa;
}
.plan-stat-v {
  font-size: 16px;
  font-weight: bold;
  color: #303133;
}
.plan-stat-l {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}
</style>
