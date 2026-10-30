"""人设文件加载的测试。

重点不是“提示词写得好不好”，而是：
- 版本选择规则是否正确；
- 文件内容与路径不一致时是否拒绝；
- 非法 id 是否被挡住，不能读到人设目录之外；
- 文件缺失/损坏时是否给出明确错误，而不是静默使用空提示词。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.persona.models import PersonaVersion
from app.persona.store import (
    PersonaError,
    PersonaNotFound,
    PersonaStore,
)


def write_persona(
    root: Path,
    persona_id: str,
    version: int,
    *,
    core: str | None = None,
) -> Path:
    directory = root / persona_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{version}.json"
    path.write_text(
        json.dumps(
            {
                "schema": "l2d.persona/v1",
                "id": persona_id,
                "version": version,
                "name": f"测试人设 {version}",
                "core": core or f"CORE v{version}",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def write_active(root: Path, persona_id: str, version: int) -> None:
    (root / "active.json").write_text(
        json.dumps({"persona_id": persona_id, "version": version}),
        encoding="utf-8",
    )


def test_missing_active_pointer_uses_latest_version(tmp_path: Path) -> None:
    write_persona(tmp_path, "merlin", 1)
    write_persona(tmp_path, "merlin", 3)
    write_persona(tmp_path, "merlin", 2)

    store = PersonaStore(tmp_path, "merlin")
    persona = store.load_active()

    assert persona.version == 3
    assert persona.core == "CORE v3"


def test_active_pointer_selects_exact_version(tmp_path: Path) -> None:
    write_persona(tmp_path, "merlin", 1)
    write_persona(tmp_path, "merlin", 2)
    write_active(tmp_path, "merlin", 1)

    store = PersonaStore(tmp_path, "merlin")

    assert store.active_ref().version == 1
    assert store.load_active().version == 1


def test_missing_persona_raises_clear_error(tmp_path: Path) -> None:
    store = PersonaStore(tmp_path, "merlin")

    with pytest.raises(PersonaNotFound) as excinfo:
        store.load_active()

    assert "personas.example" in str(excinfo.value)


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    store = PersonaStore(tmp_path, "merlin")

    with pytest.raises(PersonaError):
        store.load("../secret", 1)


def test_file_content_must_match_path(tmp_path: Path) -> None:
    # 文件叫 3.json，内容却写 version=2；这种复制/改名错误必须被发现。
    path = write_persona(tmp_path, "merlin", 3)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["version"] = 2
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    store = PersonaStore(tmp_path, "merlin")

    with pytest.raises(PersonaError) as excinfo:
        store.load("merlin", 3)

    assert "不一致" in str(excinfo.value)


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "merlin"
    directory.mkdir()
    (directory / "1.json").write_text("{not json", encoding="utf-8")

    store = PersonaStore(tmp_path, "merlin")

    with pytest.raises(PersonaError) as excinfo:
        store.load("merlin", 1)

    assert "无法读取" in str(excinfo.value)


def test_invalid_schema_is_rejected(tmp_path: Path) -> None:
    path = write_persona(tmp_path, "merlin", 1)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema"] = "l2d.persona/v999"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    store = PersonaStore(tmp_path, "merlin")

    with pytest.raises(PersonaError) as excinfo:
        store.load("merlin", 1)

    assert "格式不正确" in str(excinfo.value)


def test_list_versions_ignores_non_numeric_files(tmp_path: Path) -> None:
    write_persona(tmp_path, "merlin", 1)
    write_persona(tmp_path, "merlin", 2)
    (tmp_path / "merlin" / "README.md").write_text("not a version", encoding="utf-8")

    store = PersonaStore(tmp_path, "merlin")

    assert store.list_versions("merlin") == [1, 2]


def test_persona_model_rejects_unknown_fields() -> None:
    with pytest.raises(Exception):
        PersonaVersion.model_validate(
            {
                "schema": "l2d.persona/v1",
                "id": "merlin",
                "version": 1,
                "name": "测试",
                "core": "CORE",
                "unexpected": "should fail",
            }
        )


def test_public_example_persona_is_valid() -> None:
    """公开仓库里的示例也必须能通过加载器，避免文档和代码脱节。"""
    example_root = Path(__file__).resolve().parents[1] / "personas.example"

    store = PersonaStore(example_root, "merlin")
    persona = store.load_active()

    assert persona.id == "merlin"
    assert persona.version == 1
    assert persona.core.strip()
