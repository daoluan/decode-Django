from __future__ import unicode_literals

import logging
import sys
import types

from django import http
from django.conf import settings
from django.core import exceptions
from django.core import urlresolvers
from django.core import signals
from django.utils.encoding import force_text
from django.utils.importlib import import_module
from django.utils import six
from django.views import debug

logger = logging.getLogger('django.request')

# 好经典的 handler
class BaseHandler(object):
    # Changes that are always applied to a response (in this order).
    response_fixes = [
        http.fix_location_header,
        http.conditional_content_removal,
        http.fix_IE_for_attach,
        http.fix_IE_for_vary,
    ]

    初始化函数, 初始化请求中间件, 视图中间件, 模版中间件, 响应中间件和异常中间件.
    def __init__(self):
        self._request_middleware = self._view_middleware =
            self._template_response_middleware =
            self._response_middleware =
            self._exception_middleware = None  视图, 模版相应, 相应, 异常中间件, 请求中间件

    根据 mysite.settings.py 中的 `MIDDLEWARE_CLASSES` 添加所有的中间件.
    def load_middleware(self):
        """
        Populate middleware lists from settings.MIDDLEWARE_CLASSES.

        从 settings 中加载各种中间件

        Must be called after the environment is fixed (see __call__ in subclasses).
        """
        # 初始化四种中间件
        self._view_middleware = []
        self._template_response_middleware = []
        self._response_middleware = []
        self._exception_middleware = []

        # 临时的请求中间件, 因为在加入中间件的过程中, 可能会出现异常, 而出现异常都导致加载中间件的不成功, 因此将 self._request_middleware 的赋值放在最后, 表示已经成功.
        request_middleware = []

        # settings.MIDDLEWARE_CLASSES 设置项指定需要预装的中间件
        for middleware_path in settings.MIDDLEWARE_CLASSES:
            try:
                mw_module, mw_classname = middleware_path.rsplit('.', 1)
            except ValueError:
                raise exceptions.ImproperlyConfigured('%s isn\'t a middleware module' % middleware_path)

            try:
                尝试导入中间件所在模块.
                mod = import_module(mw_module)
            except ImportError as e:
                raise exceptions.ImproperlyConfigured('Error importing middleware %s: "%s"' % (mw_module, e))

            try:
                尝试得到某种中间件类
                mw_class = getattr(mod, mw_classname)
            except AttributeError:
                raise exceptions.ImproperlyConfigured('Middleware module "%s" does not define a "%s" class' % (mw_module, mw_classname))

            try:
                尝试实例化
                mw_instance = mw_class()
            except exceptions.MiddlewareNotUsed:
                continue

            和 urllib 的处理方法类似: 请求预处理, 视图处理?, 模版处理, 相应处理, 错误处理(详见我的 urllib 源码剖析)
            if hasattr(mw_instance, 'process_request'):
                # 这里 request_middleware 用的是 append(), 这里是有讲究的:
                # django 规定, 多个请求中间件调用的次序是其出现的次序, 下同
                request_middleware.append(mw_instance.process_request)

            if hasattr(mw_instance, 'process_view'):
                self._view_middleware.append(mw_instance.process_view)

            if hasattr(mw_instance, 'process_template_response'):
                # 这里 _template_response_middleware 用的是 insert() 头插法, 这里是有讲究的:
                # django 规定, 多个模版相应中间件调用的次序是其出现次序的逆序, 下同
                self._template_response_middleware.insert(0, mw_instance.process_template_response)

            if hasattr(mw_instance, 'process_response'):
                self._response_middleware.insert(0, mw_instance.process_response)

            if hasattr(mw_instance, 'process_exception'):
                self._exception_middleware.insert(0, mw_instance.process_exception)

        # We only assign to this when initialization is complete as it is used
        # as a flag for initialization being complete.
        # 结束的标识, 表明中间件加载成功
        self._request_middleware = request_middleware

    # 处理请求的函数, 并返回 response
    def get_response(self, request):
        "Returns an HttpResponse object for the given HttpRequest"
        根据请求, 得到响应

        try:
            为该线程提供默认的 url 处理器
            # Setup default url resolver for this thread, this code is outside
            # the try/except so we don't get a spurious "unbound local
            # variable" exception in the event an exception is raised before
            # resolver is set

            #ROOT_URLCONF = 'mysite.urls'
            urlconf = settings.ROOT_URLCONF

            # set_urlconf() 会设置 url 配置即 settings.ROOT_URLCONF
            urlresolvers.set_urlconf(urlconf)

            # 实例化 RegexURLResolver, 暂且将其理解为一个 url 的匹配处理器, 下节展开
            resolver = urlresolvers.RegexURLResolver(r'^/', urlconf)

            try:
                response = None

                # Apply request middleware 调用请求中间件
                for middleware_method in self._request_middleware:
                    response = middleware_method(request)

                    # 如果此 response 有效, 即不走下面的逻辑
                    if response:
                        break

                # 如果没有结果
                if response is None:
                    # 尝试 request 中是否有 urlconf, 一般没有, 可以忽略此段代码!!!
                    if hasattr(request, 'urlconf'):
                        # Reset url resolver with a custom urlconf. 自定义的 urlconf
                        urlconf = request.urlconf
                        urlresolvers.set_urlconf(urlconf)
                        resolver = urlresolvers.RegexURLResolver(r'^/', urlconf)
                    # 调用 RegexURLResolver.resolve(), 可以理解为启动匹配的函数; 返回 ResolverMatch 实例
                    resolver_match = resolver.resolve(request.path_info)

                    # resolver_match 对象中存储了有用的信息, 譬如 callback 就是我们在 views.py 中定义的函数.
                    callback, callback_args, callback_kwargs = resolver_match

                    # 将返回的 resolver_match 挂钩到 request
                    request.resolver_match = resolver_match

                    # Apply view middleware 调用视图中间件
                    for middleware_method in self._view_middleware:
                        response = middleware_method(request, callback, callback_args, callback_kwargs)

                        # 如果此 response 有效, 即不走下面的逻辑
                        if response:
                            break

                # response 还是为空
                if response is None:
                    try:
                        # 这里调用的是真正的处理函数, 我们一般在 view.py 中定义这些函数
                        response = callback(request, *callback_args, **callback_kwargs)

                    except Exception as e:
                        # If the view raised an exception, run it through exception
                        # middleware, and if the exception middleware returns a
                        # response, use that. Otherwise, reraise the exception.

                        # 出现异常, 调用异常中间件
                        for middleware_method in self._exception_middleware:
                            response = middleware_method(request, e)

                            # 如果此 response 有效, 即不走下面的逻辑
                            if response:
                                break

                        if response is None:
                            raise

                # response 还是为空, 可能就要异常了
                # Complain if the view returned None (a common error).
                if response is None:
                    if isinstance(callback, types.FunctionType):    # FBV
                        view_name = callback.__name__
                    else:                                           # CBV
                        view_name = callback.__class__.__name__ + '.__call__'
                    raise ValueError("The view %s.%s didn't return an HttpResponse object." % (callback.__module__, view_name))

                # If the response supports deferred rendering, apply template
                # response middleware and the render the response 如果 response 实现了 render, 那么渲染返回.
                if hasattr(response, 'render') and callable(response.render):
                    for middleware_method in self._template_response_middleware:
                        response = middleware_method(request, response)
                    response = response.render()

            except http.Http404 as e:
                logger.warning('Not Found: %s', request.path,
                            extra={
                                'status_code': 404,
                                'request': request
                            })

                # 如果是调试下, 直接要返回 404 页面
                if settings.DEBUG:
                    response = debug.technical_404_response(request, e)
                else:
                    try:
                        # 非调试模式下, 获取 url 处理器的默认 404 处理
                        callback, param_dict = resolver.resolve404()
                        response = callback(request, **param_dict)
                    except:
                        signals.got_request_exception.send(sender=self.__class__, request=request)
                        response = self.handle_uncaught_exception(request, resolver, sys.exc_info())

            # 访问拒绝
            except exceptions.PermissionDenied:
                logger.warning(
                    'Forbidden (Permission denied): %s', request.path,
                    extra={
                        'status_code': 403,
                        'request': request
                    })
                try:
                    callback, param_dict = resolver.resolve403()
                    response = callback(request, **param_dict)
                except:
                    signals.got_request_exception.send(
                            sender=self.__class__, request=request)
                    response = self.handle_uncaught_exception(request,
                            resolver, sys.exc_info())

            except SystemExit:
                # Allow sys.exit() to actually exit. See tickets #1023 and #4701
                raise

            except: # Handle everything else, including SuspiciousOperation, etc.
                # Get the exception info now, in case another exception is thrown later.
                signals.got_request_exception.send(sender=self.__class__, request=request)
                response = self.handle_uncaught_exception(request, resolver, sys.exc_info())
        finally:
            # Reset URLconf for this thread on the way out for complete
            # isolation of request.urlconf 重置, 因为前面有两种 url resolver 的可能, 拒绝混淆
            urlresolvers.set_urlconf(None)

        try:
            # Apply response middleware, regardless of the response 调用响应中间件
            for middleware_method in self._response_middleware:
                response = middleware_method(request, response)

            response = self.apply_response_fixes(request, response)

        except: # Any exception should be gathered and handled
            signals.got_request_exception.send(sender=self.__class__, request=request)
            response = self.handle_uncaught_exception(request, resolver, sys.exc_info())

        return response

    def handle_uncaught_exception(self, request, resolver, exc_info):
        """
        处理未能捕捉的错误

        Processing for any otherwise uncaught exceptions (those that will
        generate HTTP 500 responses). Can be overridden by subclasses who want
        customised 500 handling. 子类中可以重写 500 状态的处理

        Be *very* careful when overriding this because the error could be
        caused by anything, so assuming something like the database is always
        available would be an error.
        """
        if settings.DEBUG_PROPAGATE_EXCEPTIONS:
            raise

        logger.error('Internal Server Error: %s', request.path,
            exc_info=exc_info,
            extra={
                'status_code': 500,
                'request': request
            }
        )

        调试模式特殊处理
        if settings.DEBUG:
            return debug.technical_500_response(request, *exc_info)

        # If Http500 handler is not installed, re-raise last exception 如果http500 处理器都没有安装, 可能会崩溃
        if resolver.urlconf_module is None:
            six.reraise(*exc_info)

        # Return an HttpResponse that displays a friendly error message.
        #这是自定义的 500 处理器
        callback, param_dict = resolver.resolve500()
        return callback(request, **param_dict)

    def apply_response_fixes(self, request, response):
        """
        Applies each of the functions in self.response_fixes to the request and
        response, modifying the response in the process. Returns the new
        response.
        """
        for func in self.response_fixes:
            response = func(request, response)
        return response


def get_path_info(environ):
    """
    将 HTTP 请求的路径转换成 unicode
    Returns the HTTP request's PATH_INFO as a unicode string.
    """
    path_info = environ.get('PATH_INFO', str('/'))
    # Under Python 3, strings in environ are decoded with ISO-8859-1;
    # re-encode to recover the original bytestring provided by the webserver.
    if six.PY3:
        path_info = path_info.encode('iso-8859-1')
    # It'd be better to implement URI-to-IRI decoding, see #19508.
    return path_info.decode('utf-8')


def get_script_name(environ):
    """
    返回 HTTP 请求的脚本
    Returns the equivalent of the HTTP request's SCRIPT_NAME environment
    variable. If Apache mod_rewrite has been used, returns what would have been
    the script name prior to any rewriting (so it's the script name as seen
    from the client's perspective), unless the FORCE_SCRIPT_NAME setting is
    set (to anything).
    """
    if settings.FORCE_SCRIPT_NAME is not None:
        return force_text(settings.FORCE_SCRIPT_NAME)

    # If Apache's mod_rewrite had a whack at the URL, Apache set either
    # SCRIPT_URL or REDIRECT_URL to the full resource URL before applying any
    # rewrites. Unfortunately not every Web server (lighttpd!) passes this
    # information through all the time, so FORCE_SCRIPT_NAME, above, is still
    # needed.
    script_url = environ.get('SCRIPT_URL', environ.get('REDIRECT_URL', str('')))
    if script_url:
        script_name = script_url[:-len(environ.get('PATH_INFO', str('')))]
    else:
        script_name = environ.get('SCRIPT_NAME', str(''))
    # Under Python 3, strings in environ are decoded with ISO-8859-1;
    # re-encode to recover the original bytestring provided by the webserver.
    if six.PY3:
        script_name = script_name.encode('iso-8859-1')
    # It'd be better to implement URI-to-IRI decoding, see #19508.
    return script_name.decode('utf-8')
