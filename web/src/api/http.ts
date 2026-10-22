/**
 * 统一 HTTP 客户端。
 *
 * ★★ 后端所有响应都是同一个信封（`apps/common/responses.py`）：
 *
 *     { "code": "OK", "message": "", "data": <真实数据> }
 *
 *   所以这里**统一剥壳**：调用方拿到的直接是 `data`，
 *   不必每处都写 `res.data.data`。
 *
 * ★★ 两类「不是错误」的情况，必须分开处理：
 *
 *   1. **HTTP 4xx/5xx** —— 真的失败。抛 `ApiError`。
 *   2. **HTTP 200 但 `code != "OK"`** —— **业务分支**。
 *      例如 AI 生题在知识库不足时返回 200 + `code="AI.KB_INSUFFICIENT"`，
 *      后端用 `branch()` 明确表示「这是可预期的结果，不是异常」。
 *
 *   把第 2 类当成错误弹红框是错的——它需要的是**引导**
 *   （「请先补两条知识库条目」），不是「操作失败」。
 *   所以 `code != OK` 时抛 `BusinessBranch`，调用方可以
 *   `catch (e) { if (e instanceof BusinessBranch) ... }` 分支处理。
 */

export interface Envelope<T> {
  code: string
  message: string
  data: T
}

/** HTTP 层失败（4xx/5xx，或网络不可达）。 */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  /**
   * 响应体里的 `data`。
   *
   * ★★ 失败响应也可能带**结构化附加数据**——本项目的
   *    `_build_response(code, message, extra, status)` 把
   *    `extra` 放在 `data` 里。例如发布任务时名单有未绑定账号的学生，
   *    后端返回 400 + `data.unbound_student_nos=[...]`，
   *    前端要靠这个列表展示「是哪几个人」并引导去绑定。
   *
   *   ★ 丢掉它就只能给一句「发布失败」，而老师需要知道**差哪一步**。
   */
  readonly data: unknown

  constructor(status: number, code: string, message: string, data?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.data = data
  }

  /** 把 `data` 当对象读某个字段（安全，不做断言）。 */
  extra<T = unknown>(key: string): T | undefined {
    if (this.data && typeof this.data === 'object') {
      return (this.data as Record<string, unknown>)[key] as T | undefined
    }
    return undefined
  }
}

/** HTTP 200 但业务码不是 OK —— 可预期的分支，不是异常。 */
export class BusinessBranch<T = unknown> extends Error {
  readonly code: string
  readonly data: T
  constructor(code: string, message: string, data: T) {
    super(message)
    this.name = 'BusinessBranch'
    this.code = code
    this.data = data
  }
}

/** 未认证时抛这个，路由守卫据此跳登录。 */
export class Unauthorized extends ApiError {
  constructor(message = '登录状态已失效，请重新登录') {
    super(401, 'AUTH.UNAUTHENTICATED', message)
    this.name = 'Unauthorized'
  }
}

const TOKEN_KEY = 'zf.access_token'
const REFRESH_KEY = 'zf.refresh_token'

export const tokenStore = {
  get access(): string {
    return localStorage.getItem(TOKEN_KEY) ?? ''
  },
  get refresh(): string {
    return localStorage.getItem(REFRESH_KEY) ?? ''
  },
  set(access: string, refresh?: string): void {
    localStorage.setItem(TOKEN_KEY, access)
    if (refresh) localStorage.setItem(REFRESH_KEY, refresh)
  },
  clear(): void {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(REFRESH_KEY)
  },
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'
  body?: unknown
  /** 查询参数；`undefined` / `null` / `''` 会被丢掉 */
  query?: Record<string, string | number | boolean | undefined | null>
  /** ★ 登录接口本身不带 token，否则会把旧 token 一起发过去 */
  anonymous?: boolean
  /**
   * 额外请求头。
   *
   * ★ 学生端用 `X-Session-Token` 承载身份（学生没有 JWT），
   *   所以必须能透传自定义头。
   */
  headers?: Record<string, string>
  signal?: AbortSignal
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  // ★ 用相对路径：dev 由 Vite proxy 转发，生产由 Nginx 反代。
  //   这样**开发与生产的请求写法一致**，不会出现「本地能跑、
  //   上线 404」。
  const url = path.startsWith('/') ? path : `/api/v1/${path}`
  if (!query) return url
  const usp = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue
    usp.append(k, String(v))
  }
  const qs = usp.toString()
  return qs ? `${url}?${qs}` : url
}

/**
 * 发一次请求，返回**已剥壳**的 `data`。
 *
 * @throws {Unauthorized} 401
 * @throws {ApiError} 其它 HTTP 失败
 * @throws {BusinessBranch} HTTP 200 但业务码不是 OK
 */
export async function request<T = unknown>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = 'GET', body, query, anonymous, headers: extra, signal } =
    options
  // ★ FormData 必须原样发送，且**不能**手动设 Content-Type——
  //   边界串由浏览器生成；手动设成 application/json 会让后端收不到文件。
  const isForm = typeof FormData !== 'undefined' && body instanceof FormData
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined && !isForm) {
    headers['Content-Type'] = 'application/json'
  }
  if (!anonymous) {
    const t = tokenStore.access
    if (t) headers.Authorization = `Bearer ${t}`
  }
  // ★ 调用方给的头最后合并：允许覆盖默认值（如换 Content-Type）
  if (extra) Object.assign(headers, extra)

  let res: Response
  try {
    res = await fetch(buildUrl(path, query), {
      method,
      headers,
      body: body === undefined
        ? undefined
        : (isForm ? body : JSON.stringify(body)),
      signal,
    })
  } catch (e) {
    // ★ 网络层失败（后端没起、代理没配）—— 给一句能直接照做的话，
    //   而不是浏览器那句「Failed to fetch」。
    throw new ApiError(
      0,
      'NETWORK.UNREACHABLE',
      `连接不上后端（${String(e)}）。请确认后端已在 127.0.0.1:8001 运行。`,
    )
  }

  // 204 与空响应
  const text = await res.text()
  let payload: Envelope<T> | null = null
  if (text) {
    try {
      payload = JSON.parse(text) as Envelope<T>
    } catch {
      payload = null
    }
  }

  if (res.status === 401) {
    tokenStore.clear()
    throw new Unauthorized(payload?.message || undefined)
  }

  if (!res.ok) {
    throw new ApiError(
      res.status,
      payload?.code ?? `HTTP.${res.status}`,
      payload?.message || `请求失败（HTTP ${res.status}）`,
      // ★ 带上 `data`：失败响应里的结构化附加数据（如未绑定学号清单）
      payload?.data,
    )
  }

  if (payload === null) {
    // 200 但没有 JSON 信封：不是本项目的接口
    throw new ApiError(
      res.status,
      'RESPONSE.NOT_ENVELOPE',
      '后端返回的不是本项目约定的 JSON 信封',
    )
  }

  // ★ 业务分支：200 + code != OK
  if (payload.code && payload.code !== 'OK') {
    throw new BusinessBranch(payload.code, payload.message, payload.data)
  }

  return payload.data
}

/**
 * 取**二进制**响应（PDF / 图片等）。
 *
 * ★★ 为什么必须单独有这个函数，不能直接用 `<a href>` 或 `window.open`：
 *
 *   本项目的鉴权是 **JWT，存在 `localStorage`**（`tokenStore`），
 *   请求时由 `fetch` 加 `Authorization: Bearer` 头。
 *   而浏览器**打开新标签页 / 点 `<a>` 是不会带自定义头的**。
 *
 * ★ 现在也支持 **POST**（导出接口改 POST 后需要带 JSON body）。
 */
export interface BlobRequestOptions {
  query?: RequestOptions['query']
  /** 默认 GET；导出 / 排版 JSON 用 POST */
  method?: 'GET' | 'POST'
  /** POST 时的 JSON body */
  body?: unknown
  signal?: AbortSignal
}

export async function fetchBlob(
  path: string,
  options: BlobRequestOptions = {},
): Promise<Blob> {
  const { query, method = 'GET', body, signal } = options
  const headers: Record<string, string> = { Accept: '*/*' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const t = tokenStore.access
  if (t) headers.Authorization = `Bearer ${t}`

  let res: Response
  try {
    res = await fetch(buildUrl(path, query), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  } catch (e) {
    throw new ApiError(
      0,
      'NETWORK.UNREACHABLE',
      `连接不上后端（${String(e)}）。请确认后端已在 127.0.0.1:8001 运行。`,
    )
  }

  if (res.status === 401) {
    tokenStore.clear()
    throw new Unauthorized()
  }

  if (!res.ok) {
    // ★ 失败时后端会返回**正常 JSON 信封**（渲染器仍然生效），
    //   所以这里要把它解析出来，否则老师只看到「HTTP 404」
    //   而不知道是「PDF 尚未生成」还是别的。
    let code = `HTTP.${res.status}`
    let message = `请求失败（HTTP ${res.status}）`
    try {
      const j = (await res.json()) as Envelope<unknown>
      if (j?.code) code = j.code
      if (j?.message) message = j.message
    } catch {
      /* 不是 JSON 就沿用默认信息 */
    }
    throw new ApiError(res.status, code, message)
  }

  return await res.blob()
}

export const http = {
  get: <T>(path: string, query?: RequestOptions['query']) =>
    request<T>(path, { query }),
  post: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: 'POST', body }),
  /** multipart 上传（扫描件 PDF/图片）。 */
  upload: <T>(path: string, form: FormData, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: 'POST', body: form }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body }),
  /**
   * PUT。
   *
   * ★ 与 `PATCH` 的区别在语义：`PUT` 表示**整体替换/设定**该资源的值，
   *   `PATCH` 表示部分更新。
   *   本项目用到 PUT 的地方是「设/清某单个配置值」——
   *   例如 `PUT /papers/{id}/score-allocation/` 设「期望总分」，
   *   传 `null` 表示**清空**。
   *   ★ 用 `PATCH` 表达「清空」会有歧义：`null` 到底是
   *     「不改」还是「设为空」？`PUT` 没有这个歧义。
   */
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PUT', body }),
  /**
   * DELETE。
   *
   * ★ 支持两种传参，因为本项目**两种都有用到**：
   *
   *     `query`  有些删除接口用 query（如 `?item_id=`）
   *     `body`   有些用 body —— 例如 `DELETE /questions/{id}/`
   *              要读 `{"reason": "..."}` 记审计
   *
   *   ★ 当初只做了 `query`，于是「删除题目带原因」这句代码
   *     多传的第三个参数被**静默忽略**，原因永远记不进去。
   *     所以这里两个都开出来。
   */
  del: <T>(
    path: string,
    query?: RequestOptions['query'],
    body?: unknown,
  ) => request<T>(path, { method: 'DELETE', query, body }),
}
