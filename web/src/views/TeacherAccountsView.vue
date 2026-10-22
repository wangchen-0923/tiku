<script setup lang="ts">
/**
 * 教师账号（`/settings/teachers`）。
 *
 * ★★ 为什么这一页是学校平台账户的**主界面**：
 *
 *   2026-10-21 规则改版：学校不统一建班级，只授权教师账号；
 *   班级由教师自己建，教师只看自己的数据。
 *
 *   于是学校的职责被收窄成一句话 —— **给老师开账号**。
 *   这一页就是那句话的界面。没有它，新规则下的学校账户
 *   一个动作都做不了（班级页他虽然能看到，但不该由他建）。
 *
 * ★★ 页面上四件事，对应教师生命周期的四个节点：
 *
 *   1. 入职  → 新建账号（用户名 + 初始密码 + 姓名）
 *   2. 在岗  → 改姓名/手机、重置密码
 *   3. 调岗  → 改角色（教师 ↔ 学校平台账户，受等级限制）
 *   4. 离职  → **先移交资产，再停用**（顺序不能反）
 *
 *   ★ 第 4 条的顺序是刻意的：先停用会让 `transfer_assets` 找不到
 *     源账号（它只认 `deleted_at IS NULL` 的行），只能先恢复再移交。
 *     界面上的按钮排列就是按这个顺序放的。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import * as A from '@/api/accounts'
import { ApiError } from '@/api/http'

const users = ref<A.AccountUser[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const keyword = ref('')
const onlyActive = ref(true)
const page = ref(1)
const pageSize = 20

// ── 新建 / 编辑 ──
const formVisible = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const editing = ref<A.AccountUser | null>(null)
const saving = ref(false)
const form = reactive({
  username: '',
  password: '',
  real_name: '',
  phone: '',
  role: 'TEACHER',
})

// ── 重置密码 ──
const pwdVisible = ref(false)
const pwdTarget = ref<A.AccountUser | null>(null)
const pwdValue = ref('')

// ── 移交 ──
const transferVisible = ref(false)
const transferSource = ref<A.AccountUser | null>(null)
const transferTo = ref<number | null>(null)
const transferReason = ref('')

/** 接收人候选：同列表里的其他在岗账号（学生身份行不在列表里） */
const transferCandidates = computed(() =>
  users.value.filter(
    (u) => u.is_active !== false && u.id !== transferSource.value?.id,
  ),
)

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.message
  return e instanceof Error ? e.message : '请求失败'
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const r = await A.listAccounts({
      keyword: keyword.value.trim(),
      isActive: onlyActive.value ? true : undefined,
      page: page.value,
      pageSize,
    })
    // ★ 信封字段是 `items` / `total`（`ZfJSONRenderer` 包装过的），
    //   不是 DRF 默认的 `results` / `count`。
    users.value = r.items
    total.value = r.total
  } catch (e) {
    error.value = errMsg(e)
  } finally {
    loading.value = false
  }
}

function search(): void {
  page.value = 1
  void load()
}

function onPage(p: number): void {
  page.value = p
  void load()
}

function openCreate(): void {
  formMode.value = 'create'
  editing.value = null
  Object.assign(form, {
    username: '', password: '', real_name: '', phone: '', role: 'TEACHER',
  })
  formVisible.value = true
}

function openEdit(u: A.AccountUser): void {
  formMode.value = 'edit'
  editing.value = u
  Object.assign(form, {
    username: u.username,
    password: '',
    real_name: u.real_name,
    phone: u.phone,
    role: u.role,
  })
  formVisible.value = true
}

async function submit(): Promise<void> {
  if (formMode.value === 'create') {
    if (!form.username.trim() || form.password.length < 8) {
      ElMessage.warning('用户名必填，密码至少 8 位')
      return
    }
  }
  saving.value = true
  try {
    if (formMode.value === 'create') {
      await A.createAccount({
        username: form.username.trim(),
        password: form.password,
        real_name: form.real_name.trim(),
        phone: form.phone.trim(),
        role: form.role,
      })
      ElMessage.success('账号已创建')
    } else if (editing.value) {
      // ★ 用户名不可改：它是登录凭据，改它等于换一个人。
      await A.updateAccount(editing.value.id, {
        real_name: form.real_name.trim(),
        phone: form.phone.trim(),
      })
      ElMessage.success('已保存')
    }
    formVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    saving.value = false
  }
}

function openReset(u: A.AccountUser): void {
  pwdTarget.value = u
  pwdValue.value = ''
  pwdVisible.value = true
}

async function submitReset(): Promise<void> {
  if (!pwdTarget.value) return
  if (pwdValue.value.length < 8) {
    ElMessage.warning('密码至少 8 位')
    return
  }
  try {
    await A.resetPassword(pwdTarget.value.id, pwdValue.value)
    pwdVisible.value = false
    ElMessage.success('密码已重置，请当面告知该教师')
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function disable(u: A.AccountUser): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt(
      `停用后「${u.real_name || u.username}」无法登录，已出的题、卷、班级仍在库里。\n`
        + '建议先「移交」再停用。',
      '停用账号',
      { inputPlaceholder: '停用原因（留空也可以）', confirmButtonText: '停用' },
    )
    await A.disableAccount(u.id, value || '')
    ElMessage.success('账号已停用')
    await load()
  } catch (e) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(errMsg(e))
  }
}

function openTransfer(u: A.AccountUser): void {
  transferSource.value = u
  transferTo.value = null
  transferReason.value = ''
  transferVisible.value = true
}

async function submitTransfer(): Promise<void> {
  if (!transferSource.value || !transferTo.value) {
    ElMessage.warning('请选择接收人')
    return
  }
  try {
    const r = await A.transferAssets(
      transferSource.value.id, transferTo.value, transferReason.value,
    )
    transferVisible.value = false
    ElMessage.success(`已移交 ${r.total} 项资产`)
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

onMounted(load)
</script>

<template>
  <div class="ta">
    <header class="zf-page-head">
      <div>
        <h1 class="zf-page-title">教师账号</h1>
        <p class="zf-page-desc">
          学校只负责<b>授权账号</b>；班级、试卷、题目由教师自己建，
          教师之间互不可见。离职时请<b>先移交、再停用</b>。
        </p>
      </div>
      <div class="ta__acts">
        <el-button @click="load">刷新</el-button>
        <el-button type="primary" @click="openCreate">新建教师账号</el-button>
      </div>
    </header>

    <el-alert
      v-if="error"
      type="error"
      :title="error"
      show-icon
      :closable="false"
      style="margin-bottom: 12px"
    />

    <section class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">账号</h2>
        <span class="zf-dim">{{ total }} 个</span>
      </div>
      <div class="zf-card__body">
        <div class="ta__filters">
          <el-input
            v-model="keyword"
            size="small"
            placeholder="搜用户名 / 姓名 / 手机号"
            clearable
            style="max-width: 280px"
            @keyup.enter="search"
            @clear="search"
          />
          <el-checkbox v-model="onlyActive" @change="search">
            只看在岗
          </el-checkbox>
          <el-button size="small" @click="search">搜索</el-button>
        </div>

        <el-table
          v-loading="loading"
          :data="users"
          size="small"
          empty-text="还没有教师账号 —— 点右上角「新建教师账号」开始"
          style="width: 100%"
        >
          <el-table-column prop="username" label="用户名" width="150" />
          <el-table-column label="姓名" width="140">
            <template #default="{ row }">
              {{ row.real_name || '—' }}
            </template>
          </el-table-column>
          <el-table-column label="角色" width="130">
            <template #default="{ row }">
              <el-tag
                size="small"
                :type="row.role === 'TEACHER' ? 'info' : 'warning'"
              >
                {{ row.role_label || row.role }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="手机号" width="140">
            <template #default="{ row }">{{ row.phone || '—' }}</template>
          </el-table-column>
          <el-table-column label="操作" min-width="260">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openEdit(row)">
                编辑
              </el-button>
              <el-button link type="primary" size="small" @click="openReset(row)">
                重置密码
              </el-button>
              <el-button
                link
                type="warning"
                size="small"
                @click="openTransfer(row)"
              >
                移交
              </el-button>
              <el-button link type="danger" size="small" @click="disable(row)">
                停用
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <el-pagination
          v-if="total > pageSize"
          layout="prev, pager, next"
          :total="total"
          :page-size="pageSize"
          :current-page="page"
          style="margin-top: 12px"
          @current-change="onPage"
        />
      </div>
    </section>

    <!-- ══════════ 新建 / 编辑 ══════════ -->
    <el-dialog
      v-model="formVisible"
      :title="formMode === 'create' ? '新建教师账号' : '编辑账号'"
      width="440px"
    >
      <el-form label-width="88px" size="small">
        <el-form-item label="用户名" required>
          <el-input
            v-model="form.username"
            :disabled="formMode === 'edit'"
            placeholder="登录用，如 zhanglaoshi"
          />
          <div v-if="formMode === 'edit'" class="ta__hint">
            用户名不可修改（它是登录凭据，改它等于换一个人）
          </div>
        </el-form-item>
        <el-form-item v-if="formMode === 'create'" label="初始密码" required>
          <el-input
            v-model="form.password"
            type="password"
            show-password
            placeholder="至少 8 位"
          />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="form.real_name" placeholder="如 张伟" />
        </el-form-item>
        <el-form-item label="手机号">
          <el-input v-model="form.phone" placeholder="选填" />
        </el-form-item>
        <el-form-item v-if="formMode === 'create'" label="角色">
          <el-select v-model="form.role" style="width: 100%">
            <el-option label="教师账户" value="TEACHER" />
          </el-select>
          <div class="ta__hint">
            学校平台账户只能创建教师；建同级账号请找系统管理员
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">
          保存
        </el-button>
      </template>
    </el-dialog>

    <!-- ══════════ 重置密码 ══════════ -->
    <el-dialog v-model="pwdVisible" title="重置密码" width="400px">
      <p class="ta__hint" style="margin-top: 0">
        为「{{ pwdTarget?.real_name || pwdTarget?.username }}」设定新密码，
        设定后请当面告知。
      </p>
      <el-input
        v-model="pwdValue"
        type="password"
        show-password
        placeholder="至少 8 位"
      />
      <template #footer>
        <el-button @click="pwdVisible = false">取消</el-button>
        <el-button type="primary" @click="submitReset">确定</el-button>
      </template>
    </el-dialog>

    <!-- ══════════ 移交 ══════════ -->
    <el-dialog v-model="transferVisible" title="移交教学资产" width="440px">
      <p class="ta__hint" style="margin-top: 0">
        把「{{ transferSource?.real_name || transferSource?.username }}」
        建的班级、试卷、题目、知识条目、作业、答题卡，
        整体转给另一位教师。<b>只改归属，不动成绩与状态。</b>
      </p>
      <el-form label-width="80px" size="small">
        <el-form-item label="接收人">
          <el-select
            v-model="transferTo"
            filterable
            placeholder="选择接收的教师"
            style="width: 100%"
          >
            <el-option
              v-for="u in transferCandidates"
              :key="u.id"
              :label="`${u.real_name || u.username}（${u.username}）`"
              :value="u.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="原因">
          <el-input v-model="transferReason" placeholder="如 张老师调离" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="transferVisible = false">取消</el-button>
        <el-button type="primary" @click="submitTransfer">移交</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ta__acts {
  display: flex;
  gap: 8px;
}

.ta__filters {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}

.ta__hint {
  color: var(--zf-text-dim);
  font-size: var(--zf-text-sm);
  line-height: 1.5;
}
</style>
