"""
ゲーム音専用録音のためのデバイスルーティング。
未公開 COM インターフェース IPolicyConfig で既定再生デバイスを一時切り替え。
VB-Cable (https://vb-audio.com/Cable/) インストール済みが前提。
"""
from __future__ import annotations
import ctypes
import struct

_ole32 = ctypes.windll.ole32
_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
_ole32.CoInitializeEx.restype  = ctypes.HRESULT

# COINIT_MULTITHREADED=0 にしないと他スレッドの COM 初期化と競合して
# CoCreateInstance が壊れたオブジェクトを返す場合がある
_COINIT_MULTITHREADED = 0
# CoInitializeEx の成功・既初期化コード
_COM_INIT_OK = {0, 1, -2147417850}   # S_OK, S_FALSE, RPC_E_CHANGED_MODE

def _com_init() -> None:
    try:
        _ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
    except OSError:
        # このスレッドは別のモードで初期化済み (GUI スレッド等)。そのまま使えるので問題ない
        pass


# ── GUID / COM ヘルパー ────────────────────────────────────────────
class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

def _guid(s: str) -> _GUID:
    g = _GUID()
    _ole32.CLSIDFromString(s, ctypes.byref(g))
    return g

_CLSID_MMDeviceEnum    = _guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_IID_IMMDeviceEnum     = _guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
# PolicyConfig: Win10/11 用を先に試し、失敗時は Vista 互換版にフォールバック
# (CLSID, IID, SetDefaultEndpoint の vtable 位置)。
# vtable 位置はインターフェースごとに違う: IPolicyConfig は IUnknown の 3 つの後に
# GetMixFormat ... SetPropertyValue の 10 メソッドが並び SetDefaultEndpoint は 13 番目、
# IPolicyConfigVista は ResetDeviceFormat が無いぶん 1 つ手前の 12 番目。
_POLICY_PAIRS = [
    (_guid("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}"),
     _guid("{F8679F50-850A-41CF-9C72-430F290290C8}"), 13),   # Win10+
    (_guid("{294935CE-F637-4E7C-A41B-AB255460B862}"),
     _guid("{568b9108-44bf-40b4-9006-86afe5b5a620}"), 12),   # Vista 互換 (Win7-11 でも動作)
]
_IID_IPropertyStore = _guid("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")

class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]

_PKEY_FriendlyName = _PROPERTYKEY(
    fmtid=_guid("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), pid=14
)

class _PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("data", ctypes.c_uint8 * 16)]

    def as_str(self) -> str | None:
        if self.vt not in (8, 31):          # VT_BSTR=8, VT_LPWSTR=31
            return None
        ptr = struct.unpack_from("<Q", bytes(self.data))[0]
        return ctypes.wstring_at(ptr) if ptr else None


def _vtbl(obj: ctypes.c_void_p, idx: int, restype, *argtypes):
    vt = ctypes.cast(
        ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0],
        ctypes.POINTER(ctypes.c_void_p),
    )
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vt[idx])


def _release(obj: ctypes.c_void_p) -> None:
    if obj and obj.value:
        _vtbl(obj, 2, ctypes.c_ulong)(obj)


def _create(clsid: _GUID, iid: _GUID) -> ctypes.c_void_p | None:
    _com_init()
    obj = ctypes.c_void_p()
    hr = _ole32.CoCreateInstance(
        ctypes.byref(clsid), None, 0xF,
        ctypes.byref(iid), ctypes.byref(obj),
    )
    return obj if hr == 0 and obj.value else None


# ── 再生デバイス列挙 ──────────────────────────────────────────────
def get_render_devices() -> dict[str, str]:
    """アクティブな再生デバイスの {フレンドリー名: デバイスID} を返す。"""
    result: dict[str, str] = {}
    enum = _create(_CLSID_MMDeviceEnum, _IID_IMMDeviceEnum)
    if not enum:
        return result
    try:
        # EnumAudioEndpoints(eRender=0, ACTIVE=1)
        EnumEP = _vtbl(enum, 3, ctypes.HRESULT,
                       ctypes.c_uint, ctypes.c_uint,
                       ctypes.POINTER(ctypes.c_void_p))
        col = ctypes.c_void_p()
        if EnumEP(enum, 0, 1, ctypes.byref(col)) != 0 or not col.value:
            return result
        try:
            GetCount = _vtbl(col, 3, ctypes.HRESULT,
                             ctypes.POINTER(ctypes.c_uint))
            Item = _vtbl(col, 4, ctypes.HRESULT,
                         ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))
            n = ctypes.c_uint(0)
            GetCount(col, ctypes.byref(n))
            for i in range(n.value):
                dev = ctypes.c_void_p()
                if Item(col, i, ctypes.byref(dev)) != 0 or not dev.value:
                    continue
                try:
                    # GetId (vtable[5])
                    GetId = _vtbl(dev, 5, ctypes.HRESULT,
                                  ctypes.POINTER(ctypes.c_wchar_p))
                    raw_id = ctypes.c_wchar_p()
                    if GetId(dev, ctypes.byref(raw_id)) != 0 or not raw_id.value:
                        continue
                    dev_id = raw_id.value
                    _ole32.CoTaskMemFree(raw_id)

                    # OpenPropertyStore (vtable[4])
                    OpenPS = _vtbl(dev, 4, ctypes.HRESULT,
                                   ctypes.c_uint,
                                   ctypes.POINTER(ctypes.c_void_p))
                    store = ctypes.c_void_p()
                    if OpenPS(dev, 0, ctypes.byref(store)) != 0 or not store.value:
                        continue
                    try:
                        # IPropertyStore::GetValue (vtable[5])
                        GetValue = _vtbl(store, 5, ctypes.HRESULT,
                                         ctypes.POINTER(_PROPERTYKEY),
                                         ctypes.POINTER(_PROPVARIANT))
                        pv = _PROPVARIANT()
                        if GetValue(store, ctypes.byref(_PKEY_FriendlyName),
                                    ctypes.byref(pv)) == 0:
                            name = pv.as_str()
                            if name:
                                result[name] = dev_id
                            _ole32.PropVariantClear(ctypes.byref(pv))
                    finally:
                        _release(store)
                finally:
                    _release(dev)
        finally:
            _release(col)
    finally:
        _release(enum)
    return result


def get_default_playback_device_id() -> str | None:
    """現在の既定再生デバイス ID を返す。"""
    enum = _create(_CLSID_MMDeviceEnum, _IID_IMMDeviceEnum)
    if not enum:
        return None
    try:
        # GetDefaultAudioEndpoint(eRender=0, eConsole=0)
        GetDefault = _vtbl(enum, 4, ctypes.HRESULT,
                           ctypes.c_uint, ctypes.c_uint,
                           ctypes.POINTER(ctypes.c_void_p))
        dev = ctypes.c_void_p()
        if GetDefault(enum, 0, 0, ctypes.byref(dev)) != 0 or not dev.value:
            return None
        try:
            GetId = _vtbl(dev, 5, ctypes.HRESULT,
                          ctypes.POINTER(ctypes.c_wchar_p))
            raw_id = ctypes.c_wchar_p()
            if GetId(dev, ctypes.byref(raw_id)) == 0 and raw_id.value:
                result = raw_id.value
                _ole32.CoTaskMemFree(raw_id)
                return result
        finally:
            _release(dev)
    finally:
        _release(enum)
    return None


def set_default_playback_device(device_id: str) -> bool:
    """既定再生デバイスを変更する (Console/Multimedia/Communications の全ロール)。"""
    for clsid, iid, index in _POLICY_PAIRS:
        pc = _create(clsid, iid)
        if not pc:
            continue
        try:
            SetDefault = _vtbl(pc, index, ctypes.HRESULT,
                               ctypes.c_wchar_p, ctypes.c_uint)
            ok = all(SetDefault(pc, device_id, role) == 0 for role in (0, 1, 2))
            if ok:
                return True
        except Exception:
            continue
        finally:
            _release(pc)
    return False


# ── VB-Cable 検出 ──────────────────────────────────────────────────
_VBCABLE_KEYWORDS = ("VB-Audio Virtual Cable", "CABLE Input", "CABLE Output")

def find_vbcable() -> tuple[str, str] | None:
    """
    VB-Cable の再生デバイスを (フレンドリー名, デバイスID) で返す。
    見つからなければ None。
    "CABLE Output" を優先して返す（ループバック録音の対象になる側）。
    """
    try:
        devices = get_render_devices()
        # "CABLE Output" 優先
        for name, dev_id in devices.items():
            if "CABLE Output" in name:
                return (name, dev_id)
        # フォールバック: CABLE Input や VB-Audio 全般
        for name, dev_id in devices.items():
            if any(kw in name for kw in _VBCABLE_KEYWORDS):
                return (name, dev_id)
    except Exception:
        pass
    return None


def find_vbcable_loopback_name(render_name: str | None = None) -> str | None:
    """
    pyaudiowpatch のループバック一覧に現れる VB-Cable のデバイス名を返す。
    recorder.WASAPI_PREFIX が付いた形式。
    render_name (find_vbcable が返した再生デバイス名) を渡すと、そのデバイスの
    ループバックを優先する。VB-Cable は複数の再生デバイスを持つことがあり、
    音を流した先と別のデバイスを録ると無音になる。
    """
    try:
        import pyaudiowpatch as pyaudio
        from recorder import WASAPI_PREFIX
        pa = pyaudio.PyAudio()
        try:
            names = [info.get("name", "") for info in pa.get_loopback_device_info_generator()]
        finally:
            pa.terminate()
        if render_name:
            for name in names:
                if name.startswith(render_name):
                    return f"{WASAPI_PREFIX}{name}"
        for name in names:
            if any(kw in name for kw in _VBCABLE_KEYWORDS):
                return f"{WASAPI_PREFIX}{name}"
    except Exception:
        pass
    return None
