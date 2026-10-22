"""用户、认证、JWT、角色 · 只读查询。

★ 禁止写操作。返回 QuerySet 或 dataclass。
★ 取数必须带机构：`Model.objects.for_org(org_id)`。
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.accounts.models import User


class UserSelector:
    """用户查询。"""

    @staticmethod
    def list_users(
        org_id: int,
        *,
        roles: list[str] | None = None,
        keyword: str = "",
        is_active: bool | None = None,
        include_students: bool = False,
    ) -> QuerySet[User]:
        """机构内用户列表（**用户管理页用**）。

        ★ 默认**不含学生身份行**（`role=STUDENT`）：它们不是登录账号，
          一个班 45 人、一所学校上千个，混进来会把「成员管理」冲垮。
          要管学生请走「班级与名册」页（`ClassroomService`）。

        ★ 用 `_base_manager` 而不是 `objects`：`User` 继承自
          Django 的 `AbstractBaseUser`，其自定义 manager 可能
          不带软删过滤。这里显式排除已删除。
        """
        from apps.common.permissions import STAFF_ROLES

        qs = User._base_manager.filter(org_id=org_id,
                                       deleted_at__isnull=True)
        if roles:
            qs = qs.filter(role__in=roles)
        elif not include_students:
            qs = qs.filter(role__in=list(STAFF_ROLES))
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if keyword:
            # ★ 姓名 + 手机号 + 用户名三处匹配：老师可能按任一项找人
            from django.db.models import Q

            qs = qs.filter(
                Q(real_name__icontains=keyword)
                | Q(username__icontains=keyword)
                | Q(phone__icontains=keyword)
            )
        return qs.order_by("real_name", "username")

    @staticmethod
    def get(org_id: int, user_id: int) -> User | None:
        return User._base_manager.filter(
            pk=user_id, org_id=org_id, deleted_at__isnull=True).first()

    @staticmethod
    def by_username(username: str) -> User | None:
        """按用户名取（**登录用，不带机构过滤**）。

        ★ 登录时还不知道用户属于哪个机构，所以这里不能带
          `org_id` 过滤。这是全项目**唯一**允许跨机构取用户的地方，
          因此它只用于登录校验，绝不能用于任何「列数据」的场景。
        """
        return User._base_manager.filter(
            username=username, deleted_at__isnull=True).first()

    @staticmethod
    def by_phone(phone: str) -> User | None:
        """按手机号取（登录用）。同上，仅用于认证。"""
        if not phone:
            return None
        return User._base_manager.filter(
            phone=phone, deleted_at__isnull=True).first()

    @staticmethod
    def me(user_id: int) -> User | None:
        """当前登录用户（不按机构过滤——他自己就是那个机构的人）。"""
        return User._base_manager.filter(
            pk=user_id, deleted_at__isnull=True).first()

    @staticmethod
    def count_by_role(org_id: int) -> dict[str, int]:
        from django.db.models import Count

        rows = (
            User._base_manager.filter(org_id=org_id,
                                      deleted_at__isnull=True)
            .values("role").annotate(n=Count("id"))
        )
        return {r["role"]: r["n"] for r in rows}
