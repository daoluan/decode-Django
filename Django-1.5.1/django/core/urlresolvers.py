"""
This module converts requested URLs to callback view functions.

RegexURLResolver is the main class here. Its resolve() method takes a URL (as
a string) and returns a tuple in this format:

    (view_function, function_args, function_kwargs)
"""
from __future__ import unicode_literals

import re
from threading import local

from django.http import Http404
from django.core.exceptions import ImproperlyConfigured, ViewDoesNotExist
from django.utils.datastructures import MultiValueDict
from django.utils.encoding import force_str, force_text, iri_to_uri
from django.utils.functional import memoize, lazy
from django.utils.http import urlquote
from django.utils.importlib import import_module
from django.utils.module_loading import module_has_submodule
from django.utils.regex_helper import normalize
from django.utils import six
from django.utils.translation import get_language

_resolver_cache = {} # Maps URLconf modules to RegexURLResolver instances. URLconf RegexURLResolver映射

_ns_resolver_cache = {} # Maps namespaces to RegexURLResolver instances.   namespaces RegexURLResolver 映射

_callable_cache = {} # Maps view and url pattern names to their view functions. view url pattern view functions 映射

# SCRIPT_NAME prefixes for each thread are stored here. If there's no entry for
# the current thread (which is the only one we ever access), it is assumed to
# be empty.
_prefixes = local() # 该线程全局变量

# Overridden URLconfs for each thread are stored here.
_urlconfs = local() # 该线程全局变量

# 处理器匹配结果类, 当匹配成功的时候会实例化
class ResolverMatch(object):
    def __init__(self, func, args, kwargs, url_name=None, app_name=None, namespaces=None):
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.app_name = app_name

        if namespaces:
            self.namespaces = [x for x in namespaces if x]
        else:
            self.namespaces = []

        if not url_name:
            if not hasattr(func, '__name__'):
                # An instance of a callable class
                url_name = '.'.join([func.__class__.__module__, func.__class__.__name__])
            else:
                # A function
                url_name = '.'.join([func.__module__, func.__name__])

        self.url_name = url_name

    @property
    def namespace(self):
        return ':'.join(self.namespaces)

    @property
    def view_name(self):
        return ':'.join([ x for x in [ self.namespace, self.url_name ]  if x ])

    def __getitem__(self, index):
        # (self.func, self.args, self.kwargs) 012
        return (self.func, self.args, self.kwargs)[index]

    def __repr__(self):
        return "ResolverMatch(func=%s, args=%s, kwargs=%s, url_name='%s', app_name='%s', namespace='%s')" % (
            self.func, self.args, self.kwargs, self.url_name, self.app_name, self.namespace)

class Resolver404(Http404):
    pass

class NoReverseMatch(Exception):
    # Don't make this raise an error when used in a template.
    silent_variable_failure = True

def get_callable(lookup_view, can_fail=False):
    """
    将函数的字符串版本转换成一个可调用的函数, 主要是从各个 app 中搜索相关的 view

    Convert a string version of a function name to the callable object.

    If the lookup_view is not an import path, it is assumed to be a URL pattern
    label and the original string is returned.

    If can_fail is True, lookup_view might be a URL pattern label, so errors
    during the import fail and the string is returned.
    """
    if not callable(lookup_view):
        # def get_mod_func(callback):
        # # Converts 'django.views.news.stories.story_detail' to
        # # ['django.views.news.stories', 'story_detail']
        mod_name, func_name = get_mod_func(lookup_view)

        if func_name == '':
            return lookup_view

        try:
            mod = import_module(mod_name) 尝试导入模块
        except ImportError:
            parentmod, submod = get_mod_func(mod_name)

            if (not can_fail and submod != '' and
                    not module_has_submodule(import_module(parentmod), submod)):
                raise ViewDoesNotExist(
                    "Could not import %s. Parent module %s does not exist." %
                    (lookup_view, mod_name))

            if not can_fail:
                raise

        else:
            try:
                lookup_view = getattr(mod, func_name)

                # 不能调用, 异常
                if not callable(lookup_view):
                    raise ViewDoesNotExist(
                        "Could not import %s.%s. View is not callable." %
                        (mod_name, func_name))

            except AttributeError:
                if not can_fail:
                    raise ViewDoesNotExist(
                        "Could not import %s. View does not exist in module %s." %
                        (lookup_view, mod_name))
    return lookup_view

get_callable = memoize(get_callable, _callable_cache, 1)

# 获取 URL 匹配处理器
def get_resolver(urlconf):
    # 如果为空, 导入 settings 中的 ROOT_URLCONF
    if urlconf is None:
        from django.conf import settings
        urlconf = settings.ROOT_URLCONF
    return RegexURLResolver(r'^/', urlconf)

get_resolver = memoize(get_resolver, _resolver_cache, 1)

def get_ns_resolver(ns_pattern, resolver):
    # Build a namespaced resolver for the given parent urlconf pattern.
    # This makes it possible to have captured parameters in the parent
    # urlconf pattern.
    ns_resolver = RegexURLResolver(ns_pattern,
                                          resolver.url_patterns)
    return RegexURLResolver(r'^/', [ns_resolver])

get_ns_resolver = memoize(get_ns_resolver, _ns_resolver_cache, 2)

def get_mod_func(callback):
    # Converts 'django.views.news.stories.story_detail' to
    # ['django.views.news.stories', 'story_detail']
    try:
        dot = callback.rindex('.')
    except ValueError:
        return callback, ''
    return callback[:dot], callback[dot+1:]

class LocaleRegexProvider(object):
    """
    区域相关的正则表达式, 会根据地区的不同返回不同的正则表达式, django 内部维护. 在一般英文的 url 中用处不大.

    A mixin to provide a default regex property which can vary by active
    language.
    """
    def __init__(self, regex):
        # regex is either a string representing a regular expression, or a
        # translatable string (using ugettext_lazy) representing a regular
        # expression.
        self._regex = regex
        self._regex_dict = {}

    @property
    def regex(self):
        """
        Returns a compiled regular expression, depending upon the activated
        language-code.
        """
        language_code = get_language()

        if language_code not in self._regex_dict:
            if isinstance(self._regex, six.string_types):
                regex = self._regex
            else:
                regex = force_text(self._regex)
            try:
                compiled_regex = re.compile(regex, re.UNICODE)
            except re.error as e:
                raise ImproperlyConfigured(
                    '"%s" is not a valid regular expression: %s' %
                    (regex, six.text_type(e)))

            self._regex_dict[language_code] = compiled_regex

        return self._regex_dict[language_code]

# URL 正则匹配类, 用于执行正则匹配
class RegexURLPattern(LocaleRegexProvider):
    def __init__(self, regex, callback, default_args=None, name=None):
        LocaleRegexProvider.__init__(self, regex)
        # callback is either a string like 'foo.views.news.stories.story_detail'
        # which represents the path to a module and a view function name, or a
        # callable object (view).
        if callable(callback):
            self._callback = callback #设置回调
        else:
            self._callback = None
            self._callback_str = callback #如果不可调用设置回调的字符串版本

        self.default_args = default_args or {}
        self.name = name

    def __repr__(self):
        return force_str('<%s %s %s>' % (self.__class__.__name__, self.name, self.regex.pattern))

    def add_prefix(self, prefix):
        """
        Adds the prefix string to a string-based callback.
        """
        if not prefix or not hasattr(self, '_callback_str'):
            return

        self._callback_str = prefix + '.' + self._callback_str

    # 执行正则匹配
    def resolve(self, path):
        match = self.regex.search(path) # 搜索
        if match:
            # If there are any named groups, use those as kwargs, ignoring
            # non-named groups. Otherwise, pass all non-named arguments as
            # positional arguments.
            # match.groupdict() 返回正则表达式中匹配的变量以及其值, 需要了解 python 中正则表达式的使用
            kwargs = match.groupdict()
            if kwargs:
                args = ()
            else:
                args = match.groups()

            # In both cases, pass any extra_kwargs as **kwargs.
            kwargs.update(self.default_args)

            # 成功, 返回匹配结果类; 否则返回 None
            return ResolverMatch(self.callback, args, kwargs, self.name)

    # 对 callback 进行修饰, 如果 self._callback 不是一个可调用的对象, 则可能还是一个字符串, 需要解析得到可调用的对象
    @property
    def callback(self):
        if self._callback is not None:
            return self._callback

        self._callback = get_callable(self._callback_str)
        return self._callback

# URL 正则处理器类, 需要和 RegexURLPattert URL正则匹配类 区分开来:
# 实际上, RegexURLResolver 中有包含 RegexURLPattern 实例和 RegexURLResolver 实例的集合.
class RegexURLResolver(LocaleRegexProvider):
    def __init__(self, regex, urlconf_name, default_kwargs=None, app_name=None, namespace=None):
        LocaleRegexProvider.__init__(self, regex)

        # urlconf_name is a string representing the module containing URLconfs.
        # url 配置文件所在的文件
        self.urlconf_name = urlconf_name

        if not isinstance(urlconf_name, six.string_types):
            self._urlconf_module = self.urlconf_name

        self.callback = None
        self.default_kwargs = default_kwargs or {}
        self.namespace = namespace
        self.app_name = app_name
        self._reverse_dict = {}
        self._namespace_dict = {}
        self._app_dict = {}

    def __repr__(self):
        if isinstance(self.urlconf_name, list) and len(self.urlconf_name):
            # Don't bother to output the whole list, it can be huge
            urlconf_repr = '<%s list>' % self.urlconf_name[0].__class__.__name__
        else:
            urlconf_repr = repr(self.urlconf_name)
        return str('<%s %s (%s:%s) %s>') % (
            self.__class__.__name__, urlconf_repr, self.app_name,
            self.namespace, self.regex.pattern)

    def _populate(self):
        lookups = MultiValueDict() # key-list
        namespaces = {}
        apps = {}
        language_code = get_language()

        for pattern in reversed(self.url_patterns): # def url_patterns(self): 从模块中加载 urlpatterns

            # pattern 是 RegexURLPattern 类型
            p_pattern = pattern.regex.pattern

            if p_pattern.startswith('^'):
                p_pattern = p_pattern[1:]

            if isinstance(pattern, RegexURLResolver): # 如果就是本身类 RegexURLResolver 的一个实例
                if pattern.namespace:
                    namespaces[pattern.namespace] = (p_pattern, pattern)

                    if pattern.app_name:                    # 设置为空
                        apps.setdefault(pattern.app_name, [] ).append(pattern.namespace)

                else:  # 如果就是本身类 RegexURLResolver 的一个实例, 但不存在命名空间
                    parent = normalize(pattern.regex.pattern)

                    for name in pattern.reverse_dict:

                        for matches, pat, defaults in pattern.reverse_dict.getlist(name):

                            new_matches = []

                            for piece, p_args in parent:
                                new_matches.extend([(piece + suffix, p_args + args) for (suffix, args) in matches])
                            lookups.appendlist(name, (new_matches, p_pattern + pat, dict(defaults, **pattern.default_kwargs)))

                    for namespace, (prefix, sub_pattern) in pattern.namespace_dict.items():
                        namespaces[namespace] = (p_pattern + prefix, sub_pattern)

                    for app_name, namespace_list in pattern.app_dict.items():
                        apps.setdefault(app_name, []).extend(namespace_list)

            else:
                bits = normalize(p_pattern)
                lookups.appendlist(pattern.callback, (bits, p_pattern, pattern.default_args))
                if pattern.name is not None:
                    lookups.appendlist(pattern.name, (bits, p_pattern, pattern.default_args))

        self._reverse_dict[language_code] = lookups
        self._namespace_dict[language_code] = namespaces
        self._app_dict[language_code] = apps

    @property
    def reverse_dict(self):
        language_code = get_language()
        if language_code not in self._reverse_dict:
            self._populate()
        return self._reverse_dict[language_code]

    @property
    def namespace_dict(self):
        language_code = get_language()
        if language_code not in self._namespace_dict:
            self._populate()
        return self._namespace_dict[language_code]

    @property
    def app_dict(self):
        language_code = get_language()

        if language_code not in self._app_dict:
            self._populate()

        return self._app_dict[language_code]

    # 最关键的函数
    def resolve(self, path):

        tried = []

        # regex 在 RegexURLResolver 中表示前缀
        match = self.regex.search(path)

        if match:
            # 去除前缀
            new_path = path[match.end():]

            for pattern in self.url_patterns: # 穷举所有的 url pattern
                # pattern 是 RegexURLPattern 实例
                try:

"""在 RegexURLResolver.resolve() 中的一句: sub_match = pattern.resolve(new_path) 最为关键.
从上面 patterns() 函数的作用知道, pattern 可以是 RegexURLPattern 对象或者 RegexURLResolver 对象. 当为 RegexURLResolver 对象的时候, 就是启动子 url 匹配处理器, 于是又回到了上面.

RegexURLPattern 和 RegexURLResolver 都有一个 resolve() 函数, 所以, 下面的一句 resolve() 调用, 可以是调用 RegexURLPattern.resolve() 或者 RegexURLResolver.resolve()"""

                    # 返回 ResolverMatch 实例
                    sub_match = pattern.resolve(new_path)

                except Resolver404 as e:
                    # 搜集已经尝试过的匹配器, 在出错的页面中会显示错误信息
                    sub_tried = e.args[0].get('tried')

                    if sub_tried is not None:
                        tried.extend([[pattern] + t for t in sub_tried])
                    else:
                        tried.append([pattern])
                else:
                    # 是否成功匹配
                    if sub_match:
                        # match.groupdict()
                        # Return a dictionary containing all the named subgroups of the match,
                        # keyed by the subgroup name.

                        # 如果在 urls.py 的正则表达式中使用了变量, match.groupdict() 返回即为变量和值.
                        sub_match_dict = dict(match.groupdict(), **self.default_kwargs)

                        sub_match_dict.update(sub_match.kwargs)

                        # 返回 ResolverMatch 对象, 如你所知, 得到此对象将可以执行真正的逻辑操作, 即 views.py 内定义的函数.
                        return ResolverMatch(sub_match.func,
                            sub_match.args, sub_match_dict,
                            sub_match.url_name, self.app_name or sub_match.app_name,
                            [self.namespace] + sub_match.namespaces)

                    tried.append([pattern])

            # 如果没有匹配成功的项目, 将异常
            raise Resolver404({'tried': tried, 'path': new_path})

        raise Resolver404({'path' : path})

    # 修饰 urlconf_module, 返回 self._urlconf_module, 即 urlpatterns 变量所在的文件
    @property
    def urlconf_module(self):
        try:
            return self._urlconf_module
        except AttributeError:
            self._urlconf_module = import_module(self.urlconf_name)
            return self._urlconf_module

    # 返回指定文件中的 urlpatterns 变量
    @property
    def url_patterns(self):
        patterns = getattr(self.urlconf_module, "urlpatterns", self.urlconf_module)
        try:
            iter(patterns) # 是否可以迭代
        except TypeError:
            raise ImproperlyConfigured("The included urlconf %s doesn't have any patterns in it" % self.urlconf_name)

        # patterns 实际上是 RegexURLPattern 对象和 RegexURLResolver 对象的集合
        return patterns

    def _resolve_special(self, view_type):
        callback = getattr(self.urlconf_module, 'handler%s' % view_type, None)
        if not callback:
            # No handler specified in file; use default
            # Lazy import, since django.urls imports this file
            from django.conf import urls
            callback = getattr(urls, 'handler%s' % view_type)
        return get_callable(callback), {}

    def resolve403(self):
        return self._resolve_special('403')

    def resolve404(self):
        return self._resolve_special('404')

    def resolve500(self):
        return self._resolve_special('500')

    def reverse(self, lookup_view, *args, **kwargs):
        return self._reverse_with_prefix(lookup_view, '', *args, **kwargs)

    def _reverse_with_prefix(self, lookup_view, _prefix, *args, **kwargs):
        if args and kwargs:
            raise ValueError("Don't mix *args and **kwargs in call to reverse()!")
        try:
            lookup_view = get_callable(lookup_view, True)
        except (ImportError, AttributeError) as e:
            raise NoReverseMatch("Error importing '%s': %s." % (lookup_view, e))
        possibilities = self.reverse_dict.getlist(lookup_view)

        prefix_norm, prefix_args = normalize(urlquote(_prefix))[0]
        for possibility, pattern, defaults in possibilities:
            for result, params in possibility:
                if args:
                    if len(args) != len(params) + len(prefix_args):
                        continue
                    unicode_args = [force_text(val) for val in args]
                    candidate = (prefix_norm + result) % dict(zip(prefix_args + params, unicode_args))
                else:
                    if set(kwargs.keys()) | set(defaults.keys()) != set(params) | set(defaults.keys()) | set(prefix_args):
                        continue
                    matches = True
                    for k, v in defaults.items():
                        if kwargs.get(k, v) != v:
                            matches = False
                            break
                    if not matches:
                        continue
                    unicode_kwargs = dict([(k, force_text(v)) for (k, v) in kwargs.items()])
                    candidate = (prefix_norm.replace('%', '%%') + result) % unicode_kwargs
                if re.search('^%s%s' % (prefix_norm, pattern), candidate, re.UNICODE):
                    return candidate
        # lookup_view can be URL label, or dotted path, or callable, Any of
        # these can be passed in at the top, but callables are not friendly in
        # error messages.
        m = getattr(lookup_view, '__module__', None)
        n = getattr(lookup_view, '__name__', None)
        if m is not None and n is not None:
            lookup_view_s = "%s.%s" % (m, n)
        else:
            lookup_view_s = lookup_view
        raise NoReverseMatch("Reverse for '%s' with arguments '%s' and keyword "
                "arguments '%s' not found." % (lookup_view_s, args, kwargs))

class LocaleRegexURLResolver(RegexURLResolver):
    """
    A URL resolver that always matches the active language code as URL prefix.

    Rather than taking a regex argument, we just override the ``regex``
    function to always return the active language-code as regex.
    """
    def __init__(self, urlconf_name, default_kwargs=None, app_name=None, namespace=None):
        super(LocaleRegexURLResolver, self).__init__(
            None, urlconf_name, default_kwargs, app_name, namespace)

    @property
    def regex(self):
        language_code = get_language()
        if language_code not in self._regex_dict:
            regex_compiled = re.compile('^%s/' % language_code, re.UNICODE)
            self._regex_dict[language_code] = regex_compiled
        return self._regex_dict[language_code]

# path: url
# urlconf: urlpatterns 所在的文件
def resolve(path, urlconf=None):
    # 如果没有指定 urlconf, 调用 get_urlconf() 获取
    if urlconf is None:
        urlconf = get_urlconf()

    # get_resolver() 会返回 RegexURLResolver 实例, 即 url 匹配处理器
    # 并调用 RegexURLResolver.resolve(path) 启动解析过程
    return get_resolver(urlconf).resolve(path)

def reverse(viewname, urlconf=None, args=None, kwargs=None, prefix=None, current_app=None):
    if urlconf is None:
        urlconf = get_urlconf()
    resolver = get_resolver(urlconf)
    args = args or []
    kwargs = kwargs or {}

    if prefix is None:
        prefix = get_script_prefix()

    if not isinstance(viewname, six.string_types):
        view = viewname
    else:
        parts = viewname.split(':')
        parts.reverse()
        view = parts[0]
        path = parts[1:]

        resolved_path = []
        ns_pattern = ''
        while path:
            ns = path.pop()

            # Lookup the name to see if it could be an app identifier
            try:
                app_list = resolver.app_dict[ns]
                # Yes! Path part matches an app in the current Resolver
                if current_app and current_app in app_list:
                    # If we are reversing for a particular app,
                    # use that namespace
                    ns = current_app
                elif ns not in app_list:
                    # The name isn't shared by one of the instances
                    # (i.e., the default) so just pick the first instance
                    # as the default.
                    ns = app_list[0]
            except KeyError:
                pass

            try:
                extra, resolver = resolver.namespace_dict[ns]
                resolved_path.append(ns)
                ns_pattern = ns_pattern + extra
            except KeyError as key:
                if resolved_path:
                    raise NoReverseMatch(
                        "%s is not a registered namespace inside '%s'" %
                        (key, ':'.join(resolved_path)))
                else:
                    raise NoReverseMatch("%s is not a registered namespace" %
                                         key)
        if ns_pattern:
            resolver = get_ns_resolver(ns_pattern, resolver)

    return iri_to_uri(resolver._reverse_with_prefix(view, prefix, *args, **kwargs))

reverse_lazy = lazy(reverse, str)

def clear_url_caches():
    global _resolver_cache
    global _ns_resolver_cache
    global _callable_cache
    _resolver_cache.clear()
    _ns_resolver_cache.clear()
    _callable_cache.clear()

def set_script_prefix(prefix):
    """
    Sets the script prefix for the current thread.
    """
    if not prefix.endswith('/'):
        prefix += '/'
    _prefixes.value = prefix

def get_script_prefix():
    """
    Returns the currently active script prefix. Useful for client code that
    wishes to construct their own URLs manually (although accessing the request
    instance is normally going to be a lot cleaner).
    """
    return getattr(_prefixes, "value", '/')

def set_urlconf(urlconf_name):
    """
    设置当前线程的 urlconf

    Sets the URLconf for the current thread (overriding the default one in
    settings). Set to None to revert back to the default.
    """
    if urlconf_name:
        # _urlconfs 是该线程的全局变量
        _urlconfs.value = urlconf_name
    else:
        if hasattr(_urlconfs, "value"):
            del _urlconfs.value

def get_urlconf(default=None):
    """
    全局变量 _urlconfs 是一个 python 文件, 读取
    Returns the root URLconf to use for the current thread if it has been
    changed from the default one.
    """
    return getattr(_urlconfs, "value", default)

def is_valid_path(path, urlconf=None):
    """
    Returns True if the given path resolves against the default URL resolver,
    False otherwise.

    This is a convenience method to make working with "is this a match?" cases
    easier, avoiding unnecessarily indented try...except blocks.
    """
    try:
        resolve(path, urlconf)
        return True
    except Resolver404:
        return False
