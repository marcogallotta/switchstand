import base64
import os
from uuid import UUID

import bcrypt
import httpx
import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import TestClient

from switchstand.canonical_work import (
    CanonicalWorkRepository,
    canonical_metadata,
    canonical_revision,
    canonical_work,
)
from switchstand.human_review_shell import (
    create_human_review_shell,
    create_persistent_human_review_shell,
)
from switchstand.human_reviews import (
    HumanDecision,
    HumanReviewConsequence,
    HumanReviewRecord,
    HumanReviewResult,
    HumanReviewState,
    human_review_consequences,
)

WORK = UUID("60000000-0000-4000-8000-000000000001")
REVISION = "pg_exact"
ORIGIN = "https://review.example.test"
USERNAME = "marco"
PASSWORD = "correct horse"


def _record() -> HumanReviewRecord:
    consequence = HumanReviewConsequence(
        package_work_id=WORK,
        package_revision=REVISION,
        implementation_scope=("land <reviewed> code",),
        implementation_target="repository main",
        excluded_effects=("deployment", "activation"),
    )
    return HumanReviewRecord(
        consequence_id=consequence.consequence_id,
        package_work_id=WORK,
        package_revision=REVISION,
        consequence_digest=consequence.digest,
        consequence=consequence,
        decision=None,
        state="PENDING",
    )


class FakeState:
    def __init__(self) -> None:
        self.record = _record()
        self.submissions: list[tuple[UUID, UUID, str, HumanDecision]] = []

    async def prepare(self, package_work_id: UUID, package_revision: str) -> HumanReviewResult:
        if package_work_id != WORK or package_revision != REVISION:
            return HumanReviewResult(status="STALE", reason="package_revision_changed")
        return HumanReviewResult(status="PREPARED", record=self.record)

    async def submit(
        self,
        consequence_id: UUID,
        package_work_id: UUID,
        package_revision: str,
        decision: HumanDecision,
    ) -> HumanReviewResult:
        self.submissions.append((consequence_id, package_work_id, package_revision, decision))
        state = "READY_FOR_IMPLEMENTATION" if decision == "APPROVED" else decision
        decided = self.record.model_copy(update={"decision": decision, "state": state})
        return HumanReviewResult(status="RECORDED", record=decided)


@pytest.fixture
def shell() -> tuple[TestClient, FakeState, dict[str, str]]:
    state = FakeState()
    password_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
    app = create_human_review_shell(
        state,
        expected_origin=ORIGIN,
        username=USERNAME,
        password_hash=password_hash,
    )
    token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    return TestClient(app), state, {"Authorization": f"Basic {token}"}


def _form(record: HumanReviewRecord, decision: str = "APPROVED") -> dict[str, str]:
    return {
        "consequence_id": str(record.consequence_id),
        "package_work_id": str(record.package_work_id),
        "package_revision": record.package_revision,
        "consequence_digest": record.consequence_digest,
        "decision": decision,
    }


def test_shell_is_basic_authenticated_and_renders_server_owned_consequence(shell):
    client, _, auth = shell
    path = f"/human-review/{WORK}?revision={REVISION}"

    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer irrelevant"}).status_code == 401
    response = client.get(path, headers=auth)

    assert response.status_code == 200
    assert "land &lt;reviewed&gt; code" in response.text
    assert "READY_FOR_IMPLEMENTATION_ONLY" in response.text
    assert response.text.count('<button name="decision"') == 4
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"


def test_exact_origin_and_bound_fields_fail_closed_before_decision(shell):
    client, state, auth = shell
    form = _form(state.record)

    assert client.post("/human-review/submit", headers=auth, data=form).status_code == 403
    wrong_origin = {**auth, "Origin": "https://lookalike.example.test"}
    assert client.post("/human-review/submit", headers=wrong_origin, data=form).status_code == 403
    exact = {**auth, "Origin": ORIGIN}
    changed = {**form, "consequence_digest": "0" * 64}
    response = client.post("/human-review/submit", headers=exact, data=changed)

    assert response.status_code == 409
    assert "STALE" in response.text
    assert state.submissions == []


@pytest.mark.parametrize(
    ("decision", "expected_state"),
    [
        ("APPROVED", "READY_FOR_IMPLEMENTATION"),
        ("WAIT", "WAIT"),
        ("HOLD", "HOLD"),
        ("NO_DISPATCH", "NO_DISPATCH"),
    ],
)
def test_exact_decision_uses_the_single_state_writer(shell, decision, expected_state):
    client, state, auth = shell
    response = client.post(
        "/human-review/submit",
        headers={**auth, "Origin": ORIGIN},
        data=_form(state.record, decision),
    )

    assert response.status_code == 200
    assert expected_state in response.text
    assert state.submissions == [
        (state.record.consequence_id, WORK, REVISION, decision)
    ]


def test_malformed_or_oversized_submission_never_calls_writer(shell):
    client, state, auth = shell
    headers = {
        **auth,
        "Origin": ORIGIN,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    malformed = client.post("/human-review/submit", headers=headers, content=b"decision=APPROVED")
    oversized = client.post(
        "/human-review/submit",
        headers=headers,
        content=b"package_revision=" + b"x" * 9000,
    )

    assert malformed.status_code == 400
    assert oversized.status_code == 413
    assert state.submissions == []


def test_oversized_basic_password_fails_closed(shell):
    client, _, _ = shell
    token = base64.b64encode(f"{USERNAME}:{'x' * 73}".encode()).decode()
    response = client.get(
        f"/human-review/{WORK}?revision={REVISION}",
        headers={"Authorization": f"Basic {token}"},
    )
    assert response.status_code == 401


def test_unicode_basic_username_fails_closed(shell):
    client, _, _ = shell
    token = base64.b64encode(f"márco:{PASSWORD}".encode()).decode()
    response = client.get(
        f"/human-review/{WORK}?revision={REVISION}",
        headers={"Authorization": f"Basic {token}"},
    )
    assert response.status_code == 401


async def test_chunked_oversized_form_stops_before_reading_more_or_writing(shell):
    client, state, auth = shell

    async def chunks():
        yield b"x" * 5000
        yield b"x" * 5000
        raise AssertionError("the shell read beyond the first oversized prefix")

    transport = httpx.ASGITransport(app=client.app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as browser:
        response = await browser.post(
            "/human-review/submit",
            headers={**auth, "Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
            content=chunks(),
        )
    assert response.status_code == 413
    assert state.submissions == []


@pytest.mark.parametrize(
    "origin",
    ["http://review.example.test", "https://review.example.test/", "https://review.example.test/x"],
)
def test_configuration_requires_an_exact_https_origin(origin):
    state = FakeState()
    password_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
    with pytest.raises(ValueError, match="exact HTTPS origin"):
        create_human_review_shell(
            state, expected_origin=origin, username=USERNAME, password_hash=password_hash
        )


def test_configuration_rejects_a_malformed_bcrypt_hash():
    with pytest.raises(ValueError, match="valid bcrypt hash"):
        create_human_review_shell(
            FakeState(), expected_origin=ORIGIN, username=USERNAME, password_hash="$2b$broken"
        )


async def test_persistent_shell_factory_records_decision_in_real_state(database_prerequisite):
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for persistent Human Review shell test")
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(
                "DROP TABLE IF EXISTS human_review_consequences, canonical_work CASCADE"
            ))
            await connection.run_sync(lambda sync: canonical_metadata.create_all(
                sync, tables=[canonical_work, human_review_consequences]
            ))
            await connection.execute(insert(canonical_work).values(
                work_id=WORK, title="Reviewed package", normalized_title="reviewed package",
                completed=False, notes="exact package", row_version=1,
            ))
        revision = canonical_revision(WORK, 1)
        consequence = HumanReviewConsequence(
            package_work_id=WORK,
            package_revision=revision,
            implementation_scope=("land the exact reviewed package",),
            implementation_target="repository main",
            excluded_effects=("deployment", "activation"),
        )
        state = HumanReviewState(engine, CanonicalWorkRepository(engine))
        proposed = await state.propose(consequence)
        assert proposed.status == "PREPARED" and proposed.record is not None
        password_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
        app = create_persistent_human_review_shell(
            engine, expected_origin=ORIGIN, username=USERNAME, password_hash=password_hash,
        )
        token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        headers = {"Authorization": f"Basic {token}"}
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as browser:
            shown = await browser.get(
                f"/human-review/{WORK}?revision={revision}", headers=headers,
            )
            decided = await browser.post(
                "/human-review/submit",
                headers={**headers, "Origin": ORIGIN},
                data=_form(proposed.record),
            )

        assert shown.status_code == 200
        assert decided.status_code == 200
        async with engine.connect() as connection:
            row = (await connection.execute(
                human_review_consequences.select()
            )).mappings().one()
        assert (row["decision"], row["state"]) == (
            "APPROVED", "READY_FOR_IMPLEMENTATION",
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(
                "DROP TABLE IF EXISTS human_review_consequences, canonical_work CASCADE"
            ))
        await engine.dispose()
