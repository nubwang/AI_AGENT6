import { createRouter, createWebHistory } from "vue-router";
import Layout from "../components/Layout.vue";
import DataManage from "../views/DataManage.vue";

const routes = [
  {
    path: "/",
    component: Layout,
    redirect: "/data",
    children: [
      { path: "data", name: "数据管理", component: DataManage },
      {
        path: "daily-picks",
        name: "每日推荐榜单",
        component: () => import("../views/Recommend.vue"),
      },
      {
        path: "market",
        name: "全市场股票",
        component: () => import("../views/StockList.vue"),
      },
      {
        path: "market/stock/:ts_code",
        name: "股票详情",
        component: () => import("../views/StockDetail.vue"),
      },
      {
        path: "backtest",
        name: "回测中心",
        component: () => import("../views/Backtest.vue"),
      },
      {
        path: "agent",
        name: "进化大脑对话",
        component: () => import("../views/AgentChat.vue"),
      },
      {
        path: "evolution",
        name: "进化中心",
        component: () => import("../views/Evolution.vue"),
      },
      {
        path: "selfproof",
        name: "自证测试",
        component: () => import("../views/SelfProof.vue"),
      },
      {
        path: "risk",
        name: "风险监控",
        component: () => import("../views/Placeholder.vue"),
      },
      {
        path: "knowledge",
        name: "知识库管理",
        component: () => import("../views/Placeholder.vue"),
      },
      {
        path: "settings",
        name: "系统设置",
        component: () => import("../views/Placeholder.vue"),
      },
    ],
  },
];

export default createRouter({
  history: createWebHistory(),
  routes,
});
