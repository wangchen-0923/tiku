"""回填 `question.has_figure` / `figure_count`。

★ 为什么要这个迁移：

  `has_figure` 与 `figure_count` 是**反规范化缓存**字段，用于
  `with_figures()` 查询与题库统计，避免每次 join 配图表。
  但历史上**没有任何写路径维护它们**——建题时不写、挂图时不写，
  于是全部恒为 `False` / `0`。

  后果：`Question.with_figures()` 永远返回空集。
  （`referenced_figure_missing` 也依赖它，但已在模型层改为实时查询，
   不再受缓存影响。）

★ 本迁移把存量数据按配图表回填一次。此后由
  `Question.sync_figure_counters()` 在各写路径维护。

★ 用 `RunPython` 而不是 SQL：需要按 `deleted_at IS NULL` 过滤，
  用 ORM 表达更清楚且可跨数据库。
"""

from __future__ import annotations

from django.db import migrations


def backfill(apps, schema_editor):
    """按配图表回填两个缓存字段。"""
    Question = apps.get_model("questionbank", "Question")
    QuestionImage = apps.get_model("questionbank", "QuestionImage")

    # 统计每个题的未删除配图数
    counts: dict[int, int] = {}
    rows = (
        QuestionImage.objects.filter(deleted_at__isnull=True)
        .values_list("question_id", flat=True)
    )
    for qid in rows:
        counts[qid] = counts.get(qid, 0) + 1

    # 先全部归零（处理「缓存说有图但实际没有」的反向漂移）
    Question.objects.filter(has_figure=True).update(has_figure=False)
    Question.objects.filter(figure_count__gt=0).update(figure_count=0)

    for qid, n in counts.items():
        Question.objects.filter(pk=qid).update(has_figure=True, figure_count=n)


def noop(apps, schema_editor):
    """回滚：缓存字段无副作用，不回退（回退反而会引入错误值）。"""


class Migration(migrations.Migration):
    dependencies = [
        ("questionbank",
         "0002_alter_question_created_at_alter_question_deleted_at_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
