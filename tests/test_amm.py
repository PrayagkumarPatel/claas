"""tests/test_amm.py — AMM backstop pool math (see amm.py docstring)."""
import pytest

from amm import AMMPool


def test_spot_price_and_invariant():
    # zero fee: the product x*y is EXACTLY conserved by both directions
    pool = AMMPool(gpu_hours=10_000, usd=22_000, fee_bps=0)   # $2.20/GPU-hr seed
    assert pool.spot_price == pytest.approx(2.20)
    k0 = pool.gpu_hours * pool.usd
    pool.execute_buy(100)
    pool.execute_sell(250)
    assert pool.gpu_hours * pool.usd == pytest.approx(k0, rel=1e-9)

    # with fees retained in the pool (Uniswap-style LP economics), k never
    # decreases — it grows by the retained fee on every trade
    pool = AMMPool(gpu_hours=10_000, usd=22_000, fee_bps=30)
    pool.execute_buy(100)
    pool.execute_sell(250)
    assert pool.gpu_hours * pool.usd > k0


def test_buy_pushes_price_up_sell_pushes_down():
    pool = AMMPool(gpu_hours=10_000, usd=22_000)
    p0 = pool.spot_price
    pool.execute_buy(1_000)                          # heavy buy
    assert pool.spot_price > p0
    pool.execute_sell(2_000)                         # heavier sell
    assert pool.spot_price < p0


def test_fee_makes_round_trip_costly():
    """Buy then immediately sell the same hours — you pay the fee band twice."""
    pool = AMMPool(gpu_hours=10_000, usd=22_000, fee_bps=100)   # fat fee, obvious
    paid = pool.execute_buy(100)
    got = pool.execute_sell(100)
    assert got < paid
    # and the pool earned the difference (it's the counterparty of last resort)
    assert pool.usd > 22_000


def test_slippage_grows_with_size():
    pool = AMMPool(gpu_hours=10_000, usd=22_000)
    small = pool.quote_buy(10)
    big = pool.quote_buy(1_000)
    assert big / 1_000 > small / 10                  # avg price worse for size


def test_quotes_do_not_mutate():
    pool = AMMPool(gpu_hours=10_000, usd=22_000)
    before = (pool.gpu_hours, pool.usd)
    pool.quote_buy(500)
    pool.quote_sell(500)
    assert (pool.gpu_hours, pool.usd) == before


def test_draining_the_pool_is_rejected():
    pool = AMMPool(gpu_hours=100, usd=200)
    with pytest.raises(ValueError):
        pool.quote_buy(100)                          # equal to reserves
    with pytest.raises(ValueError):
        pool.quote_buy(0)
    pool.quote_buy(99.9)                             # just under is fine


def test_bad_inputs_rejected():
    with pytest.raises(ValueError):
        AMMPool(gpu_hours=0, usd=100)
    with pytest.raises(ValueError):
        AMMPool(gpu_hours=100, usd=100, fee_bps=10_000)
