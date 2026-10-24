import csv
import sys
from pathlib import Path

import pytest

CODE_DIR = Path(__file__).resolve().parents[1]
for path in (CODE_DIR, CODE_DIR / "evaluation", CODE_DIR / "tools"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from audit.loader import DATASET_DIR, read_csv  # noqa: E402

SAMPLE_OUTPUT = DATASET_DIR / "sample_output.csv"
SAMPLE_REQUESTS = DATASET_DIR / "sample_deletion_requests.csv"


@pytest.fixture
def gold_rows():
    return read_csv(SAMPLE_OUTPUT)


def write_output(path: Path, rows, header=("case_id", "deletion_status", "steps_json")) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow([row[c] for c in header] if isinstance(row, dict) else row)
    return path
