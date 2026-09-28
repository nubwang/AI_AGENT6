<script setup lang="ts">
import { ref, computed, watch } from "vue";

const props = defineProps<{ tables: any }>();

const activeModule = ref("");
const pagination = ref<Record<string, { page: number; size: number }>>({});
// 展开的表格(表级折叠, 点击展开才渲染, 避免多张大表同时渲染卡顿)
const expanded = ref<string[]>([]);

const modules = computed(() => props.tables?.modules || []);

// 默认选中第一个有数据的模块
watch(
  modules,
  (m) => {
    if (m.length && !activeModule.value) activeModule.value = m[0].key;
  },
  { immediate: true },
);

const tablesOf = (key: string) =>
  modules.value.find((m: any) => m.key === key)?.tables || [];

const emptyCount = (key: string) =>
  tablesOf(key).filter((t: any) => t.count > 0).length;

const pg = (key: string) => {
  if (!pagination.value[key]) pagination.value[key] = { page: 1, size: 50 };
  return pagination.value[key];
};

const pageRows = (table: any) => {
  const p = pg(table.key);
  return table.rows.slice((p.page - 1) * p.size, p.page * p.size);
};

const fmtCell = (val: any) => {
  if (val === null || val === undefined || val === "") return "—";
  if (typeof val === "number") {
    if (!isFinite(val)) return "—";
    const abs = Math.abs(val);
    if (abs >= 1e8) return (val / 1e8).toFixed(2) + "亿";
    if (abs >= 1e4) return (val / 1e4).toFixed(2) + "万";
    if (Number.isInteger(val)) return val.toLocaleString();
    return Number(val.toFixed(2)).toLocaleString();
  }
  return String(val);
};
</script>

<template>
  <div>
    <el-tabs v-model="activeModule" v-if="modules.length">
      <el-tab-pane
        v-for="m in modules"
        :key="m.key"
        :name="m.key"
        :lazy="true"
        :label="`${m.name}（${emptyCount(m.key)}张）`"
      >
        <template v-if="emptyCount(m.key) > 0">
          <el-collapse v-model="expanded">
            <el-collapse-item
              v-for="t in tablesOf(m.key)"
              :key="t.key"
              :name="t.key"
            >
              <template #title>
                <div
                  style="
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    width: 100%;
                    padding-right: 12px;
                  "
                >
                  <b>{{ t.name }}</b>
                  <el-tag size="small" :type="t.count > 0 ? 'success' : 'info'"
                    >{{ t.count }} 条</el-tag
                  >
                </div>
              </template>
              <template v-if="t.count > 0">
                <el-table
                  v-if="expanded.includes(t.key)"
                  :data="pageRows(t)"
                  size="small"
                  border
                  style="width: 100%"
                  max-height="480"
                >
                  <el-table-column
                    v-for="c in t.columns"
                    :key="c.key"
                    :prop="c.key"
                    :label="c.label"
                    min-width="110"
                    show-overflow-tooltip
                  >
                    <template #default="{ row }">{{
                      fmtCell(row[c.key])
                    }}</template>
                  </el-table-column>
                </el-table>
                <div
                  v-if="expanded.includes(t.key) && t.count > pg(t.key).size"
                  style="
                    margin-top: 10px;
                    display: flex;
                    justify-content: flex-end;
                  "
                >
                  <el-pagination
                    layout="prev, pager, next"
                    :total="t.count"
                    :page-size="pg(t.key).size"
                    :current-page="pg(t.key).page"
                    @current-change="
                      (p: number) => {
                        pg(t.key).page = p;
                      }
                    "
                  />
                </div>
              </template>
              <div
                v-else
                style="
                  color: #909399;
                  padding: 12px;
                  text-align: center;
                  font-size: 13px;
                "
              >
                暂无数据（数据可能尚未采集）
              </div>
            </el-collapse-item>
          </el-collapse>
        </template>
        <div v-else style="color: #909399; padding: 20px; text-align: center">
          该模块暂无数据（数据可能尚未采集）
        </div>
      </el-tab-pane>
    </el-tabs>
    <div v-else style="color: #909399; padding: 20px; text-align: center">
      暂无数据（数据可能尚未采集）
    </div>
  </div>
</template>
