import logging.config
import os


DEBUG_ENABLED = os.getenv("DEBUG", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AGENT_LOG_LEVEL = "DEBUG" if DEBUG_ENABLED else "INFO"
HANDLER_LOG_LEVEL = "DEBUG" if DEBUG_ENABLED else "INFO"

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "level": HANDLER_LOG_LEVEL,
            "stream": "ext://sys.stdout",
        },
    },
    "root": {
        "level": "INFO",
        "handlers": ["console"],
    },
    "loggers": {
        "core.agent": {
            "level": AGENT_LOG_LEVEL,
            "propagate": True,
        },
        "providers.model": {
            "level": AGENT_LOG_LEVEL,
            "propagate": True,
        },
    },
}
logging.config.dictConfig(LOGGING_CONFIG)
