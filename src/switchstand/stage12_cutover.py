"""Immutable operator evidence for the offline Stage 1+2 cutover."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID

from .edge_maintenance import Config, Failed, Unknown
from .work_corpus import load_manifest, manifest_exception_digest


@dataclass(frozen=True)
class Evidence:
    candidate_sha: str
    manifests: tuple[Path, Path]
    expected_corpus_digest: str
    exception_digest: str
    worksheet: Path
    worksheet_digest: str
    minimum_free_bytes: int


@dataclass(frozen=True)
class FrozenEvidence:
    candidate_sha: str
    manifests: tuple[Path, Path]
    corpus_digest: str
    exception_digest: str
    worksheet: Path
    worksheet_digest: str


@dataclass(frozen=True)
class Reconciled:
    state: Literal["ABSENT", "APPLIED", "UNKNOWN"]
    receipt_digest: str = ""
    backup_digest: str = ""


class CutoverCommands(Protocol):
    def schema_state(self, receipt: Path) -> Reconciled: ...
    def apply_schema(self, receipt: Path) -> None: ...
    def abort_schema(self, receipt: Path) -> None: ...
    def validate_stage2_pre_authority(self, evidence: FrozenEvidence) -> str: ...
    def prepare_stage1(self, evidence: FrozenEvidence) -> str: ...
    def capture_final_corpus(self, evidence: FrozenEvidence, destination: Path) -> str: ...
    def stage1_state(self, receipt: Path) -> Reconciled: ...
    def activate_stage1(self, prepared_digest: str, receipt: Path) -> None: ...
    def stage2_state(self, receipt: Path) -> Reconciled: ...
    def activate_stage2(self, receipt: Path) -> None: ...


def _digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and not set(value) - set("0123456789abcdef")


def _binding_digest(before: Path, after: Path) -> str:
    """Accept only Stage 1's expected null-to-assigned WorkId transition."""
    left, right = load_manifest(before), load_manifest(after)
    result = right.pop("sha256", None)
    try:
        rows = zip(cast(list[dict[str, object]], left["rows"]),
                   cast(list[dict[str, object]], right["rows"]), strict=True)
        added = 0
        for old, new in rows:
            old_id, new_id = old["work_id"], new["work_id"]
            if old_id is None:
                if new_id is None or str(UUID(str(new_id))) != new_id:
                    raise ValueError
                added += 1
            elif new_id != old_id:
                raise ValueError
            new["work_id"] = old_id
        left_counts, right_counts = cast(dict[str, int], left["counts"]), cast(dict[str, int], right["counts"])
        if right_counts["bound"] != left_counts["bound"] + added:
            raise ValueError
        right_counts["bound"] = left_counts["bound"]
    except (KeyError, TypeError, ValueError) as error:
        raise Failed("final corpus is missing an exact Stage 1 binding") from error
    left.pop("sha256", None)
    if left != right or not _digest(result):
        raise Failed("final corpus changed outside Stage 1 bindings")
    return cast(str, result)


def _read_private(path: Path, label: str) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise Failed(f"{label} is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o777 != 0o600:
            raise Failed(f"{label} is not an exact mode-0600 regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _publish_or_match(path: Path, data: bytes, label: str) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if _read_private(path, label) != data:
            raise Failed(f"existing {label} does not match this attempt") from None
        return
    except OSError as error:
        raise Failed(f"cannot freeze {label}") from error
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def freeze_evidence(evidence: Evidence, attempt_dir: Path) -> FrozenEvidence:
    """Validate and immutably copy exact pre-effect evidence into an attempt."""
    if len(evidence.candidate_sha) != 40 or any(
        character not in "0123456789abcdef" for character in evidence.candidate_sha
    ):
        raise Failed("candidate SHA is invalid")
    if evidence.minimum_free_bytes < 1:
        raise Failed("minimum free-space requirement is invalid")
    try:
        attempt = attempt_dir.lstat()
    except OSError as error:
        raise Failed("attempt directory is unavailable") from error
    if (
        attempt_dir.is_symlink()
        or not attempt_dir.is_dir()
        or attempt.st_uid != os.getuid()
        or attempt.st_mode & 0o777 != 0o700
    ):
        raise Failed("attempt directory identity or mode is invalid")
    if shutil.disk_usage(attempt_dir).free < evidence.minimum_free_bytes:
        raise Failed("insufficient free space for the offline cutover")

    sources = (
        (evidence.manifests[0], attempt_dir / "corpus-a.json", "first corpus manifest"),
        (evidence.manifests[1], attempt_dir / "corpus-b.json", "second corpus manifest"),
        (evidence.worksheet, attempt_dir / "stage2-worksheet.json", "Stage 2 worksheet"),
    )
    for source, destination, label in sources:
        _publish_or_match(destination, _read_private(source, label), label)
    directory = os.open(attempt_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)

    frozen_manifests = cast(tuple[Path, Path], tuple(item[1] for item in sources[:2]))
    try:
        first, second = (load_manifest(path) for path in frozen_manifests)
        if first != second:
            raise Failed("the two reviewed corpus manifests do not match")
        if first.get("source_candidate") != evidence.candidate_sha:
            raise Failed("corpus manifests do not bind the exact candidate")
        if first.get("sha256") != evidence.expected_corpus_digest:
            raise Failed("corpus manifest does not match the expected full digest")
        if manifest_exception_digest(first) != evidence.exception_digest:
            raise Failed("corpus exceptions do not match the reviewed digest")
    except (OSError, TypeError, ValueError) as error:
        raise Failed("corpus evidence is invalid") from error
    frozen_worksheet = sources[2][1]
    worksheet_digest = hashlib.sha256(
        _read_private(frozen_worksheet, "frozen Stage 2 worksheet")
    ).hexdigest()
    if worksheet_digest != evidence.worksheet_digest:
        raise Failed("Stage 2 worksheet digest does not match")
    return FrozenEvidence(
        evidence.candidate_sha,
        frozen_manifests,
        evidence.expected_corpus_digest,
        evidence.exception_digest,
        frozen_worksheet,
        evidence.worksheet_digest,
    )


class Stage12Cutover:
    """Resumable C1 offline step; every effect is followed by fresh reconciliation."""

    def __init__(self, attempt_dir: Path, evidence: Evidence, commands: CutoverCommands):
        self.receipt_path = attempt_dir / "stage12-cutover.json"
        self._attempt_dir, self._evidence, self._commands = attempt_dir, evidence, commands
        self._schema = attempt_dir / "schema-upgrade.json"
        self._stage1 = attempt_dir / "stage1-activation.json"
        self._stage2 = attempt_dir / "stage2-activation.json"
        self._final = attempt_dir / "corpus-final.json"
        self.database_backup = "PENDING"
        self.corpus_manifests: tuple[str, str] = (
            evidence.expected_corpus_digest,
            evidence.expected_corpus_digest,
        )
        self.worksheet = evidence.worksheet_digest

    def _load(self, frozen: FrozenEvidence) -> dict[str, object] | None:
        if not self.receipt_path.exists():
            return None
        try:
            raw = json.loads(_read_private(self.receipt_path, "cutover receipt"))
        except (Failed, TypeError, ValueError) as error:
            raise Unknown("cutover receipt is invalid") from error
        if not isinstance(raw, dict):
            raise Unknown("cutover receipt is invalid")
        value = cast(dict[str, object], raw)
        if (
            value.get("schema_version") != 1
            or value.get("candidate_sha") != frozen.candidate_sha
            or value.get("corpus_manifests") != [frozen.corpus_digest] * 2
            or value.get("worksheet") != frozen.worksheet_digest
            or value.get("exception_digest") != frozen.exception_digest
            or value.get("terminal_boundary")
            not in {"PRE_MARKER", "POSTGRES_AUTHORITY", "COMPLETE"}
        ):
            raise Unknown("cutover receipt is malformed or does not bind exact evidence")
        boundary = cast(str, value["terminal_boundary"])
        required = ["database_backup", "schema_receipt"]
        if boundary != "PRE_MARKER":
            required += ["stage1_receipt", "stage2_validation", "prepared_import"]
        if boundary == "COMPLETE":
            required.append("stage2_receipt")
        if not all(_digest(value.get(name)) for name in required) or (
            boundary != "PRE_MARKER" and not _digest(value.get("final_manifest"))
        ):
            raise Unknown("cutover receipt proof is incomplete or malformed")
        preproof = ("stage2_validation", "prepared_import", "final_manifest")
        if (
            boundary == "PRE_MARKER"
            and any(name in value for name in preproof)
            and (
                not all(_digest(value.get(name)) for name in preproof[:2])
                or not _digest(value.get("final_manifest"))
            )
        ):
            raise Unknown("cutover receipt pre-authority proof is malformed")
        if "final_manifest" in value:
            try:
                if load_manifest(self._final)["sha256"] != value["final_manifest"]:
                    raise ValueError
            except (KeyError, OSError, TypeError, ValueError) as error:
                raise Unknown("final corpus proof does not match the cutover receipt") from error
        return value

    def _write(self, frozen: FrozenEvidence, boundary: str, **proof: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "candidate_sha": frozen.candidate_sha,
            "database_backup": self.database_backup,
            "corpus_manifests": [frozen.corpus_digest] * 2,
            "worksheet": frozen.worksheet_digest,
            "exception_digest": frozen.exception_digest,
            "terminal_boundary": boundary,
            **proof,
        }
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode()
        descriptor, name = tempfile.mkstemp(
            prefix=f".{self.receipt_path.name}.", dir=self._attempt_dir
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        os.replace(temporary, self.receipt_path)
        directory = os.open(self._attempt_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return payload

    @staticmethod
    def _applied(result: Reconciled, label: str) -> Reconciled:
        if result.state != "APPLIED" or not _digest(result.receipt_digest):
            raise Unknown(f"{label} is not exactly reconciled")
        return result

    def run(self, advance: Callable[[str], None]) -> None:
        frozen = freeze_evidence(self._evidence, self._attempt_dir)
        top = self._load(frozen)
        schema = self._commands.schema_state(self._schema)
        if schema.state == "ABSENT" and top is None:
            self._commands.apply_schema(self._schema)
            schema = self._commands.schema_state(self._schema)
        schema = self._applied(schema, "schema upgrade")
        self.database_backup = schema.backup_digest
        if not _digest(self.database_backup):
            raise Unknown("database backup is not exactly reconciled")
        if top is None:
            top = self._write(frozen, "PRE_MARKER", schema_receipt=schema.receipt_digest)
        elif top.get("schema_receipt") != schema.receipt_digest or (
            top.get("database_backup") != schema.backup_digest
        ):
            raise Unknown("schema receipt changed")
        advance("PRE_MARKER")

        boundary = cast(str, top["terminal_boundary"])
        if boundary == "PRE_MARKER":
            prepared = top.get("prepared_import")
            validation = top.get("stage2_validation")
            stage1 = self._commands.stage1_state(self._stage1)
            names = ("prepared_import", "stage2_validation", "final_manifest")
            has_preproof = any(name in top for name in names)
            valid_preproof = (
                _digest(prepared)
                and _digest(validation)
                and _digest(top.get("final_manifest"))
            )
            if stage1.state == "APPLIED" and not valid_preproof:
                raise Unknown("Stage 1 applied without durable pre-authority proof")
            if stage1.state == "ABSENT" and not self._stage1.exists():
                if not valid_preproof:
                    if has_preproof:
                        raise Unknown("durable pre-authority proof is incomplete")
                    validation = self._commands.validate_stage2_pre_authority(frozen)
                    prepared = self._commands.prepare_stage1(frozen)
                    if not _digest(validation) or not _digest(prepared):
                        raise Failed("Stage 2 validation or Stage 1 prepare proof is invalid")
                    final = self._commands.capture_final_corpus(frozen, self._final)
                    if not _digest(final):
                        raise Failed("final corpus binding proof is invalid")
                    top = self._write(
                        frozen,
                        "PRE_MARKER",
                        schema_receipt=schema.receipt_digest,
                        stage2_validation=validation,
                        prepared_import=prepared,
                        final_manifest=final,
                    )
                self._commands.activate_stage1(cast(str, prepared), self._stage1)
                stage1 = self._commands.stage1_state(self._stage1)
            stage1 = self._applied(stage1, "Stage 1 authority")
            top = self._write(
                frozen,
                "POSTGRES_AUTHORITY",
                schema_receipt=schema.receipt_digest,
                stage1_receipt=stage1.receipt_digest,
                stage2_validation=cast(str, validation),
                prepared_import=cast(str, prepared),
                final_manifest=cast(str, top["final_manifest"]),
            )
        else:
            stage1 = self._applied(self._commands.stage1_state(self._stage1), "Stage 1 authority")
            if top.get("stage1_receipt") != stage1.receipt_digest:
                raise Unknown("Stage 1 receipt changed")
        advance("POSTGRES_AUTHORITY")

        if top["terminal_boundary"] == "POSTGRES_AUTHORITY":
            stage2 = self._commands.stage2_state(self._stage2)
            if stage2.state == "ABSENT" and not self._stage2.exists():
                self._commands.activate_stage2(self._stage2)
                stage2 = self._commands.stage2_state(self._stage2)
            stage2 = self._applied(stage2, "Stage 2 authority")
            top = self._write(
                frozen,
                "COMPLETE",
                schema_receipt=schema.receipt_digest,
                stage1_receipt=cast(str, top["stage1_receipt"]),
                stage2_receipt=stage2.receipt_digest,
                stage2_validation=cast(str, top["stage2_validation"]),
                prepared_import=cast(str, top["prepared_import"]),
                final_manifest=cast(str, top["final_manifest"]),
            )
        else:
            stage2 = self._applied(self._commands.stage2_state(self._stage2), "Stage 2 authority")
            if top.get("stage2_receipt") != stage2.receipt_digest:
                raise Unknown("Stage 2 receipt changed")
        advance("COMPLETE")

    def abort_pre_authority(self) -> None:
        frozen = freeze_evidence(self._evidence, self._attempt_dir)
        top = self._load(frozen)
        if top is not None and top["terminal_boundary"] != "PRE_MARKER":
            raise Unknown("authority boundary crossed; abort is forbidden")
        if self._commands.stage1_state(self._stage1).state != "ABSENT":
            raise Unknown("Stage 1 absence is not proven; abort is forbidden")
        schema = self._commands.schema_state(self._schema)
        if schema.state == "APPLIED":
            self._commands.abort_schema(self._schema)
        elif schema.state != "ABSENT":
            raise Unknown("schema state is not proven; abort is forbidden")
        if self._commands.schema_state(self._schema).state != "ABSENT":
            raise Unknown("pre-authority schema abort was not proven")


class ConcreteCommands:
    """Target-bound adapters for the reviewed schema, Stage 1, and Stage 2 CLIs."""

    def __init__(self, config: Config, evidence: Evidence):
        self.c, self.source = config, evidence
        self.target = "production" if config.target == "production" else f"disposable:{cast(Path, config.target_root).name}"
        self._environment_cache: dict[str, str] | None = None
        self._container = ""
        self._python = Path()

    @staticmethod
    def _run(command: list[str], environment: dict[str, str] | None = None, *,
             check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command, cwd=cwd, env=environment, text=True, capture_output=True, check=check,
            timeout=300,
        )

    def _environment(self) -> dict[str, str]:
        raw = _read_private(self.c.env_file, "migration environment").decode()
        supplied = {
            key.strip(): value.strip().strip("'\"")
            for line in raw.splitlines()
            if line.strip() and not line.lstrip().startswith("#") and "=" in line
            for key, value in [line.split("=", 1)]
        }
        if not supplied.get("ASANA_TOKEN"):
            raise Failed("migration environment lacks ASANA_TOKEN")
        project = "switchstand" if self.target == "production" else f"switchstand-rehearsal-{self.target.split(':', 1)[1]}"
        compose = [
            "docker", "compose", "-p", project, "--project-directory",
            str(self.c.candidate_runtime), "-f", str(self.c.candidate_runtime / "compose.state.yaml"),
        ]
        try:
            container = self._run([*compose, "ps", "-q", "postgres"]).stdout.strip()
            item = json.loads(self._run(["docker", "inspect", container]).stdout)[0]
            labels, network = item["Config"]["Labels"], f"{project}_default"
            address = item["NetworkSettings"]["Networks"][network]["IPAddress"]
            if (
                labels.get("com.docker.compose.project") != project
                or labels.get("com.docker.compose.service") != "postgres"
                or not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", address)
            ):
                raise ValueError
            self._container = container
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise Failed("migration database target is not exact") from error
        values = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": pwd.getpwuid(os.getuid()).pw_dir,
            "ASANA_TOKEN": supplied["ASANA_TOKEN"],
            "DATABASE_URL": f"postgresql+psycopg://switchstand:switchstand@{address}/switchstand",
        }
        optional = (
            "SWITCHSTAND_BACKUP_DIR" if self.target == "production"
            else "SWITCHSTAND_TEST_PROJECT_GID"
        )
        if supplied.get(optional):
            values[optional] = supplied[optional]
        identity = self._run([
            "git", "-C", str(self.c.candidate_runtime), "rev-parse", "HEAD",
            "--path-format=absolute", "--git-common-dir",
        ]).stdout.splitlines()
        if len(identity) != 2 or identity[0] != self.c.candidate_sha:
            raise Failed("migration module runtime does not match the exact candidate")
        common = Path(identity[1])
        self._python = common.parent / ".venv/bin/python"
        values |= {
            "PYTHONPATH": str(self.c.candidate_runtime / "src"),
            "SWITCHSTAND_CONTROL_PATH": str(self.c.candidate_runtime),
            "SWITCHSTAND_CONTROL_SHA": self.c.candidate_sha,
            "SWITCHSTAND_CONTROL_COMMON": str(common),
        }
        return values

    def _env(self) -> dict[str, str]:
        if self._environment_cache is None:
            self._environment_cache = self._environment()
        return self._environment_cache

    def _module(
        self, module: str, *arguments: object, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self._run(
            [str(self._python), "-m", module, *(str(value) for value in arguments)],
            self._env(),
            check=check,
            cwd=self.c.candidate_runtime,
        )

    def _schema(self, receipt: Path, operation: str = "apply") -> None:
        result = self._run(
            [
                str(self.c.candidate_runtime / "scripts/switchstand-upgrade-state"),
                "--target",
                self.target,
                operation,
                str(receipt),
            ],
            self._env(),
            check=False,
        )
        if result.returncode:
            raise Unknown(result.stderr.strip() or "schema operation failed")

    def _revision(self) -> str:
        try:
            self._env()
            revision = self._run([
                "docker", "exec", self._container, "psql", "-X", "-v", "ON_ERROR_STOP=1",
                "-U", "switchstand", "-d", "switchstand", "-At", "-c",
                "SELECT version_num FROM alembic_version",
            ]).stdout.strip()
        except (OSError, subprocess.SubprocessError) as error:
            raise Unknown("schema revision is unreadable") from error
        if revision not in {"0007_agent_chat_identity", "0010_work_metadata_authority"}:
            raise Unknown("schema revision is unsupported")
        return revision

    @staticmethod
    def _receipt(path: Path) -> str:
        return hashlib.sha256(_read_private(path, "subordinate receipt")).hexdigest()

    def _frozen(self) -> FrozenEvidence:
        root = self.c.attempt_dir
        return FrozenEvidence(
            self.source.candidate_sha,
            (root / "corpus-a.json", root / "corpus-b.json"),
            self.source.expected_corpus_digest,
            self.source.exception_digest,
            root / "stage2-worksheet.json",
            self.source.worksheet_digest,
        )

    def schema_state(self, receipt: Path) -> Reconciled:
        revision = self._revision()
        if revision == "0007_agent_chat_identity":
            return Reconciled("ABSENT")
        if not receipt.exists():
            return Reconciled("UNKNOWN")
        self._schema(receipt)
        try:
            records = [
                json.loads(line)
                for line in _read_private(receipt, "schema receipt").splitlines()
            ]
            terminal = records[-1]
            backup = _read_private(Path(cast(str, terminal["backup"])), "schema backup")
        except (AttributeError, IndexError, KeyError, TypeError, ValueError, UnicodeError, Failed):
            return Reconciled("UNKNOWN")
        if terminal.get("outcome") != "APPLIED":
            return Reconciled("UNKNOWN")
        return Reconciled("APPLIED", self._receipt(receipt), hashlib.sha256(backup).hexdigest())

    def apply_schema(self, receipt: Path) -> None:
        self._schema(receipt)

    def abort_schema(self, receipt: Path) -> None:
        self._schema(receipt, "abort-pre-authority")

    def validate_stage2_pre_authority(self, evidence: FrozenEvidence) -> str:
        result = self._module(
            "switchstand.work_metadata_migration",
            "validate",
            evidence.worksheet,
            "--confirm-offline",
            check=False,
        )
        if result.returncode:
            raise Failed(result.stderr.strip() or "Stage 2 validation failed")
        return hashlib.sha256((evidence.worksheet_digest + result.stdout).encode()).hexdigest()

    def prepare_stage1(self, evidence: FrozenEvidence) -> str:
        result = self._module(
            "switchstand.work_index_migration",
            "prepare",
            "--confirm-offline",
            "--manifest",
            evidence.manifests[0],
            "--manifest",
            evidence.manifests[1],
            "--expected-corpus-digest",
            evidence.corpus_digest,
            "--expected-exception-digest",
            evidence.exception_digest,
        )
        match = re.fullmatch(r"prepared_import_sha256=([0-9a-f]{64})\n?", result.stdout)
        if match is None:
            raise Failed("Stage 1 prepare output is invalid")
        return match.group(1)

    def capture_final_corpus(self, evidence: FrozenEvidence, destination: Path) -> str:
        self._module(
            "switchstand.work_corpus",
            "capture",
            destination,
            "--source-candidate",
            evidence.candidate_sha,
        )
        return _binding_digest(evidence.manifests[0], destination)

    def _activation_state(self, module: str, receipt: Path, worksheet: bool = False) -> Reconciled:
        if not receipt.exists():
            return Reconciled("ABSENT")
        arguments: list[object] = ["reconcile"]
        if worksheet:
            arguments.append(self._frozen().worksheet)
        arguments += ["--confirm-offline", "--receipt", receipt]
        result = self._module(module, *arguments, check=False)
        if result.returncode == 3:
            return Reconciled("ABSENT")
        if result.returncode:
            return Reconciled("UNKNOWN")
        return Reconciled("APPLIED", self._receipt(receipt))

    def stage1_state(self, receipt: Path) -> Reconciled:
        return self._activation_state("switchstand.work_index_migration", receipt)

    def activate_stage1(self, prepared_digest: str, receipt: Path) -> None:
        evidence = self._frozen()
        result = self._module(
            "switchstand.work_index_migration",
            "activate",
            "--confirm-offline",
            "--manifest",
            evidence.manifests[0],
            "--manifest",
            evidence.manifests[1],
            "--expected-corpus-digest",
            evidence.corpus_digest,
            "--expected-exception-digest",
            evidence.exception_digest,
            "--expected-prepared-digest",
            prepared_digest,
            "--receipt",
            receipt,
            check=False,
        )
        if result.returncode:
            error = result.stderr.strip() or "Stage 1 activation failed"
            if result.returncode == 1:
                raise Failed(error)
            raise Unknown(error)
    def stage2_state(self, receipt: Path) -> Reconciled:
        return self._activation_state("switchstand.work_metadata_migration", receipt, True)

    def activate_stage2(self, receipt: Path) -> None:
        result = self._module(
            "switchstand.work_metadata_migration",
            "activate",
            self._frozen().worksheet,
            "--confirm-offline",
            "--receipt",
            receipt,
            check=False,
        )
        if result.returncode:
            error = result.stderr.strip() or "Stage 2 activation failed"
            if result.returncode == 1:
                raise Failed(error)
            raise Unknown(error)
