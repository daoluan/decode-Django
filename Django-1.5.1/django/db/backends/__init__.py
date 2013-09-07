from django.db.utils import DatabaseError

try:
    from django.utils.six.moves import _thread as thread
except ImportError:
    from django.utils.six.moves import _dummy_thread as thread
from contextlib import contextmanager

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS
from django.db.backends import util
from django.db.transaction import TransactionManagementError
from django.utils.functional import cached_property
from django.utils.importlib import import_module
from django.utils import six
from django.utils.timezone import is_aware

数据库包装类, 关于的数据库的基本操作函数
class BaseDatabaseWrapper(object):
    """
    Represents a database connection.

    表示一个打开的数据库
    """
    ops = None
    vendor = 'unknown'

    def __init__(self, settings_dict, alias=DEFAULT_DB_ALIAS,
                 allow_thread_sharing=False):
        # `settings_dict` should be a dictionary containing keys such as
        # NAME, USER, etc. It's called `settings_dict` instead of `settings`
        # to disambiguate it from Django settings modules.

        # settints_dict 包含数据库名, 用户名, 密码等
        self.connection = None
        self.queries = [] #查询集
        self.settings_dict = settings_dict
        self.alias = alias
        self.use_debug_cursor = None

        # Transaction related attributes 事务相关的属性
        self.transaction_state = []
        self.savepoint_state = 0
        self._dirty = None
        self._thread_ident = thread.get_ident()
        self.allow_thread_sharing = allow_thread_sharing

    # 都是针对 self.alias 的操作
    # ==
    def __eq__(self, other):
        return self.alias == other.alias

    # !==
    def __ne__(self, other):
        return not self == other

    # http://docs.python.org/2/reference/datamodel.html#object.__hash__
    # Called by built-in function hash() and for operations on members of hashed collections including set, frozenset, and dict. __hash__() should return an integer.
    # If a class does not define a __cmp__() or __eq__() method it should not define a __hash__() operation either; if it defines __cmp__() or __eq__() but not __hash__(), its instances will not be usable in hashed collections.
    def __hash__(self):
        return hash(self.alias)

    def _commit(self):
        if self.connection is not None:
            return self.connection.commit()

    # 回滚
    def _rollback(self):
        if self.connection is not None:
            return self.connection.rollback()

    # 不懂, 进入事务管理和退出事务管理
    def _enter_transaction_management(self, managed):
        """
        A hook for backend-specific changes required when entering manual
        transaction handling.
        """
        pass

    def _leave_transaction_management(self, managed):
        """
        A hook for backend-specific changes required when leaving manual
        transaction handling. Will usually be implemented only when
        _enter_transaction_management() is also required.
        """
        pass

    def _savepoint(self, sid):
        # self.features 未知属性
        if not self.features.uses_savepoints:
            return
        self.cursor().execute(self.ops.savepoint_create_sql(sid))

    def _savepoint_rollback(self, sid):
        if not self.features.uses_savepoints:
            return
        self.cursor().execute(self.ops.savepoint_rollback_sql(sid))

    def _savepoint_commit(self, sid):
        if not self.features.uses_savepoints:
            return
        self.cursor().execute(self.ops.savepoint_commit_sql(sid))

    def abort(self):
        """
        Roll back any ongoing transaction and clean the transaction state
        stack.
        """
        if self._dirty:
            self._rollback()
            self._dirty = False

        # 脏数据: 在数据库技术中,脏数据在临时更新（脏读）中产生。事务A更新了某个数据项X，但是由于某种原因，事务A出现了问题，于是要把A回滚。但是在回滚之前，另一个事务B读取了数据项X的值(A更新后)，A回滚了事务，数据项恢复了原值。事务B读取的就是数据项X的就是一个“临时”的值，就是脏数据。
        # 简单来说就是, B 读取了临时数据

        while self.transaction_state:
            self.leave_transaction_management()

    def enter_transaction_management(self, managed=True):
        """
        Enters transaction management for a running thread. It must be balanced with
        the appropriate leave_transaction_management call, since the actual state is
        managed as a stack. 这里有管理栈的概念

        和 self.leave_transaction_management 必须成对调用

        The state and dirty flag are carried over from the surrounding block or
        from the settings, if there is no surrounding block (dirty is always false
        when no current block is running).
        """
        if self.transaction_state:
            self.transaction_state.append(self.transaction_state[-1]) #压入最后一个 state
        else:
            self.transaction_state.append(settings.TRANSACTIONS_MANAGED)

        if self._dirty is None:
            self._dirty = False

        self._enter_transaction_management(managed) #真正的进入

    def leave_transaction_management(self):
        """
        Leaves transaction management for a running thread. A dirty flag is carried
        over to the surrounding block, as a commit will commit all changes, even
        those from outside. (Commits are on connection level.)
        """
        if self.transaction_state:
            del self.transaction_state[-1] #删除最后一项
        else:
            raise TransactionManagementError(
                "This code isn't under transaction management")

        # We will pass the next status (after leaving the previous state
        # behind) to subclass hook.
        self._leave_transaction_management(self.is_managed())
        if self._dirty:
            self.rollback()
            raise TransactionManagementError(
                "Transaction managed block ended with pending COMMIT/ROLLBACK")
        self._dirty = False

    def validate_thread_sharing(self):
        """
        检测数据库并没有被其他的线程连接, 除非已经设置为所有线程可以共享

        Validates that the connection isn't accessed by another thread than the
        one which originally created it, unless the connection was explicitly
        authorized to be shared between threads (via the `allow_thread_sharing`
        property). Raises an exception if the validation fails.
        """
        if (not self.allow_thread_sharing
            and self._thread_ident != thread.get_ident()):
                raise DatabaseError("DatabaseWrapper objects created in a "
                    "thread can only be used in that same thread. The object "
                    "with alias '%s' was created in thread id %s and this is "
                    "thread id %s."
                    % (self.alias, self._thread_ident, thread.get_ident()))

    def is_dirty(self):
        """
        Returns True if the current transaction requires a commit for changes to
        happen.
        """
        return self._dirty

    def set_dirty(self):
        """
        Sets a dirty flag for the current thread and code streak. This can be used
        to decide in a managed block of code to decide whether there are open
        changes waiting for commit.
        """
        if self._dirty is not None:
            self._dirty = True
        else:
            raise TransactionManagementError("This code isn't under transaction "
                "management") #事务中才有脏数据的概念

    def set_clean(self):
        """
        Resets a dirty flag for the current thread and code streak. This can be used
        to decide in a managed block of code to decide whether a commit or rollback
        should happen.
        """
        if self._dirty is not None:
            self._dirty = False
        else:
            raise TransactionManagementError("This code isn't under transaction management")
        self.clean_savepoints()

    def clean_savepoints(self):
        self.savepoint_state = 0

    def is_managed(self):
        """
        Checks whether the transaction manager is in manual or in auto state.

        是在人工状态下还是自动状态下, 不懂
        """
        if self.transaction_state:
            return self.transaction_state[-1]
        return settings.TRANSACTIONS_MANAGED

    def managed(self, flag=True):
        """
        Puts the transaction manager into a manual state: managed transactions have
        to be committed explicitly by the user. If you switch off transaction
        management and there is a pending commit/rollback, the data will be
        commited.
        """
        top = self.transaction_state
        if top:
            top[-1] = flag
            if not flag and self.is_dirty():
                self._commit()
                self.set_clean()
        else:
            raise TransactionManagementError("This code isn't under transaction "
                "management")

    def commit_unless_managed(self):
        """
        Commits changes if the system is not in managed transaction mode.
        """
        self.validate_thread_sharing()
        if not self.is_managed():
            self._commit()
            self.clean_savepoints()
        else:
            self.set_dirty()

    def rollback_unless_managed(self):
        """
        除非在管理状态下才进行回滚, 不懂
        Rolls back changes if the system is not in managed transaction mode.
        """
        self.validate_thread_sharing()
        if not self.is_managed():
            self._rollback()
        else:
            self.set_dirty() #设置脏数据, 因为在事务管理中

    def commit(self):
        """
        Does the commit itself and resets the dirty flag.
        """
        self.validate_thread_sharing()
        self._commit()
        self.set_clean()

    def rollback(self):
        """
        This function does the rollback itself and resets the dirty flag.
        """
        self.validate_thread_sharing()
        self._rollback()
        self.set_clean()

    def savepoint(self):
        """
        Creates a savepoint (if supported and required by the backend) inside the
        current transaction. Returns an identifier for the savepoint that will be
        used for the subsequent rollback or commit.
        """
        thread_ident = thread.get_ident() 获取线程的标识

        self.savepoint_state += 1

        tid = str(thread_ident).replace('-', '')
        sid = "s%s_x%d" % (tid, self.savepoint_state)
        self._savepoint(sid)
        return sid

    def savepoint_rollback(self, sid):
        """
        Rolls back the most recent savepoint (if one exists). Does nothing if
        savepoints are not supported.
        """
        self.validate_thread_sharing()
        if self.savepoint_state:
            self._savepoint_rollback(sid)

    def savepoint_commit(self, sid):
        """
        Commits the most recent savepoint (if one exists). Does nothing if
        savepoints are not supported.
        """
        self.validate_thread_sharing()
        if self.savepoint_state:
            self._savepoint_commit(sid)

    @contextmanager 不懂
    def constraint_checks_disabled(self):
        disabled = self.disable_constraint_checking()
        try:
            yield
        finally:
            if disabled:
                self.enable_constraint_checking()

    # 取消约束
    def disable_constraint_checking(self): # constraint 约束
        """
        Backends can implement as needed to temporarily disable foreign key constraint
        checking.
        """
        pass

    # 启动约束
    def enable_constraint_checking(self):
        """
        Backends can implement as needed to re-enable foreign key constraint checking.
        """
        pass

    # 检查约束
    def check_constraints(self, table_names=None):
        """
        Backends can override this method if they can apply constraint checking (e.g. via "SET CONSTRAINTS
        ALL IMMEDIATE"). Should raise an IntegrityError if any invalid foreign key references are encountered.
        """
        pass

    def close(self):
        self.validate_thread_sharing()
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def cursor(self):
        self.validate_thread_sharing()
        if (self.use_debug_cursor or
            (self.use_debug_cursor is None and settings.DEBUG)):
            cursor = self.make_debug_cursor(self._cursor())
        else:
            # 如果是非调试模式, 需要使用专用的游标类
            cursor = util.CursorWrapper(self._cursor(), self)
        return cursor

    def make_debug_cursor(self, cursor):
        return util.CursorDebugWrapper(cursor, self)

数据库特性
class BaseDatabaseFeatures(object):
    allows_group_by_pk = False

    # True if django.db.backend.utils.typecast_timestamp is used on values
    # returned from dates() calls.
    needs_datetime_string_cast = True

    empty_fetchmany_value = []
    update_can_self_select = True

    # Does the backend distinguish between '' and None?
    interprets_empty_strings_as_nulls = False #是否将空字符串转换为 NULL
    """
    说明：
    1、等价于没有任何值、是未知数。
    2、NULL与0、空字符串、空格都不同。
    3、对空值做加、减、乘、除等运算操作，结果仍为空。
    4、NULL的处理使用NVL函数。
    5、比较时使用关键字用“is null”和“is not null”。
    6、空值不能被索引，所以查询时有些符合条件的数据可能查不出来，count(*)中，用nvl(列名,0)处理后再查。
    7、排序时比其他数据都大（索引默认是降序排列，小→大），所以NULL值总是排在最后。
    """

    # Does the backend allow inserting duplicate rows 重复的行 when a unique_together
    # constraint exists, but one of the unique_together columns is NULL?
    # 当列为 null 时候是否可以重复
    ignores_nulls_in_unique_constraints = True

    can_use_chunked_reads = True
    can_return_id_from_insert = False
    has_bulk_insert = False
    uses_autocommit = False
    uses_savepoints = False
    can_combine_inserts_with_and_without_auto_increment_pk = False

    # If True, don't use integer foreign keys referring to, e.g., positive
    # integer 正整数 primary keys. 举个例子
    related_fields_match_type = False
    allow_sliced_subqueries = True
    has_select_for_update = False
    has_select_for_update_nowait = False

    supports_select_related = True

    # Does the default test database allow multiple connections?
    # Usually an indication that the test database is in-memory
    test_db_allows_multiple_connections = True

    # Can an object be saved without an explicit primary key?
    supports_unspecified_pk = False

    # Can a fixture contain forward references? i.e., are
    # FK constraints checked at the end of transaction, or
    # at the end of each save operation?
    supports_forward_references = True

    # Does a dirty transaction need to be rolled back
    # before the cursor can be used again?
    requires_rollback_on_dirty_transaction = False

    # Does the backend allow very long model names without error?
    supports_long_model_names = True

    # Is there a REAL datatype in addition to floats/doubles?
    has_real_datatype = False
    supports_subqueries_in_group_by = True
    supports_bitwise_or = True

    # Do time/datetime fields have microsecond precision?
    supports_microsecond_precision = True

    # Does the __regex lookup support backreferencing and grouping?
    supports_regex_backreferencing = True

    # Can date/datetime lookups be performed using a string?
    supports_date_lookup_using_string = True

    # Can datetimes with timezones be used?
    supports_timezones = True

    # When performing a GROUP BY, is an ORDER BY NULL required
    # to remove any ordering?
    requires_explicit_null_ordering_when_grouping = False

    # Is there a 1000 item limit on query parameters?
    supports_1000_query_parameters = True

    # Can an object have a primary key of 0? MySQL says No. MySQL 不允许使用 0 作为主键
    allows_primary_key_0 = True

    # Do we need to NULL a ForeignKey out, or can the constraint check be
    # deferred
    # 外键约束可否延迟检测
    can_defer_constraint_checks = False

    # date_interval_sql can properly handle mixed Date/DateTime fields and timedeltas
    supports_mixed_date_datetime_comparisons = True

    # Does the backend support tablespaces? Default to False because it isn't
    # in the SQL standard.
    supports_tablespaces = False

    # Does the backend reset sequences between tests?
    supports_sequence_reset = True

    # Confirm support for introspected foreign keys
    # Every database can do this reliably, except MySQL,
    # which can't do it for MyISAM tables
    can_introspect_foreign_keys = True

    # Support for the DISTINCT ON clause
    can_distinct_on_fields = False

    def __init__(self, connection):
        self.connection = connection

    @cached_property
    def supports_transactions(self):
        "Confirm support for transactions"
        try:
            # Make sure to run inside a managed transaction block,
            # otherwise autocommit will cause the confimation to
            # fail.
            self.connection.enter_transaction_management()
            self.connection.managed(True)
            cursor = self.connection.cursor()
            cursor.execute('CREATE TABLE ROLLBACK_TEST (X INT)')
            self.connection._commit()
            cursor.execute('INSERT INTO ROLLBACK_TEST (X) VALUES (8)')
            self.connection._rollback()
            cursor.execute('SELECT COUNT(X) FROM ROLLBACK_TEST')
            count, = cursor.fetchone()
            cursor.execute('DROP TABLE ROLLBACK_TEST')
            self.connection._commit()
            self.connection._dirty = False
        finally:
            self.connection.leave_transaction_management()
        return count == 0

    @cached_property
    def supports_stddev(self):
        "Confirm support for STDDEV and related stats functions"
        class StdDevPop(object):
            sql_function = 'STDDEV_POP'

        try:
            self.connection.ops.check_aggregate_support(StdDevPop())
            return True
        except NotImplementedError:
            return False

数据库操作类
class BaseDatabaseOperations(object):
    """
    This class encapsulates 压缩 all backend-specific differences, such as the way
    a backend performs ordering or calculates the ID of a recently-inserted
    row.
    """
    compiler_module = "django.db.models.sql.compiler"

    def __init__(self, connection):
        self.connection = connection
        self._cache = None

    def autoinc_sql(self, table, column):
        """
        auto inscream sql

        Returns any SQL needed to support auto-incrementing primary keys, or
        None if no SQL is necessary.

        This SQL is executed when a table is created.
        """
        return None

    def bulk_batch_size(self, fields, objs):
        """
        Returns the maximum allowed batch size for the backend. The fields
        are the fields going to be inserted in the batch, the objs contains
        all the objects to be inserted.
        """
        return len(objs)

    def cache_key_culling_sql(self):
        """
        Returns a SQL query that retrieves the first cache key greater than the
        n smallest.

        This is used by the 'db' cache backend to determine where to start
        culling.
        用于数据库换存
        """
        return "SELECT cache_key FROM %s ORDER BY cache_key LIMIT 1 OFFSET %%s"

    def date_extract_sql(self, lookup_type, field_name):
        """
        Given a lookup_type of 'year', 'month' or 'day', returns the SQL that
        extracts a value from the given date field field_name.
        """
        raise NotImplementedError()

    def date_interval_sql(self, sql, connector, timedelta):
        """
        Implements the date interval functionality 日期间隔 for expressions
        """
        raise NotImplementedError()

    def date_trunc_sql(self, lookup_type, field_name):
        """
        Given a lookup_type of 'year', 'month' or 'day', returns the SQL that
        truncates the given date field field_name to a DATE object with only
        the given specificity.

        将日期截断 不懂
        """
        raise NotImplementedError()

    # 时间映射成 sql
    def datetime_cast_sql(self):
        """
        Returns the SQL necessary to cast a datetime value so that it will be
        retrieved as a Python datetime object instead of a string.

        将 python datetime 转换为字符串?

        This SQL should include a '%s' in place of the field's name.
        """
        return "%s"

    def deferrable_sql(self):
        """
        Returns the SQL necessary to make a constraint "initially deferred"
        during a CREATE TABLE statement.
        """
        return ''

    # 检测是否 distinct
    def distinct_sql(self, fields):
        """
        Returns an SQL DISTINCT clause which removes duplicate rows from the
        result set. If any fields are given, only the given fields are being
        checked for duplicates.
        """

        if fields:
            raise NotImplementedError('DISTINCT ON fields is not supported by this database backend')
        else:
            return 'DISTINCT'

    def drop_foreignkey_sql(self):
        """
        Returns the SQL command that drops a foreign key.
        """
        return "DROP CONSTRAINT"

    def drop_sequence_sql(self, table):
        """
        Returns any SQL necessary to drop the sequence for the given table. 删除
        Returns None if no SQL is necessary.
        """
        return None

    def fetch_returned_insert_id(self, cursor):
        """
        对于指定的游标, 返回最新创建的 id
        Given a cursor object that has just performed an INSERT...RETURNING
        statement into a table that has an auto-incrementing ID, returns the
        newly created ID.
        """
        return cursor.fetchone()[0]

    def field_cast_sql(self, db_type):
        """
        Given a column type (e.g. 'BLOB', 'VARCHAR'), returns the SQL necessary
        to cast it before using it in a WHERE statement. Note that the
        resulting string should contain a '%s' placeholder for the column being
        searched against.
        """
        return '%s'

    def force_no_ordering(self):
        """
        强制无序

        Returns a list used in the "ORDER BY" clause to force no ordering at
        all. Returning an empty list means that nothing will be included in the
        ordering.
        """
        return []

    def for_update_sql(self, nowait=False):
        """
        Returns the FOR UPDATE SQL clause to lock rows for an update operation.
        """
        if nowait:
            return 'FOR UPDATE NOWAIT'
        else:
            return 'FOR UPDATE'

    def fulltext_search_sql(self, field_name):
        """
        Returns the SQL WHERE clause to use in order to perform a full-text
        search of the given field_name. Note that the resulting string should
        contain a '%s' placeholder for the value being searched against.
        """
        raise NotImplementedError('Full-text search is not implemented for this database backend')

    不懂
    def last_executed_query(self, cursor, sql, params):
        """
        返回最后执行的 sql
        Returns a string of the query last executed by the given cursor, with
        placeholders replaced with actual values.

        `sql` is the raw query containing placeholders, and `params` is the
        sequence of parameters. These are used by default, but this method
        exists for database backends to provide a better implementation
        according to their own quoting schemes.
        """
        from django.utils.encoding import force_text

        # Convert params to contain Unicode values.
        # 可以得到提示 force_text() 是将字符串转换为 unicode
        to_unicode = lambda s: force_text(s, strings_only=True, errors='replace')
        if isinstance(params, (list, tuple)):
            u_params = tuple([to_unicode(val) for val in params])
        else:
            u_params = dict([(to_unicode(k), to_unicode(v)) for k, v in params.items()])

        return force_text(sql) % u_params

    def last_insert_id(self, cursor, table_name, pk_name):
        """
        返回最新创建的 id
        Given a cursor object that has just performed an INSERT statement into
        a table that has an auto-incrementing ID, returns the newly created ID.

        This method also receives the table name and the name of the primary-key
        column.
        """
        return cursor.lastrowid

    def lookup_cast(self, lookup_type):
        """
        "contains", "like" 语句

        Returns the string to use in a query when performing lookups
        ("contains", "like", etc). The resulting string should contain a '%s'
        placeholder for the column being searched against.
        """
        return "%s"

    def max_in_list_size(self):
        """
        a single 'IN' 的最大容量

        Returns the maximum number of items that can be passed in a single 'IN'
        list condition, or None if the backend does not impose a limit.
        """
        return None

    def max_name_length(self):
        """
        Returns the maximum length of table and column names, or None if there
        is no limit.
        """
        return None

    def no_limit_value(self):
        """
        Returns the value to use for the LIMIT when we are wanting "LIMIT
        infinity". Returns None if the limit clause can be omitted in this case.
        """
        raise NotImplementedError

    def pk_default_value(self):
        """
        主键的默认值

        Returns the value to use during an INSERT statement to specify that
        the field should use its default value.
        """
        return 'DEFAULT'

    def process_clob(self, value):
        """
        Returns the value of a CLOB column, for backends that return a locator
        object that requires additional processing.
        """
        return value

    def return_insert_id(self):
        """
        For backends that support returning the last insert ID as part
        of an insert query, this method returns the SQL and params to
        append to the INSERT query. The returned fragment should
        contain a format string to hold the appropriate column.
        """
        pass

    def compiler(self, compiler_name):
        """
        Returns the SQLCompiler class corresponding to the given name,
        in the namespace corresponding to the `compiler_module` attribute
        on this backend.
        """
        if self._cache is None:
            self._cache = import_module(self.compiler_module)
        return getattr(self._cache, compiler_name)

    def quote_name(self, name):
        """
        Returns a quoted version of the given table, index or column name. Does
        not quote the given name if it's already been quoted.
        """
        raise NotImplementedError()

    def random_function_sql(self):
        """
        Returns a SQL expression that returns a random value.
        """
        return 'RANDOM()' 默认使用这个函数

    def regex_lookup(self, lookup_type):
        """
        正则表达式创造, 返回 sql
        Returns the string to use in a query when performing regular expression
        lookups (using "regex" or "iregex"). The resulting string should
        contain a '%s' placeholder for the column being searched against.

        If the feature is not supported (or part of it is not supported), a
        NotImplementedError exception can be raised.
        """
        raise NotImplementedError

    def savepoint_create_sql(self, sid):
        """
        Returns the SQL for starting a new savepoint. Only required if the
        "uses_savepoints" feature is True. The "sid" parameter is a string
        for the savepoint id.
        """
        raise NotImplementedError

    def savepoint_commit_sql(self, sid):
        """
        Returns the SQL for committing the given savepoint.
        """
        raise NotImplementedError

    def savepoint_rollback_sql(self, sid):
        """
        Returns the SQL for rolling back the given savepoint.
        """
        raise NotImplementedError

    def set_time_zone_sql(self):
        """
        Returns the SQL that will set the connection's time zone.

        Returns '' if the backend doesn't support time zones.
        """
        return ''

    不懂
    def sql_flush(self, style, tables, sequences):
        """
        Returns a list of SQL statements required to remove all data from
        the given database tables (without actually removing the tables
        themselves).

        The returned value also includes SQL statements required to reset DB
        sequences passed in :param sequences:.

        The `style` argument is a Style object as returned by either
        color_style() or no_style() in django.core.management.color.
        """
        raise NotImplementedError()

    不懂
    def sequence_reset_by_name_sql(self, style, sequences):
        """
        Returns a list of the SQL statements required to reset sequences
        passed in :param sequences:.

        The `style` argument is a Style object as returned by either
        color_style() or no_style() in django.core.management.color.
        """
        return []
    不懂
    def sequence_reset_sql(self, style, model_list):
        """
        Returns a list of the SQL statements required to reset sequences for
        the given models.

        The `style` argument is a Style object as returned by either
        color_style() or no_style() in django.core.management.color.
        """
        return []  # No sequence reset required by default.

    # 事务开始的语句
    def start_transaction_sql(self):
        """
        Returns the SQL statement required to start a transaction.
        """
        return "BEGIN;"
    # 事务结束的语句
    def end_transaction_sql(self, success=True):
        if not success:
            return "ROLLBACK;"
        return "COMMIT;"

    def tablespace_sql(self, tablespace, inline=False):
        """
        Returns the SQL that will be used in a query to define the tablespace.

        Returns '' if the backend doesn't support tablespaces.

        If inline is True, the SQL is appended to a row; otherwise it's appended
        to the entire CREATE TABLE or CREATE INDEX statement.
        """
        return ''

    # 对 like 语句的处理, 将 _　和　％　转义
    def prep_for_like_query(self, x):
        """Prepares a value for use in a LIKE query."""
        from django.utils.encoding import force_text
        return force_text(x).replace("\\", "\\\\").replace("%", "\%").replace("_", "\_")

    # Same as prep_for_like_query(), but called for "iexact" matches, which
    # need not necessarily be implemented using "LIKE" in the backend.
    prep_for_iexact_query = prep_for_like_query

    # 自动增长的主键的检查
    def validate_autopk_value(self, value):
        """
        Certain backends do not accept some values for "serial" fields
        (for example zero in MySQL). This method will raise a ValueError
        if the value is invalid, otherwise returns validated value.
        """
        return value

    def value_to_db_date(self, value):
        """
        Transform a date value to an object compatible with what is expected
        by the backend driver for date columns.
        """
        if value is None:
            return None
        return six.text_type(value)

    def value_to_db_datetime(self, value):
        """
        Transform a datetime value to an object compatible with what is expected
        by the backend driver for datetime columns.
        """
        if value is None:
            return None
        return six.text_type(value)

    def value_to_db_time(self, value):
        """
        Transform a time value to an object compatible with what is expected
        by the backend driver for time columns.
        """
        if value is None:
            return None
        if is_aware(value):
            raise ValueError("Django does not support timezone-aware times.")
        return six.text_type(value)

    def value_to_db_decimal(self, value, max_digits, decimal_places):
        """
        Transform a decimal.Decimal value to an object compatible with what is
        expected by the backend driver for decimal (numeric) columns.
        """
        if value is None:
            return None
        return util.format_number(value, max_digits, decimal_places)

    def year_lookup_bounds(self, value):
        """
        有些搜索是按年来搜索

        Returns a two-elements list with the lower and upper bound to be used
        with a BETWEEN operator to query a field value using a year lookup

        `value` is an int, containing the looked-up year.
        """
        first = '%s-01-01 00:00:00'
        second = '%s-12-31 23:59:59.999999'
        return [first % value, second % value]

    def year_lookup_bounds_for_date_field(self, value):
        """
        Returns a two-elements list with the lower and upper bound to be used
        with a BETWEEN operator to query a DateField value using a year lookup

        `value` is an int, containing the looked-up year.

        By default, it just calls `self.year_lookup_bounds`. Some backends need
        this hook because on their DB date fields can't be compared to values
        which include a time part.
        """
        return self.year_lookup_bounds(value)

    def convert_values(self, value, field):
        """
        将数据库返回的是数据类型转换为持久化对象

        Coerce the value returned by the database backend into a consistent type
        that is compatible with the field type.
        """
        if value is None:
            return value

        internal_type = field.get_internal_type()

        if internal_type == 'FloatField':
            return float(value)
        elif (internal_type and (internal_type.endswith('IntegerField')
                                 or internal_type == 'AutoField')):
            return int(value)

        return value

    聚合函数检测
    def check_aggregate_support(self, aggregate_func):
        """Check that the backend supports the provided aggregate

        This is used on specific backends to rule out known aggregates
        that are known to have faulty implementations. If the named
        aggregate function has a known problem, the backend should
        raise NotImplementedError.
        """
        pass

    def combine_expression(self, connector, sub_expressions):
        """Combine a list of subexpressions into a single expression, using
        the provided connecting operator. This is required because operators
        can vary between backends (e.g., Oracle with %% and &) and between
        subexpression types (e.g., date expressions)
        """
        conn = ' %s ' % connector
        return conn.join(sub_expressions)

    def modify_insert_params(self, placeholders, params):
        """Allow modification of insert parameters. Needed for Oracle Spatial
        backend due to #10888.
        """
        return params

数据库内部方法类
class BaseDatabaseIntrospection(object):
    """
    This class encapsulates all backend-specific introspection utilities

    包装内部方法
    """
    data_types_reverse = {}

    def __init__(self, connection):
        self.connection = connection

    def get_field_type(self, data_type, description):
        """Hook for a database backend to use the cursor description to
        match a Django field type to a database column.

        For Oracle, the column data_type on its own is insufficient to
        distinguish between a FloatField and IntegerField, for example."""
        return self.data_types_reverse[data_type]

    def table_name_converter(self, name):
        """Apply a conversion to the name for the purposes of comparison.

        The default table name converter is for case sensitive comparison.
        """
        return name

    def table_names(self, cursor=None):
        """
        返回数据库中的所有表名

        Returns a list of names of all tables that exist in the database.
        The returned table list is sorted by Python's default sorting. We
        do NOT use database's ORDER BY here to avoid subtle differences
        in sorting order between databases.
        """
        if cursor is None:
            cursor = self.connection.cursor()
        return sorted(self.get_table_list(cursor))

    def get_table_list(self, cursor):
        """
        Returns an unsorted list of names of all tables that exist in the
        database.
        """
        raise NotImplementedError

    def django_table_names(self, only_existing=False):
        """
        返回与 Django 有关且已经在 INSTALLED_APPS 中标明的

        Returns a list of all table names that have associated Django models and
        are in INSTALLED_APPS.

        不懂, 什么叫确实存在于数据库的表?
        If only_existing is True, the resulting list will only include the tables

        that actually exist in the database.
        """

        from django.db import models, router
        tables = set()

        for app in models.get_apps():
            for model in models.get_models(app):

                if not model._meta.managed: #为什么
                    continue
                if not router.allow_syncdb(self.connection.alias, model):
                    continue

                tables.add(model._meta.db_table)
                tables.update([f.m2m_db_table() for f in model._meta.local_many_to_many])

        tables = list(tables)

        if only_existing:
            existing_tables = self.table_names()
            tables = [
                t
                for t in tables
                if self.table_name_converter(t) in existing_tables
            ]
        return tables

    不懂
    def installed_models(self, tables):
        "Returns a set of all models represented by the provided list of table names."
        from django.db import models, router

        all_models = []

        for app in models.get_apps():
            for model in models.get_models(app):
                if router.allow_syncdb(self.connection.alias, model):
                    all_models.append(model)

        tables = list(map(self.table_name_converter, tables))

        return set([
            m for m in all_models
            if self.table_name_converter(m._meta.db_table) in tables
        ])

    def sequence_list(self):
        不懂, 返回数据库所有模块的数据库序列???
        "Returns a list of information about all DB sequences for all models in all apps."
        from django.db import models, router

        apps = models.get_apps()
        sequence_list = []

        for app in apps:
            for model in models.get_models(app):

                if not model._meta.managed:
                    continue

                if model._meta.swapped:
                    continue

                if not router.allow_syncdb(self.connection.alias, model):
                    continue

                for f in model._meta.local_fields:
                    if isinstance(f, models.AutoField):
                        sequence_list.append({'table': model._meta.db_table, 'column': f.column})
                        break  # Only one AutoField is allowed per model, so don't bother continuing.

                for f in model._meta.local_many_to_many:
                    # If this is an m2m using an intermediate table,
                    # we don't need to reset the sequence.
                    if f.rel.through is None:
                        sequence_list.append({'table': f.m2m_db_table(), 'column': None})

        return sequence_list

    def get_key_columns(self, cursor, table_name):
        """
        返回 (column_name, referenced_table_name,referenced_column_name)

        Backends can override this to return a list of (column_name, referenced_table_name,
        referenced_column_name) for all key columns in given table.
        """
        raise NotImplementedError

    def get_primary_key_column(self, cursor, table_name):
        """
        返回表的主键

        Returns the name of the primary key column for the given table.
        """
        for column in six.iteritems(self.get_indexes(cursor, table_name)):
            if column[1]['primary_key']: column 应该是个复杂的数据结构, 而且不怎么结构化, 直接用数组来访问???
                return column[0]

        return None

    def get_indexes(self, cursor, table_name):
        """
        返回索引 fieldname -> infodict

        Returns a dictionary of indexed fieldname -> infodict for the given
        table, where each infodict is in the format:
            {'primary_key': boolean representing whether it's the primary key,
             'unique': boolean representing whether it's a unique index}

        Only single-column indexes are introspected.
        """
        raise NotImplementedError

数据库客户端类
class BaseDatabaseClient(object):
    """
    This class encapsulates all backend-specific methods for opening a
    client shell.

    客户端打开方法
    """
    # This should be a string representing the name of the executable
    # (e.g., "psql"). Subclasses must override this.
    executable_name = None

    def __init__(self, connection):
        # connection is an instance of BaseDatabaseWrapper.
        self.connection = connection

    def runshell(self):
        raise NotImplementedError()

数据库有效性检测类
class BaseDatabaseValidation(object):
    """
    This class encapsualtes all backend-specific model validation.

    模块有效性检测
    """
    def __init__(self, connection):
        self.connection = connection

    def validate_field(self, errors, opts, f):
        "By default, there is no backend-specific validation"
        pass
