"""验收证据台账（`P4-07` 的配套件）。

★★ 为什么需要它 —— 这是本项目验收方法学上的一个必要补丁：

  `verify_acceptance_a1_a42.py` 是**只读**的：它只读库、判活数据。
  但 A31–A36（配图强制确认）、A16（45 人扫码）、A24/A26（个性化练习闭环）
  这类验收，**必须构造数据才能判**（库里现在配图 0 张、练习集 0 份）。

  于是有了三种「判定不了」，而它们的原因完全不同：

  | 原因 | 例子 | 正确处置 |
  | --- | --- | --- |
  | 只读脚本不能写库 | A31 需要造 PENDING 配图 | **写一个会清理的构造型验证脚本**，把结论落进本台账 |
  | 缺外部数据集/真人计时 | A1 缺 30 份 Word 试卷、A17 缺 100 张扫描样本 | 保持 `NODATA`，写清缺什么 |
  | **一期范围外**（产品决策） | A17/A18/A19 因**决策 9=B**「OMR 纸扫移二期」不做 | 标 `out_of_scope`，**不要**混进「缺数据」 |

  ★ 第三条尤其重要：把「决定不做」写成「缺样本」，会让验收报告里
    永远挂着一批看起来「只差数据」的项，而实际它们**不在本期承诺内**。
    那是把范围决策伪装成数据债 —— 评审时没人能看出区别。

★ 台账的硬规矩：

  1. **证据必须有出处**：`source` 写生成它的脚本名，`item` 写验收编号，
     `at` 写生成时刻。没有出处的 `PASS` 就是自我声明，本台账拒收。
  2. **证据会过期**：默认 `max_age_days=7`，超期判 `STALE`，
     报告里按 `NODATA` 处理并注明「证据已过期，请重跑 <source>」。
     ★ 否则「三个月前跑过一次」会永远显示为通过。
  3. **一条证据只覆盖一条验收**：不做「一荣俱荣」的批量结论。
  4. 写入前先读回、再合并落盘（同一条被后跑的脚本覆盖）。
  5. ★★ **`PASS` 必须声明 `run_complete=True`**，`FAIL` 必须带 `failures`。
     两条都不是形式主义，都有实测的代价：

     | 规则 | 不遵守的实测后果 |
     | --- | --- |
     | `FAIL` 要带 `failures` | A29 只写了「共 35 条判定，不通过 5 条」（**17 个字**）→ 看报告的人无法复核，一条**跨租户 PII 泄露**差点就这么躺在台账里 |
     | `PASS` 要带 `run_complete` | 脚本运行中途崩溃，`finally` 照样写台账 → 判据集从 36 条截断成 31 条 → **截断后恰好全绿，写出 PASS**。★ **判据集变小反而更容易通过** |

     ★ 默认值是 `run_complete=False`（fail-safe）：没声明就不当作通过。
       验收器会把「PASS 但未声明跑完」按 `NODATA` 处理并点名要改哪个脚本。

用法（构造型验证脚本与只读验收器里都一样）：

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))   # scripts/ 目录

    import acceptance_evidence as evidence

    evidence.record_evidence("A31", "PASS",
                             "四条反例全部被拦：批次 3/3、定稿 2/2、组卷 1/1、发布 1/1",
                             source="verify_acceptance_figures.py")
    ev = evidence.load_evidence()          # {验收编号: Evidence}

★ 用「把 scripts/ 加进 sys.path 再直接 import」而不是 `from scripts.… import`：
  后者要求 `scripts` 是一个可导入的包，而本项目 `scripts/` 下没有 `__init__.py`
  （它是脚本目录，不是包）。靠命名空间包的隐式行为会在「换 cwd」时炸掉。
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

#: 证据台账路径（与验收报告同目录，便于一起归档）
EVIDENCE_PATH = Path(r"D:\tiku\artifacts\acceptance_evidence.json")

#: 证据默认有效期（天）。超过即 `STALE`。
DEFAULT_MAX_AGE_DAYS = 7


@dataclass(frozen=True)
class Evidence:
    """一条验收证据。"""

    item: str
    status: str          # PASS / FAIL
    measured: str
    source: str
    at: str              # ISO8601（本地时间）
    #: ★★ `FAIL` 时**必须**列出具体不通过的判据。
    #:   只写「共 35 条判定，不通过 5 条」等于没写 —— 看报告的人无法复核，
    #:   而 `FAIL` 的全部价值就在于「能照着修」。
    #: ★ 我改下面那段注释时**误删了这个字段**，于是 `Evidence(...)` 构造
    #:   TypeError → `load_evidence` 的 `except` 把**每条**都跳过 → 台账读成 0 条。
    #:   ★ 教训：`except ...: continue` 这类"跳过坏数据"的容错，会把
    #:     **系统性错误伪装成"没有数据"**（而不是报错）。
    failures: tuple[str, ...] = ()
    #: ★★★ 本次判定是否**跑完了全部判据**（默认 `False`）。
    #:
    #:   为什么默认是 `False` 而不是 `True`：实测踩过一次**假 PASS**——
    #:   脚本运行到一半抛异常（某条断言在新代码下变成抛 400），而它的
    #:   `finally` **照样把台账写了**，于是「36 条判据」变成「已跑到的 31 条」，
    #:   **截断后的集合恰好全绿 → 写出 PASS**。
    #:
    #:   ★ 判据集被截断反而更容易通过 —— 这比红着更危险。
    #:     所以：**脚本必须在全部判据跑完后显式声明 `run_complete=True`**，
    #:     否则验收器一律按 `NODATA` 处理（宁可显示"没判"，也不显示"通过"）。
    run_complete: bool = False

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.at)

    def age_days(self, now: datetime | None = None) -> float:
        ref = now or datetime.now()
        return (ref - self.when).total_seconds() / 86400.0

    def is_stale(self, max_age_days: int = DEFAULT_MAX_AGE_DAYS,
                 now: datetime | None = None) -> bool:
        return self.age_days(now) > max_age_days


def _read_raw(path: Path | None = None) -> dict[str, dict]:
    """读台账原始 JSON。

    ★★ 2026-10-17 修：这个函数原来**不接受路径**，而 `record_evidence(evidence_path=X)`
      只把**写**指向 X、**读**仍然读默认真台账 —— 于是"用临时路径做隔离测试"
      会把整份真台账原样写进临时文件（读真、写真，互相串味）。
      实测由 `verify_acceptance_resilience_perf_security.py` 的作者发现并复现。
      ★ 教训：一个"看起来支持自定义路径"的参数，如果只在半个函数里生效，
        比**完全没有**这个参数更危险 —— 用它写测试的人会以为隔离生效了。
    """
    p = path or EVIDENCE_PATH
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # ★ 台账坏掉不能静默当空 —— 那会让一批 PASS 悄悄变成 NODATA 而没人知道原因。
        raise RuntimeError(
            f"证据台账无法解析：{p}。"
            f"请修好或删除它（删除后所有构造型验收会回到 NODATA）"
        ) from None
    if not isinstance(data, dict):
        raise RuntimeError(f"证据台账格式错误（顶层应为对象）：{p}")
    return data


def load_evidence(evidence_path: Path | None = None) -> dict[str, Evidence]:
    """读台账；返回 `{验收编号: Evidence}`。"""
    out: dict[str, Evidence] = {}
    for item, row in _read_raw(evidence_path).items():
        try:
            out[item] = Evidence(
                item=item,
                status=row["status"],
                measured=row["measured"],
                source=row["source"],
                at=row["at"],
                failures=tuple(row.get("failures") or ()),
                run_complete=bool(row.get("run_complete", False)),
            )
        except (KeyError, TypeError):
            # 单条坏数据不拖垮整份台账；缺字段的那条被忽略（等于没证据）。
            continue
    return out


def record_evidence(item: str, status: str, measured: str, *,
                    source: str, failures: Sequence[str] | None = None,
                    run_complete: bool = False,
                    evidence_path: Path | None = None) -> Evidence:
    """写入/覆盖一条证据。

    ★ 参数校验是刻意的：`status` 只允许 `PASS` / `FAIL` ——
      **不允许往台账里写 `NODATA`**。`NODATA` 的意思是「没判」，
      而「没判」不应该有台账记录，否则会出现「一条 NODATA 证据」这种自相矛盾的东西。

    ★★ `FAIL` 必须带 `failures`：**只写「共 N 条判定，不通过 M 条」等于没写**。
      验收报告的第一读者是「要去修的人」，他需要知道**哪一条**不通过、
      期望什么、实测什么。实测踩过：A29 的 `measured` 只有 17 个字，
      报告里只剩「不通过 5 条」，没人能照着复核。
    """
    if status not in {"PASS", "FAIL"}:
        raise ValueError(f"证据状态只能是 PASS/FAIL，收到 {status!r}")
    if not source:
        raise ValueError("证据必须有 source（生成它的脚本名）—— 没有出处的 PASS 是自我声明")
    if not measured:
        raise ValueError("证据必须有 measured（实测值说明）")
    fails = tuple(str(x) for x in (failures or ()) if str(x).strip())
    if status == "FAIL" and not fails:
        raise ValueError(
            "FAIL 证据必须给出 failures（逐条列出不通过的判据：判据原文 + 期望 + 实测值）。"
            "只写一句「不通过 N 条」的报告，读的人无法照着修 —— 那等于没写。"
        )

    ev = Evidence(
        item=item,
        status=status,
        measured=measured,
        source=os.path.basename(source),
        at=datetime.now().isoformat(timespec="seconds"),
        failures=fails,
        run_complete=bool(run_complete),
    )
    path = evidence_path or EVIDENCE_PATH
    data = _read_raw(path)
    data[item] = {
        "status": ev.status,
        "measured": ev.measured,
        "source": ev.source,
        "at": ev.at,
        "failures": list(ev.failures),
        "run_complete": ev.run_complete,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return ev


def clear_evidence(items: list[str] | None = None, *,
                   evidence_path: Path | None = None) -> None:
    """清除证据（不传 `items` 则清空全部）。

    ★ 构造型脚本**不应该**在结束时清掉自己的证据 —— 那是它这趟跑出来的结论。
      这个函数只给「某条验收的判据改了、旧证据失效」时用。
    """
    path = evidence_path or EVIDENCE_PATH
    if items is None:
        if path.exists():
            path.unlink()
        return
    data = _read_raw(path)
    for item in items:
        data.pop(item, None)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def summarize(max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> str:
    """人读摘要（排查用）。"""
    ev = load_evidence()
    if not ev:
        return "（台账为空）"
    now = datetime.now()
    lines = []
    for item in sorted(ev):
        e = ev[item]
        stale = "★STALE" if e.is_stale(max_age_days, now) else ""
        lines.append(f"  {item:<5} {e.status:<5} {e.at} ({e.age_days(now):.1f}天前) "
                     f"{e.source} {stale}\n        {e.measured}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - 手工排查入口
    print(f"台账：{EVIDENCE_PATH}")
    print(summarize())
