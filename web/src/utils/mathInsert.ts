/**
 * 公式插入的「当前活动输入框」注册表。
 *
 * ══════════════════════════════════════════════════════════
 *  为什么需要这个注册表
 * ══════════════════════════════════════════════════════════
 *
 * 一道选择题有 4–6 个选项，每个选项都要能写公式
 * （`A. $\pi r^2$` 这种）。如果每个输入框各配一条公式工具条，
 * 一个抽屉里会出现 8 条工具条 —— 视觉噪音比功能价值大得多。
 *
 * 所以：**页面级只放一条工具条**，它作用于「最后一次获得焦点的
 * 那个公式输入框」。
 *
 * ══════════════════════════════════════════════════════════
 *  前提：工具条按钮必须 `mousedown.prevent`
 * ══════════════════════════════════════════════════════════
 *
 * 点工具条按钮时浏览器会先 `blur` 文本框 —— 一旦失焦，
 * `selectionStart/End` 就归零了，「把选中内容包成 `$…$`」直接失效。
 *
 * 在按钮上 `@mousedown.prevent` 能阻止这次焦点迁移，
 * 文本框保持聚焦、光标位置不变。
 * ★ 只用 `@click` 不行，`click` 永远发生在 `blur` 之后。
 *
 * 这也是注册表**不清除**失焦目标的原因：万一有别的元素拿走了焦点
 * （比如点了抽屉外面），工具条仍要对最后一次操作的输入框生效。
 *
 * ══════════════════════════════════════════════════════════
 *  为什么用 ref 而不是普通变量
 * ══════════════════════════════════════════════════════════
 *
 * 工具条要按「当前有没有聚焦的输入框」决定按钮是否置灰。
 * 普通变量不是响应式的，`computed` 会永远缓存第一次的 `null`，
 * 按钮就一直是灰的。所以用 `shallowRef` 承载目标本身
 * （目标内部的方法不需要被追踪，只需要「有/没有」发生变化）。
 */

import { computed, shallowRef, type ComputedRef } from 'vue'

export interface MathTarget {
  /** 插入片段；`§` 表示「选中内容 / 光标位置」 */
  insert(snippet: string, wrap?: 'inline' | 'block'): void
  /** 去掉选中范围内的 `$` 定界符 */
  strip(): void
  /** 调试/提示用：这是哪个字段 */
  label: string
}

const active = shallowRef<MathTarget | null>(null)

/** 输入框获得焦点时登记自己为活动目标 */
export function setActiveMathTarget(t: MathTarget | null): void {
  if (t) active.value = t
}

/** 组件卸载时**只**在自己仍是活动目标时清空，避免误清别的字段 */
export function clearMathTarget(t: MathTarget): void {
  if (active.value === t) active.value = null
}

export function getActiveMathTarget(): MathTarget | null {
  return active.value
}

/** 响应式地读「当前有没有活动输入框」（工具条用它控制置灰） */
export function useHasMathTarget(): ComputedRef<boolean> {
  return computed(() => active.value !== null)
}

/** 往活动目标插公式；返回 `false` 表示当前没有聚焦的公式输入框 */
export function insertMath(
  snippet: string,
  wrap?: 'inline' | 'block',
): boolean {
  if (!active.value) return false
  active.value.insert(snippet, wrap)
  return true
}

/** 去掉活动目标选区内的定界符 */
export function stripMathDelimiters(): boolean {
  if (!active.value) return false
  active.value.strip()
  return true
}
