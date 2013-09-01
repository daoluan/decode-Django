from __future__ import absolute_import, unicode_literals

import datetime
import time
import warnings
from email.header import Header
try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

from django.conf import settings
from django.core import signals
from django.core import signing
from django.core.exceptions import SuspiciousOperation
from django.http.cookie import SimpleCookie
from django.utils import six, timezone
from django.utils.encoding import force_bytes, iri_to_uri
from django.utils.http import cookie_date
from django.utils.six.moves import map


class BadHeaderError(ValueError):
    pass

基础类
class HttpResponseBase(six.Iterator):
    """
    An HTTP response base class with dictionary-accessed 字典访问 headers.

    This class doesn't handle content. It should not be used directly.
    这个类被处理内容, 要用他的子类
    Use the HttpResponse and StreamingHttpResponse subclasses instead.
    """

    status_code = 200

    def __init__(self, content_type=None, status=None, mimetype=None):
        # _headers is a mapping of the lower-case name to the original case of
        # the header (required for working with legacy systems) and the header
        # value. Both the name of the header and its value are ASCII strings.
        self._headers = {}
        self._charset = settings.DEFAULT_CHARSET
        self._closable_objects = []

        set by the handler.
        # This parameter is set by the handler. It's necessary to preserve the
        # historical behavior of request_finished.
        self._handler_class = None

        if mimetype:
            warnings.warn("Using mimetype keyword argument is deprecated, use"
                          " content_type instead", PendingDeprecationWarning)
            content_type = mimetype

        if not content_type:
            content_type = "%s; charset=%s" % (settings.DEFAULT_CONTENT_TYPE,
                    self._charset) 设置默认的 charset

        self.cookies = SimpleCookie() 应该返回要给空的 cookie dict

        if status:
            self.status_code = status

        self['Content-Type'] = content_type

    序列化 HTTP header, 得到一个字符串
    def serialize_headers(self):
        """HTTP headers as a bytestring."""
        headers = [
            ('%s: %s' % (key, value)).encode('us-ascii')
            for key, value in self._headers.values()
        ]
        return b'\r\n'.join(headers)

    if six.PY3:
        __bytes__ = serialize_headers
    else:
        __str__ = serialize_headers

    def _convert_to_charset(self, value, charset, mime_encode=False):
        """Converts headers key/value to ascii/latin1 native strings.

        `charset` must be 'ascii' or 'latin-1'. If `mime_encode` is True and
        `value` value can't be represented in the given charset, MIME-encoding
        is applied.
        """
        if not isinstance(value, (bytes, six.text_type)):
            value = str(value)

        try:
            if six.PY3: 这里是为了 compatibility
                if isinstance(value, str):  如果 value 是 str 类型, 那可以直接跳过, 如果是其他的类型, 要进行转换
                    # Ensure string is valid in given charset
                    value.encode(charset)  
                else:
                    # Convert bytestring using given charset
                    value = value.decode(charset)
            else:
                if isinstance(value, str):
                    # Ensure string is valid in given charset
                    value.decode(charset)
                else:
                    # Convert unicode string to given charset
                    value = value.encode(charset)

        except UnicodeError as e:
            if mime_encode:
                # Wrapping in str() is a workaround for #12422 under Python 2.
                value = str(Header(value, 'utf-8').encode())
            else:
                e.reason += ', HTTP response headers must be in %s format' % charset
                raise

        if str('\n') in value or str('\r') in value:
            raise BadHeaderError("Header values can't contain newlines (got %r)" % value)
        return value

    从下面可以看出, 对 response 的操作都是对其中 dict 的操作
    def __setitem__(self, header, value): 这是字典的动作
        header = self._convert_to_charset(header, 'ascii')
        value = self._convert_to_charset(value, 'latin1', mime_encode=True)
        self._headers[header.lower()] = (header, value) 其内部操作的是其成员变量 _headers

    def __delitem__(self, header):
        try:
            del self._headers[header.lower()]
        except KeyError:
            pass

    def __getitem__(self, header):
        return self._headers[header.lower()][1] 比如 response["cookie"]

    def __getstate__(self):
        # SimpleCookie is not pickeable with pickle.HIGHEST_PROTOCOL, so we
        # serialise to a string instead
        state = self.__dict__.copy()
        state['cookies'] = str(state['cookies'])
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.cookies = SimpleCookie(self.cookies)

    def has_header(self, header):
        """Case-insensitive check for a header."""
        return header.lower() in self._headers

    __contains__ = has_header

    def items(self):
        return self._headers.values() 返回 HTTP 头

    def get(self, header, alternate=None):
        return self._headers.get(header.lower(), (None, alternate))[1]

    def set_cookie(self, key, value='', max_age=None, expires=None, path='/',
                   domain=None, secure=False, httponly=False):
        """
        Sets a cookie.

        ``expires`` can be:
        - a string in the correct format,
        - a naive ``datetime.datetime`` object in UTC,
        - an aware ``datetime.datetime`` object in any time zone.
        If it is a ``datetime.datetime`` object then ``max_age`` will be calculated.

        """

        """
        指定了coolie的生存期，默认情况下coolie是暂时存在的，他们存储的值只在浏览器会话期间存在，当用户推出浏览器后这些值也会丢失，
        如果想让cookie存在一段时间，就要为expires属性设置为未来的一个过期日期。
        现在已经被max-age属性所取代，max-age用秒来设置cookie的生存期。
        """
        self.cookies[key] = value
        if expires is not None:
            if isinstance(expires, datetime.datetime):
                if timezone.is_aware(expires):
                    expires = timezone.make_naive(expires, timezone.utc)
                delta = expires - expires.utcnow()
                # Add one second so the date matches exactly (a fraction of
                # time gets lost between converting to a timedelta and
                # then the date string). 很讲究, 特别加上一秒时间以弥补其中花费的时间
                delta = delta + datetime.timedelta(seconds=1)
                # Just set max_age - the max_age logic will set expires.
                expires = None
                max_age = max(0, delta.days * 86400 + delta.seconds)
            else:
                SimpleCookie 的伟大之处
                self.cookies[key]['expires'] = expires 

        if max_age is not None:
            self.cookies[key]['max-age'] = max_age
            # IE requires expires, so set it if hasn't been already.
            if not expires:
                self.cookies[key]['expires'] = cookie_date(time.time() +
                                                           max_age) 如果没有 expires 就直接加上 max_age 已得到 expires

        if path is not None:
            self.cookies[key]['path'] = path

        域名
        if domain is not None:
            self.cookies[key]['domain'] = domain

        only used via HTTPS 在传输过程中, 会加密
        if secure:
            self.cookies[key]['secure'] = True

        仅仅用于 http 或者 https
        if httponly:
            self.cookies[key]['httponly'] = True

    def set_signed_cookie(self, key, value, salt='', **kwargs):
        value = signing.get_cookie_signer(salt=key + salt).sign(value)
        return self.set_cookie(key, value, **kwargs)

    def delete_cookie(self, key, path='/', domain=None):
        self.set_cookie(key, max_age=0, path=path, domain=domain,
                        expires='Thu, 01-Jan-1970 00:00:00 GMT')

    # Common methods used by subclasses

    def make_bytes(self, value):
        """Turn a value into a bytestring encoded in the output charset."""
        # Per PEP 3333, this response body must be bytes. To avoid returning
        # an instance of a subclass, this function returns `bytes(value)`.
        # This doesn't make a copy when `value` already contains bytes.

        # If content is already encoded (eg. gzip), assume bytes.
        if self.has_header('Content-Encoding'):
            return bytes(value)

        # Handle string types -- we can't rely on force_bytes here because:
        # - under Python 3 it attemps str conversion first
        # - when self._charset != 'utf-8' it re-encodes the content
        if isinstance(value, bytes):
            return bytes(value)
        if isinstance(value, six.text_type):
            return bytes(value.encode(self._charset))

        # Handle non-string types (#16494)
        return force_bytes(value, self._charset)

    def __iter__(self):
        return self

    def __next__(self):
        # Subclasses must define self._iterator for this function.
        return self.make_bytes(next(self._iterator)) 子类必须定义 self._iterator

    # These methods partially implement the file-like object interface.
    # See http://docs.python.org/lib/bltin-file-objects.html

    # The WSGI server must call this method upon completion of the request. 在请求结束的时候, 要调用这个函数
    # See http://blog.dscpl.com.au/2012/10/obligations-for-calling-close-on.html
    def close(self):
        for closable in self._closable_objects:
            try:
                closable.close()
            except Exception:
                pass
        signals.request_finished.send(sender=self._handler_class)

    def write(self, content):
        raise Exception("This %s instance is not writable" % self.__class__.__name__)

    def flush(self):
        pass

    def tell(self):
        raise Exception("This %s instance cannot tell its position" % self.__class__.__name__)


class HttpResponse(HttpResponseBase):
    """
    里面有一个 string, 不懂

    An HTTP response class with a string as content. 

    This content that can be read, appended to or replaced.
    """

    streaming = False

    def __init__(self, content='', *args, **kwargs):
        super(HttpResponse, self).__init__(*args, **kwargs)
        # Content is a bytestring. See the `content` property methods.
        self.content = content

    def serialize(self):
        返回整个 HTTP response 内容, 包括 HTTP header 和 content
        """Full HTTP message, including headers, as a bytestring."""
        return self.serialize_headers() + b'\r\n\r\n' + self.content 

    if six.PY3:
        __bytes__ = serialize
    else:
        __str__ = serialize

    不懂
    def _consume_content(self):
        # If the response was instantiated with an iterator, when its content
        # is accessed, the iterator is going be exhausted and the content
        # loaded in memory. At this point, it's better to abandon the original
        # iterator and save the content for later reuse. This is a temporary
        # solution. See the comment in __iter__ below for the long term plan.
        if self._base_content_is_iter:
            self.content = b''.join(self.make_bytes(e) for e in self._container)

    @property
    def content(self):
        self._consume_content() 不懂, 和 _consume_content 的操作一样
        return b''.join(self.make_bytes(e) for e in self._container) 把容器中的数据 join 起来

    @content.setter
    def content(self, value):
        if hasattr(value, '__iter__') and not isinstance(value, (bytes, six.string_types)):
            self._container = value
            self._base_content_is_iter = True
            if hasattr(value, 'close'):
                self._closable_objects.append(value)
        else:
            self._container = [value]
            self._base_content_is_iter = False

    def __iter__(self):
        # Raise a deprecation warning only if the content wasn't consumed yet,
        # because the response may be intended to be streamed.
        # Once the deprecation completes, iterators should be consumed upon
        # assignment rather than upon access. The _consume_content method
        # should be removed. See #6527.
        if self._base_content_is_iter:
            warnings.warn(
                'Creating streaming responses with `HttpResponse` is '
                'deprecated. Use `StreamingHttpResponse` instead '
                'if you need the streaming behavior.',
                PendingDeprecationWarning, stacklevel=2)
        if not hasattr(self, '_iterator'):
            self._iterator = iter(self._container)
        return self

    def write(self, content):
        self._consume_content()
        self._container.append(content)

    def tell(self):
        self._consume_content()
        return len(self.content)


class StreamingHttpResponse(HttpResponseBase):
    """
    A streaming HTTP response class with an iterator as content. 可迭代的内容

    This should only be iterated once, when the response is streamed to the
    client. However, it can be appended to or replaced with a new iterator
    that wraps the original content (or yields entirely new content).
    """

    streaming = True

    def __init__(self, streaming_content=(), *args, **kwargs):
        super(StreamingHttpResponse, self).__init__(*args, **kwargs)
        # `streaming_content` should be an iterable of bytestrings.
        # See the `streaming_content` property methods.
        self.streaming_content = streaming_content

    @property
    def content(self):
        raise AttributeError("This %s instance has no `content` attribute. "
            "Use `streaming_content` instead." % self.__class__.__name__)

    @property
    def streaming_content(self):
        return map(self.make_bytes, self._iterator)

    @streaming_content.setter
    def streaming_content(self, value):
        # Ensure we can never iterate on "value" more than once.
        self._iterator = iter(value)
        if hasattr(value, 'close'):
            self._closable_objects.append(value)


class CompatibleStreamingHttpResponse(StreamingHttpResponse):
    """
    This class maintains compatibility with middleware that doesn't know how
    to handle the content of a streaming response by exposing a `content`
    attribute that will consume and cache the content iterator when accessed.

    These responses will stream only if no middleware attempts to access the
    `content` attribute. Otherwise, they will behave like a regular response,
    and raise a `PendingDeprecationWarning`.
    """
    @property
    def content(self):
        warnings.warn(
            'Accessing the `content` attribute on a streaming response is '
            'deprecated. Use the `streaming_content` attribute instead.',
            PendingDeprecationWarning)
        content = b''.join(self)
        self.streaming_content = [content]
        return content

    @content.setter
    def content(self, content):
        warnings.warn(
            'Accessing the `content` attribute on a streaming response is '
            'deprecated. Use the `streaming_content` attribute instead.',
            PendingDeprecationWarning)
        self.streaming_content = [content]


class HttpResponseRedirectBase(HttpResponse):
    allowed_schemes = ['http', 'https', 'ftp']

    def __init__(self, redirect_to, *args, **kwargs):
        parsed = urlparse(redirect_to)
        if parsed.scheme and parsed.scheme not in self.allowed_schemes:
            raise SuspiciousOperation("Unsafe redirect to URL with protocol '%s'" % parsed.scheme)
        super(HttpResponseRedirectBase, self).__init__(*args, **kwargs)
        self['Location'] = iri_to_uri(redirect_to)


class HttpResponseRedirect(HttpResponseRedirectBase):
    status_code = 302


class HttpResponsePermanentRedirect(HttpResponseRedirectBase):
    status_code = 301


class HttpResponseNotModified(HttpResponse):
    status_code = 304

    def __init__(self, *args, **kwargs):
        super(HttpResponseNotModified, self).__init__(*args, **kwargs)
        del self['content-type']

    @HttpResponse.content.setter
    def content(self, value):
        if value:
            raise AttributeError("You cannot set content to a 304 (Not Modified) response")
        self._container = []
        self._base_content_is_iter = False


class HttpResponseBadRequest(HttpResponse):
    status_code = 400


class HttpResponseNotFound(HttpResponse):
    status_code = 404


class HttpResponseForbidden(HttpResponse):
    status_code = 403


class HttpResponseNotAllowed(HttpResponse):
    status_code = 405

    def __init__(self, permitted_methods, *args, **kwargs):
        super(HttpResponseNotAllowed, self).__init__(*args, **kwargs)
        self['Allow'] = ', '.join(permitted_methods)


class HttpResponseGone(HttpResponse):
    status_code = 410


class HttpResponseServerError(HttpResponse):
    status_code = 500


class Http404(Exception):
    pass
