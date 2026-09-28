<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import * as echarts from "echarts";
import { getStockDetail, getStockDaily, getStockTables } from "../api";
import StockTables from "../components/StockTables.vue";

const route = useRoute();
const router = useRouter();

const basic = ref<any>({});
const company = ref<any>({});
const latest = ref<any>({});
const dailyRange = ref<any>(null);
const dailyCount = ref(0);
const tables = ref<any>(null);
const loading = ref(false);

const chartRef = ref<HTMLDivElement | null>(null);
let chart: echarts.ECharts | null = null;

// ── 格式化工具 ──
const fmt = (v: any, digits = 2) =>
  v === null || v === undefined || v === "" ? "—" : Number(v).toFixed(digits);
const fmtBig = (v: any) => {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + "亿";
  if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(2) + "万";
  return n.toFixed(2);
};
const upDownColor = (v: any) => {
  if (v === null || v === undefined) return "#909399";
  return Number(v) >= 0 ? "#f56c6c" : "#67c23a";
};

// ── 数据加载 ──
const load = async () => {
  const ts_code = String(route.params.ts_code);
  loading.value = true;
  // 全部数据表独立加载: 失败仅隐藏该模块, 不影响 K线/基本信息/最新行情 展示
  getStockTables(ts_code)
    .then((res) => {
      tables.value = res.data || null;
    })
    .catch(() => {
      tables.value = null;
    });
  try {
    const [detailRes, dailyRes] = await Promise.all([
      getStockDetail(ts_code),
      getStockDaily(ts_code, { limit: 600 }),
    ]);
    const detail = detailRes.data;
    if (detail.error) {
      basic.value = {};
      dailyCount.value = 0;
      renderChart([]);
      return;
    }
    basic.value = detail.basic || {};
    company.value = detail.company || {};
    latest.value = detail.latest || {};
    dailyRange.value = detail.daily_range || null;
    const data = dailyRes.data.data || [];
    dailyCount.value = data.length;
    renderChart(data);
  } catch (e) {
    basic.value = {};
    dailyCount.value = 0;
    renderChart([]);
  } finally {
    loading.value = false;
  }
};

// ── K线图渲染 ──
const ma = (closes: number[], n: number) =>
  closes.map((_, i) => {
    if (i < n - 1) return "-";
    const sum = closes.slice(i - n + 1, i + 1).reduce((a, b) => a + b, 0);
    return +(sum / n).toFixed(2);
  });

const renderChart = (data: any[]) => {
  if (!chartRef.value) return;
  if (!chart) chart = echarts.init(chartRef.value);
  if (!data.length) {
    chart.clear();
    chart.setOption({
      title: {
        text: "暂无日线数据（数据采集中）",
        left: "center",
        top: "middle",
        textStyle: { color: "#909399", fontSize: 14 },
      },
    });
    return;
  }
  const dates = data.map((d) => d.trade_date);
  const closes = data.map((d) => Number(d.close));
  const opens = data.map((d) => Number(d.open));
  const kline = data.map((d) => [
    Number(d.open),
    Number(d.close),
    Number(d.low),
    Number(d.high),
  ]);
  const vols = data.map((d) => Number(d.vol));

  chart.setOption(
    {
      animation: false,
      axisPointer: {
        link: [{ xAxisIndex: "all" }],
        label: { backgroundColor: "#777" },
      },
      tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
      legend: { data: ["日K", "MA5", "MA10", "MA20"] },
      grid: [
        { left: 60, right: 20, top: 40, height: "52%" },
        { left: 60, right: 20, top: "70%", height: "16%" },
      ],
      xAxis: [
        {
          type: "category",
          data: dates,
          boundaryGap: true,
          axisLine: { onZero: false },
        },
        {
          type: "category",
          gridIndex: 1,
          data: dates,
          axisLabel: { show: false },
          axisTick: { show: false },
        },
      ],
      yAxis: [
        { scale: true, splitArea: { show: true } },
        {
          gridIndex: 1,
          splitNumber: 2,
          axisLabel: { show: false },
          axisLine: { show: false },
          splitLine: { show: false },
        },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1], start: 55, end: 100 },
        { type: "slider", xAxisIndex: [0, 1], top: "88%", start: 55, end: 100 },
      ],
      series: [
        {
          name: "日K",
          type: "candlestick",
          data: kline,
          itemStyle: {
            color: "#ef232a",
            color0: "#14b143",
            borderColor: "#ef232a",
            borderColor0: "#14b143",
          },
        },
        {
          name: "MA5",
          type: "line",
          data: ma(closes, 5),
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1 },
        },
        {
          name: "MA10",
          type: "line",
          data: ma(closes, 10),
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1 },
        },
        {
          name: "MA20",
          type: "line",
          data: ma(closes, 20),
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1 },
        },
        {
          name: "成交量",
          type: "bar",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: vols.map((v, i) => ({
            value: v,
            itemStyle: { color: closes[i] >= opens[i] ? "#ef232a" : "#14b143" },
          })),
        },
      ],
    },
    true,
  );
};

const resize = () => chart && chart.resize();

watch(() => route.params.ts_code, load);
onMounted(() => {
  load();
  window.addEventListener("resize", resize);
});
onBeforeUnmount(() => {
  window.removeEventListener("resize", resize);
  if (chart) {
    chart.dispose();
    chart = null;
  }
});
</script>

<template>
  <div style="padding: 24px">
    <el-page-header
      @back="router.back()"
      :content="`${basic.name || ''} (${basic.ts_code || ''})`"
    />

    <!-- K线图 -->
    <el-card v-loading="loading" style="margin-top: 16px">
      <template #header>
        <div
          style="
            display: flex;
            justify-content: space-between;
            align-items: center;
          "
        >
          <b>日线 K 线</b>
          <span style="color: #909399; font-size: 12px">
            {{ dailyCount }} 条数据
            <template v-if="dailyRange">
              | {{ dailyRange.min }} ~ {{ dailyRange.max }}</template
            >
          </span>
        </div>
      </template>
      <div ref="chartRef" style="height: 460px; width: 100%" />
    </el-card>

    <!-- 最新行情 / 估值 -->
    <el-card style="margin-top: 16px">
      <template #header><b>最新行情</b></template>
      <el-descriptions :column="5" border size="small">
        <el-descriptions-item label="日期">{{
          latest.trade_date || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="收盘">{{
          fmt(latest.close)
        }}</el-descriptions-item>
        <el-descriptions-item label="涨跌幅">
          <span :style="{ color: upDownColor(latest.pct_chg) }">{{
            latest.pct_chg === undefined || latest.pct_chg === null
              ? "—"
              : fmt(latest.pct_chg) + "%"
          }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="成交量"
          >{{ fmtBig(latest.vol) }}手</el-descriptions-item
        >
        <el-descriptions-item label="成交额"
          >{{ fmtBig(latest.amount) }}元</el-descriptions-item
        >
        <el-descriptions-item label="PE(TTM)">{{
          fmt(latest.pe_ttm)
        }}</el-descriptions-item>
        <el-descriptions-item label="PB">{{
          fmt(latest.pb)
        }}</el-descriptions-item>
        <el-descriptions-item label="换手率">{{
          latest.turnover_rate === null || latest.turnover_rate === undefined
            ? "—"
            : fmt(latest.turnover_rate) + "%"
        }}</el-descriptions-item>
        <el-descriptions-item label="总市值">{{
          fmtBig(latest.total_mv)
        }}</el-descriptions-item>
        <el-descriptions-item label="流通市值">{{
          fmtBig(latest.circ_mv)
        }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 基本信息 -->
    <el-card style="margin-top: 16px">
      <template #header><b>基本信息</b></template>
      <el-descriptions :column="4" border size="small">
        <el-descriptions-item label="代码">{{
          basic.ts_code || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="名称">{{
          basic.name || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="拼音">{{
          basic.cnspell || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="市场">{{
          basic.market || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="行业">{{
          basic.industry || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="地区">{{
          basic.area || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="上市日期">{{
          basic.list_date || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="实控人">{{
          basic.act_name || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="实控人类型">{{
          basic.act_ent_type || "—"
        }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 公司信息 -->
    <el-card style="margin-top: 16px" v-if="Object.keys(company).length > 0">
      <template #header><b>公司信息</b></template>
      <el-descriptions :column="3" border size="small">
        <el-descriptions-item label="公司名称">{{
          company.com_name || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="董事长">{{
          company.chairman || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="总经理">{{
          company.manager || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="董秘">{{
          company.secretary || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="注册资本(万)">{{
          company.reg_capital === null || company.reg_capital === undefined
            ? "—"
            : fmtBig(company.reg_capital)
        }}</el-descriptions-item>
        <el-descriptions-item label="员工数">{{
          company.employees ?? "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="省份">{{
          company.province || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="城市">{{
          company.city || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="交易所">{{
          company.exchange || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="官网" :span="3">
          <a
            v-if="company.website"
            :href="company.website"
            target="_blank"
            style="color: #409eff"
            >{{ company.website }}</a
          >
          <span v-else>—</span>
        </el-descriptions-item>
        <el-descriptions-item label="主营业务" :span="3">{{
          company.main_business || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="经营范围" :span="3">{{
          company.business_scope || "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="公司简介" :span="3">{{
          company.introduction || "—"
        }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 全部数据表 -->
    <el-card style="margin-top: 16px" v-if="tables && tables.modules">
      <template #header><b>📋 全部数据表</b></template>
      <StockTables :tables="tables" />
    </el-card>
  </div>
</template>
