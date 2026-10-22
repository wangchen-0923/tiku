# 知否 · 中职机械制造技术题库与精准作业平台

面向中职专业课教师的题库与作业系统。核心业务闭环：

```
命题 → 组卷 → 发布 → 答题（纸质扫描 / 在线） → 批改 → 学情分析
```

两条设计主线：**「知」**（求知：题目要讲得清）与 **「否」**（求真：判据要有据）。

---

## 一、技术栈

| 层 | 选型 |
| --- | --- |
| 后端 | Python 3.12 · Django 5.1 · DRF · PostgreSQL 16 · Redis |
| 前端 | Vue 3 · TypeScript · Vite · Element Plus · KaTeX |
| 文档渲染 | `render-service`：Node 20 · Express · Puppeteer · KaTeX SSR |
| 异步 | Celery（`worker_io` 扫描识别 / `worker_calc` 学情重算 / `beat`） |
| 部署 | Docker Compose · Nginx |

---

## 二、目录结构

```
server/           Django 后端（17 个 app）
web/              Vue 3 前端
render-service/   试卷 PDF 渲染服务（排版 JSON + KaTeX → Chromium → PDF）
scripts/          验收与运维脚本（verify_*.py 为主）
deploy/           部署编排、Nginx、上线手册
docs/             设计文档、技术文件集、开发进度
artifacts/        验收产物（不入库）
```

---

## 三、本地启动

### 1. 依赖环境

```bash
docker compose -f deploy/compose.db.yml up -d      # PostgreSQL 127.0.0.1:15433
docker compose -f deploy/compose.local.yml up -d   # Redis 127.0.0.1:16380
```

### 2. 后端（8001）

```bash
cd server
export PYTHONPATH="$PWD/../.devdeps:$PWD"
export DJANGO_SETTINGS_MODULE=zhifou.settings.local
export ZF_DB_ENGINE=pg ZF_DB_PORT=15433 ZF_DB_PASSWORD=zhifou ZF_CACHE=locmem

python manage.py migrate
python manage.py runserver 127.0.0.1:8001 --noreload
```

启动后**先看第一行** `[ENV] DB=postgres:127.0.0.1:15433/zhifou`。
如果不是 postgres，说明连到了 SQLite 空壳库，后面的现象都会像「数据全丢了」。

### 3. 前端（5173）

```bash
cd web
pnpm install
pnpm dev
```

### 4. 开发账号

口令**不写在源码里**（本仓库公开）。首次使用：

```bash
cp scripts/.devcreds.example scripts/.devcreds.local
# 然后填本机开发库的真实口令
```

详见 [docs/tech/README.md §5.20](docs/tech/README.md)。

---

## 四、验证

项目用「可执行验收脚本」代替部分单测，每个脚本自带断言与失败清单：

```bash
python scripts/verify_math_formula.py       # 公式：扫描/掩码/降级/宽度/折行
python scripts/verify_render_service.py     # PDF 渲染服务（真起服务、真出 PDF）
python scripts/verify_frontend_guard.py     # 路由守卫与角色等级（纯静态）
python scripts/verify_repo_hygiene.py       # 公开仓库卫生：密钥/大目录/构建产物
```

> ★ **推送前请跑 `verify_repo_hygiene.py`**。密钥一旦进 git 历史，
> 删文件没用，只能重写历史。

---

## 五、文档入口

| 文档 | 内容 |
| --- | --- |
| [docs/tech/README.md](docs/tech/README.md) | **技术文件集唯一起点**：命名、目录、状态枚举、错误码、编码规则 |
| [docs/知否-开发设计文档-v2.1.md](docs/知否-开发设计文档-v2.1.md) | 设计与取舍（为什么这么做） |
| [docs/七大模块-统一执行计划-v2.md](docs/七大模块-统一执行计划-v2.md) | 分批执行计划与出口判据 |
| [docs/开发进度记录.md](docs/开发进度记录.md) | 做到哪了、踩过哪些坑 |
| [deploy/README.md](deploy/README.md) | 上线手册 |

---

## 六、几条硬约束

1. **多租户隔离**：`tenant_id` / `org_id` 一律从 JWT 解析，**不接受请求头传递**。
2. **数据归属**：班级归教师个人（`Classroom.owner` 是隔离判据），不是学校资产。
3. **AI 调用**：业务层不直接调模型厂商 SDK，一律走 `apps/ai`，prompt 存表不落代码。
4. **公式**：库里存 TeX 原文（`$…$` / `$$…$$`），不存 KaTeX HTML。
5. **前端富文本**：只有 `MathText.vue` 一个出口，业务组件里**不允许**出现 `v-html`。
6. **密钥**：不入库。凭据走环境变量或 `scripts/.devcreds.local`。

---

## 七、许可

未声明开源许可证。如需引用或协作，请先联系仓库作者。
