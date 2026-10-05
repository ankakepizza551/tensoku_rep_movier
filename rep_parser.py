"""東方非想天則 .rep ファイルのヘッダを解析して録画時間を推定する。"""

from pathlib import Path

_HEADER_BYTES = 128
_BYTES_PER_FRAME = 4   # 1 フレームにつき 2 人分 × 2 バイトの入力
_FPS = 60
_BUFFER_SECONDS = 90  # リプレイ終了後の余裕（結果画面表示分や、推測精度のブレ吸収も含む）


def estimate_duration(rep_path: str) -> float | None:
    """
    .rep ファイルのサイズからリプレイ時間(秒)を推定して返す。
    ※非想天則のリプレイは入力ログのため、ファイルサイズからの推定はあくまで目安です。
    パース不能な場合は None を返す。
    """
    try:
        size = Path(rep_path).stat().st_size
        if size <= _HEADER_BYTES:
            return None
        frames = (size - _HEADER_BYTES) / _BYTES_PER_FRAME
        return frames / _FPS
    except Exception:
        return None


def auto_stop_duration(rep_path: str) -> float | None:
    """推定再生時間 + バッファ秒数を返す。"""
    d = estimate_duration(rep_path)
    if d is None:
        return None
    # 通常はゲームの画面状態からリプレイ終了を検出して止める。こちらはそれが効かない
    # 場合の打ち切り時間なので、推定が外れても途中で切れないよう 2 倍の余裕を取る。
    return d * 2 + _BUFFER_SECONDS
