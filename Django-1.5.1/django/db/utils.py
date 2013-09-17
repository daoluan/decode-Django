import os
import pkgutil
from threading import local

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.importlib import import_module
from django.utils._os import upath
from django.utils import six


DEFAULT_DB_ALIAS = 'default'

# Define some exceptions that mirror the PEP249 interface.
# We will rethrow any backend-specific errors using these
# common wrappers
class DatabaseError(Exception):
    pass

class IntegrityError(DatabaseError):
    pass

导入数据库后端模块
def load_backend(backend_name):
    # Look for a fully qualified database backend name
    try:
        # 导入模块 django.db.backends, 一般会在这里成功返回, 除非指定了未知的数据库后端名
        return import_module('.base', backend_name)
    except ImportError as e_user:
        # The database backend wasn't found. Display a helpful error message
        # listing all possible (built-in) database backends.
        backend_dir = os.path.join(os.path.dirname(upath(__file__)), 'backends')
        try:
            # 内建的数据库后端驱动, 除了 dummy
            builtin_backends = [
                name for _, name, ispkg in pkgutil.iter_modules([backend_dir])
                if ispkg and name != 'dummy']

        except EnvironmentError:
            builtin_backends = []

        # 查看是否给定的 backend_name 在内建的后端驱动中, 如果没有
        if backend_name not in ['django.db.backends.%s' % b for b in
                                builtin_backends]:
            # 异常
            backend_reprs = map(repr, sorted(builtin_backends))
            error_msg = ("%r isn't an available database backend.\n"
                         "Try using 'django.db.backends.XXX', where XXX "
                         "is one of:\n    %s\nError was: %s" %
                         (backend_name, ", ".join(backend_reprs), e_user))
            raise ImproperlyConfigured(error_msg)
        else:
            # If there's some other error, this must be an error in Django
            raise


class ConnectionDoesNotExist(Exception):
    pass

解析并存储 settings.DATABASES
class ConnectionHandler(object):
    # 在 django.db.__init__ 中有一句:
    # connections = ConnectionHandler(settings.DATABASES)
    # 可见, ConnectionHandler.databases == settings.DATABASES
    def __init__(self, databases):
        if not databases:
            # 如果没有设置数据库, 将安装无用的数据库后端驱动
            self.databases = {
                DEFAULT_DB_ALIAS: {
                    'ENGINE': 'django.db.backends.dummy',
                },
            }
        else:
            self.databases = databases

        # 线程本地数据, 为该线程分配一个数据变量
        self._connections = local()

    def ensure_defaults(self, alias):
        """
        当没有数据库设置的时候, 会被调用
        Puts the defaults into the settings dictionary for a given connection
        where no settings is provided.
        """
        try:
            conn = self.databases[alias]
        except KeyError:
            raise ConnectionDoesNotExist("The connection %s doesn't exist" % alias)

        # 防止 ENGINE 没有设置
        conn.setdefault('ENGINE', 'django.db.backends.dummy')

        # 看似多余的一项检测
        if conn['ENGINE'] == 'django.db.backends.' or not conn['ENGINE']:
            conn['ENGINE'] = 'django.db.backends.dummy'

        conn.setdefault('OPTIONS', {})

        conn.setdefault('TIME_ZONE', 'UTC' if settings.USE_TZ else settings.TIME_ZONE)

        # 如果 ['NAME', 'USER', 'PASSWORD', 'HOST', 'PORT'] 都没有指定, 则默认为空
        for setting in ['NAME', 'USER', 'PASSWORD', 'HOST', 'PORT']:
            conn.setdefault(setting, '')

        # 如果 ['TEST_CHARSET', 'TEST_COLLATION', 'TEST_NAME', 'TEST_MIRROR'] 没有指定, 默认为 None
        for setting in ['TEST_CHARSET', 'TEST_COLLATION', 'TEST_NAME', 'TEST_MIRROR']:
            conn.setdefault(setting, None)

    def __getitem__(self, alias):
        # alias 别名
        if hasattr(self._connections, alias):
            return getattr(self._connections, alias)

        如果没有, 会尝试加载
        self.ensure_defaults(alias)

        db = self.databases[alias]

        backend = load_backend(db['ENGINE'])

        # self._connections 中的每一项都是 DatabaseWrapper 实例
        conn = backend.DatabaseWrapper(db, alias)
        setattr(self._connections, alias, conn)
        return conn

    def __setitem__(self, key, value):
        setattr(self._connections, key, value)

    def __iter__(self):
        return iter(self.databases)

    def all(self):
        return [self[alias] for alias in self]


class ConnectionRouter(object):
    def __init__(self, routers):

        self.routers = []

        for r in routers:
            if isinstance(r, six.string_types):

                try:
                    module_name, klass_name = r.rsplit('.', 1)
                    module = import_module(module_name) 导入模块
                except ImportError as e:
                    raise ImproperlyConfigured('Error importing database router %s: "%s"' % (klass_name, e))

                try:
                    router_class = getattr(module, klass_name)
                except AttributeError:
                    raise ImproperlyConfigured('Module "%s" does not define a database router name "%s"' % (module, klass_name))
                else:
                    router = router_class()
            else:
                router = r
            self.routers.append(router)

    def _router_func(action):
        def _route_db(self, model, **hints):
            chosen_db = None
            for router in self.routers:
                try:
                    method = getattr(router, action)
                except AttributeError:
                    # If the router doesn't have a method, skip to the next one.
                    pass
                else:
                    chosen_db = method(model, **hints)
                    if chosen_db:
                        return chosen_db
            try:
                return hints['instance']._state.db or DEFAULT_DB_ALIAS
            except KeyError:
                return DEFAULT_DB_ALIAS
        return _route_db

    db_for_read = _router_func('db_for_read')
    db_for_write = _router_func('db_for_write')

    def allow_relation(self, obj1, obj2, **hints):
        for router in self.routers:
            try:
                method = router.allow_relation
            except AttributeError:
                # If the router doesn't have a method, skip to the next one.
                pass
            else:
                allow = method(obj1, obj2, **hints)
                if allow is not None:
                    return allow
        return obj1._state.db == obj2._state.db

    def allow_syncdb(self, db, model):
        for router in self.routers:
            try:
                method = router.allow_syncdb
            except AttributeError:
                # If the router doesn't have a method, skip to the next one.
                pass
            else:
                allow = method(db, model)
                if allow is not None:
                    return allow
        return True
