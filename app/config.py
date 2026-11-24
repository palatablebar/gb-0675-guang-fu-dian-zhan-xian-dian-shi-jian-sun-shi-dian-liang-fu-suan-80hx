from pathlib import Path
import json, os
ROOT = Path(__file__).resolve().parents[1]
META = json.loads((ROOT / 'app/project.json').read_text())
MODULES = META['keys']
RULE_VERSION = '2026.11.23-r1'
DB_PATH = os.environ.get('DB_PATH', '/data/workbench.sqlite')
SENTINELS = {'', '-', 'N/A', 'NULL', '未知'}
MAX_BYTES = 2_000_000
# 储能并网点仲裁参数（可被批次 context 覆盖）
STORAGE_TOLERANCE_KWH = '0.5'
EXPORT_LIMIT_KW = '120'
BASIS_SCOPE = {'charge_kw', 'discharge_kw', 'export_limit_kw'}
