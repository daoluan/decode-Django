import hashlib
import logging
import re

from django.conf import settings
from django import http
from django.core.mail import mail_managers
from django.utils.http import urlquote
from django.utils import six
from django.core import urlresolvers


logger = logging.getLogger('django.request')


class CommonMiddleware(object):
    """
    "Common" middleware for taking care of some basic operations:

        - Forbids access to User-Agents in settings.DISALLOWED_USER_AGENTS 
          拒绝某些客户端

        - URL rewriting: Based on the APPEND_SLASH and PREPEND_WWW settings,
          this middleware appends missing slashes and/or prepends missing
          "www."s. 
          填补 www. 前缀

            - If APPEND_SLASH is set and the initial URL doesn't end with a
              slash, and it is not found in urlpatterns, a new URL is formed by
              appending a slash at the end. If this new URL is found in
              urlpatterns, then an HTTP-redirect is returned to this new URL;
              otherwise the initial URL is processed as usual.

              如果 APPEND_SLASH 设置了,且 url 不是以 / 结尾,则会填补 / 然后返回新的 url

        - ETags: If the USE_ETAGS setting is set, ETags will be calculated from
          the entire page content and Not Modified responses will be returned
          appropriately.
    """

    def process_request(self, request):
        """
        Check for denied User-Agents and rewrite the URL based on
        settings.APPEND_SLASH and settings.PREPEND_WWW
        """

        # Check for denied User-Agents 检测被拒绝的客户端
        if 'HTTP_USER_AGENT' in request.META:
            for user_agent_regex in settings.DISALLOWED_USER_AGENTS:
                if user_agent_regex.search(request.META['HTTP_USER_AGENT']):
                    logger.warning('Forbidden (User agent): %s', request.path,
                        extra={
                            'status_code': 403,
                            'request': request
                        }
                    )
                    return http.HttpResponseForbidden('<h1>Forbidden</h1>')

        # Check for a redirect based on settings.APPEND_SLASH 
        #看是否需要填补 www.
        # and settings.PREPEND_WWW
        host = request.get_host()
        old_url = [host, request.path]
        new_url = old_url[:]

        if (settings.PREPEND_WWW and old_url[0] and
                not old_url[0].startswith('www.')):
            new_url[0] = 'www.' + old_url[0]

        # Append a slash if APPEND_SLASH is set and the URL doesn't have a
        # trailing slash and there is no pattern for the current path 填补 /
        if settings.APPEND_SLASH and (not old_url[1].endswith('/')):
            urlconf = getattr(request, 'urlconf', None)
            if (not urlresolvers.is_valid_path(request.path_info, urlconf) and
                    urlresolvers.is_valid_path("%s/" % request.path_info, urlconf)):

                    new_url[1] = new_url[1] + '/' # 填

                if settings.DEBUG and request.method == 'POST':
                    raise RuntimeError((""
                    "You called this URL via POST, but the URL doesn't end "
                    "in a slash and you have APPEND_SLASH set. Django can't "
                    "redirect to the slash URL while maintaining POST data. "
                    "Change your form to point to %s%s (note the trailing "
                    "slash), or set APPEND_SLASH=False in your Django "
                    "settings.") % (new_url[0], new_url[1]))

        if new_url == old_url:
            # No redirects required.
            return

        if new_url[0]:
            newurl = "%s://%s%s" % (
                request.is_secure() and 'https' or 'http',
                new_url[0], urlquote(new_url[1]))
        else:
            newurl = urlquote(new_url[1])

        if request.META.get('QUERY_STRING', ''):
            if six.PY3:
                newurl += '?' + request.META['QUERY_STRING']
            else:
                # `query_string` is a bytestring. Appending it to the unicode
                # string `newurl` will fail if it isn't ASCII-only. This isn't
                # allowed; only broken software generates such query strings.
                # Better drop the invalid query string than crash (#15152).
                try:
                    newurl += '?' + request.META['QUERY_STRING'].decode()
                except UnicodeDecodeError:
                    pass
        return http.HttpResponsePermanentRedirect(newurl)

    def process_response(self, request, response):
        "Send broken link emails and calculate the Etag, if needed."

        # 如果返回 404,找不到资源
        if response.status_code == 404:
            if settings.SEND_BROKEN_LINK_EMAILS and not settings.DEBUG:
                # If the referrer was from an internal link or a non-search-engine site,
                # send a note to the managers.
                domain = request.get_host()
                referer = request.META.get('HTTP_REFERER', None)
                is_internal = _is_internal_request(domain, referer)
                path = request.get_full_path()

                if referer and not _is_ignorable_404(path) and (is_internal or '?' not in referer):
                    ua = request.META.get('HTTP_USER_AGENT', '<none>')
                    ip = request.META.get('REMOTE_ADDR', '<none>')

                    mail_managers("Broken %slink on %s" % ((is_internal and 'INTERNAL ' or ''), domain),
                        "Referrer: %s\nRequested URL: %s\nUser agent: %s\nIP address: %s\n" \
                                  % (referer, request.get_full_path(), ua, ip),
                                  fail_silently=True)

                    # mail_managers 会发送错误信息给开发者
                return response

        # Use ETags, if requested.
        if settings.USE_ETAGS:
            if response.has_header('ETag'):
                etag = response['ETag']
            elif response.streaming:
                etag = None
            else:
                etag = '"%s"' % hashlib.md5(response.content).hexdigest()

            if etag is not None:
                if (200 <= response.status_code < 300
                    and request.META.get('HTTP_IF_NONE_MATCH') == etag):
                    cookies = response.cookies
                    response = http.HttpResponseNotModified() #304
                    response.cookies = cookies
                else:
                    response['ETag'] = etag

        return response

def _is_ignorable_404(uri):
    """
    Returns True if a 404 at the given URL *shouldn't* notify the site managers. 如果是 404 不通知开发人员或者管理人员,返回 true
    """
    if getattr(settings, 'IGNORABLE_404_STARTS', ()):
        import warnings
        warnings.warn('The IGNORABLE_404_STARTS setting has been deprecated '
                      'in favor of IGNORABLE_404_URLS.', DeprecationWarning)
        # 如果以某些字符开头的 uri 且状态码是 404,就返回
        for start in settings.IGNORABLE_404_STARTS:
            if uri.startswith(start):
                return True

    if getattr(settings, 'IGNORABLE_404_ENDS', ()):
        import warnings
        warnings.warn('The IGNORABLE_404_ENDS setting has been deprecated '
                      'in favor of IGNORABLE_404_URLS.', DeprecationWarning)
        # 如果以某些字符的 uri 且状态码是 404,就返回
        for end in settings.IGNORABLE_404_ENDS:
            if uri.endswith(end):
                return True
    # 还有一个 IGNORABLE_404_URLS 设置,专门放置忽略 404 的uri.######## 不是真正的忽略
    return any(pattern.search(uri) for pattern in settings.IGNORABLE_404_URLS)

def _is_internal_request(domain, referer):
    """
    Returns true if the referring URL is the same domain as the current request.
    判断是否为本站点的 url
    """
    # Different subdomains are treated as different domains.
    return referer is not None and re.match("^https?://%s/" % re.escape(domain), referer)
