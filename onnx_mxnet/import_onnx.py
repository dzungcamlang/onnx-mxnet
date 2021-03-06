# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#     http://www.apache.org/licenses/LICENSE-2.0
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

# Derived from Apache 2.0 licensed onnx.py file from DMLC NNVM:
# https://github.com/dmlc/nnvm/blob/3da53e46db57c438b05fbebe8aa332ee8c5994d1/python/nnvm/frontend/onnx.py

# coding: utf-8

""" Support import export formats."""
from __future__ import absolute_import as _abs
import mxnet as mx
from .import_helper import _identity_list, _convert_map

def _convert_operator(op_name, attrs, identity_list=None, convert_map=None):
    """Convert from onnx operator to mxnet operator.
    The converter must specify conversions explicitly for incompatible name, and
    apply handlers to operator attributes.

    Parameters
    ----------
    op_name : str
        Operator name, such as Convolution, FullyConnected
    attrs : dict
        Dict of operator attributes
    identity_list : list
        List of operators that don't require conversion
    convert_map : dict
        Dict of name : callable, where name is the op's name that
        require conversion to mxnet, callable are functions which
        take attrs and return (new_op_name, new_attrs)

    Returns
    -------
    (op_name, attrs)
        Converted (op_name, attrs) for mxnet.
    """
    identity_list = identity_list if identity_list else _identity_list
    convert_map = convert_map if convert_map else _convert_map
    if op_name in identity_list:
        pass
    elif op_name in convert_map:
        op_name, attrs = convert_map[op_name](attrs)
    else:
        raise NotImplementedError("Operator {} not implemented.".format(op_name))
    op = getattr(mx.sym, op_name, None)
    if not op:
        raise RuntimeError("Unable to map op_name {} to sym".format(op_name))
    return op, attrs

class GraphProto(object):
    """A helper class for handling mxnet symbol copying from pb2.GraphProto.
    Definition: https://github.com/onnx/onnx/blob/master/onnx/onnx.proto
    """
    def __init__(self):
        self._nodes = {}
        self._params = {}
        self._renames = {}
        self._num_input = 0
        self._num_param = 0

    def from_onnx(self, graph):
        """Construct symbol from onnx graph.
        The inputs from onnx graph is vague, only providing "1", "2"...
        For convenience, we rename the `real` input names to "input_0",
        "input_1"... And renaming parameters to "param_0", "param_1"...

        Parameters
        ----------
        graph : onnx protobuf object
            The loaded onnx graph

        Returns
        -------
        sym :mx.sym
            The returned mxnet symbol
        params : dict
            A dict of name: mx.nd.array pairs, used as pretrained weights
        """
        # parse network inputs, aka parameters
        for init_tensor in graph.initializer:
            if not init_tensor.name.strip():
                raise ValueError("Tensor's name is required.")
            self._params[init_tensor.name] = self._parse_array(init_tensor)
        # converting GraphProto message
        for i in graph.input:
            if i.name in self._params:
                # i is a param instead of input
                name_param = 'param_{}'.format(self._num_param)
                self._num_param += 1
                self._params[name_param] = self._params.pop(i.name)
                self._nodes[name_param] = mx.sym.Variable(name=name_param, shape=self._params[name_param].shape)
                self._renames[i.name] = name_param
            else:
                name_input = 'input_{}'.format(self._num_input)
                self._num_input += 1
                self._nodes[name_input] = mx.sym.Variable(name=name_input)
                self._renames[i.name] = name_input
        # construct nodes, nodes are stored as directed acyclic graph
        # converting NodeProto message
        for idx, node in enumerate(graph.node):
            op_name = node.op_type
            node_name = node.name.strip()
            node_name = node_name if node_name else None
            attr = self._parse_attr(node.attribute)
            new_op, new_attr = _convert_operator(op_name, attr)
            inputs = [self._nodes[self._renames.get(i, i)] for i in node.input]
            # some workarounds for onnx problem
            new_attr = self._fix_bias(new_op, new_attr, len(inputs))
            new_attr = self._fix_channels(new_op, new_attr, list(node.input))
            self._fix_bias_shape(node.op_type, graph.node[idx - 1].op_type, node.input)
            # calling again to get new symbols after some workarounds
            inputs = [self._nodes[self._renames.get(i, i)] for i in node.input]
            if op_name == 'Gemm':
                new_op, inputs, new_attr = self._fix_gemm('FullyConnected', inputs, attr)
            op = new_op(name=node_name, *inputs, **new_attr)
            node_output = self._fix_outputs(op_name, node.output)
            assert len(node_output) == len(op.list_outputs()), (
                "Number of output mismatch {} vs {} in {}.".format(
                    len(node_output), len(op.list_outputs()), op_name))
            for k, i in zip(list(node_output), range(len(node_output))):
                self._nodes[k] = op[i]
        # now return the outputs
        out = [self._nodes[i.name] for i in graph.output]
        if len(out) > 1:
            out = mx.sym.Group(out)
        else:
            out = out[0]
        return out, self._params

    def _fix_gemm(self, op_name, inputs, old_attr):
        """Using FullyConnected operator in place of linalg_gemm to perform same operation"""
        op = getattr(mx.sym, op_name, None)
        alpha = float(old_attr.get('alpha', 1.0))
        beta = float(old_attr.get('beta', 1.0))
        transA = int(old_attr.get('transA', 0))
        transB = int(old_attr.get('transB', 0))
        if transA:
            inputs[0] = mx.sym.transpose(inputs[0], axes=(1, 0))
        if not transB:
            inputs[1] = mx.sym.transpose(inputs[1], axes=(1, 0))
        new_inputs = [alpha*inputs[0], inputs[1], beta*inputs[2]]
        new_attr = {'num_hidden' : self._params[inputs[2].name].shape[0]}
        return op, new_inputs, new_attr

    def _parse_array(self, tensor_proto):
        """Grab data in TensorProto and convert to numpy array."""
        try:
            from onnx.numpy_helper import to_array
        except ImportError as e:
            raise ImportError("Unable to import onnx which is required {}".format(e))
        np_array = to_array(tensor_proto).reshape(tuple(tensor_proto.dims))
        return mx.nd.array(np_array)

    def _parse_attr(self, attr_proto):
        """Convert a list of AttributeProto to a dict, with names as keys."""
        attrs = {}
        for a in attr_proto:
            for f in ['f', 'i', 's']:
                if a.HasField(f):
                    attrs[a.name] = getattr(a, f)
            for f in ['floats', 'ints', 'strings']:
                if list(getattr(a, f)):
                    assert a.name not in attrs, "Only one type of attr is allowed"
                    attrs[a.name] = tuple(getattr(a, f))
            for f in ['t', 'g']:
                if a.HasField(f):
                    raise NotImplementedError("Filed {} is not supported in mxnet.".format(f))
            for f in ['tensors', 'graphs']:
                if list(getattr(a, f)):
                    raise NotImplementedError("Filed {} is not supported in mxnet.".format(f))
            if a.name not in attrs:
                raise ValueError("Cannot parse attribute: \n{}\n.".format(a))
        return attrs

    def _fix_outputs(self, op, outputs):
        """A workaround to handle dropout or similar operator that have more than one out
        in ONNX.
        """
        if op == 'Dropout':
            assert len(outputs) == 2, "ONNX have two outputs for dropout layer."
            outputs = outputs[:-1]
        return outputs

    def _fix_bias(self, op, attrs, num_inputs):
        """A workaround for 'use_bias' attribute since onnx don't provide this attribute,
        we have to check the number of inputs to decide it."""
        if op not in [mx.sym.Convolution, mx.sym.Deconvolution, mx.sym.FullyConnected]:
            return attrs
        if num_inputs == 3:
            attrs['no_bias'] = False
        elif num_inputs == 2:
            attrs['no_bias'] = True
        else:
            raise ValueError("Unexpected number of inputs for: {}".format(op))
        return attrs


    def _fix_bias_shape(self, op_name, last_op_name, inputs):
        """A workaround to reshape bias term to (1, num_channel)."""
        if op_name == 'Add' and last_op_name == 'Conv':
            assert len(list(inputs)) == 2
            bias_name = self._renames.get(inputs[1], inputs[1])
            bias = self._params[bias_name]
            assert len(bias.shape) == 1
            # reshape to (1, n)
            bias = mx.nd.array(bias.asnumpy().reshape((1, -1, 1, 1)))
            # broadcast_add expects shape with sym.variable
            self._nodes[bias_name] = mx.sym.Variable(name=bias_name, shape=bias.shape)
            self._params[bias_name] = bias


    def _fix_channels(self, op, attrs, inputs):
        """A workaround for getting 'channels' or 'units' since onnx don't provide
        these attributes. We check the shape of weights provided to get the number.
        """
        if op not in [mx.sym.Convolution, mx.sym.Deconvolution, mx.sym.FullyConnected]:
            return attrs
        weight_name = self._renames[inputs[1]]
        if not weight_name in self._params:
            raise ValueError("Unable to get channels/units attr from onnx graph.")
        else:
            wshape = self._params[weight_name].shape
            assert len(wshape) >= 2, "Weights shape is invalid: {}".format(wshape)
            channels = wshape[0]
            if op in [mx.sym.FullyConnected]:
                attrs['num_hidden'] = channels
            else:
                attrs['num_filter'] = channels
        return attrs
