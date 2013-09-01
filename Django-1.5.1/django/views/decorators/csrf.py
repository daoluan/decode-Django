import warnings

from django.middleware.csrf import CsrfViewMiddleware, get_token
from django.utils.decorators import decorator_from_middleware, available_attrs
from functools import wraps

csrf_protect = decorator_from_middleware(CsrfViewMiddleware)
csrf_protect.__name__ = "csrf_protect"
csrf_protect.__doc__ = """
This decorator adds CSRF protection in exactly the same way as
CsrfViewMiddleware, but it can be used on a per view basis.  Using both, or
using the decorator multiple times, is harmless and efficient.
"""

CSRF 中间件

class _EnsureCsrfToken(CsrfViewMiddleware): 继承自 CsrfViewMiddleware
    # We need this to behave just like the CsrfViewMiddleware, but not reject
    # requests.
    def _reject(self, request, reason):
        return None


requires_csrf_token = decorator_from_middleware(_EnsureCsrfToken)
requires_csrf_token.__name__ = 'requires_csrf_token'
requires_csrf_token.__doc__ = """
Use this decorator on views that need a correct csrf_token available to
RequestContext, but without the CSRF protection that csrf_protect
enforces.
"""


class _EnsureCsrfCookie(CsrfViewMiddleware): 继承自 CsrfViewMiddleware
    def _reject(self, request, reason):
        return None

    def process_view(self, request, callback, callback_args, callback_kwargs):
        retval = super(_EnsureCsrfCookie, self).process_view(request, callback, callback_args, callback_kwargs)
        # Forces process_response to send the cookie
        get_token(request)
        return retval


ensure_csrf_cookie = decorator_from_middleware(_EnsureCsrfCookie)
ensure_csrf_cookie.__name__ = 'ensure_csrf_cookie'
ensure_csrf_cookie.__doc__ = """
Use this decorator to ensure that a view sets a CSRF cookie, whether or not it
uses the csrf_token template tag, or the CsrfViewMiddleware is used.
"""


def csrf_response_exempt(view_func):
    """
    Modifies a view function so that its response is exempt 免除 from
    from the post-processing of the CSRF middleware.
    """
    warnings.warn("csrf_response_exempt is deprecated. It no longer performs a "
                  "function, and calls to it can be removed.", 好似被废弃的函数
                  DeprecationWarning)
    return view_func

好似被废弃的函数 csrf_response_exempt is deprecated
def csrf_view_exempt(view_func):
    """
    Marks a view function as being exempt from CSRF view protection.
    """
    warnings.warn("csrf_view_exempt is deprecated. Use csrf_exempt instead.",
                  DeprecationWarning)
    return csrf_exempt(view_func)

上面的两个函数被免除了, csrf_exempt 代替
def csrf_exempt(view_func): 
    """
    Marks a view function as being exempt from the CSRF view protection.
    """
    # We could just do view_func.csrf_exempt = True, but decorators
    # are nicer if they don't have side-effects, so we return a new
    # function.
    def wrapped_view(*args, **kwargs):
        return view_func(*args, **kwargs)

        在调用 wraps 的时候, 会返回 partial 对象, 它近似于下面的代码, partial 对象调用 partial(wrapped_view), 实际是返回 wrapped_view
        而在 wrapped_view 中会调用 view_func.
        因此, 当调用 view_func 的时候, 实际上调用的是 wrapped_view, wrapped_view 因为 wraps 会拷贝 view_func 中的所有属性
    wrapped_view.csrf_exempt = True
    """
    def partial(func, *args, **keywords):
        def newfunc(*fargs, **fkeywords):
            newkeywords = keywords.copy()
            newkeywords.update(fkeywords)
            return func(*(args + fargs), **newkeywords)
        newfunc.func = func  赋值 func 
        newfunc.args = args
        newfunc.keywords = keywords
        return newfunc
    """
    return wraps(view_func, assigned=available_attrs(view_func))(wrapped_view)
