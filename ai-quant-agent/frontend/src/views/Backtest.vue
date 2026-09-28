<script setup lang="ts">
import { ref, onMounted, computed } from "vue";
import { triggerBacktest, getBacktestStatus, getBacktestReport } from "../api";

const loading = ref(false);
const status = ref<any>({});
const report = ref<any>(null);
const error = ref("");
// 默认全市场（max_stocks 留空），输入则限制股票数（调试用）
const params = ref<{
  start_date: string;
  end_date: string;
  max_stocks?: number;
}>({
  start_date: "20100101",
  end_date: "",
});

const fmtPct = (v: number | undefined) =>
  v === undefined || v === null ? "-" : `${(v * 100).toFixed(1)}%`;

const samples = computed(() => report.value?.result?.samples || {});
const probRules = computed(
  () => report.value?.result?.probability_table?.rules || [],
);
const perf = computed(() => report.value?.result?.performance || {});
const rankings = computed(
  () => report.value?.result?.attribution?.rankings || [],
);
const ab = computed(() => report.value?.result?.ab_validation || {});
const recs = computed(() => report.value?.result?.recommendations || []);
const logs = computed(() => status.value?.logs || []);

async function runBacktest() {
  loading.value = true;
  error.value = "";
  try {
    const p: any = { ...params.value };
    if (!p.end_date) delete p.end_date;
    if (!p.max_stocks) delete p.max_stocks; // 留空 = 全市场
    await triggerBacktest(p);
    await pollStatus();
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || "触发回测失败";
  } finally {
    loading.value = false;
  }
}

// 轮询上限：全市场回测实测可达 2 小时以上（本次 115 分钟），5s 间隔给足 12 小时（8640 次）。
// BugFix: 旧实现 for(i<1200) 仅约 100 分钟，回测未结束时轮询就耗尽退出，
// 页面永久冻结在「运行中 + 61%」的旧快照（后端其实已完成）——必须覆盖最长回测耗时。
const MAX_POLLS = 8640;

async function pollStatus() {
  // 单次轮询失败容错：瞬时超时/网络抖动不中断回测，连续失败超阈值才报错
  let pollFails = 0;
  let polls = 0;
  while (polls < MAX_POLLS) {
    polls += 1;
    try {
      const res = await getBacktestStatus();
      status.value = res.data;
      pollFails = 0;
      if (res.data.status === "success") {
        await loadReport();
        return;
      }
      if (res.data.status === "failed") {
        error.value = res.data.message || "回测失败";
        return;
      }
      if (res.data.status === "idle") {
        // 触发后状态仍 idle = 未被真正启动(可能系统忙被拒)，给出明确提示而非静默退出
        status.value.message =
          res.data.message || "回测未启动（系统忙或被拒），请稍后重试";
        return;
      }
      if (res.data.status !== "running" && res.data.status !== "pending")
        return;
    } catch (e: any) {
      pollFails += 1;
      if (pollFails >= 6) {
        error.value = e?.message || "轮询回测状态失败";
        return;
      }
    }
    await new Promise((r) => setTimeout(r, 5000));
  }
  error.value = "轮询超时（超过 12 小时仍未结束），请刷新页面或检查后端状态";
}

// 页面加载/刷新时同步后端真实状态：若回测仍在跑则自动接续轮询，
// 避免"后端已完成、页面仍显示运行中"的状态不同步（旧实现仅 loadReport，不取状态）
async function syncStatusOnMount() {
  try {
    const res = await getBacktestStatus();
    status.value = res.data;
    if (res.data?.status === "running" || res.data?.status === "pending") {
      await pollStatus();
    } else if (res.data?.status === "success") {
      await loadReport();
    }
  } catch {
    /* 后端未就绪时保持空状态，不阻塞页面 */
  }
}

async function loadReport() {
  try {
    const res = await getBacktestReport();
    report.value = res.data;
  } catch (e: any) {
    error.value = e?.message || "加载回测报告失败";
  }
}

onMounted(async () => {
  await loadReport();
  await syncStatusOnMount();
});
</script>

<template>
  <div style="padding: 24px">
    <h2>📊 回测中心</h2>

    <el-card style="margin-bottom: 16px">
      <el-form inline>
        <el-form-item label="起始日期">
          <el-input
            v-model="params.start_date"
            style="width: 140px"
            placeholder="YYYYMMDD"
          />
        </el-form-item>
        <el-form-item label="截止日期">
          <el-input
            v-model="params.end_date"
            style="width: 140px"
            placeholder="空=最新"
          />
        </el-form-item>
        <el-form-item label="股票数(留空=全市场)">
          <el-input-number
            v-model="params.max_stocks"
            :min="10"
            :max="5535"
            placeholder="全市场"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="runBacktest">
            触发回测
          </el-button>
        </el-form-item>
      </el-form>
      <div v-if="status.message" style="color: #606266; margin-top: 8px">
        状态: {{ status.message }} | 进度:
        {{ Math.round((status.progress || 0) * 100) }}%
      </div>
      <div v-if="error" style="color: #f56c6c; margin-top: 8px">
        {{ error }}
      </div>
    </el-card>

    <!-- 回测详细日志（实时展示后端在做什么） -->
    <el-card style="margin-bottom: 16px">
      <template #header>
        📜 回测详细日志
        <el-tag
          v-if="status.status === 'running'"
          size="small"
          type="warning"
          style="margin-left: 8px"
        >
          运行中
        </el-tag>
      </template>
      <div class="log-box">
        <div v-if="!logs.length" style="color: #909399">
          暂无日志（触发回测后实时显示后端各阶段输出）
        </div>
        <div v-for="(l, i) in logs" :key="i" class="log-line">{{ l }}</div>
      </div>
    </el-card>

    <template v-if="report">
      <!-- 样本统计 -->
      <el-card style="margin-bottom: 16px">
        <template #header>样本统计（精准采集）</template>
        <el-row :gutter="16">
          <el-col :span="4"><b>总数</b> {{ samples.total || 0 }}</el-col>
          <el-col :span="4"><b>成功</b> {{ samples.success || 0 }}</el-col>
          <el-col :span="4"><b>失败A</b> {{ samples.failure_A || 0 }}</el-col>
          <el-col :span="4"><b>失败B</b> {{ samples.failure_B || 0 }}</el-col>
          <el-col :span="4"><b>丢弃</b> {{ samples.discard || 0 }}</el-col>
        </el-row>
      </el-card>

      <!-- 条件概率表（用户核心需求） -->
      <el-card style="margin-bottom: 16px">
        <template #header>条件概率表（阈值 → 上涨概率）</template>
        <el-table
          :data="probRules"
          size="small"
          empty-text="样本不足，未产出规则（全市场回测后会出现）"
        >
          <el-table-column prop="feature" label="特征" />
          <el-table-column prop="threshold" label="阈值" />
          <el-table-column prop="sample_count" label="样本数" width="100" />
          <el-table-column label="上涨概率" width="120">
            <template #default="{ row }">{{ fmtPct(row.up_prob) }}</template>
          </el-table-column>
          <el-table-column label="基线" width="100">
            <template #default="{ row }">{{
              fmtPct(row.baseline_prob)
            }}</template>
          </el-table-column>
          <el-table-column label="提升" width="100">
            <template #default="{ row }"
              >+{{ (row.gain * 100).toFixed(1) }}pct</template
            >
          </el-table-column>
        </el-table>
      </el-card>

      <!-- A/B 验证 -->
      <el-card style="margin-bottom: 16px">
        <template #header>
          A/B 验证
          <el-tag
            :type="ab.decision === 'Go' ? 'success' : 'danger'"
            style="margin-left: 8px"
          >
            {{ ab.decision }}
          </el-tag>
        </template>
        <el-row :gutter="16">
          <el-col :span="6"
            ><b>准确率提升</b> {{ ab.accuracy_lift?.toFixed(1) }}pct</el-col
          >
          <el-col :span="6"
            ><b>McNemar p</b> {{ ab.p_mcnemar?.toFixed(4) }}</el-col
          >
          <el-col :span="6"
            ><b>配对t p</b> {{ ab.p_paired_t?.toFixed(4) }}</el-col
          >
          <el-col :span="6"
            ><b>95% CI</b> [{{ ab.ci?.[0]?.toFixed(1) }},
            {{ ab.ci?.[1]?.toFixed(1) }}]</el-col
          >
        </el-row>
        <div v-if="ab.reasons?.length" style="margin-top: 8px">
          <div
            v-for="(r, i) in ab.reasons"
            :key="i"
            style="color: #909399; font-size: 13px"
          >
            {{ r }}
          </div>
        </div>
      </el-card>

      <!-- 绩效分析 -->
      <el-card style="margin-bottom: 16px">
        <template #header>绩效分析</template>
        <el-row :gutter="16">
          <el-col :span="4"><b>命中率</b> {{ fmtPct(perf.hit_rate) }}</el-col>
          <el-col :span="4"><b>准确率</b> {{ fmtPct(perf.accuracy) }}</el-col>
          <el-col :span="4"
            ><b>平均T+5</b> {{ fmtPct(perf.avg_t5_return) }}</el-col
          >
          <el-col :span="4"
            ><b>T+20延续</b> {{ fmtPct(perf.t20_continuation) }}</el-col
          >
          <el-col :span="4"><b>夏普</b> {{ perf.sharpe?.toFixed(2) }}</el-col>
          <el-col :span="4"
            ><b>最大回撤</b> {{ fmtPct(perf.max_drawdown) }}</el-col
          >
        </el-row>
      </el-card>

      <!-- 归因分析 -->
      <el-card style="margin-bottom: 16px">
        <template #header>成功/失败归因分析</template>
        <el-table
          :data="rankings.slice(0, 10)"
          size="small"
          empty-text="样本不足"
        >
          <el-table-column prop="feature" label="特征" />
          <el-table-column prop="auc" label="AUC" width="100" />
          <el-table-column prop="info_gain" label="信息增益" width="100" />
          <el-table-column prop="success_mean" label="成功均值" width="110" />
          <el-table-column prop="failure_mean" label="失败均值" width="110" />
          <el-table-column label="关键" width="80">
            <template #default="{ row }">{{ row.is_key ? "⭐" : "" }}</template>
          </el-table-column>
        </el-table>
      </el-card>

      <!-- 每日推荐（演示） -->
      <el-card>
        <template #header>每日推荐（综合上涨概率 TOP-10 演示）</template>
        <el-table :data="recs" size="small" empty-text="样本不足，暂无推荐">
          <el-table-column prop="ts_code" label="代码" width="110" />
          <el-table-column prop="form_type" label="形态" width="80" />
          <el-table-column label="综合概率" width="120">
            <template #default="{ row }">{{ fmtPct(row.prob) }}</template>
          </el-table-column>
          <el-table-column label="负向分" width="100">
            <template #default="{ row }">{{
              row.negative_score?.toFixed(2)
            }}</template>
          </el-table-column>
          <el-table-column prop="risk_level" label="风险" width="80" />
          <el-table-column label="命中条件" min-width="200">
            <template #default="{ row }">
              <span
                v-for="(h, i) in (row.hit_rules || []).slice(0, 3)"
                :key="i"
                style="margin-right: 8px"
              >
                {{ h.feature || h.combo }}:{{ (h.up_prob * 100).toFixed(0) }}%
              </span>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </template>
    <el-empty v-else description="尚未触发回测，点击上方按钮开始" />
  </div>
</template>

<style scoped>
/* 回测详细日志：终端风格深色滚动区 */
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
