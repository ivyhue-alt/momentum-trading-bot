"""Tests for the signal indicators.

These are the only functions in the bot that are pure arithmetic, which
makes them the only ones that can be checked without a broker connection.
They are also where a silent wrong answer does the most damage: the bot ran
for weeks without crashing while computing a compressed EMA spread.
"""
import pytest
from momentum_trading_bot import ema, rsi


# ---- ema ----

def test_ema_of_a_constant_series_is_that_constant():
    assert ema([10.0] * 50, 12) == pytest.approx(10.0)


def test_ema_falls_back_to_the_mean_when_history_is_short():
    """Fewer bars than the period means there is nothing to smooth."""
    assert ema([1.0, 2.0, 3.0], 12) == pytest.approx(2.0)


def test_ema_seeds_from_the_sma_not_the_first_bar():
    """REGRESSION. The original seeded from values[0], so a single opening
    bar dominated the average for roughly `period` steps. Seeded from the
    SMA of the first `period` values, a series of exactly `period` length
    returns that mean exactly."""
    assert ema([0.0] * 11 + [120.0], 12) == pytest.approx(10.0)


def test_fast_ema_tracks_a_rising_series_more_closely_than_slow():
    """The whole crossover signal depends on this being true."""
    closes = [float(i) for i in range(1, 121)]
    assert ema(closes[-48:], 12) > ema(closes[-104:], 26)


# ---- rsi ----

def test_rsi_returns_neutral_without_enough_history():
    assert rsi([1.0, 2.0, 3.0], 14) == pytest.approx(50.0)


def test_rsi_is_maximal_when_every_bar_gains():
    closes = [float(i) for i in range(1, 40)]
    assert rsi(closes, 14) == pytest.approx(100.0)


def test_rsi_is_minimal_when_every_bar_loses():
    closes = [float(40 - i) for i in range(39)]
    assert rsi(closes, 14) == pytest.approx(0.0)


def test_rsi_sits_between_the_bounds_on_mixed_movement():
    closes = [10.0, 11.0, 10.5, 11.5, 11.0, 12.0, 11.5, 12.5,
              12.0, 13.0, 12.5, 13.5, 13.0, 14.0, 13.5, 14.5]
    assert 0.0 < rsi(closes, 14) < 100.0