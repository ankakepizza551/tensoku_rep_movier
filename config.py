import json
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    "th123_path": "",
    "output_dir": str(Path.home() / "Videos"),
    "framerate": 60,
    "wait_after_launch": 5.0,   # ゲーム起動後キー送信するまでの秒数
    "key_delay": 0.3,           # キー操作間の待機秒数
    "down_count": 6,            # タイトルからリプレイメニューへの↓回数
    "z_after_select": 4,        # リスト入場+フォルダ入場+ファイル選択+再生
    "crf": 20,                  # FFmpeg品質 (低いほど高品質)
    "preset": "veryfast",
    "auto_open_trim": False,
    "auto_stop": True,
    "game_audio_only": False,
    "filename_template": "{stem}",
}


def load() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    return DEFAULT_CONFIG.copy()


def save(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
