<script setup lang="ts">
/**
 * 知识点 · 知识库条目面板（新建 / 编辑 / 归档 / 删除）。
 *
 * ★★ 为什么做成组件而不是在两处各写一遍：
 *    「知识体系」（`/knowledge`，老师天天看指标）与
 *    「知识体系管理」（`/knowledge/manage`，一学期动几次结构）
 *    都需要管条目。两份实现必然分叉 —— 一个改了按钮判据、另一个没改，
 *    而这类分叉**不会报错**，只会让用户在某一页看到不一样的按钮。
 *
 * ★★★ 三条硬规则（都来自后端，前端只负责**如实显示**）：
 *
 *   1. **能不能改由后端说了算**：用 `row.editable`，不要自己拿
 *      `owner.id === 我.id` 再算一遍。后端那道判据还包含
 *      「关联知识点下有题就拒」（Q7），前端重算必然算漏。
 *   2. **不可改时必须说明原因**：`owner === null`（学校统一导入）、
 *      `owner.id !== 我`（同事的）、`blocking_nodes` 非空（有题）。
 *      只把按钮置灰不给理由，等于让老师猜。
 *   3. **删除与归档语义不同**（界面上必须分开写清）：
 *        归档 = 只改状态，**可逆**，不写审计
 *        删除 = 软删 + **写审计**，默认列表不再出现
 *      把两者合并成一个「删除」，老师会以为归档就删掉了。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import * as K from '@/api/knowledge'
import type { KnowledgeEntry } from '@/api/knowledge'
import { ApiError } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const props = defineProps<{
  /** 当前知识点 id */
  nodeId: number
  /** 可关联的知识点（新建/编辑时的多选来源）；不传则只保留当前知识点 */
  nodes?: Array<{ id: number; name: string; code: string }>
}>()

const emit = defineEmits<{ (e: 'changed'): void }>()

const auth = useAuthStore()

const entries = ref<KnowledgeEntry[]>([])
const loading = ref(false)
const error = ref('')

const canWrite = computed(() => auth.can('knowledge.entry.manage'))

const STATUS_LABEL: Record<string, string> = {
  ACTIVE: '启用',
  DRAFT: '草稿',
  ARCHIVED: '已归档',
}

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : '请求失败'
}

async function load(): Promise<void> {
  if (!props.nodeId) return
  loading.value = true
  error.value = ''
  try {
    entries.value = await K.listEntries({ nodeId: props.nodeId })
  } catch (e) {
    entries.value = []
    error.value = errMsg(e)
  } finally {
    loading.value = false
  }
}

watch(() => props.nodeId, load, { immediate: true })

/** 按钮为什么不能点。★ 三种原因分开写，因为处理方式完全不同。 */
function whyDisabled(row: KnowledgeEntry): string {
  if (row.blocking_nodes.length) {
    const first = row.blocking_nodes[0]
    const rest = row.blocking_nodes.length > 1
      ? ` 等 ${row.blocking_nodes.length} 个知识点`
      : ''
    return `该条目关联的 ${first.code}（${first.name}）下已有 ${first.question_count} 题${rest}——`
      + '知识库是 AI 生题的依据，有点在练时不允许改动依据。'
  }
  if (row.owner === null) {
    return '这条是学校统一导入的公共条目，只有学校平台账户可以修改。'
  }
  return `这条由 ${row.owner.name} 创建，只有创建者本人或学校平台账户可以修改。`
}

// ══════════════════════════════════════════════════
//  新建 / 编辑
// ══════════════════════════════════════════════════

const dlg = ref(false)
const mode = ref<'create' | 'edit'>('create')
const saving = ref(false)
const editingId = ref<number | null>(null)
const form = ref({
  entry_type: 'CONCEPT',
  title: '',
  content: '',
  source_type: 'TEACHER_INPUT',
  source_ref: '',
  node_ids: [] as number[],
})

const nodeOptions = computed(() => {
  if (props.nodes?.length) return props.nodes
  return []
})

function openCreate(): void {
  mode.value = 'create'
  editingId.value = null
  form.value = {
    entry_type: 'CONCEPT',
    title: '',
    content: '',
    source_type: 'TEACHER_INPUT',
    source_ref: '',
    node_ids: [props.nodeId],
  }
  dlg.value = true
}

function openEdit(row: KnowledgeEntry): void {
  mode.value = 'edit'
  editingId.value = row.id
  form.value = {
    entry_type: row.entry_type,
    title: row.title,
    content: row.content,
    source_type: row.source_type,
    source_ref: row.source_ref,
    node_ids: [...row.node_ids],
  }
  dlg.value = true
}

async function save(): Promise<void> {
  if (!form.value.content.trim()) {
    ElMessage.warning('内容不能为空（它是 AI 生题的实际依据）')
    return
  }
  if (!form.value.node_ids.length) {
    ElMessage.warning('至少要关联一个知识点（不挂知识点的条目检索不到）')
    return
  }
  // ★ 后端 `title` 必填（`EntryCreateSerializer` 比服务层严），
  //   而老师不一定会写标题 —— 空标题在提交前补成内容前 40 字，
  //   而不是让老师吃一个 400。
  const title = form.value.title.trim()
    || form.value.content.trim().slice(0, 40)

  saving.value = true
  try {
    if (mode.value === 'create') {
      await K.createEntry({
        title,
        content: form.value.content.trim(),
        entry_type: form.value.entry_type,
        source_type: form.value.source_type,
        source_ref: form.value.source_ref.trim(),
        node_ids: form.value.node_ids,
      })
      ElMessage.success('条目已创建')
    } else if (editingId.value) {
      await K.updateEntry(editingId.value, {
        title,
        content: form.value.content.trim(),
        entry_type: form.value.entry_type,
        source_type: form.value.source_type,
        source_ref: form.value.source_ref.trim(),
        node_ids: form.value.node_ids,
      })
      ElMessage.success('条目已更新')
    }
    dlg.value = false
    await load()
    emit('changed')
  } catch (e) {
    // ★ 409 的 `data.blocking` 是「为什么不能改」，原样列出来 ——
    //   只回一句「保存失败」，老师不知道要先处理哪几个知识点。
    if (e instanceof ApiError) {
      const blocking = e.extra<Array<{ code: string; name: string; question_count: number }>>(
        'blocking',
      )
      if (Array.isArray(blocking) && blocking.length) {
        ElMessage.error(
          `不能保存：${blocking.map((b) => `${b.code}（${b.name}，${b.question_count} 题）`).join('、')}`
          + ' 下已有题目。请先把这些题改挂到别的知识点。',
        )
        return
      }
    }
    ElMessage.error(errMsg(e))
  } finally {
    saving.value = false
  }
}

// ══════════════════════════════════════════════════
//  归档 / 删除
// ══════════════════════════════════════════════════

async function archive(row: KnowledgeEntry): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `归档「${row.title}」？\n\n`
      + '归档只是把它从「启用」下线，**内容仍在、可再启用**，不写审计。',
      '归档条目',
      { type: 'info', confirmButtonText: '归档', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await K.archiveEntry(row.id)
    ElMessage.success('已归档')
    await load()
    emit('changed')
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function remove(row: KnowledgeEntry): Promise<void> {
  let reason = ''
  try {
    const r = await ElMessageBox.prompt(
      `删除「${row.title}」？\n\n`
      + '这是**软删**（默认列表不再出现，可在审计中追溯），与「归档」不同。\n'
      + '填写原因会记进审计，便于事后追查。',
      '删除条目',
      {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        inputPlaceholder: '删除原因（可留空）',
        inputValue: '',
      },
    )
    reason = String(r.value ?? '')
  } catch {
    return
  }
  try {
    await K.deleteEntry(row.id, reason)
    ElMessage.success('已删除（软删）')
    await load()
    emit('changed')
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}
</script>

<template>
  <div class="zf-card">
    <div class="zf-card__head">
      <h2 class="zf-card__title">知识库条目</h2>
      <div class="zf-row">
        <span class="zf-dim ep__count">{{ entries.length }} 条</span>
        <el-button
          v-if="canWrite"
          size="small"
          type="primary"
          @click="openCreate"
        >
          新建条目
        </el-button>
      </div>
    </div>

    <div v-loading="loading" class="zf-card__body">
      <el-alert
        v-if="error"
        type="error"
        :closable="false"
        show-icon
        :title="error"
        class="ep__alert"
      />

      <div v-if="!entries.length && !loading" class="zf-empty">
        该知识点没有知识库条目
        <div class="zf-dim ep__hint">
          ★ 没有知识库条目时，AI 生题会被 L1 闸门**拒绝** ——
          这是三条不可绕过的保证之一
        </div>
      </div>

      <ul v-else class="ep__list">
        <li v-for="row in entries" :key="row.id" class="ep__item">
          <div class="ep__main">
            <div class="ep__line1">
              <el-tag size="small" effect="plain">
                {{ K.ENTRY_TYPE_LABEL[row.entry_type] ?? row.entry_type }}
              </el-tag>
              <span class="ep__title">{{ row.title || '（无标题）' }}</span>
              <el-tag
                v-if="row.status !== 'ACTIVE'"
                size="small"
                :type="row.status === 'ARCHIVED' ? 'info' : 'warning'"
              >
                {{ STATUS_LABEL[row.status] ?? row.status }}
              </el-tag>
            </div>
            <div class="ep__content">{{ row.content }}</div>
            <div class="ep__meta">
              <span class="zf-dim">
                {{ K.ENTRY_SOURCE_LABEL[row.source_type] ?? row.source_type }}
                <template v-if="row.source_ref"> · {{ row.source_ref }}</template>
              </span>
              <span class="zf-dim ep__owner">
                归属：
                <template v-if="row.owner">{{ row.owner.name }}</template>
                <template v-else>学校公共</template>
              </span>
              <span v-if="row.adopted_count" class="zf-dim">
                被 AI 采纳 {{ row.adopted_count }} 次
              </span>
              <span
                v-if="row.blocking_nodes.length"
                class="ep__blocked"
              >
                有题不可改
              </span>
            </div>
          </div>

          <div v-if="canWrite" class="ep__ops">
            <el-tooltip
              :disabled="row.editable"
              :content="whyDisabled(row)"
              placement="top"
            >
              <span>
                <el-button
                  size="small"
                  text
                  type="primary"
                  :disabled="!row.editable"
                  @click="openEdit(row)"
                >
                  编辑
                </el-button>
              </span>
            </el-tooltip>
            <el-tooltip
              :disabled="row.editable || row.status === 'ARCHIVED'"
              :content="row.editable ? '已归档' : whyDisabled(row)"
              placement="top"
            >
              <span>
                <el-button
                  size="small"
                  text
                  :disabled="!row.editable || row.status === 'ARCHIVED'"
                  @click="archive(row)"
                >
                  归档
                </el-button>
              </span>
            </el-tooltip>
            <el-tooltip
              :disabled="row.editable"
              :content="whyDisabled(row)"
              placement="top"
            >
              <span>
                <el-button
                  size="small"
                  text
                  type="danger"
                  :disabled="!row.editable"
                  @click="remove(row)"
                >
                  删除
                </el-button>
              </span>
            </el-tooltip>
          </div>
        </li>
      </ul>
    </div>

    <!-- ══════════ 新建 / 编辑 ══════════ -->
    <el-dialog
      v-model="dlg"
      :title="mode === 'create' ? '新建知识库条目' : '编辑知识库条目'"
      width="560px"
    >
      <el-form label-width="88px">
        <el-form-item label="类型">
          <el-select v-model="form.entry_type" style="width: 100%">
            <el-option
              v-for="(label, key) in K.ENTRY_TYPE_LABEL"
              :key="key"
              :label="label"
              :value="key"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="标题">
          <el-input
            v-model="form.title"
            maxlength="200"
            show-word-limit
            placeholder="留空则自动取内容前 40 字"
          />
        </el-form-item>
        <el-form-item label="内容" required>
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="5"
            maxlength="4000"
            show-word-limit
            placeholder="这条知识点的事实性内容。AI 生题时会被引用，请写准确。"
          />
        </el-form-item>
        <el-form-item label="关联知识点" required>
          <el-select
            v-model="form.node_ids"
            multiple
            filterable
            style="width: 100%"
            placeholder="至少一个"
          >
            <el-option
              v-for="n in nodeOptions"
              :key="n.id"
              :label="`${n.code} ${n.name}`"
              :value="n.id"
            />
          </el-select>
          <div class="ep__fieldnote">
            一条内容可挂多个知识点（如「力的合成」同属两个考点）。
            <template v-if="mode === 'edit'">
              改动关联时会**按新旧全部知识点**重新过一遍闸门。
            </template>
          </div>
        </el-form-item>
        <el-form-item label="来源">
          <el-select v-model="form.source_type" style="width: 100%">
            <el-option
              v-for="(label, key) in K.ENTRY_SOURCE_LABEL"
              :key="key"
              :label="label"
              :value="key"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="来源引用">
          <el-input
            v-model="form.source_ref"
            maxlength="300"
            placeholder="教材页码 / 文档名，可留空"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ep__count {
  font-size: var(--zf-text-xs);
}

.ep__alert {
  margin-bottom: var(--zf-space-3);
}

.ep__hint {
  margin-top: 6px;
  font-size: var(--zf-text-xs);
  line-height: var(--zf-leading-loose);
}

.ep__list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.ep__item {
  display: flex;
  align-items: flex-start;
  gap: var(--zf-space-3);
  padding: var(--zf-space-2) 0;
  border-bottom: 1px solid var(--zf-border);
}

.ep__item:last-child {
  border-bottom: none;
}

.ep__main {
  min-width: 0;
  flex: 1;
}

.ep__line1 {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
  flex-wrap: wrap;
}

.ep__title {
  font-size: var(--zf-text-sm);
  font-weight: var(--zf-weight-medium);
  color: var(--zf-text-primary);
}

.ep__content {
  margin-top: 3px;
  font-size: var(--zf-text-sm);
  color: var(--zf-text-secondary);
  line-height: var(--zf-leading-loose);
  /* 两行截断：列表要扫得快，全文在编辑里看 */
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.ep__meta {
  display: flex;
  align-items: center;
  gap: var(--zf-space-3);
  flex-wrap: wrap;
  margin-top: 4px;
  font-size: var(--zf-text-xs);
}

.ep__owner {
  white-space: nowrap;
}

.ep__blocked {
  font-size: var(--zf-text-xs);
  color: var(--zf-warning);
}

.ep__ops {
  display: flex;
  align-items: center;
  gap: 2px;
  flex: none;
}

.ep__fieldnote {
  margin-top: 2px;
  font-size: var(--zf-text-xs);
  color: var(--zf-text-tertiary, var(--zf-text-secondary));
  line-height: var(--zf-leading-loose);
}

@media (max-width: 720px) {
  .ep__item {
    flex-direction: column;
  }

  .ep__ops {
    padding-left: 2px;
  }
}
</style>
