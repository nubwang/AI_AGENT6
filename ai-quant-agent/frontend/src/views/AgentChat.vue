<script setup lang="ts">
import { ref, computed, nextTick, onMounted } from "vue";
import { ElMessage } from "element-plus";
import {
  askAgent,
  getEvolveCodeStatus,
  triggerEvolveCodePropose,
  applyEvolveCode,
  rollbackEvolveCode,
} from "../api";

// ── 聊天数据 ─────────────────────────────
const messages = ref<any[]>([]);
const question = ref("");
const sending = ref(false);
const proposing = ref(false);
const codeLoading = ref(false);
const codeStatus = ref<any>({});
const lastSuggest = ref<any>(null);
const msgArea = ref<HTMLElement | null>(null);

const codePending = computed(
  () => codeStatus.value?.state?.pending_patches || [],
);
const codeHistory = computed(() => codeStatus.value?.state?.history || []);

const quick = [
  "为什么昨天推荐的股票没涨？",
  "帮我优化形态识别",
  "检查回测和每日推荐是否脱节",
  "怎么提高次日上涨概率",
];

async function scrollBottom() {
  await nextTick();
  if (msgArea.value) msgArea.value.scrollTop = msgArea.value.scrollHeight;
}

async function loadCodeStatus() {
  codeLoading.value = true;
  try {
    const r = await getEvolveCodeStatus();
    codeStatus.value = r.data;
  } catch (e: any) {
    ElMessage.error(e?.message || "刷新提案失败");
  } finally {
    codeLoading.value = false;
  }
}

// ── 对话 ─────────────────────────────────
async function send() {
  const q = question.value.trim();
  if (!q || sending.value) return;
  messages.value.push({ role: "user", content: q });
  question.value = "";
  await scrollBottom();
  sending.value = true;
  try {
    const r = await askAgent(q);
    const reply = r.data || {};
    messages.value.push({
      role: "agent",
      content: reply.answer || "（无回复）",
    });
    lastSuggest.value = reply;
    // ZOO CODE 式：进化大脑已自动生成代码提案 → 自动刷新展示提案卡片，你只需确认应用
    if (reply.proposal_generated) {
      ElMessage.success(
        "💡 进化大脑已自动生成代码提案，请在下方确认后点“应用”",
      );
      await loadCodeStatus();
    } else if (reply.suggest_code_change) {
      await loadCodeStatus();
    }
  } catch (e: any) {
    messages.value.push({
      role: "agent",
      content:
        "⚠️ 进化大脑连接失败：" +
        (e?.response?.data?.detail || e?.message || ""),
    });
  } finally {
    sending.value = false;
    await scrollBottom();
  }
}

function quickAsk(q: string) {
  question.value = q;
  send();
}

// ── 对话式改代码 ─────────────────────────
async function proposeCode() {
  proposing.value = true;
  try {
    const r = await triggerEvolveCodePropose("manual");
    const d = r.data || {};
    if (d.ok === false) ElMessage.error(d.message || "生成提案失败");
    else ElMessage.success(d.message || "提案已生成");
    await loadCodeStatus();
  } catch (e: any) {
    ElMessage.error(e?.message || "生成提案失败");
  } finally {
    proposing.value = false;
  }
}

async function apply(id: number) {
  try {
    const r = await applyEvolveCode(id);
    const d = r.data || {};
    if (d.ok === false) ElMessage.error(d.message || "应用失败");
    else ElMessage.success(d.message || "已应用（待重启生效）");
    await loadCodeStatus();
  } catch (e: any) {
    ElMessage.error(e?.message || "应用失败");
  }
}

async function rollback(index = 1) {
  try {
    const r = await rollbackEvolveCode(index);
    ElMessage.success(r.data?.message || "已回滚");
    await loadCodeStatus();
  } catch (e: any) {
    ElMessage.error(e?.message || "回滚失败");
  }
}

onMounted(loadCodeStatus);
</script>

<template>
  <div class="agent-chat">
    <el-card shadow="never">
      <template #header>
        <div
          style="
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
          "
        >
          <span>🤖 进化大脑对话（像 ZOO CODE 一样：对话后修改代码）</span>
          <el-button
            size="small"
            :loading="codeLoading"
            @click="loadCodeStatus"
          >
            刷新提案
          </el-button>
        </div>
      </template>

      <!-- 消息区 -->
      <div
        ref="msgArea"
        class="msg-area"
        style="
          height: 46vh;
          overflow-y: auto;
          background: #f5f7fa;
          border-radius: 8px;
          padding: 12px;
        "
      >
        <el-empty
          v-if="!messages.length"
          description="向进化大脑提问，例如：为什么昨天推荐的股票没涨？或：帮我优化形态识别"
          :image-size="80"
        />
        <div
          v-for="(m, i) in messages"
          :key="i"
          class="msg-row"
          :style="{
            display: 'flex',
            margin: '10px 0',
            flexDirection: m.role === 'user' ? 'row-reverse' : 'row',
            alignItems: 'flex-start',
          }"
        >
          <div
            class="avatar"
            style="
              width: 30px;
              height: 30px;
              border-radius: 50%;
              display: flex;
              align-items: center;
              justify-content: center;
              flex-shrink: 0;
              font-size: 14px;
            "
            :style="
              m.role === 'user'
                ? 'background:#409eff;color:#fff'
                : 'background:#fff;border:1px solid #e4e7ed'
            "
          >
            {{ m.role === "user" ? "我" : "🧠" }}
          </div>
          <div
            class="bubble"
            style="
              max-width: 72%;
              margin: 0 10px;
              padding: 10px 14px;
              border-radius: 10px;
              white-space: pre-wrap;
              word-break: break-word;
              font-size: 14px;
              line-height: 1.6;
            "
            :style="
              m.role === 'user'
                ? 'background:#409eff;color:#fff'
                : 'background:#fff;border:1px solid #e4e7ed;color:#303133'
            "
          >
            {{ m.content }}
          </div>
        </div>
      </div>

      <!-- 建议改代码 + 生成提案 -->
      <div
        v-if="lastSuggest && lastSuggest.suggest_code_change"
        style="margin: 12px 0"
      >
        <el-alert
          type="warning"
          :closable="false"
          show-icon
          :title="
            '💡 进化大脑建议改代码：' + (lastSuggest.code_target || '待定')
          "
          description="点击下方按钮让进化大脑生成代码提案（含备份/编译/回滚护栏，你确认后才应用）"
        />
        <el-button
          type="warning"
          style="margin-top: 8px"
          :loading="proposing"
          @click="proposeCode"
        >
          让进化大脑生成代码提案
        </el-button>
      </div>

      <!-- 待确认代码提案 -->
      <div v-if="codePending.length">
        <el-divider content-position="left">
          📋 待确认代码提案（确认后应用，改坏可回滚）
        </el-divider>
        <el-table :data="codePending" size="small">
          <el-table-column label="提案内容" min-width="210">
            <template #default="{ row }">
              <template v-if="row.mode === 'param'">
                <el-tag size="small" type="warning">参数</el-tag>
                <span style="margin-left: 4px"
                  >{{ row.param }} = <b>{{ row.value }}</b></span
                >
              </template>
              <template v-else>{{ row.file }}</template>
            </template>
          </el-table-column>
          <el-table-column label="改动理由" prop="reason" min-width="180" />
          <el-table-column label="证据" prop="evidence" min-width="160" />
          <el-table-column label="操作" width="90">
            <template #default="{ row }">
              <el-button size="small" type="primary" @click="apply(row.id)">
                应用
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 历史/回滚 -->
      <div v-if="codeHistory.length" style="margin-top: 10px">
        <el-divider content-position="left">🕘 进化改动历史</el-divider>
        <div style="display: flex; flex-wrap: wrap; gap: 6px">
          <el-tag
            v-for="(h, i) in codeHistory.slice(-6).reverse()"
            :key="i"
            :type="
              h.status === 'applied'
                ? 'success'
                : h.status === 'rolled_back'
                  ? 'info'
                  : 'danger'
            "
            effect="plain"
            size="small"
          >
            {{ h.ts?.slice(5, 16) }} {{ h.file?.split("/").pop() }}
            {{ h.status }}
          </el-tag>
        </div>
        <el-button size="small" style="margin-top: 6px" @click="rollback(1)">
          回滚最近一次进化改动
        </el-button>
      </div>

      <!-- 输入区 -->
      <div class="input-area" style="margin-top: 12px">
        <el-input
          v-model="question"
          type="textarea"
          :rows="2"
          placeholder="问进化大脑...（例如：为什么最近推荐命中率下降？帮我优化负向抑制参数）Ctrl+Enter 发送"
          @keydown.ctrl.enter="send"
        />
        <div style="display: flex; justify-content: flex-end; margin-top: 8px">
          <el-button type="primary" :loading="sending" @click="send">
            发送
          </el-button>
        </div>
      </div>

      <!-- 快捷指令 -->
      <div style="margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px">
        <el-tag
          v-for="q in quick"
          :key="q"
          size="small"
          style="cursor: pointer"
          @click="quickAsk(q)"
        >
          {{ q }}
        </el-tag>
      </div>
    </el-card>
  </div>
</template>
