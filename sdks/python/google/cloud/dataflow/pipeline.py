# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Pipeline, the top-level Dataflow object.

A pipeline holds a DAG of data transforms. Conceptually the nodes of the DAG
are transforms (PTransform objects) and the edges are values (mostly PCollection
objects). The transforms take as inputs one or more PValues and output one or
more PValues.

The pipeline offers functionality to traverse the graph (completely or
partially). The actual operation to be executed for each node visited is
specified through a runner object.

Typical usage:

  # Create a pipeline object using a local runner for execution.
  pipeline = Pipeline(runner=DirectPipelineRunner())

  # Add to the pipeline a "Create" transform. When executed this
  # transform will produce a PCollection object with the specified values.
  pcoll = pipeline.create('label', [1, 2, 3])

  # get() will execute part of the DAG stored in the pipeline needed to
  # materialize the pcoll value. The execution of the nodes visited is done
  # using the specified local runner.
  print pcoll.get()

"""

from __future__ import absolute_import

import argparse

from google.cloud.dataflow import error
from google.cloud.dataflow import pvalue
from google.cloud.dataflow import typehints
from google.cloud.dataflow.runners import create_runner
from google.cloud.dataflow.runners import PipelineRunner
from google.cloud.dataflow.transforms import format_full_label
from google.cloud.dataflow.transforms import ptransform
from google.cloud.dataflow.typehints import TypeCheckError


class Pipeline(object):
  """A pipeline object that manages a DAG of PValues and their PTransforms.

  Conceptually the PValues are the DAG's nodes and the PTransforms computing
  the PValues are the edges.

  All the transforms applied to the pipeline must have distinct full labels.
  If same transform instance needs to be applied then a clone should be created
  with a new label (e.g., transform.clone('new label')).
  """

  def __init__(self, runner=None, options=None):
    """Initialize a pipeline object.

    Args:
      runner: The pipeline runner that will be used to execute the pipeline.
        For registered runners, the runner name can be specified, otherwise
        a runner object must be supplied.
      options: An object containing parsed arguments as it is returned by an
        ArgumentParser instance created using the standard argparse module
        (see https://docs.python.org/2/library/argparse.html).

    Raises:
      TypeError: if the runner argument is not of type Runner or None.
    """
    self.options = options
    if self.options and runner is None:
      runner = self.options.runner

    if isinstance(runner, str):
      runner = create_runner(runner)
    elif not isinstance(runner, PipelineRunner):
      raise TypeError('Runner must be a PipelineRunner object or the '
                      'name of a registered runner.')
    # List of PValue objects representing a DAG of transformations.
    self._nodes = []
    # Default runner to be used when when PCollection.get() is called.
    self.runner = runner
    # Stack of transforms generated by nested apply() calls. The stack will
    # contain a root node as an enclosing (parent) node for top transforms.
    self.transforms_stack = [AppliedPTransform(None, None, '', None)]
    # Set of transform labels (full labels) applied to the pipeline.
    # If a transform is applied and the full label is already in the set
    # then the transform will have to be cloned with a new label.
    self.applied_labels = set()

  def _add_pvalue(self, pval):
    """Adds a PValue to the pipeline's node list."""
    if pval not in self._nodes:
      self._nodes.append(pval)

  def _current_transform(self):
    """Returns the transform currently on the top of the stack."""
    return self.transforms_stack[-1]

  def _root_transform(self):
    """Returns the root transform of the transform stack."""
    return self.transforms_stack[0]

  def run(self):
    """Runs the pipeline. Returns whatever our runner returns after running."""
    return self.runner.run(self)

  def visit(self, visitor, node=None):
    """Visits depth-first every node of a pipeline's DAG.

    If node is specified then only that node's predecessors (inputs and
    recursively their creating transforms) and outputs will be visited.

    Args:
      visitor: PipelineVisitor object whose callbacks will be called for each
        node visited. See PipelineVisitor comments.
      node: if specified it is expected to be a PValue and only the nodes of
        the DAG reachable from this node will be visited.

    Raises:
      TypeError: if node is specified and is not a PValue.
      pipeline.PipelineError: if node is specified and does not belong to this
        pipeline instance.
    """

    # Make sure the specified node has its transform registered as an output
    # producer. We can have this situation for PCollections created as results
    # of accessing a tag of a FlatMap().with_outputs() result.
    if node is not None:
      if not isinstance(node, pvalue.PValue):
        raise TypeError(
            'Expected a PValue for the node argument instead of: %r' % node)
      if node not in self._nodes:
        raise error.PipelineError('PValue not in pipeline: %r' % node)
      assert node.producer is not None

    visited = set()
    start_transform = self._root_transform() if node is None else node.producer
    start_transform.visit(visitor, self, visited)

  def apply(self, transform, pvalueish=None):
    """Applies a custom transform using the pvalueish specified.

    Args:
      transform: the PTranform (or callable) to apply.
      pvalueish: the input for the PTransform (typically a PCollection).

    Raises:
      TypeError: if the transform object extracted from the argument list is
        not a callable type or a descendant from PTransform.
      RuntimeError: if the transform object was already applied to this pipeline
        and needs to be cloned in order to apply again.
    """
    if not isinstance(transform, ptransform.PTransform):

      class CallableTransform(ptransform.PTransform):

        def __init__(self, callee):
          super(CallableTransform, self).__init__(
              label=getattr(callee, '__name__', 'Callable'))
          self._callee = callee

        def apply(self, *args, **kwargs):
          return self._callee(*args, **kwargs)

      assert callable(transform)
      transform = CallableTransform(transform)

    full_label = format_full_label(self._current_transform(), transform)
    if full_label in self.applied_labels:
      raise RuntimeError(
          'Transform with label %s already applied. Please clone the current '
          'instance using a new label or alternatively create a new instance. '
          'To clone a transform use: transform.clone(\'NEW LABEL\').'
          % full_label)
    self.applied_labels.add(full_label)

    pvalueish, inputs = transform._extract_input_pvalues(pvalueish)
    try:
      inputs = tuple(inputs)
      for leaf_input in inputs:
        if not isinstance(leaf_input, pvalue.PValue):
          raise TypeError
    except TypeError:
      raise NotImplementedError(
          'Unable to extract PValue inputs from %s; either %s does not accept '
          'inputs of this format, or it does not properly override '
          '_extract_input_values' % (pvalueish, transform))

    child = AppliedPTransform(
        self._current_transform(), transform, full_label, inputs)
    self._current_transform().add_part(child)
    self.transforms_stack.append(child)

    if self.options is not None and self.options.pipeline_type_check:
      transform.type_check_inputs(pvalueish)

    pvalueish_result = self.runner.apply(transform, pvalueish)

    if self.options is not None and self.options.pipeline_type_check:
      transform.type_check_outputs(pvalueish_result)

    for result in ptransform.GetPValues().visit(pvalueish_result):
      assert isinstance(result, (pvalue.PValue, pvalue.DoOutputsTuple))

      # Make sure we set the producer only for a leaf node in the transform DAG.
      # This way we preserve the last transform of a composite transform as
      # being the real producer of the result.
      if result.producer is None:
        result.producer = child
      self._current_transform().add_output(result)
      # TODO(robertwb): Multi-input, multi-output inference.
      if (self.options is not None and self.options.pipeline_type_check and
          isinstance(result, pvalue.PCollection) and not result.element_type):
        input_element_type = (
            inputs[0].element_type
            if len(inputs) == 1
            else typehints.Any)
        type_hints = transform.get_type_hints()
        declared_output_type = type_hints.simple_output_type(transform.label)
        if declared_output_type:
          input_types = type_hints.input_types
          if input_types and input_types[0]:
            declared_input_type = input_types[0][0]
            result.element_type = typehints.bind_type_variables(
                declared_output_type,
                typehints.match_type_variables(declared_input_type,
                                               input_element_type))
          else:
            result.element_type = declared_output_type
        else:
          result.element_type = transform.infer_output_type(input_element_type)

      assert isinstance(result.producer.inputs, tuple)

    if (self.options is not None
        and self.options.type_check_strictness == 'ALL_REQUIRED'
        and transform.get_type_hints().output_types is None):
      ptransform_name = '%s(%s)' % (transform.__class__.__name__, full_label)
      raise TypeCheckError('Pipeline type checking is enabled, however no '
                           'output type-hint was found for the '
                           'PTransform %s' % ptransform_name)

    self.transforms_stack.pop()
    return pvalueish_result


class PipelineVisitor(object):
  """Visitor pattern class used to traverse a DAG of transforms.

  This is an internal class used for bookkeeping by a Pipeline.
  """

  def visit_value(self, value, producer_node):
    """Callback for visiting a PValue in the pipeline DAG.

    Args:
      value: PValue visited (typically a PCollection instance).
      producer_node: AppliedPTransform object whose transform produced the
        pvalue.
    """
    pass

  def visit_transform(self, transform_node):
    """Callback for visiting a transform node in the pipeline DAG."""
    pass

  def enter_composite_transform(self, transform_node):
    """Callback for entering traversal of a composite transform node."""
    pass

  def leave_composite_transform(self, transform_node):
    """Callback for leaving traversal of a composite transform node."""
    pass


class AppliedPTransform(object):
  """A transform node representing an instance of applying a PTransform.

  This is an internal class used for bookkeeping by a Pipeline.
  """

  def __init__(self, parent, transform, full_label, inputs):
    self.parent = parent
    self.transform = transform
    # Note that we want the PipelineVisitor classes to use the full_label,
    # inputs, side_inputs, and outputs fields from this instance instead of the
    # ones of the PTransform instance associated with it. Doing this permits
    # reusing PTransform instances in different contexts (apply() calls) without
    # any interference. This is particularly useful for composite transforms.
    self.full_label = full_label
    self.inputs = inputs or ()
    self.side_inputs = () if transform is None else tuple(transform.side_inputs)
    self.outputs = []
    self.parts = []

  def add_output(self, output):
    assert (isinstance(output, pvalue.PValue) or
            isinstance(output, pvalue.DoOutputsTuple))
    self.outputs.append(output)

  def add_part(self, part):
    assert isinstance(part, AppliedPTransform)
    self.parts.append(part)

  def is_composite(self):
    """Returns whether this is a composite transform.

    A composite transform has parts (inner transforms) or isn't the
    producer for any of its outputs. (An exmaple of a transform that
    is not a producer is one that returns its inputs instead.)
    """
    return self.parts or all(pval.producer is not self for pval in self.outputs)

  def visit(self, visitor, pipeline, visited):
    """Visits all nodes reachable from the current node."""

    for pval in self.inputs:
      if pval not in visited and not isinstance(pval, pvalue.PBegin):
        assert pval.producer is not None
        pval.producer.visit(visitor, pipeline, visited)
        # The value should be visited now since we visit outputs too.
        assert pval in visited

    # Visit side inputs.
    for pval in self.side_inputs:
      if isinstance(pval, pvalue.AsSideInput) and pval.pvalue not in visited:
        pval = pval.pvalue  # Unpack marker-object-wrapped pvalue.
        assert pval.producer is not None
        pval.producer.visit(visitor, pipeline, visited)
        # The value should be visited now since we visit outputs too.
        assert pval in visited
        # TODO(silviuc): Is there a way to signal that we are visiting a side
        # value? The issue is that the same PValue can be reachable through
        # multiple paths and therefore it is not guaranteed that the value
        # will be visited as a side value.

    # Visit a composite or primitive transform.
    if self.is_composite():
      visitor.enter_composite_transform(self)
      for part in self.parts:
        part.visit(visitor, pipeline, visited)
      visitor.leave_composite_transform(self)
    else:
      visitor.visit_transform(self)

    # Visit the outputs (one or more). It is essential to mark as visited the
    # tagged PCollections of the DoOutputsTuple object. A tagged PCollection is
    # connected directly with its producer (a multi-output ParDo), but the
    # output of such a transform is the containing DoOutputsTuple, not the
    # PCollection inside it. Without the code below a tagged PCollection will
    # not be marked as visited while visiting its producer.
    for pval in self.outputs:
      if isinstance(pval, pvalue.DoOutputsTuple):
        pvals = (v for v in pval)
      else:
        pvals = (pval,)
      for v in pvals:
        if v not in visited:
          visited.add(v)
          visitor.visit_value(v, self)


class PipelineOptions(object):
  """Pipeline options class used as container for command line options.

  The class is essentially a wrapper over the standard argparse Python module
  (see https://docs.python.org/3/library/argparse.html).  To define one option
  or a group of options you subclass from PipelineOptions:

    class XyzOptions(PipelineOptions):

      @classmethod
      def _add_argparse_args(cls, parser):
        parser.add_argument('--abc', default='start')
        parser.add_argument('--xyz', default='end')

  The arguments for the add_argument() method are exactly the ones
  described in the argparse ublic documentation.

  Pipeline objects require an options object during initialization.
  This is obtained simply by initializing an options class as defined above:

    p = Pipeline(options=XyzOptions())
    if p.options.xyz == 'end':
      raise ValueError('Option xyz has an invalid value.')

  By default the options classes will use command line arguments to initialize
  the options.
  """

  def __init__(self, flags=None, **kwargs):
    """Initialize an options class.

    The initializer will traverse all subclasses, add all their argparse
    arguments and then parse the command line specified by flags or by default
    the one obtained from sys.argv.

    The subclasses are not expected to require a redefinition of __init__.

    Args:
      flags: An iterable of command line arguments to be used. If not specified
        then sys.argv will be used as input for parsing arguments.

      **kwargs: Add overrides for arguments passed in flags.
    """
    self._flags = flags
    self._all_options = kwargs
    parser = argparse.ArgumentParser()
    for cls in type(self).mro():
      if cls == PipelineOptions:
        break
      elif '_add_argparse_args' in cls.__dict__:
        cls._add_argparse_args(parser)
    # The _visible_options attribute will contain only those options from the
    # flags (i.e., command line) that can be recognized. The _all_options
    # field contains additional overrides.
    self._visible_options, _ = parser.parse_known_args(flags or [])

  @classmethod
  def _add_argparse_args(cls, parser):
    # Override this in subclasses to provide options.
    pass

  def view_as(self, cls):
    view = cls(self._flags)
    view._all_options = self._all_options
    return view

  def _visible_option_list(self):
    return sorted(option
                  for option in dir(self._visible_options) if option[0] != '_')

  def __dir__(self):
    return sorted(dir(type(self)) + self.__dict__.keys() +
                  self._visible_option_list())

  def __getattr__(self, name):
    if name in self._visible_option_list():
      return self._all_options.get(name, getattr(self._visible_options, name))
    else:
      raise AttributeError("'%s' object has no attribute '%s'" %
                           (type(self).__name__, name))

  def __setattr__(self, name, value):
    if name in ('_flags', '_all_options', '_visible_options'):
      super(PipelineOptions, self).__setattr__(name, value)
    elif name in self._visible_option_list():
      self._all_options[name] = value
    else:
      raise AttributeError("'%s' object has no attribute '%s'" %
                           (type(self).__name__, name))

  def __str__(self):
    return '%s(%s)' % (type(self).__name__,
                       ', '.join('%s=%s' % (option, getattr(self, option))
                                 for option in self._visible_option_list()))
