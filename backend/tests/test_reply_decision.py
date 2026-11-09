"""私聊回复概率：意愿、公式、随机数与决策日志。

这一层是纯函数，重点不是“概率调得好不好”，而是：
- 公式里的每个因子都真的参与计算；
- 概率永远落在 floor / 0.98 之间，不会因为回复债无限接近 1；
- 强制回复（命令、危机）不掷骰子；
- 每次决策都能在 decision_log 里查到原因。
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from app.memory import db
from app.persona.models import PersonaVersion
from app.relationship.decision import (
    blend_willingness,
    decide,
    record_decision,
    reply_probability,
    willingness_for,
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        memory_db_path=str(tmp_path / "memory.sqlite3"),
        embedding_dim=4,
    )
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.init_schema()
    connection = db.connect()
    yield connection
    connection.close()


def dynamics(**overrides):
    data = {
        "schema": "l2d.persona/v1",
        "id": "merlin",
        "version": 1,
        "name": "梅林",
        "core": "C",
    }
    if overrides:
        data["dynamics"] = overrides
    return PersonaVersion.model_validate(data).dynamics


def test_question_and_name_raise_willingness():
    plain = willingness_for("今天天气不错")
    question = willingness_for("梅林，你在吗？")
    assert question > plain
    assert 0.05 <= plain < question <= 1.0


def test_repeat_lowers_willingness():
    first = willingness_for("在吗", recent=[])
    repeated = willingness_for("在吗", recent=["在吗", "在吗"])
    assert repeated < first


def test_probability_uses_formula_and_clamps():
    d = dynamics()
    p0, p = reply_probability(
        willingness=1.0,
        affinity=1.2,
        valence=0.0,
        energy=1.0,
        reply_debt=0,
        dynamics=d,
    )
    assert p0 == pytest.approx(0.95)
    assert p == pytest.approx(p0)

    p0, p = reply_probability(
        willingness=0.0,
        affinity=0.0,
        valence=-1.0,
        energy=0.0,
        reply_debt=0,
        dynamics=d,
    )
    assert p0 == pytest.approx(d.reply.floor)
    assert p == pytest.approx(d.reply.floor)


def test_low_patience_and_security_lower_probability():
    d = dynamics()
    high, _ = reply_probability(
        willingness=0.5,
        affinity=0.8,
        valence=0,
        energy=0.5,
        reply_debt=0,
        dynamics=d,
        patience=100,
        security=100,
    )
    low, _ = reply_probability(
        willingness=0.5,
        affinity=0.8,
        valence=0,
        energy=0.5,
        reply_debt=0,
        dynamics=d,
        patience=0,
        security=0,
    )
    assert low < high


def test_reply_debt_raises_probability_but_never_above_098():
    d = dynamics()
    _, low = reply_probability(
        willingness=0.5,
        affinity=0.8,
        valence=0,
        energy=0.5,
        reply_debt=0,
        dynamics=d,
    )
    _, high = reply_probability(
        willingness=0.5,
        affinity=0.8,
        valence=0,
        energy=0.5,
        reply_debt=5,
        dynamics=d,
    )
    assert high > low
    assert high <= 0.98


def test_force_reply_skips_roll():
    d = dynamics()
    decision = decide(
        text="救命",
        recent=[],
        affinity=0.25,
        valence=0,
        energy=0.1,
        reply_debt=0,
        dynamics=d,
        rng=random.Random(0),
        force_reply=True,
    )
    assert decision.reply is True
    assert decision.roll is None
    assert decision.reason == "forced"


def test_roll_compares_against_probability():
    d = dynamics()

    class AlwaysZero(random.Random):
        def random(self):
            return 0.0

    decision = decide(
        text="在吗",
        recent=[],
        affinity=0.79,
        valence=0,
        energy=1,
        reply_debt=0,
        dynamics=d,
        rng=AlwaysZero(0),
    )
    assert decision.reply is True
    assert decision.roll == 0.0


def test_positive_bias_raises_willingness():
    d = dynamics()
    blended = blend_willingness(0.5, 0.8, 1.0, d.reply.reply_bias_weight)
    assert blended > 0.5


def test_negative_bias_lowers_willingness():
    assert blend_willingness(0.5, -0.8, 1.0, 1.0) < 0.5


def test_bias_is_scaled_by_confidence():
    full = blend_willingness(0.5, 0.8, 1.0, 1.0)
    half = blend_willingness(0.5, 0.8, 0.5, 1.0)
    assert 0.5 < half < full


def test_blend_never_leaves_safe_range():
    assert blend_willingness(0.0, -1.0, 1.0, 5.0) >= 0.05
    assert blend_willingness(1.0, 1.0, 1.0, 5.0) <= 1.0


def test_decide_keeps_rule_willingness_and_bias():
    d = dynamics()
    decision = decide(
        text="在吗",
        recent=[],
        affinity=0.79,
        valence=0,
        energy=1,
        reply_debt=0,
        dynamics=d,
        rng=random.Random(0),
        reply_bias=-0.5,
        bias_confidence=1.0,
        appraisal_event="boundary",
    )
    assert decision.rule_willingness == pytest.approx(willingness_for("在吗", []))
    assert decision.willingness < decision.rule_willingness
    assert decision.appraisal_event == "boundary"
    assert decision.appraisal_confidence == 1.0
    assert decision.reply_bias == -0.5


def test_record_decision_records_appraisal_fields(conn):
    d = dynamics()
    decision = decide(
        text="谢谢你",
        recent=[],
        affinity=0.79,
        valence=0.1,
        energy=0.9,
        reply_debt=0,
        dynamics=d,
        rng=random.Random(1),
        reply_bias=0.4,
        bias_confidence=0.9,
        appraisal_event="care",
    )
    record_decision(
        conn,
        persona_key="merlin",
        channel_id="qq_private",
        person_key="wp:1",
        message_id=123,
        decision=decision,
        rule_score=decision.rule_willingness,
    )
    row = conn.execute("SELECT * FROM decision_log").fetchone()
    assert row["appraisal_event"] == "care"
    assert row["reply_bias"] == pytest.approx(0.4)
    assert row["appraisal_confidence"] == pytest.approx(0.9)
    assert row["rule_score"] == pytest.approx(decision.rule_willingness)
    assert row["willingness"] == pytest.approx(decision.willingness)


def test_record_decision_writes_row(conn):
    d = dynamics()
    decision = decide(
        text="在吗",
        recent=[],
        affinity=0.79,
        valence=0.1,
        energy=0.9,
        reply_debt=0,
        dynamics=d,
        rng=random.Random(1),
    )
    record_decision(
        conn,
        persona_key="merlin",
        channel_id="qq_private",
        person_key="wp:1",
        message_id=123,
        decision=decision,
    )
    row = conn.execute("SELECT * FROM decision_log").fetchone()
    assert row["persona_key"] == "merlin"
    assert row["verdict"] in {"reply", "silent"}
    assert row["roll"] == pytest.approx(decision.roll)


def test_stage_multiplier_scales_p0():
    d = dynamics()
    kwargs = dict(
        willingness=0.6, affinity=0.79, valence=0.1, energy=0.9,
        reply_debt=0, dynamics=d, patience=100.0, security=100.0,
    )
    normal, _ = reply_probability(**kwargs, stage_multiplier=1.0)
    withdrawing, _ = reply_probability(**kwargs, stage_multiplier=0.5)
    assert withdrawing == pytest.approx(normal * 0.5, rel=0.01)


def test_decide_uses_stage_multiplier_and_records_stage(conn):
    d = dynamics()
    base = dict(
        text="在吗", recent=[], affinity=0.79, valence=0.1, energy=0.9,
        reply_debt=0, dynamics=d, rng=random.Random(1),
    )
    normal = decide(**base)
    cold = decide(**base, stage="cold")
    assert cold.conflict_stage == "cold"
    assert cold.p0 < normal.p0

    record_decision(
        conn,
        persona_key="merlin",
        channel_id="qq_private",
        person_key="wp:1",
        message_id=1,
        decision=cold,
    )
    row = conn.execute("SELECT conflict_stage FROM decision_log").fetchone()
    assert row["conflict_stage"] == "cold"
