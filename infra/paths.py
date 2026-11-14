"""プロジェクト内の基準パス解決（Composition Root / infra 共通）。"""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
CATALOGS_DIR = ROOT_DIR / "catalogs"
CONFIG_PATH = ROOT_DIR / "config.json"
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "casino.db"
