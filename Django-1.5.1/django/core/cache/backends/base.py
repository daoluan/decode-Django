"Base Cache class."
from __future__ import unicode_literals

import warnings

from django.core.exceptions import ImproperlyConfigured, DjangoRuntimeWarning
from django.utils.importlib import import_module

class InvalidCacheBackendError(ImproperlyConfigured):
    pass

class CacheKeyWarning(DjangoRuntimeWarning):
    pass


# Memcached does not accept keys longer than this.
MEMCACHE_MAX_KEY_LENGTH = 250 key 的长度有限制

def default_key_func(key, key_prefix, version):
    """
    默认的 key 函数
    Default function to generate keys.

    Constructs the key used by all other methods. By default it prepends
    the `key_prefix'. KEY_FUNCTION can be used to specify an alternate
    function with custom key making behavior.
    """
    return '%s:%s:%s' % (key_prefix, version, key)

def get_key_func(key_func):
    """
    Function to decide which key function to use. 决定使用哪种 key 函数

    Defaults to ``default_key_func``.
    """
    if key_func is not None:
        if callable(key_func):
            return key_func
        else:
            key_func_module_path, key_func_name = key_func.rsplit('.', 1)
            key_func_module = import_module(key_func_module_path) 模块路径
            return getattr(key_func_module, key_func_name)          函数的名字
    return default_key_func

class BaseCache(object):
    def __init__(self, params):
        timeout = params.get('timeout', params.get('TIMEOUT', 300)) 超时时间

        try:
            timeout = int(timeout)
        except (ValueError, TypeError):
            timeout = 300

        self.default_timeout = timeout

        options = params.get('OPTIONS', {})

        最大的条目数有限制
        max_entries = params.get('max_entries', options.get('MAX_ENTRIES', 300))

        try:
            self._max_entries = int(max_entries)
        except (ValueError, TypeError):
            self._max_entries = 300

        不懂
        cull_frequency = params.get('cull_frequency', options.get('CULL_FREQUENCY', 3))

        try:
            self._cull_frequency = int(cull_frequency)
        except (ValueError, TypeError):
            self._cull_frequency = 3

        self.key_prefix = params.get('KEY_PREFIX', '')
        self.version = params.get('VERSION', 1)
        self.key_func = get_key_func(params.get('KEY_FUNCTION', None))

    def make_key(self, key, version=None):
        key 生成器
        """Constructs the key used by all other methods. By default it
        uses the key_func to generate a key (which, by default,
        prepends the `key_prefix' and 'version'). An different key
        function can be provided at the time of cache construction;
        alternatively, you can subclass the cache backend to provide
        custom key making behavior.
        """
        if version is None:
            version = self.version

        new_key = self.key_func(key, self.key_prefix, version)
        return new_key

    def add(self, key, value, timeout=None, version=None):
        """
        在某个 key 中存储 value
        Set a value in the cache if the key does not already exist. If
        timeout is given, that timeout will be used for the key; otherwise
        the default cache timeout will be used.

        如果存储成功, 返回 true
        Returns True if the value was stored, False otherwise. 
        """
        raise NotImplementedError

    def get(self, key, default=None, version=None):
        """
        读 value
        Fetch a given key from the cache. If the key does not exist, return
        default, which itself defaults to None.
        """
        raise NotImplementedError

    def set(self, key, value, timeout=None, version=None):
        """
        Set a value in the cache. If timeout is given, that timeout will be
        used for the key; otherwise the default cache timeout will be used.
        """
        raise NotImplementedError

    def delete(self, key, version=None):
        """
        Delete a key from the cache, failing silently.
        """
        raise NotImplementedError

    def get_many(self, keys, version=None):
        """
        返回一堆
        Fetch a bunch of keys from the cache. For certain backends (memcached,
        pgsql) this can be *much* faster when fetching multiple values.

        Returns a dict mapping each key in keys to its value. If the given
        key is missing, it will be missing from the response dict.
        """
        d = {}
        for k in keys:
            val = self.get(k, version=version)
            if val is not None:
                d[k] = val
        return d

    def has_key(self, key, version=None):
        """
        是否存在此 key
        Returns True if the key is in the cache and has not expired.
        """
        return self.get(key, version=version) is not None

    def incr(self, key, delta=1, version=None):
        """
        简单的 +1 操作
        Add delta to value in the cache. If the key does not exist, raise a
        ValueError exception.
        """
        value = self.get(key, version=version)
        if value is None:
            raise ValueError("Key '%s' not found" % key)
        new_value = value + delta
        self.set(key, new_value, version=version)
        return new_value

    def decr(self, key, delta=1, version=None):
        """
        简单的 -1 操作
        Subtract delta from value in the cache. If the key does not exist, raise
        a ValueError exception.
        """
        return self.incr(key, -delta, version=version)

    def __contains__(self, key):
        """
        Returns True if the key is in the cache and has not expired.
        """
        # This is a separate method, rather than just a copy of has_key(),
        # so that it always has the same functionality as has_key(), even
        # if a subclass overrides it.
        return self.has_key(key)

    def set_many(self, data, timeout=None, version=None):
        """
        Set a bunch of values in the cache at once from a dict of key/value
        pairs.  For certain backends (memcached), this is much more efficient
        than calling set() multiple times.

        If timeout is given, that timeout will be used for the key; otherwise
        the default cache timeout will be used.
        """
        for key, value in data.items():
            self.set(key, value, timeout=timeout, version=version)

    def delete_many(self, keys, version=None):
        """
        Set a bunch of values in the cache at once.  For certain backends
        (memcached), this is much more efficient than calling delete() multiple
        times.
        """
        for key in keys:
            self.delete(key, version=version)

    def clear(self):
        删除所有
        """Remove *all* values from the cache at once."""
        raise NotImplementedError

    def validate_key(self, key):
        """
        Warn about keys that would not be portable to the memcached
        backend. This encourages (but does not force) writing backend-portable
        cache code.

        """ key 的长度不能过长
        if len(key) > MEMCACHE_MAX_KEY_LENGTH:
            warnings.warn('Cache key will cause errors if used with memcached: '
                    '%s (longer than %s)' % (key, MEMCACHE_MAX_KEY_LENGTH),
                    CacheKeyWarning)

        for char in key:
            if ord(char) < 33 or ord(char) == 127:
                warnings.warn('Cache key contains characters that will cause '
                        'errors if used with memcached: %r' % key,
                              CacheKeyWarning)
                

    def incr_version(self, key, delta=1, version=None):
        """Adds delta to the cache version for the supplied key. Returns the
        new version.
        """
        if version is None:
            version = self.version

        value = self.get(key, version=version)
        if value is None:
            raise ValueError("Key '%s' not found" % key)

        self.set(key, value, version=version+delta)
        self.delete(key, version=version)
        return version+delta

    def decr_version(self, key, delta=1, version=None):
        """Substracts delta from the cache version for the supplied key. Returns
        the new version.
        """
        return self.incr_version(key, -delta, version)
