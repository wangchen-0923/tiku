/**
 * 知否 · 教师端 Web 入口。
 *
 * ★ 样式加载顺序**有意为之**，不要调换：
 *     1. `tokens.css`   —— CSS 变量。必须最先，后面全靠它
 *     2. `reset.css`    —— 去掉浏览器默认值（★ 手机端不溢出的前提）
 *     3. `zf-base.css`  —— body / .zf-num 等全局基础
 *     4. Element Plus   —— 组件库样式
 *     5. `element-theme.css` —— 用令牌覆盖 Element Plus 的默认值
 *     6. `app.css`      —— 本项目自有样式
 *
 *   若把 Element Plus 放在 tokens 之前，主题覆盖会失效
 *   （同为单类选择器时后加载者胜）。
 *
 *   ★ reset 必须排在 Element Plus **之前**：它是「去掉默认值」，
 *     排在后面会把 Element Plus 自己的间距也重置掉。
 */

import { createPinia } from 'pinia'
import { createApp } from 'vue'

import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'

import App from './App.vue'
import router from './router'

import './styles/tokens.css'
import './styles/reset.css'
import './styles/zf-base.css'
import 'element-plus/dist/index.css'
// ★ KaTeX 样式（含字体）：必须紧跟 Element Plus 之后、主题覆盖之前。
//   字体走包内 `dist/fonts/`，由 Vite 打包成同源静态资源 ——
//   ★ 不要改成 CDN：校内网络常无外网，公式会退化成方框。
import 'katex/dist/katex.min.css'
import './styles/element-theme.css'
import './styles/app.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
// ★ 中文 locale：不设的话日期选择器、分页器都是英文
app.use(ElementPlus, { locale: zhCn })
app.mount('#app')
