"""资源（asset）与对象存储 · 业务写操作。

★ 分层职责（见 01 文档）：事务边界在这里；禁止解析 request、禁止返回 Response。

★★ 一条**铁律**（`media/models.py` 的模块 docstring）：

    **删除 `asset` 前必须 `ref_count = 0`。**

  同一张图可能被多道题、多份试卷快照、多个练习引用。
  提前清理会让**已发布试卷的配图变成死链**——
  而老师在考试前一天才发现，代价极高。

  所以本模块的 `delete_asset()` 在 `ref_count > 0` 时**拒绝删除并说明原因**，
  而不是「强制删掉再让引用方自己想办法」。

★ 三个入口：

    `register_upload()`      后端直传完成后登记 asset（前端调）
    `upload_directly()`      小文件走后端（≤500KB 的扫描小图）
    `presign_upload()`       签发预签名 URL（前端直传）

★ 两条**输入校验契约**（都是实测复现出缺陷后补上的，回归见
  `scripts/verify_media_upload_hardening.py`）：

    `storage.validate_upload(data=...)`  扩展名 × **内容魔数**必须一致，
                                         不一致在**写盘之前**拒绝
                                         （`MEDIA.CONTENT_TYPE_MISMATCH`），
                                         且大小按**实际字节**判定
    `resolve_signed_url_ttl()`           签名 URL 的 TTL 非法即拒
                                         （400），**不**静默钳制

★ 生命周期（00 文档 6.3）：

    SCAN_RAW       180 天
    EXPORT_ZIP     24 小时
    UPLOAD_SOURCE  与 ingest_batch 同寿（默认 2 年）
    QUESTION_FIGURE / PAPER_PDF / SHEET_PDF   永久
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.audit.services import audit_log
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.media.models import Asset
from apps.media.storage import (
    ALLOWED_KINDS,
    DEFAULT_MAX_BYTES,
    KIND_MAX_BYTES,
    SIGNED_URL_TTL_MAX_SECONDS,
    SIGNED_URL_TTL_MIN_SECONDS,
    ObjectInfo,
    StorageError,
    build_key,
    get_backend,
    is_safe_key,
    sha256_of,
    signed_url_ttl,
    validate_upload,
)

logger = logging.getLogger(__name__)

A_ASSET_CREATE = "asset.create"
A_ASSET_DELETE = "asset.delete"

#: 保留期（天）。`None` 表示永久（题库资产）。
RETAIN_DAYS: dict[str, int | None] = {
    "SCAN_RAW": 180,
    "EXPORT_ZIP": 1,           # 24 小时 → 1 天
    "UPLOAD_SOURCE": 730,      # 与 ingest_batch 同寿
    "SCAN_RECTIFIED": 180,
    "QUESTION_FIGURE": None,
    "PAPER_PDF": None,
    "SHEET_PDF": None,
    "REPORT_PDF": None,
    "KNOWLEDGE_ATTACH": None,
}

#: 走后端直传的大小上限（00 文档 6.2：≤500KB 可简化）
DIRECT_UPLOAD_MAX_BYTES = 500 * 1024


# ══════════════════════════════════════════════════════════════
#  签名 URL 的 TTL 判定
# ══════════════════════════════════════════════════════════════
def resolve_signed_url_ttl(ttl: Any | None = None) -> int:
    """把 TTL 收敛成一个合法秒数；**非法值直接拒绝**（不静默钳制）。

    契约
    ----
    - `ttl is None` → 用配置值 `ZF_OBJECT_STORAGE.signed_url_ttl_seconds`
      （默认 900 秒）。★ 配置值**同样**要落在合法区间内：一个运维手抖
      写上的 30 天默认值会被这里挡住，而不是悄悄签出 30 天的 URL。
    - 合法区间：`[SIGNED_URL_TTL_MIN_SECONDS, SIGNED_URL_TTL_MAX_SECONDS]`
      = `[1, 86400]`（上下限**含等号**）。理由见 `media/storage.py` 里那两
      个常量的注释。
    - 越界 / 非整数 / 布尔值 / 空串 → `COMMON.VALIDATION_ERROR`（HTTP 400）。

    ★★ 为什么是「拒绝」而不是「钳制」（这是一个**已实测复现**的缺陷）：

      补上它之前 `get_url()` 写的是 `ttl or signed_url_ttl()`，两处都错：

        ① `-99999` 被**原样透传**进 URL
           （实测：`memory://...?ttl=-99999&method=GET`）；
        ② `0` 被 `or` 悄悄换成默认 900 —— 调用方写着「0 秒」，
           拿到的是 15 分钟，而且**没有任何提示**。

      钳制方案的问题是「调用方无法知道自己的意图没被采纳」：
      老师的场景是「导出文件给一个 10 分钟的下载链接」，若他误传了
      `ttl=-1`（本该理解成「不许用」），钳制会给他一个**能用的**链接。
      把非法值变成 400 让错误在**第一次调用**就暴露，比让它以
      「链接比预期多活 24 小时」的形式留在生产里好得多。

    ★ 为什么不做「消费/验签入口」：`ttl` 是签发侧的参数，它在这里能被
      完整判定；而「验签 + 过期 + 越权」需要真实对象存储后端，
      属于 `media` HTTP 端点那一批工作（本函数只保证**签出去的 TTL
      是合法的、并且调用方知道它合法**）。
    """
    if ttl is None:
        value = signed_url_ttl()
    else:
        # ★ `bool` 是 `int` 的子类：`ttl=True` 会变成 1 秒，
        #   这不是调用方想表达的「是」，必须先挡掉。
        if isinstance(ttl, bool):
            raise ValidationFailed(
                detail=f"签名 URL 的 ttl 必须是秒数（整数），收到布尔值 {ttl!r}"
            )
        if isinstance(ttl, float) and not ttl.is_integer():
            raise ValidationFailed(
                detail=f"签名 URL 的 ttl 必须是整秒，收到 {ttl!r}"
            )
        try:
            value = int(ttl)
        except (TypeError, ValueError):
            raise ValidationFailed(
                detail=f"签名 URL 的 ttl 必须是秒数（整数），收到 {ttl!r}"
            ) from None

    if not (SIGNED_URL_TTL_MIN_SECONDS
            <= value <= SIGNED_URL_TTL_MAX_SECONDS):
        raise ValidationFailed(
            detail=(
                f"签名 URL 的有效期 {value} 秒不合法：允许 "
                f"{SIGNED_URL_TTL_MIN_SECONDS}~{SIGNED_URL_TTL_MAX_SECONDS} "
                f"秒（上限 24 小时）。0 或负值不是「立即过期」，"
                f"请改用合理有效期或直接不签发"
            ),
            extra={
                "ttl": value,
                "min": SIGNED_URL_TTL_MIN_SECONDS,
                "max": SIGNED_URL_TTL_MAX_SECONDS,
            },
        )
    return value


def retain_until_for(kind: str, now=None) -> Any:
    """按 kind 算保留截止时间。永久资产返回 None。"""
    days = RETAIN_DAYS.get(kind)
    if days is None:
        return None
    return (now or timezone.now()) + timedelta(days=days)


class MediaService:
    """资源登记、引用计数与生命周期。"""

    # ══════════════════════════════════════════════════════════
    #  预签名直传
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def presign_upload(
        *,
        org_id: int,
        kind: str,
        filename: str,
        mime: str = "",
        byte_size: int | None = None,
    ) -> dict[str, Any]:
        """签发预签名上传 URL。

        ★★ 校验必须在**发签名之前**做（kind / 扩展名 / 大小）：
          让前端自己校验等于没校验——任何人都能改前端代码。

        ★ 返回的 `key` 就是最终 oss_key：前端直传后调
          `register_upload(key=...)` 登记。**不接受前端自报 key**，
          否则可以伪造任意路径。

        ★ 这一步**拿不到文件内容**（文件还在客户端），所以这里只能校验
          kind / 扩展名 / 自报大小 —— 魔数校验发生在**真正拿到字节**的
          `upload_directly()` 里（二者是同一套 `validate_upload()` 契约）。
          直传路径的内容风险由「登记时确认对象存在」承担，这一点如实说明，
          不假装这里校验过内容。

        ★ `expires_in` 走 `resolve_signed_url_ttl(None)`：连**配置值**也要
          落在合法区间内，避免一个运维手抖的默认值悄悄签出超长 URL。
        """
        ext = validate_upload(
            kind=kind, filename=filename, mime=mime, size=byte_size
        )
        key = build_key(org_id=org_id, kind=kind, ext=ext)
        backend = get_backend()
        ttl = resolve_signed_url_ttl(None)
        return {
            "key": key,
            "url": backend.signed_url(key, ttl=ttl, method="PUT"),
            "method": "PUT",
            "expires_in": ttl,
            "backend": backend.name,
            "max_bytes": KIND_MAX_BYTES.get(kind, DEFAULT_MAX_BYTES),
            "mime": mime or "",
        }

    # ══════════════════════════════════════════════════════════
    #  后端直传（小文件）
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def upload_directly(
        *,
        org_id: int,
        kind: str,
        filename: str,
        data: bytes,
        mime: str = "",
        created_by_id: int | None = None,
        width: int | None = None,
        height: int | None = None,
        dpi: int | None = None,
        page_count: int | None = None,
        phash: int | None = None,
    ) -> Asset:
        """后端中转上传（小文件、或未启用直传时）。

        ★ 大于 `DIRECT_UPLOAD_MAX_BYTES` 时**建议**走直传，
          但仍允许（因为后端中转本身不是错误，只是占用带宽）。
          日志记一条 WARNING 便于观察。

        ★★ `data` 会被**逐字节校验**（这里是唯一能拿到文件本体的入口）：

          · 扩展名与内容魔数不符 → `ContentTypeMismatch`
            （`MEDIA.CONTENT_TYPE_MISMATCH`）——**在写盘之前**拒绝；
          · 大小上限按 `len(data)` 判定：`size` 不是调用方自报的数字，
            而是**实测字节数**（自报值在 `validate_upload` 里只用于
            记一条 WARNING）。
        """
        ext = validate_upload(
            kind=kind, filename=filename, mime=mime, data=data
        )
        if len(data) > DIRECT_UPLOAD_MAX_BYTES:
            logger.warning(
                "大文件走后端中转（%s，%.1fMB）：建议改预签名直传",
                kind, len(data) / 1024 / 1024,
            )

        key = build_key(org_id=org_id, kind=kind, ext=ext)
        backend = get_backend()
        info: ObjectInfo = backend.put(key, data, mime=mime or "")
        return MediaService._create_asset(
            org_id=org_id, kind=kind, key=key, info=info,
            mime=mime or "", created_by_id=created_by_id,
            width=width, height=height, dpi=dpi, page_count=page_count,
            phash=phash, content_sha=sha256_of(data),
        )

    # ══════════════════════════════════════════════════════════
    #  登记（直传完成后）
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def register_upload(
        *,
        org_id: int,
        kind: str,
        key: str,
        mime: str = "",
        byte_size: int | None = None,
        created_by_id: int | None = None,
        width: int | None = None,
        height: int | None = None,
        dpi: int | None = None,
        page_count: int | None = None,
        phash: int | None = None,
        verify_exists: bool = True,
    ) -> Asset:
        """登记一个已直传到对象存储的文件。

        契约：
          - `key` 必须是本系统生成的形态（`zf/{org}/{kind}/...`）
          - `key` 尚未被登记过（`oss_key` 有唯一约束）
          - `verify_exists=True` 时**确认对象真的存在**——
            否则会出现「asset 记录在、文件不在」的死链，
            而它要到用户点开图片时才暴露

        ★ 为什么必须校验 key 形态：`oss_key` 会参与对象存储操作，
          若接受前端自报的任意字符串，等于把对象存储的命名空间
          交给前端。

        ★ 大小口径（★ 自报值不可信）：`verify_exists=True` 时
          `stat` 给的是**存储后端实测的大小**，上限判定与 `byte_size`
          都优先采用它；调用方自报的 `byte_size` 只在拿不到 stat 时兜底。
          本层**不做魔数校验**——文件是前端直传进对象存储的，
          服务端手里没有字节；要判内容必须把对象读回来，
          而那是「直传后校验」的另一批工作（如实说明，不假装做过）。
        """
        if not is_safe_key(key):
            raise ValidationFailed(
                detail=f"非法的对象存储 key：{key!r}（须形如 "
                       f"zf/{{org_id}}/{{kind}}/yyyy/mm/uuid.ext）"
            )
        # key 里的 kind 必须与声明的 kind 一致
        parts = key.split("/")
        if parts[2] != kind:
            raise ValidationFailed(
                detail=f"key 中的类型 {parts[2]} 与声明 {kind} 不一致"
            )
        if int(parts[1]) != int(org_id):
            raise ValidationFailed(
                detail=f"key 中的机构 {parts[1]} 与当前机构 {org_id} 不一致"
            )
        if kind not in ALLOWED_KINDS:
            raise ValidationFailed(detail=f"未知资源类型 {kind!r}")

        if Asset.objects.for_org(org_id).filter(oss_key=key).exists():
            raise ZfError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                detail=f"该 key 已登记过：{key}",
            )

        backend = get_backend()
        if verify_exists and not backend.exists(key):
            raise ValidationFailed(
                detail=f"对象存储里找不到 {key}——"
                       f"请确认直传已完成（或传 verify_exists=False 跳过检查）"
            )

        stat = backend.stat(key) if verify_exists else None
        # ★ 存储后端报的大小是**实测值**，优先于调用方自报的 byte_size
        size = stat.size if stat is not None else (byte_size or 0)
        if stat is not None and byte_size is not None and byte_size != stat.size:
            logger.warning(
                "登记大小与对象实际大小不符（自报 %s / 实际 %s）：%s",
                byte_size, stat.size, key,
            )
        if stat is not None or byte_size is not None:
            validate_upload(
                kind=kind, filename=key, mime=mime,
                size=size if stat is not None else byte_size,
            )
        return MediaService._create_asset(
            org_id=org_id, kind=kind, key=key,
            info=ObjectInfo(key=key, size=size, mime=mime),
            mime=mime, created_by_id=created_by_id,
            width=width, height=height, dpi=dpi, page_count=page_count,
            phash=phash, content_sha="",
        )

    @staticmethod
    def _create_asset(
        *, org_id: int, kind: str, key: str, info: ObjectInfo, mime: str,
        created_by_id: int | None, width, height, dpi, page_count, phash,
        content_sha: str,
    ) -> Asset:
        a = Asset.objects.create(
            org_id=org_id,
            kind=kind,
            oss_key=key,
            mime=mime or info.mime or "",
            byte_size=info.size,
            width=width,
            height=height,
            dpi=dpi,
            page_count=page_count,
            phash=phash,
            sha256=content_sha or "",
            ref_count=0,
            retain_until=retain_until_for(kind),
            thumb_keys={},
        )
        if content_sha:
            # 内容哈希存在 thumb_keys 的保留位里会污染语义，
            # 所以放日志与返回值即可（DB 无对应字段）。
            logger.info("asset 登记 id=%s sha256=%s", a.id, content_sha[:16])
        audit_log.record(
            A_ASSET_CREATE, a, created_by_id,
            after={"kind": kind, "key": key, "size": info.size},
        )
        return a

    # ══════════════════════════════════════════════════════════
    #  引用计数
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def attach(*, org_id: int, asset_id: int, count: int = 1) -> Asset:
        """增加引用（题目挂图、试卷快照引用等）。

        ★ 走模型方法 `add_ref()`——它是唯一的增减入口，
          用 `F()` 表达式原子自增，避免并发丢更新。
        """
        a = MediaService._must_get(org_id, asset_id)
        a.add_ref(count)
        return a

    @staticmethod
    @transaction.atomic
    def detach(*, org_id: int, asset_id: int, count: int = 1) -> Asset:
        """减少引用（解绑配图、删除题目等）。

        ★ **减到 0 不代表立刻删除**：还要看 `retain_until`。
          立刻删会让「误删题目后想恢复」变得不可能。
        """
        a = MediaService._must_get(org_id, asset_id)
        a.release_ref(count)
        return a

    # ══════════════════════════════════════════════════════════
    #  ★ 删除（铁律：ref_count 必须为 0）
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def delete_asset(
        *,
        org_id: int,
        asset_id: int,
        operator_id: int | None = None,
        force: bool = False,
        reason: str = "",
        delete_object: bool = True,
    ) -> dict[str, Any]:
        """删除 asset（软删记录 + 可选删对象）。

        ★★ **`ref_count > 0` 时拒绝删除**（除非 `force=True`）。

          理由：同一张图可能被多道题、多份试卷快照、多个练习引用。
          提前清理会让**已发布试卷的配图变成死链**——
          而老师在考试前一天才发现，代价极高。

        ★ `force=True` 是给「管理员明确知道自己在做什么」的场景。
          它会**记审计并带上理由**，不是静默强删。

        ★ 只软删 `Asset` 记录、**默认同时删对象**——
          留下对象不删会持续产生存储费用，而记录已软删意味着
          再也查不到它，清理任务也扫不到（`deleted_at` 非空）。
        """
        a = MediaService._must_get(org_id, asset_id)

        if a.ref_count > 0 and not force:
            raise ZfError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                detail=(
                    f"该资源仍被引用 {a.ref_count} 次，不能删除"
                    f"（先解绑所有引用，或确认后用 force=True）"
                ),
                extra={"ref_count": a.ref_count, "oss_key": a.oss_key},
            )

        if a.ref_count > 0 and force:
            logger.warning(
                "强制删除仍被引用的资源 id=%s ref_count=%s key=%s reason=%s",
                a.id, a.ref_count, a.oss_key, reason,
            )

        deleted_object = False
        if delete_object and a.oss_key:
            try:
                deleted_object = get_backend().delete(a.oss_key)
            except StorageError as exc:
                # ★ 对象删除失败**不阻断记录删除**：
                #   记录已无用，留着只会让清理任务反复扫到它。
                #   但必须告警——孤儿对象要能被运维发现并手工清理。
                logger.exception(
                    "删除对象失败 key=%s（记录仍会删除，注意孤儿对象）",
                    a.oss_key,
                )

        before = {
            "ref_count": a.ref_count,
            "oss_key": a.oss_key,
            "kind": a.kind,
            "byte_size": a.byte_size,
        }
        a.delete()      # 软删（`SoftDeleteModel`）
        audit_log.record_strict(
            A_ASSET_DELETE, a, operator_id,
            before=before,
            after={"deleted_object": deleted_object, "forced": force},
            reason=reason or ("强制删除" if force else "正常删除"),
        )
        return {
            "asset_id": asset_id,
            "deleted_object": deleted_object,
            "forced": force,
            "ref_count_at_delete": before["ref_count"],
        }

    # ══════════════════════════════════════════════════════════
    #  读
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def get_url(
        *, org_id: int, asset_id: int, ttl: int | None = None
    ) -> str:
        """取临时访问 URL。★ 永不返回永久公开 URL。

        TTL 契约
        --------
        `ttl=None` → 用配置默认值（900 秒）；显式传值则必须落在
        `[1, 86400]` 秒内。**非法值抛 `COMMON.VALIDATION_ERROR`（400），
        不做静默钳制** —— 理由写在 `resolve_signed_url_ttl()` 的 docstring
        里（一句话：钳制会让调用方以为自己签了个更长的有效期）。
        实测过的旧行为：`ttl=-99999` 被原样透传进 URL，
        `ttl=0` 被 `or` 悄悄换成 900。

        ★★ 当前后端的**能力边界**（如实写明，不假装它有签名能力）：

        | 后端 | `signed_url()` 返回 | 过期 | 越权 |
        | --- | --- | --- | --- |
        | `LocalStorage`（本地/开发） | `file://<绝对路径>` | **不成立** | **不成立** |
        | `MemoryStorage`（测试） | `memory://<key>?ttl=N&method=GET` | **不成立** | **不成立** |
        | `S3Storage`（生产） | 厂商预签名 URL | 由厂商保证 | 由厂商保证 |

        也就是说：`LocalStorage.signed_url()` 返回的 `file://` 里
        **连 ttl 都没有**（它压根不做签名），`MemoryStorage` 的 `ttl=N`
        只是把数字拼进字符串、**没有任何地方校验它**。
        于是「URL 过期后取不到」「拿 URL 越权取别机构的对象」这两条
        在本地/内存后端**不成立**——拿到 URL 的人可以一直用，
        本地后端下等于拿到了一个绝对路径。

        本方法能保证、也已经实测的只有两件事（不要把它们说成更多）：
          ① **机构隔离发生在签发时刻**：`_must_get()` 按 org 过滤，
             跨机构取 asset 抛 `COMMON.NOT_FOUND`（不会签出任何 URL）；
          ② **签出去的 TTL 一定是合法值**（上面那套校验）。

        ★ 真正的「验签 / 过期 / 消费入口」不在本方法职责内（那是
          `media` HTTP 端点那一批工作）；生产用 S3 时由厂商保证。
        """
        a = MediaService._must_get(org_id, asset_id)
        return get_backend().signed_url(
            a.oss_key, ttl=resolve_signed_url_ttl(ttl)
        )

    @staticmethod
    def read_bytes(*, org_id: int, asset_id: int) -> bytes:
        a = MediaService._must_get(org_id, asset_id)
        return get_backend().get(a.oss_key)

    @staticmethod
    def _must_get(org_id: int, asset_id: int) -> Asset:
        a = Asset.objects.for_org(org_id).filter(pk=asset_id).first()
        if a is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="资源不存在")
        return a

    # ══════════════════════════════════════════════════════════
    #  生命周期清理
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def cleanup_expired(
        *, org_id: int | None = None, limit: int = 500, dry_run: bool = False
    ) -> dict[str, Any]:
        """清理到期且无引用的资源（00 文档 6.3）。

        契约：
          - **只清理 `retain_until` 已到期且 `ref_count = 0`** 的
          - `retain_until` 为空的（永久资产）**永不清理**
          - `dry_run=True` 只报告不删

        ★ 上线前必须先 dry-run 一遍：
          清理任务删的是**对象存储里的真实文件**，删错不可恢复。
        """
        now = timezone.now()
        qs = Asset.objects.all() if org_id is None else Asset.objects.for_org(org_id)
        qs = qs.filter(
            ref_count=0,
            retain_until__isnull=False,
            retain_until__lt=now,
        ).order_by("retain_until")[:limit]

        rows = list(qs)
        if dry_run:
            return {
                "dry_run": True,
                "count": len(rows),
                "items": [
                    {"id": a.id, "kind": a.kind, "key": a.oss_key,
                     "byte_size": a.byte_size,
                     "retain_until": str(a.retain_until)}
                    for a in rows
                ],
                "total_bytes": sum(a.byte_size for a in rows),
            }

        deleted = 0
        freed = 0
        failed: list[dict[str, Any]] = []
        for a in rows:
            try:
                with transaction.atomic():
                    MediaService.delete_asset(
                        org_id=a.org_id, asset_id=a.id,
                        reason="到期自动清理（retain_until 已过且无引用）",
                    )
                deleted += 1
                freed += a.byte_size
            except Exception as exc:      # noqa: BLE001
                failed.append({"id": a.id, "error": str(exc)[:200]})
                logger.exception("清理资源失败 id=%s", a.id)
        return {
            "dry_run": False,
            "deleted": deleted,
            "failed": failed,
            "freed_bytes": freed,
        }

    @staticmethod
    def stats(*, org_id: int) -> dict[str, Any]:
        """存储用量统计。★ 运维看板用。"""
        from django.db.models import Count, Sum

        rows = (
            Asset.objects.for_org(org_id)
            .values("kind")
            .annotate(n=Count("id"), bytes=Sum("byte_size"))
            .order_by("-bytes")
        )
        by_kind = [
            {"kind": r["kind"], "count": r["n"],
             "bytes": int(r["bytes"] or 0),
             "mb": round((r["bytes"] or 0) / 1024 / 1024, 2)}
            for r in rows
        ]
        total = sum(x["bytes"] for x in by_kind)
        cleanable = Asset.objects.for_org(org_id).filter(
            ref_count=0, retain_until__isnull=False,
            retain_until__lt=timezone.now(),
        )
        return {
            "total_count": sum(x["count"] for x in by_kind),
            "total_bytes": total,
            "total_mb": round(total / 1024 / 1024, 2),
            "by_kind": by_kind,
            "cleanable_count": cleanable.count(),
            "cleanable_bytes": int(
                cleanable.aggregate(s=Sum("byte_size"))["s"] or 0
            ),
        }
