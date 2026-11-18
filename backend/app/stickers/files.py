"""表情素材文件的落盘与读取。

目录布局（相对于 `backend/`）：

    data/stickers/library/<sha256>.<ext>   正式素材
    data/stickers/inbox/                   手工投放、等待导入

文件名用内容哈希，天然去重；扩展名只影响发送时的提示，格式以文件头为准。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.config import get_settings
from app.vision_common import ALLOWED_MIME, guess_mime

_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


def base_dir() -> Path:
    path = Path(get_settings().sticker_dir)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[2] / path


def library_dir() -> Path:
    return base_dir() / "library"


def inbox_dir() -> Path:
    return base_dir() / "inbox"


def ensure_dirs() -> None:
    library_dir().mkdir(parents=True, exist_ok=True)
    inbox_dir().mkdir(parents=True, exist_ok=True)


def save_bytes(data: bytes, mime: str = "") -> tuple[str, str, str]:
    """保存到 library，返回 `(content_hash, file_name, mime)`。

    同内容重复保存是幂等的；大小上限由调用方校验。
    """
    if not data:
        raise ValueError("图片内容为空")
    detected = guess_mime(data, mime)
    if not detected:
        raise ValueError("不是受支持的图片格式（JPEG/PNG/GIF/WebP）")

    content_hash = hashlib.sha256(data).hexdigest()
    extension = _EXTENSIONS.get(detected, "bin")
    file_name = f"{content_hash}.{extension}"
    ensure_dirs()
    target = library_dir() / file_name
    if not target.exists():
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_bytes(data)
        temp.replace(target)
    return content_hash, file_name, detected


def path_for(file_name: str) -> Path:
    """只接受 library 目录下的纯文件名，防止路径穿越。"""
    name = Path(str(file_name or "")).name
    if not name or name != str(file_name):
        raise ValueError("非法文件路径")
    return library_dir() / name


def delete_file(file_name: str) -> None:
    try:
        path_for(file_name).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def read_bytes(file_name: str) -> bytes:
    return path_for(file_name).read_bytes()


__all__ = [
    "ALLOWED_MIME",
    "base_dir",
    "library_dir",
    "inbox_dir",
    "ensure_dirs",
    "save_bytes",
    "path_for",
    "delete_file",
    "read_bytes",
]
