import json
import sys
import types
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import rls
from quant.ai import budget, consent, picks, pricing
from quant.ai import client as llm
from quant.ai.client import (
    AnthropicLlm,
    LlmAuthError,
    LlmBadRequest,
    LlmNotConfigured,
    LlmRateLimited,
    LlmReply,
    LlmUnavailable,
    ToolCall,
    Usage,
)
from quant.api.ai import llm_client
from quant.config import get_settings
from quant.main import app
from quant.models import (
    AiPick,
    FxRate,
    Instrument,
    LlmUsage,
    Notification,
    PriceEOD,
    Transaction,
    User,
)

from .conftest import login, make_user

ISIN = "US0000000001"
MODEL = get_settings().llm_model


def reply(
    text: str = "",
    calls: list[ToolCall] | None = None,
    usage: Usage | None = None,
    model: str = MODEL,
    refused: bool = False,
) -> LlmReply:
    return LlmReply(
        text=text,
        tool_calls=calls or [],
        stop_reason="refusal" if refused else ("tool_use" if calls else "end_turn"),
        usage=usage or Usage(input_tokens=1000, output_tokens=500),
        model=model,
        request_id="req_1",
        refused=refused,
        raw_content=[{"type": "text", "text": text}],
    )


def pick_call(id_: str = "t1", **over: Any) -> ToolCall:
    data = {
        "name": "Alpha Corp",
        "isin": ISIN,
        "direction": "buy",
        "horizon_months": 12,
        "rationale": "Solid growth.",
    }
    data.update(over)
    return ToolCall(id_, "record_pick", data)


class Fake:
    """A scripted `LlmClient`: replies come out in order and every request is kept."""

    def __init__(self, *replies: LlmReply) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def send(self, *, system: str, messages: list[dict[str, Any]], tools: list[Any]) -> LlmReply:
        self.requests.append(
            {
                "system": system,
                "messages": json.loads(json.dumps(messages, default=str)),
                "tools": tools,
            }
        )
        return self.replies.pop(0)


def hold(db: Session, user: User, isin: str = ISIN, name: str = "Alpha Corp") -> None:
    rls.bypass(db)
    db.add(
        Transaction(
            user_id=user.id, broker="tr", external_id=f"x-{isin}",
            executed_at=datetime(2025, 1, 2, tzinfo=UTC), date=date(2025, 1, 2), kind="trade",
            category="TRADING", type="BUY", asset_class="STOCK", isin=isin, name=name,
            shares=D("10"), price=D("100"), amount=D("-1000"), currency="EUR",
        )
    )  # fmt: skip
    db.commit()


@pytest.fixture
def owner(client: TestClient, admin: User) -> TestClient:
    login(client, admin.email)
    return client


@pytest.fixture
def fake() -> Iterator[Fake]:
    f = Fake()
    app.dependency_overrides[llm_client] = lambda: f
    yield f
    app.dependency_overrides.pop(llm_client, None)


def accept(owner: TestClient, anonymise: bool = False) -> None:
    r = owner.post("/api/ai/consent", json={"accept": True, "anonymise_amounts": anonymise})
    assert r.status_code == 200, r.text


# --- consent (FR-57, FR-4) ---------------------------------------------------------------------


def test_status_asks_for_consent_and_shows_the_disclaimer(owner: TestClient) -> None:
    body = owner.get("/api/ai/status").json()
    assert body["accepted"] is False
    assert "not investment advice" in body["disclaimer"].lower()
    assert body["label"] == consent.LABEL
    assert body["budget"]["user_cap_eur"] == "500"


def test_a_question_needs_consent(owner: TestClient, fake: Fake) -> None:
    r = owner.post("/api/ai/ask", json={"question": "What should I buy?"})
    assert r.status_code == 403
    assert fake.requests == []  # nothing left the app


def test_consent_must_be_a_yes_and_is_stored_with_the_option(owner: TestClient) -> None:
    assert owner.post("/api/ai/consent", json={"accept": False}).status_code == 400
    accept(owner, anonymise=True)
    body = owner.get("/api/ai/status").json()
    assert body["accepted"] is True and body["anonymise_amounts"] is True


def test_an_older_disclaimer_version_asks_again(
    owner: TestClient, db: Session, admin: User
) -> None:
    accept(owner)
    rls.bypass(db)
    row = consent.get(db, admin.id)
    assert row is not None
    row.version = 0
    db.commit()
    assert owner.get("/api/ai/status").json()["accepted"] is False


# --- ask, picks (FR-52, FR-53) ------------------------------------------------------------------


def test_a_recommendation_goes_into_the_pick_log(
    owner: TestClient, fake: Fake, db: Session, admin: User
) -> None:
    hold(db, admin)
    rls.bypass(db)
    instrument = Instrument(
        code="ALPHA", isin=ISIN, name="Alpha Corp", asset_class="stock", currency="EUR"
    )
    db.add(instrument)
    db.flush()
    db.add(
        PriceEOD(
            instrument_id=instrument.id,
            date=date(2026, 10, 1),
            close=D("123.45"),
            currency="EUR",
            source="yahoo",
        )
    )
    db.commit()  # fmt: skip
    accept(owner)
    fake.replies = [
        reply("Looking at Alpha.", [pick_call()]),
        reply("Add to Alpha Corp. Main risk: a weak quarter."),
    ]
    body = owner.post("/api/ai/ask", json={"question": "  What   should I buy? "}).json()
    assert body["answer"].startswith("Add to Alpha")
    assert body["label"] == consent.LABEL
    assert body["fallback_used"] is False and body["refused"] is False
    assert any("positions" in p for p in body["data_points"])
    [pick] = body["picks"]
    assert (pick["direction"], pick["horizon_months"], pick["held"]) == ("buy", 12, True)
    assert pick["question"] == "What should I buy?"
    assert pick["price"] == "123.450000" and pick["price_date"] == "2026-10-01"
    listed = owner.get("/api/ai/picks").json()
    assert [p["id"] for p in listed] == [pick["id"]]
    # The tool result went back with the model's own content block.
    last = fake.requests[1]["messages"][-1]["content"][0]
    assert last["type"] == "tool_result" and last["tool_use_id"] == "t1"


def test_the_portfolio_summary_has_names_and_weights_but_no_personal_data(
    owner: TestClient, fake: Fake, db: Session, admin: User
) -> None:
    hold(db, admin)
    accept(owner)
    fake.replies = [reply("Nothing to change.")]
    owner.post("/api/ai/ask", json={"question": "How am I doing?"})
    prompt = fake.requests[0]["messages"][0]["content"]
    assert "Alpha Corp" in prompt and ISIN in prompt
    assert admin.email not in prompt and admin.display_name not in prompt
    assert "Opus" not in fake.requests[0]["system"]
    assert fake.requests[0]["tools"][0]["name"] == "record_pick"


def test_hide_amounts_sends_weights_only(
    owner: TestClient, fake: Fake, db: Session, admin: User
) -> None:
    hold(db, admin)
    accept(owner, anonymise=True)
    fake.replies = [reply("Fine.")]
    body = owner.post("/api/ai/ask", json={"question": "How am I doing?"}).json()
    prompt = fake.requests[0]["messages"][0]["content"]
    assert "weight_pct" in prompt
    for hidden in ("value_eur", "unrealised_pct", "valued_total_eur"):
        assert hidden not in prompt
    assert any("hidden" in p for p in body["data_points"])


def test_a_recommendation_without_a_pick_gets_one_follow_up(
    owner: TestClient, fake: Fake, db: Session, admin: User
) -> None:
    hold(db, admin)
    accept(owner)
    fake.replies = [
        reply("You should sell Alpha Corp."),
        reply("", [pick_call(direction="sell")]),
        reply("Recorded."),
    ]
    body = owner.post("/api/ai/ask", json={"question": "Sell anything?"}).json()
    assert [p["direction"] for p in body["picks"]] == ["sell"]
    assert body["answer"] == "You should sell Alpha Corp."  # not the follow-up's "Recorded."
    follow_up = fake.requests[1]["messages"][-1]["content"]
    assert "record_pick" in follow_up


def test_the_follow_up_happens_once_and_none_is_accepted(
    owner: TestClient, fake: Fake, db: Session, admin: User
) -> None:
    accept(owner)
    fake.replies = [reply("Hold your shares."), reply("none")]
    body = owner.post("/api/ai/ask", json={"question": "Anything?"}).json()
    assert body["picks"] == [] and len(fake.requests) == 2
    assert body["answer"] == "Hold your shares."  # not the follow-up's "none"


def test_an_answer_with_no_advice_is_one_call(owner: TestClient, fake: Fake) -> None:
    accept(owner)
    fake.replies = [reply("A diversified portfolio spreads risk.")]
    body = owner.post("/api/ai/ask", json={"question": "What is diversification?"}).json()
    assert body["picks"] == [] and len(fake.requests) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"direction": "maybe"},
        {"horizon_months": 0},
        {"horizon_months": 121},
        {"horizon_months": "12"},
        {"horizon_months": True},
        {"rationale": "  "},
        {"name": ""},
    ],
)
def test_an_invalid_pick_is_sent_back_as_a_tool_error(
    owner: TestClient, fake: Fake, db: Session, bad: dict[str, Any]
) -> None:
    accept(owner)
    fake.replies = [reply("", [pick_call(**bad)]), reply("Sorry, I could not record it.")]
    body = owner.post("/api/ai/ask", json={"question": "Buy what?"}).json()
    assert body["picks"] == []
    result = fake.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    rls.bypass(db)
    assert db.scalars(select(AiPick)).all() == []


def test_an_unknown_tool_is_an_error_not_a_crash(owner: TestClient, fake: Fake) -> None:
    accept(owner)
    fake.replies = [reply("", [ToolCall("t9", "rm_rf", {})]), reply("ok")]
    assert owner.post("/api/ai/ask", json={"question": "Hello there"}).status_code == 200
    assert fake.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_the_loop_stops_after_a_few_turns(owner: TestClient, fake: Fake) -> None:
    accept(owner)
    fake.replies = [reply("again", [pick_call(f"t{i}")]) for i in range(10)]
    assert owner.post("/api/ai/ask", json={"question": "Loop forever"}).status_code == 200
    assert len(fake.requests) == 4  # MAX_TURNS + 1


def test_a_refusal_is_reported_and_costs_are_kept(
    owner: TestClient, fake: Fake, db: Session
) -> None:
    accept(owner)
    fake.replies = [reply("", refused=True)]
    body = owner.post("/api/ai/ask", json={"question": "Something odd"}).json()
    assert body["refused"] is True and "declined" in body["answer"]
    rls.bypass(db)
    assert len(db.scalars(select(LlmUsage)).all()) == 1


def test_a_fallback_model_is_reported(owner: TestClient, fake: Fake) -> None:
    accept(owner)
    fake.replies = [reply("Fine.", model="claude-other")]
    body = owner.post("/api/ai/ask", json={"question": "Hello there"}).json()
    assert body["fallback_used"] is True and body["model"] == "claude-other"


def test_question_length_is_checked(owner: TestClient, fake: Fake) -> None:
    accept(owner)
    assert owner.post("/api/ai/ask", json={"question": "x" * 1001}).status_code == 422
    assert owner.post("/api/ai/ask", json={"question": "hi"}).status_code == 422
    assert owner.post("/api/ai/ask", json={"question": "      "}).status_code == 422
    assert fake.requests == []


def test_picks_are_private_to_their_owner(
    owner: TestClient, fake: Fake, db: Session, client: TestClient
) -> None:
    accept(owner)
    fake.replies = [reply("", [pick_call()]), reply("Done.")]
    owner.post("/api/ai/ask", json={"question": "Buy what?"})
    other = make_user(db, "other@example.com")
    other_client = TestClient(app)
    login(other_client, other.email)
    assert other_client.get("/api/ai/picks").json() == []
    assert other_client.get("/api/ai/status").json()["accepted"] is False
    assert other_client.get("/api/ai/status").json()["budget"]["total_eur"] is None
    assert owner.get("/api/ai/status").json()["budget"]["total_eur"] is not None


def test_the_ai_needs_a_login(client: TestClient) -> None:
    for call in (
        client.get("/api/ai/status"),
        client.get("/api/ai/picks"),
        client.post("/api/ai/ask", json={"question": "Hello there"}),
        client.post("/api/ai/consent", json={"accept": True}),
    ):
        assert call.status_code == 401


# --- the ledger and the caps (FR-56, D36) -------------------------------------------------------


def test_cost_is_tokens_times_the_list_price(db: Session) -> None:
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=500_000,
        cache_write_tokens=200_000,
    )
    # 4 + 2 + 0.10 + 1 = 7.10 USD
    assert pricing.cost_usd(usage, get_settings()) == D("7.100000")
    assert pricing.to_eur(D("7.10"), D("1.1")) == D("6.454545")
    assert pricing.to_eur(D("7.10"), None) == D(
        "7.100000"
    )  # no rate: one dollar counts as one euro
    assert pricing.to_eur(D("7.10"), D("0")) == D("7.100000")


def test_the_euro_rate_must_be_recent(db: Session) -> None:
    db.add(FxRate(quote="USD", date=date(2026, 10, 1), rate=D("1.25"), source="ecb"))
    db.commit()
    assert pricing.usd_per_eur(db, date(2026, 10, 5)) == D("1.25")
    assert pricing.usd_per_eur(db, date(2026, 10, 20)) is None


def test_every_call_is_in_the_ledger(owner: TestClient, fake: Fake, db: Session) -> None:
    accept(owner)
    fake.replies = [
        reply("", [pick_call()], usage=Usage(2000, 100, 300, 50)),
        reply("Done.", usage=Usage(1000, 200)),
    ]
    owner.post("/api/ai/ask", json={"question": "Buy what?"})
    rls.bypass(db)
    rows = db.scalars(select(LlmUsage).order_by(LlmUsage.created_at)).all()
    assert [
        (r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens) for r in rows
    ] == [
        (2000, 100, 300, 50),
        (1000, 200, 0, 0),
    ]
    assert all(r.purpose == "ask" and r.model == MODEL and r.cost_eur > 0 for r in rows)
    pick = db.scalars(select(AiPick)).one()
    assert pick.usage_id == rows[0].id


def put_usage(db: Session, user: User, eur: str, when: datetime | None = None) -> None:
    rls.bypass(db)
    row = LlmUsage(
        user_id=user.id, purpose="ask", model="m", input_tokens=0, output_tokens=0,
        cache_read_tokens=0, cache_write_tokens=0, cost_usd=D(eur), cost_eur=D(eur),
    )  # fmt: skip
    if when:
        row.created_at = when
    db.add(row)
    db.commit()


def test_the_user_cap_pauses_that_user_only(
    owner: TestClient, fake: Fake, db: Session, admin: User
) -> None:
    accept(owner)
    put_usage(db, admin, "500")
    r = owner.post("/api/ai/ask", json={"question": "Hello there"})
    assert r.status_code == 429 and "your" in r.json()["detail"]
    assert fake.requests == []
    other = make_user(db, "other@example.com")
    other_client = TestClient(app)
    login(other_client, other.email)
    other_client.post("/api/ai/consent", json={"accept": True})
    fake.replies = [reply("Fine.")]
    assert other_client.post("/api/ai/ask", json={"question": "Hello there"}).status_code == 200


def test_the_total_cap_pauses_everybody(
    owner: TestClient, fake: Fake, db: Session, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    accept(owner)
    other = make_user(db, "other@example.com")
    monkeypatch.setattr(get_settings(), "llm_monthly_cap_eur", D("100"))
    put_usage(db, other, "100")
    r = owner.post("/api/ai/ask", json={"question": "Hello there"})
    assert r.status_code == 429 and "household" in r.json()["detail"]


def test_last_months_spend_does_not_count(
    owner: TestClient, fake: Fake, db: Session, admin: User
) -> None:
    accept(owner)
    put_usage(db, admin, "9999", when=datetime(2020, 1, 15, tzinfo=UTC))
    fake.replies = [reply("Fine.")]
    assert owner.post("/api/ai/ask", json={"question": "Hello there"}).status_code == 200


def test_a_month_is_a_berlin_month() -> None:
    # 23:30 UTC on 30 Sep is already 1 Oct in Berlin.
    start, label = budget.month_bounds(datetime(2026, 9, 30, 23, 30, tzinfo=UTC))
    assert label == "2026-10" and start == datetime(2026, 9, 30, 22, 0, tzinfo=UTC)


def test_admins_are_told_once_at_each_threshold(
    db: Session, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    member = make_user(db, "m@example.com")
    monkeypatch.setattr(get_settings(), "llm_monthly_cap_eur", D("100"))
    put_usage(db, member, "49")
    assert budget.alert_thresholds() == 0
    put_usage(db, member, "2")  # 51 %
    assert budget.alert_thresholds() == 1
    assert budget.alert_thresholds() == 0  # the same month: stored once
    put_usage(db, member, "50")  # 101 %
    assert budget.alert_thresholds() == 2  # 80 and 100
    rls.bypass(db)
    rows = db.scalars(select(Notification).where(Notification.kind == "ai_budget")).all()
    assert {r.user_id for r in rows} == {admin.id}  # a member is not told
    assert sorted(r.dedupe_key.split(":")[-1] for r in rows) == ["100", "50", "80"]
    assert any(r.severity == "warning" for r in rows)


def test_a_failed_alert_does_not_fail_the_question(
    owner: TestClient, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> int:
        raise RuntimeError("db down")

    monkeypatch.setattr(budget, "alert_thresholds", boom)
    accept(owner)
    fake.replies = [reply("Fine.")]
    assert owner.post("/api/ai/ask", json={"question": "Hello there"}).status_code == 200


# --- the adapter ----------------------------------------------------------------------------


def test_without_a_key_the_ai_is_not_set_up(
    owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)
    accept(owner)
    r = owner.post("/api/ai/ask", json={"question": "Hello there"})
    assert r.status_code == 503 and "not set up" in r.json()["detail"]
    assert owner.get("/api/ai/status").json()["configured"] is False


def test_a_missing_package_is_not_set_up_either(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import raises ImportError
    with pytest.raises(LlmNotConfigured, match="package"):
        AnthropicLlm(api_key="k", model="m", effort="medium", max_tokens=10, fallbacks=True)


class FakeSdk(types.ModuleType):
    """Just enough of the `anthropic` package: exceptions, a client, and a recorded call."""

    class APIError(Exception):
        pass

    class AuthenticationError(APIError):
        pass

    class PermissionDeniedError(APIError):
        pass

    class RateLimitError(APIError):
        pass

    class BadRequestError(APIError):
        pass

    class NotFoundError(APIError):
        pass

    class APIStatusError(APIError):
        pass

    class APIConnectionError(APIError):
        pass

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        super().__init__("anthropic")
        self.client_args: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []
        outer = self

        class Messages:
            def create(self, **kw: Any) -> Any:
                outer.calls.append(kw)
                if error:
                    raise error
                return result

        class Beta:
            messages = Messages()

        class Anthropic:
            def __init__(self, **kw: Any) -> None:
                outer.client_args = kw
                self.beta = Beta()

        self.Anthropic = Anthropic


def sdk_response(**over: Any) -> Any:
    data: dict[str, Any] = {
        "content": [
            types.SimpleNamespace(type="thinking", thinking="..."),
            types.SimpleNamespace(type="text", text="Hello."),
            types.SimpleNamespace(type="tool_use", id="t1", name="record_pick", input={"a": 1}),
        ],
        "stop_reason": "tool_use",
        "usage": types.SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=None,
            cache_creation_input_tokens=3,
        ),
        "model": "claude-x",
        "_request_id": "req_9",
    }
    data.update(over)
    return types.SimpleNamespace(**data)


def adapter(monkeypatch: pytest.MonkeyPatch, sdk: FakeSdk, fallbacks: bool = True) -> AnthropicLlm:
    monkeypatch.setitem(sys.modules, "anthropic", sdk)
    return AnthropicLlm(
        api_key="sk-secret", model="claude-m", effort="high", max_tokens=99, fallbacks=fallbacks
    )


def test_the_adapter_sends_the_opus_5_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = FakeSdk(sdk_response())
    out = adapter(monkeypatch, sdk).send(
        system="S", messages=[{"role": "user", "content": "q"}], tools=[picks.TOOL]
    )
    [call] = sdk.calls
    assert call["model"] == "claude-m" and call["max_tokens"] == 99 and call["system"] == "S"
    assert call["output_config"] == {"effort": "high"}
    assert call["tool_choice"] == {"type": "auto"}  # a forced tool is a 400 on this model
    assert "thinking" not in call and "temperature" not in call
    assert call["betas"] == [llm.FALLBACK_BETA] and call["fallbacks"] == "default"
    assert sdk.client_args["api_key"] == "sk-secret"
    assert out.text == "Hello." and out.model == "claude-x" and out.request_id == "req_9"
    assert out.tool_calls == [ToolCall("t1", "record_pick", {"a": 1})]
    assert out.usage == Usage(10, 5, 0, 3)  # a None count is 0
    assert len(out.raw_content) == 3 and out.refused is False


def test_fallbacks_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = FakeSdk(sdk_response())
    adapter(monkeypatch, sdk, fallbacks=False).send(system="S", messages=[], tools=[])
    assert "betas" not in sdk.calls[0] and "fallbacks" not in sdk.calls[0]


def test_a_refusal_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = FakeSdk(sdk_response(stop_reason="refusal", content=[]))
    assert adapter(monkeypatch, sdk).send(system="S", messages=[], tools=[]).refused is True
    sdk = FakeSdk(
        sdk_response(stop_reason="end_turn", stop_details=types.SimpleNamespace(category="x"))
    )
    assert adapter(monkeypatch, sdk).send(system="S", messages=[], tools=[]).refused is True


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("AuthenticationError", LlmAuthError),
        ("PermissionDeniedError", LlmAuthError),
        ("RateLimitError", LlmRateLimited),
        ("BadRequestError", LlmBadRequest),
        ("NotFoundError", LlmBadRequest),
        ("APIStatusError", LlmUnavailable),
        ("APIConnectionError", LlmUnavailable),
    ],
)
def test_sdk_errors_become_adapter_errors_without_the_key(
    monkeypatch: pytest.MonkeyPatch, name: str, expected: type[Exception]
) -> None:
    sdk = FakeSdk(error=getattr(FakeSdk, name)("sk-secret leaked in the message"))
    with pytest.raises(expected) as info:
        adapter(monkeypatch, sdk).send(system="S", messages=[], tools=[])
    assert "sk-secret" not in str(info.value)


def test_errors_become_http_statuses(owner: TestClient, fake: Fake) -> None:
    class Boom:
        def __init__(self, exc: Exception) -> None:
            self.exc = exc

        def send(self, **kw: Any) -> LlmReply:
            raise self.exc

    accept(owner)
    for exc, code in [
        (LlmRateLimited("slow down"), 429),
        (LlmUnavailable("down"), 502),
        (LlmAuthError("key"), 503),
    ]:
        app.dependency_overrides[llm_client] = lambda exc=exc: Boom(exc)
        assert owner.post("/api/ai/ask", json={"question": "Hello there"}).status_code == code


def test_the_new_tables_are_row_level_secured(db: Session, admin: User) -> None:
    other = make_user(db, "o@example.com")
    put_usage(db, admin, "1")
    rls.bypass(db)
    db.add(
        AiPick(
            user_id=admin.id,
            question="q",
            name="n",
            direction="buy",
            horizon_months=1,
            rationale="r",
            held=False,
        )
    )
    db.commit()
    db.expire_all()
    rls.scope_to_user(db, other.id)
    db.info.pop(rls.BYPASS_KEY, None)
    assert db.scalars(select(AiPick)).all() == []
    assert db.scalars(select(LlmUsage)).all() == []


def test_a_dated_name_of_the_configured_model_is_not_a_fallback(
    owner: TestClient, fake: Fake
) -> None:
    accept(owner)
    fake.replies = [reply("Fine.", model=f"{MODEL}-20261001")]
    assert (
        owner.post("/api/ai/ask", json={"question": "Hello there"}).json()["fallback_used"] is False
    )


def test_a_pick_the_model_cannot_fix_in_time_is_reported(owner: TestClient, fake: Fake) -> None:
    accept(owner)
    good = [reply("again", [pick_call(f"t{i}")]) for i in range(3)]
    fake.replies = [*good, reply("last", [pick_call("t9", horizon_months=0)])]
    body = owner.post("/api/ai/ask", json={"question": "Buy what?"}).json()
    assert len(body["picks"]) == 3
    assert body["notes"] and "could not be recorded" in body["notes"][0]


def test_a_pick_fixed_on_the_next_turn_has_no_note(owner: TestClient, fake: Fake) -> None:
    accept(owner)
    fake.replies = [
        reply("", [pick_call(horizon_months=0)]),
        reply("", [pick_call("t2")]),
        reply("Done."),
    ]
    body = owner.post("/api/ai/ask", json={"question": "Buy what?"}).json()
    assert len(body["picks"]) == 1 and body["notes"] == []


def test_profit_is_left_out_when_the_cost_is_unknown(
    owner: TestClient, fake: Fake, db: Session, admin: User
) -> None:
    # A free receipt has no cost, so the position must not carry a profit figure.
    rls.bypass(db)
    db.add(
        Transaction(
            user_id=admin.id, broker="tr", external_id="gift",
            executed_at=datetime(2025, 1, 2, tzinfo=UTC), date=date(2025, 1, 2), kind="delivery",
            category="DELIVERY", type="FREE_RECEIPT", asset_class="CRYPTO", isin="XF000BTC0017",
            name="Bitcoin", shares=D("0.1"), currency="EUR",
        )
    )  # fmt: skip
    db.commit()
    accept(owner)
    fake.replies = [reply("Fine.")]
    owner.post("/api/ai/ask", json={"question": "How am I doing?"})
    assert "unrealised_pct" not in fake.requests[0]["messages"][0]["content"]


def test_a_pick_finds_its_price_by_ticker_when_the_isin_is_unknown(db: Session) -> None:
    instrument = Instrument(
        code="ALPHA", isin=None, name="Alpha", asset_class="stock", currency="EUR"
    )
    db.add(instrument)
    db.flush()
    db.add(
        PriceEOD(
            instrument_id=instrument.id,
            date=date(2026, 10, 1),
            close=D("5"),
            currency="EUR",
            source="yahoo",
        )
    )
    db.commit()  # fmt: skip
    assert picks.latest_price(db, "XX0000000000", "alpha") == (D("5"), "EUR", date(2026, 10, 1))
    assert picks.latest_price(db, "XX0000000000", None) is None


def test_a_threshold_is_checked_only_when_a_call_crosses_it(
    db: Session, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "llm_monthly_cap_eur", D("100"))
    put_usage(db, admin, "60")
    assert budget.alert_thresholds(added=D("1")) == 0  # 59 -> 60: nothing crossed
    assert budget.alert_thresholds(added=D("20")) == 1  # 40 -> 60 crosses 50


def test_the_app_cannot_change_the_ledger_or_the_pick_log(db: Session, admin: User) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import ProgrammingError

    put_usage(db, admin, "1")
    db.add(
        AiPick(
            user_id=admin.id,
            question="q",
            name="n",
            direction="buy",
            horizon_months=1,
            rationale="r",
            held=False,
        )
    )
    db.commit()  # fmt: skip
    for sql in (
        "DELETE FROM llm_usage",
        "UPDATE llm_usage SET cost_eur = 0",
        "DELETE FROM ai_picks",
        "UPDATE ai_picks SET name = 'x'",
    ):
        with pytest.raises(ProgrammingError):
            db.execute(text(sql))
        db.rollback()


def test_a_short_reply_after_the_tool_results_does_not_replace_the_answer(
    owner: TestClient, fake: Fake
) -> None:
    accept(owner)
    fake.replies = [
        reply("Alpha looks solid. Main risk: one weak quarter.", [pick_call()]),
        reply("Done."),
    ]
    body = owner.post("/api/ai/ask", json={"question": "Buy what?"}).json()
    assert body["answer"].startswith("Alpha looks solid")


class Boom:
    """Answers once, then fails."""

    def __init__(self, first: LlmReply, exc: Exception) -> None:
        self.first, self.exc, self.calls = first, exc, 0

    def send(self, **kw: Any) -> LlmReply:
        self.calls += 1
        if self.calls == 1:
            return self.first
        raise self.exc


def test_an_error_after_the_first_turn_keeps_the_answer_and_the_picks(
    owner: TestClient, db: Session
) -> None:
    accept(owner)
    boom = Boom(reply("Alpha looks solid.", [pick_call()]), LlmRateLimited("slow"))
    app.dependency_overrides[llm_client] = lambda: boom
    r = owner.post("/api/ai/ask", json={"question": "Buy what?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "Alpha looks solid." and len(body["picks"]) == 1
    assert any("failed part way" in n for n in body["notes"])
    # A first-turn error is still an error.
    boom.calls = 1  # the next call raises
    assert owner.post("/api/ai/ask", json={"question": "Again please"}).status_code == 429


def test_the_cap_is_checked_before_every_call(
    owner: TestClient, fake: Fake, db: Session, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    accept(owner)
    monkeypatch.setattr(get_settings(), "llm_user_monthly_cap_eur", D("0.01"))
    # The first call costs 0.01 or more (1000 in, 500 out at the list price), so the second is refused.
    fake.replies = [reply("Alpha looks solid.", [pick_call()]), reply("never sent")]
    body = owner.post("/api/ai/ask", json={"question": "Buy what?"}).json()
    assert len(fake.requests) == 1 and len(body["picks"]) == 1
    assert any("budget ran out" in n for n in body["notes"])


def test_a_cut_off_answer_is_flagged(owner: TestClient, fake: Fake) -> None:
    accept(owner)
    cut = reply("Half an ans")
    cut.stop_reason = "max_tokens"
    fake.replies = [cut]
    body = owner.post("/api/ai/ask", json={"question": "Tell me more"}).json()
    assert any("cut off" in n for n in body["notes"])


def test_a_pick_with_only_a_ticker_still_counts_as_held_and_priced(
    owner: TestClient, fake: Fake, db: Session, admin: User
) -> None:
    hold(db, admin)
    rls.bypass(db)
    instrument = Instrument(
        code="NVDA", isin=ISIN, name="Alpha Corp", asset_class="stock", currency="USD"
    )
    db.add(instrument)
    db.flush()
    db.add(
        PriceEOD(
            instrument_id=instrument.id,
            date=date(2026, 10, 1),
            close=D("10"),
            currency="USD",
            source="yahoo",
        )
    )
    db.commit()  # fmt: skip
    accept(owner)
    fake.replies = [
        reply("", [pick_call(isin=None, ticker="nvda", direction="sell")]),
        reply("Sell it."),
    ]
    [pick] = owner.post("/api/ai/ask", json={"question": "Sell what?"}).json()["picks"]
    assert pick["held"] is True and pick["price"] == "10.000000"
    # A wrong ISIN with the right ticker is resolved the same way.
    fake.replies = [reply("", [pick_call("t2", isin="XX0000000000", ticker="NVDA")]), reply("ok")]
    [again] = owner.post("/api/ai/ask", json={"question": "Sell what?"}).json()["picks"]
    assert again["held"] is True and again["price"] == "10.000000"


def test_wider_recommendation_words_trigger_the_follow_up(owner: TestClient, fake: Fake) -> None:
    accept(owner)
    fake.replies = [reply("I would overweight Alpha."), reply("none")]
    owner.post("/api/ai/ask", json={"question": "Any ideas?"})
    assert len(fake.requests) == 2
