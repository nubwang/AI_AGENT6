<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from "vue";
import { ElMessageBox } from "element-plus";
import {
  listApis,
  getStats,
  triggerCollection,
  collectStage1,
  collectStage2,
  collectStage3,
  collectStage4,
  getCollectStatus,
  stopStage1,
  stopStage2,
  stopStage3,
  stopStage4,
  stopCollectAll,
  getRecommendScanStatus,
  confirmRecommendScan,
  dismissRecommendScan,
} from "../api";

const apiGroups = ref<any[]>([]);
const stats = ref<any>({});
const loading = ref(false);
const stageLoading = ref({ 1: false, 2: false, 3: false, 4: false });
const result = ref("");
const wsStatus = ref("未连接");
const pendingStatus = ref<any>({
  has_pending: false,
  total_pending: 0,
  stages: {},
});
const logs = ref<{ time: string; content: string; type: string }[]>([]);
const logContainer = ref<HTMLDivElement | null>(null);
const dotCount = ref(0);
let dotTimer: ReturnType<typeof setInterval> | null = null;
let statusTimer: ReturnType<typeof setInterval> | null = null;

const progress = ref({
  stage: 0,
  stage_name: "",
  total: 0,
  current: 0,
  api: "",
  status: "idle",
  overall_pct: 0,
  elapsed: 0,
  eta: 0,
  api_count: 0,
  stages: [
    { idx: 1, name: "基础信息", total: 0, current: 0, pct: 0 },
    { idx: 2, name: "日频批量数据", total: 0, current: 0, pct: 0 },
    { idx: 3, name: "个股数据循环", total: 0, current: 0, pct: 0 },
    { idx: 4, name: "补充数据", total: 0, current: 0, pct: 0 },
  ],
  api_statuses: {} as Record<
    string,
    { desc: string; stage: number; status: string; rows: number }
  >,
});
let ws: WebSocket | null = null;
let wsMounted = true; // 组件是否仍挂载: 卸载后停止重连, 防止WS连接泄漏
let wsReconnectTimer: ReturnType<typeof setTimeout> | null = null;
// 采集完成 → 弹窗询问是否跑每日推荐（同一数据日期只问一次，防重复弹窗）
const scanPromptedDate = ref("");
let scanCheckTimer: ReturnType<typeof setInterval> | null = null;
const scanAsking = ref(false);

// 当前阶段百分比
const currentPct = computed(() =>
  progress.value.total > 0
    ? Math.round((progress.value.current / progress.value.total) * 100)
    : 0,
);

// 是否有采集任务在运行(用于禁用触发按钮, 防止连点)
const collecting = computed(() => progress.value.status === "running");

const overallPct = computed(() => progress.value.overall_pct || 0);

const fmtTime = (sec: number) => {
  if (!sec && sec !== 0) return "--:--";
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
};

const dots = computed(() => ".".repeat(dotCount.value));

// ── 42 API 实时状态计算 ──────────────────────────────
const stageNames = ["", "基础信息", "日频批量", "个股循环", "补充数据"];

// 按阶段分组的API列表
const apisByStage = computed(() => {
  const groups: {
    stage: number;
    name: string;
    apis: { key: string; desc: string; status: string; rows: number }[];
  }[] = [];
  const map: Record<number, (typeof groups)[0]> = {};
  for (const [key, info] of Object.entries(progress.value.api_statuses)) {
    const s = info.stage;
    if (!map[s]) {
      map[s] = { stage: s, name: stageNames[s] || `阶段${s}`, apis: [] };
      groups.push(map[s]);
    }
    map[s].apis.push({
      key,
      desc: info.desc,
      status: info.status,
      rows: info.rows,
    });
  }
  return groups.sort((a, b) => a.stage - b.stage);
});

// 各状态数量统计
const statusCounts = computed(() => {
  const c = { done: 0, running: 0, pending: 0, error: 0 };
  for (const info of Object.values(progress.value.api_statuses)) {
    if (c[info.status as keyof typeof c] !== undefined)
      c[info.status as keyof typeof c]++;
  }
  return c;
});

// 状态样式
const statusStyle = (status: string) => {
  const s: Record<
    string,
    { color: string; bg: string; icon: string; label: string }
  > = {
    done: { color: "#67c23a", bg: "#f0f9eb", icon: "✅", label: "已完成" },
    running: { color: "#409eff", bg: "#ecf5ff", icon: "🔄", label: "采集中" },
    pending: { color: "#909399", bg: "#f4f4f5", icon: "⏳", label: "待采集" },
    error: { color: "#f56c6c", bg: "#fef0f0", icon: "❌", label: "失败" },
  };
  return s[status] || s.pending;
};

const connectWs = () => {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${protocol}//${location.hostname}:8000/ws/logs`);
  wsStatus.value = "连接中...";
  ws.onopen = () => {
    wsMounted = true;
    wsStatus.value = "已连接";
    ws?.send("ping");
  };
  ws.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.type === "progress") {
      progress.value = data;
      // 采集刚结束 → 询问是否立即运行每日推荐扫描（后端 confirm 模式）
      if (data.status === "done") maybeAskScan();
    } else {
      const content =
        data.content ||
        `${data.desc || data.api} → ${data.status} (${data.rows || 0}行)`;
      logs.value.push({ time: data.ts || "", content, type: data.type });
      if (logs.value.length > 500) logs.value = logs.value.slice(-300);
      // 自动滚动
      setTimeout(() => {
        if (logContainer.value)
          logContainer.value.scrollTop = logContainer.value.scrollHeight;
      }, 50);
    }
  };
  ws.onclose = () => {
    if (!wsMounted) return; // 组件已卸载: 不再自动重连
    wsStatus.value = "已断开";
    ws = null;
    wsReconnectTimer = setTimeout(connectWs, 3000);
  };
  ws.onerror = () => {
    wsStatus.value = "连接错误";
    ws?.close();
  };
};

onMounted(() => {
  loadData();
  loadCollectStatus();
  connectWs();
  dotTimer = setInterval(() => {
    dotCount.value = (dotCount.value + 1) % 4;
  }, 500);
  // 每10秒刷新断点续采状态
  statusTimer = setInterval(loadCollectStatus, 10000);
  // 采集完成后的"是否跑每日推荐"询问：WS 掉线/定时采集时的兜底轮询
  maybeAskScan();
  scanCheckTimer = setInterval(maybeAskScan, 15000);
});

onUnmounted(() => {
  wsMounted = false; // 停止自动重连, 防止切走后WS连接持续泄漏
  if (wsReconnectTimer) {
    clearTimeout(wsReconnectTimer);
    wsReconnectTimer = null;
  }
  ws?.close();
  if (dotTimer) clearInterval(dotTimer);
  if (statusTimer) clearInterval(statusTimer);
  if (scanCheckTimer) clearInterval(scanCheckTimer);
});

const loadData = async () => {
  try {
    const [apisRes, statsRes] = await Promise.all([listApis(), getStats()]);
    apiGroups.value = apisRes.data;
    stats.value = statsRes.data;
  } catch (e: any) {
    result.value = `❌ ${e.message}`;
  }
};

const loadCollectStatus = async () => {
  try {
    const res = await getCollectStatus();
    pendingStatus.value = res.data;
  } catch (e) {
    // 忽略状态获取失败
  }
};

// 采集完成 → 弹窗询问是否立即运行每日推荐扫描（plans/09 八）
// 仅在后端 auto_scan_after_collect=confirm 时 confirm_pending.pending 才为真；
// auto 模式后端已自动跑、off 模式后端会跳过，前端都不需要参与。
const maybeAskScan = async () => {
  if (scanAsking.value) return; // 正在弹窗/等用户点击 → 不重复弹
  let scan: any = null;
  try {
    const res = await getRecommendScanStatus();
    scan = res.data;
  } catch (e) {
    return; // 状态取不到就静默跳过: 绝不因推荐模块异常干扰采集页面
  }
  const cp = scan?.confirm_pending;
  if (!cp?.pending) return;
  const dateKey = cp.data_date || "";
  // 同一数据日期只问一次（用户选过"稍后再说"就不再打扰）
  if (dateKey && scanPromptedDate.value === dateKey) return;

  scanAsking.value = true;
  let accepted = false;
  let action = "";
  try {
    await ElMessageBox.confirm(
      `数据采集已完成（数据日期 ${dateKey || "未知"}）。是否立即运行每日推荐扫描？`,
      "是否运行每日推荐扫描",
      {
        confirmButtonText: "立即运行",
        cancelButtonText: "稍后再说",
        distinguishCancelAndClose: true,
        type: "info",
      },
    );
    accepted = true;
  } catch (a: any) {
    action = typeof a === "string" ? a : "close"; // 'cancel' | 'close'
  }
  scanPromptedDate.value = dateKey; // 无论选什么，同一数据日期都不再重复弹
  try {
    if (accepted) {
      await confirmRecommendScan();
      result.value = "▶️ 已开始每日推荐扫描（可到「每日推荐」页查看进度）";
    } else {
      await dismissRecommendScan(
        action === "cancel" ? "user_skip" : "user_close",
      );
      result.value = "⏸️ 已跳过每日推荐扫描（可随时在「每日推荐」页手动运行）";
    }
  } catch (e: any) {
    result.value = `❌ 每日推荐扫描请求失败: ${e.message}`;
  } finally {
    scanAsking.value = false;
  }
};

const doTriggerCollection = async () => {
  loading.value = true;
  result.value = "";
  logs.value = [];
  try {
    await triggerCollection();
    result.value = "✅ 已触发全量采集";
  } catch (e: any) {
    result.value = `❌ ${e.message}`;
  }
  loading.value = false;
};

const _stageLabel = (s: number) =>
  ["", "基础信息", "日频批量", "个股循环", "补充数据"][s] || `阶段${s}`;
const doCollectStage = async (stage: number) => {
  stageLoading.value[stage as keyof typeof stageLoading.value] = true;
  result.value = "";
  logs.value = [];
  try {
    if (stage === 1) await collectStage1();
    else if (stage === 2) await collectStage2();
    else if (stage === 3) await collectStage3();
    else if (stage === 4) await collectStage4();
    result.value = `✅ 阶段${stage}(${_stageLabel(stage)}) 已触发`;
  } catch (e: any) {
    result.value = `❌ ${e.message}`;
  }
  stageLoading.value[stage as keyof typeof stageLoading.value] = false;
};

const doStopStage = async (stage: number) => {
  result.value = "";
  try {
    if (stage === 1) await stopStage1();
    else if (stage === 2) await stopStage2();
    else if (stage === 3) await stopStage3();
    else if (stage === 4) await stopStage4();
    result.value = `⏹️ 阶段${stage}(${_stageLabel(stage)}) 停止中, 进度已保存`;
  } catch (e: any) {
    result.value = `❌ ${e.message}`;
  }
};

const doStopAll = async () => {
  result.value = "";
  try {
    await stopCollectAll();
    result.value = "⏹️ 全量采集停止中, 各阶段进度已保存";
  } catch (e: any) {
    result.value = `❌ ${e.message}`;
  }
};

const statusTag = (s: string) =>
  ({ pending: "info", done: "success", partial: "warning" })[s] || "info";
const statusText = (s: string) =>
  ({ pending: "未采集", done: "已完成", partial: "部分完成" })[s] || s;

const logColor = (content: string) => {
  if (!content) return "";
  if (content.startsWith("✅")) return "#67c23a";
  if (content.startsWith("❌")) return "#f56c6c";
  if (content.startsWith("⏭️")) return "#e6a23c";
  if (content.match(/^[📦📅📊📎🚀]/)) return "#409eff";
  return "";
};
</script>

<template>
  <div
    style="padding: 24px; display: flex; gap: 16px; height: calc(100vh - 48px)"
  >
    <!-- ====== 左栏：进度 + 42API看板 ====== -->
    <div style="flex: 1; overflow-y: auto; min-width: 0">
      <h2>🔄 Tushare 数据采集管理</h2>
      <p style="color: #909399; margin-bottom: 12px">
        42个API | 2010-01-01 至今 | 200次/分钟限流 |
        <a href="https://tushare.pro" target="_blank" style="color: #409eff"
          >Tushare Pro</a
        >
      </p>

      <!-- 概览卡片 -->
      <el-row :gutter="12" style="margin-bottom: 12px">
        <el-col :span="6">
          <el-card
            ><p style="font-size: 20px; font-weight: bold">
              {{ stats.stock_count || "-" }}
            </p>
            <p style="color: #909399; font-size: 13px">股票总数</p></el-card
          >
        </el-col>
        <el-col :span="6">
          <el-card
            ><p style="font-size: 20px">{{ stats.trade_dates || "-" }}</p>
            <p style="color: #909399; font-size: 13px">交易日数</p></el-card
          >
        </el-col>
        <el-col :span="6">
          <el-card
            ><p style="font-size: 20px">
              {{ ((stats.daily_rows || 0) / 10000).toFixed(1) }}万
            </p>
            <p style="color: #909399; font-size: 13px">日线数据</p></el-card
          >
        </el-col>
        <el-col :span="6">
          <el-card
            ><p style="font-size: 20px">{{ stats.total_tables || "-" }}</p>
            <p style="color: #909399; font-size: 13px">数据表数</p></el-card
          >
        </el-col>
      </el-row>

      <!-- 控制栏 -->
      <el-card style="margin-bottom: 12px">
        <div
          style="
            display: flex;
            gap: 8px;
            align-items: center;
            margin-bottom: 12px;
            flex-wrap: wrap;
          "
        >
          <el-button
            type="primary"
            :loading="loading"
            :disabled="collecting"
            @click="doTriggerCollection"
            >▶ 全量采集</el-button
          >
          <el-button
            type="danger"
            size="small"
            :disabled="!collecting"
            @click="doStopAll"
            >⏹ 停止全量</el-button
          >
          <el-button
            size="small"
            :type="stageLoading[1] ? 'warning' : 'default'"
            :loading="stageLoading[1]"
            :disabled="collecting"
            @click="doCollectStage(1)"
            >阶段1: 基础信息</el-button
          >
          <el-button
            type="danger"
            size="small"
            plain
            :disabled="!collecting"
            @click="doStopStage(1)"
            >⏹</el-button
          >
          <el-button
            size="small"
            :type="stageLoading[2] ? 'warning' : 'default'"
            :loading="stageLoading[2]"
            :disabled="collecting"
            @click="doCollectStage(2)"
            >阶段2: 日频批量</el-button
          >
          <el-button
            type="danger"
            size="small"
            plain
            :disabled="!collecting"
            @click="doStopStage(2)"
            >⏹</el-button
          >
          <el-button
            size="small"
            :type="stageLoading[3] ? 'warning' : 'default'"
            :loading="stageLoading[3]"
            :disabled="collecting"
            @click="doCollectStage(3)"
            >阶段3: 个股循环</el-button
          >
          <el-button
            type="danger"
            size="small"
            plain
            :disabled="!collecting"
            @click="doStopStage(3)"
            >⏹</el-button
          >
          <el-button
            size="small"
            :type="stageLoading[4] ? 'warning' : 'default'"
            :loading="stageLoading[4]"
            :disabled="collecting"
            @click="doCollectStage(4)"
            >阶段4: 补充数据</el-button
          >
          <el-button
            type="danger"
            size="small"
            plain
            :disabled="!collecting"
            @click="doStopStage(4)"
            >⏹</el-button
          >
          <el-button @click="loadData">🔄 刷新</el-button>
          <el-tag
            :type="wsStatus === '已连接' ? 'success' : 'danger'"
            size="small"
            >WS:{{ wsStatus }}</el-tag
          >
          <el-tag
            v-if="pendingStatus.has_pending"
            type="warning"
            size="small"
            effect="dark"
            >🔁 待续采 {{ pendingStatus.total_pending }} 项</el-tag
          >

          <template v-if="progress.status === 'running'">
            <el-tag
              type="warning"
              size="small"
              effect="dark"
              style="font-family: monospace"
              >⏱ {{ fmtTime(progress.elapsed) }}</el-tag
            >
            <el-tag
              type="info"
              size="small"
              effect="plain"
              style="font-family: monospace"
              >📡 {{ progress.api_count.toLocaleString() }}次</el-tag
            >
            <span
              style="color: #409eff; font-size: 13px; font-family: monospace"
              >采集中{{ dots }}</span
            >
          </template>
          <template v-else-if="progress.status === 'done'">
            <el-tag type="success" size="small" effect="dark"
              >✅ 完成 ({{ fmtTime(progress.elapsed) }})</el-tag
            >
          </template>

          <!-- 42API统计 -->
          <template v-if="Object.keys(progress.api_statuses).length > 0">
            <el-tag size="small" type="success" v-if="statusCounts.done"
              >{{ statusCounts.done }}✅</el-tag
            >
            <el-tag size="small" type="primary" v-if="statusCounts.running"
              >{{ statusCounts.running }}🔄</el-tag
            >
            <el-tag size="small" type="info" v-if="statusCounts.pending"
              >{{ statusCounts.pending }}⏳</el-tag
            >
            <el-tag size="small" type="danger" v-if="statusCounts.error"
              >{{ statusCounts.error }}❌</el-tag
            >
          </template>
        </div>
        <p v-if="result" style="font-size: 13px; margin-bottom: 8px">
          {{ result }}
        </p>

        <!-- 总体进度 -->
        <div v-if="progress.stage > 0" style="margin-bottom: 12px">
          <div
            style="
              display: flex;
              justify-content: space-between;
              font-size: 13px;
              margin-bottom: 4px;
            "
          >
            <span style="font-weight: bold">📊 总进度</span>
            <span
              >{{ overallPct }}%<span
                v-if="progress.eta > 0 && progress.status === 'running'"
                style="color: #909399; font-size: 12px"
              >
                · 预计剩余 {{ fmtTime(progress.eta) }}</span
              ></span
            >
          </div>
          <el-progress
            :percentage="overallPct"
            :status="progress.status === 'done' ? 'success' : ''"
            :stroke-width="20"
            :format="() => `${overallPct}%`"
          />
        </div>

        <!-- 当前阶段进度 -->
        <div v-if="progress.stage > 0" style="margin-bottom: 8px">
          <div
            style="
              display: flex;
              justify-content: space-between;
              font-size: 13px;
              margin-bottom: 4px;
            "
          >
            <span
              >阶段{{ progress.stage }}/4:
              <strong>{{ progress.stage_name }}</strong></span
            >
            <span
              >{{ progress.current }}/{{ progress.total }} ({{
                currentPct
              }}%)</span
            >
          </div>
          <el-progress
            :percentage="currentPct"
            :status="progress.status === 'done' ? 'success' : ''"
            :stroke-width="16"
          />
          <div
            v-if="progress.api"
            style="
              font-size: 12px;
              color: #909399;
              margin-top: 4px;
              font-family: monospace;
            "
          >
            ▶ {{ progress.api }}
          </div>
        </div>
      </el-card>

      <!-- ════ 42 API 实时状态看板 ════ -->
      <div
        v-if="Object.keys(progress.api_statuses).length > 0"
        style="margin-bottom: 12px"
      >
        <div
          v-for="g in apisByStage"
          :key="g.stage"
          style="margin-bottom: 12px"
        >
          <div
            style="
              display: flex;
              justify-content: space-between;
              align-items: center;
              margin-bottom: 6px;
            "
          >
            <span style="font-weight: bold; font-size: 14px">
              {{ ["📦", "📅", "📊", "📎"][g.stage - 1] || "📋" }} 阶段{{
                g.stage
              }}: {{ g.name }}
              <el-tag size="small" style="margin-left: 6px"
                >{{ g.apis.length }}个</el-tag
              >
            </span>
          </div>
          <div
            style="
              display: grid;
              grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
              gap: 6px;
            "
          >
            <div
              v-for="api in g.apis"
              :key="api.key"
              :style="{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                padding: '5px 8px',
                borderRadius: '4px',
                fontSize: '12px',
                border: '1px solid #ebeef5',
                background: statusStyle(api.status).bg,
                color: statusStyle(api.status).color,
                transition: 'all 0.3s',
              }"
            >
              <span style="font-size: 14px">{{
                statusStyle(api.status).icon
              }}</span>
              <div style="flex: 1; min-width: 0">
                <div
                  style="
                    font-weight: 500;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                  "
                >
                  {{ api.key }}
                </div>
                <div
                  style="
                    font-size: 11px;
                    opacity: 0.7;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                  "
                >
                  {{ api.desc }}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 无采集时显示静态API表格 -->
      <template v-if="Object.keys(progress.api_statuses).length === 0">
        <el-card
          v-for="g in apiGroups"
          :key="g.group"
          style="margin-bottom: 8px"
        >
          <template #header
            ><div style="display: flex; justify-content: space-between">
              <span>{{ g.group }}</span
              ><el-tag size="small">{{ g.apis.length }}个</el-tag>
            </div></template
          >
          <el-table :data="g.apis" size="small" style="width: 100%">
            <el-table-column prop="name" label="接口" width="150" />
            <el-table-column prop="desc" label="说明" width="140" />
            <el-table-column prop="period" label="频率" width="80" />
            <el-table-column prop="row_count" label="行数" width="100"
              ><template #default="{ row }">{{
                row.row_count > 0 ? row.row_count.toLocaleString() : "-"
              }}</template></el-table-column
            >
            <el-table-column
              prop="date_range"
              label="数据范围"
              min-width="180"
            />
            <el-table-column label="状态" width="100"
              ><template #default="{ row }"
                ><el-tag :type="statusTag(row.status)" size="small">{{
                  statusText(row.status)
                }}</el-tag></template
              ></el-table-column
            >
            <el-table-column label="操作" width="100"
              ><template #default="{ row }"
                ><el-button
                  size="small"
                  :disabled="row.status === 'done' || collecting"
                  @click="doTriggerCollection"
                  >采集</el-button
                ></template
              ></el-table-column
            >
          </el-table>
        </el-card>
      </template>
    </div>

    <!-- ====== 右栏：日志面板 ====== -->
    <div
      style="
        width: 440px;
        display: flex;
        flex-direction: column;
        flex-shrink: 0;
      "
    >
      <el-card style="flex: 1; display: flex; flex-direction: column">
        <template #header>
          <div
            style="
              display: flex;
              justify-content: space-between;
              align-items: center;
            "
          >
            <span
              >📋 实时日志<el-tag
                size="small"
                type="info"
                style="margin-left: 8px"
                >{{ logs.length }}条</el-tag
              ></span
            >
            <el-button size="small" @click="logs = []">清空</el-button>
          </div>
        </template>
        <div
          ref="logContainer"
          style="
            flex: 1;
            overflow-y: auto;
            font-size: 12px;
            font-family:
              &quot;Cascadia Code&quot;, &quot;Fira Code&quot;,
              &quot;JetBrains Mono&quot;, monospace;
            background: #1e1e1e;
            color: #d4d4d4;
            padding: 8px;
            border-radius: 4px;
          "
        >
          <div
            v-if="logs.length === 0"
            style="color: #666; padding: 20px; text-align: center"
          >
            <div v-if="progress.status === 'running'" style="font-size: 14px">
              采集中{{ dots }}
            </div>
            <div v-else>
              点击「开始采集」启动数据入库<br /><span
                style="font-size: 11px; color: #555"
                >预计全量采集耗时 8-15 小时</span
              >
            </div>
          </div>
          <div
            v-for="(log, i) in logs"
            :key="i"
            :style="{
              padding: log.type === 'stage' ? '3px 0' : '1px 0',
              fontWeight: log.type === 'stage' ? 'bold' : 'normal',
            }"
          >
            <span style="color: #888">[{{ log.time }}]</span>
            <span v-if="log.type === 'stage'" style="color: #409eff">{{
              log.content
            }}</span>
            <span v-else :style="{ color: logColor(log.content) }">{{
              log.content
            }}</span>
          </div>
        </div>
      </el-card>
    </div>
  </div>
</template>
