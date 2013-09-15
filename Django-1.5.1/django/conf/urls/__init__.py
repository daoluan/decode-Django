from django.core.urlresolvers import (RegexURLPattern,
    RegexURLResolver, LocaleRegexURLResolver)
from django.core.exceptions import ImproperlyConfigured
from django.utils.importlib import import_module
from django.utils import six

# import * 引入的时候只导入一下六个方法或类
__all__ = ['handler403', 'handler404', 'handler500', 'include', 'patterns', 'url']

handler403 = 'django.views.defaults.permission_denied'
handler404 = 'django.views.defaults.page_not_found'
handler500 = 'django.views.defaults.server_error'

# url 里面可以用 incude 函数
def include(arg, namespace=None, app_name=None):
    if isinstance(arg, tuple):
        # callable returning a namespace hint
        if namespace:
            raise ImproperlyConfigured('Cannot override the namespace for a dynamic module that provides a namespace')

        # 获取 urlconf 模块文件, 应用名, 命名空间
        urlconf_module, app_name, namespace = arg
    else:
        # No namespace hint - use manually provided namespace
        urlconf_module = arg

    if isinstance(urlconf_module, six.string_types):
        # 尝试导入模块
        urlconf_module = import_module(urlconf_module)

    # 在 urlconf_module 中导入 urlpatterns
    # 在 urlconf_module 中肯定会有 urlpatterns 这个变量
    patterns = getattr(urlconf_module, 'urlpatterns', urlconf_module)

    # Make sure we can iterate through the patterns (without this, some
    # testcases will break).
    if isinstance(patterns, (list, tuple)):
        for url_pattern in patterns:
            # Test if the LocaleRegexURLResolver is used within the include;
            # this should throw an error since this is not allowed!
            if isinstance(url_pattern, LocaleRegexURLResolver):
                raise ImproperlyConfigured(
                    'Using i18n_patterns in an included URLconf is not allowed.')

    # 返回模块, app 名 ,命名空间
    return (urlconf_module, app_name, namespace)

def patterns(prefix, *args): 特意留一个 prefix
    pattern_list = []
    for t in args:
        if isinstance(t, (list, tuple)):
            t = url(prefix=prefix, *t) 自动转换

        elif isinstance(t, RegexURLPattern):
            t.add_prefix(prefix)

        pattern_list.append(t)

    # 返回 RegexURLResolver 或者 RegexURLPattern 对象的列表
    return pattern_list

# url 函数
def url(regex, view, kwargs=None, name=None, prefix=''):
    if isinstance(view, (list,tuple)): 如果是 list 或者 tuple
        # For include(...) processing. 处理包含 include(...)
        urlconf_module, app_name, namespace = view

        # 此处返回 RegexURLResolver, 区分下面返回 RegexURLPattern
        return RegexURLResolver(regex, urlconf_module, kwargs, app_name=app_name, namespace=namespace)
    else:
        if isinstance(view, six.string_types):
            if not view:
                raise ImproperlyConfigured('Empty URL pattern view name not permitted (for pattern %r)' % regex)
            if prefix:
                view = prefix + '.' + view

        # 返回 RegexURLPattern 的对象
        return RegexURLPattern(regex, view, kwargs, name)
    # 从上面可以获知, url 会返回 RegexURLResolver 或者 RegexURLPattern 对象
