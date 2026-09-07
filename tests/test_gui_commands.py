from __future__ import annotations

import pytest
import queue
import threading
from unittest.mock import Mock

import stock_analyzer_gui as gui
from stock_analyzer_gui import StockAnalyzerApp, normalize_individual_command


def test_gui_preserves_user_analysis_phrase() -> None:
    assert normalize_individual_command("AAPL 분석해줘") == "AAPL 분석해줘"
    assert normalize_individual_command("삼성전자 분석해줘") == "삼성전자 분석해줘"


def test_gui_accepts_plain_symbol_as_a_convenience() -> None:
    assert normalize_individual_command("005930") == "005930 분석해줘"
    assert normalize_individual_command("NVDA") == "NVDA 분석해줘"


def test_gui_rejects_empty_individual_input() -> None:
    with pytest.raises(ValueError):
        normalize_individual_command("   ")


def test_gui_resolves_relative_report_path_from_app_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gui, "ROOT", tmp_path)
    report = tmp_path / "reports" / "_gui_path_test.html"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("ok", encoding="utf-8")
    assert StockAnalyzerApp._extract_report_path("HTML 보고서: reports/_gui_path_test.html") == report


def _headless_app():
    app = object.__new__(StockAnalyzerApp)
    app._running = False
    app._cancelled = False
    app._process = None
    app._process_lock = threading.Lock()
    app._events = queue.Queue()
    app._last_report = None
    app.report_button = Mock()
    app.cancel_button = Mock()
    app.progress = Mock()
    app._set_running_controls = Mock()
    app._set_status = Mock()
    app._append_log = Mock()
    app._ensure_credentials = Mock(return_value=True)
    app.after = Mock()
    return app


def test_gui_does_not_reuse_previous_report_for_an_empty_run(tmp_path, monkeypatch) -> None:
    old_report = tmp_path / "old.html"
    old_report.write_text("previous run", encoding="utf-8")
    app = _headless_app()
    app._last_report = old_report
    monkeypatch.setattr(gui.threading, "Thread", Mock())

    app._start_analysis("us", "AAPL 분석해줘")
    assert app._last_report is None
    app.report_button.configure.assert_called_with(state="disabled")

    app._events.put(("done", ("미국", 0)))
    app._drain_events()
    assert app._set_status.call_args.args[0] == "결과 없음"
    assert app._set_status.call_args.kwargs["kind"] == "error"
    assert not app._running


def test_gui_ignores_report_directories_and_non_report_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gui, "ROOT", tmp_path)
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "program.exe").write_text("not a report", encoding="utf-8")
    (tmp_path / "outside.html").write_text("not an analyzer report", encoding="utf-8")
    for path in ("", "reports", "reports/program.exe", "reports/../outside.html"):
        assert StockAnalyzerApp._extract_report_path(f"HTML 보고서: {path}") is None


def test_gui_cancel_before_worker_start_does_not_launch_child(monkeypatch) -> None:
    app = _headless_app()
    app._cancelled = True
    popen = Mock(side_effect=AssertionError("cancelled analysis must not start"))
    monkeypatch.setattr(gui.subprocess, "Popen", popen)

    app._run_process(gui.US_ANALYZER, "AAPL 분석해줘", "미국")

    popen.assert_not_called()
    assert app._events.get_nowait()[0] == "done"
    assert app._process is None


def test_gui_cancel_during_process_creation_terminates_the_child(monkeypatch) -> None:
    app = _headless_app()
    app._running = True
    creating = threading.Event()
    release_creation = threading.Event()
    cancel_requested = threading.Event()
    stopped = threading.Event()

    class Output:
        def __iter__(self):
            assert stopped.wait(3), "child was left running after cancellation"
            return iter(())

        def close(self):
            pass

    process = Mock()
    process.stdout = Output()
    process.poll.side_effect = lambda: 1 if stopped.is_set() else None
    process.terminate.side_effect = stopped.set
    process.wait.return_value = 1

    def create_process(*args, **kwargs):
        creating.set()
        assert release_creation.wait(3)
        return process

    monkeypatch.setattr(gui.subprocess, "Popen", create_process)
    app._append_log.side_effect = lambda _: cancel_requested.set()
    worker = threading.Thread(target=app._run_process, args=(gui.US_ANALYZER, "AAPL 분석해줘", "미국"))
    canceller = threading.Thread(target=app._cancel_analysis)
    worker.start()
    try:
        assert creating.wait(3)
        canceller.start()
        assert cancel_requested.wait(3)
    finally:
        release_creation.set()
        worker.join(5)
        if canceller.ident is not None:
            canceller.join(5)

    assert not worker.is_alive()
    assert not canceller.is_alive()
    process.terminate.assert_called_once()
    assert stopped.is_set()
    assert app._process is None


def test_gui_handles_credential_file_write_error(monkeypatch) -> None:
    app = _headless_app()
    store = Mock()
    store.load.return_value = None
    store.save.side_effect = OSError("write unavailable")
    monkeypatch.setattr(gui, "CredentialStore", Mock(return_value=store))
    monkeypatch.setattr(gui.simpledialog, "askstring", Mock(return_value="test-only"))
    showerror = Mock()
    monkeypatch.setattr(gui.messagebox, "showerror", showerror)

    assert not StockAnalyzerApp._ensure_credentials(app)
    showerror.assert_called_once()
