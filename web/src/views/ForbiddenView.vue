<script setup lang="ts">
/**
 * 403 · 无权访问。
 *
 * ★ 为什么单独一个页面，而不是在守卫里直接 `return { name: 'Home' }`：
 *
 *   静默跳回首页会让用户以为「我点的菜单坏了」或者「系统卡了」，
 *   他会反复点同一个入口。明确告诉他「你的账户没有这个权限」，
 *   他才知道该去找学校平台账户（或系统管理员）开通。
 *
 *   这与后端的原则一致：权限不足返回 403 并说明原因，
 *   而不是返回空列表让他以为「这里本来就没数据」。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

const who = computed(() => auth.roleLabel || '当前账户')
</script>

<template>
  <div class="fb">
    <div class="fb__code">403</div>
    <p class="zf-muted">
      {{ who }}没有这个页面的访问权限
    </p>
    <p class="fb__hint">
      教师账户只能访问自己的教学数据；如需管理班级、账号或全校学情，
      请联系学校的<strong>学校平台账户</strong>。
    </p>
    <div class="fb__actions">
      <el-button @click="router.back()">返回上一页</el-button>
      <el-button type="primary" @click="router.push({ name: 'Home' })">
        回到概览
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.fb {
  min-height: 60vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--zf-space-3);
  padding: var(--zf-space-6);
  text-align: center;
}

.fb__code {
  font-size: 48px;
  font-weight: var(--zf-weight-semibold);
  color: var(--zf-text-tertiary);
  font-variant-numeric: tabular-nums;
}

.fb__hint {
  max-width: 460px;
  font-size: var(--zf-font-sm);
  color: var(--zf-text-tertiary);
  line-height: 1.7;
}

.fb__actions {
  display: flex;
  gap: var(--zf-space-2);
}
</style>
