"""Fixed runtime locations and owner-only storage helpers."""

from __future__ import annotations

import os
import platform
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re
import stat
import tomllib
from collections.abc import Iterator
from pathlib import Path


class ConfigurationError(RuntimeError):
    """Raised when a fixed local runtime boundary is unsafe or unavailable."""


DEFAULT_ALIAS = "default"
# A named alias is a closed token; `default` matches it but is reserved for today's single account.
ALIAS_GRAMMAR = re.compile(r"[a-z][a-z0-9]{0,11}")
# Set only by named_aliases_for_tests until the phase B registry admits named aliases.
_NAMED_ALIASES_ADMITTED: ContextVar[bool] = ContextVar("telegram_search_mcp_named_aliases", default=False)


class AccountPathsError(ConfigurationError):
    """A fixed refusal; the rejected alias is never echoed."""

    def __init__(self) -> None:
        self.code = "alias_unavailable"
        super().__init__(self.code)


@contextmanager
def named_aliases_for_tests() -> Iterator[None]:
    """The explicit test hook: inside it, and only there, an AccountPaths may hold a named alias.

    Nothing in src or packaging enters it (tests/test_account_paths.py checks); phase B's registry becomes
    the only other way in.
    """
    token = _NAMED_ALIASES_ADMITTED.set(True)
    try:
        yield
    finally:
        _NAMED_ALIASES_ADMITTED.reset(token)


@dataclass(frozen=True)
class AccountPaths:
    """Every per-account location and name; the default alias is today's single account in today's paths.

    `for_alias(DEFAULT_ALIAS, home)` is the only public constructor until the alias registry exists
    (0.55 phase B); any other alias refuses with `alias_unavailable`. Every construction, the generated
    __init__, dataclasses.replace and `relocated` included, refuses a named alias outside
    named_aliases_for_tests. The Keychain service is the one name every alias shares (spec decision 6);
    the Whisper model stays shared outside (WHISPER_MODEL_PATH). Call sites take `account=None` for the
    default alias, which keeps every 0.54 call and constant.
    """
    alias: str
    root: Path
    session: Path
    database: Path
    files: Path
    run: Path
    socket: Path
    broker_lock: Path
    connect_lock: Path
    connect_status: Path
    connect_plist: Path
    artifacts: Path
    artifacts_quarantine: Path
    uploads: Path
    uploads_quarantine: Path
    staging: Path
    staging_quarantine: Path
    policy: Path
    enrollment_record: Path
    keychain_service: str
    broker_label: str
    broker_plist: Path
    connect_label: str

    def __post_init__(self) -> None:
        if type(self.alias) is not str or (self.alias != DEFAULT_ALIAS and (
                not _NAMED_ALIASES_ADMITTED.get() or ALIAS_GRAMMAR.fullmatch(self.alias) is None)):
            raise AccountPathsError()

    @property
    def server_name(self) -> str:
        """The MCP server name, read through the product identity at call time like every product name."""
        from . import product_identity as identity
        return identity.MCP_SERVER_NAME if self.alias == DEFAULT_ALIAS else f"{identity.MCP_SERVER_NAME}_{self.alias}"

    @classmethod
    def for_alias(cls, alias: str, home: Path) -> AccountPaths:
        if type(alias) is not str or alias != DEFAULT_ALIAS:
            raise AccountPathsError()
        return cls._home_layout(DEFAULT_ALIAS, Path(home), ("TelegramSearchMCP",), "")

    @classmethod
    def _derive(cls, alias: str, home: Path) -> AccountPaths:
        """A named alias's layout: a sibling of the legacy tree, never inside it (spec decision 6).

        Private, and `default` is reserved: it never takes this layout. Building the result needs the
        explicit test hook until the registry (phase B) admits named aliases.
        """
        if type(alias) is not str or alias == DEFAULT_ALIAS or ALIAS_GRAMMAR.fullmatch(alias) is None:
            raise AccountPathsError()
        return cls._home_layout(alias, Path(home), ("TelegramSearchMCP-accounts", alias), f".a.{alias}")

    @classmethod
    def _home_layout(cls, alias: str, home: Path, root: tuple[str, ...], job_suffix: str) -> AccountPaths:
        service = f"com.{home.name}.telegram-search-mcp"
        job = service + job_suffix
        broker_label = f"{job}.broker"
        return cls._layout(alias, home.joinpath("Library", "Application Support", *root), service, broker_label,
                           home / "Library" / "LaunchAgents" / f"{broker_label}.plist", f"{job}.connect")

    def relocated(self, root: Path) -> AccountPaths:
        """The same names with every location under `root`: the inert test boundary of SetupPaths(root)."""
        return self._layout(self.alias, root, self.keychain_service, self.broker_label, self.broker_plist,
                            self.connect_label)

    @classmethod
    def _layout(cls, alias: str, root: Path, keychain_service: str, broker_label: str, broker_plist: Path,
                connect_label: str) -> AccountPaths:
        session, run = root / "tdlib", root / "run"
        artifacts, uploads, staging = root / "artifacts", root / "incoming-uploads", root / "outgoing-staging"
        return cls(alias=alias, root=root, session=session, database=session / "db", files=session / "files",
                   run=run, socket=run / "broker.sock", broker_lock=run / "broker.lock",
                   connect_lock=run / "connect.lock", connect_status=run / "connect-status.json",
                   connect_plist=run / f"{connect_label}.plist",
                   artifacts=artifacts, artifacts_quarantine=artifacts.with_name(artifacts.name + "-quarantine"),
                   uploads=uploads, uploads_quarantine=uploads.with_name(uploads.name + "-quarantine"),
                   staging=staging, staging_quarantine=staging.with_name(staging.name + "-quarantine"),
                   policy=root / "runtime.toml", enrollment_record=root / "enrollment.json",
                   keychain_service=keychain_service, broker_label=broker_label, broker_plist=broker_plist,
                   connect_label=connect_label)


def _owner_paths(home: Path) -> tuple[Path, str, str, Path]:
    """Derive per-user runtime locations without recording an account name."""
    paths = AccountPaths.for_alias(DEFAULT_ALIAS, home)
    return paths.root, paths.keychain_service, paths.broker_label, paths.broker_plist


DEFAULT_ACCOUNT_PATHS = AccountPaths.for_alias(DEFAULT_ALIAS, Path.home())
APP_SUPPORT_ROOT, KEYCHAIN_SERVICE, LAUNCH_AGENT_LABEL, LAUNCH_AGENT_PLIST = (
    DEFAULT_ACCOUNT_PATHS.root, DEFAULT_ACCOUNT_PATHS.keychain_service, DEFAULT_ACCOUNT_PATHS.broker_label,
    DEFAULT_ACCOUNT_PATHS.broker_plist)
TDLIB_SESSION_DIRECTORY = DEFAULT_ACCOUNT_PATHS.session
TDLIB_LIBRARY = Path("/opt/homebrew/opt/tdlib/lib/libtdjson.dylib")
EXPECTED_TDLIB_KEG = "HEAD-d1085f9"
# The Homebrew/source path answers the same synchronous getOption probe the bundle path runs.
EXPECTED_TDLIB_VERSION = "1.8.67"
EXPECTED_TDLIB_COMMIT = "d1085f9cebc5a62379991ae1652673954f229c1f"
HOMEBREW_OPENSSL = Path("/opt/homebrew/opt/openssl@3")
BROKER_RUNTIME_DIRECTORY = DEFAULT_ACCOUNT_PATHS.run
BROKER_SOCKET_PATH = DEFAULT_ACCOUNT_PATHS.socket
BROKER_LOCK_PATH = DEFAULT_ACCOUNT_PATHS.broker_lock
CONNECT_JOB_LABEL = DEFAULT_ACCOUNT_PATHS.connect_label
CACHE_DIRECTORY = DEFAULT_ACCOUNT_PATHS.artifacts
UPLOAD_ROOT = DEFAULT_ACCOUNT_PATHS.uploads
STAGING_ROOT = DEFAULT_ACCOUNT_PATHS.staging
# One speech model for every account (spec 3A), beside the default account's tree.
WHISPER_MODEL_PATH = APP_SUPPORT_ROOT / "models" / "ggml-small.bin"


def ensure_private_directory(path: Path) -> Path:
    """Create a non-symlink directory and enforce owner-only permissions."""
    if path.is_symlink():
        raise ConfigurationError("refusing a symlink storage directory")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise ConfigurationError("unable to prepare private storage") from error
    if not path.is_dir() or path.is_symlink():
        raise ConfigurationError("private storage path is not a directory")
    try:
        os.chmod(path, 0o700)
    except OSError as error:
        raise ConfigurationError("unable to secure private storage") from error
    return path


def validate_tdlib_runtime() -> Path:
    """Verify the approved Homebrew TDLib runtime without reading its contents."""
    return tdlib_runtime()[0]


def tdlib_runtime() -> tuple[Path, tuple[str, str] | None]:
    """The approved library and, on the Homebrew path, the (version, commit) it must answer.

    The bundled path returns None: validate_native already ran the synchronous probe.
    """
    from .bundle_runtime import selected_native
    bundled = selected_native()
    if bundled is not None:
        return bundled, None
    return _homebrew_tdlib(), (EXPECTED_TDLIB_VERSION, EXPECTED_TDLIB_COMMIT)


def homebrew_openssl_keg() -> str:
    """The OpenSSL keg the Homebrew TDLib loads (`brew upgrade` can move it); report only."""
    try:
        resolved = HOMEBREW_OPENSSL.resolve(strict=True)
    except OSError:
        return "unavailable"
    parts = resolved.parts
    if len(parts) < 3 or parts[-2] != "openssl@3" or parts[-3] != "Cellar":
        return "unrecognized"
    version = parts[-1]
    if not 1 <= len(version) <= 32 or not all(c.isascii() and (c.isalnum() or c in "._-") for c in version):
        return "unrecognized"
    return "openssl@3 " + version


def _homebrew_tdlib() -> Path:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ConfigurationError("the approved TDLib runtime requires Apple ARM64 macOS")
    if not TDLIB_LIBRARY.is_file():
        raise ConfigurationError("the pinned TDLib library is unavailable")
    try:
        resolved = TDLIB_LIBRARY.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError("the pinned TDLib library cannot be resolved") from error
    if EXPECTED_TDLIB_KEG not in resolved.parts:
        raise ConfigurationError("the installed TDLib keg does not match the approved pin")
    return resolved


# Trusted local policy is independent of the MCP consumer's tool allowlist.
RUNTIME_POLICY_PATH = DEFAULT_ACCOUNT_PATHS.policy
LEGACY_CAPABILITIES = ("artifacts", "read", "send")
KNOWN_CAPABILITIES = frozenset((*LEGACY_CAPABILITIES, "retained_selections", "formatted_text_send", "topic_text_send", "read_messages", "read_history", "read_reply_chain", "list_topics", "read_topic_history", "search_messages", "list_chats", "search_chats", "verified_targets", "attachment_pages", "spreadsheets", "presentations", "reply_text_send", "reply_artifact_send", "reply_media_targets", "reply_formatted_targets", "reply_lexical_targets", "reply_identity_targets", "reply_datetime_targets", "reply_url_targets", "reply_email_targets", "reply_phone_targets", "reply_bank_card_targets", "reply_text_url_targets", "forward_send", "video_send", "archive_chats", "edit_own_text"))
# Owner-facing capability profiles: every optional capability belongs to exactly one profile; the base
# (read, artifacts) is not a profile. Enabling a profile also enables the profiles it depends on, and
# disabling one also disables the profiles that depend on it.
BASE_CAPABILITIES = ("read", "artifacts")
CAPABILITY_PROFILES: dict[str, tuple[str, ...]] = {
    "readers": ("read_messages", "read_history", "read_reply_chain", "list_topics", "read_topic_history",
                "search_messages", "list_chats", "search_chats", "verified_targets", "attachment_pages",
                "spreadsheets", "presentations", "retained_selections"),
    "send": ("send", "formatted_text_send", "topic_text_send"),
    "replies": ("reply_text_send", "reply_artifact_send", "reply_media_targets", "reply_formatted_targets",
                "reply_lexical_targets", "reply_identity_targets", "reply_datetime_targets", "reply_url_targets",
                "reply_email_targets", "reply_phone_targets", "reply_bank_card_targets", "reply_text_url_targets"),
    "forward": ("forward_send",),
    "video": ("video_send",),
    "archive_edit": ("archive_chats", "edit_own_text"),
}
PROFILE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "replies": ("send",), "forward": ("send",), "video": ("send",), "archive_edit": ("send",)}


def profile_of(capability: str) -> str | None:
    return next((name for name, members in CAPABILITY_PROFILES.items() if capability in members), None)


def unlocking_profiles(missing) -> tuple[str, ...] | None:
    """The fewest profiles whose enabling (with their dependencies) adds every missing capability, in profile
    order; None when one of them belongs to no profile (a base capability)."""
    names = [profile_of(capability) for capability in missing]
    if not names or None in names:
        return None
    implied = {dependency for name in names for dependency in PROFILE_DEPENDENCIES.get(name, ())}
    return tuple(name for name in CAPABILITY_PROFILES if name in names and name not in implied)


# Retained selections live a fixed, never-sliding time from capture start.
SELECTION_TTL_DEFAULT, SELECTION_TTL_MINIMUM, SELECTION_TTL_MAXIMUM = 900, 60, 1800


@dataclass(frozen=True)
class RuntimePolicy:
    config_version: int = 1
    enabled_capabilities: tuple[str, ...] = LEGACY_CAPABILITIES
    expected_package_version: str | None = None
    expected_contract_version: int | None = None
    expected_schema_fingerprint: str | None = None
    source_path: Path | None = None
    max_draft_ttl_seconds: int = 900
    selection_ttl_seconds: int = SELECTION_TTL_DEFAULT

    def __post_init__(self) -> None:
        if type(self.max_draft_ttl_seconds) is not int or not 1 <= self.max_draft_ttl_seconds <= 86400:
            raise ValueError("draft TTL must be an integer from 1 to 86400 seconds")
        if (type(self.selection_ttl_seconds) is not int or
                not SELECTION_TTL_MINIMUM <= self.selection_ttl_seconds <= SELECTION_TTL_MAXIMUM):
            raise ValueError("selection TTL must be an integer from 60 to 1800 seconds")

    def public_settings(self) -> dict[str, object]:
        return {"config_version": self.config_version,
                "enabled_capabilities": list(self.enabled_capabilities),
                "expected_package_version": self.expected_package_version,
                "expected_contract_version": self.expected_contract_version,
                "expected_schema_fingerprint": self.expected_schema_fingerprint,
                "max_draft_ttl_seconds": self.max_draft_ttl_seconds,
                "selection_ttl_seconds": self.selection_ttl_seconds}

    def require_current(self) -> None:
        if self.source_path is not None and load_runtime_policy(self.source_path) != self:
            raise ConfigurationError("runtime configuration changed; restart matching components")


def load_runtime_policy(path: Path = RUNTIME_POLICY_PATH) -> RuntimePolicy:
    """Read at most 16 KiB from one owner-only regular file; never accept MCP paths."""
    directory_fd = None
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parent_metadata = os.fstat(directory_fd)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        if parent_metadata.st_uid != os.getuid() or stat.S_IMODE(parent_metadata.st_mode) != 0o700:
            os.close(fd)
            raise ConfigurationError("runtime configuration directory must be owner-only")
    except FileNotFoundError:
        return RuntimePolicy(source_path=path)
    except OSError:
        raise ConfigurationError("runtime configuration is unsafe or unavailable") from None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_size > 16384):
            raise ConfigurationError("runtime configuration must be a bounded owner-only file")
        data = os.read(fd, 16385)
        if len(data) != metadata.st_size:
            raise ConfigurationError("runtime configuration changed during reading")
    finally:
        os.close(fd)
    try:
        value = tomllib.loads(data.decode("utf-8"))
        allowed = {"config_version", "enabled_capabilities", "expected_package_version",
                   "expected_contract_version", "expected_schema_fingerprint", "max_draft_ttl_seconds",
                   "selection_ttl_seconds"}
        if set(value) - allowed or type(value.get("config_version")) is not int or value["config_version"] != 1:
            raise ValueError
        capabilities = value.get("enabled_capabilities")
        if (not isinstance(capabilities, list) or any(type(v) is not str or v not in KNOWN_CAPABILITIES for v in capabilities)
                or len(set(capabilities)) != len(capabilities)):
            raise ValueError
        package = value.get("expected_package_version")
        contract = value.get("expected_contract_version")
        fingerprint = value.get("expected_schema_fingerprint")
        if package is not None and (type(package) is not str or not 1 <= len(package) <= 64):
            raise ValueError
        if contract is not None and (type(contract) is not int or contract < 1):
            raise ValueError
        if fingerprint is not None and (type(fingerprint) is not str or len(fingerprint) != 64
                                       or any(c not in "0123456789abcdef" for c in fingerprint)):
            raise ValueError
        return RuntimePolicy(enabled_capabilities=tuple(sorted(capabilities)),
                             expected_package_version=package, expected_contract_version=contract,
                             expected_schema_fingerprint=fingerprint, source_path=path,
                             max_draft_ttl_seconds=value.get("max_draft_ttl_seconds", 900),
                             selection_ttl_seconds=value.get("selection_ttl_seconds", SELECTION_TTL_DEFAULT))
    except (ValueError, UnicodeError, TypeError):
        raise ConfigurationError("runtime configuration version or settings are invalid") from None
