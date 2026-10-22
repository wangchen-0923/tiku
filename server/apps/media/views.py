"""资源（asset）与对象存储 · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。

★ 本文件只实现「取原始字节」这一件事：
  `GET /api/v1/media/assets/{id}/content/`。

  ★ 前端拿 `asset_id` 后必须**用已认证的 fetch 取字节**再
    `URL.createObjectURL`，不能直接 `<img src="/api/v1/media/...">` ——
    `<img>` 不带 `Authorization` 头，会 401（JWT 存在 localStorage）。
"""

from __future__ import annotations

from django.http import HttpResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.common.exceptions import ErrorCode, ZfError
from apps.common.permissions import IsTeacherAccount


class AssetContentView(APIView):
    """`GET /api/v1/media/assets/{id}/content/` 取资源**原始字节**。

    ★ 认证 + **本机构校验**：`Asset.objects.for_org()` 过滤，
      跨机构取图返回 404（不泄露「该 id 存在」）。

    ★ `Cache-Control: private, max-age=300`：
      图按 `asset_id` 缓存，前端离开页面时统一 `revokeObjectURL`。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int):
        org_id = getattr(request.user, "org_id", None)
        if not org_id:
            raise ZfError(ErrorCode.AUTH_FORBIDDEN,
                          detail="当前账号未绑定机构，无法访问资源")

        from apps.media.models import Asset
        from apps.media.services import MediaService

        asset = Asset.objects.for_org(int(org_id)).filter(pk=pk).first()
        if asset is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="资源不存在")
        try:
            data = MediaService.read_bytes(org_id=int(org_id), asset_id=pk)
        except ZfError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ZfError(
                ErrorCode.COMMON_NOT_FOUND,
                detail=f"资源字节读取失败：{exc}",
            ) from exc

        resp = HttpResponse(
            data, content_type=asset.mime or "application/octet-stream")
        resp["Cache-Control"] = "private, max-age=300"
        resp["Content-Length"] = str(len(data))
        return resp
