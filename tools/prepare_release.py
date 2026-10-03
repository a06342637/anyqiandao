import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_release import build

archive = build(ROOT / '.local' / 'release')
shutil.copyfile(archive, ROOT / '.local' / 'release.tar.gz')
