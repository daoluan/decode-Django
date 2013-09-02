"Thread-safe in-memory cache backend."
线程安全内存缓存方案

Django 内部实现

import time
try:
    from django.utils.six.moves import cPickle as pickle
except ImportError:
    import pickle

from django.core.cache.backends.base import BaseCache
from django.utils.synch import RWLock

# Global in-memory store of cache data. Keyed by name, to provide
# multiple named local memory caches.

_caches = {} 缓存
_expire_info = {} 过期信息
_locks = {}     锁锁

class LocMemCache(BaseCache):
    def __init__(self, name, params):
        BaseCache.__init__(self, params)
        global _caches, _expire_info, _locks
        self._cache = _caches.setdefault(name, {})
        self._expire_info = _expire_info.setdefault(name, {})
        self._lock = _locks.setdefault(name, RWLock()) 读写锁

    def add(self, key, value, timeout=None, version=None):

        key = self.make_key(key, version=version)
        self.validate_key(key)

        with self._lock.writer(): 写锁
            exp = self._expire_info.get(key) 获取过期时间

            if exp is None or exp <= time.time(): 已经过期
                try:
                    pickled = pickle.dumps(value, pickle.HIGHEST_PROTOCOL)
                    self._set(key, pickled, timeout)
                    return True
                except pickle.PickleError:
                    pass
            return False

    def get(self, key, default=None, version=None):
        key = self.make_key(key, version=version)
        self.validate_key(key)

        with self._lock.reader(): 读锁
            exp = self._expire_info.get(key)

            if exp is None:
                return default
            elif exp > time.time(): 没有过期
                try:
                    pickled = self._cache[key]
                    return pickle.loads(pickled)
                except pickle.PickleError:
                    return default

        with self._lock.writer(): 写锁, 因为过期了
            try:
                del self._cache[key]
                del self._expire_info[key]
            except KeyError:
                pass
            return default

    def _set(self, key, value, timeout=None):
        if len(self._cache) >= self._max_entries:
            self._cull() 挑出

        if timeout is None:
            timeout = self.default_timeout

        self._cache[key] = value
        self._expire_info[key] = time.time() + timeout

    def set(self, key, value, timeout=None, version=None):
        key = self.make_key(key, version=version)
        self.validate_key(key)

        with self._lock.writer(): 写锁
            try:
                pickled = pickle.dumps(value, pickle.HIGHEST_PROTOCOL)
                self._set(key, pickled, timeout)
            except pickle.PickleError:
                pass

    不懂, 有什么用
    def incr(self, key, delta=1, version=None):
        value = self.get(key, version=version)

        if value is None:
            raise ValueError("Key '%s' not found" % key)

        new_value = value + delta
        key = self.make_key(key, version=version)

        with self._lock.writer():
            try:
                pickled = pickle.dumps(new_value, pickle.HIGHEST_PROTOCOL)
                self._cache[key] = pickled
            except pickle.PickleError:
                pass

        return new_value

    def has_key(self, key, version=None):
        key = self.make_key(key, version=version)
        self.validate_key(key)

        with self._lock.reader():
            exp = self._expire_info.get(key)

            if exp is None:
                return False
            elif exp > time.time():
                return True

        with self._lock.writer(): 写锁, 过期
            try:
                del self._cache[key]
                del self._expire_info[key]
            except KeyError:
                pass
            return False

    def _cull(self):
        if self._cull_frequency == 0: _cull_frequency 没有设置, 全部删除
            self.clear()
        else:
            doomed = [k for (i, k) in enumerate(self._cache) if i % self._cull_frequency == 0]

            for k in doomed:
                self._delete(k)

    def _delete(self, key):
        删除缓存和过期信息
        try:
            del self._cache[key]
        except KeyError:
            pass

        try:
            del self._expire_info[key]
        except KeyError:
            pass

    def delete(self, key, version=None):
        key = self.make_key(key, version=version)
        self.validate_key(key)

        with self._lock.writer(): 写锁
            self._delete(key)

    def clear(self):
        self._cache.clear()
        self._expire_info.clear()

# For backwards compatibility
class CacheClass(LocMemCache):
    pass
