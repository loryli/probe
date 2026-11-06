"""Application logs use UTC and never include configuration or request bodies."""

import logging
import time
from logging.config import dictConfig


class UTCFormatter(logging.Formatter):
    @staticmethod
    def converter(timestamp: float | None = None) -> time.struct_time:
        return time.gmtime(timestamp)


def configure_logging(level: str) -> None:
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "application": {
                    "()": UTCFormatter,
                    "format": "%(asctime)sZ %(levelname)s %(name)s %(message)s",
                    "datefmt": "%Y-%m-%dT%H:%M:%S",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "application",
                }
            },
            "loggers": {
                "interview_assistant": {
                    "handlers": ["console"],
                    "level": level,
                    "propagate": False,
                }
            },
        }
    )
