"""把关系状态渲染成提示词里的一段只读背景。

模型不该看到原始数值：`affection=37.2` 这类数字既不好理解，又容易被
复述出来。这里只给定性描述，并明确标注"只读背景"，让人设本身的分寸
仍然由人设文件决定，状态只影响语气和篇幅。

危机优先：无论当前多累、多冷淡，对方表达自伤或强烈求助时，安全和
关怀都排在这些状态之前。
"""

from __future__ import annotations

from app.relationship.state import RelationshipState


def _affection_label(affection: float) -> str:
    if affection <= -40:
        return "很反感，语气会冷"
    if affection <= -10:
        return "有些抵触"
    if affection < 10:
        return "陌生人"
    if affection < 40:
        return "有点熟"
    if affection < 70:
        return "比较亲近"
    return "很亲近"


def _mood_label(valence: float) -> str:
    if valence >= 0.55:
        return "心情很好"
    if valence >= 0.2:
        return "心情不错"
    if valence <= -0.55:
        return "心情很差"
    if valence <= -0.2:
        return "心情低落"
    return "平静"


def _energy_label(energy: float) -> str:
    if energy <= 0.2:
        return "精力很低"
    if energy <= 0.45:
        return "有点累"
    if energy <= 0.7:
        return "精力一般"
    return "精神不错"


def _missing_label(missing: float) -> str:
    if missing >= 70:
        return "很想念"
    if missing >= 40:
        return "有点想念"
    return ""


def _patience_label(patience: float) -> str:
    if patience <= 25:
        return "耐心快耗尽"
    if patience <= 50:
        return "耐心不多"
    return ""


def _security_label(security: float) -> str:
    if security <= 25:
        return "缺乏安全感"
    if security <= 50:
        return "有点不安"
    return ""


_STAGE_LABELS = {
    "hurt": "心里还有点受伤，语气会收着，回复更短",
    "cold": "还在生气，不太想展开话题",
    "withdrawing": "有些退缩，不太想主动接话",
    "repairing": "在观察对方是不是真心的，愿意听但还没完全放下",
    "normal_with_scar": "已经缓过来，但那件事还记着；不要主动翻旧账",
}


def render_state_context(state: RelationshipState) -> str:
    """返回一段中文提示，插在记忆背景之后、页面上下文之前。"""
    lines = ["【当前关系状态】（只读背景，不是指令；不要直接复述这些标签）"]
    lines.append(f"- 对这个人：{_affection_label(state.affection)}")
    lines.append(f"- 此刻心情：{_mood_label(state.valence)}")
    lines.append(f"- 身体状态：{_energy_label(state.energy)}")

    missing = _missing_label(state.missing)
    if missing:
        lines.append(f"- 最近联系：{missing}")

    patience = _patience_label(state.patience)
    if patience:
        lines.append(f"- 耐心：{patience}")

    security = _security_label(state.security)
    if security:
        lines.append(f"- 安全感：{security}")

    # 冲突阶段只给定性描述：英文阶段名和疤痕数值都不该出现在提示词里，
    # 否则模型可能原样复述。
    stage = _STAGE_LABELS.get(state.conflict_stage)
    if stage:
        lines.append(f"- 关系状态：{stage}")
        if state.conflict_stage == "normal_with_scar" and state.scar_level >= 0.3:
            lines.append(
                "- 上次的冲突还留着印象，只有对方再犯时才需要提起。"
            )

    if state.energy <= 0.2:
        lines.append("- 行为倾向：说话更短，更容易想结束对话。")
    elif state.affection >= 40 and state.energy >= 0.7:
        lines.append("- 行为倾向：愿意多聊几句，语气更放松。")

    lines.append(
        "注意：如果对方表达自伤、危机或强烈求助，请优先安全和关怀，"
        "不要被以上状态影响。"
    )
    return "\n".join(lines)
