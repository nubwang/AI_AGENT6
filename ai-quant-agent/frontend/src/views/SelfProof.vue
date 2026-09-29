<script setup lang="ts">
/**
 * 自证测试（plans/25）
 *
 * 一句话：用历史数据自证选股准确率 —— 把 as_of 设到过去某天，用当天能看到的数据重跑选股，
 * 再用之后的真实行情验证；不用等几天才能验证。
 *
 * 页面上三件最重要的事（都做成醒目提示，避免误读）：
 *   1. **重演口径**：它证明"方法在同一段历史里是否有效"，不是当年实盘成绩；
 *   2. **主指标是超额**（vs 同期全市场等权），不是绝对胜率（胜率会被大盘 beta 主导）；
 *   3. **抽样太小会一条票都出不来**（实测出票率 0.13%/只/天）→ 默认全市场，省时间请调大步长。
 */
import { onMounted, onUnmounted, ref } from "vue";
import { ElMessage } from "element-plus";
import {
  estimateSelfProof,
  getRegimeCandidates,
  getSelfProofGate,
  getSelfProofParams,
  getSelfProofReport,
  getSelfProofStatus,
  listSelfProofRuns,
  runSelfProof,
  settleSelfProof,
  stopSelfProof,
} from "../api";

const gate = ref<any>(null);
const params = ref<any>(null);
const est = ref<any>(null);
const status = ref<any>({});
const report = ref<any>(null);
const runs = ref<any[]>([]);
const regime = ref<any>(null);
const busy = ref(false);
const loadingReport = ref(false);
let timer: any = null;

const form = ref({
  start: "20240102",
  end: "",
  step: 1,
  universe: 0, // 0 = 全市场（推荐）
  max_days: 250,
  llm: false,
  workers: 1,
});

const pct = (v: any) =>
  v === null || v === undefined ? "—" : `${(Number(v) * 100).toFixed(2)}%`;
const num = (v: any, d = 4) =>
  v === null || v === undefined ? "—" : Number(v).toFixed(d);

async function loadBase() {
  try {
    const [g, p, r, rg] = await Promise.all([
      getSelfProofGate(),
      getSelfProofParams(),
      listSelfProofRuns(20),
      getRegimeCandidates(),
    ]);
    gate.value = g.data;
    params.value = p.data;
    runs.value = r.data.items || [];
    regime.value = rg.data;
    form.value.step = Number(p.data.step ?? 1) || 1;
    form.value.universe = Number(p.data.universe ?? 0) || 0;
    form.value.max_days = Number(p.data.max_days ?? 250) || 250;
    form.value.workers = Number(p.data.workers ?? 1) || 1;
  } catch (e: any) {
    ElMessage.error(
      "加载自证基础信息失败：" + (e?.response?.data?.detail || e.message),
    );
  }
}

async function doEstimate() {
  busy.value = true;
  try {
    const { data } = await estimateSelfProof({ ...form.value });
    est.value = data;
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e.message);
  } finally {
    busy.value = false;
  }
}

async function doRun() {
  busy.value = true;
  try {
    const { data } = await runSelfProof({ ...form.value });
    est.value = data.estimate;
    ElMessage.success(`已启动自证任务：${data.run_id}`);
    await refreshStatus();
    startPolling();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e.message);
  } finally {
    busy.value = false;
  }
}

async function doStop() {
  try {
    const { data } = await stopSelfProof();
    ElMessage.info(data.message || "已请求停止");
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e.message);
  }
}

async function refreshStatus() {
  try {
    const { data } = await getSelfProofStatus();
    status.value = data;
    if (data.status === "running") {
      startPolling();
    } else {
      stopPolling();
      if (data.status === "success" || data.status === "stopped") {
        await loadRuns();
      }
    }
  } catch {
    /* 轮询失败不打扰用户 */
  }
}

function startPolling() {
  if (timer) return;
  timer = setInterval(refreshStatus, 3000);
}
function stopPolling() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
}

async function loadReport(runId: string, rebuild = false) {
  if (!runId) return;
  loadingReport.value = true;
  try {
    const { data } = await getSelfProofReport(runId, rebuild);
    report.value = data;
    if (rebuild) ElMessage.success("已重新结算并组装报告");
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e.message);
  } finally {
    loadingReport.value = false;
  }
}

async function loadRuns() {
  const { data } = await listSelfProofRuns(20);
  runs.value = data.items || [];
}

async function doSettle(runId: string) {
  try {
    await settleSelfProof(runId);
    await loadReport(runId);
    await loadRuns();
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e.message);
  }
}

onMounted(async () => {
  await loadBase();
  await refreshStatus();
});
onUnmounted(stopPolling);
</script>

<template>
  <div style="padding: 20px">
    <h2 style="margin: 0 0 12px">🧾 自证测试</h2>
    <div style="color: #606266; margin-bottom: 16px">
      把「选股」放回历史：选一个起始日 → 用**当天能看到的数据**重跑选股 →
      用**之后的真实行情**验证。 不用等几天才知道准不准。
    </div>

    <!-- ① 数据门 / 口径声明（必须最先看到） -->
    <el-alert
      v-if="gate"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 16px"
    >
      <template #title>口径与边界（先看这里，不然数字会读错）</template>
      <div style="line-height: 1.9">
        · 最早可自证日期：<b>{{
          gate.data_gate?.earliest_full_as_of || "—"
        }}</b>
        （数据门：早于它的 as_of 数据不全）｜最新可用：{{
          gate.latest_as_of || "—"
        }}<br />
        · 结果一律是
        <b>重演口径</b
        >：证明「方法在同一段历史里是否有效」，不是当年实盘成绩<br />
        · 主指标是 <b>超额</b>（vs 同期全市场等权），不是胜率 —— 胜率会被大盘
        beta 主导<br />
        · 幸存者偏差：历史全市场扫描<b>缺退市股</b> （{{
          gate.survivorship?.missing_count ?? "—"
        }}
        只 / {{ pct(gate.survivorship?.missing_ratio) }}），报告里会带这条声明
      </div>
    </el-alert>

    <!-- ② 模式 A：起始日 → 自证 -->
    <el-card style="margin-bottom: 16px">
      <template #header>
        <b>模式 A · 从起始日一路自证</b>
        <span style="color: #909399; margin-left: 8px; font-size: 12px">
          （规则档默认零 API 费用；LLM 档 =
          完全复刻每日推荐，预算不设上限、不会熔断）
        </span>
      </template>
      <el-form :inline="true" label-width="86px">
        <el-form-item label="起始日">
          <el-date-picker
            v-model="form.start"
            type="date"
            value-format="YYYYMMDD"
            placeholder="如 20240102"
            :disabled-date="(d: Date) => d.getTime() > Date.now()"
          />
        </el-form-item>
        <el-form-item label="结束日">
          <el-date-picker
            v-model="form.end"
            type="date"
            value-format="YYYYMMDD"
            placeholder="留空 = 到最新"
          />
        </el-form-item>
        <el-form-item label="步长(天)">
          <el-input-number v-model="form.step" :min="1" :max="20" />
        </el-form-item>
        <el-form-item label="抽样只数">
          <el-input-number
            v-model="form.universe"
            :min="0"
            :max="6000"
            :step="50"
          />
          <span style="margin-left: 6px; color: #909399; font-size: 12px"
            >0 = 全市场（推荐）</span
          >
        </el-form-item>
        <el-form-item label="最多天数">
          <el-input-number
            v-model="form.max_days"
            :min="1"
            :max="2000"
            :step="10"
          />
        </el-form-item>
        <el-form-item label="LLM 档">
          <el-switch v-model="form.llm" />
        </el-form-item>
        <el-form-item label="并行度">
          <el-input-number v-model="form.workers" :min="1" :max="16" />
          <span style="margin-left: 6px; color: #e6a23c; font-size: 12px">
            实验性：并行实测尚未达标，建议保持 1
          </span>
        </el-form-item>
        <el-form-item>
          <el-button :loading="busy" @click="doEstimate"
            >预估耗时 / 费用 / 样本量</el-button
          >
          <el-button type="primary" :loading="busy" @click="doRun"
            >开始自证</el-button
          >
        </el-form-item>
      </el-form>

      <div v-if="est" style="margin-top: 8px">
        <el-descriptions :column="4" border size="small">
          <el-descriptions-item label="回放天数"
            >{{ est.days }} 天</el-descriptions-item
          >
          <el-descriptions-item label="区间">
            {{ est.first }} ~ {{ est.last }}
          </el-descriptions-item>
          <el-descriptions-item label="预计耗时">{{
            est.wall_human
          }}</el-descriptions-item>
          <el-descriptions-item label="预计出票">
            {{ est.picks_estimate }} 条
            <el-tag
              :type="est.sample_enough ? 'success' : 'danger'"
              size="small"
            >
              {{ est.sample_enough ? "够判" : "不足" }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="引擎">{{
            est.engine_note
          }}</el-descriptions-item>
          <el-descriptions-item label="有效股票数">
            {{ est.universe_effective }} 只
          </el-descriptions-item>
          <el-descriptions-item label="LLM 费用" :span="2">
            <span v-if="est.llm_cost">
              低 {{ est.llm_cost.cost_low_cny ?? "—" }} / 中
              {{ est.llm_cost.cost_mid_peak_cny ?? "—" }} / 高
              {{ est.llm_cost.cost_high_cny ?? "—" }} 元
              <span v-if="est.llm_cost.note" style="color: #909399">
                （{{ est.llm_cost.note }}）
              </span>
            </span>
            <span v-else>规则档 0 元</span>
          </el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-if="est.warning"
          type="error"
          :closable="false"
          show-icon
          style="margin-top: 10px"
          :title="est.warning"
        />
        <el-alert
          v-if="est.warning_long"
          type="warning"
          :closable="false"
          show-icon
          style="margin-top: 8px"
          :title="est.warning_long"
        />
      </div>
    </el-card>

    <!-- ③ 运行状态 -->
    <el-card style="margin-bottom: 16px">
      <template #header><b>运行状态</b></template>
      <el-descriptions :column="4" border size="small">
        <el-descriptions-item label="状态">
          <el-tag
            :type="
              status.status === 'running'
                ? 'primary'
                : status.status === 'success'
                  ? 'success'
                  : status.status === 'failed'
                    ? 'danger'
                    : 'info'
            "
          >
            {{ status.status || "idle" }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="任务">
          {{ status.run_id || "—" }}
          <el-button
            v-if="status.run_id"
            link
            type="primary"
            size="small"
            @click="loadReport(status.run_id)"
          >
            看报告
          </el-button>
        </el-descriptions-item>
        <el-descriptions-item label="已完成">
          {{ status.state?.done ?? 0 }} / {{ status.state?.total ?? "—" }} 天
        </el-descriptions-item>
        <el-descriptions-item label="已出票">
          {{ status.state?.picks_total ?? 0 }} 条
        </el-descriptions-item>
        <el-descriptions-item label="引擎">
          workers = {{ status.state?.workers ?? "—" }}（1 = 串行）
        </el-descriptions-item>
        <el-descriptions-item label="耗时">
          {{ status.state?.elapsed_sec ?? "—" }} 秒
        </el-descriptions-item>
        <el-descriptions-item label="提示" :span="2">{{
          status.message || "—"
        }}</el-descriptions-item>
      </el-descriptions>
      <div v-if="status.status === 'running'" style="margin-top: 12px">
        <el-progress :percentage="status.progress?.pct || 0" />
        <div style="color: #909399; font-size: 12px; margin-top: 6px">
          当前 as_of {{ status.progress?.as_of }} ｜ 预计剩余
          {{ status.progress?.eta_sec ?? "—" }} 秒（粗估，随行情复杂度波动）
        </div>
      </div>
      <div style="margin-top: 12px">
        <el-button
          type="danger"
          plain
          @click="doStop"
          :disabled="status.status !== 'running'"
        >
          停止（当前这天跑完即停，可续跑）
        </el-button>
        <el-button @click="refreshStatus">刷新</el-button>
      </div>
    </el-card>

    <!-- ④ 报告 -->
    <!-- 后端契约：没有结论时返回 ok=false + reason + hint，绝不用一堆 None 冒充结论 -->
    <el-card v-if="report && report.ok === false" style="margin-bottom: 16px">
      <template #header><b>自证报告</b></template>
      <el-alert
        type="error"
        :closable="false"
        show-icon
        :title="report.reason || '该任务还没有可用结论'"
      >
        <div style="line-height: 1.8">{{ report.hint }}</div>
      </el-alert>
      <div style="color: #909399; font-size: 12px; margin-top: 8px">
        任务：{{ report.run_id }} ｜ 点下方历史任务里的『重新结算』可重试。
      </div>
    </el-card>

    <el-card v-if="report && report.ok !== false" style="margin-bottom: 16px">
      <template #header>
        <b>自证报告</b>
        <span style="color: #909399; margin-left: 8px; font-size: 12px">
          {{ report.run_id }} ｜ {{ report.flags?.terminology || "" }}
        </span>
        <el-button
          link
          type="primary"
          style="margin-left: 10px"
          :loading="loadingReport"
          @click="loadReport(report.run_id, true)"
        >
          重新结算
        </el-button>
      </template>

      <el-descriptions :column="4" border size="small">
        <el-descriptions-item label="超额 T+5（主指标）">
          <b>{{ pct(report.metrics?.excess_t5?.avg) }}</b>
          <span style="color: #909399">
            ｜ n={{ report.metrics?.excess_t5?.n }}</span
          >
        </el-descriptions-item>
        <el-descriptions-item label="超额胜率">
          {{ pct(report.metrics?.excess_t5?.win_rate) }}
        </el-descriptions-item>
        <el-descriptions-item label="t 值（粗筛）">
          {{ num(report.metrics?.excess_t5?.t_stat, 2) }}
        </el-descriptions-item>
        <el-descriptions-item label="样本量门槛">
          <el-tag
            :type="report.verdict?.sufficient_samples ? 'success' : 'warning'"
          >
            {{
              report.verdict?.sufficient_samples ? "达门槛" : "不足（只作观察）"
            }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="毛收益 T+5">
          {{ pct(report.metrics?.t5?.avg_gross) }}
        </el-descriptions-item>
        <el-descriptions-item label="基准（随机候选）">
          {{ pct(report.metrics?.bench_t5?.avg) }}
        </el-descriptions-item>
        <el-descriptions-item label="lift vs 基准">
          {{ pct(report.metrics?.lift_vs_bench) }}
        </el-descriptions-item>
        <el-descriptions-item label="T+1 / T+20 胜率">
          {{ pct(report.metrics?.t1?.win_rate) }} /
          {{ pct(report.metrics?.t20?.win_rate) }}
        </el-descriptions-item>
      </el-descriptions>

      <el-tabs style="margin-top: 12px">
        <el-tab-pane label="分环境">
          <el-table
            :data="Object.entries(report.layers?.by_regime || {})"
            size="small"
          >
            <el-table-column prop="0" label="环境" width="120" />
            <el-table-column label="n" width="80">
              <template #default="{ row }">{{ row[1].n }}</template>
            </el-table-column>
            <el-table-column label="平均 T+5">
              <template #default="{ row }">{{ pct(row[1].avg_t5) }}</template>
            </el-table-column>
            <el-table-column label="平均超额">
              <template #default="{ row }">{{
                pct(row[1].avg_excess_t5)
              }}</template>
            </el-table-column>
            <el-table-column label="超额胜率">
              <template #default="{ row }">{{
                pct(row[1].win_rate_excess)
              }}</template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane label="分形态">
          <el-table
            :data="Object.entries(report.layers?.by_form || {})"
            size="small"
          >
            <el-table-column prop="0" label="形态" width="120" />
            <el-table-column label="n" width="80">
              <template #default="{ row }">{{ row[1].n }}</template>
            </el-table-column>
            <el-table-column label="平均 T+5">
              <template #default="{ row }">{{ pct(row[1].avg_t5) }}</template>
            </el-table-column>
            <el-table-column label="平均超额">
              <template #default="{ row }">{{
                pct(row[1].avg_excess_t5)
              }}</template>
            </el-table-column>
            <el-table-column label="超额胜率">
              <template #default="{ row }">{{
                pct(row[1].win_rate_excess)
              }}</template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane label="IS / OOS">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="模式">
              {{ report.is_oos?.mode }}（占比 {{ report.is_oos?.ratio }}）
            </el-descriptions-item>
            <el-descriptions-item label="冻结留出年份">
              {{
                (report.is_oos?.frozen_oos_years || []).join(", ") ||
                "由年份轮换决定"
              }}
            </el-descriptions-item>
            <el-descriptions-item label="IS 内样本">
              n={{ report.is_oos?.in_sample?.n }} ｜ 超额均值
              {{ pct(report.is_oos?.in_sample?.avg_excess_t5) }}
            </el-descriptions-item>
            <el-descriptions-item label="OOS 留出（验收看这条）">
              n={{ report.is_oos?.out_of_sample?.n }} ｜ 超额均值
              {{ pct(report.is_oos?.out_of_sample?.avg_excess_t5) }}
            </el-descriptions-item>
          </el-descriptions>
          <el-alert
            type="info"
            :closable="false"
            style="margin-top: 8px"
            :title="report.is_oos?.note"
          />
        </el-tab-pane>
        <el-tab-pane label="失效归因">
          <div style="margin-bottom: 8px">
            失败 {{ report.attribution?.n_failures ?? "—" }} 条 ｜
            {{ report.attribution?.note }}
          </div>
          <el-table
            :data="Object.entries(report.attribution?.error_type_counts || {})"
            size="small"
          >
            <el-table-column prop="0" label="归因" />
            <el-table-column prop="1" label="条数" width="100" />
          </el-table>
        </el-tab-pane>
        <el-tab-pane label="费用 / 声明">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="LLM 调用次数">
              {{ report.usage?.llm_calls ?? 0 }}
            </el-descriptions-item>
            <el-descriptions-item label="费用（元）">
              {{ report.usage?.cost_cny ?? 0 }}
            </el-descriptions-item>
            <el-descriptions-item label="说明" :span="2">
              {{ report.usage?.note || report.usage?.cost_note }}
            </el-descriptions-item>
            <el-descriptions-item label="前视徽标" :span="2">
              <pre style="margin: 0; white-space: pre-wrap">{{
                JSON.stringify(report.flags?.forward_look || {}, null, 2)
              }}</pre>
            </el-descriptions-item>
            <el-descriptions-item label="数据门声明" :span="2">
              {{ report.flags?.data_gate?.earliest_full_as_of || "—" }}
              起可自证； 幸存者偏差缺失
              {{ report.flags?.survivorship?.missing_count ?? "—" }} 只
            </el-descriptions-item>
          </el-descriptions>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- ⑤ 历史任务 -->
    <el-card style="margin-bottom: 16px">
      <template #header>
        <b>历史自证任务</b>
        <el-button
          link
          type="primary"
          style="margin-left: 10px"
          @click="loadRuns"
          >刷新</el-button
        >
      </template>
      <el-table :data="runs" size="small">
        <el-table-column prop="run_id" label="任务" width="230" />
        <el-table-column prop="start" label="起始" width="110" />
        <el-table-column prop="step" label="步长" width="70" />
        <el-table-column label="抽样" width="90">
          <template #default="{ row }">{{ row.universe || "全市场" }}</template>
        </el-table-column>
        <el-table-column label="天数" width="100">
          <template #default="{ row }"
            >{{ row.dates_done }}/{{ row.dates_total }}</template
          >
        </el-table-column>
        <el-table-column label="推荐" width="80">
          <template #default="{ row }">{{ row.picks_total }}</template>
        </el-table-column>
        <el-table-column label="耗时(秒)" width="100">
          <template #default="{ row }">{{ row.elapsed_sec }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">{{ row.status }}</template>
        </el-table-column>
        <el-table-column label="操作">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              size="small"
              @click="loadReport(row.run_id)"
            >
              看报告
            </el-button>
            <el-button
              link
              type="primary"
              size="small"
              @click="doSettle(row.run_id)"
            >
              重新结算
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- ⑥ 模式 B：大盘相似波段 -->
    <el-card>
      <template #header><b>模式 B · 大盘相似波段（跨年份验证）</b></template>
      <el-alert
        v-if="regime && !regime.implemented"
        type="info"
        :closable="false"
        show-icon
        :title="regime.note"
      />
      <div v-else-if="regime" style="color: #606266">
        Top-K：{{ regime.items?.length || 0 }} 段
      </div>
      <div style="color: #909399; font-size: 12px; margin-top: 8px">
        在它落地前，跨年份验证请用上面的模式 A：把「起始日」设到目标年份即可。
      </div>
    </el-card>
  </div>
</template>
