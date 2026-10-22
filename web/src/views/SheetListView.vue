<script setup lang="ts">
/**
 * 答题卡列表。
 *
 * ★ 新链路：建卡（手工）→ 绑定试卷 → 导出打印 → 扫描识别。
 *   列表不再有「由试卷生成」，样式与模板合并为「答题卡」。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import * as S from '@/api/sheet'

const router = useRouter()
const rows = ref<S.SheetTemplateBrief[]>([])
const loading = ref(false)
const error = ref('')
const keyword = ref('')
const status = ref('')

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    rows.value = await S.listTemplates({
      keyword: keyword.value || undefined,
      status: status.value || undefined,
    })
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败'
    rows.value = []
  } finally {
    loading.value = false
  }
}

onMounted(load)

function createNew(): void {
  void router.push({ name: 'SheetDesignerNew' })
}

function edit(row: S.SheetTemplateBrief): void {
  void router.push({ name: 'SheetDesigner', params: { id: row.id } })
}

async function copy(row: S.SheetTemplateBrief): Promise<void> {
  try {
    const t = await S.duplicateTemplate(row.id)
    ElMessage.success(`已复制为草稿 v${t.version}`)
    await load()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '复制失败')
  }
}

async function remove(row: S.SheetTemplateBrief): Promise<void> {
  try {
    await ElMessageBox.confirm(
      row.status === 'DRAFT'
        ? `删除草稿「${row.name}」？`
        : `「${row.name}」已发布，将改为「已弃用」（历史扫描仍可识别）。继续？`,
      '删除答题卡',
      { confirmButtonText: '确定', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await S.deleteTemplate(row.id)
    ElMessage.success('已处理')
    await load()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '删除失败')
  }
}

async function pdf(row: S.SheetTemplateBrief): Promise<void> {
  try {
    const blob = await S.getTemplatePdfBlob(row.id)
    S.downloadBlob(blob, `${row.name}-v${row.version}.pdf`)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'PDF 下载失败')
  }
}

function scan(): void {
  void router.push({ name: 'SheetScan' })
}
</script>

<template>
  <div class="zf-stack">
    <div class="head">
      <div>
        <h1 class="page-title">答题卡</h1>
        <p class="zf-muted page-sub">
          手工建卡 → 绑定定稿试卷 → 导出打印 → 扫描识别 → 批改
        </p>
      </div>
      <div class="zf-row">
        <el-button @click="scan">扫描识别</el-button>
        <el-button type="primary" @click="createNew">新建答题卡</el-button>
      </div>
    </div>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="error"
    />

    <section class="zf-card">
      <div class="zf-card__head">
        <h2 class="zf-card__title">全部答题卡</h2>
        <div class="zf-row">
          <el-input
            v-model="keyword"
            clearable
            size="small"
            placeholder="按名称搜索"
            style="width: 180px"
            @keyup.enter="load"
          />
          <el-select
            v-model="status"
            clearable
            size="small"
            placeholder="全部状态"
            style="width: 130px"
            @change="load"
          >
            <el-option label="草稿" value="DRAFT" />
            <el-option label="已发布" value="ACTIVE" />
            <el-option label="已弃用" value="DEPRECATED" />
          </el-select>
          <el-button size="small" @click="load">刷新</el-button>
        </div>
      </div>
      <div class="zf-card__body">
        <el-table v-loading="loading" :data="rows" size="small">
          <el-table-column prop="name" label="名称" min-width="180">
            <template #default="{ row }">
              <a class="link" @click="edit(row)">{{ row.name }}</a>
            </template>
          </el-table-column>
          <el-table-column label="纸张" width="90">
            <template #default="{ row }">
              {{ S.PAGE_SIZE_LABEL[row.page_size] ?? row.page_size }}
            </template>
          </el-table-column>
          <el-table-column prop="page_count" label="页数" width="70" />
          <el-table-column prop="question_count" label="题量" width="70" />
          <el-table-column prop="total_score" label="总分" width="70" />
          <el-table-column label="版本" width="70">
            <template #default="{ row }">v{{ row.version }}</template>
          </el-table-column>
          <el-table-column label="状态" width="96">
            <template #default="{ row }">
              <el-tag
                size="small"
                :type="S.TEMPLATE_STATUS_TONE[row.status] ?? 'info'"
              >
                {{ S.TEMPLATE_STATUS_LABEL[row.status] ?? row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="250" fixed="right">
            <template #default="{ row }">
              <el-button size="small" text type="primary" @click="edit(row)">
                编辑
              </el-button>
              <el-button size="small" text @click="copy(row)">复制</el-button>
              <el-button
                size="small"
                text
                :disabled="!row.asset_id"
                @click="pdf(row)"
              >
                PDF
              </el-button>
              <el-button size="small" text type="danger" @click="remove(row)">
                删除
              </el-button>
            </template>
          </el-table-column>
          <template #empty>
            <div class="zf-empty">
              {{ loading ? '读取中…' : '还没有答题卡，点右上角「新建答题卡」' }}
            </div>
          </template>
        </el-table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--zf-space-4);
  flex-wrap: wrap;
}
.page-title {
  margin: 0;
  font-size: var(--zf-text-2xl);
  font-weight: var(--zf-weight-semibold);
}
.page-sub {
  margin: var(--zf-space-1) 0 0;
  font-size: var(--zf-text-sm);
}
.link {
  color: var(--zf-primary);
  cursor: pointer;
}
</style>
