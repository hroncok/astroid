# copyright 2003-2015 LOGILAB S.A. (Paris, FRANCE), all rights reserved.
# contact http://www.logilab.fr/ -- mailto:contact@logilab.fr
#
# This file is part of astroid.
#
# astroid is free software: you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by the
# Free Software Foundation, either version 2.1 of the License, or (at your
# option) any later version.
#
# astroid is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License
# for more details.
#
# You should have received a copy of the GNU Lesser General Public License along
# with astroid. If not, see <http://www.gnu.org/licenses/>.

"""
Inference objects are a way to represent objects which are available
only at runtime, so they can't be found in the original AST tree.
For instance, inferring the following frozenset use, leads to an inferred
FrozenSet:

    Call(func=Name('frozenset'), args=Tuple(...))
"""
import sys
import types

import six

from astroid import context as contextmod
from astroid import decorators
from astroid import exceptions
from astroid.interpreter.util import infer_stmts
from astroid.interpreter import runtimeabc
from astroid.tree import base
from astroid.tree import treeabc
from astroid import util

manager = util.lazy_import('manager')
objectmodel = util.lazy_import('interpreter.objectmodel')
MANAGER = manager.AstroidManager()

BUILTINS = six.moves.builtins.__name__


if sys.version_info >= (3, 0):
    BOOL_SPECIAL_METHOD = '__bool__'
else:
    BOOL_SPECIAL_METHOD = '__nonzero__'
PROPERTIES = {BUILTINS + '.property', 'abc.abstractproperty'}
# List of possible property names. We use this list in order
# to see if a method is a property or not. This should be
# pretty reliable and fast, the alternative being to check each
# decorator to see if its a real property-like descriptor, which
# can be too complicated.
# Also, these aren't qualified, because each project can
# define them, we shouldn't expect to know every possible
# property-like decorator!
# TODO(cpopa): just implement descriptors already.
POSSIBLE_PROPERTIES = {"cached_property", "cachedproperty",
                       "lazyproperty", "lazy_property", "reify",
                       "lazyattribute", "lazy_attribute",
                       "LazyProperty", "lazy"}


def is_property(meth):
    if PROPERTIES.intersection(meth.decoratornames()):
        return True
    stripped = {name.split(".")[-1] for name in meth.decoratornames()
                if name is not util.Uninferable}
    return any(name in stripped for name in POSSIBLE_PROPERTIES)


class Proxy(object):
    """a simple proxy object"""

    _proxied = None # proxied object may be set by class or by instance

    def __init__(self, proxied=None):
        if proxied is not None:
            self._proxied = proxied

    def __getattr__(self, name):
        if name == '_proxied':
            return getattr(self.__class__, '_proxied')
        if name in self.__dict__:
            return self.__dict__[name]
        return getattr(self._proxied, name)

    def infer(self, context=None):
        yield self


def _infer_method_result_truth(instance, method_name, context):
    # Get the method from the instance and try to infer
    # its return's truth value.
    meth = next(instance.igetattr(method_name, context=context), None)
    if meth and hasattr(meth, 'infer_call_result'):
        if not meth.callable():
            return util.Uninferable
        for value in meth.infer_call_result(instance, context=context):
            if value is util.Uninferable:
                return value

            inferred = next(value.infer(context=context))
            return inferred.bool_value()
    return util.Uninferable


class BaseInstance(Proxy):
    """An instance base class, which provides lookup methods for potential instances."""

    special_attributes = None

    def display_type(self):
        return 'Instance of'

    def getattr(self, name, context=None, lookupclass=True):
        try:
            values = self._proxied.instance_attr(name, context)
        except exceptions.AttributeInferenceError:
            if self.special_attributes and name in self.special_attributes:
                return [self.special_attributes.lookup(name)]

            if lookupclass:
                # Class attributes not available through the instance
                # unless they are explicitly defined.
                return self._proxied.getattr(name, context,
                                             class_context=False)

            util.reraise(exceptions.AttributeInferenceError(target=self,
                                                            attribute=name,
                                                            context=context))
        # since we've no context information, return matching class members as
        # well
        if lookupclass:
            try:
                return values + self._proxied.getattr(name, context,
                                                      class_context=False)
            except exceptions.AttributeInferenceError:
                pass
        return values

    def igetattr(self, name, context=None):
        """inferred getattr"""
        if not context:
            context = contextmod.InferenceContext()
        try:
            # avoid recursively inferring the same attr on the same class
            if context.push((self._proxied, name)):
                return

            # XXX frame should be self._proxied, or not ?
            attrs = self.getattr(name, context, lookupclass=False)
            for stmt in infer_stmts(self._wrap_attr(attrs, context),
                                    context, frame=self):
                yield stmt
        except exceptions.AttributeInferenceError:
            try:
                # fallback to class'igetattr since it has some logic to handle
                # descriptors
                attrs = self._proxied.igetattr(name, context, class_context=False) 
                for stmt in self._wrap_attr(attrs, context):
                    yield stmt
            except exceptions.AttributeInferenceError as error:
                util.reraise(exceptions.InferenceError(**vars(error)))

    def _wrap_attr(self, attrs, context=None):
        """wrap bound methods of attrs in a InstanceMethod proxies"""
        for attr in attrs:
            if isinstance(attr, UnboundMethod):
                if is_property(attr):
                    for inferred in attr.infer_call_result(self, context):
                        yield inferred
                else:
                    yield BoundMethod(attr._proxied, self)
            elif hasattr(attr, 'name') and attr.name == '<lambda>':
                # This is a lambda function defined at class level,
                # since its scope is the underlying _proxied class.
                # Unfortunately, we can't do an isinstance check here,
                # because of the circular dependency between astroid.bases
                # and astroid.scoped_nodes.
                if isinstance(attr.statement().scope(), treeabc.ClassDef):
                    if attr.args.args and attr.args.args[0].name == 'self':
                        yield BoundMethod(attr, self)
                        continue
                yield attr
            else:
                yield attr

    def infer_call_result(self, caller, context=None):
        """infer what a class instance is returning when called"""
        inferred = False
        for node in self._proxied.igetattr('__call__', context):
            if node is util.Uninferable or not node.callable():
                continue
            for res in node.infer_call_result(caller, context):
                inferred = True
                yield res
        if not inferred:
            raise exceptions.InferenceError(node=self, caller=caller,
                                            context=context)


@util.register_implementation(runtimeabc.Instance)
class Instance(BaseInstance):
    """A special node representing a class instance."""

    special_attributes = util.lazy_descriptor(lambda: objectmodel.InstanceModel())

    def __repr__(self):
        return '<Instance of %s.%s at 0x%s>' % (self._proxied.root().name,
                                                self._proxied.name,
                                                id(self))
    def __str__(self):
        return 'Instance of %s.%s' % (self._proxied.root().name,
                                      self._proxied.name)

    def callable(self):
        try:
            self._proxied.getattr('__call__', class_context=False)
            return True
        except exceptions.AttributeInferenceError:
            return False

    def pytype(self):
        return self._proxied.qname()

    def bool_value(self):
        """Infer the truth value for an Instance

        The truth value of an instance is determined by these conditions:

           * if it implements __bool__ on Python 3 or __nonzero__
             on Python 2, then its bool value will be determined by
             calling this special method and checking its result.
           * when this method is not defined, __len__() is called, if it
             is defined, and the object is considered true if its result is
             nonzero. If a class defines neither __len__() nor __bool__(),
             all its instances are considered true.
        """
        context = contextmod.InferenceContext()
        context.callcontext = contextmod.CallContext(args=[self])

        try:
            result = _infer_method_result_truth(self, BOOL_SPECIAL_METHOD, context)
        except (exceptions.InferenceError, exceptions.AttributeInferenceError):
            # Fallback to __len__.
            try:
                result = _infer_method_result_truth(self, '__len__', context)
            except (exceptions.AttributeInferenceError, exceptions.InferenceError):
                return True
        return result

    def getitem(self, index, context=None):
        if context:
            new_context = context.clone()
        else:
            context = new_context = contextmod.InferenceContext()

        # Create a new callcontext for providing index as an argument.
        new_context.callcontext = contextmod.CallContext(args=[index])
        new_context.boundnode = self

        method = next(self.igetattr('__getitem__', context=context))
        if not isinstance(method, BoundMethod):
            raise exceptions.InferenceError(
                'Could not find __getitem__ for {node!r}.',
                node=self, context=context)

        try:
            return next(method.infer_call_result(self, new_context))
        except StopIteration:
            util.reraise(exceptions.InferenceError(
                message='Inference for {node!r}[{index!s}] failed.',
                node=self, index=index, context=context))


class Method(Proxy):

    def __repr__(self):
        frame = self._proxied.parent.frame()
        return '<%s %s of %s at 0x%s' % (self.__class__.__name__,
                                         self._proxied.name,
                                         frame.qname(), id(self))

    def getattr(self, name, context=None):
        if name in self.special_attributes:
            return [self.special_attributes.lookup(name)]
        return self._proxied.getattr(name, context)

    def igetattr(self, name, context=None):
        if name in self.special_attributes:
            return iter((self.special_attributes.lookup(name), ))
        return self._proxied.igetattr(name, context)

    def bool_value(self):
        return True


@util.register_implementation(runtimeabc.UnboundMethod)
class UnboundMethod(Method):
    """a special node representing a method not bound to an instance"""

    special_attributes = util.lazy_descriptor(lambda: objectmodel.UnboundMethodModel())

    def infer_call_result(self, caller, context):
        # If we're unbound method __new__ of builtin object, the result is an
        # instance of the class given as first argument.
        if (self._proxied.name == '__new__' and
                self._proxied.parent.frame().qname() == '%s.object' % BUILTINS):
            infer = caller.args[0].infer() if caller.args else []
            return ((x is util.Uninferable and x or Instance(x)) for x in infer)
        return self._proxied.infer_call_result(caller, context)

    def is_bound(self):
        return False


@util.register_implementation(runtimeabc.BoundMethod)
class BoundMethod(UnboundMethod):
    """a special node representing a method bound to an instance"""

    special_attributes = util.lazy_descriptor(lambda: objectmodel.BoundMethodModel())

    def __init__(self, proxy, bound):
        UnboundMethod.__init__(self, proxy)
        self.bound = bound

    def is_bound(self):
        return True

    def infer_call_result(self, caller, context=None):
        if context is None:
            context = contextmod.InferenceContext()
        context = context.clone()
        context.boundnode = self.bound
        return super(BoundMethod, self).infer_call_result(caller, context)


@util.register_implementation(runtimeabc.Generator)
class Generator(BaseInstance):
    """a special node representing a generator.

    Proxied class is set once for all in raw_building.
    """

    special_attributes = util.lazy_descriptor(lambda: objectmodel.GeneratorModel())

    def __init__(self, parent):
        self.parent = parent

    def callable(self):
        return False

    def pytype(self):
        return '%s.generator' % BUILTINS

    def display_type(self):
        return 'Generator'

    def bool_value(self):
        return True

    def __repr__(self):
        return '<Generator(%s) l.%s at 0x%s>' % (self._proxied.name, self.lineno, id(self))

    def __str__(self):
        return 'Generator(%s)' % (self._proxied.name)

    @decorators.cachedproperty
    def _proxied(self):
        builtins = MANAGER.builtins()
        return builtins.getattr(types.GeneratorType.__name__)[0]


@util.register_implementation(runtimeabc.FrozenSet)
class FrozenSet(base.BaseContainer, Instance):
    """Class representing a FrozenSet composite node."""

    def pytype(self):
        return '%s.frozenset' % BUILTINS

    @decorators.cachedproperty
    def _proxied(self):
        builtins = MANAGER.builtins()
        return builtins.getattr('frozenset')[0]


@util.register_implementation(runtimeabc.Super)
class Super(base.NodeNG):
    """Proxy class over a super call.

    This class offers almost the same behaviour as Python's super,
    which is MRO lookups for retrieving attributes from the parents.

    The *mro_pointer* is the place in the MRO from where we should
    start looking, not counting it. *mro_type* is the object which
    provides the MRO, it can be both a type or an instance.
    *self_class* is the class where the super call is, while
    *scope* is the function where the super call is.
    """

    # Need to make this lazy, due to circular dependencies.
    special_attributes = util.lazy_descriptor(lambda: objectmodel.SuperModel())

    def __init__(self, mro_pointer, mro_type, self_class, scope):
        self.type = mro_type
        self.mro_pointer = mro_pointer
        self._class_based = False
        self._self_class = self_class
        self._scope = scope

    def super_mro(self):
        """Get the MRO which will be used to lookup attributes in this super."""
        if not isinstance(self.mro_pointer, treeabc.ClassDef):
            raise exceptions.SuperError(
                "The first argument to super must be a subtype of "
                "type, not {mro_pointer}.", super_=self)

        if isinstance(self.type, treeabc.ClassDef):
            # `super(type, type)`, most likely in a class method.
            self._class_based = True
            mro_type = self.type
        else:
            mro_type = getattr(self.type, '_proxied', None)
            if not isinstance(mro_type, (runtimeabc.Instance, treeabc.ClassDef)):
                raise exceptions.SuperError(
                    "The second argument to super must be an "
                    "instance or subtype of type, not {type}.",
                    super_=self)

        if not mro_type.newstyle:
            raise exceptions.SuperError("Unable to call super on old-style classes.", super_=self)

        mro = mro_type.mro()
        if self.mro_pointer not in mro:
            raise exceptions.SuperError(
                "The second argument to super must be an "
                "instance or subtype of type, not {type}.",
                super_=self)

        index = mro.index(self.mro_pointer)
        return mro[index + 1:]

    @decorators.cachedproperty
    def _proxied(self):
        builtins = MANAGER.builtins()
        return builtins.getattr('super')[0]

    def pytype(self):
        return '%s.super' % BUILTINS

    def display_type(self):
        return 'Super of'

    @property
    def name(self):
        """Get the name of the MRO pointer."""
        return self.mro_pointer.name

    def igetattr(self, name, context=None):
        """Retrieve the inferred values of the given attribute name."""

        if name in self.special_attributes:
            yield self.special_attributes.lookup(name)
            return

        try:
            mro = self.super_mro()
        # Don't let invalid MROs or invalid super calls
        # leak out as is from this function.
        except exceptions.SuperError as exc:
            msg = ('Lookup for {name} on {target!r} because super call {super!r} '
                  'is invalid.'),
            structured = exceptions.AttributeInferenceError(msg, target=self,
                                                            attribute=name,
                                                            context=context,
                                                            super_=exc.super_)
            util.reraise(structured)
        except exceptions.MroError as exc:
            msg = ('Lookup for {name} on {target!r} failed because {cls!r} has an '
                   'invalid MRO.')
            structured = exceptions.AttributeInferenceError(msg, target=self,
                                                            attribute=name, context=context,
                                                            mros=exc.mros, cls=exc.cls)
            util.reraise(structured)

        found = False
        for cls in mro:
            if name not in cls.locals: # and name not in cls.external_attrs:
                continue

            found = True
            for inferred in infer_stmts([cls[name]], context, frame=self):
                if not isinstance(inferred, treeabc.FunctionDef):
                    yield inferred
                    continue

                # We can obtain different descriptors from a super depending
                # on what we are accessing and where the super call is.
                if inferred.type == 'classmethod':
                    yield BoundMethod(inferred, cls)
                elif self._scope.type == 'classmethod' and inferred.type == 'method':
                    yield inferred
                elif self._class_based or inferred.type == 'staticmethod':
                    yield inferred
                elif is_property(inferred):
                    # TODO: support other descriptors as well.
                    for value in inferred.infer_call_result(self, context):
                        yield value
                else:
                    yield BoundMethod(inferred, cls)

        if not found:
            raise exceptions.AttributeInferenceError(target=self,
                                                     attribute=name,
                                                     context=context)

    def getattr(self, name, context=None):
        return list(self.igetattr(name, context=context))
