from __future__ import annotations

"""Windows-DPAPI credential storage for Kiwoom REST API credentials."""

import base64
import ctypes
import getpass
import json
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


class CredentialError(RuntimeError):
    """Raised when Kiwoom REST credentials cannot be loaded safely."""


@dataclass(frozen=True)
class KiwoomCredentials:
    appkey: str
    secretkey: str


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialError("암호 저장은 Windows에서만 지원합니다.")
    in_blob, keepalive = _blob(data)
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "Real2 Kiwoom REST" ,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise CredentialError("Windows 자격 증명 암호화에 실패했습니다.")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
        del keepalive


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialError("암호 저장은 Windows에서만 지원합니다.")
    in_blob, keepalive = _blob(data)
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise CredentialError("저장된 키움 자격 증명을 복호화하지 못했습니다.")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
        del keepalive


class CredentialStore:
    """Load credentials from environment or an encrypted per-user file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> KiwoomCredentials | None:
        appkey = os.getenv("KIWOOM_APPKEY", "").strip()
        secretkey = os.getenv("KIWOOM_SECRETKEY", "").strip()
        if appkey and secretkey:
            return KiwoomCredentials(appkey, secretkey)
        if not self.path.exists():
            return None
        try:
            encrypted = base64.b64decode(self.path.read_bytes(), validate=True)
            payload = json.loads(_unprotect(encrypted).decode("utf-8"))
            appkey = str(payload.get("appkey", "")).strip()
            secretkey = str(payload.get("secretkey", "")).strip()
        except Exception as exc:
            raise CredentialError(
                "저장된 키를 읽지 못했습니다. --configure로 다시 설정해 주세요."
            ) from exc
        if not appkey or not secretkey:
            return None
        return KiwoomCredentials(appkey, secretkey)

    def save(self, credentials: KiwoomCredentials) -> None:
        if not credentials.appkey.strip() or not credentials.secretkey.strip():
            raise CredentialError("앱키와 시크릿키를 모두 입력해야 합니다.")
        payload = json.dumps(
            {"appkey": credentials.appkey.strip(), "secretkey": credentials.secretkey.strip()},
            ensure_ascii=False,
        ).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(base64.b64encode(_protect(payload)))

    def configure_interactively(self) -> KiwoomCredentials:
        print("\n키움 REST API 앱키와 시크릿키가 필요합니다.")
        print("입력값은 현재 Windows 사용자만 풀 수 있도록 암호화해 저장합니다.")
        appkey = getpass.getpass("앱키 (화면에 표시되지 않음): ").strip()
        secretkey = getpass.getpass("시크릿키 (화면에 표시되지 않음): ").strip()
        credentials = KiwoomCredentials(appkey=appkey, secretkey=secretkey)
        self.save(credentials)
        return credentials

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()

