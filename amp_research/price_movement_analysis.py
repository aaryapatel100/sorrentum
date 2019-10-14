import logging
from typing import Dict, Iterable

import pandas as pd

import core.signal_processing as sigp
import helpers.dbg as dbg
import vendors.kibot.utils as kut

_LOG = logging.getLogger(__name__)


TAU = 18


def get_zscored_returns(
    price_df_dict: Dict[str, pd.DataFrame],
    symbol: str,
    period: str,
    tau: int = TAU,
    demean: bool = False,
):
    dbg.dassert_in(period, ["daily", "minutely"])
    prices_symbol = price_df_dict[symbol]
    if period == "minutely":
        rets = kut.compute_ret_0_from_1min_prices(prices_symbol, "pct_change")
    else:
        rets = kut.compute_ret_0_from_daily_prices(
            prices_symbol, "open", "pct_change"
        )
    zscored_rets = sigp.rolling_zscore(rets, tau, demean=demean)
    _LOG.debug("zscored_rets=\n%s", zscored_rets.head())
    abs_zscored_rets = zscored_rets.abs()
    return abs_zscored_rets


def get_top_movements_by_group(
    price_df_dict: Dict[str, pd.DataFrame],
    commodity_symbols_kibot: Dict[str, Iterable],
    group: str,
    period: str,
    n_movements: int = 100,
):
    zscored_returns = []
    for symbol in commodity_symbols_kibot[group]:
        zscored_ret = get_zscored_returns(price_df_dict, symbol, period)
        zscored_returns.append(zscored_ret)
    zscored_returns = pd.concat(zscored_returns, axis=1)
    mean_zscored_rets = zscored_returns.mean(axis=1, skipna=True)
    return mean_zscored_rets.sort_values(ascending=False).head(n_movements)


def get_top_movements_for_symbol(
    price_df_dict: Dict[str, pd.DataFrame],
    symbol: str,
    period: str,
    n_movements: int = 100,
):
    zscored_rets = get_zscored_returns(price_df_dict, symbol, period)
    return zscored_rets.sort_values(ascending=False).head(n_movements)
