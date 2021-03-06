#!/usr/bin/env python
# -*- coding: utf-8 -*-
# pylint: disable=logging-format-interpolation
"""Tape for recording gradient calculation."""
from __future__ import absolute_import
from __future__ import print_function
from __future__ import with_statement
import contextlib
import collections

import numpy
import mxnet

from . import array
from . import array_variants
from .utils import log

# pylint: disable=invalid-name
_logger = log.get_logger(__name__)
# pylint: enable=invalid-name

GradRecord = collections.namedtuple(
    'GradRecord', ['grad_func', 'result', 'owner'])


class Tape(object):
    """Records gradient calculation."""
    global_tape = None
    timestamp = 0

    def __init__(self):
        # Stores grad value result from target back to [KEY]. Array -> grad result (Array)
        self._grads = {}
        # Store derivation graph of gradients. Array -> list of grad records (or record tuples)
        self._array_grad_records = {}
        self.__class__.timestamp += 1

    def add_partial_derivative(self, grad_func, owner, result):
        """Add partial derivative.

        Parameters
        ----------
        grad_func
            Function for calculating gradient.
        owner
            Owners of the gradient. Usually it is just one array. But a list is also allowed.
        result
            Result of previous forward execution.
        """

        def add_owner(single_owner):
            """Add owner.

            Deal with situation when there are multiple owners.
            """
            if isinstance(single_owner, array.Value):
                if single_owner not in self._array_grad_records:
                    self._array_grad_records[single_owner] = []
                self._array_grad_records[single_owner].append(
                    GradRecord(
                        grad_func=grad_func,
                        result=result,
                        owner=owner))
            elif single_owner is not None:
                # None means a placeholder for an array that needs no gradient.
                for sub_owner in single_owner:
                    add_owner(sub_owner)

        add_owner(owner)

    def set_gradient_target(self, target):
        """Set gradient targets to ones."""

        def set_single_gradient_target(target):
            """Set gradient target for one."""
            if isinstance(target, array.Value):
                self._grads[target] = array.Value.wrap(1.0 if isinstance(
                    target, array.Number) else numpy.ones(target.shape))
            else:
                for sub_target in target:
                    set_single_gradient_target(sub_target)

        set_single_gradient_target(target)

    def _is_gradable(self, current_array):
        """Check if the gradient can now be calculated relative to the specified array.

        It means that all resulting arrays taken the specified array as input
        have gradients calculated.
        """

        def check_grad_record_empty(arr):
            """Check if gradient record is empty for the array. This means it haven't
            been calculated.
            """
            if isinstance(arr, array.Value):
                if len(self._array_grad_records.get(arr, [])) != 0:
                    return False
            else:
                for sub_arr in arr:
                    if not check_grad_record_empty(sub_arr):
                        return False
            return True

        for res in self._array_grad_records.get(current_array, []):
            if not check_grad_record_empty(res.result):
                return False
        return True

    def _get_cached_gradient(self, arr):
        """Get cached gradient. Initialize if not exist."""
        if isinstance(arr, (tuple, list)):
            return (
                self._get_cached_gradient(sub_result)
                for sub_result in arr
            )
        if arr not in self._grads:
            if isinstance(arr, array.Number):
                self._grads[arr] = array.Value.wrap(0.0)
            elif arr.has_type(array_variants.ArrayType.MXNET):
                with arr.context.as_mxnet_context():
                    self._grads[arr] = array.Value.wrap(
                        mxnet.nd.zeros(arr.shape))
            else:
                # TODO(minjie): the initialization may incur memcopy.
                self._grads[arr] = array.Value.wrap(numpy.zeros(arr.shape))
        return self._grads[arr]

    def _cumulate_gradient(self, arr, grad):
        def add_gradient(arr, grad):
            """Add gradient.

            Recurse when handle multiple arrays.
            """
            if isinstance(arr, array.Value):
                current_gradient = self._get_cached_gradient(arr)
                current_gradient += array.Value.wrap(grad)
                self._grads[arr] = current_gradient
            elif arr is not None:
                if len(arr) != len(grad):
                    _logger.fatal('Number of gradients does not match.')
                for sub_arr, sub_grad in zip(arr, grad):
                    add_gradient(sub_arr, sub_grad)

        add_gradient(arr, grad)

    def get_gradient(self, origin):
        """Get gradient of the specified array.

        Target gradients must be set prior to this function call.
        """
        dfs_stack = [origin]
        while len(dfs_stack) != 0:
            current_array = dfs_stack[-1]
            assert isinstance(current_array,
                              array.Value), 'Type is not `array.Value`.'
            if self._is_gradable(current_array):
                dfs_stack.pop()
                # Initialize if necessary.
                self._get_cached_gradient(current_array)
                grad_records = self._array_grad_records.get(current_array, [])

                for grad_record in grad_records:
                     # TODO: add primitive_type info in debug info later.
                    _logger.debug(
                        'Calling derivative func "{}"'.format(grad_record.grad_func))
                    grad = grad_record.grad_func(self._get_cached_gradient(grad_record.result))
                    self._cumulate_gradient(grad_record.owner, grad)

                def remove_grad_record(owner, grad_record):
                    """Remove gradient record from owner."""
                    if isinstance(owner, array.Value):
                        self._array_grad_records[owner].remove(grad_record)
                    elif owner is not None:
                        for sub_owner in owner:
                            remove_grad_record(sub_owner, grad_record)

                for grad_record in grad_records[:]:
                    remove_grad_record(grad_record.owner, grad_record)
            else:

                def push_grad_record(arr):
                    """Push gradient record for a result array."""
                    if isinstance(arr, array.Value):
                        dfs_stack.append(arr)
                    else:
                        for sub_arr in arr:
                            push_grad_record(sub_arr)

                for rec in self._array_grad_records[current_array]:
                    push_grad_record(rec.result)
        return self._get_cached_gradient(origin)


@contextlib.contextmanager
def tape():
    """Convenience context wrapper for creating temporary `Tape`."""
    Tape.global_tape = Tape()
    yield Tape.global_tape
    Tape.global_tape = None


def global_tape():
    """Returns current global `Tape`."""
    return Tape.global_tape
