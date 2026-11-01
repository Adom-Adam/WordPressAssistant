"""绑定码与 QQ ↔ 博客账号绑定的存储层测试。

这里不碰 WordPress：绑定码明文在 PHP 那边，后端只收到哈希。
所以测试只验证后端该负责的三件事——哈希归一化、绑定判定的优先级、解绑与兼容迁移。
"""

from __future__ import annotations

import sqlite3

from app.credits import CreditStore, hash_bind_code, is_valid_bind_code


def make_store(tmp_path) -> CreditStore:
    return CreditStore(str(tmp_path / "quota.sqlite3"))


def test_hash_normalizes_width_and_separators() -> None:
    assert hash_bind_code("1234-5678") == hash_bind_code("１２３４ ５６７８")
    assert hash_bind_code("1234-5678") == hash_bind_code(" 12345678 ")
    assert len(hash_bind_code("1234-5678")) == 64


def test_is_valid_bind_code() -> None:
    assert is_valid_bind_code("1234-5678")
    assert is_valid_bind_code("12345678")
    assert not is_valid_bind_code("1234-567")
    assert not is_valid_bind_code("123456789")
    assert not is_valid_bind_code("abcd-efgh")
    # NFKC 不会把阿拉伯-印度数字折成 ASCII；只认 0-9，否则 hash 时编码会炸。
    assert not is_valid_bind_code("١٢٣٤٥٦٧٨")


def test_bind_code_hash_roundtrip(tmp_path) -> None:
    store = make_store(tmp_path)
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=True)

    result = store.bind_qq("10001", hash_bind_code("1234-5678"))

    assert (result.status, result.user_id) == ("ok", 7)
    assert store.qq_uin_of(7) == "10001"
    binding = store.binding_of_qq("10001")
    assert binding is not None
    assert binding.user_id == 7
    assert binding.is_admin is True
    assert binding.tier == "user"


def test_invalid_code_is_rejected(tmp_path) -> None:
    store = make_store(tmp_path)
    store.set_bind_code(7, hash_bind_code("1234-5678"), is_admin=False)

    assert store.bind_qq("10001", hash_bind_code("8765-4321")).status == "invalid_code"


def test_qq_already_bound_to_other_account(tmp_path) -> None:
    store = make_store(tmp_path)
    store.set_bind_code(7, hash_bind_code("1111-1111"), is_admin=False)
    store.set_bind_code(8, hash_bind_code("2222-2222"), is_admin=False)
    assert store.bind_qq("10001", hash_bind_code("1111-1111")).status == "ok"

    result = store.bind_qq("10001", hash_bind_code("2222-2222"))

    assert result.status == "qq_taken"
    assert store.qq_uin_of(8) is None


def test_account_already_bound_to_other_qq(tmp_path) -> None:
    store = make_store(tmp_path)
    store.set_bind_code(7, hash_bind_code("3333-3333"), is_admin=False)
    assert store.bind_qq("10001", hash_bind_code("3333-3333")).status == "ok"

    result = store.bind_qq("20002", hash_bind_code("3333-3333"))

    assert result.status == "account_taken"
    assert store.qq_uin_of(7) == "10001"


def test_unbind_clears_qq_and_code(tmp_path) -> None:
    store = make_store(tmp_path)
    store.set_bind_code(7, hash_bind_code("1111-1111"), is_admin=False)
    store.bind_qq("10001", hash_bind_code("1111-1111"))

    assert store.unbind_user(7) is True
    assert store.qq_uin_of(7) is None
    assert store.binding_of_qq("10001") is None
    # 解绑后旧码立即失效
    assert store.bind_qq("20002", hash_bind_code("1111-1111")).status == "invalid_code"
    assert store.unbind_user(7) is False


def test_migration_adds_columns_to_existing_db(tmp_path) -> None:
    path = tmp_path / "quota.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE user_profile (
            user_id INTEGER PRIMARY KEY,
            tier TEXT NOT NULL DEFAULT 'user',
            tier_expires_at TEXT,
            qq_uin TEXT,
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO user_profile (user_id, created_at, updated_at)
        VALUES (5, '2026-01-01T00:00:00+08:00', '2026-01-01T00:00:00+08:00');
        """
    )
    conn.commit()
    conn.close()

    store = CreditStore(str(path))
    store.set_bind_code(5, hash_bind_code("1234-5678"), is_admin=False)

    assert store.bind_qq("10001", hash_bind_code("1234-5678")).user_id == 5
