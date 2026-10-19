"""Lazy proxy that avoids importing remote-AI SDKs until an AI action is used."""

import threading

from core.logger import logger


class LazyAIEngine:
    def __init__(self):
        self._instance = None
        self._lock = threading.Lock()

    @property
    def initialized(self):
        return self._instance is not None

    def _get_instance(self):
        if self._instance is not None:
            return self._instance
        with self._lock:
            if self._instance is None:
                logger.info("[AIEngine] تحميل SDKs عند أول استخدام فعلي فقط.")
                from utils.runtime_health import log_memory_milestone
                log_memory_milestone("AI_INIT_START", force=True)
                from engines.ai_engine import AIEngine

                self._instance = AIEngine()
                log_memory_milestone("AI_INIT_END", force=True)
        return self._instance

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        attribute = getattr(self._get_instance(), name)
        if not callable(attribute):
            return attribute

        def _measured(*args, **kwargs):
            from utils.runtime_health import log_memory_milestone
            log_memory_milestone("AI_CALL_START", force=True, details={"method": name})
            try:
                return attribute(*args, **kwargs)
            finally:
                log_memory_milestone("AI_CALL_END", force=True, details={"method": name})

        return _measured
