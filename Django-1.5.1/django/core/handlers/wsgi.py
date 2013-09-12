from __future__ import unicode_literals

import codecs
import logging
import sys
from io import BytesIO
from threading import Lock

from django import http
from django.core import signals
from django.core.handlers import base
from django.core.urlresolvers import set_script_prefix
from django.utils import datastructures
from django.utils.encoding import force_str, force_text, iri_to_uri

logger = logging.getLogger('django.request')


# See http://www.iana.org/assignments/http-status-codes
STATUS_CODE_TEXT = {
    100: 'CONTINUE',
    101: 'SWITCHING PROTOCOLS',
    102: 'PROCESSING',
    200: 'OK',
    201: 'CREATED',
    202: 'ACCEPTED',
    203: 'NON-AUTHORITATIVE INFORMATION',
    204: 'NO CONTENT',
    205: 'RESET CONTENT',
    206: 'PARTIAL CONTENT',
    207: 'MULTI-STATUS',
    208: 'ALREADY REPORTED',
    226: 'IM USED',
    300: 'MULTIPLE CHOICES',
    301: 'MOVED PERMANENTLY',
    302: 'FOUND',
    303: 'SEE OTHER',
    304: 'NOT MODIFIED',
    305: 'USE PROXY',
    306: 'RESERVED',
    307: 'TEMPORARY REDIRECT',
    400: 'BAD REQUEST',
    401: 'UNAUTHORIZED',
    402: 'PAYMENT REQUIRED',
    403: 'FORBIDDEN',
    404: 'NOT FOUND',
    405: 'METHOD NOT ALLOWED',
    406: 'NOT ACCEPTABLE',
    407: 'PROXY AUTHENTICATION REQUIRED',
    408: 'REQUEST TIMEOUT',
    409: 'CONFLICT',
    410: 'GONE',
    411: 'LENGTH REQUIRED',
    412: 'PRECONDITION FAILED',
    413: 'REQUEST ENTITY TOO LARGE',
    414: 'REQUEST-URI TOO LONG',
    415: 'UNSUPPORTED MEDIA TYPE',
    416: 'REQUESTED RANGE NOT SATISFIABLE',
    417: 'EXPECTATION FAILED',
    422: 'UNPROCESSABLE ENTITY',
    423: 'LOCKED',
    424: 'FAILED DEPENDENCY',
    426: 'UPGRADE REQUIRED',
    500: 'INTERNAL SERVER ERROR',
    501: 'NOT IMPLEMENTED',
    502: 'BAD GATEWAY',
    503: 'SERVICE UNAVAILABLE',
    504: 'GATEWAY TIMEOUT',
    505: 'HTTP VERSION NOT SUPPORTED',
    506: 'VARIANT ALSO NEGOTIATES',
    507: 'INSUFFICIENT STORAGE',
    508: 'LOOP DETECTED',
    510: 'NOT EXTENDED',
}

class LimitedStream(object):
    '''
    LimitedStream wraps another stream in order to not allow reading from it 不允许读
    past specified amount of bytes.
    '''
    def __init__(self, stream, limit, buf_size=64 * 1024 * 1024):
        self.stream = stream
        self.remaining = limit
        self.buffer = b''
        self.buf_size = buf_size

    def _read_limited(self, size=None):
        if size is None or size > self.remaining:
            size = self.remaining
        if size == 0:
            return b''
        result = self.stream.read(size)
        self.remaining -= len(result)
        return result

    def read(self, size=None):
        if size is None:
            result = self.buffer + self._read_limited()
            self.buffer = b''

        elif size < len(self.buffer):
            result = self.buffer[:size]
            self.buffer = self.buffer[size:]

        else: # size >= len(self.buffer) 只能读一部分
            result = self.buffer + self._read_limited(size - len(self.buffer))
            self.buffer = b''
        return result

    def readline(self, size=None):
        while b'\n' not in self.buffer and \
              (size is None or len(self.buffer) < size):
            # 直到读到 \n 为止
            if size:
                # since size is not None here, len(self.buffer) < size
                chunk = self._read_limited(size - len(self.buffer))
            else:
                chunk = self._read_limited()

            if not chunk:
                break

            self.buffer += chunk

        sio = BytesIO(self.buffer)
        if size:
            line = sio.readline(size)
        else:
            line = sio.readline()

        self.buffer = sio.read() # 把剩下的数据放入 buffer
        return line

继承自 http.HttpRequest
class WSGIRequest(http.HttpRequest):
    def __init__(self, environ): # 要传入环境变量
        script_name = base.get_script_name(environ) # 脚本
        path_info = base.get_path_info(environ)     # 路径

        if not path_info or path_info == script_name:
            # Sometimes PATH_INFO exists, but is empty (e.g. accessing
            # the SCRIPT_NAME URL without a trailing slash). We really need to
            # operate as if they'd requested '/'. Not amazingly nice to force
            # the path like this, but should be harmless.
            #
            # (The comparison of path_info to script_name is to work around an
            # apparent bug in flup 1.0.1. See Django ticket #8490).
            path_info = '/'

        self.environ = environ
        self.path_info = path_info
        self.path = '%s%s' % (script_name, path_info)

        self.META = environ
        self.META['PATH_INFO'] = path_info
        self.META['SCRIPT_NAME'] = script_name
        self.method = environ['REQUEST_METHOD'].upper()

        _, content_params = self._parse_content_type(self.META.get('CONTENT_TYPE', '')) # 分析请求内容类型

        if 'charset' in content_params:
            try:
                codecs.lookup(content_params['charset'])
            except LookupError:
                pass
            else:
                self.encoding = content_params['charset']

        self._post_parse_error = False

        try:
            content_length = int(self.environ.get('CONTENT_LENGTH')) # 长度
        except (ValueError, TypeError):
            content_length = 0

        self._stream = LimitedStream(self.environ['wsgi.input'], content_length)
        self._read_started = False

    def _is_secure(self):
        return 'wsgi.url_scheme' in self.environ and self.environ['wsgi.url_scheme'] == 'https' # url 的方案是否安全的方案

    def _parse_content_type(self, ctype):
        """
        Media Types parsing according to RFC 2616, section 3.7.

        Returns the data type and parameters. For example:
        Input: "text/plain; charset=iso-8859-1"
        Output: ('text/plain', {'charset': 'iso-8859-1'})
        """
        content_type, _, params = ctype.partition(';')
        content_params = {}

        for parameter in params.split(';'):
            k, _, v = parameter.strip().partition('=')
            content_params[k] = v
        return content_type, content_params

    def _get_request(self):
        if not hasattr(self, '_request'):
            self._request = datastructures.MergeDict(self.POST, self.GET) # 合并字典
        return self._request

    def _get_get(self):
        if not hasattr(self, '_get'):
            # The WSGI spec says 'QUERY_STRING' may be absent.
            self._get = http.QueryDict(self.environ.get('QUERY_STRING', ''), encoding=self._encoding) # 获取 get query string

        return self._get

    def _set_get(self, get):
        self._get = get

    def _get_post(self):
        if not hasattr(self, '_post'):
            self._load_post_and_files()
        return self._post

    def _set_post(self, post):
        self._post = post

    def _get_cookies(self):
        if not hasattr(self, '_cookies'):
            self._cookies = http.parse_cookie(self.environ.get('HTTP_COOKIE', ''))
        return self._cookies

    def _set_cookies(self, cookies):
        self._cookies = cookies

    def _get_files(self):
        if not hasattr(self, '_files'):
            self._load_post_and_files()
        return self._files

    # 很有意思的设计 http://docs.python.org/2/library/functions.html#property
    GET = property(_get_get, _set_get)
    POST = property(_get_post, _set_post)
    COOKIES = property(_get_cookies, _set_cookies)
    FILES = property(_get_files)
    REQUEST = property(_get_request)

# 继承, 但只实现了 __call__ 方法, 方便使用
class WSGIHandler(base.BaseHandler):
    initLock = Lock()

    # 关于此, 日后展开, 可以将其视为一个代表 http 请求的类
    request_class = WSGIRequest

    # WSGIHandler 也可以作为函数来调用
    def __call__(self, environ, start_response):
        # Set up middleware if needed. We couldn't do this earlier, because
        # settings weren't available.

        # 这里的检测: 因为 self._request_middleware 是最后才设定的, 所以如果为空,
        # 很可能是因为 self.load_middleware() 没有调用成功.
        if self._request_middleware is None:
            with self.initLock:
                try:
                    # Check that middleware is still uninitialised.
                    if self._request_middleware is None:
                        因为 load_middleware() 可能没有调用, 调用一次.
                        self.load_middleware()
                except:
                    # Unload whatever middleware we got
                    self._request_middleware = None
                    raise

        set_script_prefix(base.get_script_name(environ))
        signls.request_started.send(sender=self.__class__) # __class__ 代表自己的类

        try:
            # 实例化 request_class = WSGIRequest, 将在日后文章中展开, 可以将其视为一个代表 http 请求的类
            request = self.request_class(environ)

        except UnicodeDecodeError:
            logger.warning('Bad Request (UnicodeDecodeError)',
                exc_info=sys.exc_info(),
                extra={
                    'status_code': 400,
                }
            )
            response = http.HttpResponseBadRequest()
        else:
            # 调用 self.get_response(), 将会返回一个相应对象 response
            response = self.get_response(request)

        # 将 self 挂钩到 response 对象
        response._handler_class = self.__class__

        try:
            status_text = STATUS_CODE_TEXT[response.status_code]
        except KeyError:
            status_text = 'UNKNOWN STATUS CODE'

         # 状态码
        status = '%s %s' % (response.status_code, status_text)

        response_headers = [(str(k), str(v)) for k, v in response.items()]

        # 对于每个一个 cookie, 都在 header 中设置: Set-cookie xxx=yyy
        for c in response.cookies.values():
            response_headers.append((str('Set-Cookie'), str(c.output(header=''))))

        # start_response() 操作已经在上节中介绍了
        start_response(force_str(status), response_headers)

        # 成功返回相应对象
        return response
