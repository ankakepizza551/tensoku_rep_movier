"""
指定したプロセスが鳴らしている音だけを録音する (プロセス ループバック)。

Windows 10 バージョン 2004 以降の機能。既定の再生デバイスを切り替えたり VB-Cable を
入れたりしなくても、ゲームの音だけを録れる。録音中もスピーカーから普通に音が聞こえる。
COM のインターフェースを ctypes で直接呼んでいる。
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
import wave
from ctypes import wintypes
from pathlib import Path
from typing import Callable

_MIN_BUILD = 19041   # Windows 10 バージョン 2004


def is_supported() -> bool:
    """この Windows でプロセス単位の録音が使えるか。"""
    try:
        return sys.getwindowsversion().build >= _MIN_BUILD
    except Exception:
        return False


# ── COM の定義 ────────────────────────────────────────────────
class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> _GUID:
    g = _GUID()
    ctypes.windll.ole32.CLSIDFromString(s, ctypes.byref(g))
    return g


_IID_IUnknown            = _guid("{00000000-0000-0000-C000-000000000046}")
_IID_IAgileObject        = _guid("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
_IID_CompletionHandler   = _guid("{41D949AB-9862-444A-80F6-C261334DA5EB}")
_IID_IAudioClient        = _guid("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
_IID_IAudioCaptureClient = _guid("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")

_VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
_VT_BLOB = 65

_AUDCLNT_SHAREMODE_SHARED = 0
_AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
_AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
_AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
_AUDCLNT_BUFFERFLAGS_SILENT = 0x2
_AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR = 0x4

_E_NOINTERFACE = -2147467262   # 0x80004002

CHANNELS = 2
BYTES_PER_FRAME = CHANNELS * 2   # 16 ビット


class _WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
                ("nSamplesPerSec", ctypes.c_ulong), ("nAvgBytesPerSec", ctypes.c_ulong),
                ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
                ("cbSize", ctypes.c_ushort)]


class _ActivationParams(ctypes.Structure):
    """AUDIOCLIENT_ACTIVATION_PARAMS (プロセス ループバック用)"""
    _fields_ = [("ActivationType", ctypes.c_ulong),
                ("TargetProcessId", ctypes.c_ulong),
                ("ProcessLoopbackMode", ctypes.c_ulong)]


class _PropVariantBlob(ctypes.Structure):
    """PROPVARIANT (VT_BLOB)"""
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("cbSize", ctypes.c_ulong), ("pBlobData", ctypes.c_void_p)]


_QueryInterface = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                     ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
_AddRefRelease = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_ActivateCompleted = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)


class _HandlerVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", _QueryInterface), ("AddRef", _AddRefRelease),
                ("Release", _AddRefRelease), ("ActivateCompleted", _ActivateCompleted)]


class _Handler(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_HandlerVtbl))]


def _method(obj: ctypes.c_void_p, index: int, *argtypes):
    """COM オブジェクトの vtable から index 番目のメソッドを取り出す (戻り値は HRESULT)。"""
    vtbl = ctypes.cast(
        ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0],
        ctypes.POINTER(ctypes.c_void_p),
    )
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])


def _release(obj: ctypes.c_void_p) -> None:
    if obj and obj.value:
        _method(obj, 2)(obj)


def _same_guid(a: _GUID, b: _GUID) -> bool:
    return bytes(a) == bytes(b)


class ProcessLoopbackCapture:
    """pid のプロセス (とその子プロセス) が鳴らす音を wav_path に録音する。"""

    def __init__(self, pid: int, wav_path: Path, log: Callable = print):
        self.pid = pid
        self.wav_path = Path(wav_path)
        self.log = log
        self.sample_rate = 48000
        self.start_time: float = 0.0     # 録音の先頭にあたる時刻 (time.time())
        self.frames_written = 0
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._ok = False
        self._error = ""
        self._thread: threading.Thread | None = None

    def start(self, timeout: float = 5.0) -> bool:
        """録音を開始する。開始できたら True。"""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        if not self._ok:
            self._stop.set()
            self.log(f"[警告] プロセス単位の録音を開始できませんでした ({self._error or '応答なし'})")
        return self._ok

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    # ── 録音スレッド ──────────────────────────────────────────
    def _run(self) -> None:
        ole32 = ctypes.windll.ole32
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoInitializeEx(None, 0)   # COINIT_MULTITHREADED

        client = ctypes.c_void_p()
        capture = ctypes.c_void_p()
        event = None
        wf = None
        try:
            client = self._activate()
            fmt = self._initialize(client)
            self.sample_rate = fmt.nSamplesPerSec

            hr = _method(client, 14, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
                client, ctypes.byref(_IID_IAudioCaptureClient), ctypes.byref(capture))   # GetService
            if hr != 0 or not capture.value:
                raise OSError(f"GetService 0x{hr & 0xFFFFFFFF:08X}")
            event = kernel32.CreateEventW(None, False, False, None)
            hr = _method(client, 13, wintypes.HANDLE)(client, event)                     # SetEventHandle
            if hr != 0:
                raise OSError(f"SetEventHandle 0x{hr & 0xFFFFFFFF:08X}")

            wf = wave.open(str(self.wav_path), "wb")
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)

            hr = _method(client, 10)(client)                                             # Start
            if hr != 0:
                raise OSError(f"Start 0x{hr & 0xFFFFFFFF:08X}")
            started_at = time.time()
            self._ok = True
            self._ready.set()

            self._capture_loop(capture, event, wf, started_at)
            _method(client, 11)(client)                                                  # Stop
        except Exception as e:
            self._error = str(e)
            self._ready.set()
        finally:
            if wf:
                try:
                    wf.close()
                except Exception:
                    pass
            _release(capture)
            _release(client)
            if event:
                kernel32.CloseHandle(event)
            ole32.CoUninitialize()

    def _activate(self) -> ctypes.c_void_p:
        """対象プロセス専用の IAudioClient を取得する。"""
        done = threading.Event()

        def _qi(this, riid, ppv):
            iid = riid.contents
            if any(_same_guid(iid, g) for g in (_IID_IUnknown, _IID_IAgileObject, _IID_CompletionHandler)):
                ppv[0] = this
                return 0
            ppv[0] = None
            return _E_NOINTERFACE

        def _completed(this, operation):
            done.set()
            return 0

        # コールバックと vtable は、呼ばれ終わるまで参照を持ち続ける必要がある
        vtbl = _HandlerVtbl(_QueryInterface(_qi), _AddRefRelease(lambda this: 1),
                            _AddRefRelease(lambda this: 1), _ActivateCompleted(_completed))
        handler = _Handler(ctypes.pointer(vtbl))

        params = _ActivationParams(_ACTIVATION_TYPE_PROCESS_LOOPBACK, self.pid,
                                   _LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE)
        variant = _PropVariantBlob(vt=_VT_BLOB, cbSize=ctypes.sizeof(params),
                                   pBlobData=ctypes.cast(ctypes.pointer(params), ctypes.c_void_p))

        activate = ctypes.windll.Mmdevapi.ActivateAudioInterfaceAsync
        activate.restype = ctypes.c_long
        activate.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(_GUID), ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        operation = ctypes.c_void_p()
        hr = activate(_VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, ctypes.byref(_IID_IAudioClient),
                      ctypes.byref(variant), ctypes.byref(handler), ctypes.byref(operation))
        if hr != 0:
            raise OSError(f"ActivateAudioInterfaceAsync 0x{hr & 0xFFFFFFFF:08X}")
        try:
            if not done.wait(5.0):
                raise OSError("有効化が完了しませんでした")
            result = ctypes.c_long(0)
            unknown = ctypes.c_void_p()
            hr = _method(operation, 3, ctypes.POINTER(ctypes.c_long), ctypes.POINTER(ctypes.c_void_p))(
                operation, ctypes.byref(result), ctypes.byref(unknown))                  # GetActivateResult
            if hr != 0 or result.value != 0 or not unknown.value:
                code = hr if hr != 0 else result.value
                raise OSError(f"有効化に失敗 0x{code & 0xFFFFFFFF:08X}")
            client = ctypes.c_void_p()
            hr = _method(unknown, 0, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
                unknown, ctypes.byref(_IID_IAudioClient), ctypes.byref(client))          # QueryInterface
            _release(unknown)
            if hr != 0 or not client.value:
                raise OSError(f"IAudioClient 0x{hr & 0xFFFFFFFF:08X}")
            return client
        finally:
            _release(operation)

    def _initialize(self, client: ctypes.c_void_p) -> _WAVEFORMATEX:
        """録音形式を決めて初期化する (プロセス ループバックでは形式をこちらから指定する)。"""
        init = _method(client, 3, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_longlong,
                       ctypes.c_longlong, ctypes.POINTER(_WAVEFORMATEX), ctypes.c_void_p)
        flags = (_AUDCLNT_STREAMFLAGS_LOOPBACK | _AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                 | _AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM)
        hr = 0
        for rate in (48000, 44100):
            fmt = _WAVEFORMATEX(1, CHANNELS, rate, rate * BYTES_PER_FRAME, BYTES_PER_FRAME, 16, 0)
            hr = init(client, _AUDCLNT_SHAREMODE_SHARED, flags, 200000, 0, ctypes.byref(fmt), None)
            if hr == 0:
                return fmt
        raise OSError(f"Initialize 0x{hr & 0xFFFFFFFF:08X}")

    def _capture_loop(self, capture, event, wf, started_at: float) -> None:
        kernel32 = ctypes.windll.kernel32
        next_size = _method(capture, 5, ctypes.POINTER(ctypes.c_uint))
        get_buffer = _method(capture, 3, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint),
                             ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulonglong),
                             ctypes.POINTER(ctypes.c_ulonglong))
        release_buffer = _method(capture, 4, ctypes.c_uint)
        rate = self.sample_rate
        qpc_first: int | None = None   # 最初のデータの時刻 (100 ナノ秒単位)

        def write_silence(frames: int) -> None:
            if frames > 0:
                wf.writeframes(b"\x00" * (frames * BYTES_PER_FRAME))
                self.frames_written += frames

        while not self._stop.is_set():
            kernel32.WaitForSingleObject(event, 100)
            while True:
                size = ctypes.c_uint(0)
                if next_size(capture, ctypes.byref(size)) != 0 or size.value == 0:
                    break
                data = ctypes.c_void_p()
                frames = ctypes.c_uint(0)
                flags = ctypes.c_ulong(0)
                position = ctypes.c_ulonglong(0)
                qpc = ctypes.c_ulonglong(0)
                if get_buffer(capture, ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags),
                              ctypes.byref(position), ctypes.byref(qpc)) != 0:
                    break
                n = frames.value
                timestamp_ok = qpc.value and not (flags.value & _AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR)
                if qpc_first is None:
                    # 録音の先頭の時刻。データに付いている時刻は time.perf_counter() と同じ時計
                    # なので、そこから現在時刻との差を出して time.time() の値に直す。
                    self.start_time = time.time() - n / rate
                    if timestamp_ok:
                        qpc_first = qpc.value
                        age = time.perf_counter() - qpc.value / 1e7
                        if 0 <= age < 2.0:
                            self.start_time = time.time() - age
                elif timestamp_ok:
                    # 対象が無音の間はデータが届かない。詰めて書くと以降の音が前にずれるので、
                    # 届かなかった時間のぶん無音を挟む。
                    expected = round((qpc.value - qpc_first) / 1e7 * rate)
                    if expected - self.frames_written > rate * 0.03:
                        write_silence(expected - self.frames_written)

                if flags.value & _AUDCLNT_BUFFERFLAGS_SILENT or not data.value:
                    write_silence(n)
                else:
                    wf.writeframes(ctypes.string_at(data.value, n * BYTES_PER_FRAME))
                    self.frames_written += n
                release_buffer(capture, n)

        # 最後が無音のまま終わった場合、その分を足して映像と同じ長さにする
        if qpc_first is not None:
            write_silence(round((time.time() - self.start_time) * rate) - self.frames_written)
        else:
            self.start_time = started_at
