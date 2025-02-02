"""Abstract representation of instances."""

import logging

from pytype import datatypes
from pytype.abstract import _base
from pytype.abstract import abstract_utils
from pytype.abstract import class_mixin
from pytype.abstract import function

log = logging.getLogger(__name__)


class SimpleValue(_base.BaseValue):
  """A basic abstract value that represents instances.

  This class implements instances in the Python sense. Instances of the same
  class may vary.

  Note that the cls attribute will point to another abstract value that
  represents the class object itself, not to some special type representation.

  Attributes:
    members: A name->value dictionary of the instance's attributes.
  """

  def __init__(self, name, ctx):
    """Initialize a SimpleValue.

    Args:
      name: Name of this value. For debugging and error reporting.
      ctx: The abstract context.
    """
    super().__init__(name, ctx)
    self._cls = None  # lazily loaded 'cls' attribute
    self.members = datatypes.MonitorDict()
    # Lazily loaded to handle recursive types.
    # See Instance._load_instance_type_parameters().
    self._instance_type_parameters = datatypes.AliasingMonitorDict()
    # This attribute depends on self.cls, which isn't yet set to its true value.
    self._maybe_missing_members = None
    # The latter caches the result of get_type_key. This is a recursive function
    # that has the potential to generate too many calls for large definitions.
    self._cached_type_key = (
        (self.members.changestamp, self._instance_type_parameters.changestamp),
        None)

  @property
  def instance_type_parameters(self):
    return self._instance_type_parameters

  @property
  def maybe_missing_members(self):
    if self._maybe_missing_members is None:
      self._maybe_missing_members = isinstance(
          self.cls, class_mixin.Class) and self.cls.is_dynamic
    return self._maybe_missing_members

  @maybe_missing_members.setter
  def maybe_missing_members(self, v):
    self._maybe_missing_members = v

  def has_instance_type_parameter(self, name):
    """Check if the key is in `instance_type_parameters`."""
    name = abstract_utils.full_type_name(self, name)
    return name in self.instance_type_parameters

  def get_children_maps(self):
    return (self.instance_type_parameters, self.members)

  def get_instance_type_parameter(self, name, node=None):
    name = abstract_utils.full_type_name(self, name)
    param = self.instance_type_parameters.get(name)
    if not param:
      log.info("Creating new empty type param %s", name)
      if node is None:
        node = self.ctx.root_node
      param = self.ctx.program.NewVariable([], [], node)
      self.instance_type_parameters[name] = param
    return param

  def merge_instance_type_parameter(self, node, name, value):
    """Set the value of a type parameter.

    This will always add to the type parameter unlike set_attribute which will
    replace value from the same basic block. This is because type parameters may
    be affected by a side effect so we need to collect all the information
    regardless of multiple assignments in one basic block.

    Args:
      node: Optionally, the current CFG node.
      name: The name of the type parameter.
      value: The value that is being used for this type parameter as a Variable.
    """
    name = abstract_utils.full_type_name(self, name)
    log.info("Modifying type param %s", name)
    if name in self.instance_type_parameters:
      self.instance_type_parameters[name].PasteVariable(value, node)
    else:
      self.instance_type_parameters[name] = value

  def call(self, node, _, args, alias_map=None):
    node, var = self.ctx.attribute_handler.get_attribute(
        node, self, "__call__", self.to_binding(node))
    if var is not None and var.bindings:
      return function.call_function(self.ctx, node, var, args)
    else:
      raise function.NotCallable(self)

  def argcount(self, node):
    node, var = self.ctx.attribute_handler.get_attribute(
        node, self, "__call__", self.to_binding(node))
    if var and var.bindings:
      return min(v.argcount(node) for v in var.data)
    else:
      # It doesn't matter what we return here, since any attempt to call this
      # value will lead to a not-callable error anyways.
      return 0

  def __repr__(self):
    return "<%s [%r]>" % (self.name, self.cls)

  def _get_class(self):
    return self.ctx.convert.unsolvable

  @property
  def cls(self):
    if not self.ctx.converter_minimally_initialized:
      return self.ctx.convert.unsolvable
    if not self._cls:
      self._cls = self.ctx.convert.unsolvable  # prevent infinite recursion
      self._cls = self._get_class()
    return self._cls

  @cls.setter
  def cls(self, cls):
    self._cls = cls

  def set_class(self, node, var):
    """Set the __class__ of an instance, for code that does "x.__class__ = y."""
    # Simplification: Setting __class__ is done rarely, and supporting this
    # action would complicate pytype considerably by forcing us to track the
    # class in a variable, so we instead fall back to Any.
    try:
      new_cls = abstract_utils.get_atomic_value(var)
    except abstract_utils.ConversionError:
      self.cls = self.ctx.convert.unsolvable
    else:
      if self.cls != new_cls:
        self.cls = self.ctx.convert.unsolvable
    return node

  def get_type_key(self, seen=None):
    cached_changestamps, saved_key = self._cached_type_key
    if saved_key and cached_changestamps == (
        self.members.changestamp,
        self.instance_type_parameters.changestamp):
      return saved_key
    if not seen:
      seen = set()
    seen.add(self)
    key = {self.cls}
    for name, var in self.instance_type_parameters.items():
      subkey = frozenset(
          value.data.get_default_type_key()  # pylint: disable=g-long-ternary
          if value.data in seen else value.data.get_type_key(seen)
          for value in var.bindings)
      key.add((name, subkey))
    if key:
      type_key = frozenset(key)
    else:
      type_key = super().get_type_key()
    self._cached_type_key = (
        (self.members.changestamp, self.instance_type_parameters.changestamp),
        type_key)
    return type_key

  def _unique_parameters(self):
    parameters = super()._unique_parameters()
    parameters.extend(self.instance_type_parameters.values())
    return parameters

  def instantiate(self, node, container=None):
    return Instance(self, self.ctx, container).to_variable(node)


class Instance(SimpleValue):
  """An instance of some object."""

  def __init__(self, cls, ctx, container=None):
    super().__init__(cls.name, ctx)
    self.cls = cls
    self._instance_type_parameters_loaded = False
    self._container = container
    cls.register_instance(self)

  def _load_instance_type_parameters(self):
    if self._instance_type_parameters_loaded:
      return
    all_formal_type_parameters = datatypes.AliasingMonitorDict()
    abstract_utils.parse_formal_type_parameters(
        self.cls, None, all_formal_type_parameters, self._container)
    self._instance_type_parameters.uf = all_formal_type_parameters.uf
    for name, param in all_formal_type_parameters.items():
      if param is None:
        value = self.ctx.program.NewVariable()
        log.info("Initializing type param %s: %r", name, value)
        self._instance_type_parameters[name] = value
      else:
        self._instance_type_parameters[name] = param.instantiate(
            self.ctx.root_node, self._container or self)
    # We purposely set this flag at the very end so that accidentally accessing
    # instance_type_parameters during loading will trigger an obvious crash due
    # to infinite recursion, rather than silently returning an incomplete dict.
    self._instance_type_parameters_loaded = True

  @property
  def full_name(self):
    return self.cls.full_name

  @property
  def instance_type_parameters(self):
    self._load_instance_type_parameters()
    return self._instance_type_parameters
