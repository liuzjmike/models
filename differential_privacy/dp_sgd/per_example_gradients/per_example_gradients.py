# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================

"""Per-example gradients for selected ops."""

import collections

import numpy as np
import tensorflow as tf

OrderedDict = collections.OrderedDict


def _ListUnion(list_1, list_2):
  """Returns the union of two lists.

  Python sets can have a non-deterministic iteration order. In some
  contexts, this could lead to TensorFlow producing two different
  programs when the same Python script is run twice. In these contexts
  we use lists instead of sets.

  This function is not designed to be especially fast and should only
  be used with small lists.

  Args:
    list_1: A list
    list_2: Another list

  Returns:
    A new list containing one copy of each unique element of list_1 and
    list_2. Uniqueness is determined by "x in union" logic; e.g. two
    string of that value appearing in the union.

  Raises:
    TypeError: The arguments are not lists.
  """

  if not (isinstance(list_1, list) and isinstance(list_2, list)):
    raise TypeError("Arguments must be lists.")

  union = []
  for x in list_1 + list_2:
    if x not in union:
      union.append(x)

  return union


def Interface(ys, xs):
  """Maps xs to consumers.

    Returns a dict mapping each element of xs to any of its consumers that are
    indirectly consumed by ys.

  Args:
    ys: The outputs
    xs: The inputs
  Returns:
    out: Dict mapping each member x of `xs` to a list of all Tensors that are
         direct consumers of x and are eventually consumed by a member of
         `ys`.
  """

  if isinstance(ys, (list, tuple)):
    queue = list(ys)
  else:
    queue = [ys]

  out = OrderedDict()
  if isinstance(xs, (list, tuple)):
    for x in xs:
      out[x] = []
  else:
    out[xs] = []

  done = set()

  while queue:
    y = queue.pop()
    if y in done:
      continue
    done = done.union(set([y]))
    for x in y.op.inputs:
      if x in out:
        out[x].append(y)
      else:
        assert id(x) not in [id(foo) for foo in out]
    queue.extend(y.op.inputs)

  return out


class PXGRegistry(object):
  """Per-Example Gradient registry.

  Maps names of ops to per-example gradient rules for those ops.
  These rules are only needed for ops that directly touch values that
  are shared between examples. For most machine learning applications,
  this means only ops that directly operate on the parameters.


  See http://arxiv.org/abs/1510.01799 for more information, and please
  consider citing that tech report if you use this function in published
  research.
  """

  def __init__(self):
    self.d = OrderedDict()

  def __call__(self, op,
               colocate_gradients_with_ops=False,
               gate_gradients=False):
    if op.node_def.op not in self.d:
      raise NotImplementedError("No per-example gradient rule registered "
                                "for " + op.node_def.op + " in pxg_registry.")
    return self.d[op.node_def.op](op,
                                  colocate_gradients_with_ops,
                                  gate_gradients)

  def Register(self, op_name, pxg_class):
    """Associates `op_name` key with `pxg_class` value.

    Registers `pxg_class` as the class that will be called to perform
    per-example differentiation through ops with `op_name`.

    Args:
      op_name: String op name.
      pxg_class: An instance of any class with the same signature as MatMulPXG.
    """
    self.d[op_name] = pxg_class


pxg_registry = PXGRegistry()


class MatMulPXG(object):
  """Per-example gradient rule for MatMul op.
  """

  def __init__(self, op,
               colocate_gradients_with_ops=False,
               gate_gradients=False):
    """Construct an instance of the rule for `op`.

    Args:
      op: The Operation to differentiate through.
      colocate_gradients_with_ops: currently unsupported
      gate_gradients: currently unsupported
    """
    assert op.node_def.op == "MatMul"
    self.op = op
    self.colocate_gradients_with_ops = colocate_gradients_with_ops
    self.gate_gradients = gate_gradients

  def __call__(self, x, z_grads):
    """Build the graph for the per-example gradient through the op.

    Assumes that the MatMul was called with a design matrix with examples
    in rows as the first argument and parameters as the second argument.

    Args:
      x: The Tensor to differentiate with respect to. This tensor must
         represent the weights.
      z_grads: The list of gradients on the output of the op.

    Returns:
      x_grads: A Tensor containing the gradient with respect to `x` for
       each example. This is a 3-D tensor, with the first axis corresponding
       to examples and the remaining axes matching the shape of x.
    """
    idx = list(self.op.inputs).index(x)
    assert idx != -1
    assert len(z_grads) == len(self.op.outputs)
    assert idx == 1  # We expect weights to be arg 1
    # We don't expect anyone to per-example differentiate with repsect
    # to anything other than the weights.
    x, _ = self.op.inputs
    z_grads, = z_grads
    x_expanded = tf.expand_dims(x, 2)
    z_grads_expanded = tf.expand_dims(z_grads, 1)
    return tf.multiply(x_expanded, z_grads_expanded)


pxg_registry.Register("MatMul", MatMulPXG)


class Conv2DPXG(object):
  """Per-example gradient rule of Conv2d op.

  Same interface as MatMulPXG.
  """

  def __init__(self, op,
               colocate_gradients_with_ops=False,
               gate_gradients=False):

    assert op.node_def.op == "Conv2D"
    self.op = op
    self.colocate_gradients_with_ops = colocate_gradients_with_ops
    self.gate_gradients = gate_gradients

  def __call__(self, w, z_grads):
    idx = list(self.op.inputs).index(w)
    # Make sure that `op` was actually applied to `w`
    assert idx != -1
    assert len(z_grads) == len(self.op.outputs)
    # The following assert may be removed when we are ready to use this
    # for general purpose code.
    # This assert is only expected to hold in the contex of our preliminary
    # MNIST experiments.
    assert idx == 1  # We expect convolution weights to be arg 1

    images, _ = self.op.inputs
    strides = self.op.get_attr("strides")
    padding = self.op.get_attr("padding")
    data_format = self.op.get_attr("data_format")
    
    def _conv2d_one_example_grad(x):
      image, grad = x
      assert image.get_shape().ndims == 4
      assert grad.get_shape().ndims == 4
      return tf.nn.conv2d_backprop_filter(image, w.get_shape(), grad
                                          strides, padding,
                                          data_format=data_format)

    images = tf.expand_dims(images, axis=1)
    z_grads = tf.transpose(z_grads, perm=[1, 0, 2, 3, 4])
    grads = tf.map_fn(_conv2d_one_example_grad,
                      (images, z_grads),
                      dtype=tf.float32)
    return tf.concat(grads, 0)


pxg_registry.Register("Conv2D", Conv2DPXG)


class AddPXG(object):
  """Per-example gradient rule for Add op.

  Same interface as MatMulPXG.
  """

  def __init__(self, op,
               colocate_gradients_with_ops=False,
               gate_gradients=False):

    assert op.node_def.op == "Add"
    self.op = op
    self.colocate_gradients_with_ops = colocate_gradients_with_ops
    self.gate_gradients = gate_gradients

  def __call__(self, x, z_grads):
    idx = list(self.op.inputs).index(x)
    # Make sure that `op` was actually applied to `x`
    assert idx != -1
    assert len(z_grads) == len(self.op.outputs)
    # The following assert may be removed when we are ready to use this
    # for general purpose code.
    # This assert is only expected to hold in the contex of our preliminary
    # MNIST experiments.
    assert idx == 1 # We expect biases to be arg 1
    # We don't expect anyone to per-example differentiate with respect
    # to anything other than the biases.
    x, _ = self.op.inputs
    z_grads, = z_grads
    return z_grads

  
pxg_registry.Register("Add", AddPXG)


def PerExampleGradients(ys, xs, grad_ys=None, name="gradients",
                        colocate_gradients_with_ops=False,
                        gate_gradients=False):
  """Symbolic differentiation, separately for each example.

  Matches the interface of tf.gradients, but the return values each have an
  additional axis corresponding to the examples.

  Assumes that the cost in `ys` is additive across examples.
  e.g., no batch normalization.
  Individual rules for each op specify their own assumptions about how
  examples are put into tensors.
  """

  # Find the interface between the xs and the cost
  for x in xs:
    assert isinstance(x, tf.Tensor), type(x)
  interface = Interface(ys, xs)
  merged_interface = []
  for x in xs:
    merged_interface = _ListUnion(merged_interface, interface[x])
  # Differentiate with respect to the interface
  interface_gradients = tf.gradients(ys, merged_interface, grad_ys=grad_ys,
                                     name=name,
                                     colocate_gradients_with_ops=
                                     colocate_gradients_with_ops,
                                     gate_gradients=gate_gradients)
  grad_dict = OrderedDict(zip(merged_interface, interface_gradients))
  # Build the per-example gradients with respect to the xs
  if colocate_gradients_with_ops:
    raise NotImplementedError("The per-example gradients are not yet "
                              "colocated with ops.")
  if gate_gradients:
    raise NotImplementedError("The per-example gradients are not yet "
                              "gated.")
  out = []
  for x in xs:
    zs = interface[x]
    ops = []
    for z in zs:
      ops = _ListUnion(ops, [z.op])
    if len(ops) != 1:
      raise NotImplementedError("Currently we only support the case "
                                "where each x is consumed by exactly "
                                "one op. but %s is consumed by %d ops."
                                % (x.name, len(ops)))
    op = ops[0]
    pxg_rule = pxg_registry(op, colocate_gradients_with_ops, gate_gradients)
    x_grad = pxg_rule(x, [grad_dict[z] for z in zs])
    out.append(x_grad)
  return out
