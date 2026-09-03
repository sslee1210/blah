from __future__ import annotations

from core.credentials import KiwoomCredentials
from core.kiwoom_rest import KiwoomRestClient, _daily_frame, _minute_frame, number


def test_signed_kiwoom_prices_are_treated_as_absolute_prices() -> None:
    assert number("-201.4500", absolute=True) == 201.45
    assert number("+1,234.5000", absolute=True) == 1234.5
    assert number("", absolute=True) is None


def test_daily_response_is_sorted_and_deduplicated() -> None:
    rows = [
        {"dt": "20260103", "open_pric": "-10", "high_pric": "11", "low_pric": "9", "cur_prc": "10.5", "acc_trde_qty": "100"},
        {"dt": "20260102", "open_pric": "9", "high_pric": "10", "low_pric": "8", "cur_prc": "9.5", "acc_trde_qty": "90"},
        {"dt": "20260103", "open_pric": "10", "high_pric": "11", "low_pric": "9", "cur_prc": "10.5", "acc_trde_qty": "100"},
    ]
    frame = _daily_frame(rows)
    assert list(frame.index.strftime("%Y%m%d")) == ["20260102", "20260103"]
    assert frame.iloc[-1]["open"] == 10
    assert frame.iloc[-1]["trade_value"] == 1050


def test_minute_response_uses_new_york_timezone() -> None:
    rows = [
        {"cntr_tm": "20260102103000", "open_pric": "10", "high_pric": "11", "low_pric": "9", "cur_prc": "10.5", "trde_qty": "100"}
    ]
    frame = _minute_frame(rows)
    assert len(frame) == 1
    assert str(frame.index.tz) == "America/New_York"


def test_paging_passes_server_continuation_key(monkeypatch) -> None:
    client = KiwoomRestClient(KiwoomCredentials("app", "secret"))
    calls = []

    def fake_request(api_id, path, body, *, cont_yn="N", next_key=""):
        calls.append((cont_yn, next_key))
        if len(calls) == 1:
            return {"result_list": [{"id": 1}], "return_code": 0}, {"cont-yn": "Y", "next-key": "NEXT"}
        return {"result_list": [{"id": 2}], "return_code": 0}, {"cont-yn": "N", "next-key": ""}

    monkeypatch.setattr(client, "request", fake_request)
    rows = client.paged("usa06012", "/api/us/chart", {}, list_key="result_list")
    assert rows == [{"id": 1}, {"id": 2}]
    assert calls == [("N", ""), ("Y", "NEXT")]
