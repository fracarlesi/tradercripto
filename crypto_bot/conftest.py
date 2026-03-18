"""Root conftest: prevent OpenMP/XGBoost deadlock on macOS ARM64."""

import os

os.environ.setdefault("OMP_NUM_THREADS", "1")
