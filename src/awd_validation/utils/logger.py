"""
Structured logging setup for AWD Validation Pipeline.
"""
import logging
import sys
from pathlib import Path
from datetime import datetime
from loguru import logger


def setup_logger(run_id: str, log_dir: str = "/app/logs") -> logger:
    """
    Configure loguru logger for a validation run.
    
    Args:
        run_id: Unique identifier for this validation run
        log_dir: Directory to write log files
        
    Returns:
        Configured logger instance
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Remove default logger
    logger.remove()

    # Console handler - INFO and above
    logger.add(
        sys.stdout,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> | "
            "<level>{message}</level>"
        ),
        level="INFO",
        colorize=True,
    )

    # Full log file - DEBUG and above
    logger.add(
        log_path / f"validation_{run_id}.log",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss} | "
            "{level: <8} | "
            "{name}:{function}:{line} | "
            "{message}"
        ),
        level="DEBUG",
        rotation="50 MB",
        retention="30 days",
        encoding="utf-8",
    )

    # Error-only log file
    logger.add(
        log_path / f"errors_{run_id}.log",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss} | "
            "{level: <8} | "
            "{name}:{function}:{line} | "
            "{message}"
        ),
        level="ERROR",
        rotation="10 MB",
        encoding="utf-8",
    )

    logger.info(f"Logger initialized for run: {run_id}")
    return logger
