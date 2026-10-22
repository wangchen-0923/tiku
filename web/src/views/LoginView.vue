<script setup lang="ts">
/**
 * 登录页。
 *
 * ★★ 一个刻意的取舍：**不区分「用户不存在」与「密码错误」**。
 *
 *   后端对两种情况返回同一个 `AUTH.INVALID_CREDENTIALS` 与同一句
 *   提示，这是防止账号枚举的安全设计。前端若自作聪明地按
 *   HTTP 状态或返回码去区分，就把这个防护绕过去了。
 *   所以这里只把后端给的 message 原样显示。
 *
 * ★ 登录成功后跳回 `?redirect=` 指向的地址（路由守卫记下的）。
 *   不这么做的话，用户点了一个深链接、被踢去登录、登录完
 *   回到首页——原来要看的东西还得再找一遍。
 */
import { ElMessage } from 'element-plus'
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const form = reactive({ username: '', password: '' })
const submitting = ref(false)

async function handleSubmit(): Promise<void> {
  if (!form.username || !form.password) {
    ElMessage.warning('请输入用户名与密码')
    return
  }
  submitting.value = true
  try {
    await auth.login(form.username, form.password)
    ElMessage.success('登录成功')
    const redirect = route.query.redirect
    await router.push(
      typeof redirect === 'string' && redirect ? redirect : { name: 'Home' },
    )
  } catch {
    // ★ 错误文案来自 store（= 后端原文），这里只负责展示
    ElMessage.error(auth.error || '登录失败')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <section class="zf-card login-card">
      <div class="zf-card__body">
        <h1 class="login-title">知否</h1>
        <p class="zf-muted login-sub">
          中等职业学校 · 机械制造技术题库与精准作业系统
        </p>

        <el-form label-position="top" @submit.prevent="handleSubmit">
          <el-form-item label="用户名">
            <el-input
              v-model="form.username"
              size="large"
              placeholder="请输入用户名"
              autocomplete="username"
              @keyup.enter="handleSubmit"
            />
          </el-form-item>
          <el-form-item label="密码">
            <el-input
              v-model="form.password"
              type="password"
              size="large"
              placeholder="请输入密码"
              show-password
              autocomplete="current-password"
              @keyup.enter="handleSubmit"
            />
          </el-form-item>
          <el-button
            type="primary"
            size="large"
            style="width: 100%"
            :loading="submitting"
            @click="handleSubmit"
          >
            登录
          </el-button>
        </el-form>

        <el-alert
          v-if="auth.error"
          type="error"
          :closable="false"
          show-icon
          style="margin-top: var(--zf-space-4)"
          :title="auth.error"
        />
      </div>
    </section>

    <p class="zf-dim login-foot">
      本地开发环境 · 后端 127.0.0.1:8001
    </p>
  </div>
</template>

<style scoped>
.login-wrap {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--zf-space-4);
  padding: var(--zf-space-6);
  background: var(--zf-bg-page);
}

.login-card {
  width: 100%;
  max-width: 380px;
}

.login-title {
  margin: 0 0 var(--zf-space-1);
  font-size: var(--zf-text-2xl, 24px);
  font-weight: var(--zf-weight-semibold);
  color: var(--zf-text-primary);
  letter-spacing: 0.04em;
  text-align: center;
}

.login-sub {
  margin: 0 0 var(--zf-space-6);
  font-size: var(--zf-text-xs);
  text-align: center;
}

.login-foot {
  font-size: var(--zf-text-xs);
}
</style>
