"""Shared runtime state for rate limits, locks, and idempotency."""
import hashlib
import json
import threading
import time
from contextlib import contextmanager

from .config import ENVIRONMENT, REDIS_URL


class InMemoryRuntimeState:
    def __init__(self):
        self._lock = threading.RLock()
        self._values = {}
        self._locks = {}

    def allow(self, key, limit, window_seconds):
        now = time.time()
        with self._lock:
            start, count = self._values.get(key, (now, 0))
            if now - start >= window_seconds:
                start, count = now, 0
            if count >= limit:
                return False
            self._values[key] = (start, count + 1)
            return True

    @contextmanager
    def lock(self, key, timeout_seconds=30):
        with self._lock:
            lock = self._locks.setdefault(key, threading.Lock())
        if not lock.acquire(timeout=timeout_seconds):
            raise TimeoutError("runtime lock timeout")
        try:
            yield
        finally:
            lock.release()

    def get_idempotency(self, key):
        with self._lock:
            value = self._values.get("idempotency:" + key)
            if not value or value[0] <= time.time():
                return None
            return value[1]

    def set_idempotency(self, key, value, ttl_seconds=86400):
        with self._lock:
            self._values["idempotency:" + key] = (time.time() + ttl_seconds, value)


class RedisRuntimeState:
    def __init__(self, url):
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("install redis for production runtime state") from exc
        self._redis = redis.Redis.from_url(url, decode_responses=True)

    def allow(self, key, limit, window_seconds):
        count = self._redis.incr(key)
        if count == 1:
            self._redis.expire(key, window_seconds)
        return count <= limit

    @contextmanager
    def lock(self, key, timeout_seconds=30):
        with self._redis.lock("lock:" + key, timeout=timeout_seconds, blocking_timeout=timeout_seconds):
            yield

    def get_idempotency(self, key):
        value = self._redis.get("idempotency:" + key)
        return json.loads(value) if value else None

    def set_idempotency(self, key, value, ttl_seconds=86400):
        self._redis.setex("idempotency:" + key, ttl_seconds, json.dumps(value))


def request_fingerprint(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


if ENVIRONMENT == "production":
    if not REDIS_URL:
        raise RuntimeError("REDIS_URL must be configured in production")
    runtime_state = RedisRuntimeState(REDIS_URL)
else:
    runtime_state = InMemoryRuntimeState()