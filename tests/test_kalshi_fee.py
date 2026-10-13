"""Kalshi-Taker-Gebuehr: ceil(0.07*C*P*(1-P)) auf den Cent. Referenz in exakter Ganzzahl-Arithmetik."""
import pytest
from hypothesis import given, strategies as st

from platforms import kalshi


def ref_fee_cents(contracts, price_cents):
    # fee[USD] = 0.07*C*(p/100)*(1-p/100) = 7*C*p*(100-p)/10^6 USD = 7*C*p*(100-p)/10^4 Cent
    num = 7 * contracts * price_cents * (100 - price_cents)
    return -(-num // 10000)  # ceil


def test_bekannter_fall_4_kontrakte_50_cent():
    # exakt 0,07 USD -> darf nicht auf 0,08 aufgerundet werden (Float-Rauschen 0.07000000000000002)
    assert kalshi.taker_fee(4, 0.50) == pytest.approx(0.07)


@given(st.integers(1, 2000), st.integers(1, 99))
def test_fee_gleich_exakter_referenz(contracts, price_cents):
    got = kalshi.taker_fee(contracts, price_cents / 100)
    assert round(got * 100) == ref_fee_cents(contracts, price_cents)


@given(st.floats(0.01, 5000, allow_nan=False), st.floats(0.01, 0.99, allow_nan=False))
def test_fee_nie_unter_exaktem_wert_und_max_ein_cent_darueber(contracts, price):
    got = kalshi.taker_fee(contracts, price)
    exact = 0.07 * contracts * price * (1 - price)
    assert got >= exact - 1e-9
    assert got < exact + 0.01 + 1e-9
