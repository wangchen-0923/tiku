"""JWT 认证。

★ 设计要点：
  - 访问令牌短寿命（默认 2 小时），刷新令牌长寿命（默认 14 天）
  - 刷新令牌**一次性使用**（rotation）：刷新后旧的立即失效，防重放
  - 载荷只放必要字段（`sub`/`org_id`/`role`/`jti`），**不放姓名与手机号**——
    令牌在客户端可被解码，不应携带 PII
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import jwt
from django.conf import settings
from django.utils import timezone
from rest_framework import authentication, exceptions

# ★ 不要在这里顶层 import `apps.common.exceptions`。
#   原因：DRF 启动时会 import 本模块（`DEFAULT_AUTHENTICATION_CLASSES`），
#   而此时 `apps.common.exceptions` 可能仍在初始化中，形成**循环导入**，
#   报 "cannot import name 'ErrorCode' from partially initialized module"。
#   后果很隐蔽：`ZfError` 抛不出来，业务判据全部失效。
#   因此在本模块内按需（函数内）导入。

#: 令牌类型（防「用刷新令牌当访问令牌用」）
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"

ACCESS_TOKEN_LIFETIME = timedelta(hours=2)
REFRESH_TOKEN_LIFETIME = timedelta(days=14)

ALGORITHM = "HS256"


def _secret() -> str:
    return settings.SECRET_KEY


def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def _decode(token: str) -> dict[str, Any]:
    return jwt.decode(token, _secret(), algorithms=[ALGORITHM])


def password_fingerprint(user) -> str:
    """密码指纹：改密码后旧令牌立刻失效（U26）。

    ★★ 为什么需要它

      JWT 是**无状态**的：令牌一旦签发，服务端就不再持有它，
      改密码**不会**让已发出的令牌失效——它会一直有效到 `exp`（2 小时）。
      对老师的实际影响是：密码可能已经因为手机丢失而改过，
      但捡到手机的人还能继续用旧会话操作 2 小时。

      原生方案是给 `user` 加 `token_version` 字段并在每次改密时 +1，
      但那要一次迁移、一次回填，而这个能力本该一开始就有。

    ★ 为什么用「密码哈希的 sha256」而不是「哈希前缀」

      argon2 的哈希形如 `argon2$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>`，
      **前 16 个字符对所有用户完全相同**（算法与参数），拿前缀当指纹等于没做。
      对整串再 sha256 一次，既取到「随密码变化」的部分，又不把哈希本身放进令牌
      （令牌在客户端可解码）。

    ★ 覆盖范围

      - 用户自助改密（`change_password`）→ 哈希变 → 指纹变 → 旧令牌失效
      - 学校平台账户重置密码（`reset_password`）→ 同上
      - 两种情况都**不需要**额外写代码，因为都走 `set_password`
    """
    import hashlib

    raw = (getattr(user, "password", "") or "").encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def issue_tokens(user) -> dict[str, str]:
    """签发访问令牌与刷新令牌。"""
    now = timezone.now()
    base = {
        "sub": str(user.pk),
        # ★ 只放标识与权限，不放姓名/手机号
        "org_id": user.org_id,
        "role": getattr(user, "role", ""),
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        # ★ 密码指纹：改密后旧令牌立即作废（U26）
        "pv": password_fingerprint(user),
    }
    access = _encode(
        {**base, "type": TOKEN_TYPE_ACCESS,
         "exp": int((now + ACCESS_TOKEN_LIFETIME).timestamp())}
    )
    refresh = _encode(
        {**base, "type": TOKEN_TYPE_REFRESH,
         "exp": int((now + REFRESH_TOKEN_LIFETIME).timestamp())}
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": int(ACCESS_TOKEN_LIFETIME.total_seconds()),
    }


def decode_access_token(token: str) -> dict[str, Any]:
    """校验并解出访问令牌载荷。失败抛 `ZfError`。

    ★★ 失败路径**必须**能真的抛出 `ZfError`。

      我第一版只 `from apps.common.exceptions import ZfError`，
      **忘了导入 `ErrorCode`**。于是令牌过期或格式错误时抛的是
      `NameError: name 'ErrorCode' is not defined`，而被
      `except Exception` 吞掉后转成 500「服务内部错误」。

      后果对老师是这样的：登录过期后每个页面都报「服务内部错误」，
      他会一直重试、刷新、清缓存，**而不是重新登录**——
      因为提示里没有一个字提到登录。

      ★ 这一条只能靠「真的用一个坏令牌走一遍」测出来。
        正常登录的路径全绿，静态检查也不报错。
    """
    from apps.common.exceptions import ErrorCode, ZfError

    try:
        payload = _decode(token)
    except jwt.ExpiredSignatureError as exc:
        raise ZfError(
            ErrorCode.AUTH_TOKEN_EXPIRED, "登录已过期，请重新登录"
        ) from exc
    except jwt.PyJWTError as exc:
        raise ZfError(
            ErrorCode.AUTH_INVALID_CREDENTIALS, "登录凭证无效，请重新登录"
        ) from exc

    if payload.get("type") != TOKEN_TYPE_ACCESS:
        raise ZfError(
            ErrorCode.AUTH_INVALID_CREDENTIALS,
            "凭证类型不正确（可能误用了刷新令牌），请重新登录",
        )
    return payload


class JWTAuthentication(authentication.BaseAuthentication):
    """DRF 认证类。"""

    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            # ★ 返回 None 而不是抛错：允许匿名接口继续走权限类判断
            return None
        if len(header) == 1:
            raise exceptions.AuthenticationFailed("缺少令牌内容")
        if len(header) > 2:
            raise exceptions.AuthenticationFailed("令牌格式不正确")

        try:
            token = header[1].decode()
        except UnicodeError as exc:
            raise exceptions.AuthenticationFailed("令牌编码不正确") from exc

        return self.authenticate_credentials(token)

    def authenticate_credentials(self, token: str):
        from apps.accounts.models import User

        try:
            payload = decode_access_token(token)
        except Exception as exc:  # ZfError
            # ★★ 把**真实错误码与文案**挂到 DRF 异常上。
            #
            #   只传 `str(exc)` 的话，异常处理器拿不到码，
            #   会把「令牌过期」也映射成「账号或密码错误」——
            #   而这两件事要老师做的事完全不同
            #   （重新登录 vs 确认密码）。
            err = exceptions.AuthenticationFailed(
                getattr(exc, "detail", None) or str(exc))
            err.zf_code = getattr(exc, "code", None) or \
                "AUTH.INVALID_CREDENTIALS"
            err.zf_message = getattr(exc, "detail", None) or str(exc)
            raise err from exc

        user_id = payload.get("sub")
        try:
            user = User.objects.get(pk=user_id, deleted_at__isnull=True)
        except User.DoesNotExist as exc:
            raise exceptions.AuthenticationFailed("用户不存在或已停用") from exc

        if not user.is_active:
            raise exceptions.AuthenticationFailed("账号已停用")

        # ★ 机构变更后旧令牌应失效：令牌里的 org 必须与当前一致
        token_org = payload.get("org_id")
        if token_org != user.org_id:
            raise exceptions.AuthenticationFailed("账号机构已变更，请重新登录")

        # ★★ 改密码后旧令牌必须失效（U26）
        #
        #   严格判定：**缺失 `pv` 也拒绝**。
        #   若对缺失放行，就会出现「老令牌永远有效、新令牌才受管」的
        #   两套行为——而攻击者拿到的恰好是那种改密前签发的旧令牌。
        #   代价是本次升级后所有已登录用户需重新登录一次，这是可接受的。
        token_pv = payload.get("pv")
        if token_pv != password_fingerprint(user):
            raise exceptions.AuthenticationFailed(
                "密码已变更，请用新密码重新登录"
            )

        return (user, payload)

    def authenticate_header(self, request) -> str:
        return self.keyword
