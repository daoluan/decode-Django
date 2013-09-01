from __future__ import unicode_literals

import logging
from functools import update_wrapper

from django import http
from django.core.exceptions import ImproperlyConfigured
from django.template.response import TemplateResponse
from django.utils.decorators import classonlymethod
from django.utils import six

logger = logging.getLogger('django.request')

不懂
class ContextMixin(object):
    """
    A default context mix in that passes the keyword arguments received by
    get_context_data as the template context.
    """
    返回 kwargs, 在 view 不在 kwargs 字典中的时候, 会设置 kwargs['view'] = self
    def get_context_data(self, **kwargs):
        if 'view' not in kwargs:
            kwargs['view'] = self
        return kwargs

内部通用视图的父类
class View(object):
    """
    所有 view 类的父类
    Intentionally simple parent class for all views. Only implements
    dispatch-by-method and simple sanity checking. 清晰的检测
    """

    http_method_names = ['get', 'post', 'put', 'delete', 'head', 'options', 'trace'] 所有的 http method

    def __init__(self, **kwargs):
        """
        Constructor. Called in the URLconf; 
        can contain helpful extra keyword arguments, and other things.
        """
        # Go through keyword arguments, and either save their values to our
        # instance, or raise an error.
        聪明的地方在这里, 如果需要色湖之多余的属性, 可以直接放入, 这里会设置
        for key, value in six.iteritems(kwargs):
            setattr(self, key, value)

    @classonlymethod只能被class调用
    def as_view(cls, **initkwargs):
        """
        Main entry point for a request-response process.  请求应答入口
        """
        # sanitize keyword arguments
        for key in initkwargs:

            不懂
            if key in cls.http_method_names:
                raise TypeError("You tried to pass in the %s method name as a "
                                "keyword argument to %s(). Don't do that."
                                % (key, cls.__name__))

            if not hasattr(cls, key):
                raise TypeError("%s() received an invalid keyword %r. as_view "
                                "only accepts arguments that are already "
                                "attributes of the class." % (cls.__name__, key))

        def view(request, *args, **kwargs):
            self = cls(**initkwargs)

            if hasattr(self, 'get') and not hasattr(self, 'head'):
                self.head = self.get 如果没有 head 就用 get 替代

            self.request = request
            self.args = args
            self.kwargs = kwargs

            #dispatch 在这里调用
            return self.dispatch(request, *args, **kwargs) 

        # take name and docstring from class
        让 cls 修饰 view
                                                cls 可能是一个 templateview 类
        update_wrapper(view, cls, updated=())  这句好似已经无效了

        # and possible attributes set by decorators
        # like csrf_exempt from dispatch
        update_wrapper(view, cls.dispatch, assigned=())
        return view

    这是调度器, 应该所有的请求都会经由这里处理
    def dispatch(self, request, *args, **kwargs):
        尽可能调用正确的方法, 如果没有匹配的方法, 交给错误处理器
        # Try to dispatch to the right method; if a method doesn't exist,
        # defer to the error handler. Also defer to the error handler if the
        # request method isn't on the approved list.

        #如果是  'get', 'post', 'put', 'delete', 'head', 'options', 'trace' 之一
        if request.method.lower() in self.http_method_names: 
            handler = getattr(self, request.method.lower(), self.http_method_not_allowed) 获取 get post head 等方法
        else:
            handler = self.http_method_not_allowed 这里是其他的 http 命令, django 会产生错误信息

        return handler(request, *args, **kwargs) 参数放入

    def http_method_not_allowed(self, request, *args, **kwargs):
        logger.warning('Method Not Allowed (%s): %s', request.method, request.path, 产生警告
            extra={
                'status_code': 405,
                'request': self.request
            }
        )
        return http.HttpResponseNotAllowed(self._allowed_methods())

    def options(self, request, *args, **kwargs):
        """
        HTTP options 命令
        Handles responding to requests for the OPTIONS HTTP verb.
        """
        response = http.HttpResponse()
        response['Allow'] = ', '.join(self._allowed_methods())
        response['Content-Length'] = '0'
        return response

    def _allowed_methods(self): 产生允许的 http 命令
        return [m.upper() for m in self.http_method_names if hasattr(self, m)]

模板响应混入类, 可以渲染模板
class TemplateResponseMixin(object):
    """
    A mixin that can be used to render a template. 渲染模板
    """
    template_name = None 可以自定义模板
    # from django.template.response import TemplateResponse
    response_class = TemplateResponse
    content_type = None

    def render_to_response(self, context, **response_kwargs):
        """
        Returns a response, using the `response_class` for this
        view, with a template rendered with the given context.

        If any keyword arguments are provided, they will be
        passed to the constructor of the response class.
        """
        response_kwargs.setdefault('content_type', self.content_type)

    # from django.template.response import TemplateResponse

        return self.response_class(
            request = self.request,
            template = self.get_template_names(), 子类可以重写
            context = context,
            **response_kwargs
        )

    def get_template_names(self):
        """
        Returns a list of template names to be used for the request. Must return
        a list. May not be called if render_to_response is overridden.
        """
        if self.template_name is None:
            raise ImproperlyConfigured(
                "TemplateResponseMixin requires either a definition of "
                "'template_name' or an implementation of 'get_template_names()'")
        else:
            return [self.template_name]


class TemplateView(TemplateResponseMixin, ContextMixin, View):
    渲染模板的 view
    """
    A view that renders a template.  This view will also pass into the context
    any keyword arguments passed by the url conf.
    """
    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        return self.render_to_response(context)


class RedirectView(View):
    """
    具有重定向功能的的 view
    A view that provides a redirect on any GET request.
    """
    permanent = True 永久重定向
    url = None
    query_string = False

    def get_redirect_url(self, **kwargs):
        """
        Return the URL redirect to. Keyword arguments from the
        URL pattern match generating the redirect request
        are provided as kwargs to this method.
        """
        if self.url:
            url = self.url % kwargs
            args = self.request.META.get('QUERY_STRING', '')
            if args and self.query_string:
                url = "%s?%s" % (url, args)
            return url 把 query_string 放在后面, 返回 
        else:
            return None

    def get(self, request, *args, **kwargs):
        url = self.get_redirect_url(**kwargs) 

        if url:
            if self.permanent:
                return http.HttpResponsePermanentRedirect(url)
            else:
                return http.HttpResponseRedirect(url)
        else:
            警告
            logger.warning('Gone: %s', self.request.path,
                        extra={
                            'status_code': 410,
                            'request': self.request
                        })
            return http.HttpResponseGone()

    def head(self, request, *args, **kwargs):
        return self.get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self.get(request, *args, **kwargs)

    def options(self, request, *args, **kwargs):
        return self.get(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        return self.get(request, *args, **kwargs)

    def put(self, request, *args, **kwargs):
        return self.get(request, *args, **kwargs)
