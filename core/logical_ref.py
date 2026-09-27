"""
逻辑引用（Logical Reference）

============================================================
【这个模块解决什么问题】
============================================================

项目里有多处需要「引用一个东西，但不给它访问能力」：

    TaskResultRef.result_ref    承/接方提交的成果（Step 4D）
    TaskSharedContext.artifact_refs   共享上下文里的相关引用（Step 4F）

它们必须是**逻辑名字**，例如：

    "analysis-v2"
    "parser-design-b"
    "spec-doc-3"

而**不能**是：

    "/etc/passwd"              ← 文件路径
    "../../secrets"            ← 相对路径逃逸
    "~/Documents"              ← home 路径
    "https://example.com/x"    ← URL

============================================================
【为什么不只是「洁癖」】
============================================================

这是一条**安全约束**，不是格式偏好：

    如果引用长得像路径，下游（人或代码）就会倾向于把它当路径用。
    一旦真的 open(ref)，提交方就能让**别人**去读任意文件。

所以在**数据类型这一层**就挡住 —— 让它根本不像一个可访问目标。

============================================================
【为什么抽成共用函数，而不是各写一遍】
============================================================

Step 4D 把这条规则写在 `TaskResultRef.__post_init__` 里。
Step 4F 需要**同一条**规则。

这时有两条路：

    ❌ 复制一份     → 两处安全规则，迟早漂移；
                      改了 A 忘了 B 就留下一个漏洞
    ✅ 抽出来共用   → 只有一处，改一次两边都变

所以抽到这里。`TaskResultRef` 改为调用本函数，
**行为完全不变**（有回归测试钉住）。

（Step 4F 的规范允许为这种情况做极小修改，条件是：
  证明必要 + 保持旧行为 + 加回归测试 + 在报告中说明。）
"""

from __future__ import annotations

#: 被视为「像文件系统路径」的前缀。
#: 注意 `..` 单独不在这里 —— 它由「路径分量」检查处理，
#: 因为 `"a/../b"` 这种中间含 `..` 的形式也要挡住。
_PATH_PREFIXES = ("/", "~", "./", "../", "\\")

#: 单个引用允许的最大长度（防止把大段文本塞进「名字」里）
MAX_REF_LENGTH = 200


class InvalidLogicalRef(ValueError):
    """
    引用不合法（是路径、是 URL、为空、太长……）。

    继承 `ValueError` 而不是自定义基类 ——
    因为 `TaskResultRef` 原来抛的就是 `ValueError`，
    换成别的类型会改变已发布的异常契约（有测试在断言它）。
    """


def validate_logical_ref(
    ref: object,
    *,
    field: str = "ref",
    allow_empty: bool = False,
) -> str:
    """
    校验并规范化一个逻辑引用。

    参数：
        ref          待校验的值
        field        字段名（用在错误信息里，便于定位）
        allow_empty  允许空字符串（返回 ""）

    返回：
        去掉首尾空白的引用

    抛出：
        InvalidLogicalRef

    【规则】
        1. 必须是字符串
        2. 不能是空白（除非 allow_empty=True）
        3. 不能以路径前缀开头（/ ~ ./ ../ \\）
        4. 不能含 "://"（URL）
        5. 不能含空字节
        6. 路径分量里不能有 ".."（挡住 "a/../b"）
        7. 长度不能超过 MAX_REF_LENGTH
    """
    if allow_empty and (ref is None or ref == ""):
        return ""

    if not isinstance(ref, str):
        raise InvalidLogicalRef(
            f"{field} 必须是字符串，收到 {type(ref).__name__}。"
        )

    value = ref.strip()

    if not value:
        raise InvalidLogicalRef(
            f"{field} 不能是空白。\n"
            f"引用必须说明「指向的是什么」—— 不能是一个空名字。"
        )

    if len(value) > MAX_REF_LENGTH:
        raise InvalidLogicalRef(
            f"{field} 太长（{len(value)} > {MAX_REF_LENGTH}）。\n"
            f"引用是一个**名字**，不是一段文本。"
        )

    if value.startswith(_PATH_PREFIXES):
        raise InvalidLogicalRef(
            f"{field} 不能是文件系统路径：{value!r}\n"
            f"（它是一个逻辑名字，比如 'analysis-v2'。\n"
            f" 允许路径会让提交方通过构造一个名字让别人去读任意文件。）"
        )

    if "://" in value:
        raise InvalidLogicalRef(
            f"{field} 不能是 URL：{value!r}\n"
            f"（它是一个逻辑名字，不代表任何可访问的地址。）"
        )

    if "\x00" in value:
        raise InvalidLogicalRef(
            f"{field} 不能包含空字节。"
        )

    # 路径分量检查：挡住 "a/../b"、"a/.."、".."
    # （开头的 "../" 已经由前缀检查挡住了，这里补中间和结尾的）
    if ".." in value.split("/"):
        raise InvalidLogicalRef(
            f"{field} 不能包含 '..' 路径分量：{value!r}\n"
            f"（相对路径逃逸的常见形式。）"
        )

    return value


def is_valid_logical_ref(ref: object) -> bool:
    """`validate_logical_ref` 的布尔版本（不抛异常）。"""
    try:
        validate_logical_ref(ref)
    except InvalidLogicalRef:
        return False
    return True
