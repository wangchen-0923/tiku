<script setup lang="ts">
/**
 * 学生答题 H5（`/s/{publish_token}`）。
 *
 * ★★ 这个页面的用户是**中职学生，用手机**，所以三条硬约束：
 *
 *   1. **一屏一题**，不是整卷滚动。
 *      45 道题滚动到第 30 题时，学生会忘记自己哪些没答；
 *      一屏一题配合底部答题卡，随时能看到进度。
 *
 *   2. **选项按钮要够大**（≥ 48px 高）。
 *      手指点选，不是鼠标。小按钮会造成误选，
 *      而误选在考试里是直接丢分。
 *
 *   3. **不显示任何「答案对不对」的反馈**。
 *      作答期间不告诉学生对错（`answer_visibility` 由老师控制），
 *      否则先答的人可以把答案传出去。
 *
 * ★★ 三种状态要分清楚，不能混：
 *
 *     入口信息加载失败 / 令牌无效  → 死路，只给「重新扫码」
 *     未绑定                      → 显示绑定表单
 *     已绑定                      → 显示卷面
 *
 *   ★ 「已交卷」是第四种：学生绑定后进来会看到 `has_submitted=true`，
 *     这时要显示交卷成功页而不是卷面——否则他会重复作答，
 *     而后端会拒（`ASSIGNMENT_ALREADY_SUBMITTED`），体验很糟。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'

import { BusinessBranch } from '@/api/http'
import * as S from '@/api/student'
import type { AnswerPayload, StudentPaper, StudentQuestion } from '@/api/student'
import MathText from '@/components/MathText.vue'

const route = useRoute()
const token = computed(() => String(route.params.token ?? ''))

type Phase = 'loading' | 'error' | 'bind' | 'answering' | 'done'
const phase = ref<Phase>('loading')
const errorMsg = ref('')
const entry = ref<S.StudentEntry | null>(null)
const paper = ref<StudentPaper | null>(null)

// ── 绑定表单 ──
const bindForm = reactive({ student_no: '', name: '' })
const bindError = ref('')
const binding = ref(false)

// ── 作答 ──
/** `sheet_seq → 作答`：选项题存卷面字母数组，填空题存逐空文本 */
const answers = reactive<Record<number, { labels: string[]; blanks: string[] }>>(
  {},
)
const index = ref(0)
const submitting = ref(false)
const savedTip = ref('')
const studentName = ref('')

// ── 倒计时 ──
const now = ref(Date.now())
let timer: number | undefined

const questions = computed<StudentQuestion[]>(() => paper.value?.items ?? [])
const current = computed<StudentQuestion | null>(
  () => questions.value[index.value] ?? null,
)
const total = computed(() => questions.value.length)
const answeredCount = computed(
  () =>
    questions.value.filter((q) => {
      const a = answers[q.sheet_seq]
      if (!a) return false
      return S.isOptionType(q.type)
        ? a.labels.length > 0
        : a.blanks.some((x) => x.trim() !== '')
    }).length,
)
const remain = computed(() => S.remainMs(paper.value?.end_at ?? '', now.value))
const timeText = computed(() => S.formatDuration(remain.value))
const overtime = computed(() => Boolean(paper.value) && remain.value <= 0)

/** 初始化每个空的作答槽位（避免模板里做可选链） */
function ensureSlots(): void {
  for (const q of questions.value) {
    if (!answers[q.sheet_seq]) {
      answers[q.sheet_seq] = {
        labels: [],
        blanks: S.isOptionType(q.type)
          ? []
          : new Array(S.countBlanks(q.stem_text || q.stem_html || '')).fill(''),
      }
    }
  }
}

// ══════════════════════════════════════════════════
//  加载
// ══════════════════════════════════════════════════

async function load(): Promise<void> {
  phase.value = 'loading'
  errorMsg.value = ''
  try {
    entry.value = await S.getEntry(token.value)
  } catch (e) {
    phase.value = 'error'
    errorMsg.value =
      e instanceof Error ? e.message : '扫码链接无效或已失效'
    return
  }

  // 已有会话 → 直接取卷（刷新页面不该让学生重新绑定）
  if (S.studentSession.get(token.value)) {
    const ok = await loadPaper()
    if (ok) return
  }
  phase.value = 'bind'
}

async function loadPaper(): Promise<boolean> {
  try {
    paper.value = await S.getPaper(token.value)
    ensureSlots()
    // ★ 后端会在绑定返回里带 `has_submitted`；这里再判一次，
    //   覆盖「绑定后离开、再回来」的情况。
    const st = paper.value.status
    if (st === 'SUBMITTED' || st === 'GRADED') {
      phase.value = 'done'
      return true
    }
    if (st === 'NOT_STARTED' || st === 'IN_PROGRESS') {
      phase.value = 'answering'
      return true
    }
    phase.value = 'error'
    errorMsg.value = `任务当前状态为 ${st}，不能作答`
    return false
  } catch (e) {
    // 会话失效（换设备 / 服务端清了）→ 退回绑定
    S.studentSession.clear(token.value)
    if (e instanceof Error && e.message.includes('绑定')) {
      phase.value = 'bind'
      return false
    }
    phase.value = 'error'
    errorMsg.value = e instanceof Error ? e.message : '取卷面失败'
    return false
  }
}

onMounted(async () => {
  await load()
  timer = window.setInterval(() => {
    now.value = Date.now()
  }, 1000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})

// ══════════════════════════════════════════════════
//  绑定
// ══════════════════════════════════════════════════

async function handleBind(): Promise<void> {
  bindError.value = ''
  if (!bindForm.student_no.trim()) {
    bindError.value = '请输入学号'
    return
  }
  if (!bindForm.name.trim()) {
    bindError.value = '请输入姓名'
    return
  }
  binding.value = true
  try {
    const r = await S.bind(token.value, {
      student_no: bindForm.student_no.trim(),
      name: bindForm.name.trim(),
    })
    studentName.value = bindForm.name.trim()
    if (r.has_submitted) {
      phase.value = 'done'
      return
    }
    await loadPaper()
  } catch (e) {
    // ★ 学号不在名册是**业务分支**（HTTP 200 + code），
    //   用后端给的 message（那是一句能照着做的提示），
    //   不要自己编一句「绑定失败」。
    if (e instanceof BusinessBranch) {
      bindError.value = e.message
    } else {
      bindError.value = e instanceof Error ? e.message : '绑定失败'
    }
  } finally {
    binding.value = false
  }
}

// ══════════════════════════════════════════════════
//  作答
// ══════════════════════════════════════════════════

function pickOption(q: StudentQuestion, label: string): void {
  const a = answers[q.sheet_seq]
  if (!a) return
  if (S.isMultiSelect(q.type)) {
    const i = a.labels.indexOf(label)
    if (i >= 0) a.labels.splice(i, 1)
    else a.labels.push(label)
  } else {
    // 单选/判断：再点一次取消（学生会想「我点错了要取消」）
    a.labels = a.labels[0] === label ? [] : [label]
  }
}

function isPicked(q: StudentQuestion, label: string): boolean {
  return (answers[q.sheet_seq]?.labels ?? []).includes(label)
}

function buildPayload(): AnswerPayload[] {
  const out: AnswerPayload[] = []
  for (const q of questions.value) {
    const a = answers[q.sheet_seq]
    if (!a) continue
    if (S.isOptionType(q.type)) {
      if (a.labels.length) {
        out.push({ sheet_seq: q.sheet_seq, labels: [...a.labels] })
      }
    } else {
      if (a.blanks.some((x) => x.trim() !== '')) {
        out.push({
          sheet_seq: q.sheet_seq,
          // ★ 空也照传：后端按「第 i 空」对位，
          //   中间留空不能把后面的空往前挤。
          blanks: a.blanks.map((x) => x.trim()),
        })
      }
    }
  }
  return out
}

/** 保存进度（不交卷）。★ 断网/切后台时学生不会丢答案。 */
async function saveProgress(): Promise<void> {
  if (submitting.value) return
  submitting.value = true
  try {
    const r = await S.submit(token.value, buildPayload(), false)
    savedTip.value = `已保存 ${r.saved} 道题`
    if (r.failed) {
      // ★ 部分失败必须告诉学生：否则他以为都存上了
      ElMessage.warning(`${r.failed} 道题未能保存，请检查后重试`)
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '保存失败')
  } finally {
    submitting.value = false
  }
}

async function handleSubmit(): Promise<void> {
  const unanswered = total.value - answeredCount.value
  try {
    await ElMessageBox.confirm(
      unanswered > 0
        ? `还有 ${unanswered} 道题未作答，确定交卷吗？\n交卷后不能修改。`
        : '确定交卷吗？交卷后不能修改。',
      '确认交卷',
      { confirmButtonText: '交卷', cancelButtonText: '再检查一下' },
    )
  } catch {
    return
  }
  submitting.value = true
  try {
    const r = await S.submit(token.value, buildPayload(), true)
    if (r.failed) {
      // ★★ 部分失败**不能**说「交卷成功」——学生会以为全交上了。
      await ElMessageBox.alert(
        `有 ${r.failed} 道题未能保存，请联系老师：\n` +
          r.errors.map((e) => `第 ${e.sheet_seq} 题：${e.error}`).join('\n'),
        '部分作答未保存',
        { confirmButtonText: '我知道了' },
      )
    }
    phase.value = 'done'
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '交卷失败')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="sa">
    <!-- ── 加载中 ── -->
    <div v-if="phase === 'loading'" class="sa__center">
      <p class="zf-muted">正在加载…</p>
    </div>

    <!-- ── 死路（令牌无效 / 状态不对）── -->
    <div v-else-if="phase === 'error'" class="sa__center">
      <div class="sa__err">!</div>
      <h2 class="sa__title">无法进入答题</h2>
      <p class="zf-muted sa__msg">{{ errorMsg }}</p>
      <p class="zf-dim sa__hint">请确认扫描的是老师最新发的二维码，或联系老师。</p>
    </div>

    <!-- ── 绑定身份 ── -->
    <div v-else-if="phase === 'bind'" class="sa__bind">
      <h1 class="sa__title">{{ entry?.title || '答题' }}</h1>
      <p class="zf-muted sa__msg">请输入学号与姓名，确认你的身份</p>

      <div v-if="entry && !entry.is_open" class="sa__closed">
        本次答题时间窗为
        {{ new Date(entry.start_at).toLocaleString() }} –
        {{ new Date(entry.end_at).toLocaleString() }}
        <template v-if="!entry.allow_late">，目前已关闭</template>
      </div>

      <div class="sa__form">
        <label class="sa__label">学号</label>
        <input
          v-model="bindForm.student_no"
          class="sa__input"
          inputmode="numeric"
          autocomplete="off"
          placeholder="如 2026001"
        />
        <label class="sa__label">姓名</label>
        <input
          v-model="bindForm.name"
          class="sa__input"
          autocomplete="off"
          placeholder="如 张三"
        />
        <div v-if="bindError" class="sa__binderr">{{ bindError }}</div>
        <button
          class="sa__btn sa__btn--primary"
          :disabled="binding"
          @click="handleBind"
        >
          {{ binding ? '正在进入…' : '进入答题' }}
        </button>
        <p class="zf-dim sa__hint">
          学号与姓名都要填对——只填学号的话，
          别人用你的学号就能替你答题。
        </p>
      </div>
    </div>

    <!-- ── 交卷完成 ── -->
    <div v-else-if="phase === 'done'" class="sa__center">
      <div class="sa__ok">✓</div>
      <h2 class="sa__title">已交卷</h2>
      <p class="zf-muted sa__msg">
        {{ studentName ? studentName + '，' : '' }}你的作答已提交，可以关闭页面了。
      </p>
      <p class="zf-dim sa__hint">成绩由老师批改后公布。</p>
    </div>

    <!-- ── 答题 ── -->
    <template v-else-if="phase === 'answering' && current">
      <header class="sa__bar">
        <div class="sa__barleft">
          <span class="sa__prog">{{ index + 1 }} / {{ total }}</span>
          <span class="zf-dim">已答 {{ answeredCount }}</span>
        </div>
        <div class="sa__barright" :class="{ 'sa__barright--urgent': remain < 300000 }">
          <span class="zf-dim">剩余</span> {{ timeText }}
        </div>
      </header>

      <div v-if="overtime" class="sa__overtime">
        已到截止时间，请立即交卷（老师可设置是否接受迟交）。
      </div>

      <main class="sa__q">
        <div class="sa__qmeta">
          <span class="sa__badge">{{ S.typeLabel(current.type) }}</span>
          <span class="zf-dim">{{ Number(current.score).toFixed(0) }} 分</span>
          <span v-if="S.isMultiSelect(current.type)" class="sa__multi">
            多选
          </span>
        </div>

        <!-- ★ 题干用后端给的 `stem_html`（快照）。含图题本轮不渲染配图。
             ★★ 走 MathText 而不是 v-html：公式要在这里也渲染出来，
                否则会出现「老师在题库里看到的是分数、学生在答题页看到的是
                `$\frac{1}{2}$`」这种最尴尬的不一致。 -->
        <MathText tag="div" class="sa__stem" :source="current.stem_html" />

        <!-- 选项题 -->
        <div v-if="S.isOptionType(current.type)" class="sa__opts">
          <button
            v-for="o in current.options"
            :key="o.position_label"
            class="sa__opt"
            :class="{ 'sa__opt--on': isPicked(current, o.position_label) }"
            @click="pickOption(current, o.position_label)"
          >
            <!-- ★ 显示 `sheet_label`（判断题印 T/F），提交 `position_label`（A/B 位置） -->
            <span class="sa__optlabel">{{ o.sheet_label }}</span>
            <MathText
              class="sa__opttext"
              :source="o.content_html || o.content_text"
            />
          </button>
          <p v-if="!current.options.length" class="sa__noopt">
            这道题没有选项数据，请联系老师（题号 {{ current.sheet_seq }}）
          </p>
        </div>

        <!-- 填空题 -->
        <div v-else class="sa__blanks">
          <div
            v-for="(_, i) in answers[current.sheet_seq]?.blanks ?? []"
            :key="i"
            class="sa__blank"
          >
            <span class="sa__blankno">第 {{ i + 1 }} 空</span>
            <input
              v-model="answers[current.sheet_seq].blanks[i]"
              class="sa__input"
              autocomplete="off"
              :placeholder="`请输入第 ${i + 1} 空的答案`"
            />
          </div>
          <p class="zf-dim sa__hint">
            按空的顺序填写。不会的空可以跳过，不要填错位。
          </p>
        </div>
      </main>

      <footer class="sa__foot">
        <button
          class="sa__btn"
          :disabled="index === 0"
          @click="index = Math.max(0, index - 1)"
        >
          上一题
        </button>
        <button
          v-if="index < total - 1"
          class="sa__btn sa__btn--primary"
          @click="index = Math.min(total - 1, index + 1)"
        >
          下一题
        </button>
        <button
          v-else
          class="sa__btn sa__btn--primary"
          :disabled="submitting"
          @click="handleSubmit"
        >
          交卷
        </button>
      </footer>

      <!-- ★ 答题卡：一屏一题时必须能一眼看到「哪些没答」 -->
      <div class="sa__card">
        <div class="sa__cardhead">
          <span>答题卡</span>
          <span class="zf-dim">{{ answeredCount }}/{{ total }}</span>
        </div>
        <div class="sa__grid">
          <button
            v-for="(q, i) in questions"
            :key="q.sheet_seq"
            class="sa__cell"
            :class="{
              'sa__cell--on': i === index,
              'sa__cell--done': (answers[q.sheet_seq]?.labels.length ?? 0) > 0
                || (answers[q.sheet_seq]?.blanks.some((x) => x.trim()) ?? false),
            }"
            @click="index = i"
          >
            {{ i + 1 }}
          </button>
        </div>
        <div class="sa__cardfoot">
          <span v-if="savedTip" class="zf-dim">{{ savedTip }}</span>
          <button
            class="sa__btn sa__btn--sm"
            :disabled="submitting"
            @click="saveProgress"
          >
            保存进度
          </button>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* ★ 学生端是**手机优先**：所有尺寸按手指操作定，
   触控目标不小于 48px。 */
.sa {
  min-height: 100vh;
  background: var(--zf-bg-page);
  padding-bottom: 220px; /* 给底部按钮 + 答题卡留位置 */
}

.sa__center,
.sa__bind {
  max-width: 420px;
  margin: 0 auto;
  padding: 48px var(--zf-space-5);
  text-align: center;
}
.sa__bind {
  text-align: left;
}

.sa__title {
  margin: var(--zf-space-3) 0 var(--zf-space-1);
  font-size: var(--zf-text-xl);
  font-weight: var(--zf-weight-semibold);
  color: var(--zf-text-primary);
}
.sa__msg {
  margin: 0;
  font-size: var(--zf-text-sm);
  line-height: var(--zf-leading-loose);
}
.sa__hint {
  margin-top: var(--zf-space-3);
  font-size: var(--zf-text-xs);
  line-height: var(--zf-leading-loose);
}

.sa__err,
.sa__ok {
  width: 56px;
  height: 56px;
  margin: 0 auto;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 26px;
  font-weight: var(--zf-weight-semibold);
}
.sa__err {
  background: var(--zf-danger-bg);
  color: var(--zf-danger);
}
.sa__ok {
  background: var(--zf-success-bg);
  color: var(--zf-success);
}

.sa__closed {
  margin: var(--zf-space-4) 0;
  padding: var(--zf-space-3);
  border-radius: var(--zf-radius-md);
  background: var(--zf-warning-bg);
  color: #7a4a00;
  font-size: var(--zf-text-xs);
  line-height: var(--zf-leading-loose);
}

.sa__form {
  margin-top: var(--zf-space-5);
}
.sa__label {
  display: block;
  margin: var(--zf-space-4) 0 var(--zf-space-1);
  font-size: var(--zf-text-sm);
  color: var(--zf-text-secondary);
}
/* ★ 输入框 52px：手指点得准 */
.sa__input {
  width: 100%;
  height: 52px;
  padding: 0 var(--zf-space-4);
  font-size: 16px; /* ★ ≥16px 否则 iOS 聚焦时会自动放大页面 */
  border: 1px solid var(--zf-border-strong);
  border-radius: var(--zf-radius-sm);
  background: var(--zf-bg-surface);
  color: var(--zf-text-primary);
  outline: none;
  box-sizing: border-box;
}
.sa__input:focus {
  border-color: var(--zf-brand);
  box-shadow: 0 0 0 3px var(--zf-brand-subtle);
}
.sa__binderr {
  margin-top: var(--zf-space-3);
  padding: var(--zf-space-2) var(--zf-space-3);
  border-radius: var(--zf-radius-sm);
  background: var(--zf-danger-bg);
  color: var(--zf-danger);
  font-size: var(--zf-text-xs);
  line-height: var(--zf-leading-normal);
}

.sa__btn {
  min-height: 52px;
  padding: 0 var(--zf-space-5);
  border: 1px solid var(--zf-border-strong);
  border-radius: var(--zf-radius-sm);
  background: var(--zf-bg-surface);
  color: var(--zf-text-primary);
  font-size: 16px;
  cursor: pointer;
}
.sa__btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.sa__btn--primary {
  background: var(--zf-brand);
  border-color: var(--zf-brand);
  color: var(--zf-text-inverse);
}
.sa__btn--sm {
  min-height: 40px;
  font-size: var(--zf-text-sm);
  padding: 0 var(--zf-space-4);
}
.sa__form .sa__btn {
  width: 100%;
  margin-top: var(--zf-space-5);
}

/* ── 顶部信息条 ── */
.sa__bar {
  position: sticky;
  top: 0;
  z-index: 5;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--zf-space-3) var(--zf-space-4);
  background: var(--zf-bg-surface);
  border-bottom: 1px solid var(--zf-border);
}
.sa__barleft,
.sa__barright {
  display: flex;
  align-items: baseline;
  gap: var(--zf-space-2);
  font-size: var(--zf-text-sm);
}
.sa__prog {
  font-weight: var(--zf-weight-semibold);
  font-size: var(--zf-text-md);
}
.sa__barright {
  font-variant-numeric: tabular-nums;
}
.sa__barright--urgent {
  color: var(--zf-danger);
  font-weight: var(--zf-weight-semibold);
}
.sa__overtime {
  padding: var(--zf-space-3) var(--zf-space-4);
  background: var(--zf-danger-bg);
  color: var(--zf-danger);
  font-size: var(--zf-text-sm);
}

/* ── 题目区 ── */
.sa__q {
  padding: var(--zf-space-5) var(--zf-space-4);
}
.sa__qmeta {
  display: flex;
  align-items: center;
  gap: var(--zf-space-2);
  margin-bottom: var(--zf-space-3);
  font-size: var(--zf-text-xs);
}
.sa__badge {
  padding: 2px var(--zf-space-2);
  border-radius: var(--zf-radius-xs);
  background: var(--zf-brand-subtle);
  color: var(--zf-brand);
}
.sa__multi {
  padding: 2px var(--zf-space-2);
  border-radius: var(--zf-radius-xs);
  background: var(--zf-warning-bg);
  color: var(--zf-warning);
}
.sa__stem {
  font-size: 17px;
  line-height: 1.75;
  color: var(--zf-text-primary);
  word-break: break-word;
}
/* 题干里的选项文本（题库把 A.xxx B.xxx 混在题干里）*/
.sa__stem :deep(p) {
  margin: 0 0 var(--zf-space-3);
}

.sa__opts {
  display: flex;
  flex-direction: column;
  gap: var(--zf-space-3);
  margin-top: var(--zf-space-5);
}
/* ★ 选项按钮：高 ≥ 56px，整行可点 */
.sa__opt {
  display: flex;
  align-items: flex-start;
  gap: var(--zf-space-3);
  width: 100%;
  min-height: 56px;
  padding: var(--zf-space-3) var(--zf-space-4);
  text-align: left;
  border: 1.5px solid var(--zf-border);
  border-radius: var(--zf-radius-md);
  background: var(--zf-bg-surface);
  font-size: 16px;
  line-height: 1.6;
  cursor: pointer;
  transition: border-color 0.12s, background 0.12s;
}
.sa__opt--on {
  border-color: var(--zf-brand);
  background: var(--zf-brand-subtle);
}
.sa__optlabel {
  flex: none;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--zf-bg-hover);
  font-weight: var(--zf-weight-semibold);
  font-size: var(--zf-text-sm);
}
.sa__opt--on .sa__optlabel {
  background: var(--zf-brand);
  color: var(--zf-text-inverse);
}
.sa__opttext {
  flex: 1;
  word-break: break-word;
}
/* 选项数据缺失时的兜底提示（不该出现；出现说明题目数据有问题） */
.sa__noopt {
  color: var(--zf-text-tertiary, #909399);
  font-size: var(--zf-font-sm, 13px);
}

.sa__blanks {
  margin-top: var(--zf-space-5);
}
.sa__blank {
  margin-bottom: var(--zf-space-4);
}
.sa__blankno {
  display: block;
  margin-bottom: var(--zf-space-1);
  font-size: var(--zf-text-xs);
  color: var(--zf-text-secondary);
}

/* ── 底部按钮（固定）── */
.sa__foot {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 6;
  display: flex;
  gap: var(--zf-space-3);
  padding: var(--zf-space-3) var(--zf-space-4);
  background: var(--zf-bg-surface);
  border-top: 1px solid var(--zf-border);
}
.sa__foot .sa__btn {
  flex: 1;
}

/* ── 答题卡 ── */
.sa__card {
  margin: var(--zf-space-5) var(--zf-space-4);
  padding: var(--zf-space-4);
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-md);
  background: var(--zf-bg-surface);
}
.sa__cardhead,
.sa__cardfoot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: var(--zf-text-sm);
}
.sa__cardfoot {
  margin-top: var(--zf-space-3);
}
.sa__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(44px, 1fr));
  gap: var(--zf-space-2);
  margin-top: var(--zf-space-3);
}
.sa__cell {
  height: 44px;
  border: 1px solid var(--zf-border);
  border-radius: var(--zf-radius-sm);
  background: var(--zf-bg-surface);
  font-size: var(--zf-text-sm);
  font-variant-numeric: tabular-nums;
  cursor: pointer;
}
.sa__cell--done {
  background: var(--zf-success-bg);
  border-color: var(--zf-success);
  color: var(--zf-success);
}
.sa__cell--on {
  outline: 2px solid var(--zf-brand);
  outline-offset: 1px;
  font-weight: var(--zf-weight-semibold);
}
</style>
