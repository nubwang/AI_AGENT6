<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useRouter } from "vue-router";
import { getStocks } from "../api";

const router = useRouter();
const loading = ref(false);
const items = ref<any[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = ref(20);
const keyword = ref("");
const market = ref("");

const markets = ["主板", "创业板", "科创板", "北交所"];

const load = async () => {
  loading.value = true;
  try {
    const { data } = await getStocks({
      page: page.value,
      page_size: pageSize.value,
      keyword: keyword.value || undefined,
      market: market.value || undefined,
    });
    items.value = data.items;
    total.value = data.total;
  } catch (e: any) {
    items.value = [];
    total.value = 0;
  } finally {
    loading.value = false;
  }
};

const goDetail = (row: any) => {
  router.push(`/market/stock/${row.ts_code}`);
};

const fmt = (v: any, digits = 2) =>
  v === null || v === undefined || v === "" ? "—" : Number(v).toFixed(digits);
const upDownColor = (v: any) => {
  if (v === null || v === undefined) return "#909399";
  return Number(v) >= 0 ? "#f56c6c" : "#67c23a";
};

onMounted(load);
</script>

<template>
  <div style="padding: 24px">
    <h2>📊 全市场股票</h2>
    <p style="color: #909399; margin-bottom: 16px">
      共 {{ total }} 只股票，点击任意行查看详情（日线 K 线 + 全部信息）
    </p>

    <el-card>
      <!-- 搜索栏 -->
      <div
        style="
          display: flex;
          gap: 12px;
          margin-bottom: 16px;
          align-items: center;
        "
      >
        <el-input
          v-model="keyword"
          placeholder="搜索代码 / 名称"
          clearable
          style="width: 240px"
          @keyup.enter="
            page = 1;
            load();
          "
          @clear="
            page = 1;
            load();
          "
        />
        <el-select
          v-model="market"
          placeholder="市场"
          clearable
          style="width: 130px"
          @change="
            page = 1;
            load();
          "
        >
          <el-option v-for="m in markets" :key="m" :label="m" :value="m" />
        </el-select>
        <el-button
          type="primary"
          @click="
            page = 1;
            load();
          "
          >查询</el-button
        >
        <el-tag type="info">数据: {{ total }} 只</el-tag>
      </div>

      <!-- 表格 -->
      <el-table
        :data="items"
        v-loading="loading"
        style="width: 100%; cursor: pointer"
        @row-click="goDetail"
      >
        <el-table-column prop="ts_code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" width="120" />
        <el-table-column prop="industry" label="行业" min-width="110" />
        <el-table-column prop="market" label="市场" width="90" />
        <el-table-column prop="area" label="地区" width="100" />
        <el-table-column prop="list_date" label="上市日期" width="110" />
        <el-table-column label="最新收盘" width="100" align="right">
          <template #default="{ row }">{{ fmt(row.latest_close) }}</template>
        </el-table-column>
        <el-table-column label="涨跌幅" width="100" align="right">
          <template #default="{ row }">
            <span :style="{ color: upDownColor(row.latest_pct_chg) }">
              {{
                row.latest_pct_chg === null || row.latest_pct_chg === undefined
                  ? "—"
                  : fmt(row.latest_pct_chg) + "%"
              }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80" align="center">
          <template #default="{ row }">
            <el-button
              type="primary"
              size="small"
              link
              @click.stop="goDetail(row)"
            >
              详情
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页: 每页固定20条, 按需加载(翻页才请求后端) -->
      <div style="margin-top: 16px; display: flex; justify-content: flex-end">
        <el-pagination
          layout="total, prev, pager, next"
          :total="total"
          :page-size="20"
          :current-page="page"
          @current-change="
            (p: number) => {
              page = p;
              load();
            }
          "
        />
      </div>
    </el-card>
  </div>
</template>
