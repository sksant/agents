# coding=utf-8
# Copyright 2018 The TF-Agents Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Base extension to Keras network to simplify copy operations."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc
import six

import tensorflow as tf  # pylint: disable=g-explicit-tensorflow-version-import

from tensorflow.keras import layers  # pylint: disable=unused-import
from tf_agents.specs import tensor_spec
from tf_agents.trajectories import time_step
from tf_agents.utils import common
from tf_agents.utils import object_identity

# pylint:disable=g-direct-tensorflow-import
from tensorflow.python.keras.utils import layer_utils  # TF internal
from tensorflow.python.training.tracking import base  # TF internal
from tensorflow.python.util import tf_decorator  # TF internal
from tensorflow.python.util import tf_inspect  # TF internal
# pylint:enable=g-direct-tensorflow-import


class _NetworkMeta(abc.ABCMeta):
  """Meta class for Network object.

  We mainly use this class to capture all args to `__init__` of all `Network`
  instances, and store them in `instance._saved_kwargs`.  This in turn is
  used by the `instance.copy` method.
  """

  def __new__(mcs, classname, baseclasses, attrs):
    """Control the creation of subclasses of the Network class.

    Args:
      classname: The name of the subclass being created.
      baseclasses: A tuple of parent classes.
      attrs: A dict mapping new attributes to their values.

    Returns:
      The class object.

    Raises:
      RuntimeError: if the class __init__ has *args in its signature.
    """
    if baseclasses[0] == tf.keras.layers.Layer:
      # This is just Network below.  Return early.
      return abc.ABCMeta.__new__(mcs, classname, baseclasses, attrs)

    init = attrs.get("__init__", None)

    if not init:
      # This wrapper class does not define an __init__.  When someone creates
      # the object, the __init__ of its parent class will be called.  We will
      # call that __init__ instead separately since the parent class is also a
      # subclass of Network.  Here just create the class and return.
      return abc.ABCMeta.__new__(mcs, classname, baseclasses, attrs)

    arg_spec = tf_inspect.getargspec(init)
    if arg_spec.varargs is not None:
      raise RuntimeError(
          "%s.__init__ function accepts *args.  This is not allowed." %
          classname)

    def _capture_init(self, *args, **kwargs):
      """Captures init args and kwargs and stores them into `_saved_kwargs`."""
      if len(args) > len(arg_spec.args) + 1:
        # Error case: more inputs than args.  Call init so that the appropriate
        # error can be raised to the user.
        init(self, *args, **kwargs)
      # Convert to a canonical kwarg format.
      kwargs = tf_inspect.getcallargs(init, self, *args, **kwargs)
      kwargs.pop("self")
      init(self, **kwargs)
      # Avoid auto tracking which prevents keras from tracking layers that are
      # passed as kwargs to the Network.
      with base.no_automatic_dependency_tracking_scope(self):
        setattr(self, "_saved_kwargs", kwargs)

    attrs["__init__"] = tf_decorator.make_decorator(init, _capture_init)
    return abc.ABCMeta.__new__(mcs, classname, baseclasses, attrs)


@six.add_metaclass(_NetworkMeta)
class Network(tf.keras.layers.Layer):
  """Base extension to Keras network to simplify copy operations."""

  def __init__(self, input_tensor_spec, state_spec, name=None):
    """Creates an instance of `Network`.

    Args:
      input_tensor_spec: A nest of `tensor_spec.TensorSpec` representing the
        input observations.
      state_spec: A nest of `tensor_spec.TensorSpec` representing the state
        needed by the network. Use () if none.
      name: (Optional.) A string representing the name of the network.
    """
    super(Network, self).__init__(name=name)
    common.check_tf1_allowed()

    # Required for summary() to work.
    self._is_graph_network = False

    self._input_tensor_spec = input_tensor_spec
    self._state_spec = state_spec

  @property
  def state_spec(self):
    return self._state_spec

  @property
  def input_tensor_spec(self):
    """Returns the spec of the input to the network of type InputSpec."""
    return self._input_tensor_spec

  def create_variables(self, **kwargs):
    if not self.built:
      random_input = tensor_spec.sample_spec_nest(
          self.input_tensor_spec, outer_dims=(1,))
      random_state = tensor_spec.sample_spec_nest(
          self.state_spec, outer_dims=(1,))
      step_type = tf.fill((1,), time_step.StepType.FIRST)
      self.__call__(
          random_input, step_type=step_type, network_state=random_state,
          **kwargs)

  @property
  def variables(self):
    if not self.built:
      raise ValueError(
          "Network has not been built, unable to access variables.  "
          "Please call `create_variables` or apply the network first.")
    return super(Network, self).variables

  @property
  def trainable_variables(self):
    if not self.built:
      raise ValueError(
          "Network has not been built, unable to access variables.  "
          "Please call `create_variables` or apply the network first.")
    return super(Network, self).trainable_variables

  @property
  def layers(self):
    """Get the list of all (nested) sub-layers used in this Network."""
    return list(_filter_empty_layer_containers(self._layers))

  def get_layer(self, name=None, index=None):
    """Retrieves a layer based on either its name (unique) or index.

    If `name` and `index` are both provided, `index` will take precedence.
    Indices are based on order of horizontal graph traversal (bottom-up).

    Arguments:
        name: String, name of layer.
        index: Integer, index of layer.

    Returns:
        A layer instance.

    Raises:
        ValueError: In case of invalid layer name or index.
    """
    if index is not None and name is not None:
      raise ValueError("Provide only a layer name or a layer index.")

    if index is not None:
      if len(self.layers) <= index:
        raise ValueError("Was asked to retrieve layer at index " + str(index) +
                         " but model only has " + str(len(self.layers)) +
                         " layers.")
      else:
        return self.layers[index]

    if name is not None:
      for layer in self.layers:
        if layer.name == name:
          return layer
      raise ValueError("No such layer: " + name + ".")

  def summary(self, line_length=None, positions=None, print_fn=None):
    """Prints a string summary of the network.

    Args:
        line_length: Total length of printed lines
            (e.g. set this to adapt the display to different
            terminal window sizes).
        positions: Relative or absolute positions of log elements
            in each line. If not provided,
            defaults to `[.33, .55, .67, 1.]`.
        print_fn: Print function to use. Defaults to `print`.
            It will be called on each line of the summary.
            You can set it to a custom function
            in order to capture the string summary.

    Raises:
        ValueError: if `summary()` is called before the model is built.
    """
    if not self.built:
      raise ValueError("This model has not yet been built. "
                       "Build the model first by calling `build()` or "
                       "`__call__()` with some data, or `create_variables()`.")
    layer_utils.print_summary(self,
                              line_length=line_length,
                              positions=positions,
                              print_fn=print_fn)

  def copy(self, **kwargs):
    """Create a shallow copy of this network.

    **NOTE** Network layer weights are *never* copied.  This method recreates
    the `Network` instance with the same arguments it was initialized with
    (excepting any new kwargs).

    Args:
      **kwargs: Args to override when recreating this network.  Commonly
        overridden args include 'name'.

    Returns:
      A shallow copy of this network.
    """
    return type(self)(**dict(self._saved_kwargs, **kwargs))

  def __call__(self, inputs, *args, **kwargs):
    """A wrapper around `Network.call`.

    A typical `call` method in a class subclassing `Network` looks like this:

    ```python
    def call(self,
             observation,
             step_type=None,
             network_state=(),
             training=False):
        ...
        return outputs, new_network_state
    ```
    In this case, we will validate the first argument (`observation`)
    against `self.input_tensor_spec`.

    If a `network_state` kwarg is given it is also validated against
    `self.state_spec`.  Similarly, the return value
    of the `call` method is expected to be a tuple/list with 2 values:
    `(output, new_state)`; we validate `new_state` against `self.state_spec`.

    Args:
      inputs: The inputs to `self.call`, matching `self.input_state_spec`.
      *args: Additional arguments to `self.call`.
      **kwargs: Additional keyword arguments to `self.call`.

    Returns:
      A tuple `(outputs, new_network_state)`.
    """
    tf.nest.assert_same_structure(inputs, self.input_tensor_spec)
    network_state = kwargs.get("network_state", None)
    if network_state is not None:
      tf.nest.assert_same_structure(network_state, self.state_spec)
    outputs, new_state = super(Network, self).__call__(inputs, *args, **kwargs)
    tf.nest.assert_same_structure(new_state, self.state_spec)
    return outputs, new_state

  def _check_trainable_weights_consistency(self):
    """Check trainable weights count consistency.

    This method makes up for the missing method (b/143631010) of the same name
    in `keras.Network`, which is needed when calling `Network.summary()`. This
    method is a no op. If a Network wants to check the consistency of trainable
    weights, see `keras.Model._check_trainable_weights_consistency` as a
    reference.
    """
    # TODO(b/143631010): If recognized and fixed, remove this entire method.
    return

  def get_initial_state(self, batch_size=None):
    """Returns an initial state usable by the network.

    Args:
      batch_size: Tensor or constant: size of the batch dimension. Can be None
        in which case not dimensions gets added.

    Returns:
      A nested object of type `self.state_spec` containing properly
      initialized Tensors.
    """
    return self._get_initial_state(batch_size)

  def _get_initial_state(self, batch_size):
    """Returns the initial state of the policy network.

    Args:
      batch_size: A constant or Tensor holding the batch size. Can be None, in
        which case the state will not have a batch dimension added.

    Returns:
      A nest of zero tensors matching the spec of the policy network state.
    """
    return tensor_spec.zero_spec_nest(
        self._state_spec,
        outer_dims=None if batch_size is None else [batch_size])


class DistributionNetwork(Network):
  """Base class for networks which generate Distributions as their output."""

  def __init__(self, input_tensor_spec, state_spec, output_spec, name):
    super(DistributionNetwork, self).__init__(
        input_tensor_spec=input_tensor_spec, state_spec=state_spec, name=name)
    self._output_spec = output_spec

  @property
  def output_spec(self):
    return self._output_spec


def _is_layer(obj):
  """Implicit check for Layer-like objects."""
  # TODO(b/110718070): Replace with isinstance(obj, tf.keras.layers.Layer).
  return hasattr(obj, "_is_layer") and not isinstance(obj, type)


def _filter_empty_layer_containers(layer_list):
  """Remove empty layer containers."""
  existing = object_identity.ObjectIdentitySet()
  to_visit = layer_list[::-1]
  while to_visit:
    obj = to_visit.pop()
    if obj in existing:
      continue
    existing.add(obj)
    if _is_layer(obj):
      yield obj
    else:
      sub_layers = getattr(obj, "layers", None) or []

      # Trackable data structures will not show up in ".layers" lists, but
      # the layers they contain will.
      to_visit.extend(sub_layers[::-1])
