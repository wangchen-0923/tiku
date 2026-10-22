<script setup lang="ts">
/**
 * 知识点批量补标页（2026-10-16 需求方口径）。
 *
 * ★★ 与难度打标页的**形态不同**，因为活儿不同：
 *
 *   难度打标：1944 道题**每道都要标** → 一屏一题 + 键盘流。
 *   知识点补标：落点只有 44 个，痛点是「**21 个知识点一道题都没有**」
 *              → 先选知识点，再批量勾题。
 *              一屏一题在这里更慢：老师心里想的是
 *              「这几道都该挂到 K02」，不是「这道题该挂哪」。
 *
 * ★★★ 三条不能省的界面义务：
 *
 *   ① **每题必须显示「现在挂的是哪个点」**。这一步是**覆盖**，
 *      不显示等于让老师闭着眼睛改数据。
 *   ② **候选池只含该知识点所属任务的题**。后端会拒绝跨任务挂点
 *      （组卷按「任务 + 知识点」取数），候选池若不限任务，
 *      老师勾一半都会被拒 —— 那是把业务约束伪装成随机失败。
 *   ③ **结果分三桶显示**（已改 / 本来就对 / 被拒）。
 *      把「本来就对」混进「已改」，老师会以为动了 50 道，其实一道没动。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import * as KM from '@/api/knowledgeMark'
import MathText from '@/components/MathText.vue'
import { truncateMath } from '@/utils/math'
import type { KnowledgeNodeRow } from '@/api/knowledge'

const loadingNodes = ref(false)
const loadingCand = ref(false)
const submitting = ref(false)

const nodes = ref<KnowledgeNodeRow[]>([])
const selectedNode = ref<KnowledgeNodeRow | null>(null)
const nodeKeyword = ref('')
const onlyZero = ref(false)

const candidates = ref<KM.KpCandidate[]>([])
const candidateKeyword = ref('')
const checked = ref<number[]>([])
const summary = ref<{ progress: KM.KpProgress; gap: KM.KpGap } | null>(null)
const result = ref<KM.KpBatchResult | null>(null)

/** 课程 id：本期只有一门课，取第一个（与难度打标页同做法） */
const courseId = ref(1)

const filteredNodes = computed(() => {
  const k = nodeKeyword.value.trim().toLowerCase()
  return nodes.value.filter((n) => {
    if (onlyZero.value && n.question_count > 0) return false
    if (!k) return true
    return (
      n.code.toLowerCase().includes(k) ||
      n.name.toLowerCase().includes(k) ||
      (n.task_name ?? '').toLowerCase().includes(k)
    )
  })
})

/** 已经挂在这个点上的题（在候选里被勾选时不需要重复写） */
const alreadyOnNode = computed(
  () =>
    new Set(
      candidates.value
        .filter((c) => c.current_primary?.id === selectedNode.value?.id)
        .map((c) => c.id),
    ),
)

/** 勾选的题里，有多少道**会被覆盖**（原本挂的是别的点） */
const willOverwrite = computed(
  () => checked.value.filter((id) => !alreadyOnNode.value.has(id)).length,
)

async function loadSummary(): Promise<void> {
  try {
    summary.value = await KM.getKpSummary()
  } catch {
    summary.value = null
  }
}

async function loadNodes(): Promise<void> {
  loadingNodes.value = true
  try {
    nodes.value = await KM.listKpNodes(courseId.value)
  } catch (e) {
    ElMessage.error(`知识点加载失败：${(e as Error).message}`)
  } finally {
    loadingNodes.value = false
  }
}

async function pickNode(n: KnowledgeNodeRow): Promise<void> {
  selectedNode.value = n
  checked.value = []
  result.value = null
  candidateKeyword.value = ''
  await loadCandidates()
}

async function loadCandidates(): Promise<void> {
  const node = selectedNode.value
  if (!node) return
  loadingCand.value = true
  try {
    const d = await KM.getKpCandidates(node.id, {
      search: candidateKeyword.value.trim() || undefined,
      limit: 100,
    })
    candidates.value = d.items ?? []
  } catch (e) {
    candidates.value = []
    ElMessage.error(`候选题目加载失败：${(e as Error).message}`)
  } finally {
    loadingCand.value = false
  }
}

function selectAllPage(): void {
  const ids = new Set(checked.value)
  for (const c of candidates.value) ids.add(c.id)
  checked.value = [...ids]
}

function clearChecked(): void {
  checked.value = []
}

/**
 * 表格勾选变化。
 *
 * ★ 写成命名函数而不是模板里的内联箭头 + 类型标注：
 *   模板表达式**不支持 TS 类型标注**，`(rows: KM.KpCandidate[]) => …`
 *   会在 `vue-tsc` 阶段报错。
 */
function onSelectionChange(rows: KM.KpCandidate[]): void {
  checked.value = rows.map((r) => r.id)
}

async function submit(): Promise<void> {
  const node = selectedNode.value
  if (!node) return
  if (!checked.value.length) {
    ElMessage.warning('先勾选要挂到该知识点的题目')
    return
  }
  const overwrite = willOverwrite.value
  try {
    await ElMessageBox.confirm(
      `把勾选的 ${checked.value.length} 道题挂到「${node.code} ${node.name}」。` +
        `其中 ${overwrite} 道原本挂的是别的知识点，` +
        '会被**覆盖**（原关联保留为次要关联，可回溯）。',
      '确认批量补标',
      { confirmButtonText: '确认覆盖', cancelButtonText: '再看看', type: 'warning' },
    )
  } catch {
    return
  }

  submitting.value = true
  try {
    result.value = await KM.setKnowledgeBatch(node.id, checked.value)
    const r = result.value
    if (r.failed) {
      ElMessage.warning(
        `已改标 ${r.updated} 道；被拒 ${r.failed} 道（原因见下方清单）`,
      )
    } else {
      ElMessage.success(
        `已改标 ${r.updated} 道，本来就对 ${r.skipped} 道`,
      )
    }
    checked.value = []
    await Promise.all([loadCandidates(), loadNodes(), loadSummary()])
  } catch (e) {
    ElMessage.error(`批量补标失败：${(e as Error).message}`)
  } finally {
    submitting.value = false
  }
}

function stemPreview(s: string): string {
  const t = (s ?? '').replace(/\s+/g, ' ').trim()
  // ★ 用 truncateMath 而不是裸 slice：截在 `$\frac{1}{2}$` 中间会让
  //   定界符不配对，整段退化成字面文本
  return truncateMath(t, 46)
}

function optionText(c: KM.KpCandidate): string {
  return (c.options ?? [])
    .map((o) => `${o.label}.${(o.content_text ?? '').trim()}`)
    .join('  ')
}

onMounted(async () => {
  await Promise.all([loadNodes(), loadSummary()])
  const first = nodes.value.find((n) => n.question_count === 0) ?? nodes.value[0]
  if (first) await pickNode(first)
})
</script>

<template>
  <div class="kp">
    <!-- ══ 顶部：缺口与进度 ══ -->
    <el-card shadow="never" class="kp__bar">
      <div class="kp__bar-row">
        <div>
          <h2 class="kp__title">知识点批量补标</h2>
          <p class="kp__sub">
            选一个知识点，再批量勾选要挂到它下面的题。已有的知识点会被
            <b>覆盖</b>（原关联保留为次要关联，可回溯）。
          </p>
        </div>
        <div v-if="summary" class="kp__stats">
          <div class="kp__stat">
            <div class="kp__stat-n">{{ summary.progress.marked }}</div>
            <div class="kp__stat-t">已人工确认</div>
          </div>
          <div class="kp__stat">
            <div class="kp__stat-n">{{ summary.progress.remaining }}</div>
            <div class="kp__stat-t">仍是导入占位</div>
          </div>
          <div class="kp__stat" :class="{ 'is-alert': summary.gap.zero_nodes > 0 }">
            <div class="kp__stat-n">{{ summary.gap.zero_nodes }}</div>
            <div class="kp__stat-t">知识点一道题都没有</div>
          </div>
        </div>
      </div>
    </el-card>

    <div class="kp__body">
      <!-- ══ 左：知识点 ══ -->
      <el-card shadow="never" class="kp__pane">
        <template #header>
          <div class="kp__pane-head">
            <span>知识点（{{ filteredNodes.length }} / {{ nodes.length }}）</span>
            <el-switch
              v-model="onlyZero"
              size="small"
              active-text="只看 0 题"
            />
          </div>
        </template>
        <el-input
          v-model="nodeKeyword"
          size="small"
          clearable
          placeholder="搜索编码 / 名称 / 任务"
          class="kp__search"
        />
        <div v-loading="loadingNodes" class="kp__list">
          <div
            v-for="n in filteredNodes"
            :key="n.id"
            class="kp__node"
            :class="{ 'is-active': selectedNode?.id === n.id }"
            @click="pickNode(n)"
          >
            <div class="kp__node-top">
              <span class="kp__node-name">{{ n.name }}</span>
              <el-tag
                :type="n.question_count === 0 ? 'danger' : 'info'"
                size="small"
                effect="plain"
              >
                {{ n.question_count }} 题
              </el-tag>
            </div>
            <div class="kp__node-meta">
              <code>{{ n.code }}</code>
              <span>· {{ n.task_name }}</span>
            </div>
          </div>
          <el-empty
            v-if="!loadingNodes && !filteredNodes.length"
            description="没有匹配的知识点"
            :image-size="60"
          />
        </div>
      </el-card>

      <!-- ══ 右：候选题目 ══ -->
      <el-card shadow="never" class="kp__pane kp__pane--main">
        <template #header>
          <div class="kp__pane-head">
            <span v-if="selectedNode">
              挂到
              <b>{{ selectedNode.code }}</b>
              {{ selectedNode.name }}
              <span class="kp__task">（任务：{{ selectedNode.task_name }}）</span>
            </span>
            <span v-else>先选左边一个知识点</span>
            <div class="kp__acts">
              <el-button size="small" :disabled="!candidates.length"
                @click="selectAllPage">全选本页</el-button>
              <el-button size="small" :disabled="!checked.length"
                @click="clearChecked">清空</el-button>
              <el-button
                type="primary"
                size="small"
                :loading="submitting"
                :disabled="!checked.length"
                @click="submit"
              >
                批量补标（{{ checked.length }}）
              </el-button>
            </div>
          </div>
        </template>

        <el-alert
          v-if="checked.length"
          :closable="false"
          type="info"
          class="kp__hint"
          :title="`将挂到「${selectedNode?.code}」共 ${checked.length} 道；其中 ${willOverwrite} 道会被覆盖（原来挂的是别的点）`"
        />

        <div class="kp__cand-bar">
          <el-input
            v-model="candidateKeyword"
            size="small"
            clearable
            placeholder="在候选里搜题干"
            @keyup.enter="loadCandidates"
          />
          <el-button size="small" @click="loadCandidates">搜索</el-button>
          <span class="kp__cand-meta">
            候选 {{ candidates.length }} 道（仅该任务下的题，跨任务挂点会被后端拒绝）
          </span>
        </div>

        <el-table
          v-loading="loadingCand"
          :data="candidates"
          size="small"
          height="52vh"
          @selection-change="onSelectionChange"
        >
          <el-table-column type="selection" width="42" />
          <el-table-column label="#" width="64">
            <template #default="{ row }">
              <span class="kp__qid">#{{ row.id }}</span>
            </template>
          </el-table-column>
          <el-table-column label="题型" width="88">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ row.type }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="题干 / 选项" min-width="320">
            <template #default="{ row }">
              <MathText
                tag="div"
                class="kp__stem"
                mode="text"
                :source="stemPreview(row.stem_text)"
              />
              <div class="kp__opts">{{ optionText(row) }}</div>
            </template>
          </el-table-column>
          <el-table-column label="现在挂的知识点" width="230">
            <template #default="{ row }">
              <template v-if="row.current_primary">
                <el-tag
                  :type="row.current_primary.id === selectedNode?.id ? 'success' : 'warning'"
                  size="small"
                  effect="plain"
                >
                  {{ row.current_primary.code }}
                </el-tag>
                <span class="kp__src">
                  {{ KM.KP_SOURCE_LABELS[row.current_primary.source] ?? row.current_primary.source }}
                </span>
              </template>
              <span v-else class="kp__src">无</span>
            </template>
          </el-table-column>
        </el-table>

        <!-- ══ 结果三桶 ══ -->
        <div v-if="result" class="kp__result">
          <div class="kp__result-row">
            <el-tag type="success" effect="dark">已改标 {{ result.updated }}</el-tag>
            <el-tag type="info" effect="plain">本来就对 {{ result.skipped }}</el-tag>
            <el-tag :type="result.failed ? 'danger' : 'info'" effect="plain">
              被拒 {{ result.failed }}
            </el-tag>
            <span class="kp__src">共提交 {{ result.requested }} 道</span>
          </div>
          <ul v-if="result.failed_items.length" class="kp__fails">
            <li v-for="f in result.failed_items" :key="f.question_id">
              #{{ f.question_id }} —— {{ f.detail }}
            </li>
          </ul>
          <div v-if="result.skipped_items.length" class="kp__src">
            本来就对：{{ result.skipped_items.map((s) => `#${s.question_id}`).join('、') }}
          </div>
        </div>
      </el-card>
    </div>
  </div>
</template>

<style scoped>
.kp { display: flex; flex-direction: column; gap: 12px; }
.kp__bar { padding-bottom: 6px; }
.kp__bar-row { display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; }
.kp__title { margin: 0 0 4px; font-size: 18px; color: var(--zf-text-primary); }
.kp__sub { margin: 0; font-size: var(--zf-text-xs); color: var(--zf-text-tertiary); }
.kp__stats { display: flex; gap: 18px; }
.kp__stat { text-align: right; min-width: 92px; }
.kp__stat-n { font-size: 22px; font-weight: 600; color: var(--zf-text-primary); }
.kp__stat-t { font-size: var(--zf-text-xs); color: var(--zf-text-tertiary); }
.kp__stat.is-alert .kp__stat-n { color: var(--zf-danger, #E5484D); }

.kp__body { display: grid; grid-template-columns: 320px 1fr; gap: 12px; align-items: start; }
.kp__pane-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; font-size: var(--zf-text-sm);
}
.kp__acts { display: flex; gap: 6px; }
.kp__task { color: var(--zf-text-tertiary); font-weight: 400; }
.kp__search { margin-bottom: 8px; }
.kp__list { max-height: 62vh; overflow: auto; }
.kp__node {
  padding: 8px 10px; border-radius: 6px; cursor: pointer;
  border: 1px solid transparent;
}
.kp__node:hover { background: var(--zf-bg-hover); }
.kp__node.is-active {
  background: var(--zf-brand-subtle);
  border-color: var(--zf-brand-border);
}
.kp__node-top { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.kp__node-name { font-size: var(--zf-text-sm); color: var(--zf-text-primary); }
.kp__node-meta {
  margin-top: 2px; font-size: var(--zf-text-xs); color: var(--zf-text-tertiary);
  display: flex; gap: 6px; align-items: baseline;
}
.kp__node-meta code { font-family: var(--zf-font-mono); }
.kp__hint { margin-bottom: 8px; }
.kp__cand-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.kp__cand-bar :deep(.el-input) { max-width: 280px; }
.kp__cand-meta { font-size: var(--zf-text-xs); color: var(--zf-text-tertiary); }
.kp__qid { font-family: var(--zf-font-mono); font-size: var(--zf-text-xs); color: var(--zf-text-tertiary); }
.kp__stem { font-size: var(--zf-text-sm); color: var(--zf-text-primary); }
.kp__opts {
  font-size: var(--zf-text-xs); color: var(--zf-text-secondary);
  margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.kp__src { margin-left: 6px; font-size: var(--zf-text-xs); color: var(--zf-text-tertiary); }
.kp__result { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--zf-border-subtle); }
.kp__result-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.kp__fails {
  margin: 8px 0 0; padding-left: 18px;
  font-size: var(--zf-text-xs); color: var(--zf-text-secondary);
}
</style>
