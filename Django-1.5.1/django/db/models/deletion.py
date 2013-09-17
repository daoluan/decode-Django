from functools import wraps
from operator import attrgetter

from django.db import connections, transaction, IntegrityError
from django.db.models import signals, sql
from django.utils.datastructures import SortedDict
from django.utils import six


class ProtectedError(IntegrityError):
    def __init__(self, msg, protected_objects):
        self.protected_objects = protected_objects
        super(ProtectedError, self).__init__(msg, protected_objects)

级联
def CASCADE(collector, field, sub_objs, using):
    # Collector.collect() 原型
    # def collect(self, objs, source=None, nullable=False, collect_related=True, source_attr=None, reverse_dependency=False)
    collector.collect(sub_objs, source=field.rel.to,
                      source_attr=field.name, nullable=field.null)

    # 如果 field.null 为真, 外键将被设置为 null
    if field.null and not connections[using].features.can_defer_constraint_checks:
        collector.add_field_update(field, None, sub_objs)


def PROTECT(collector, field, sub_objs, using):
    raise ProtectedError("Cannot delete some instances of model '%s' because "
        "they are referenced through a protected foreign key: '%s.%s'" % (
            field.rel.to.__name__, sub_objs[0].__class__.__name__, field.name
        ),
        sub_objs
    )


def SET(value):
    if callable(value):
        def set_on_delete(collector, field, sub_objs, using):
            collector.add_field_update(field, value(), sub_objs)
    else:
        def set_on_delete(collector, field, sub_objs, using):
            collector.add_field_update(field, value, sub_objs)
    return set_on_delete


SET_NULL = SET(None)


def SET_DEFAULT(collector, field, sub_objs, using):
    collector.add_field_update(field, field.get_default(), sub_objs)


def DO_NOTHING(collector, field, sub_objs, using):
    pass


def force_managed(func):
    @wraps(func)
    def decorated(self, *args, **kwargs):

        尝试进入事务
        if not transaction.is_managed(using=self.using):
            transaction.enter_transaction_management(using=self.using)
            forced_managed = True
        else:
            forced_managed = False

        try:
            执行删除
            func(self, *args, **kwargs)

            数据库的 commit 操作
            if forced_managed:
                transaction.commit(using=self.using)
            else:
                transaction.commit_unless_managed(using=self.using)
        finally:
            结束事务
            if forced_managed:
                transaction.leave_transaction_management(using=self.using)
    return decorated

Collector 是一个数据库条目对象收集器, 可以用各种`添加`函数添加各种 queryset, 在添加后需要调用 Collector.delete() 函数, 它会执行真正的删除操作.
Collector.delete() 依托 django.db.models.sql 模块工作.
class Collector(object):
    def __init__(self, using):
        self.using = using

        # Initially, {model: set([instances])}, later values become lists.
        self.data = {}

        self.batches = {} # {model: {field: set([instances])}}
        self.field_updates = {} # {model: {(field, value): set([instances])}}

        # fast_deletes is a list of queryset-likes that can be deleted without
        # fetching the objects into memory.
        self.fast_deletes = []

        # Tracks deletion-order dependency for databases without transactions
        # or ability to defer constraint checks. Only concrete model classes
        # should be included, as the dependencies exist only between actual
        # database tables; proxy models are represented here by their concrete
        # parent.
        self.dependencies = {} # {model: set([models])} 依赖模块

    在容器中添加对象
    def add(self, objs, source=None, nullable=False, reverse_dependency=False):
        """
        Adds 'objs' to the collection of objects to be deleted.  If the call is
        the result of a cascade, 'source' should be the model that caused it,
        and 'nullable' should be set to True if the relation can be null.

        Returns a list of all objects that were not already collected.
        """
        if not objs:
            return []

        new_objs = []
        model = objs[0].__class__
        instances = self.data.setdefault(model, set())

        for obj in objs:
            if obj not in instances:
                new_objs.append(obj)

        instances.update(new_objs)

        # Nullable relationships can be ignored -- they are nulled out before
        # deleting, and therefore do not affect the order in which objects have
        # to be deleted.
        if source is not None and not nullable:
            if reverse_dependency:
                source, model = model, source
            self.dependencies.setdefault(
                source._meta.concrete_model, set()).add(model._meta.concrete_model)
        return new_objs

    批量删除, 不是真正的删除, 而是直接放入到 list 中待删除
    def add_batch(self, model, field, objs):
        """
        Schedules a batch delete. 计划批量删除. Every instance of 'model' that is related to
        an instance of 'obj' through 'field' will be deleted. 级联删除?
        """
        self.batches.setdefault(model, {}).setdefault(field, set()).update(objs)

    def add_field_update(self, field, value, objs):
        """
        Schedules a field update. 'objs' must be a homogenous iterable
        collection of model instances (e.g. a QuerySet).
        """
        if not objs:
            return

        model = objs[0].__class__
        self.field_updates.setdefault(
            model, {}).setdefault(
            (field, value), set()).update(objs)

    判断是否可以快速删除, 不懂内部机制
    def can_fast_delete(self, objs, from_field=None):
        # 参数说明
        # objs: QuerySet 对象
        # from_field: 外键

        """
        Determines if the objects in the given queryset-like can be
        fast-deleted.

        当不存在级联, 不存在父模块, 不存在信号监听的对象时候, 会返回真

        This can be done if there are no cascades, no
        parents and no signal listeners for the object class.

        The 'from_field' tells where we are coming from - we need this to
        determine if the objects are in fact to be deleted. Allows also
        skipping parent -> child -> parent chain preventing fast delete of
        the child.
        """
        # from_field 所指一般为外键, 外键的 on_delete() 不为 CASCADE() 即可能是 DO_NOTHING() 或者其他用户自定义的函数, 此时不能快速删除
        if from_field and from_field.rel.on_delete is not CASCADE:
            return False

        # 如果 QuerySet 对象没有指定模块和 _raw_delete() 方法, 则无法快速删除
        if not (hasattr(objs, 'model') and hasattr(objs, '_raw_delete')):
            return False

        # QuerySet 对象的模块没有被监听, 则无法快速删除
        model = objs.model
        if (signals.pre_delete.has_listeners(model)
                or signals.post_delete.has_listeners(model)
                or signals.m2m_changed.has_listeners(model)):
            return False

        # The use of from_field comes from the need to avoid cascade back to
        # parent when parent delete is cascading to child.
        opts = model._meta

        # 如果存在父模块且 from_field 就是和父模块关联的属性, 则无法快速删除.
        # 下面的 link 是 OneToOneField 或者 ManyToManyField, 详见 django.db.models.base.py
        if any(link != from_field for link in opts.concrete_model._meta.parents.values()):
            return False

        # Foreign keys pointing to this model, both from m2m and other
        # models.
        for related in opts.get_all_related_objects(
            include_hidden=True, include_proxy_eq=True):
            # 联系 django.db.models.related 中的 ***Rel 类, 默认会把 rel.on_delete() 设置为 CASCADE() 函数
            if related.field.rel.on_delete is not DO_NOTHING:
                return False

        # GFK deletes
        for relation in opts.many_to_many:
            if not relation.rel.through: #当无外置多对多管理表的时候
                return False
        return True

    只收集不删除
    def collect(self, objs, source=None, nullable=False, collect_related=True,
        source_attr=None, reverse_dependency=False):
        """
        Adds 'objs' to the collection of objects to be deleted as well as all
        parent instances.  'objs' must be a homogenous iterable collection of
        model instances (e.g. a QuerySet).

        If 'collect_related' is True,
        related objects will be handled by their respective on_delete handler.

        If the call is the result of a cascade, 'source' should be the model
        that caused it and 'nullable' should be set to True, if the relation
        can be null.

        If 'reverse_dependency' is True, 'source' will be deleted before the
        current model, rather than after. (Needed for cascading to parent
        models, the one case in which the cascade follows the forwards
        direction of an FK rather than the reverse direction.)
        """
        if self.can_fast_delete(objs):
            self.fast_deletes.append(objs)
            return

        返回新增加的需要删除的 objs
        new_objs = self.add(objs, source, nullable,
                            reverse_dependency=reverse_dependency)
        if not new_objs:
            return

        model = new_objs[0].__class__

        # Recursively collect concrete model's parent models, but not their
        # related objects. These will be found by meta.get_all_related_objects()
        concrete_model = model._meta.concrete_model

        迭代所有的父模块
        for ptr in six.itervalues(concrete_model._meta.parents):
            if ptr:
                # FIXME: This seems to be buggy and execute a query for each
                # parent object fetch. We have the parent data in the obj,
                # but we don't have a nice way to turn that data into parent
                # object instance.
                递归调用之前需要获取父对象
                parent_objs = [getattr(obj ptr.name) for obj in new_objs]

                递归调用
                self.collect(parent_objs, source=model,
                             source_attr=ptr.rel.related_name,
                             collect_related=False,
                             reverse_dependency=True)

        是否级联
        if collect_related:
            # get_all_related_objects() 返回属性关联对象 RelatedObject list
            for related in model._meta.get_all_related_objects(
                    include_hidden=True, include_proxy_eq=True):

                field = related.field

                 # 如果什么都不做, 循环继续
                if field.rel.on_delete == DO_NOTHING:
                    continue

                # 找出所有关联表中的表项, 类似于下面的 SQL 语句:
                # select * from self where new_objs.id in (select id from related)
                sub_objs = self.related_objects(related, new_objs)

                # can_fast_delete() 返回真的其中一个条件就是关联表中没有外键, 也就是说关联表中已经不存在级联了
                if self.can_fast_delete(sub_objs, from_field=field):
                    self.fast_deletes.append(sub_objs)
                # 如果关联表中还存在级联, 需要再次 collect()
                elif sub_objs:
                    field.rel.on_delete(self, field, sub_objs, self.using)

            # TODO This entire block is only needed as a special case to
            # support cascade-deletes for GenericRelation. It should be
            # removed/fixed when the ORM gains a proper abstraction for virtual
            # or composite fields, and GFKs are reworked to fit into that.
            for relation in model._meta.many_to_many:
                if not relation.rel.through:
                    sub_objs = relation.bulk_related_objects(new_objs, self.using)
                    self.collect(sub_objs,
                                 source=model,
                                 source_attr=relation.rel.related_name,
                                 nullable=True)

    def related_objects(self, related, objs):
        """
        Gets a QuerySet of objects related to ``objs`` via the relation ``related``.
        """

"""        有关 __in 参见如下:
        In a given list.
        Example:

        Entry.objects.filter(id__in=[1, 3, 4])"""

        return related.model._base_manager.using(self.using).filter(
            **{"%s__in" % related.field.name: objs}
        )

    def instances_with_model(self):
        for model, instances in six.iteritems(self.data):
            for obj in instances:
                yield model, obj

    def sort(self):
        sorted_models = []
        concrete_models = set()
        models = list(self.data)
        while len(sorted_models) < len(models):
            found = False
            for model in models:
                if model in sorted_models:
                    continue
                dependencies = self.dependencies.get(model._meta.concrete_model)
                if not (dependencies and dependencies.difference(concrete_models)):
                    sorted_models.append(model)
                    concrete_models.add(model._meta.concrete_model)
                    found = True
            if not found:
                return
        self.data = SortedDict([(model, self.data[model])
                                for model in sorted_models])

    force_managed 修饰器是为了能再删除的时候能够进入事务
    @force_managed
    def delete(self):
        # sort instance collections
        for model, instances in self.data.items():
            self.data[model] = sorted(instances, key=attrgetter("pk"))

        # if possible, bring the models in an order suitable for databases that
        # don't support transactions or cannot defer constraint checks until the
        # end of a transaction.
        self.sort()

        # send pre_delete signals
        for model, obj in self.instances_with_model():
            if not model._meta.auto_created: 如果不是自动创建的??? 会发送信号, 为什么发送信号???
                signals.pre_delete.send(
                    sender=model, instance=obj, using=self.using
                )

        # fast deletes
        for qs in self.fast_deletes:
            # 调用的 QuerySet._raw_delete(), 其会调用 sql.DeleteQuery()
            qs._raw_delete(using=self.using)

        # update fields 为什么要更新???
        for model, instances_for_fieldvalues in six.iteritems(self.field_updates):
            query = sql.UpdateQuery(model)
            for (field, value), instances in six.iteritems(instances_for_fieldvalues):
                query.update_batch([obj.pk for obj in instances],
                                   {field.name: value}, self.using)

        # reverse instance collections
        for instances in six.itervalues(self.data): six.itervalues() 只迭代值, 默认会迭代 key
            instances.reverse()

        # delete batches
        for model, batches in six.iteritems(self.batches):
            query = sql.DeleteQuery(model)
            for field, instances in six.iteritems(batches):
                query.delete_batch([obj.pk for obj in instances], self.using, field)

        # delete instances
        for model, instances in six.iteritems(self.data):
            query = sql.DeleteQuery(model)
            pk_list = [obj.pk for obj in instances]
            query.delete_batch(pk_list, self.using)

        # send post_delete signals
        for model, obj in self.instances_with_model():
            if not model._meta.auto_created:
                signals.post_delete.send(
                    sender=model, instance=obj, using=self.using
                )

        # update collected instances
        for model, instances_for_fieldvalues in six.iteritems(self.field_updates):
            for (field, value), instances in six.iteritems(instances_for_fieldvalues):
                for obj in instances:
                    setattr(obj, field.attname, value)

        for model, instances in six.iteritems(self.data):
            for instance in instances:
                setattr(instance, model._meta.pk.attname, None)
