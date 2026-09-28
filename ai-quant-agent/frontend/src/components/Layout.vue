<script setup lang="ts">
import { useRouter, useRoute } from "vue-router";

const router = useRouter();
const route = useRoute();

const navItems = [
  { path: "/daily-picks", icon: "🏆", label: "每日推荐榜单" },
  { path: "/market", icon: "📊", label: "全市场股票" },
  { path: "/backtest", icon: "📈", label: "回测中心" },
  { path: "/agent", icon: "🤖", label: "Agent对话" },
  { path: "/risk", icon: "⚠️", label: "风险监控" },
  { path: "/knowledge", icon: "📚", label: "知识库管理" },
  { path: "/evolution", icon: "🧬", label: "进化中心" },
  { path: "/selfproof", icon: "🧾", label: "自证测试" },
  { path: "/data", icon: "🔄", label: "数据管理" },
  { path: "/settings", icon: "⚙️", label: "系统设置" },
];
</script>

<template>
  <el-container style="height: 100vh">
    <el-aside width="200px" style="background: #304156">
      <div
        style="
          padding: 20px;
          color: #fff;
          font-size: 18px;
          font-weight: bold;
          text-align: center;
          border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        "
      >
        AI Quant Agent
      </div>
      <el-menu
        :default-active="route.path"
        background-color="#304156"
        text-color="#bfcbd9"
        active-text-color="#409eff"
        @select="(index: string) => router.push(index)"
        style="border-right: none"
      >
        <el-menu-item
          v-for="item in navItems"
          :key="item.path"
          :index="item.path"
        >
          <span>{{ item.icon }}</span>
          <span style="margin-left: 8px">{{ item.label }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-main style="background: #f5f7fa; padding: 0">
      <router-view />
    </el-main>
  </el-container>
</template>
