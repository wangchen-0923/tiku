<script setup lang="ts">
import { useRouter } from 'vue-router'

const router = useRouter()
</script>

<template>
  <div class="nf">
    <div class="nf__code">404</div>
    <p class="zf-muted">这个页面不存在</p>
    <el-button type="primary" @click="router.push({ name: 'Home' })">
      回到概览
    </el-button>
  </div>
</template>

<style scoped>
.nf {
  min-height: 60vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--zf-space-3);
}

.nf__code {
  font-size: 48px;
  font-weight: var(--zf-weight-semibold);
  color: var(--zf-text-tertiary);
  font-variant-numeric: tabular-nums;
}
</style>

