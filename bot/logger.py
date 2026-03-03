import logging
from logging.handlers import RotatingFileHandler


def setup_logger(name: str = "orfvg") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Console: INFO+
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    ))

    # File: DEBUG (all decisions)
    fh = RotatingFileHandler("orfvg.log", maxBytes=10 * 1024 * 1024, backupCount=5)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    ))

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger
