"""Factor scores, the universe, the watchlist, fund scorecards and score history (FR-30 to FR-35)."""

from datetime import date, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from quant import rls
from quant.ingest.mapping import held_isins, valid_isin
from quant.models import (
    FactorScore,
    Fundamental,
    Instrument,
    InstrumentFact,
    PriceEOD,
    UniverseMember,
    WatchlistItem,
)
from quant.portfolio import cash, consolidation, factors, scorecards

from .conftest import login, make_user

TODAY = date(2025, 6, 30)


def make_isin(n: int, country: str = "US") -> str:
    """A valid ISIN (the check digit is found by trying the ten digits)."""
    body = f"{country}{n:09d}"
    return next(body + str(d) for d in range(10) if valid_isin(body + str(d)))


def add_share(
    db: Session, n: int, sector: str | None = "Technology", name: str | None = None
) -> Instrument:
    isin = make_isin(n)
    inst = Instrument(
        code=f"S{n}",
        isin=isin,
        name=name or f"Share {n}",
        asset_class="stock",
        currency="EUR",
        symbols={},
        sector=sector,
    )
    db.add(inst)
    db.commit()
    return inst


def add_prices(
    db: Session, inst: Instrument, days: int, daily: float, end: date = TODAY, start_at: float = 100
) -> None:
    """One close a day for `days` days up to `end`, growing by `daily` (0.001 = 0.1%) a day."""
    level = start_at
    for i in range(days):
        day = end - timedelta(days=days - 1 - i)
        db.add(
            PriceEOD(
                instrument_id=inst.id,
                date=day,
                close=D(str(round(level, 4))),
                currency="EUR",
                source="test",
            )
        )
        level *= 1 + daily
    db.commit()


def fundamentals(db: Session, isin: str, /, **fields: float) -> None:
    db.add(
        Fundamental(
            isin=isin, as_of=TODAY, source="test", **{k: D(str(v)) for k, v in fields.items()}
        )
    )
    db.commit()


FULL = dict(
    ev_ebit=15,
    fcf_yield=4,
    ev_sales=5,
    roic=20,
    gross_margin=60,
    net_debt_ebitda=1,
    revenue_growth=12,
    revenue_cagr_3y=10,
    eps_revision=3,
)


# --- pure statistics ---------------------------------------------------------------------------


def test_percentile_ranks_order_ties_and_direction() -> None:
    ranks = factors.percentile_ranks({"a": 1.0, "b": 2.0, "c": 3.0}, True)
    assert ranks == {"a": 0.0, "b": 50.0, "c": 100.0}
    assert factors.percentile_ranks({"a": 1.0, "b": 2.0, "c": 3.0}, False)["a"] == 100.0
    tied = factors.percentile_ranks({"a": 5.0, "b": 5.0, "c": 9.0}, True)
    assert tied["a"] == tied["b"] == 25.0 and tied["c"] == 100.0
    assert factors.percentile_ranks({"only": 7.0}, True) == {"only": 50.0}
    assert factors.percentile_ranks({}, True) == {}


def series_of(days: int, daily: float, end: date = TODAY) -> list[tuple[date, float]]:
    out, level = [], 100.0
    for i in range(days):
        out.append((end - timedelta(days=days - 1 - i), level))
        level *= 1 + daily
    return out


def test_price_inputs_from_a_long_rising_series() -> None:
    values, flags = factors.price_inputs(series_of(400, 0.001), None)
    assert flags == set()
    assert values["ret_12_1"] is not None and values["ret_12_1"] > 0.2
    assert values["ret_6m"] is not None and 0.15 < values["ret_6m"] < 0.25
    assert values["dist_ma200"] is not None and values["dist_ma200"] > 0
    assert values["vol_1y"] is not None and values["vol_1y"] < 0.01  # a smooth climb
    assert values["drawdown_1y"] == 0.0
    assert values["beta"] is None  # no benchmark given


def test_a_short_history_is_flagged_and_leaves_inputs_missing() -> None:
    values, flags = factors.price_inputs(series_of(100, 0.001), None)
    assert "short_history" in flags
    assert values["ret_12_1"] is None and values["dist_ma200"] is None
    assert values["ret_6m"] is None  # 182 days back is before the first bar
    values, flags = factors.price_inputs([], None)
    assert "short_history" in flags and all(v is None for v in values.values())


def test_beta_follows_the_benchmark() -> None:
    mine = series_of(400, 0.0)
    wiggle = []
    level = 100.0
    for i, (d, _) in enumerate(mine):
        level *= 1.01 if i % 2 else 0.99
        wiggle.append((d, level))
    bench = factors._daily_returns(wiggle)
    own_double = []
    level = 100.0
    for i, (d, _) in enumerate(mine):
        level *= 1.02 if i % 2 else 0.98
        own_double.append((d, level))
    values, _ = factors.price_inputs(own_double, bench)
    assert values["beta"] is not None and 1.8 < values["beta"] < 2.2


def row(**fields: float) -> Fundamental:
    return Fundamental(
        isin="X", as_of=TODAY, source="t", **{k: D(str(v)) for k, v in fields.items()}
    )


def test_fundamentals_with_a_loss_have_no_multiple() -> None:
    values, flags = factors.fundamental_inputs(row(ev_ebit=-4, ev_sales=0, roic=10), TODAY)
    assert values["ev_ebit"] is None and values["ev_sales"] is None  # not "cheap": missing
    assert values["roic"] == 10.0 and flags == set()


def test_missing_and_old_fundamentals_are_flagged() -> None:
    assert factors.fundamental_inputs(None, TODAY)[1] == {"no_fundamentals"}
    assert "no_fundamentals" in factors.fundamental_inputs(row(), TODAY)[1]
    old = Fundamental(isin="X", as_of=TODAY - timedelta(days=201), source="t", roic=D(5))
    assert "stale_fundamentals" in factors.fundamental_inputs(old, TODAY)[1]


def raw(isin: str, sector: str | None = "Tech", /, **values: float) -> factors.Raw:
    full: dict[str, float | None] = {n: None for f in factors.FACTORS for n, _ in factors.INPUTS[f]}
    full.update(values)
    return factors.Raw(isin, isin, sector, full, set())


def test_the_best_share_in_a_sector_scores_highest_and_weights_spread() -> None:
    names = [f"S{i}" for i in range(6)]
    raws = [
        raw(n, roic=10 + 10 * i, gross_margin=30 + 5 * i, net_debt_ebitda=3 - 0.4 * i)
        for i, n in enumerate(names)
    ]
    scored = factors.score_all(raws)
    quality = [q for n in names if (q := scored[n].factors["quality"]) is not None]
    assert (
        len(quality) == 6 and quality == sorted(quality) and quality[0] == 0 and quality[-1] == 100
    )
    # Only quality exists: one factor is too little for a composite.
    assert scored["S5"].composite is None and scored["S5"].data_quality == "poor"
    assert factors.score_all([raw("A", ret_6m=0.2, roic=30)])["A"].peers == 1


def test_the_composite_respells_the_weights_over_the_factors_present() -> None:
    raws = [raw(f"S{i}", ret_6m=i, roic=i * 10, vol_1y=10 - i) for i in range(6)]
    top = factors.score_all(raws)["S5"]
    # momentum (30), quality (25) and low_vol (10): all three are 100 for the best share.
    assert top.factors["momentum"] == 100 and top.factors["quality"] == 100
    assert top.factors["low_vol"] == 100 and top.composite == 100.0
    worst = factors.score_all(raws)["S0"]
    assert worst.composite == 0.0
    # Three factors, but none from value or growth, and each missing inputs: partial, not ok.
    assert top.data_quality == "partial"


def test_data_quality_is_ok_only_with_all_five_factors_and_no_flags() -> None:
    allv = {n: 1.0 for f in factors.FACTORS for n, _ in factors.INPUTS[f]}
    raws = [raw(f"S{i}", **{k: v + i for k, v in allv.items()}) for i in range(6)]
    assert {s.data_quality for s in factors.score_all(raws).values()} == {"ok"}
    raws[0].flags.add("stale_price")
    assert factors.score_all(raws)["S0"].data_quality == "poor"
    raws[1].flags.add("stale_fundamentals")
    assert factors.score_all(raws)["S1"].data_quality == "partial"


def test_a_small_sector_is_ranked_against_the_whole_universe_and_flagged() -> None:
    raws = [raw(f"T{i}", "Tech", roic=i) for i in range(5)] + [raw("L1", "Lone", roic=50)]
    scored = factors.score_all(raws)
    assert "small_peer_group" in scored["L1"].flags
    assert scored["L1"].factors["quality"] == 100  # best of the whole universe
    assert "small_peer_group" not in scored["T0"].flags
    assert factors.FLAG_TEXT["small_peer_group"]


def test_a_missing_input_is_left_out_not_made_up() -> None:
    raws = [raw(f"S{i}", roic=i * 10) for i in range(6)]  # gross margin and debt are missing
    scored = factors.score_all(raws)
    assert "partial_inputs" in scored["S3"].flags
    assert scored["S3"].factors["value"] is None and scored["S3"].factors["growth"] is None


# --- the nightly job ---------------------------------------------------------------------------


def seed_universe(db: Session) -> list[Instrument]:
    shares = [add_share(db, i) for i in range(1, 7)]
    for i, s in enumerate(shares):
        add_prices(db, s, 400, 0.0005 * (i + 1))
        assert s.isin is not None
        fundamentals(db, s.isin, **{k: v * (1 + i / 10) for k, v in FULL.items()})
    return shares


def test_the_job_stores_a_score_a_day_and_a_rerun_replaces_it(db: Session) -> None:
    shares = seed_universe(db)
    first = factors.compute_scores(db, TODAY)
    assert first.rows_written == 6 and first.attempted == 6
    rows = db.scalars(select(FactorScore).where(FactorScore.day == TODAY)).all()
    assert len(rows) == 6
    best = max(rows, key=lambda r: r.momentum or D(0))
    assert best.isin == shares[-1].isin  # the steepest climb has the best momentum
    assert all(r.composite is not None and r.data_quality in ("ok", "partial") for r in rows)
    assert all(0 <= (r.composite_pct or 0) <= 100 for r in rows)
    factors.compute_scores(db, TODAY)
    assert len(db.scalars(select(FactorScore).where(FactorScore.day == TODAY)).all()) == 6
    factors.compute_scores(db, TODAY + timedelta(days=1))  # a new day adds, never replaces
    assert len(db.scalars(select(FactorScore)).all()) == 12


def test_a_share_without_fundamentals_gets_price_factors_and_poor_quality(db: Session) -> None:
    seed_universe(db)
    bare = add_share(db, 9)
    add_prices(db, bare, 400, 0.001)
    factors.compute_scores(db, TODAY)
    row = db.scalar(select(FactorScore).where(FactorScore.isin == bare.isin))
    assert row is not None
    assert row.value is None and row.quality is None and row.growth is None
    assert row.momentum is not None and row.low_vol is not None
    assert "no_fundamentals" in row.flags and row.data_quality == "poor"
    assert row.composite is not None  # two factors are enough for a number, never for "ok"


def test_a_share_with_old_prices_is_stale_and_one_without_prices_has_no_score_values(
    db: Session,
) -> None:
    seed_universe(db)
    old = add_share(db, 8)
    add_prices(db, old, 400, 0.001, end=TODAY - timedelta(days=30))
    empty = add_share(db, 7)
    factors.compute_scores(db, TODAY)
    stale = db.scalar(select(FactorScore).where(FactorScore.isin == old.isin))
    none = db.scalar(select(FactorScore).where(FactorScore.isin == empty.isin))
    assert stale is not None and "stale_price" in stale.flags and stale.data_quality == "poor"
    assert none is not None and none.composite is None and none.data_quality == "poor"
    assert "stale_price" in none.flags


def test_only_active_shares_are_in_the_universe(db: Session) -> None:
    seed_universe(db)
    off = add_share(db, 12)
    off.active = False
    fund = Instrument(
        code="FUND", isin=make_isin(13), name="Fund", asset_class="etf", currency="EUR", symbols={}
    )
    db.add(fund)
    db.commit()
    assert {i.code for i in factors.universe(db)} == {f"S{i}" for i in range(1, 7)}


def snapshot(db: Session, isin: str, day: date, **kw: Any) -> None:
    base: dict[str, Any] = dict(
        value=D(50), quality=D(50), growth=D(50), momentum=D(50), low_vol=D(50),
        composite=D(50), composite_pct=D(50), data_quality="ok", flags=[], peers=6,
    )  # fmt: skip
    base.update(kw)
    db.add(FactorScore(isin=isin, day=day, **base))
    db.commit()


def test_a_steady_fall_over_three_months_is_named(db: Session) -> None:
    isin = make_isin(1)
    for back, quality in ((90, 80), (60, 70), (30, 60), (0, 50)):
        snapshot(db, isin, TODAY - timedelta(days=back), quality=D(quality))
    notes = factors.trend_notes(factors.history(db, isin, 200, TODAY))
    assert notes == ["Quality has fallen for 3 months (80 to 50)."]


def test_a_rise_and_a_wobble_and_a_young_history(db: Session) -> None:
    isin = make_isin(2)
    for back, growth, momentum in ((90, 40, 50), (60, 50, 60), (30, 60, 50), (0, 70, 55)):
        snapshot(db, isin, TODAY - timedelta(days=back), growth=D(growth), momentum=D(momentum))
    notes = factors.trend_notes(factors.history(db, isin, 200, TODAY))
    assert notes == ["Growth has risen for 3 months (40 to 70)."]  # momentum wobbled: no note
    young = make_isin(3)
    snapshot(db, young, TODAY - timedelta(days=10))
    snapshot(db, young, TODAY)
    assert factors.trend_notes(factors.history(db, young, 200, TODAY)) == []
    assert factors.trend_notes([]) == []


# --- the unlock trigger and the consolidation rule that use the scores ---------------------------


def cheap_share(db: Session, n: int = 20) -> Instrument:
    inst = add_share(db, n)
    # 100 a year ago, 70 now: 30% below the high.
    for back, close in ((300, 100), (150, 90), (1, 70)):
        db.add(
            PriceEOD(
                instrument_id=inst.id,
                date=TODAY - timedelta(days=back),
                close=D(close),
                currency="EUR",
                source="t",
            )
        )
    db.commit()
    return inst


def test_the_score_trigger_needs_top_10_percent_ok_quality_and_a_cheap_price(db: Session) -> None:
    inst = cheap_share(db)
    assert inst.isin is not None
    assert cash.score_trigger(db, inst, inst.isin, TODAY)[0] is None  # no score: no data
    snapshot(db, inst.isin, TODAY, composite_pct=D(95))
    ok, text = cash.score_trigger(db, inst, inst.isin, TODAY)
    assert ok is True and "top-5%" in text and "30.0% below" in text


@pytest.mark.parametrize(
    ("change", "words"),
    [
        ({"composite_pct": D(80)}, "must be in the top 10%"),
        ({"data_quality": "partial"}, "data quality is partial"),
        ({"data_quality": "poor"}, "data quality is poor"),
    ],
)
def test_a_score_that_is_not_good_enough_does_not_unlock(
    db: Session, change: dict[str, Any], words: str
) -> None:
    inst = cheap_share(db)
    assert inst.isin is not None
    snapshot(db, inst.isin, TODAY, **{"composite_pct": D(95), **change})
    ok, text = cash.score_trigger(db, inst, inst.isin, TODAY)
    assert ok is False and words in text


def test_the_score_trigger_wants_a_discount_a_steady_quality_and_a_fresh_score(db: Session) -> None:
    rich = add_share(db, 21)
    assert rich.isin is not None
    db.add(
        PriceEOD(
            instrument_id=rich.id,
            date=TODAY - timedelta(days=1),
            close=D(100),
            currency="EUR",
            source="t",
        )
    )
    db.commit()
    snapshot(db, rich.isin, TODAY, composite_pct=D(99))
    ok, text = cash.score_trigger(db, rich, rich.isin, TODAY)
    assert ok is False and "not 25% below" in text

    broken = cheap_share(db, 22)
    assert broken.isin is not None
    snapshot(db, broken.isin, TODAY - timedelta(days=95), quality=D(80))
    snapshot(db, broken.isin, TODAY, composite_pct=D(99), quality=D(60))
    ok, text = cash.score_trigger(db, broken, broken.isin, TODAY)
    assert ok is False and "quality score fell" in text

    old = cheap_share(db, 23)
    assert old.isin is not None
    snapshot(db, old.isin, TODAY - timedelta(days=30), composite_pct=D(99))
    assert cash.score_trigger(db, old, old.isin, TODAY)[0] is None


def test_small_positions_are_judged_by_a_usable_score_only(db: Session) -> None:
    isin = make_isin(30)
    assert consolidation._usable_score(db, isin, TODAY) is None
    snapshot(db, isin, TODAY, composite_pct=D(85))
    assert consolidation._usable_score(db, isin, TODAY) == D(85)
    poor = make_isin(31)
    snapshot(db, poor, TODAY, composite_pct=D(95), data_quality="poor")
    assert consolidation._usable_score(db, poor, TODAY) is None  # gaps protect nothing
    stale = make_isin(32)
    snapshot(db, stale, TODAY - timedelta(days=20), composite_pct=D(95))
    assert consolidation._usable_score(db, stale, TODAY) is None


# --- the universe and the watchlist feed the price job --------------------------------------------


def test_listed_and_watched_shares_are_tracked_like_held_ones(db: Session) -> None:
    owner = make_user(db, "w@example.com")
    listed, watched = make_isin(40), make_isin(41)
    db.add(UniverseMember(list_name="Test", isin=listed, name="Listed Inc"))
    db.add(UniverseMember(list_name="Test", isin="US0000000009", name="Bad check digit"))
    rls.bypass(db)  # the watchlist is row-level secured
    db.add(WatchlistItem(user_id=owner.id, isin=watched, note="Watched Ltd"))
    db.commit()
    tracked = {h.isin: h for h in held_isins(db)}
    assert set(tracked) == {listed, watched}  # the bad ISIN is dropped, the rest are shares
    assert tracked[listed].asset_class == "stock" and tracked[listed].name == "Listed Inc"


# --- the API ---------------------------------------------------------------------------------------


def test_an_admin_loads_a_universe_list_and_it_is_replaced_not_added_to(
    owner: TestClient,
) -> None:
    body = {
        "list_name": "Semis",
        "members": [
            {"isin": make_isin(50), "name": "One"},
            {"isin": make_isin(51), "name": "Two"},
            {"isin": "US0000000009", "name": "Bad"},
        ],
    }
    out = owner.post("/api/scores/universe", json=body)
    assert out.status_code == 200, out.text
    assert out.json() == {"list_name": "Semis", "members": 2, "rejected": ["US0000000009"]}
    body["members"] = body["members"][:1]
    assert owner.post("/api/scores/universe", json=body).json()["members"] == 1
    assert owner.get("/api/scores/universe").json() == [
        {"list_name": "Semis", "members": 1, "rejected": []}
    ]
    assert owner.delete("/api/scores/universe/Semis").status_code == 204
    assert owner.get("/api/scores/universe").json() == []
    assert (
        owner.post(
            "/api/scores/universe", json={"list_name": "X", "members": [{"isin": "nope"}]}
        ).status_code
        == 422
    )


def test_fundamentals_are_stored_replaced_and_checked(owner: TestClient, db: Session) -> None:
    isin = make_isin(52)
    row = {"isin": isin, "as_of": "2025-06-01", "source": "csv", "roic": "21.5", "ev_ebit": "12"}
    out = owner.post(
        "/api/scores/fundamentals", json={"rows": [row, {**row, "isin": "US0000000009"}]}
    )
    assert out.json() == {"stored": 1, "rejected": ["US0000000009"]}
    again = {k: v for k, v in row.items() if k != "ev_ebit"}
    owner.post("/api/scores/fundamentals", json={"rows": [{**again, "roic": "25"}]})
    stored = db.scalars(select(Fundamental)).all()
    assert len(stored) == 1 and stored[0].roic == D("25") and stored[0].source == "csv"
    assert stored[0].ev_ebit is None  # a new row for the same day replaces the old one


def test_the_scores_list_detail_and_scopes(owner: TestClient, db: Session) -> None:
    shares = [add_share(db, i) for i in range(60, 63)]
    for i, s in enumerate(shares):
        assert s.isin is not None
        snapshot(db, s.isin, TODAY, composite=D(40 + 20 * i), composite_pct=D(30 * i + 10),
                 flags=["no_fundamentals"] if i == 0 else [])  # fmt: skip
        snapshot(db, s.isin, TODAY - timedelta(days=1), composite=D(1))
    assert shares[0].isin is not None and shares[1].isin is not None
    assert (
        owner.post("/api/scores/watchlist", json={"isin": shares[1].isin, "note": "n"}).status_code
        == 201
    )
    out = owner.get("/api/scores").json()
    assert out["day"] == TODAY.isoformat() and "not a forecast" in out["note"]
    assert [s["name"] for s in out["scores"]] == ["Share 62", "Share 61", "Share 60"]  # best first
    assert out["weights"]["momentum"] == 30
    low = out["scores"][-1]
    assert low["flags"] == ["no_fundamentals"] and "fundamentals" in low["flag_text"][0]
    assert [s["watched"] for s in out["scores"]] == [False, True, False]
    assert [s["name"] for s in owner.get("/api/scores?scope=watched").json()["scores"]] == [
        "Share 61"
    ]
    assert owner.get("/api/scores?scope=held").json()["scores"] == []
    detail = owner.get(f"/api/scores/{shares[1].isin}").json()
    assert detail["score"]["name"] == "Share 61" and len(detail["history"]) == 2
    assert owner.get("/api/scores/US0000000009").status_code == 404


def test_the_scores_list_is_empty_before_the_first_run(owner: TestClient) -> None:
    out = owner.get("/api/scores").json()
    assert out["day"] is None and out["scores"] == []


def test_the_watchlist_is_per_user(owner: TestClient, client: TestClient, db: Session) -> None:
    isin = make_isin(70)
    assert (
        owner.post("/api/scores/watchlist", json={"isin": isin.lower(), "note": "idea"}).status_code
        == 201
    )
    assert (
        owner.post("/api/scores/watchlist", json={"isin": isin, "note": "again"}).status_code == 201
    )
    [item] = owner.get("/api/scores/watchlist").json()
    assert item["isin"] == isin and item["note"] == "again" and item["has_score"] is False
    assert owner.post("/api/scores/watchlist", json={"isin": "US0000000009"}).status_code == 422
    make_user(db, "friend@example.com")
    client.cookies.clear()
    assert login(client, "friend@example.com") == 200
    assert client.get("/api/scores/watchlist").json() == []  # the owner's item is not theirs
    friend_isin = make_isin(71)
    assert client.post("/api/scores/watchlist", json={"isin": friend_isin}).status_code == 201
    client.cookies.clear()
    assert login(client, "owner@example.com") == 200
    assert [i["isin"] for i in client.get("/api/scores/watchlist").json()] == [isin]
    assert client.delete(f"/api/scores/watchlist/{friend_isin}").status_code == 204  # not theirs
    assert client.delete(f"/api/scores/watchlist/{isin}").status_code == 204
    assert client.get("/api/scores/watchlist").json() == []
    client.cookies.clear()
    assert login(client, "friend@example.com") == 200
    assert len(client.get("/api/scores/watchlist").json()) == 1  # still there


def test_the_watchlist_is_a_user_table_with_row_level_security() -> None:
    assert "watchlist" in rls.USER_TABLES


def test_scores_and_cards_need_the_disclaimer_and_admins_load_the_data(
    owner: TestClient, client: TestClient, db: Session
) -> None:
    make_user(db, "new@example.com", accepted=False)
    client.cookies.clear()
    assert login(client, "new@example.com") == 200
    assert client.get("/api/scores").status_code == 403
    assert client.get("/api/scores/funds").status_code == 403
    make_user(db, "friend@example.com")
    client.cookies.clear()
    assert login(client, "friend@example.com") == 200
    assert (
        client.post("/api/scores/universe", json={"list_name": "X", "members": []}).status_code
        == 403
    )
    assert client.post("/api/scores/fundamentals", json={"rows": []}).status_code == 403
    assert client.put(f"/api/scores/facts/{make_isin(1)}", json={"kind": "etf"}).status_code == 403
    assert client.get("/api/scores").status_code == 200  # reading is open to every user


# --- the scorecards ------------------------------------------------------------------------------------


def line(card: scorecards.Scorecard, label: str) -> scorecards.Line:
    return next(x for x in card.lines if x.label == label)


def fact(**kw: Any) -> InstrumentFact:
    return InstrumentFact(isin="IE00B4L5Y983", kind=kw.pop("kind", "etf"), **kw)


def test_an_etf_card_with_nothing_entered_says_unknown_everywhere() -> None:
    card = scorecards.build("IE00B4L5Y983", "World", "etf", None, [])
    assert {x.verdict for x in card.lines} == {"unknown"}
    assert set(card.missing) == set(scorecards.ETF_FIELDS)
    assert card.tax_note == ""


def test_an_etf_card_judges_each_fact() -> None:
    good = scorecards.build(
        "I", "Good", "etf",
        fact(ter=D("0.20"), tracking_difference=D("-0.1"), fund_size_eur=D(5_000_000_000),
             replication="physical", distribution="acc", domicile="ie"),
        ["Other ETF: 80% of the holdings"],
    )  # fmt: skip
    assert [x.verdict for x in good.lines] == ["good", "good", "good", "good", "info", "good"]
    assert good.missing == [] and good.overlaps == ["Other ETF: 80% of the holdings"]
    assert "Vorabpauschale" in line(good, "Pays out or accumulates").note
    assert "Teilfreistellung" in good.tax_note and "tax adviser" in good.tax_note
    bad = scorecards.build(
        "I", "Bad", "etf",
        fact(ter=D("0.75"), tracking_difference=D("0.9"), fund_size_eur=D(20_000_000),
             replication="synthetic", distribution="dist", domicile="US"),
        [],
    )  # fmt: skip
    verdicts = {x.label: x.verdict for x in bad.lines}
    assert verdicts["Cost (TER)"] == "watch" and verdicts["Fund size"] == "watch"
    assert verdicts["Replication"] == "watch" and verdicts["Domicile"] == "bad"
    assert verdicts["Tracking difference"] == "watch"
    assert "UCITS" in line(bad, "Domicile").note


def test_an_etc_card_physical_with_a_delivery_claim_and_a_futures_one() -> None:
    gold = scorecards.build(
        "D", "Xetra-Gold", "etc",
        fact(kind="etc", ter=D("0.36"), replication="physical", delivery_claim=True,
             issuer="Deutsche Börse Commodities"),
        [],
    )  # fmt: skip
    assert [x.verdict for x in gold.lines] == ["good", "good", "good", "info"]
    assert "tax-free after one year" in line(gold, "Delivery claim").note
    assert "Verify with your tax adviser" in gold.tax_note
    assert "Roll method" not in {x.label for x in gold.lines}
    oil = scorecards.build(
        "O",
        "Oil",
        "etc",
        fact(kind="etc", ter=D("0.90"), replication="futures", delivery_claim=False),
        [],
    )
    labels = {x.label: x for x in oil.lines}
    assert labels["Physical or synthetic"].verdict == "watch"
    assert "contango" in labels["Physical or synthetic"].note.lower()
    assert labels["Roll method"].verdict == "unknown" and "roll_method" in oil.missing
    assert labels["Cost (TER)"].verdict == "watch"


def test_facts_are_entered_by_an_admin_and_show_on_the_card(owner: TestClient, db: Session) -> None:
    isin = make_isin(80, "IE")
    put = owner.put(
        f"/api/scores/facts/{isin}",
        json={
            "kind": "etf",
            "ter": "0.20",
            "replication": "physical",
            "domicile": "ie",
            "distribution": "acc",
        },
    )
    assert put.status_code == 204, put.text
    stored = db.get(InstrumentFact, isin)
    assert stored is not None and stored.domicile == "IE" and stored.ter == D("0.200")
    [card] = owner.get("/api/scores/funds").json()
    assert card["isin"] == isin and card["kind"] == "etf" and card["held"] is False
    by_label = {x["label"]: x for x in card["lines"]}
    assert (
        by_label["Domicile"]["verdict"] == "good" and by_label["Fund size"]["verdict"] == "unknown"
    )
    assert set(card["missing"]) == {"tracking_difference", "fund_size_eur"}
    assert owner.put(f"/api/scores/facts/{isin}", json={"kind": "bond"}).status_code == 422
    assert owner.put("/api/scores/facts/US0000000009", json={"kind": "etf"}).status_code == 422
    assert owner.delete(f"/api/scores/facts/{isin}").status_code == 204
    assert owner.get("/api/scores/funds").json() == []
