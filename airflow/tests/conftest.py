import os
import sys
import tempfile
from pathlib import Path

DAGS = Path(__file__).resolve().parents[1] / "dags"

# Isolated, throwaway Airflow home so tests never touch a real metadata DB or config.
os.environ.setdefault("AIRFLOW_HOME", tempfile.mkdtemp(prefix="ascstats-airflow-"))
os.environ.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "False")
os.environ.setdefault("AIRFLOW__CORE__UNIT_TEST_MODE", "True")
sys.path.insert(0, str(DAGS))
