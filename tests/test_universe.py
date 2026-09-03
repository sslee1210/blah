from __future__ import annotations

from core.kiwoom_rest import StockInfo
from core.universe import build_scan_universe, is_supported_common_stock


class FakeClient:
    def ranking(self, api_id: str, *, max_rows: int = 250):
        if api_id == "usa20550":
            return [
                {"stex_tp": "ND", "stk_cd": "AAPL", "stk_nm": "애플", "stk_enm": "APPLE INC", "cur_prc": "200", "mac": "3000000"},
                {"stex_tp": "ND", "stk_cd": "QQQ", "stk_nm": "QQQ", "stk_enm": "INVESCO QQQ ETF", "cur_prc": "500", "mac": "300000"},
                {"stex_tp": "NY", "stk_cd": "CHEAP", "stk_nm": "저가", "stk_enm": "CHEAP INC", "cur_prc": "2", "mac": "10000"},
            ]
        return [
            {"stex_tp": "ND", "stk_cd": "AAPL", "stk_nm": "애플", "stk_enm": "APPLE INC", "cur_prc": "200", "trde_prica": "400000"},
            {"stex_tp": "NY", "stk_cd": "ABC.WS", "stk_nm": "ABC 워런트", "stk_enm": "ABC WARRANT", "cur_prc": "10", "trde_prica": "50000"},
        ]


def test_security_filter_excludes_non_common_instruments() -> None:
    assert is_supported_common_stock(StockInfo("AAPL", "ND", "애플", "APPLE INC", "기술", False))
    assert not is_supported_common_stock(StockInfo("QQQ", "ND", "", "INVESCO QQQ ETF", "", True))
    assert not is_supported_common_stock(StockInfo("ABC.WS", "NY", "", "ABC WARRANT", "", False))


def test_universe_blends_ranks_and_excludes_etf_and_low_price() -> None:
    master = [
        StockInfo("AAPL", "ND", "애플", "APPLE INC", "기술", False),
        StockInfo("QQQ", "ND", "", "INVESCO QQQ ETF", "ETF", True),
        StockInfo("CHEAP", "NY", "저가", "CHEAP INC", "기타", False),
        StockInfo("ABC.WS", "NY", "", "ABC WARRANT", "기타", False),
    ]
    selected, stats = build_scan_universe(FakeClient(), master, target_size=50)
    assert [item.stock.symbol for item in selected] == ["AAPL"]
    assert stats["excluded_non_common_or_low_price"] == 3

