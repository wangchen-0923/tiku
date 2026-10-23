"""验收：顶栏层级结构（2026-10-23 导航收敛）。

    <python> scripts/verify_nav_hierarchy.py

全静态，不需要浏览器/数据库，跑一次不到 1 秒。

★★ 这个脚本要拦住的是**回退**，不是功能缺陷。

   收敛之前顶栏是 11 项，`.zf-topbar__nav` 只能横向滚动才看得全；
   而滚动条是隐藏的（`scrollbar-width: none`），用户**看不出右边还有内容**。
   所以「积分与凭证」实际上是进不去的。

   收敛之后一级只剩 8 个核心模块，被收起的 6 个入口进了各自页面。
   这份结构很容易被下一次「顺手加个入口」破坏——加一项不报错、
   不告警，只是顶栏悄悄又开始滚。所以用静态断言钉住：

     1. 一级项的数量与名字（多一个就红）
     2. 被收起的入口**不在** `<nav>` 里
     3. 每个收起的入口在**它该在的页面里**确实有（否则等于功能丢了）
     4. 每个入口的 route name 在 router 里真实存在（写错就是点了 404）
     5. 入口的权限要求与目标页的 `minLevel` 不矛盾

★ 为什么不用真机脚本：真机要起前后端、要账号、要 30 秒，
  这种「结构有没有被破坏」的检查必须能在每次改动里跑。
  渲染与点击由人工真机抽查（截图见 artifacts/nav_grouping/）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web" / "src"

PASS = 0
FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}   {extra}")


def read(name: str) -> str:
    try:
        return (WEB / name).read_text(encoding="utf-8")
    except OSError:
        return ""


def strip_comments(src: str) -> str:
    """去掉 HTML / JS / CSS 注释。

    ★ 必须先去注释再断言：本项目的注释里**大量引用**页面名与路由名
      （例如「原来难度打标在顶栏」）。直接 `in` 判断会把注释算成代码，
      结果是**永久红**——而永久红的检查等于没有检查
      （2026-10-23 U27 的验收脚本就栽在这上面）。
    """
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)
    return src


APP = strip_comments(read("App.vue"))
ROUTER = strip_comments(read("router/index.ts"))
QLIST = strip_comments(read("views/QuestionListView.vue"))
KB = strip_comments(read("views/KnowledgeView.vue"))
SETTINGS = strip_comments(read("views/SettingsView.vue"))


def nav_block(src: str) -> str:
    """取 `<nav ...>` 到 `</nav>` 之间的内容（顶栏菜单的唯一来源）。"""
    m = re.search(r"<nav[^>]*>(.*?)</nav>", src, flags=re.S)
    return m.group(1) if m else ""


NAV = nav_block(APP)


def nav_links(src: str) -> list[str]:
    """顶栏里所有 RouterLink 的目标路由名。"""
    return re.findall(r'<RouterLink[^>]*:to="\{ name: \'([A-Za-z]+)\' \}"', src)


def check_nav_shape() -> None:
    print("\n== A 一级导航的形状 ==")

    check("顶栏 nav 区块存在", bool(NAV.strip()))

    names = nav_links(NAV)
    core = ["Home", "QuestionList", "Knowledge", "Classroom", "PaperList",
            "AssignmentList", "SheetList", "Settings"]

    # ★ 一级项是**固定清单**：顺序也要对（概览→题库→知识体系→…→设置）。
    #   顺序错不算 bug，但改动时看到这条红，会提醒「你是不是多加了什么」。
    without_optional = [n for n in names if n != "PublicReview"]
    check(
        f"一级核心入口恰好 {len(core)} 个且顺序正确",
        without_optional == core,
        f"实际: {'|'.join(without_optional)}",
    )
    check(
        "公共题审核只出现一次且在最后（仅平台管理员可见）",
        names.count("PublicReview") == 1 and names[-1] == "PublicReview",
        f"实际: {'|'.join(names)}",
    )
    check(
        f"一级项总数 ≤ {len(core) + 1}（超了顶栏又要横向滚动）",
        len(names) <= len(core) + 1,
        f"实际 {len(names)} 项",
    )

    # ★★ 被收起的一律不许回顶栏。
    hidden = {
        "难度打标": "QuestionMarkDifficulty",
        "导入题目": "ImportPaper",
        "知识点补标": "QuestionMarkKnowledge",
        "知识管理": "KnowledgeManage",
        "教师账号": "TeacherAccounts",
        "积分与凭证": "Billing",
    }
    for label, name in hidden.items():
        check(
            f"「{label}」不在顶栏",
            name not in names and label not in NAV,
            f"nav 里仍有 {name} / {label}",
        )

    # ★ 明确「不是下拉菜单」：用户要求的是页内集成。
    check("顶栏没有下拉菜单（不是把入口藏进菜单）",
          "el-dropdown" not in NAV)


def check_page_entries() -> None:
    print("\n== B 入口在各自页面里 ==")

    # ── 题库页：导入题目 + 难度打标（工具栏）──
    check("题库页有「导入题目」入口", "name: 'ImportPaper'" in QLIST)
    check("题库页有「难度打标」入口", "name: 'QuestionMarkDifficulty'" in QLIST)

    # ★★ 难度告警卡内的入口：卡片只在 `needs_annotation` 时渲染，
    #   所以按钮必须**在卡片内部**，否则会出现「一张讲难度没分层的卡片
    #   旁边挂着个按钮」这种布局错位。
    alert = re.search(r'v-if="stats\?\.difficulty && [^"]*needs_annotation"(.*?)</el-alert>',
                      QLIST, flags=re.S)
    check(
        "「去快速打标」按钮位于难度未分层告警卡内部",
        bool(alert) and "QuestionMarkDifficulty" in alert.group(1),
        "告警卡里找不到该入口",
    )
    check(
        "告警卡文案不再只说「详情→编辑」（已改为指向快速打标）",
        "去快速打标" in QLIST,
    )

    # ── 知识体系页：知识点补标 + 结构管理 ──
    check("知识体系页有「知识点补标」入口", "name: 'QuestionMarkKnowledge'" in KB)
    check("知识体系页有「知识体系管理」入口", "name: 'KnowledgeManage'" in KB)
    check(
        "结构管理入口受 knowledge.node.manage 控制",
        "knowledge.node.manage" in KB,
    )
    # ★ 零题知识点卡是补标的靶子：按钮要跟着「数量 > 0」出现。
    zero_card = re.search(r'probs--ok\': !zeroQuestion\.length[^>]*>(.*?)</div>\s*</div>',
                          KB, flags=re.S)
    check(
        "「去补标」按钮在零题知识点卡内且按数量条件渲染",
        bool(zero_card)
        and "QuestionMarkKnowledge" in zero_card.group(1)
        and "v-if=\"zeroQuestion.length\"" in zero_card.group(1),
        "零题知识点卡里找不到条件渲染的补标入口",
    )

    # ── 设置页：教师账号 + 积分与凭证 ──
    check("设置页有「教师账号」条目", "name: 'TeacherAccounts'" in SETTINGS)
    check("设置页有「积分与凭证」条目", "name: 'Billing'" in SETTINGS)
    check("设置页有「个人中心」条目", "name: 'Profile'" in SETTINGS)
    check(
        "教师账号条目要求 user.create",
        re.search(r"name: 'TeacherAccounts'.{0,400}?requires: 'user\.create'",
                  SETTINGS, flags=re.S) is not None,
        "条目缺少能力约束",
    )
    # ★★ 平台管理员也满足 `user.create`（min: SCHOOL），但他没有机构，
    #   后端会 403。必须再挂一条「有机构」判据，否则会给出一个
    #   点进去全是 403 的入口。
    check(
        "★ 教师账号条目还要求「属于某个机构」（挡住无机构的平台管理员）",
        re.search(r"name: 'TeacherAccounts'.{0,400}?requiresOrg: true",
                  SETTINGS, flags=re.S) is not None,
        "缺少 requiresOrg",
    )
    # ★ 设置页只分发，不放表单：两处都能改 = 两套校验。
    check("设置页不含任何输入控件（只做分发）",
          "<el-input" not in SETTINGS and "<el-form" not in SETTINGS)


def check_routes() -> None:
    print("\n== C 入口指向真实路由 ==")

    targets = {
        "QuestionList": "题目列表",
        "QuestionMarkDifficulty": "难度打标",
        "ImportPaper": "导入题目",
        "Knowledge": "知识体系",
        "QuestionMarkKnowledge": "知识点补标",
        "KnowledgeManage": "知识体系管理",
        "Settings": "设置",
        "TeacherAccounts": "教师账号",
        "Billing": "积分与凭证",
        "Profile": "个人中心",
    }
    for name, label in targets.items():
        check(f"路由 {name}（{label}）存在", f"name: '{name}'" in ROUTER)

    check("设置页路由指向 SettingsView.vue", "views/SettingsView.vue" in ROUTER)
    check("设置页路径为 /settings", "path: '/settings'" in ROUTER)
    check("教师账号仍是 /settings/teachers（面包屑读作 设置 › 教师账号）",
          "path: '/settings/teachers'" in ROUTER)

    # ★ 入口的权限要求不得**弱于**目标页的 minLevel：
    #   弱了会渲染出一个点了被踢到 Forbidden 的入口。
    check(
        "教师账号页 minLevel = SCHOOL（与入口的 user.create 同门槛）",
        re.search(r"name: 'TeacherAccounts'.{0,300}?minLevel: LEVEL\.SCHOOL",
                  ROUTER, flags=re.S) is not None,
    )
    check(
        "知识体系管理页 minLevel = SCHOOL（与入口同门槛）",
        re.search(r"name: 'KnowledgeManage'.{0,300}?minLevel: LEVEL\.SCHOOL",
                  ROUTER, flags=re.S) is not None,
    )


def main() -> int:
    print("顶栏层级结构（导航收敛）  静态检查")
    check_nav_shape()
    check_page_entries()
    check_routes()
    print(f"\nPASS {PASS} / {PASS + FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
