<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import {
  getTodayPicks,
  getDatePicks,
  triggerRecommendScan,
  stopRecommendScan,
  confirmRecommendScan,
  dismissRecommendScan,
  getRecommendScanStatus,
  getAgentRefineStatus,
} from "../api";

const router = useRouter();
const loading = ref(false);
const scanning = ref(false);
const report = ref<any>(null);
const picks = ref<any[]>([]);
const queryDate = ref<string>("");
const scanStatus = ref<any>({});
const scanLogs = ref<string[]>([]);
// 停止扫描（协作式取消）：点过后按钮进入 loading，等后端把状态切到 cancelled 才复位
const stopping = ref(false);
// 多 Agent 精筛
const agentStatus = ref<any>({});
const agentLogs = ref<string[]>([]);
const agentPicks = ref<any[]>([]);
// 展示：Agent 精筛完成后优先显示 Agent 最终榜单（含理由），否则显示规则榜单
const displayPicks = computed(() =>
  agentPicks.value.length ? agentPicks.value : picks.value,
);
// 规则形态信号降级检测：榜单所有股票均未命中任何已验证规则形态时提示
// （规则库看涨规则较少时，规则信号主要起看跌抑制作用，排序依赖 20 天向量信号）
const ruleSignalInactive = computed(() => {
  const rows = displayPicks.value;
  if (!rows || !rows.length) return false;
  return rows.every((r: any) => !r.hit_rules || !r.hit_rules.length);
});

const fmtProb = (v: any) =>
  v === null || v === undefined || v === ""
    ? "—"
    : `${(Number(v) * 100).toFixed(0)}%`;
const fmtNum = (v: any, digits = 3) =>
  v === null || v === undefined || v === "" ? "—" : Number(v).toFixed(digits);
// 价格展示（A 股最小变动 0.01 元）
const fmtPrice = (v: any) =>
  v === null || v === undefined || v === "" ? "—" : Number(v).toFixed(2);
// 现价涨跌幅（A 股习惯：红涨绿跌）
const fmtSignedPct = (v: any) =>
  v === null || v === undefined || v === ""
    ? "—"
    : `${Number(v) >= 0 ? "+" : ""}${Number(v).toFixed(2)}%`;
// 交易日 YYYYMMDD → MM-DD（现价日期标注用）
const fmtDateShort = (v: any) => {
  const s = String(v || "");
  return s.length === 8 ? `${s.slice(4, 6)}-${s.slice(6, 8)}` : s || "—";
};

// ── 基准对照（影子）────────────────────────────────────────────────
// 来源：后端 `_with_baseline_shadow()`（plans/24 §11.19~§11.22）。
// **仅当可进化参数 `daily_baseline_mode = shadow` 时**后端才会附加该字段；
// 默认 `off` ⇒ `shadow` 为 null ⇒ 本卡片**不渲染** ⇒ 界面上与从前一字不差。
const shadow = computed(() => report.value?.baseline_shadow || null);
const fmtPct = (v: any, digits = 1) => {
  if (v === null || v === undefined || v === "" || Number.isNaN(Number(v)))
    return "—";
  return `${(Number(v) * 100).toFixed(digits)}%`;
};
// 基准两行（B1/B2）：把 `gate` 与 `portfolios` 拼成表格所需形状
const shadowRows = computed(() => {
  const sh = shadow.value;
  if (!sh) return [];
  const pair: Array<[string, string]> = [
    ["B1", "market_ew"],
    ["B2", "size_q1"],
  ];
  return pair
    .filter(([k]) => sh.gate?.[k])
    .map(([k, mode]) => {
      const g = sh.gate[k] || {};
      const pf = sh.portfolios?.[mode] || {};
      return {
        key: k,
        label: g.label || k,
        codes: `${pf.n_picked ?? "—"} / 池 ${pf.pool_size ?? "—"}`,
        net: fmtPct(g.net_per_year),
        t: g.t === null || g.t === undefined ? "—" : Number(g.t).toFixed(2),
        // t_hac = Newey-West（修正重叠样本，plans/24 §11.28）。**仅披露**：判定仍用上面的 t。
        t_hac:
          g.t_hac === null || g.t_hac === undefined
            ? "—"
            : Number(g.t_hac).toFixed(2),
        verdict: g.verdict || "—",
        reason: (g.reasons || []).join("；") || "—",
      };
    });
});
// 「我们自己」四行：重点是把 **not_decidable** 与 reject 区分显示
const oursRows = computed(() => {
  const sh = shadow.value;
  if (!sh) return [];
  const kinds = ["ours_top30", "ours_all", "ours_rules_top30", "ours_agent"];
  return kinds
    .filter((k) => sh.gate_ours?.[k])
    .map((k) => {
      const g = sh.gate_ours[k] || {};
      return {
        key: k,
        label: g.label || k,
        gross: fmtPct(g.gross_per_period, 2),
        verdict: g.verdict || "—",
        seg: `${g.segments_have ?? "?"} / ${g.segments_total ?? "?"}`,
        missing: (g.segments_missing || []).join("、") || "无",
        reason: (g.reasons || []).join("；") || "—",
      };
    });
});

const riskType = (level: string) =>
  level === "高" ? "danger" : level === "中" ? "warning" : "success";
const formLabel: Record<string, string> = {
  A: "A平台突破",
  B: "B V型反转",
  C: "C中继加速",
  D: "D直接拉升",
  E: "E连板",
};

const load = async () => {
  loading.value = true;
  // 切换日期时清空 Agent 精筛榜单：Agent 精筛只针对"今日/最新扫描"，
  // 历史榜单由后端 /date 接口直接返回（含该日期的历史 Agent 榜单）。
  // 否则旧 Agent 榜单会通过 displayPicks 遮蔽所选日期的榜单。
  if (queryDate.value) agentPicks.value = [];
  try {
    const { data } = queryDate.value
      ? await getDatePicks(queryDate.value.replace(/-/g, ""))
      : await getTodayPicks();
    report.value = data;
    picks.value = data?.top_picks || [];
  } catch (e: any) {
    report.value = null;
    picks.value = [];
  } finally {
    loading.value = false;
  }
};

const startScanPolling = () => {
  // 轮询扫描状态 + Agent 精筛（扫描最后一环，自动触发，无需单独按钮）。
  // 用户要求：定时任务/采集完成自动扫描在前端也要有反应（按钮/日志/"执行中"状态），
  // 不能静默执行——统一轮询 scan/status 与 agent/status，running 即展示进度与日志。
  scanning.value = true;
  const timer = setInterval(async () => {
    try {
      const scan = (await getRecommendScanStatus()).data;
      scanStatus.value = scan;
      scanLogs.value = scan?.logs || [];
      // 轮询顺带同步"待确认"标记（例如定时采集刚跑完、或另一页面点了稍后再说）
      confirmPending.value = scan?.confirm_pending || null;
      const ag = (await getAgentRefineStatus()).data;
      agentStatus.value = ag;
      agentLogs.value = ag?.logs || [];
      if (ag?.status === "success") {
        agentPicks.value = ag?.report?.top_picks || [];
      }
      const done =
        ag?.status === "success" ||
        ag?.status === "failed" ||
        ag?.status === "cancelled" ||
        scan?.status === "cancelled" ||
        (scan?.status === "success" && (!ag?.status || ag?.status === "idle"));
      if (done) {
        clearInterval(timer);
        scanning.value = false;
        stopping.value = false;
        load();
      }
    } catch (e: any) {
      clearInterval(timer);
      scanning.value = false;
      stopping.value = false;
    }
  }, 5000);
};

const triggerScan = async () => {
  scanLogs.value = [];
  scanStatus.value = {};
  agentLogs.value = [];
  agentStatus.value = {};
  agentPicks.value = [];
  stopping.value = false;
  try {
    await triggerRecommendScan();
    startScanPolling();
  } catch (e: any) {
    scanning.value = false;
  }
};

// 停止扫描：请求后端协作式取消（后端在下一只股票/下一批精筛前退出，不落盘半成品榜单）。
// 不清空 scanning/stopping——继续轮询，等后端把状态切成 cancelled 再复位，避免"看似停了实际还在跑"。
const stopScan = async () => {
  if (stopping.value) return;
  stopping.value = true;
  scanLogs.value.push(
    `${new Date().toLocaleTimeString()} [INFO] 已发送停止请求，等待后端在当前股票处理完后中断…`,
  );
  try {
    const { data } = await stopRecommendScan();
    if (data?.message) {
      scanLogs.value.push(
        `${new Date().toLocaleTimeString()} [INFO] ${data.message}`,
      );
    }
    if (data?.cancelled === false && data?.status !== "stopping") {
      // 后端已没有在跑的任务（例如刚好跑完）：直接复位
      scanning.value = false;
      stopping.value = false;
    }
  } catch (e: any) {
    stopping.value = false;
    scanLogs.value.push(
      `${new Date().toLocaleTimeString()} [ERROR] 停止请求失败：${e?.message || e}`,
    );
  }
};

// 采集完成后的"是否运行每日推荐扫描"确认（对齐 plans/09 八 / 后端 confirm 模式）。
// 后端默认只标记 confirm_pending，不擅自扫描；由用户在气泡条里点「立即运行」才真正触发。
const confirmPending = ref<any>(null);
const confirmRunning = ref(false);

const runConfirmedScan = async () => {
  if (confirmRunning.value) return;
  confirmRunning.value = true;
  scanLogs.value = [];
  scanStatus.value = {};
  agentLogs.value = [];
  agentStatus.value = {};
  agentPicks.value = [];
  stopping.value = false;
  try {
    await confirmRecommendScan();
    confirmPending.value = null;
    startScanPolling();
  } catch (e: any) {
    scanLogs.value.push(
      `${new Date().toLocaleTimeString()} [ERROR] 启动每日推荐扫描失败：${e?.message || e}`,
    );
  } finally {
    confirmRunning.value = false;
  }
};

const dismissConfirmedScan = async () => {
  try {
    await dismissRecommendScan("user_skip");
  } catch (e: any) {
    // 后端已清空或网络异常都无所谓：前端先收起提示，用户可以随时手动触发
  }
  confirmPending.value = null;
};

// 页面加载即检查：若有定时/采集自动/手动扫描正在跑，立即进入"执行中"轮询（不静默）
onMounted(async () => {
  load();
  try {
    const scan = (await getRecommendScanStatus()).data;
    const ag = (await getAgentRefineStatus()).data;
    // 采集刚结束但还没确认/跳过 → 展示提示条（跨页面/刷新后仍可见）
    confirmPending.value = scan?.confirm_pending || null;
    if (scan?.status === "running" || ag?.status === "running") {
      startScanPolling();
    } else if (ag?.status === "success" && ag?.report?.top_picks?.length) {
      agentPicks.value = ag?.report?.top_picks || [];
    }
  } catch (e: any) {
    // 状态查询失败不影响榜单加载
  }
});

const goDetail = (row: any) => {
  router.push(`/market/stock/${row.ts_code}`);
};
</script>

<template>
  <div style="padding: 24px">
    <h2>🏆 每日推荐榜单</h2>
    <p style="color: #909399; margin-bottom: 16px">
      按"综合上涨概率"排序（形态粗筛 + 条件概率打分 + 反向排除 +
      风险过滤），命中条件可解释
    </p>

    <el-card>
      <!-- 采集完成后的待确认提示：默认 confirm 模式下后端只标记，用户点「立即运行」才真正扫描 -->
      <el-alert
        v-if="confirmPending?.pending"
        type="success"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
      >
        <template #title>
          数据采集已完成{{
            confirmPending?.data_date
              ? `（数据日期 ${confirmPending.data_date}）`
              : ""
          }}，是否立即运行每日推荐扫描？
        </template>
        <div style="display: flex; gap: 8px; margin-top: 6px">
          <el-button
            type="primary"
            size="small"
            :loading="confirmRunning"
            @click="runConfirmedScan"
          >
            立即运行
          </el-button>
          <el-button size="small" @click="dismissConfirmedScan">
            稍后再说
          </el-button>
        </div>
      </el-alert>

      <div
        style="
          display: flex;
          gap: 12px;
          align-items: center;
          margin-bottom: 16px;
          flex-wrap: wrap;
        "
      >
        <el-date-picker
          v-model="queryDate"
          type="date"
          placeholder="查看历史榜单（留空=今日）"
          value-format="YYYY-MM-DD"
          clearable
          style="width: 220px"
          @change="load"
        />
        <el-button
          type="primary"
          :loading="scanning"
          :disabled="stopping"
          @click="triggerScan"
        >
          {{ scanning ? "扫描+精筛中..." : "触发全市场扫描" }}
        </el-button>
        <!-- 停止扫描：仅在运行中显示；协作式取消（后端在下一只股票/下一批精筛前退出，不落盘半成品榜单） -->
        <el-button
          v-if="scanning"
          type="danger"
          plain
          :loading="stopping"
          @click="stopScan"
        >
          {{ stopping ? "正在停止..." : "停止扫描" }}
        </el-button>
        <el-button @click="load">刷新</el-button>
        <span v-if="report" style="color: #909399; font-size: 13px">
          扫描 {{ report.total_scanned }} 只 | 确认启动
          {{ report.confirmed_count }} | 候选池 {{ report.candidate_pool }} |
          推荐 {{ displayPicks.length }} 条
          <el-tag
            v-if="report.message"
            size="small"
            type="warning"
            style="margin-left: 8px"
          >
            {{ report.message }}
          </el-tag>
        </span>
      </div>

      <el-alert
        v-if="ruleSignalInactive && displayPicks.length"
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
        title="规则形态信号当前未命中"
        description="本榜单未命中任何已验证规则形态（规则库看涨规则较少，排序主要依赖 20 天向量信号 + 看跌抑制；可运行'规则形态验证'更新规则库）"
      />

      <el-table
        :data="displayPicks"
        v-loading="loading"
        border
        stripe
        @row-click="goDetail"
        style="width: 100%"
      >
        <el-table-column label="Agent理由" min-width="220">
          <template #default="{ row }">
            <div v-if="row.agent_reason" style="line-height: 1.5">
              <el-tag
                :type="
                  row.agent_confidence === 'high'
                    ? 'success'
                    : row.agent_confidence === 'low'
                      ? 'info'
                      : 'warning'
                "
                size="small"
                style="margin-right: 6px"
                >{{ row.agent_confidence || "mid" }}</el-tag
              >
              {{ row.agent_reason }}
            </div>
            <span v-else style="color: #c0c4cc">—</span>
          </template>
        </el-table-column>
        <el-table-column label="排名" width="70" align="center">
          <template #default="{ row }">
            <span style="font-weight: 600">{{ row.rank }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="ts_code" label="代码" width="110" />
        <el-table-column label="现价（元）" width="130" align="center">
          <template #header>
            <el-tooltip
              content="最新已采集交易日收盘价（未复权，可对照行情软件；非盘中实时）"
              placement="top"
            >
              <span>现价（元）</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <div
              v-if="row.price_now !== undefined && row.price_now !== null"
              style="line-height: 1.4"
            >
              <div style="font-weight: bold">
                {{ fmtPrice(row.price_now) }}
                <span
                  style="font-size: 12px"
                  :style="{
                    color:
                      (row.price_now_pct ?? 0) >= 0 ? '#f56c6c' : '#67c23a',
                  }"
                >
                  {{ fmtSignedPct(row.price_now_pct) }}
                </span>
              </div>
              <div style="color: #909399; font-size: 12px">
                {{ fmtDateShort(row.price_now_date) }} 收盘
              </div>
              <el-tag
                v-if="row.price_now_tag"
                size="small"
                :type="row.price_now_tag_type || 'info'"
                style="margin-top: 2px"
              >
                {{ row.price_now_tag }}
              </el-tag>
            </div>
            <span v-else style="color: #c0c4cc">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="名称" min-width="110" />
        <el-table-column label="形态" width="120" align="center">
          <template #default="{ row }">
            <el-tag size="small" type="primary">{{
              formLabel[row.form_type] || row.form_type
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="综合上涨概率" width="120" align="center">
          <template #default="{ row }">
            <span style="font-size: 16px; font-weight: 700; color: #f56c6c">
              {{ fmtProb(row.up_probability) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="次日概率(T+1)" width="110" align="center">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="(row.rule_prob || 0) > 0.5 ? 'success' : 'info'"
            >
              {{ fmtProb(row.rule_prob) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="命中规则" min-width="200">
          <template #default="{ row }">
            <template v-if="row.hit_rules && row.hit_rules.length">
              <el-tag
                v-for="(hr, i) in row.hit_rules.slice(0, 3)"
                :key="i"
                size="small"
                type="warning"
                style="margin: 2px 4px 2px 0"
              >
                {{ hr.name }} {{ fmtProb(hr.hit_rate_t1) }}
              </el-tag>
              <span
                v-if="row.hit_rules.length > 3"
                style="color: #909399; font-size: 12px"
                >+{{ row.hit_rules.length - 3 }}</span
              >
            </template>
            <span v-else style="color: #c0c4cc">—</span>
          </template>
        </el-table-column>
        <el-table-column label="买入价（元）" width="130" align="center">
          <template #default="{ row }">
            <div v-if="row.entry_plan" style="line-height: 1.4">
              <div style="color: #e6a23c; font-weight: bold">
                {{ fmtPrice(row.entry_plan.buy_low) }} ~
                {{ fmtPrice(row.entry_plan.buy_high) }}
              </div>
              <div style="color: #909399; font-size: 12px">
                T+1 开盘 · 高开 >3% 不追
              </div>
            </div>
            <span v-else style="color: #c0c4cc">—</span>
          </template>
        </el-table-column>
        <el-table-column label="止损价（元）" width="110" align="center">
          <template #default="{ row }">
            <div v-if="row.entry_plan" style="line-height: 1.4">
              <div style="color: #f56c6c; font-weight: bold">
                {{ fmtPrice(row.entry_plan.stop_loss) }}
              </div>
              <div style="color: #909399; font-size: 12px">收盘跌破离场</div>
            </div>
            <span v-else style="color: #c0c4cc">—</span>
          </template>
        </el-table-column>
        <el-table-column label="止盈价（元）" width="140" align="center">
          <template #default="{ row }">
            <div v-if="row.entry_plan" style="line-height: 1.4">
              <div style="color: #67c23a; font-weight: bold">
                T+{{ row.entry_plan.hold_days }}:
                {{ fmtPrice((row.entry_plan.take_profit || [])[0]?.price) }}
              </div>
              <div style="color: #67c23a; font-size: 12px">
                主升浪:
                {{ fmtPrice((row.entry_plan.take_profit || [])[1]?.price) }}
              </div>
            </div>
            <span v-else style="color: #c0c4cc">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作计划（含依据）" min-width="240">
          <template #default="{ row }">
            <div v-if="row.entry_plan" style="line-height: 1.5">
              <div style="font-size: 12px">{{ row.entry_plan_text }}</div>
              <div style="color: #909399; font-size: 12px; margin-top: 2px">
                回踩低吸 {{ fmtPrice(row.entry_plan.pullback_low) }}~{{
                  fmtPrice(row.entry_plan.pullback_high)
                }}
                · 基准价(T0收盘) {{ fmtPrice(row.entry_plan.ref_price) }} ·
                {{ row.entry_plan.basis?.source }}
                <template v-if="row.entry_plan.basis?.samples">
                  （样本 {{ row.entry_plan.basis.samples }}，T+5 胜率
                  {{ fmtProb(row.entry_plan.basis.win_rate_t5) }}）
                </template>
              </div>
            </div>
            <div
              v-else-if="row.entry_advice && row.entry_advice.desc"
              style="line-height: 1.5"
            >
              <el-tag size="small" type="warning" style="margin-bottom: 2px">
                {{ row.entry_advice.desc }}
              </el-tag>
              <div style="color: #909399; font-size: 12px">
                T+20 均收益 {{ fmtProb(row.entry_advice.avg_ret_t20) }} · 样本
                {{ row.entry_advice.count }}
              </div>
            </div>
            <span v-else style="color: #c0c4cc">—</span>
          </template>
        </el-table-column>
        <el-table-column label="命中条件（可解释）" min-width="260">
          <template #default="{ row }">
            <template v-if="row.hit_conditions && row.hit_conditions.length">
              <el-tag
                v-for="(h, i) in row.hit_conditions.slice(0, 4)"
                :key="i"
                size="small"
                style="margin: 2px 4px 2px 0"
              >
                {{ h.feature }}≥{{ fmtNum(h.threshold) }}→{{
                  fmtProb(h.up_prob)
                }}
              </el-tag>
            </template>
            <span v-else style="color: #c0c4cc">无命中高胜率条件</span>
          </template>
        </el-table-column>
        <el-table-column label="相似度" width="90" align="center">
          <template #default="{ row }">{{ fmtNum(row.similarity) }}</template>
        </el-table-column>
        <el-table-column label="风险" width="80" align="center">
          <template #default="{ row }">
            <el-tag :type="riskType(row.risk_level)" size="small">{{
              row.risk_level
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80" align="center">
          <template #default="{ row }">
            <el-button
              size="small"
              text
              type="primary"
              @click.stop="goDetail(row)"
              >详情</el-button
            >
          </template>
        </el-table-column>
      </el-table>

      <el-empty
        v-if="!loading && !displayPicks.length"
        :description="
          queryDate
            ? `该日期（${queryDate}）暂无榜单，请选择其他日期`
            : '今日榜单尚未生成，请点击「触发全市场扫描」（或等待每日 18:00 自动扫描）'
        "
      />
    </el-card>

    <!-- 基准对照（影子，plans/24 §11.19~§11.22）：
         后端仅在 `daily_baseline_mode=shadow` 时附加该字段 ⇒ 默认 off 时本卡片不渲染。
         它**不参与推荐决策**，只是把「B1/B2 基准」与「我们自己的可判定性」摆出来对照。 -->
    <el-card v-if="shadow" shadow="never" style="margin-top: 16px">
      <template #header>
        <div
          style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap"
        >
          <span style="font-weight: 600">基准对照（影子）</span>
          <el-tag size="small" type="info">不参与推荐决策</el-tag>
          <span style="color: #909399; font-size: 12px">
            持 {{ shadow.hold_days }} 日 · 扣双边费率
            {{ fmtPct(shadow.fee_rt, 2) }} · 单边滑点
            {{ shadow.slip_bp ?? "—" }}bp · 口径来源 plans/24 §11.16~§11.24
          </span>
        </div>
      </template>

      <el-alert
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
      >
        <template #title>{{ shadow.disclaimer }}</template>
      </el-alert>

      <div style="font-weight: 600; margin-bottom: 6px">
        ① 基准（索引化组合，不是推荐标的）
      </div>
      <el-table :data="shadowRows" size="small" border>
        <el-table-column prop="label" label="基准" min-width="220" />
        <el-table-column prop="codes" label="名单 / 池" width="140" />
        <el-table-column prop="net" label="年化净" width="90" />
        <el-table-column prop="t" label="t" width="70" />
        <el-table-column label="t_HAC" width="88">
          <template #header>
            <el-tooltip
              content="Newey-West t：修正重叠样本（lag = 持有期 − 1）。仅披露，不参与判定（plans/24 §11.28）"
              placement="top"
            >
              <span>t_HAC</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">{{ row.t_hac }}</template>
        </el-table-column>
        <el-table-column label="四条件" width="90">
          <template #default="{ row }">
            <el-tag
              :type="row.verdict === 'usable' ? 'success' : 'danger'"
              size="small"
            >
              {{ row.verdict === "usable" ? "可用" : row.verdict }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          prop="reason"
          label="原因"
          min-width="260"
          show-overflow-tooltip
        />
      </el-table>

      <div style="font-weight: 600; margin: 14px 0 6px">
        ② 我们自己（先判「可判定性」，再判四条件 —— §11.21 的护栏）
      </div>
      <el-table :data="oursRows" size="small" border>
        <el-table-column prop="label" label="口径" min-width="220" />
        <el-table-column prop="gross" label="毛/期" width="90" />
        <el-table-column label="判定" width="130">
          <template #default="{ row }">
            <el-tag
              :type="
                row.verdict === 'not_decidable'
                  ? 'info'
                  : row.verdict === 'usable'
                    ? 'success'
                    : 'danger'
              "
              size="small"
            >
              {{ row.verdict === "not_decidable" ? "不可判定" : row.verdict }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="seg" label="已有段" width="80" />
        <el-table-column prop="missing" label="缺失段" min-width="160" />
        <el-table-column
          prop="reason"
          label="原因"
          min-width="240"
          show-overflow-tooltip
        />
      </el-table>

      <div
        v-if="shadow.t_basis"
        style="color: #909399; font-size: 12px; margin-top: 10px"
      >
        口径：{{ shadow.t_basis }}
      </div>

      <div style="color: #909399; font-size: 12px; margin-top: 10px">
        参考（§11.18 同窗口实测）：随机抽 30 只
        {{ fmtPct(shadow.reference?.random30_per_year) }}/年 · 我们
        {{ fmtPct(shadow.reference?.ours_per_year) }}/年 · 差
        <b>{{ fmtPct(shadow.reference?.gap_pp) }}</b>
        <span v-if="shadow.reference?.source"
          >（{{ shadow.reference.source }}）</span
        >
      </div>
      <div
        v-for="(n, i) in shadow.notes || []"
        :key="i"
        style="color: #909399; font-size: 12px; margin-top: 4px"
      >
        · {{ n }}
      </div>
    </el-card>

    <!-- Agent 精筛日志（实时展示多 Agent 在做什么） -->
    <el-card
      v-if="agentStatus.status || agentLogs.length"
      style="margin-top: 16px"
    >
      <template #header>
        🤖 Agent 精筛日志
        <el-tag
          v-if="agentStatus.status === 'running'"
          size="small"
          type="warning"
          style="margin-left: 8px"
        >
          精筛中
        </el-tag>
        <el-tag
          v-else-if="agentStatus.status === 'success'"
          size="small"
          type="success"
          style="margin-left: 8px"
        >
          完成
        </el-tag>
        <el-tag
          v-else-if="agentStatus.status === 'failed'"
          size="small"
          type="danger"
          style="margin-left: 8px"
        >
          失败
        </el-tag>
        <span
          v-if="agentStatus.message"
          style="color: #909399; margin-left: 8px"
        >
          {{ agentStatus.message }}
        </span>
      </template>
      <div class="log-box">
        <div v-if="!agentLogs.length" style="color: #909399">
          暂无日志（点击「🤖 Agent 精筛」后实时显示各 Agent 输出）
        </div>
        <div v-for="(l, i) in agentLogs" :key="i" class="log-line">{{ l }}</div>
      </div>
    </el-card>

    <!-- 扫描详细日志（实时展示后端在做什么） -->
    <el-card style="margin-top: 16px">
      <template #header>
        📜 扫描详细日志
        <el-tag
          v-if="scanStatus.status === 'running'"
          size="small"
          type="warning"
          style="margin-left: 8px"
        >
          扫描中
        </el-tag>
        <el-tag
          v-else-if="scanStatus.status === 'success'"
          size="small"
          type="success"
          style="margin-left: 8px"
        >
          完成
        </el-tag>
        <el-tag
          v-else-if="scanStatus.status === 'failed'"
          size="small"
          type="danger"
          style="margin-left: 8px"
        >
          失败
        </el-tag>
        <el-tag
          v-else-if="scanStatus.status === 'cancelled'"
          size="small"
          type="info"
          style="margin-left: 8px"
        >
          已停止
        </el-tag>
        <el-tag
          v-if="scanStatus.cancel_requested && scanStatus.status === 'running'"
          size="small"
          type="warning"
          style="margin-left: 8px"
        >
          正在停止…
        </el-tag>
      </template>
      <div class="log-box">
        <div v-if="!scanLogs.length" style="color: #909399">
          暂无日志（触发扫描后实时显示后端各阶段输出）
        </div>
        <div v-for="(l, i) in scanLogs" :key="i" class="log-line">{{ l }}</div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
/* 扫描详细日志：终端风格深色滚动区 */
.log-box {
  background: #1e1e2e;
  border-radius: 6px;
  padding: 12px 14px;
  max-height: 360px;
  overflow-y: auto;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  font-size: 12px;
  line-height: 1.7;
  color: #c8d3f5;
  white-space: pre-wrap;
  word-break: break-all;
}

.log-line {
  padding: 1px 0;
  border-bottom: 1px dashed rgba(255, 255, 255, 0.06);
}

.log-line:last-child {
  border-bottom: none;
}

.log-box::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

.log-box::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.18);
  border-radius: 4px;
}

.log-box::-webkit-scrollbar-track {
  background: transparent;
}
</style>
