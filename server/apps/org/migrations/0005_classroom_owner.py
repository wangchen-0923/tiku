"""classroom 加「归属教师」字段，并把历史班级回填给原班主任。

★★ 为什么拆成 0005 / 0006 两个迁移（不要合并）：

    PostgreSQL 在同一个事务里对**同一张表**先做 DML（UPDATE 回填）、
    再 ALTER TABLE ADD CONSTRAINT 时，会报

        cannot ALTER TABLE "classroom" because it has pending trigger events

    因为 UPDATE 的行还没提交，约束校验需要看到一致快照。
    拆成两次 `migrate` 就各自是独立事务，问题消失。

    ★ 这个坑在 `accounts/0005_three_tier` 已经踩过一次，同样的解法。
"""

from __future__ import annotations

from django.db import migrations, models
import django.db.models.deletion


def fill_owner_from_head_teacher(apps, schema_editor):
    """把历史班级的归属补给原班主任。

    ★ 只补 `head_teacher` 有值的那些。没有班主任的历史班保持无主
      （`owner=NULL`）—— 不猜、不随便挂到某个账号上：

        · 猜错的后果是「这个班莫名出现在某位老师的列表里」，
          他能改能删别人的班，比看不见严重得多
        · 无主班学校账户仍能看到并指派归属（`PATCH` 传 `owner_id`）

    ★ 分两步 UPDATE 而不是一条 CASE WHEN：可读性优先，
      260 个班的数据量下性能无差别。
    """
    Classroom = apps.get_model("org", "Classroom")

    Classroom.objects.filter(
        deleted_at__isnull=True,
        owner__isnull=True,
        head_teacher__isnull=False,
    ).update(owner=models.F("head_teacher_id"))


def unfill_owner(apps, schema_editor):
    """回滚：把「归属人 == 班主任」的那些清空。

    ★ 只清这一种，避免把 0006 之后人工指派的归属也一起抹掉。
    """
    Classroom = apps.get_model("org", "Classroom")
    Classroom.objects.filter(
        deleted_at__isnull=True,
        owner__isnull=False,
        head_teacher__isnull=False,
        owner=models.F("head_teacher"),
    ).update(owner=None)


class Migration(migrations.Migration):

    dependencies = [
        ("org", "0004_alter_classroom_created_at_and_more"),
        ("accounts", "0006_platform_admin_no_org"),
    ]

    operations = [
        migrations.AddField(
            model_name="classroom",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                help_text="创建者。教师只能看到自己创建的班级；学校账户可见本校全部",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="owned_classrooms",
                to="accounts.user",
                verbose_name="归属教师",
            ),
        ),
        migrations.RunPython(fill_owner_from_head_teacher, unfill_owner),
    ]
