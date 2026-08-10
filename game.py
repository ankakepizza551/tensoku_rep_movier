"""th123 の起動・ウィンドウ検出・キー操作を担当するモジュール。"""

import ctypes
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

import win32api
import win32con
import win32gui
import win32process

# Virtual Key コード
VK_Z = 0x5A
VK_DOWN = 0x28

# ! はどの数字・漢字より前にソートされるので replay/ の先頭に来る
TEMP_FOLDER_NAME = "!"          # 先頭フォルダ名（どの日付フォルダより前にソート）
TEMP_REP_NAME    = "temp.rep"   # フォルダ内の一時ファイル名


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          ctypes.c_long),
        ("dy",          ctypes.c_long),
        ("mouseData",   ctypes.c_ulong),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR
    ]

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         ctypes.c_ushort),
        ("wScan",       ctypes.c_ushort),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR
    ]

class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg",    ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]

class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]
    _fields_ = [("type", ctypes.c_ulong), ("_u", _U)]

_INPUT_KEYBOARD       = 1
_KEYEVENTF_KEYUP      = 0x0002
_KEYEVENTF_EXTENDEDKEY = 0x0001

# 拡張キー（E0プレフィックスが必要な矢印キー等）
_EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}
# VK_PRIOR, VK_NEXT, VK_END, VK_HOME, VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_INSERT, VK_DELETE


def _send_key(hwnd: int, vk: int) -> int:
    """
    SendInput でキーダウン→待機→キーアップを送る。
    DirectInput の即時ポーリングに確実に捕捉させるためダウンとアップを分ける。
    """
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.05)

    scan  = ctypes.windll.user32.MapVirtualKeyW(vk, 0)
    flags = _KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0
    sz    = ctypes.sizeof(_INPUT)

    down = (_INPUT * 1)(
        _INPUT(type=_INPUT_KEYBOARD,
               _u=_INPUT._U(ki=_KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags)))
    )
    up = (_INPUT * 1)(
        _INPUT(type=_INPUT_KEYBOARD,
               _u=_INPUT._U(ki=_KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags | _KEYEVENTF_KEYUP)))
    )
    n  = ctypes.windll.user32.SendInput(1, down, sz)
    time.sleep(0.1)   # DirectInput のポーリング間隔より長く保持
    n += ctypes.windll.user32.SendInput(1, up, sz)
    return n


# ── ウィンドウ検索 ────────────────────────────────────────

def _get_exe_name_for_pid(pid: int) -> str | None:
    """
    管理者権限で動いているプロセスにも対応できるよう
    PROCESS_QUERY_LIMITED_INFORMATION + QueryFullProcessImageNameW で取得する。
    """
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    hproc = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not hproc:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size))
        return buf.value if ok else None
    finally:
        ctypes.windll.kernel32.CloseHandle(hproc)


def _find_hwnd_by_exe(exe_name: str) -> int | None:
    """プロセス名で可視ウィンドウの hwnd を返す（管理者プロセスも対応）。"""
    found: list[int] = []
    exe_lower = exe_name.lower()

    def _cb(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            path = _get_exe_name_for_pid(pid)
            if path and Path(path).name.lower() == exe_lower:
                found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else None


def is_already_running(th123_path: str) -> bool:
    """th123.EXE がすでに起動中かどうかを確認する。"""
    return _find_hwnd_by_exe(Path(th123_path).name) is not None


def find_game_hwnd(th123_path: str) -> int | None:
    """起動中の th123.EXE のウィンドウハンドルを返す。見つからなければ None。"""
    return _find_hwnd_by_exe(Path(th123_path).name)


def _find_hwnd_by_pid(pid: int) -> int | None:
    """指定 PID のプロセスに紐づく可視ウィンドウの hwnd を返す。"""
    found: list[int] = []

    def _cb(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _, win_pid = win32process.GetWindowThreadProcessId(hwnd)
            if win_pid == pid:
                found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else None


def wait_for_window(pid: int, timeout: float = 30.0, log: Callable = print) -> int:
    """起動したプロセスの PID でウィンドウを待機する。既存の同名プロセスとは区別される。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = _find_hwnd_by_pid(pid)
        if hwnd:
            title = win32gui.GetWindowText(hwnd)
            log(f"ゲームウィンドウを検出しました: 「{title}」")
            return hwnd
        time.sleep(0.5)
    raise RuntimeError(f"ゲームウィンドウが {timeout:.0f}秒以内に見つかりませんでした")


def focus_window(hwnd: int) -> None:
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.3)
    except Exception:
        pass


def set_topmost(hwnd: int, topmost: bool) -> None:
    """ウィンドウを常に最前面 or 通常に切り替える。"""
    flag = win32con.HWND_TOPMOST if topmost else win32con.HWND_NOTOPMOST
    try:
        win32gui.SetWindowPos(
            hwnd, flag, 0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
        )
    except Exception:
        pass


def sample_window_pixels(hwnd: int, cols: int = 5, rows: int = 4) -> list[int] | None:
    """
    ゲームウィンドウのクライアント領域をグリッドサンプリングしてピクセル値リストを返す。
    画面変化の検出に使用する。取得失敗時は None を返す。
    """
    try:
        cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
        w, h = cr - cl, cb - ct
        if w <= 0 or h <= 0:
            return None
        pt = ctypes.wintypes.POINT(0, 0)
        ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt))
        ox, oy = pt.x, pt.y
        xs = [ox + int(w * (i + 1) / (cols + 1)) for i in range(cols)]
        ys = [oy + int(h * (j + 1) / (rows + 1)) for j in range(rows)]
        hdc = ctypes.windll.user32.GetDC(0)
        pixels = [ctypes.windll.gdi32.GetPixel(hdc, x, y) for x in xs for y in ys]
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return pixels
    except Exception:
        return None


# ── リプレイファイル管理 ──────────────────────────────────

def copy_rep_to_game(rep_path: str, th123_path: str) -> Path:
    """
    replay/!/temp.rep としてコピーする。
    「!」フォルダはどの日付フォルダよりソート順が前なのでリスト先頭に来る。
    ゲームはフォルダを先に表示するため、ファイルではなくフォルダで先頭を取る。
    """
    temp_dir = Path(th123_path).parent / "replay" / TEMP_FOLDER_NAME
    temp_dir.mkdir(parents=True, exist_ok=True)
    dest = temp_dir / TEMP_REP_NAME
    shutil.copy2(rep_path, dest)
    return dest


def remove_rep_from_game(dest: Path) -> None:
    if dest.exists():
        dest.unlink()
    # フォルダが空になったら消す
    try:
        dest.parent.rmdir()
    except OSError:
        pass


# ── キー操作 ────────────────────────────────────────────

def _send_key_with_verify(
    hwnd: int,
    vk: int,
    log: Callable = print,
    attempts: int = 3,
    wait_secs: float = 2.0,
    threshold: int = 8,
) -> None:
    """
    キーを送信し、画面が大きく変化したら受付成功と判定する。
    反応がなければ最大 attempts 回まで再送する。
    タイトル画面で初回 Z が入らずリプレイ画面に進めない問題への対策。
    """
    for i in range(1, attempts + 1):
        baseline = sample_window_pixels(hwnd)
        _send_key(hwnd, vk)
        if baseline is None:
            return  # ピクセル取得不可なら単発送信で終了
        deadline = time.time() + wait_secs
        while time.time() < deadline:
            time.sleep(0.15)
            current = sample_window_pixels(hwnd)
            if current is None or len(current) != len(baseline):
                continue
            changed = sum(p != q for p, q in zip(baseline, current))
            if changed >= threshold:
                if i > 1:
                    log(f"  → {i} 回目で受付")
                return
        if i < attempts:
            log(f"  → 反応なし、再送 ({i}/{attempts})")
        else:
            log(f"  → 警告: 画面変化が検出できませんでした")


def send_key_sequence(
    hwnd: int,
    down_count: int,
    z_after_select: int,
    key_delay: float,
    log: Callable = print,
) -> None:
    """
    タイトル画面 → リプレイ再生開始 までキーを送る。
    一時ファイルは常にリスト先頭なので ↓ での位置合わせは不要。

    シーケンス:
        Z                   タイトル確定
        ↓ × down_count      リプレイ鑑賞メニューへ
        Z × z_after_select  リスト入場 → 先頭選択 → 再生開始
    """
    # 診断: フォーカスが正しくゲームウィンドウに当たっているか確認
    focus_hwnd = ctypes.windll.user32.GetForegroundWindow()
    focus_title = win32gui.GetWindowText(focus_hwnd)
    game_title  = win32gui.GetWindowText(hwnd)
    log(f"[診断] フォーカス中: 「{focus_title}」 / ゲーム: 「{game_title}」 / 一致: {focus_hwnd == hwnd}")

    log("z (タイトル確定)")
    _send_key_with_verify(hwnd, VK_Z, log=log)
    # タイトル確定後はメニュー遷移アニメーションがあり、その間は入力を受け付けない。
    # key_delay より長い固定1秒を確保する。
    time.sleep(max(key_delay, 1.0))

    log(f"↓ × {down_count} (リプレイ鑑賞メニューへ)")
    for _ in range(down_count):
        _send_key(hwnd, VK_DOWN)
        time.sleep(key_delay)

    log(f"Z × {z_after_select} (リスト入場・選択・再生)")
    for i in range(z_after_select):
        if i == 0:
            # 最初の Z はメニュー→リスト遷移で大きく変わる。検証付きで送る
            _send_key_with_verify(hwnd, VK_Z, log=log)
        else:
            _send_key(hwnd, VK_Z)
        time.sleep(key_delay)

    log("キー操作完了")


# ── ゲーム起動 ───────────────────────────────────────────

class _ElevatedProcess:
    """ShellExecuteEx で起動した管理者プロセスのラッパー。Popen と同じ poll/terminate インターフェース。"""

    def __init__(self, h_process: int):
        self._h = h_process
        import win32process as _wp
        self.pid: int = _wp.GetProcessId(h_process)

    def poll(self) -> int | None:
        import win32event
        result = win32event.WaitForSingleObject(self._h, 0)
        return None if result == win32event.WAIT_TIMEOUT else 0

    def terminate(self) -> None:
        import win32process
        try:
            win32process.TerminateProcess(self._h, 0)
        except Exception:
            pass


def launch_game(th123_path: str) -> subprocess.Popen | _ElevatedProcess:
    """
    th123.EXE を起動する。
    管理者権限が必要な場合 (WinError 740) は ShellExecuteEx で UAC 昇格して起動する。
    戻り値の .pid で起動したプロセスの PID を取得できる。
    """
    try:
        return subprocess.Popen(
            [th123_path],
            cwd=str(Path(th123_path).parent),
        )
    except OSError as e:
        if getattr(e, "winerror", None) == 740:
            import win32com.shell.shell as shell
            import win32com.shell.shellcon as shellcon
            result = shell.ShellExecuteEx(
                fMask=shellcon.SEE_MASK_NOCLOSEPROCESS,
                lpVerb="runas",
                lpFile=th123_path,
                lpDirectory=str(Path(th123_path).parent),
                nShow=1,
            )
            return _ElevatedProcess(result["hProcess"])
        raise


def is_running(proc) -> bool:
    return proc.poll() is None
