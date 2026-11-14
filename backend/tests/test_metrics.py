"""Return and risk statistics (FR-21, FR-22)."""

from datetime import date

import pytest

from quant.portfolio import metrics


def test_xirr_of_a_simple_gain() -> None:
    # 100 in, 110 out one year later: 10% a year.
    rate = metrics.xirr([(date(2024, 1, 1), -100.0), (date(2024, 12, 31), 110.0)])
    assert rate == pytest.approx(0.10, abs=1e-6)


def test_xirr_with_a_second_deposit() -> None:
    # 100 in, 100 more half a year in, 231 out at the end: solve by hand with the formula.
    flows = [(date(2024, 1, 1), -100.0), (date(2024, 7, 1), -100.0), (date(2025, 1, 1), 231.0)]
    rate = metrics.xirr(flows)
    assert rate is not None
    npv = sum(a / (1 + rate) ** ((d - flows[0][0]).days / 365) for d, a in flows)
    assert npv == pytest.approx(0.0, abs=1e-6)
    assert 0.1 < rate < 0.25


def test_xirr_loss_is_negative() -> None:
    rate = metrics.xirr([(date(2024, 1, 1), -100.0), (date(2025, 1, 1), 80.0)])
    assert rate is not None and rate == pytest.approx(-0.2, abs=1e-3)


@pytest.mark.parametrize(
    "flows",
    [
        [],
        [(date(2024, 1, 1), -100.0)],
        [(date(2024, 1, 1), 100.0), (date(2025, 1, 1), 50.0)],  # no money paid in
        [(date(2024, 1, 1), -100.0), (date(2024, 1, 10), 110.0)],  # under 30 days
    ],
)
def test_xirr_has_no_answer(flows: list[tuple[date, float]]) -> None:
    assert metrics.xirr(flows) is None


def test_chain_and_total_return() -> None:
    assert metrics.chain([0.1, -0.1]) == pytest.approx([100.0, 110.0, 99.0])
    assert metrics.total_return([0.1, 0.1]) == pytest.approx(0.21)
    assert metrics.total_return([]) is None


def test_volatility_is_annualised_and_needs_data() -> None:
    returns = [0.01, -0.01] * 20
    vol = metrics.annualised_volatility(returns)
    assert vol is not None
    sample = metrics.stdev(returns)
    assert sample is not None and vol == pytest.approx(sample * 252**0.5)
    assert metrics.annualised_volatility([0.01] * 10) is None


def test_max_drawdown() -> None:
    days = [date(2024, 1, d) for d in range(1, 8)]
    levels = [100.0, 120.0, 90.0, 95.0, 130.0, 104.0, 110.0]
    result = metrics.max_drawdown(days, levels)
    assert result is not None
    depth, peak, low = result
    assert depth == pytest.approx(0.25)  # 120 to 90
    assert (peak, low) == (days[1], days[2])
    rising = metrics.max_drawdown(days[:3], [1.0, 2.0, 3.0])
    assert rising is not None and rising[0] == 0.0
    assert metrics.max_drawdown(days[:1], [1.0]) is None


def test_beta_and_correlation() -> None:
    bench = {date.fromordinal(739000 + i): ((-1) ** i) * 0.01 * (1 + i % 3) for i in range(80)}
    double = {d: 2 * r for d, r in bench.items()}
    assert metrics.beta(double, bench) == pytest.approx(2.0)
    assert metrics.beta(bench, bench) == pytest.approx(1.0)
    assert metrics.beta({d: r for d, r in list(bench.items())[:10]}, bench) is None  # too few days
    flat = {d: 0.0 for d in bench}
    assert metrics.beta(double, flat) is None  # no variance in the benchmark
    a = list(bench.values())
    assert metrics.correlation(a, [2 * x for x in a]) == pytest.approx(1.0)
    assert metrics.correlation(a, [-x for x in a]) == pytest.approx(-1.0)
    assert metrics.correlation(a, [0.0] * len(a)) is None
    assert metrics.correlation(a, a[:-1]) is None


def test_var_and_cvar() -> None:
    # -4.9% ... +5.0% in 0.1% steps: 100 values, the 5% quantile is between -4.5% and -4.4%.
    returns = [i / 1000 for i in range(-49, 51)]
    result = metrics.var_cvar(returns)
    assert result is not None
    var, cvar = result
    assert var == pytest.approx(0.04405)
    assert cvar == pytest.approx(0.047)  # the mean of -4.9% ... -4.5%
    assert cvar >= var
    assert metrics.var_cvar(returns[:30]) is None


def test_var_uses_only_the_last_year() -> None:
    old_crash = [-0.5] * 100
    recent = [0.001 * ((-1) ** i) for i in range(252)]
    result = metrics.var_cvar(old_crash + recent)
    assert result is not None and result[0] < 0.01  # the crash is older than a year


def test_a_gain_only_book_has_no_loss() -> None:
    result = metrics.var_cvar([0.01] * 100)
    assert result == (0.0, 0.0)
