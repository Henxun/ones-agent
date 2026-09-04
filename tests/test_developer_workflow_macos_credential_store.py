from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat

import pytest

from src.developer_workflow.credential_store import (
    CRED_TYPE_GENERIC,
    CredentialStore,
    CredentialStoreError,
    _CredentialRecord,
    credential_target,
)
from src.developer_workflow.macos_credentials import (
    _ERR_SEC_DUPLICATE_ITEM,
    MacOSCredentialStore,
    _ERR_SEC_SUCCESS,
    _SecurityFrameworkBackend,
    _macos_generation_lock,
    _prepare_lock_root,
)
from src.developer_workflow.setup_models import RuntimeSecrets, SecretKind


@dataclass
class FakeKeychainBackend:
    records: dict[str, _CredentialRecord]

    def __init__(self) -> None:
        self.records = {}
        self.write_count = 0
        self.fail_on_write: int | None = None
        self.last_write_buffer: bytearray | None = None

    def write_generic(
        self, target: str, value: bytearray, *, persist: str
    ) -> None:
        assert persist == "local_machine"
        self.write_count += 1
        self.last_write_buffer = value
        if self.fail_on_write == self.write_count:
            raise RuntimeError("TOKEN-SECRET Keychain detail")
        self.records[target] = _CredentialRecord(
            target=target,
            credential_type=CRED_TYPE_GENERIC,
            blob=bytearray(value),
        )

    def write_fresh_generic(
        self, target: str, value: bytearray, *, persist: str
    ) -> bool:
        if target in self.records:
            return False
        self.write_generic(target, value, persist=persist)
        return True

    def read_generic(self, target: str) -> _CredentialRecord | None:
        record = self.records.get(target)
        if record is None:
            return None
        return _CredentialRecord(
            target=record.target,
            credential_type=record.credential_type,
            blob=bytearray(record.blob),
        )

    def delete_generic(self, target: str) -> bool:
        return self.records.pop(target, None) is not None

    def list_generic_targets(self, prefix: str) -> tuple[str, ...]:
        return tuple(target for target in self.records if target.startswith(prefix))


class _SuccessfulSecurity:
    def SecItemAdd(self, query: int, result: None) -> int:
        assert query == 100
        assert result is None
        return _ERR_SEC_SUCCESS


class RecordingQueryBackend(_SecurityFrameworkBackend):
    """无需访问真实 Keychain，只记录 ctypes backend 组装的 query keys。"""

    __slots__ = ("query_keys", "strings")

    def __init__(self) -> None:
        self._security = _SuccessfulSecurity()
        self._constants = {
            "kSecClassGenericPassword": 1,
            "kSecAttrService": 2,
            "kSecAttrAccount": 3,
            "kSecValueData": 4,
        }
        self.query_keys: list[str] = []
        self.strings: list[str] = []

    def _dictionary(self) -> int:
        return 100

    def _string(self, value: str) -> int:
        assert value == "ones-dev.credentials" or value.startswith("ones-dev/")
        self.strings.append(value)
        return 200

    def _data(self, value: bytearray) -> int:
        assert value
        return 300

    def _set(self, dictionary: int, key: str, value: int) -> None:
        assert dictionary == 100
        self.query_keys.append(key)

    def _release_all(self, values: list[int]) -> None:
        return None


class FailingQueryBackend(_SecurityFrameworkBackend):
    __slots__ = ("released", "string_calls")

    def __init__(self) -> None:
        self._constants = {"kSecClassGenericPassword": 1}
        self.released: list[int] = []
        self.string_calls = 0

    def _dictionary(self) -> int:
        return 100

    def _string(self, value: str) -> int:
        self.string_calls += 1
        if self.string_calls == 2:
            raise OSError("allocation failed")
        return 200

    def _set(self, dictionary: int, key: str, value: int) -> None:
        return None

    def _release_all(self, values: list[int]) -> None:
        self.released.extend(reversed(values))


class _DuplicateSecurity:
    def SecItemAdd(self, query: int, result: None) -> int:
        del query, result
        return _ERR_SEC_DUPLICATE_ITEM


class FailingUpdateAttributesBackend(_SecurityFrameworkBackend):
    __slots__ = ("dictionary_calls", "released", "value")

    def __init__(self) -> None:
        self._security = _DuplicateSecurity()
        self._constants = {
            "kSecClassGenericPassword": 1,
            "kSecAttrService": 2,
            "kSecAttrAccount": 3,
            "kSecValueData": 4,
        }
        self.dictionary_calls = 0
        self.released: list[int] = []
        self.value = 200

    def _dictionary(self) -> int:
        self.dictionary_calls += 1
        if self.dictionary_calls == 3:
            raise OSError("allocation failed")
        return 100 + self.dictionary_calls

    def _string(self, value: str) -> int:
        del value
        self.value += 1
        return self.value

    def _data(self, value: bytearray) -> int:
        del value
        self.value += 1
        return self.value

    def _set(self, dictionary: int, key: str, value: int) -> None:
        del dictionary, key, value

    def _release_all(self, values: list[int]) -> None:
        self.released.extend(reversed(values))


def test_macos_store_implements_protocol_and_generation_lifecycle() -> None:
    backend = FakeKeychainBackend()
    store: CredentialStore = MacOSCredentialStore(backend)
    generation = "a" * 32
    secrets = RuntimeSecrets(
        {
            SecretKind.ONES_EMAIL: "person@example.com",
            SecretKind.ONES_PASSWORD: "TOKEN-SECRET",
        }
    )

    assert store.write_fresh_generation("profile-1", generation, secrets) is True
    assert store.read_generation(
        "profile-1",
        generation,
        (SecretKind.ONES_EMAIL, SecretKind.ONES_PASSWORD),
    ) == secrets
    assert store.list_generations("profile-1") == (generation,)
    store.delete_generation("profile-1", generation)
    assert store.list_generations("profile-1") == ()


def test_generation_write_rolls_back_partial_keychain_failure() -> None:
    backend = FakeKeychainBackend()
    backend.fail_on_write = 2
    store = MacOSCredentialStore(backend)

    with pytest.raises(CredentialStoreError, match="^credential operation failed$"):
        store.write_fresh_generation(
            "profile-1",
            "a" * 32,
            RuntimeSecrets(
                {
                    SecretKind.ONES_EMAIL: "person@example.com",
                    SecretKind.ONES_PASSWORD: "TOKEN-SECRET",
                }
            ),
        )

    assert backend.records == {}


def test_fresh_generation_never_overwrites_a_duplicate_keychain_item() -> None:
    backend = FakeKeychainBackend()
    generation = "d" * 32
    target = credential_target("profile-1", generation, SecretKind.ONES_PASSWORD)
    backend.records[target] = _CredentialRecord(
        target=target,
        credential_type=CRED_TYPE_GENERIC,
        blob=bytearray(b"original-secret"),
    )
    store = MacOSCredentialStore(backend)

    with pytest.raises(CredentialStoreError, match="^credential operation failed$"):
        store.write_fresh_generation(
            "profile-1",
            generation,
            RuntimeSecrets({SecretKind.ONES_PASSWORD: "replacement-secret"}),
        )

    assert backend.records[target].blob == bytearray(b"original-secret")


def test_macos_store_zeroizes_mutable_buffer_and_sanitizes_backend_error() -> None:
    backend = FakeKeychainBackend()
    backend.fail_on_write = 1
    store = MacOSCredentialStore(backend)

    with pytest.raises(CredentialStoreError) as captured:
        store.write(
            "profile-1", "a" * 32, SecretKind.ONES_PASSWORD, "TOKEN-SECRET"
        )

    assert backend.last_write_buffer == bytearray(len("TOKEN-SECRET"))
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "TOKEN-SECRET" not in repr(captured.value)


@pytest.mark.skipif(os.name != "posix", reason="flock is POSIX-only")
def test_generation_lock_is_owner_only_and_hides_profile(
    tmp_path: Path,
) -> None:
    os.chmod(tmp_path, 0o700)
    with _macos_generation_lock(tmp_path, "private-profile", "a" * 32):
        files = tuple(tmp_path.iterdir())
        assert len(files) == 1
        assert "private-profile" not in files[0].name
        metadata = files[0].stat()
        assert stat.S_ISREG(metadata.st_mode)
        assert stat.S_IMODE(metadata.st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="owner-only modes are POSIX-only")
def test_lock_root_prepares_shared_application_support_parent(
    tmp_path: Path,
) -> None:
    os.chmod(tmp_path, 0o700)
    app_root = tmp_path / "ones-dev"
    lock_root = app_root / "credential-locks"

    assert _prepare_lock_root(lock_root) == lock_root.resolve(strict=True)
    assert stat.S_IMODE(app_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock_root.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="flock is POSIX-only")
def test_generation_lock_rejects_preexisting_permissive_file(
    tmp_path: Path,
) -> None:
    os.chmod(tmp_path, 0o700)
    generation = "b" * 32
    # Discover the digest-only name without exposing it as a production API.
    import hashlib

    digest = hashlib.sha256(
        f"profile-1\x00{generation}".encode("ascii")
    ).hexdigest()
    lock_path = tmp_path / f"generation-{digest}.lock"
    lock_path.write_text("")
    os.chmod(lock_path, 0o644)

    with pytest.raises(CredentialStoreError, match="^credential operation failed$"):
        with _macos_generation_lock(tmp_path, "profile-1", generation):
            pass


def test_fake_backend_targets_remain_canonical_and_secret_free() -> None:
    backend = FakeKeychainBackend()
    store = MacOSCredentialStore(backend)
    store.write(
        "profile-1", "c" * 32, SecretKind.ONES_PASSWORD, "TOKEN-SECRET"
    )

    assert tuple(backend.records) == (
        credential_target("profile-1", "c" * 32, SecretKind.ONES_PASSWORD),
    )
    assert all("TOKEN-SECRET" not in target for target in backend.records)


@pytest.mark.parametrize("fresh", [False, True])
def test_add_query_uses_default_keychain_without_dpk_only_attributes(
    fresh: bool,
) -> None:
    backend = RecordingQueryBackend()
    target = credential_target(
        "profile-1", "e" * 32, SecretKind.ONES_PASSWORD
    )

    if fresh:
        assert backend.write_fresh_generic(
            target, bytearray(b"TOKEN-SECRET"), persist="local_machine"
        ) is True
    else:
        backend.write_generic(
            target, bytearray(b"TOKEN-SECRET"), persist="local_machine"
        )

    assert backend.query_keys == [
        "kSecClass",
        "kSecAttrService",
        "kSecAttrAccount",
        "kSecValueData",
    ]
    assert "kSecAttrAccessible" not in backend.query_keys
    assert "kSecUseDataProtectionKeychain" not in backend.query_keys
    assert backend.strings[0] == "ones-dev.credentials"


def test_base_query_releases_partial_core_foundation_allocations() -> None:
    backend = FailingQueryBackend()

    with pytest.raises(OSError, match="allocation failed"):
        backend._base_query("ones-dev/profile/generation/password")

    assert backend.released == [200, 100]


def test_update_releases_query_when_attribute_allocation_fails() -> None:
    backend = FailingUpdateAttributesBackend()

    with pytest.raises(OSError, match="allocation failed"):
        backend.write_generic(
            "ones-dev/profile/generation/password",
            bytearray(b"TOKEN-SECRET"),
            persist="local_machine",
        )

    assert 102 in backend.released
