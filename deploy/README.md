# 本地环境启动（唯一入口）

> 2026-10-18 统一：本项目**开发库固定用 PostgreSQL 15433**，Redis 固定 16380。
> 现场容器名：`tiku_postgres_15433`、`tiku_redis`；数据卷：`tiku_pg_data`、`tiku_redis_data`。
>
> **禁止 `docker compose down -v`**，也**不要**启动名为 `tiku_postgres` 的旧容器
> （它与 15433 容器挂同一个卷，两个 Postgres 写同一数据目录会损坏数据）。
> 该旧容器已删除，仅保留数据卷。

## 1. 启动数据库（唯一方式）

```powershell
cd D:\tiku
docker compose -f deploy/compose.db.yml up -d
docker compose -f deploy/compose.db.yml ps
```

首次把现场 `docker run` 容器切换成 compose 管理时（**只删容器，不删卷**）：

```powershell
docker stop tiku_postgres_15433 tiku_redis
docker rm tiku_postgres_15433 tiku_redis
docker compose -f deploy/compose.db.yml up -d
```

> 数据在 `tiku_pg_data` / `tiku_redis_data` 卷里，删容器不会丢数据。
> 不要执行 `docker volume rm`，不要执行 `down -v`。

## 2. 后端环境变量（PostgreSQL，推荐）

```powershell
$env:ZF_DB_HOST="127.0.0.1"
$env:ZF_DB_PORT="15433"
$env:ZF_DB_NAME="zhifou"
$env:ZF_DB_USER="zhifou"
$env:ZF_DB_PASSWORD="zhifou"
Remove-Item Env:\ZF_DB_ENGINE -ErrorAction SilentlyContinue
$env:ZF_REDIS_URL="redis://127.0.0.1:16380/0"
# 跑测试/脚本时建议 LocMem，避免限流计数跨测试残留；要验证 Redis 时去掉
$env:ZF_CACHE="locmem"
$env:PYTHONPATH="D:\tiku\.devdeps;D:\tiku\server"
$py="C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
```

或直接点源脚本（端口已写死为 15433/16380）：

```powershell
powershell -ExecutionPolicy Bypass -Command ". .\deploy\use-postgres.ps1; cd server; & $py manage.py migrate"
```

Git Bash / CI 下的等价写法（**每次开新终端都要带**）：

```bash
export ZF_DB_PORT=15433 ZF_DB_PASSWORD=zhifou
export PYTHONPATH="D:/tiku/.devdeps;D:/tiku/server"
```

> ★★ **漏掉 `ZF_DB_PORT` 时的报错极具误导性**：
> `base.py` 的缺省端口是 **5432**（生产容器内部端口），于是 PG 回的是
> `FATAL: password authentication failed for user "zhifou"`。
> 看起来是「密码配错了」，实际是**连错了端口**——真机上 5432 上没有这个库。
> 遇到这个报错先核对端口，不要先去改密码。
> （2026-10-23 实际踩过一次，排查了半小时。）

## 3. 迁移 + 启动

```powershell
cd D:\tiku\server
& $py scripts\check_env.py --require-postgres   # 端口不通会直接失败
& $py manage.py migrate
& $py manage.py runserver 127.0.0.1:8001
```

启动时会打印一行：

```
[ENV] DB=postgres:127.0.0.1:15433/zhifou | REDIS=redis://127.0.0.1:16380/0 | MEDIA=D:\tiku\server\media
```

`scripts/check_env.py` 在 PostgreSQL 模式下会先探测端口；若不通会提示
`ZF_DB_PORT=15433`（5432 是另一个项目 `question-bank-pg` 的库）。

## 4. SQLite 回退（仅开发/自检）

SQLite **不是**正式开发库，只用于迁移生成/无数据库环境自检：

```powershell
$env:ZF_DB_ENGINE="sqlite"
Remove-Item Env:\ZF_DB_HOST,Env:\ZF_DB_PORT,Env:\ZF_DB_NAME,Env:\ZF_DB_USER,Env:\ZF_DB_PASSWORD -ErrorAction SilentlyContinue
$env:ZF_CACHE="locmem"
$env:ZF_REDIS_URL="redis://127.0.0.1:16380/0"
cd D:\tiku\server
python manage.py migrate
python manage.py runserver 127.0.0.1:8001
```

SQLite 模式下**不要再起任何 compose**。

## 5. 数据归属结论（2026-10-18）

| 库 | 内容 | 用途 |
| --- | --- | --- |
| PostgreSQL 15433 | 28 份试卷、4 个作业、48 条作答、1946 道题 | **正式开发库** |
| SQLite `server/db.sqlite3.dev-only` | 0 份试卷、0 个作业、0 条作答；仅有 2026-10-18 调试时建的 4 张答题卡 | 仅作测试/备份，不启动服务 |

SQLite 的答题卡已导出到
`artifacts/backup/sheet-sqlite-20261018.json`，数据库文件备份在
`artifacts/backup/db.sqlite3.dev-only.20261018-1920.bak`。
**没有删除任何数据**；如需把 SQLite 的卡迁到 Postgres，另行确认后执行。

---

## 6. 公式渲染服务（可选，但印刷级公式需要它）

计划 §3.2「公式导出走 B」的实现。**不启动它系统也能导出 PDF** ——
会自动回退到 reportlab，公式降级成近似文本（`1/2`、`x²`），
并在导出结果里返回 `PAPER.FORMULA_DEGRADED`（非阻塞告警）。

```bash
# ① 共享密钥（与 Django 的 ZF_RENDER_TOKEN 必须一致）
export RENDER_TOKEN=$(openssl rand -hex 24)

# ② 起服务（首次会构建镜像，装 fonts-noto-cjk + Chromium，约 1.5GB）
docker compose -f deploy/compose.render.yml up -d --build

# ③ 验字体：必须 fonts.ok = true, leftover_relative_urls = 0
curl -s localhost:7001/health | python -m json.tool

# ④ 让 Django 用上它
set ZF_RENDER_URL=http://127.0.0.1:7001
set ZF_RENDER_TOKEN=%RENDER_TOKEN%
```

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `ZF_RENDER_URL` | 空（不启用） | 渲染服务地址 |
| `ZF_RENDER_TOKEN` | 空 | **内网共享密钥**，不是用户 JWT |
| `ZF_RENDER_TIMEOUT` | `35` | 比服务端 30s 多 5s，让服务端超时先触发 |

**三个部署坑（都在代码里有对应处理，但要知道为什么）**：

1. **中文字体**：镜像必须装 `fonts-noto-cjk`。不装时 Chromium 不报错，
   只是把中文画成方框 —— 这是「本地正常、线上全坏」的头号原因。
2. **公式字体离线**：KaTeX 的 20 个 woff2 全部内嵌成 base64。
   校内网络常无外网，指望 CDN 会得到退化字形。
3. **`/dev/shm`**：compose 里给了 `shm_size: 512mb`。容器默认 64MB，
   多页打印会以 `Target closed` 崩掉，而报错信息完全看不出是这个原因。

★ **必须与 U36（部署验收）合并验收**：真导一份含公式的卷子，
目视确认公式可缩放、中文不是方框、分页与前端预览一致。

---

## 7. 自定义 API Key 的加密密钥（`ZF_SECRET_KEK`）

用户在「积分与凭证」页自己填的 API Key，落库前用 AES-256-GCM 加密，密钥来自
`ZF_SECRET_KEK`。

```bash
# 生成（32 字节随机，base64）—— 只需生成一次
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
```

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `ZF_SECRET_KEK` | 空（回退 `SECRET_KEY` 派生） | base64 的 32 字节密钥 |

### ★★ 四条纪律（比配置本身重要）

1. **不能放代码库、不能放数据库、不能写进任何 `.env` 模板文件。**
   仓库是 public 的，放一次就永久留在 git 历史里。
2. **上线后不能更换。** 换了之后所有用户已保存的 Key 全部解不开，
   而且**没有补救办法**——GCM 没有"用旧密钥再解一次"的可能。
   用户只能重新填一遍。
3. **必须有独立备份。** 与数据库备份分开存：数据库备份能恢复密文，
   但没有 KEK 就永远解不开。建议放团队密码管理器（1Password / Bitwarden）
   的运维条目，条目名建议 `zhifou/PROD/ZF_SECRET_KEK`。
   **本条只记录"放在哪"，密钥值本身不进任何文档。**
4. **与 `ZF_PII_KEK` 分开。** 后者保护学生个人信息、理论上需定期轮换；
   前者保护用户 Key、几乎不可能轮换。混用一把密钥的后果是：
   为了轮换 PII 密钥，把所有用户的 API Key 一起弄废。

### 怎么确认线上配对了

登录后调 `GET /api/v1/billing/keys/`，响应里的 `data.kek_source`：

| 值 | 含义 |
| --- | --- |
| `env` | 已显式配置 `ZF_SECRET_KEK` ✅ |
| `derived` | **未配置**，正在用 `SECRET_KEY` 派生 ⚠️ 生产环境必须修 |

`derived` 在开发环境可用（重启后 `SECRET_KEY` 不变就能解开），
但生产环境一旦轮换 `SECRET_KEY`（例如重新部署时未固定），
所有已保存的 Key 立刻失效。
