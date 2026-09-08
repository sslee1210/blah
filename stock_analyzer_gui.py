from __future__ import annotations

"""Small Windows GUI that routes domestic and US stock-analysis commands."""

import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from core.credentials import CredentialError, CredentialStore, KiwoomCredentials


ROOT = Path(__file__).resolve().parent
US_ANALYZER = ROOT / "us_ichimoku_analyzer.py"
DOMESTIC_ANALYZER = ROOT / "domestic_stock_analyzer.py"
CREDENTIAL_PATH = ROOT / ".runtime" / "kiwoom_rest_credentials.dat"


def normalize_individual_command(text: str) -> str:
    """Accept friendly input and ensure the analyzer receives an analysis command."""

    cleaned = re.sub(r"\s+", " ", text.strip())
    if not cleaned:
        raise ValueError("분석할 종목명 또는 종목코드를 입력해 주세요.")
    if "분석" not in cleaned:
        cleaned = f"{cleaned} 분석해줘"
    return cleaned


class StockAnalyzerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Real 통합 주식 분석기")
        self.geometry("980x760")
        self.minsize(860, 680)
        self.configure(bg="#f3f6fb")

        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._running = False
        self._last_report: Path | None = None
        self._process: subprocess.Popen[str] | None = None
        self._process_lock = threading.Lock()
        self._cancelled = False
        self._action_buttons: list[tk.Button] = []
        self._market_entries: list[tk.Entry] = []

        self._build_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_events)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure(
            "Small.TButton",
            font=("Malgun Gothic", 9),
            padding=(11, 7),
        )
        style.configure(
            "Slim.Horizontal.TProgressbar",
            troughcolor="#e8edf5",
            background="#2563eb",
            bordercolor="#e8edf5",
            lightcolor="#2563eb",
            darkcolor="#2563eb",
            thickness=7,
        )

    def _build_ui(self) -> None:
        outer = tk.Frame(self, bg="#f3f6fb")
        outer.pack(fill="both", expand=True, padx=30, pady=26)

        header = tk.Frame(outer, bg="#f3f6fb")
        header.pack(fill="x", pady=(0, 20))
        heading = tk.Frame(header, bg="#f3f6fb")
        heading.pack(side="left", fill="x", expand=True)
        tk.Label(
            heading,
            text="Real 통합 주식 분석기",
            bg="#f3f6fb",
            fg="#0f2744",
            font=("Malgun Gothic", 24, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            heading,
            text="키움 REST API 기반 · 국내 KOSPI/KOSDAQ · 미국 NASDAQ/NYSE/AMEX",
            bg="#f3f6fb",
            fg="#6b7c93",
            font=("Malgun Gothic", 9),
            anchor="w",
        ).pack(fill="x", pady=(4, 0))

        api_badge = tk.Label(
            header,
            text="  API KEY · 암호화 저장  ",
            bg="#e9f7ef",
            fg="#18794e",
            font=("Malgun Gothic", 9, "bold"),
            padx=4,
            pady=7,
        )
        api_badge.pack(side="right", anchor="n", pady=4)

        cards = tk.Frame(outer, bg="#f3f6fb")
        cards.pack(fill="x")
        cards.grid_columnconfigure(0, weight=1, uniform="market")
        cards.grid_columnconfigure(1, weight=1, uniform="market")

        self.domestic_entry = self._market_section(
            cards,
            title="국내 주식",
            subtitle="KOSPI · KOSDAQ",
            placeholder="삼성전자 또는 005930",
            market="domestic",
            badge="KR",
            accent="#2563eb",
            column=0,
        )
        self.us_entry = self._market_section(
            cards,
            title="미국 주식",
            subtitle="NASDAQ · NYSE · AMEX",
            placeholder="AAPL 또는 애플",
            market="us",
            badge="US",
            accent="#6d4aff",
            column=1,
        )

        result_card = tk.Frame(
            outer,
            bg="#ffffff",
            highlightbackground="#dfe6ef",
            highlightthickness=1,
        )
        result_card.pack(fill="both", expand=True, pady=(20, 0))

        status_header = tk.Frame(result_card, bg="#ffffff")
        status_header.pack(fill="x", padx=18, pady=(15, 10))
        title_row = tk.Frame(status_header, bg="#ffffff")
        title_row.pack(side="left", fill="x", expand=True)
        tk.Label(
            title_row,
            text="분석 진행 / 결과",
            bg="#ffffff",
            fg="#172b4d",
            font=("Malgun Gothic", 12, "bold"),
        ).pack(side="left")
        self.status_badge = tk.Label(
            title_row,
            text="대기 중",
            bg="#eef2f7",
            fg="#52667a",
            font=("Malgun Gothic", 8, "bold"),
            padx=10,
            pady=4,
        )
        self.status_badge.pack(side="left", padx=(10, 0))

        self.clear_button = ttk.Button(
            status_header,
            text="로그 지우기",
            style="Small.TButton",
            command=self._clear_log,
        )
        self.clear_button.pack(side="right")
        self.report_button = ttk.Button(
            status_header,
            text="최근 보고서 열기",
            style="Small.TButton",
            state="disabled",
            command=self._open_last_report,
        )
        self.report_button.pack(side="right", padx=(0, 8))
        self.cancel_button = ttk.Button(
            status_header,
            text="분석 취소",
            style="Small.TButton",
            state="disabled",
            command=self._cancel_analysis,
        )
        self.cancel_button.pack(side="right", padx=(0, 8))

        self.status_detail = tk.Label(
            result_card,
            text="종목을 입력하거나 전체 시장 분석을 시작하세요.",
            bg="#ffffff",
            fg="#718096",
            font=("Malgun Gothic", 9),
            anchor="w",
        )
        self.status_detail.pack(fill="x", padx=18, pady=(0, 9))

        self.progress = ttk.Progressbar(
            result_card,
            mode="indeterminate",
            style="Slim.Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x", padx=18, pady=(0, 12))

        log_frame = tk.Frame(
            result_card,
            bg="#f8fafc",
            highlightbackground="#e5eaf1",
            highlightthickness=1,
        )
        log_frame.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        self.log = tk.Text(
            log_frame,
            wrap="word",
            relief="flat",
            bg="#f8fafc",
            fg="#334155",
            font=("Consolas", 10),
            padx=14,
            pady=12,
            borderwidth=0,
        )
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._append_log("준비 완료 · 개별 종목 또는 전체 시장 분석을 선택해 주세요.\n")

    def _market_section(
        self,
        parent: tk.Widget,
        *,
        title: str,
        subtitle: str,
        placeholder: str,
        market: str,
        badge: str,
        accent: str,
        column: int,
    ) -> tk.Entry:
        section = tk.Frame(
            parent,
            bg="#ffffff",
            highlightbackground="#dfe6ef",
            highlightthickness=1,
        )
        section.grid(row=0, column=column, sticky="nsew", padx=(0, 8) if column == 0 else (8, 0))

        top = tk.Frame(section, bg="#ffffff")
        top.pack(fill="x", padx=18, pady=(17, 12))
        badge_label = tk.Label(
            top,
            text=badge,
            bg=accent,
            fg="#ffffff",
            font=("Segoe UI", 9, "bold"),
            width=4,
            pady=5,
        )
        badge_label.pack(side="left", anchor="n")
        title_box = tk.Frame(top, bg="#ffffff")
        title_box.pack(side="left", padx=(10, 0), fill="x", expand=True)
        tk.Label(
            title_box,
            text=title,
            bg="#ffffff",
            fg="#172b4d",
            font=("Malgun Gothic", 14, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            title_box,
            text=subtitle,
            bg="#ffffff",
            fg="#7b8794",
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", pady=(1, 0))

        tk.Label(
            section,
            text="종목명 또는 종목코드",
            bg="#ffffff",
            fg="#52667a",
            font=("Malgun Gothic", 9),
            anchor="w",
        ).pack(fill="x", padx=18, pady=(0, 6))

        entry = tk.Entry(
            section,
            font=("Malgun Gothic", 11),
            relief="solid",
            bd=1,
            highlightthickness=0,
            bg="#ffffff",
            fg="#94a3b8",
            insertbackground="#172b4d",
        )
        entry.pack(fill="x", padx=18, ipady=9)
        entry.insert(0, placeholder)
        self._market_entries.append(entry)

        def focus_in(_: tk.Event) -> None:
            if entry.get() == placeholder:
                entry.delete(0, "end")
                entry.configure(fg="#172b4d")

        def focus_out(_: tk.Event) -> None:
            if not entry.get().strip():
                entry.insert(0, placeholder)
                entry.configure(fg="#94a3b8")

        entry.bind("<FocusIn>", focus_in)
        entry.bind("<FocusOut>", focus_out)
        entry.bind(
            "<Return>",
            lambda _event, m=market, e=entry, p=placeholder: self._run_individual(m, e, p),
        )

        button_row = tk.Frame(section, bg="#ffffff")
        button_row.pack(fill="x", padx=18, pady=(12, 17))
        analyze_button = tk.Button(
            button_row,
            text="종목 분석",
            command=lambda m=market, e=entry, p=placeholder: self._run_individual(m, e, p),
            bg=accent,
            fg="#ffffff",
            activebackground=accent,
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            font=("Malgun Gothic", 9, "bold"),
            padx=16,
            pady=9,
            cursor="hand2",
        )
        analyze_button.pack(side="left", fill="x", expand=True)
        all_button = tk.Button(
            button_row,
            text="전체 시장 분석",
            command=lambda m=market: self._run_all(m),
            bg="#eef2f7",
            fg="#334155",
            activebackground="#e2e8f0",
            activeforeground="#172b4d",
            relief="flat",
            bd=0,
            font=("Malgun Gothic", 9, "bold"),
            padx=14,
            pady=9,
            cursor="hand2",
        )
        all_button.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self._action_buttons.extend((analyze_button, all_button))
        return entry

    def _run_individual(self, market: str, entry: ttk.Entry, placeholder: str) -> None:
        raw = entry.get().strip()
        if raw == placeholder:
            raw = ""
        try:
            command = normalize_individual_command(raw)
        except ValueError as exc:
            messagebox.showinfo("입력 확인", str(exc), parent=self)
            return
        self._start_analysis(market, command)

    def _run_all(self, market: str) -> None:
        command = "전체 분석해줘" if market == "us" else "국내 전체 분석해줘"
        self._start_analysis(market, command)

    def _start_analysis(self, market: str, command: str) -> None:
        if self._running:
            messagebox.showinfo("분석 중", "현재 분석이 끝난 뒤 다시 실행해 주세요.", parent=self)
            return

        if not self._ensure_credentials():
            return

        analyzer = US_ANALYZER if market == "us" else DOMESTIC_ANALYZER
        market_name = "미국" if market == "us" else "국내"
        if not analyzer.exists():
            self._append_log(
                f"\n[{market_name}] {command}\n"
                "국내주식 분석 엔진은 아직 연결 전입니다. GUI 연결부는 준비되어 있으며 "
                "domestic_stock_analyzer.py가 추가되면 같은 방식으로 바로 실행됩니다.\n"
            )
            if market == "domestic":
                messagebox.showinfo(
                    "국내 분석 엔진 준비 중",
                    "화면과 입력 동작은 완성되었습니다. 다음 단계에서 키움 국내주식 REST 엔진을 연결합니다.",
                    parent=self,
                )
            return

        self._running = True
        self._cancelled = False
        self._last_report = None
        self.report_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self._set_running_controls(True)
        self._set_status(
            "분석 중",
            f"{market_name} 시장 · {command}",
            kind="running",
        )
        self.progress.start(12)
        self._append_log(f"\n[{market_name} 분석 시작] {command}\n")

        worker = threading.Thread(
            target=self._run_process,
            args=(analyzer, command, market_name),
            daemon=True,
        )
        worker.start()

    def _run_process(self, analyzer: Path, command: str, market_name: str) -> None:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        try:
            with self._process_lock:
                # Closing/cancelling must also cover the interval before Popen
                # has returned and registered its child process.
                if self._cancelled:
                    self._events.put(("done", (market_name, 1)))
                    return
                process = subprocess.Popen(
                    [sys.executable, "-u", str(analyzer), "--once", command],
                    cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                )
                self._process = process
            assert process.stdout is not None
            for line in process.stdout:
                clean = line.rstrip("\r\n")
                self._events.put(("line", clean))
                report = self._extract_report_path(clean)
                if report is not None:
                    self._events.put(("report", report))
            code = process.wait()
            self._events.put(("done", (market_name, code)))
        except Exception as exc:
            self._terminate_running_process()
            self._events.put(("error", (market_name, str(exc))))
        finally:
            child = locals().get("process")
            if child is not None and child.stdout is not None:
                child.stdout.close()
            with self._process_lock:
                if self._process is child:
                    self._process = None

    @staticmethod
    def _extract_report_path(line: str) -> Path | None:
        for prefix in ("HTML 보고서:", "보고서:"):
            if line.startswith(prefix):
                path_text = line.split(":", 1)[1].strip()
                if not path_text:
                    return None
                candidate = Path(path_text)
                if not candidate.is_absolute():
                    candidate = ROOT / candidate
                candidate = candidate.resolve()
                report_roots = [ROOT / "reports"]
                for variable in ("REAL2_REPORTS_DIR", "REAL_KR_REPORTS_DIR"):
                    configured = os.getenv(variable, "").strip()
                    if configured:
                        folder = Path(configured)
                        report_roots.append(folder if folder.is_absolute() else ROOT / folder)
                if (
                    any(candidate.is_relative_to(folder.resolve()) for folder in report_roots)
                    and candidate.suffix.lower() in {".html", ".md"}
                    and candidate.is_file()
                ):
                    return candidate
        return None

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "line":
                    self._append_log(str(payload) + "\n")
                elif kind == "report":
                    self._last_report = Path(payload)
                    self.report_button.configure(state="normal")
                elif kind == "done":
                    market_name, code = payload  # type: ignore[misc]
                    self._finish_run()
                    if self._cancelled:
                        self._set_status("취소됨", f"{market_name} 분석을 사용자가 취소했습니다.", kind="muted")
                        self._append_log(f"[{market_name} 분석 취소 완료]\n")
                    elif code == 0 and self._last_report is not None and self._last_report.is_file():
                        detail = f"{market_name} 분석이 완료되었습니다. 최근 보고서를 바로 열 수 있습니다."
                        self._set_status("완료", detail, kind="success")
                        self._append_log(f"[{market_name} 분석 완료]\n")
                    elif code == 0:
                        self._set_status(
                            "결과 없음",
                            f"{market_name} 실행은 종료됐지만 이번 분석 보고서를 확인하지 못했습니다. 아래 로그를 확인하세요.",
                            kind="error",
                        )
                        self._append_log(f"[{market_name} 분석 결과 없음] 생성된 보고서를 확인하지 못했습니다.\n")
                    else:
                        self._set_status(
                            "오류",
                            f"{market_name} 분석이 오류 코드 {code}로 종료되었습니다. 아래 로그를 확인하세요.",
                            kind="error",
                        )
                        self._append_log(f"[{market_name} 분석 종료] 오류 코드 {code}\n")
                elif kind == "error":
                    market_name, message = payload  # type: ignore[misc]
                    self._finish_run()
                    self._set_status(
                        "실행 오류",
                        f"{market_name} 분석을 시작하거나 실행하는 중 문제가 발생했습니다.",
                        kind="error",
                    )
                    self._append_log(f"[{market_name} 실행 오류] {message}\n")
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _finish_run(self) -> None:
        self._running = False
        self.progress.stop()
        self.cancel_button.configure(state="disabled")
        self._set_running_controls(False)

    def _set_running_controls(self, running: bool) -> None:
        button_state = "disabled" if running else "normal"
        entry_state = "disabled" if running else "normal"
        for button in self._action_buttons:
            button.configure(state=button_state)
        for entry in self._market_entries:
            entry.configure(state=entry_state)

    def _set_status(self, label: str, detail: str, *, kind: str) -> None:
        palette = {
            "running": ("#e8f1ff", "#1d4ed8"),
            "success": ("#e9f7ef", "#18794e"),
            "error": ("#fff0f0", "#c23b3b"),
            "muted": ("#eef2f7", "#52667a"),
        }
        bg, fg = palette.get(kind, palette["muted"])
        self.status_badge.configure(text=label, bg=bg, fg=fg)
        self.status_detail.configure(text=detail)

    def _ensure_credentials(self) -> bool:
        store = CredentialStore(CREDENTIAL_PATH)
        try:
            if store.load() is not None:
                return True
        except CredentialError as exc:
            messagebox.showwarning(
                "API 키 다시 설정",
                f"저장된 키를 읽지 못했습니다. 새 키로 다시 저장해 주세요.\n\n{exc}",
                parent=self,
            )

        appkey = simpledialog.askstring(
            "키움 REST API 설정",
            "앱키를 입력해 주세요.\n입력값은 Windows DPAPI로 암호화해 저장합니다.",
            show="*",
            parent=self,
        )
        if not appkey:
            return False
        secretkey = simpledialog.askstring(
            "키움 REST API 설정",
            "시크릿키를 입력해 주세요.",
            show="*",
            parent=self,
        )
        if not secretkey:
            return False
        try:
            store.save(KiwoomCredentials(appkey=appkey, secretkey=secretkey))
        except (CredentialError, OSError) as exc:
            messagebox.showerror("API 키 저장 실패", str(exc), parent=self)
            return False
        self._append_log("키움 REST API 키를 암호화해 저장했습니다.\n")
        return True

    def _cancel_analysis(self) -> None:
        if not self._running:
            return
        self._cancelled = True
        self.cancel_button.configure(state="disabled")
        self._set_status("취소 중", "실행 중인 분석 프로세스를 종료하고 있습니다.", kind="muted")
        self._append_log("[취소 요청] 실행 중인 분석을 종료합니다...\n")
        self._terminate_running_process()

    def _terminate_running_process(self) -> None:
        with self._process_lock:
            process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._events.put(("line", "[취소 지연] 분석 프로세스 종료를 기다리고 있습니다."))
        except OSError:
            pass

    def _on_close(self) -> None:
        if self._running:
            close = messagebox.askyesno(
                "분석 종료",
                "분석이 진행 중입니다. 실행 중인 분석도 함께 종료할까요?",
                parent=self,
            )
            if not close:
                return
            self._cancelled = True
            self._terminate_running_process()
        self.destroy()

    def _append_log(self, text: str) -> None:
        self.log.insert("end", text)
        self.log.see("end")

    def _clear_log(self) -> None:
        self.log.delete("1.0", "end")
        self._append_log("로그를 지웠습니다.\n")

    def _open_last_report(self) -> None:
        if self._last_report is None or not self._last_report.exists():
            messagebox.showinfo("보고서", "열 수 있는 최근 보고서가 없습니다.", parent=self)
            return
        try:
            os.startfile(self._last_report)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("보고서 열기 실패", str(exc), parent=self)


def main() -> int:
    app = StockAnalyzerApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
