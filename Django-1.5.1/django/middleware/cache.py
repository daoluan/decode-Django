"""
Cache middleware. If enabled, each Django-powered page will be cached based on
URL. The canonical way to enable cache middleware is to set
``UpdateCacheMiddleware`` as your first piece of middleware, and
``FetchFromCacheMiddleware`` as the last::

这种设置方法:
    MIDDLEWARE_CLASSES = [
        'django.middleware.cache.UpdateCacheMiddleware',
        ...
        'django.middleware.cache.FetchFromCacheMiddleware'
    ]

This is counter-intuitive, but correct: ``UpdateCacheMiddleware`` needs to run
last during the response phase, which processes middleware bottom-up;

从这里可以得到提示是 middleware 在 response 的时候是从下往上执行的, 并不是直观的从上往下

``FetchFromCacheMiddleware`` needs to run last during the request phase, which
processes middleware top-down.

从这里可以得到提示是 middleware 在 request 的时候是从上往下的执行的

The single-class ``CacheMiddleware`` can be used for some simple sites.
However, if any other piece of middleware needs to affect the cache key, you'll
need to use the two-part ``UpdateCacheMiddleware`` and
``FetchFromCacheMiddleware``. This'll most often happen when you're using
Django's ``LocaleMiddleware``.

More details about how the caching works:

* Only GET or HEAD-requests with status code 200 are cached.

* The number of seconds each page is stored for is set by the "max-age" section
  of the response's "Cache-Control" header, falling back to the
  CACHE_MIDDLEWARE_SECONDS setting if the section was not found.

CACHE_MIDDLEWARE_SECONDS 控制着 cache 的时间

* If CACHE_MIDDLEWARE_ANONYMOUS_ONLY is set to True, only anonymous requests
  (i.e., those not made by a logged-in user) will be cached. This is a simple
  and effective way of avoiding the caching of the Django admin (and any other
  user-specific content).

* This middleware expects that a HEAD request is answered with the same response
  headers exactly like the corresponding GET request. 也即是返回一样的 header

* When a hit occurs, a shallow copy of the original response object is returned
  from process_request.

* Pages will be cached based on the contents of the request headers listed in
  the response's "Vary" header.

* This middleware also sets ETag, Last-Modified, Expires and Cache-Control
  headers on the response object.

"""

from django.conf import settings

# 要看懂这个,还需要看懂 core.cache 
from django.core.cache import get_cache, DEFAULT_CACHE_ALIAS
from django.utils.cache import get_cache_key, learn_cache_key, patch_response_headers, get_max_age


class UpdateCacheMiddleware(object):
    """
    处理 response,必须放到第一个位置(从下到上,也就是说他会最后一个被执行)
    Response-phase cache middleware that updates the cache if the response is
    cacheable. 更新缓存

    Must be used as part of the two-part update/fetch cache middleware.
    UpdateCacheMiddleware must be the first piece of middleware in
    MIDDLEWARE_CLASSES so that it'll get called last during the response phase.

    UpdateCacheMiddleware 在 response 中会在最后才执行
    """
    def __init__(self):
        self.cache_timeout = settings.CACHE_MIDDLEWARE_SECONDS
        self.key_prefix = settings.CACHE_MIDDLEWARE_KEY_PREFIX
        self.cache_anonymous_only = getattr(settings, 'CACHE_MIDDLEWARE_ANONYMOUS_ONLY', False)
        self.cache_alias = settings.CACHE_MIDDLEWARE_ALIAS
        self.cache = get_cache(self.cache_alias)

    def _session_accessed(self, request):
        try:
            return request.session.accessed
        except AttributeError:
            return False

    def _should_update_cache(self, request, response):
        if not hasattr(request, '_cache_update_cache') or not request._cache_update_cache:
            return False

        # 看不懂
        # If the session has not been accessed otherwise, we don't want to
        # cause it to be accessed here. If it hasn't been accessed, then the
        # user's logged-in status has not affected the response anyway.
        if self.cache_anonymous_only and self._session_accessed(request):
            assert hasattr(request, 'user'), "The Django cache middleware with CACHE_MIDDLEWARE_ANONYMOUS_ONLY=True requires authentication middleware to be installed. Edit your MIDDLEWARE_CLASSES setting to insert 'django.contrib.auth.middleware.AuthenticationMiddleware' before the CacheMiddleware."
            if request.user.is_authenticated():
                # cache_anonymous_only 设置了,那么只有未验证的用户才需要缓存
                # Don't cache user-variable requests from authenticated users.
                return False
        return True

    def process_response(self, request, response):
        """Sets the cache, if needed."""
        if not self._should_update_cache(request, response):
            # We don't need to update the cache, just return.
            return response

        if response.streaming or response.status_code != 200: # 如果状态码不是 200,直接返回
            return response

        # Try to get the timeout from the "max-age" section of the "Cache-
        # Control" header before reverting to using the default cache_timeout
        # length.
        timeout = get_max_age(response)
        if timeout == None:
            timeout = self.cache_timeout # 如果没有直接设置 settint 中默认的设置
        elif timeout == 0:
            # max-age was set to 0, don't bother caching.
            return response # 如果是0,就不缓存了

        patch_response_headers(response, timeout) # patch 修补, 修改时间用
        if timeout:
            cache_key = learn_cache_key(request, response, timeout, self.key_prefix, cache=self.cache)
            if hasattr(response, 'render') and callable(response.render):
                response.add_post_render_callback(
                    lambda r: self.cache.set(cache_key, r, timeout)
                )
            else:
                self.cache.set(cache_key, response, timeout)
        return response

class FetchFromCacheMiddleware(object):
    """
    处理 request,必须放到最后一个位置(从上到下,也就是说他会最后一个被执行)
    Request-phase cache middleware that fetches a page from the cache. 在缓存中获取页面

    Must be used as part of the two-part update/fetch cache middleware.
    FetchFromCacheMiddleware must be the last piece of middleware in
    MIDDLEWARE_CLASSES so that it'll get called last during the request phase.
    """

    def __init__(self):
        self.cache_timeout = settings.CACHE_MIDDLEWARE_SECONDS
        self.key_prefix = settings.CACHE_MIDDLEWARE_KEY_PREFIX
        self.cache_anonymous_only = getattr(settings, 'CACHE_MIDDLEWARE_ANONYMOUS_ONLY', False)
        self.cache_alias = settings.CACHE_MIDDLEWARE_ALIAS
        self.cache = get_cache(self.cache_alias)

    def process_request(self, request):
        """
        Checks whether the page is already cached and returns the cached
        version if available.
        """
        if not request.method in ('GET', 'HEAD'):
            request._cache_update_cache = False
            return None # Don't bother checking the cache.

        # try and get the cached GET response  GEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEET
        cache_key = get_cache_key(request, self.key_prefix, 'GET', cache=self.cache)
        if cache_key is None:
            request._cache_update_cache = True
            return None # No cache information available, need to rebuild.

        response = self.cache.get(cache_key, None)
        # if it wasn't found and we are looking for a HEAD, try looking just for that HEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD
        if response is None and request.method == 'HEAD':
            cache_key = get_cache_key(request, self.key_prefix, 'HEAD', cache=self.cache)
            response = self.cache.get(cache_key, None)

        if response is None:
            request._cache_update_cache = True
            return None # No cache information available, need to rebuild.

        # hit, return cached response
        request._cache_update_cache = False
        return response

# 不懂用来做什么
class CacheMiddleware(UpdateCacheMiddleware, FetchFromCacheMiddleware):
    """
    Cache middleware that provides basic behavior for many simple sites. 为简单的页面提供了基本的功能

    Also used as the hook point for the cache decorator, which is generated
    using the decorator-from-middleware utility.
    """
    def __init__(self, cache_timeout=None, cache_anonymous_only=None, **kwargs):
        # We need to differentiate between "provided, but using default value",
        # and "not provided". If the value is provided using a default, then
        # we fall back to system defaults. If it is not provided at all,
        # we need to use middleware defaults.

        cache_kwargs = {}

        try:
            self.key_prefix = kwargs['key_prefix']
            if self.key_prefix is not None:
                cache_kwargs['KEY_PREFIX'] = self.key_prefix
            else:
                self.key_prefix = ''
        except KeyError:
            self.key_prefix = settings.CACHE_MIDDLEWARE_KEY_PREFIX
            cache_kwargs['KEY_PREFIX'] = self.key_prefix

        try:
            self.cache_alias = kwargs['cache_alias']
            if self.cache_alias is None:
                self.cache_alias = DEFAULT_CACHE_ALIAS
            if cache_timeout is not None:
                cache_kwargs['TIMEOUT'] = cache_timeout
        except KeyError:
            self.cache_alias = settings.CACHE_MIDDLEWARE_ALIAS
            if cache_timeout is None:
                cache_kwargs['TIMEOUT'] = settings.CACHE_MIDDLEWARE_SECONDS
            else:
                cache_kwargs['TIMEOUT'] = cache_timeout

        if cache_anonymous_only is None:
            self.cache_anonymous_only = getattr(settings, 'CACHE_MIDDLEWARE_ANONYMOUS_ONLY', False)
        else:
            self.cache_anonymous_only = cache_anonymous_only

        self.cache = get_cache(self.cache_alias, **cache_kwargs)
        self.cache_timeout = self.cache.default_timeout
