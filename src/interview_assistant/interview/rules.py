"""规则模式：不调用模型时的确定性出题与判断，用于无密钥核对闭环。"""

from interview_assistant.interview.schemas import DecisionDraft
from interview_assistant.planner.text import plain_text

RULE_PROMPT_VERSION = "interview_rule.v2"

FOLLOW_UP_DETAIL = "能展开说说具体做法、当时的取舍，以及最后的结果吗？"
FOLLOW_UP_BASIS = "这里最关键的判断依据是什么？换一种方案会有什么代价？"


def project_question(
    project_name: str, fact_type: str, content: str, *, topic: str = "", angle: int = 0,
) -> str:
    focus = plain_text(topic or fact_type or "项目实现")[:60]
    if angle % 2:
        return f"在「{project_name}」的「{focus}」部分，你遇到过什么困难或方案取舍？请讲一个具体例子。"
    return f"我们聊聊「{project_name}」的「{focus}」部分。你本人负责哪些工作，具体是怎样实现的？"


def objective_question(topic: str) -> str:
    return f"围绕「{topic}」，能具体讲讲你的做法、关键取舍和结果吗？"


def decide(
    answer_text: str, *, followups_remaining: int, base_remaining: int,
) -> DecisionDraft:
    """回答过短先追问细节；有实质内容就判断目标已覆盖，交回服务端推进。"""
    length = len(answer_text.strip())
    if followups_remaining > 0 and length < 60:
        return DecisionDraft(
            objective_status="insufficient",
            follow_up_needed=True,
            follow_up_question=FOLLOW_UP_DETAIL,
            observation="回答较短，未展开具体做法。",
            reason="规则模式：回答过短，先追问细节。",
        )
    if followups_remaining > 0 and length < 200 and base_remaining >= 0:
        return DecisionDraft(
            objective_status="partial",
            follow_up_needed=True,
            follow_up_question=FOLLOW_UP_BASIS,
            observation="回答有内容，但缺少判断依据。",
            reason="规则模式：补充追问判断依据。",
        )
    return DecisionDraft(
        objective_status="covered" if length >= 200 else "partial",
        follow_up_needed=False,
        observation="规则模式未做语义判断，仅按回答长度推进。",
        reason="规则模式：按长度退出当前目标。",
    )
