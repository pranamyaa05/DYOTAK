"""Structured logging for DYOTAK with job_id and stage context."""

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger with structured formatting."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)]
    )
