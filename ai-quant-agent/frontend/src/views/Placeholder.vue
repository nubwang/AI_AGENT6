<script setup lang="ts">
import { useRoute } from "vue-router";
const route = useRoute();
</script>

<template>
  <div style="padding: 24px">
    <h2>{{ route.name }}</h2>
    <p style="color: #909399">功能开发中...</p>
  </div>
</template>
