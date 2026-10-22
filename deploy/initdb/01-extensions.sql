-- 知否 · PostgreSQL 初始化脚本
--
-- ★★ 为什么需要这个文件：
--
--   1. **`vector` 扩展必须在建表前存在**
--      Django 的迁移里用 `VectorField`，而 `CREATE EXTENSION` 不是
--      Django 迁移能可靠做的事（它需要超级用户，且时机在建库之后、
--      迁移之前）。放在 `docker-entrypoint-initdb.d/` 是官方支持的
--      时机：容器**首次**初始化数据目录时执行。
--
--      ★ 注意「首次」：这个目录里的脚本**只在空数据目录时执行一次**。
--        已有数据卷时改这里不会生效——需要 `down -v` 重建，
--        或手工 `CREATE EXTENSION`。
--
--   2. **`pg_trgm`** 用于题干模糊查重（`QUESTION.DUPLICATE` 的
--      相似题检索），`unaccent` 用于中英混排归一。
--
--   3. **`uuid-ossp`** 供 `uuid_generate_v4()` 使用（本项目的
--      `uuid` 字段目前由 Django 侧生成，但导出/脚本里可能用到）。
--
--   4. **时区与排序**：容器的 `TZ=UTC`，而业务时区是 Asia/Shanghai
--      （README 第八节：时间一律 ISO 8601 带时区）。
--      排序用 `C` 是为了让 `ORDER BY` 稳定可预测；
--      中文排序靠应用层（按拼音/编码），不依赖数据库 collation。

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS btree_gin;

-- ★ 确认扩展真的装上了：initdb 脚本出错时容器会启动失败，
--   而「静默没装」会让第一次迁移在一个难懂的地方炸掉。
DO $$
DECLARE
    missing text;
BEGIN
    SELECT string_agg(e, ', ') INTO missing
    FROM unnest(ARRAY['vector', 'pg_trgm', 'uuid-ossp', 'btree_gin']) AS e
    WHERE NOT EXISTS (
        SELECT 1 FROM pg_extension WHERE extname = e
    );
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION '知否初始化失败：缺少扩展 %', missing;
    END IF;
    RAISE NOTICE '知否初始化完成：vector + pg_trgm + uuid-ossp + btree_gin 就绪';
END
$$;
