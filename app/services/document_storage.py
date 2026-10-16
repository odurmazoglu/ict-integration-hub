import os
from pathlib import Path, PurePosixPath
from typing import Protocol


class DocumentStorageError(Exception):
    pass


class InvalidDocumentStorageKeyError(DocumentStorageError):
    """The key itself is unusable (absolute, empty, ``.``/``..`` parts, or escapes the root).

    Deterministic: the same key fails the same way on every attempt.
    """


class DocumentNotFoundError(DocumentStorageError):
    """No document exists at a valid key while the storage root is verified accessible.

    Deterministic, unlike :class:`DocumentStorageUnavailableError`: the root was proven
    present, readable and populated, so the document itself is gone.
    """


class DocumentStorageUnavailableError(DocumentStorageError):
    """The storage itself could not be read right now (root missing, unmounted or empty,
    permission denied, I/O error). Transient: never a verdict about the document."""


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
        target = self._resolve(storage_key)
        try:
            return target.read_bytes()
        except FileNotFoundError as exc:
            # A missing file is only a verdict about the document when the root itself is
            # verified; an absent or empty (e.g. unmounted volume) root is an outage.
            if self._root_verified():
                raise DocumentNotFoundError("Stored document was not found.") from exc
            raise DocumentStorageUnavailableError("Document storage root is not available.") from exc
        except OSError as exc:
            raise DocumentStorageUnavailableError("Document storage read failed.") from exc

    def delete(self, storage_key: str) -> None:
        try:
            self._resolve(storage_key).unlink(missing_ok=True)
        except OSError as exc:
            raise DocumentStorageError("Document storage cleanup failed.") from exc

    def _resolve(self, storage_key: str) -> Path:
        if not isinstance(storage_key, str) or "\x00" in storage_key:
            raise InvalidDocumentStorageKeyError("Invalid document storage key.")
        path = PurePosixPath(storage_key)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise InvalidDocumentStorageKeyError("Invalid document storage key.")
        target = (self._root / Path(*path.parts)).resolve()
        root = self._root.resolve()
        if not target.is_relative_to(root):
            raise InvalidDocumentStorageKeyError("Invalid document storage key.")
        return target

    def _root_verified(self) -> bool:
        """The root exists, is a readable/traversable directory and is populated.

        An empty root is treated as unavailable: an unmounted volume usually leaves an
        empty mountpoint behind, and a populated production store is never empty.
        """

        try:
            if not self._root.is_dir() or not os.access(self._root, os.R_OK | os.X_OK):
                return False
            with os.scandir(self._root) as entries:
                return next(entries, None) is not None
        except OSError:
            return False


def _delete_if_exists(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
