from __future__ import annotations

from app.relationship.appraisal import (
    AppraisalResult,
    build_appraisal_prompt,
    parse_appraisal,
)


def test_parse_valid_appraisal():
    result = parse_appraisal(
        '{"event":"care","intensity":0.8,"confidence":0.9,'
        '"reply_bias":0.3,"reason":"关心"}'
    )
    assert result == AppraisalResult("care", 0.8, 0.9, 0.3, "关心")


def test_parse_strips_code_fence():
    raw = '```json\n{"event":"apology","intensity":1,"confidence":1,"reply_bias":0}\n```'
    assert parse_appraisal(raw).event == "apology"


def test_parse_rejects_unknown_event():
    assert parse_appraisal('{"event":"gift","intensity":1,"confidence":1}') is None


def test_parse_rejects_non_object_and_broken_json():
    assert parse_appraisal("我不知道") is None
    assert parse_appraisal("[1,2,3]") is None
    assert parse_appraisal('{"event":') is None


def test_parse_clamps_ranges():
    result = parse_appraisal('{"event":"tease","intensity":9,"confidence":-1,"reply_bias":5}')
    assert (result.intensity, result.confidence, result.reply_bias) == (1.0, 0.0, 1.0)


def test_parse_truncates_reason_to_80_chars():
    raw = '{"event":"care","reason":"' + "长" * 200 + '"}'
    assert len(parse_appraisal(raw).reason) == 80


def test_parse_missing_numbers_defaults_to_zero():
    result = parse_appraisal('{"event":"neutral"}')
    assert result == AppraisalResult("neutral", 0.0, 0.0, 0.0, "")


def test_prompt_lists_events_and_forbids_decisions():
    prompt = build_appraisal_prompt(
        persona_name="梅林",
        user_text="你今天辛苦了",
        recent=["上一句"],
        state_context="【当前关系状态】平静",
        local_time="2026-11-09 21:30",
        recent_events=["上次她关心了梅林"],
    )
    assert "care" in prompt
    assert "betrayal" in prompt
    assert "只输出一个 JSON 对象" in prompt
    assert "不要" in prompt and "回不回" in prompt
    assert "2026-11-09 21:30" in prompt
    assert "上次她关心了梅林" in prompt
    assert "平静" in prompt
