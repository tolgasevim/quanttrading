from datetime import date, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from quant import rls
from quant.ai import commentary
from quant.ai.client import LlmUnavailable, ToolCall
from quant.api.ai import optional_llm
from quant.main import app
from quant.models import (
    AiCommentary,
    AiPick,
    JobRun,
    LlmUsage,
    Notification,
    PriceEOD,
    User,
)

from .conftest import login, make_user
from .test_ai import Fake, accept, hold, pick_call, reply
from .test_ai_tools import A, B, priced

TODAY = date.today()
MONDAY = commentary.week_start(TODAY)


@pytest.fixture
def portfolio(db: Session, admin: User) -> User:
    hold(db, admin, A, "NVIDIA Corp")
    hold(db, admin, B, "SAP SE")
    priced(db, A, "NVIDIA", "150", "Technology", {8: "100"})  # +50 % over the week
    priced(db, B, "SAP", "45", "Software", {8: "50"})  # -10 %
    return admin


def make(db: Session, user: User, client: Any = None) -> AiCommentary:
    rls.scope_to_user(db, user.id)
    made = commentary.create_commentary(db, user.id, TODAY, client)
    assert made is not None
    return made


def test_weeks_start_on_monday() -> None:
    assert commentary.week_start(date(2026, 10, 7)) == date(2026, 10, 5)
    assert commentary.week_start(date(2026, 10, 11)) == date(2026, 10, 5)  # Sunday
    assert commentary.week_start(date(2026, 10, 5)) == date(2026, 10, 5)


def test_weekly_moves_compare_the_newest_close_with_one_a_week_older(
    db: Session, portfolio: User
) -> None:
    rls.bypass(db)
    moves = commentary.weekly_moves(db, [A, B, "XX0000000000"], TODAY)
    assert moves == {A: D("50.0"), B: D("-10.0")}


def test_an_old_close_gives_no_weekly_move(db: Session, admin: User) -> None:
    rls.bypass(db)
    from quant.models import Instrument

    row = Instrument(code="OLD", isin=A, name="Old", asset_class="stock", currency="EUR")
    db.add(row)
    db.flush()
    for age in (30, 40):
        db.add(PriceEOD(instrument_id=row.id, date=TODAY - timedelta(days=age), close=D("5"), currency="EUR", source="t"))  # fmt: skip
    db.commit()
    assert commentary.weekly_moves(db, [A], TODAY) == {}


def test_without_consent_the_commentary_is_a_template_and_no_model_is_called(
    db: Session, portfolio: User
) -> None:
    fake = Fake()
    made = make(db, portfolio, fake)
    assert made.kind == "template" and fake.requests == []
    assert "not switched on" in (made.why_template or "")
    assert "Weekly summary for the week of" in made.text and "not written by the AI" in made.text
    assert "NVIDIA Corp +50.0 %" in made.text and "SAP SE -10.0 %" in made.text
    assert "Largest weights" in made.text and "2 positions" in made.text


def test_with_consent_the_ai_writes_it_and_the_label_and_ledger_follow(
    db: Session, owner: TestClient, portfolio: User
) -> None:
    accept(owner)
    fake = Fake(
        reply("", [pick_call("t1", isin=A, name="NVIDIA Corp", direction="hold")]),
        reply("A strong week for NVIDIA. Main risk: concentration. " + "x" * 80),
    )
    made = make(db, portfolio, fake)
    assert made.kind == "ai" and made.why_template is None and made.model
    assert made.text.startswith("A strong week for NVIDIA")
    assert "AI-generated" in made.text  # the label (FR-53)
    sent = fake.requests[0]["messages"][-1]["content"]
    assert "Facts the app computed" in sent and "NVIDIA Corp +50.0 %" in sent
    rls.bypass(db)
    assert {u.purpose for u in db.scalars(select(LlmUsage))} == {"weekly"}  # the ledger (FR-56)
    pick = db.scalars(select(AiPick)).one()  # a recommendation in it is logged (FR-52)
    assert pick.direction == "hold"


@pytest.mark.parametrize(
    ("setup", "why"),
    [
        ("refuse", "declined"),
        ("empty", "no text"),
        ("down", "not available"),
        ("budget", "budget"),
        ("nokey", "not available"),
    ],
)
def test_every_ai_problem_ends_in_a_template(
    db: Session,
    owner: TestClient,
    portfolio: User,
    monkeypatch: pytest.MonkeyPatch,
    setup: str,
    why: str,
) -> None:
    from quant.config import get_settings

    accept(owner)
    client: Any
    if setup == "refuse":
        client = Fake(reply("", refused=True))
    elif setup == "empty":
        client = Fake(reply(""))
    elif setup == "down":

        class Down:
            def send(self, **kw: Any) -> Any:
                raise LlmUnavailable("down")

        client = Down()
    elif setup == "budget":
        monkeypatch.setattr(get_settings(), "llm_user_monthly_cap_eur", D("0"))
        client = Fake()
    else:
        client = None
        monkeypatch.setattr(get_settings(), "anthropic_api_key", None)
    made = make(db, portfolio, client)
    assert made.kind == "template" and why in (made.why_template or "")
    assert "Weekly summary" in made.text


def test_a_week_has_one_commentary_and_one_notification(db: Session, portfolio: User) -> None:
    first = make(db, portfolio, Fake())
    again = make(db, portfolio, Fake())
    assert again.id == first.id
    rls.bypass(db)
    assert len(db.scalars(select(AiCommentary)).all()) == 1
    rows = db.scalars(select(Notification).where(Notification.kind == "weekly_commentary")).all()
    assert len(rows) == 1 and rows[0].dedupe_key == f"weekly:{MONDAY.isoformat()}"


def test_a_user_without_holdings_gets_none(db: Session, admin: User) -> None:
    rls.scope_to_user(db, admin.id)
    assert commentary.create_commentary(db, admin.id, TODAY, None) is None


def test_the_template_counts_alerts_picks_and_scores(db: Session, portfolio: User) -> None:
    rls.bypass(db)
    db.add(Notification(user_id=portfolio.id, kind="daily_move", severity="info", title="t", body="b", dedupe_key="k1"))  # fmt: skip
    db.add(AiPick(user_id=portfolio.id, question="q", name="n", direction="buy", horizon_months=1, rationale="r", held=False))  # fmt: skip
    db.commit()
    made = make(db, portfolio, None)
    assert "Alerts this week: 1. AI picks made this week: 1; new pick scores: 0." in made.text


def test_the_weekly_job_covers_every_user_and_isolates_failures(
    db: Session, portfolio: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = make_user(db, "other@example.com")
    hold(db, other, A, "NVIDIA Corp")
    broken = make_user(db, "broken@example.com")
    hold(db, broken, A, "NVIDIA Corp")
    real = commentary.create_commentary

    def flaky(session: Session, user_id: Any, today: date, client: Any = None) -> Any:
        if user_id == broken.id:
            raise RuntimeError("boom")
        return real(session, user_id, today, client)

    monkeypatch.setattr(commentary, "create_commentary", flaky)
    result = commentary.run_weekly(TODAY, Fake())
    assert result.rows_written == 2 and result.errors == {str(broken.id): "RuntimeError"}
    rls.bypass(db)
    assert {c.user_id for c in db.scalars(select(AiCommentary))} == {portfolio.id, other.id}
    assert commentary.run_weekly(TODAY, Fake()).rows_written == 0  # a second run changes nothing


def test_the_weekly_job_records_a_run(db: Session, portfolio: User) -> None:
    from quant import worker

    worker.run_weekly_commentary()
    rls.bypass(db)
    run = db.scalars(select(JobRun).where(JobRun.job == "weekly_commentary")).one()
    assert run.status.value in ("success", "partial")
    assert db.scalars(select(AiCommentary)).one().kind == "template"  # no key in the test setup


def test_the_commentary_api(client: TestClient, db: Session, portfolio: User) -> None:
    login(client, portfolio.email)
    assert client.get("/api/ai/commentaries").json() == []
    app.dependency_overrides[optional_llm] = lambda: None
    try:
        body = client.post("/api/ai/commentaries/now").json()
        again = client.post("/api/ai/commentaries/now").json()
    finally:
        app.dependency_overrides.pop(optional_llm, None)
    assert body["kind"] == "template" and body["week_start"] == MONDAY.isoformat()
    assert again["id"] == body["id"]
    listed = client.get("/api/ai/commentaries").json()
    assert [c["id"] for c in listed] == [body["id"]]
    assert client.get("/api/ai/commentaries?limit=0").status_code == 422


def test_the_api_has_nothing_to_write_without_holdings(client: TestClient, admin: User) -> None:
    login(client, admin.email)
    app.dependency_overrides[optional_llm] = lambda: None
    try:
        assert client.post("/api/ai/commentaries/now").status_code == 409
    finally:
        app.dependency_overrides.pop(optional_llm, None)


def test_commentaries_are_private_and_need_a_login(
    client: TestClient, db: Session, portfolio: User
) -> None:
    make(db, portfolio, None)
    other = make_user(db, "other@example.com")
    login(client, other.email)
    assert client.get("/api/ai/commentaries").json() == []
    assert TestClient(app).get("/api/ai/commentaries").status_code == 401
    assert TestClient(app).post("/api/ai/commentaries/now").status_code == 401


def test_the_app_cannot_edit_a_commentary(db: Session, portfolio: User) -> None:
    make(db, portfolio, None)
    rls.bypass(db)
    for sql in ("DELETE FROM ai_commentaries", "UPDATE ai_commentaries SET text = 'x'"):
        with pytest.raises(ProgrammingError):
            db.execute(text(sql))
        db.rollback()


def test_a_tool_call_inside_the_weekly_text_does_not_break_it(
    db: Session, owner: TestClient, portfolio: User
) -> None:
    accept(owner)
    fake = Fake(
        reply("", [ToolCall("t1", "get_fx", {"currency": "USD"})]),
        reply("Rates are not stored. " + "y" * 90),
    )
    made = make(db, portfolio, fake)
    assert made.kind == "ai" and made.text.startswith("Rates are not stored.")


def test_hide_amounts_keeps_the_euro_value_out_of_what_the_model_sees(
    db: Session, owner: TestClient, portfolio: User
) -> None:
    accept(owner, anonymise=True)
    fake = Fake(reply("A calm week. " + "x" * 90))
    made = make(db, portfolio, fake)
    sent = fake.requests[0]["messages"][-1]["content"]
    assert "market value" not in sent and "EUR." not in sent.split("Question:")[0].split("Facts")[1]
    assert made.kind == "ai"


def test_a_template_does_not_lock_the_week_when_the_ai_works_later(
    db: Session, owner: TestClient, portfolio: User
) -> None:
    first = make(db, portfolio, None)  # Sunday: no consent yet
    assert first.kind == "template"
    again = make(db, portfolio, Fake())  # without a retry, the same one comes back
    assert again.id == first.id
    accept(owner)
    rls.scope_to_user(db, portfolio.id)
    fake = Fake(reply("Now with the AI. " + "x" * 90))
    better = commentary.create_commentary(db, portfolio.id, TODAY, fake, retry_ai=True)
    assert better is not None and better.kind == "ai"
    stays = commentary.create_commentary(db, portfolio.id, TODAY, Fake(), retry_ai=True)
    assert stays is not None and stays.id == better.id  # the AI text stays
    rls.bypass(db)
    assert len(db.scalars(select(AiCommentary)).all()) == 2  # the template stays as a record


def test_a_failed_retry_keeps_the_template(db: Session, owner: TestClient, portfolio: User) -> None:
    first = make(db, portfolio, None)
    accept(owner)
    rls.scope_to_user(db, portfolio.id)
    kept = commentary.create_commentary(
        db, portfolio.id, TODAY, Fake(reply("", refused=True)), retry_ai=True
    )
    assert kept is not None and kept.id == first.id and kept.kind == "template"


def test_the_list_shows_the_ai_text_of_a_week_that_has_both(
    client: TestClient, db: Session, owner: TestClient, portfolio: User
) -> None:
    make(db, portfolio, None)
    accept(owner)
    rls.scope_to_user(db, portfolio.id)
    commentary.create_commentary(
        db, portfolio.id, TODAY, Fake(reply("AI text. " + "x" * 90)), retry_ai=True
    )
    listed = owner.get("/api/ai/commentaries").json()
    assert len(listed) == 1 and listed[0]["kind"] == "ai"


def test_only_portfolio_alerts_are_counted(db: Session, portfolio: User) -> None:
    rls.bypass(db)
    for n, kind in enumerate(("daily_move", "job_failed", "ai_budget")):
        db.add(Notification(user_id=portfolio.id, kind=kind, severity="info", title="t", body="b", dedupe_key=f"k{n}"))  # fmt: skip
    db.commit()
    assert "Alerts this week: 1." in make(db, portfolio, None).text


def test_without_hide_amounts_the_model_gets_the_value_and_the_template_always_has_it(
    db: Session, owner: TestClient, portfolio: User
) -> None:
    accept(owner)
    fake = Fake(reply("A calm week. " + "x" * 90))
    make(db, portfolio, fake)
    assert "market value 1,950.00 EUR" in fake.requests[0]["messages"][-1]["content"]
    other = make_user(db, "t@example.com")
    hold(db, other, A, "NVIDIA Corp")
    # No consent at all: no model call, and the local template shows the value.
    template = make(db, other, Fake())
    assert template.kind == "template" and "market value" in template.text
