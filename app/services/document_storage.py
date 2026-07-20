import os
from pathlib import Path, PurePosixPath
from typing import Protocol


class DocumentStorageError(Exception):
    pass


class DocumentStorage(Protocol):
    backend_name: str

    def read(self, storage_key: str) -> bytes:
        pass

    def write(self, storage_key: str, content: bytes) -> None:
        pass

    def delete(self, storage_key: str) -> None:
        pass


class LocalDocumentStorage:
    backend_name = "local_filesystem"

    def __init__(self, root: Path) -> None:
        self._root = root

    def write(self, storage_key: str, content: bytes) -> None:
        target = self._resolve(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target.with_name(f".{target.name}.tmp")
        try:
            temp_path.write_bytes(content)
            os.replace(temp_path, target)
        except OSError as exc:
            _delete_if_exists(temp_path)
            raise DocumentStorageError("Document storage write failed.") from exc

    def read(self, storage_key: str) -> bytes:
        try:
            return self._resolve(storage_key).read_bytes()
        except OSError as exc:
            raise DocumentStorageError("Document storage read failed.") from exc

    def delete(self, storage_key: str) -> None:
        try:
            self._resolve(storage_key).unlink(missing_ok=True)
        except OSError as exc:
            raise DocumentStorageError("Document storage cleanup failed.") from exc

    def _resolve(self, storage_key: str) -> Path:
        path = PurePosixPath(storage_key)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise DocumentStorageError("Invalid document storage key.")
        target = (self._root / Path(*path.parts)).resolve()
        root = self._root.resolve()
        if not target.is_relative_to(root):
            raise DocumentStorageError("Invalid document storage key.")
        return target


def _delete_if_exists(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
