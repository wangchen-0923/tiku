/// <reference types="vite/client" />

/**
 * `*.vue` 单文件组件的类型声明。
 *
 * ★ 为什么需要它：TypeScript 不认识 `.vue` 后缀，没有这份声明
 *   就会在 `import App from './App.vue'` 上报
 *   「找不到模块」。（`vue-tsc` 自带处理，但编辑器的 TS 语言
 *   服务在部分配置下仍会报错，写上更稳妥。）
 */
declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<object, object, unknown>
  export default component
}
