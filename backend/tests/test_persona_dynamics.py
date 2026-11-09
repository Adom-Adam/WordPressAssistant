from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.persona.models import (
    APPRAISAL_CAP_EVENTS,
    CONFLICT_STAGES,
    AppraisalDynamics,
    ConflictDynamics,
    Dynamics,
    PersonaVersion,
)


def make_persona(**overrides):
    data = {
        "schema": "l2d.persona/v1",
        "id": "merlin",
        "version": 1,
        "name": "梅林",
        "core": "CORE",
    }
    data.update(overrides)
    return PersonaVersion.model_validate(data)


def test_dynamics_defaults_are_available_without_a_block():
    persona = make_persona()
    assert persona.dynamics.reply.base == 0.95
    assert persona.dynamics.relationship.roles["owner"].initial == 60
    assert persona.dynamics.relationship.roles["exception"].initial == 0
    assert persona.dynamics.relationship.roles["special"].initial == 0
    assert persona.dynamics.relationship.roles["normal"].initial == 0
    assert persona.state_key == "merlin"
    assert persona.memory_key == "merlin"


def test_partial_dynamics_merges_with_defaults():
    persona = make_persona(
        dynamics={
            "reply": {"base": 0.8},
            "namespace": {"state": "merlin-v2"},
        }
    )
    assert persona.dynamics.reply.base == 0.8
    assert persona.dynamics.reply.affinity_mid == 0.70
    assert persona.dynamics.relationship.affection_unit == 1.0
    assert persona.state_key == "merlin-v2"
    assert persona.memory_key == "merlin"


def test_fatigue_half_life_defaults_to_sixty_minutes():
    persona = make_persona()
    assert persona.dynamics.energy.fatigue_half_life_minutes == 60.0


def test_hourly_curve_must_have_24_entries():
    with pytest.raises(ValidationError):
        make_persona(dynamics={"energy": {"hourly_curve": [0.5] * 23}})


def test_all_four_roles_are_required_when_roles_are_overridden():
    with pytest.raises(ValidationError):
        make_persona(
            dynamics={
                "relationship": {
                    "roles": {
                        "owner": {"initial": 60, "cap": 100, "growth": 1.0}
                    }
                }
            }
        )


def test_unknown_dynamics_field_is_rejected():
    with pytest.raises(ValidationError):
        make_persona(dynamics={"reply": {"unknown": 1}})


def test_chat_signal_defaults_are_settable():
    persona = make_persona()
    assert persona.dynamics.relationship.chat_affection_units == 0.5
    assert persona.dynamics.relationship.chat_valence == 0.03

    persona = make_persona(
        dynamics={
            "relationship": {"chat_affection_units": 2.0, "chat_valence": -0.05}
        }
    )
    assert persona.dynamics.relationship.chat_affection_units == 2.0
    assert persona.dynamics.relationship.chat_valence == -0.05


def test_appraisal_defaults_match_design():
    appraisal = Dynamics().appraisal
    assert appraisal.confidence_min == 0.5
    assert appraisal.daily_affection_cap == 3.0
    assert appraisal.event_caps["care"] == 1.0
    assert appraisal.event_caps["betrayal"] == -3.0
    assert set(appraisal.event_caps) == set(APPRAISAL_CAP_EVENTS)


def test_unknown_appraisal_event_is_rejected():
    caps = AppraisalDynamics().event_caps
    with pytest.raises(Exception):
        AppraisalDynamics(event_caps={**caps, "gift": 5.0})


def test_tease_high_affection_is_positive():
    appraisal = Dynamics().appraisal
    assert appraisal.tease_affection_low < 0 < appraisal.tease_affection_high
    assert appraisal.tease_valence_low < 0 < appraisal.tease_valence_high


def test_reply_bias_weight_defaults_to_one():
    assert Dynamics().reply.reply_bias_weight == 1.0


def test_conflict_defaults_match_design():
    conflict = Dynamics().conflict
    assert conflict.stage_multipliers["withdrawing"] == 0.5
    assert conflict.min_dwell_minutes["hurt"] == 30
    assert conflict.withdrawing_hours_to_scar == 336.0
    assert set(conflict.stage_multipliers) == set(CONFLICT_STAGES)


def test_unknown_stage_is_rejected():
    multipliers = Dynamics().conflict.stage_multipliers
    with pytest.raises(Exception):
        ConflictDynamics(stage_multipliers={**multipliers, "angry": 0.1})


# ---------------------------------------------------------------- 4b
def test_proactive_and_group_defaults():
    dynamics = make_persona().dynamics

    assert dynamics.proactive.recent_active_minutes == 10.0
    assert dynamics.proactive.repeat_similarity == 0.6
    assert dynamics.proactive.ignore_grace_hours == 24.0
    assert dynamics.proactive.ignore_affection_points == 0.2
    assert dynamics.proactive.ignore_security_points == 0.5
    assert dynamics.proactive.compose_timeout_seconds == 10.0
    assert dynamics.group.reply_floor == 0.90


def test_proactive_and_group_reject_bad_values():
    with pytest.raises(ValidationError):
        make_persona(dynamics={"proactive": {"repeat_similarity": 2}})
    with pytest.raises(ValidationError):
        make_persona(dynamics={"group": {"reply_floor": 1.5}})
