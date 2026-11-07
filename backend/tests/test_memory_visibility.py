from __future__ import annotations

from app.memory.visibility import Viewer, is_visible, sql_filter

A_WEB = Viewer(person_keys=("wp:12",), channel="web")
A_QQ = Viewer(person_keys=("wp:12",), channel="qq_private")
A_IN_GROUP = Viewer(person_keys=("wp:12",), channel="qq_group", group_id=789)
B_WEB = Viewer(person_keys=("wp:13",), channel="web")


def test_personal_memory_only_for_owner() -> None:
    assert is_visible(scope="personal", audience=["wp:12"], sensitivity="normal", viewer=A_WEB)
    assert not is_visible(scope="personal", audience=["wp:12"], sensitivity="normal", viewer=B_WEB)


def test_linked_keys_are_the_same_person() -> None:
    merged = Viewer(person_keys=("wp:12", "qq:123456"), channel="web")
    assert is_visible(scope="personal", audience=["qq:123456"], sensitivity="normal", viewer=merged)


def test_group_viewer_sees_group_and_world_only() -> None:
    assert is_visible(scope="group", audience=["group:789"], sensitivity="normal", viewer=A_IN_GROUP)
    assert not is_visible(scope="group", audience=["group:790"], sensitivity="normal", viewer=A_IN_GROUP)
    assert is_visible(scope="world", audience=["public"], sensitivity="normal", viewer=A_IN_GROUP)
    assert not is_visible(scope="personal", audience=["wp:13"], sensitivity="normal", viewer=A_IN_GROUP)


def test_private_memory_is_not_visible_in_group() -> None:
    assert is_visible(scope="personal", audience=["wp:12"], sensitivity="private", viewer=A_WEB)
    assert not is_visible(scope="personal", audience=["wp:12"], sensitivity="private", viewer=A_IN_GROUP)


def test_secret_only_in_owner_private_channel() -> None:
    assert is_visible(scope="personal", audience=["wp:12"], sensitivity="secret", viewer=A_QQ)
    assert not is_visible(scope="personal", audience=["wp:12"], sensitivity="secret", viewer=A_IN_GROUP)
    assert not is_visible(scope="world", audience=["public"], sensitivity="secret", viewer=A_WEB)


def test_merlin_layer_needs_internal_context() -> None:
    assert not is_visible(scope="merlin", audience=["wp:12"], sensitivity="normal", viewer=A_WEB)
    internal = Viewer(person_keys=("wp:12",), channel="web", include_merlin=True)
    assert is_visible(scope="merlin", audience=["wp:12"], sensitivity="normal", viewer=internal)


def test_sql_filter_includes_expected_clauses() -> None:
    where, params = sql_filter(A_IN_GROUP)
    assert "scope = 'personal'" in where
    assert "group:789" in params
    assert "scope = 'world'" in where
