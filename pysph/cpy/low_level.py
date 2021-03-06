"""Low level utility code. The intention is for users to use these but with the
knowledge that these are not general cross-backend tools but rather specific
tools.

"""

import re
import inspect

import numpy as np

from .config import get_config
from .array import Array, get_backend
from .transpiler import Transpiler
from .types import KnownType, ctype_to_dtype


LID_0 = LDIM_0 = GDIM_0 = GID_0 = 0


def local_barrier():
    """Dummy method to keep Python happy.

    This is a valid function in OpenCL but has no meaning in Python for now.
    """
    pass


class LocalMem(object):
    '''A local memory specification for a GPU kernel.

    An example illustrates this best::

       >>> l = LocalMem(2)
       >>> m = l.get('double', 128)
       >>> m.size
       2048

    Note that this is basically ``sizeof(double) * 128 * 2``
    '''
    def __init__(self, size, backend=None):
        '''
        Constructor

        Parameters
        ----------

        size: int: a multiple of the current work group size.
        baackend: str: one of 'opencl', 'cuda'
        '''
        self.backend = get_backend(backend)
        if backend == 'cython':
            raise NotImplementedError(
                'LocalMem is only meaningful for the opencl/cuda backends.'
            )
        self.size = size
        self._cache = {}

    def get(self, c_type, workgroup_size):
        """Return the local memory required given the type and work group size.
        """
        key = (c_type, workgroup_size)
        if key in self._cache:
            return self._cache[key]
        elif self.backend == 'opencl':
            import pyopencl as cl
            dtype = ctype_to_dtype(c_type)
            sz = dtype().itemsize
            mem = cl.LocalMemory(sz * self.size * workgroup_size)
            self._cache[key] = mem
            return mem
        else:
            raise NotImplementedError(
                'Backend %s not implemented' % self.backend
            )


def splay(queue, n, kernel_specific_max_wg_size=None):
    dev = queue.device
    max_work_items = min(128, dev.max_work_group_size)

    if kernel_specific_max_wg_size is not None:
        max_work_items = min(max_work_items, kernel_specific_max_wg_size)

    min_work_items = min(64, max_work_items)
    full_groups = dev.max_compute_units * 4 * 8
    # 4 to overfill the device
    # 8 is an Nvidia constant--that's how many
    # groups fit onto one compute device

    if n < min_work_items:
        group_count = 1
        work_items_per_group = min_work_items
    elif n < (full_groups * min_work_items):
        group_count = (n + min_work_items - 1) // min_work_items
        work_items_per_group = min_work_items
    elif n < (full_groups * max_work_items):
        group_count = full_groups
        grp = (n + min_work_items - 1) // min_work_items
        work_items_per_group = (
            (grp + full_groups - 1) // full_groups) * min_work_items
    else:
        group_count = (n + max_work_items - 1) // max_work_items
        work_items_per_group = max_work_items

    return (group_count*work_items_per_group,), (work_items_per_group,)


class Kernel(object):
    """A simple abstraction to create GPU kernels with pure Python.

    This will not work currently with the Cython backend.

    The idea is that one can create a Python function with suitable type
    annotations along with standard names from the CLUDA header (`LDIM_0,
    LID_0, GID_0, local_barrier()`, )etc.) to write kernels in pure Python.

    Note
    ----

    This works best with functions with annotations via the @annotate decorator
    or with function annotation as we need the type information for some simple
    type checking of the passed constants.

    """
    def __init__(self, func, backend='opencl'):
        backend = get_backend(backend)
        if backend == 'cython':
            raise NotImplementedError(
                'Kernels only work with opencl/cuda backends.'
            )
        self.tp = Transpiler(backend=backend)
        self.backend = backend
        self.name = func.__name__
        self.func = func
        self.source = ''  # The generated source.
        self._config = get_config()
        self._use_double = self._config.use_double
        from .opencl import get_queue
        self.queue = get_queue()
        self._func_info = self._get_func_info()
        self._generate()

    def _to_float(self, s):
        return re.sub(r'\bdouble\b', 'float', s)

    def _get_func_info(self):
        getfullargspec = getattr(inspect, 'getfullargspec', inspect.getargspec)
        argspec = getfullargspec(self.func)
        annotations = getattr(
            argspec, 'annotations', self.func.__annotations__
        )

        arg_info = []
        for arg in argspec.args:
            kt = annotations[arg]
            if not self._use_double:
                kt = KnownType(
                    self._to_float(kt.type), self._to_float(kt.base_type)
                )
            arg_info.append((arg, kt))
        func_info = {
            'args': arg_info,
            'return': annotations.get('return', KnownType('void'))
        }
        return func_info

    def _generate(self):
        self.tp.add(self.func)
        self._correct_opencl_address_space()

        self.tp.compile()
        self.source = self.tp.source
        self.knl = getattr(self.tp.mod, self.name)
        import pyopencl as cl
        self._max_work_group_size = self.knl.get_work_group_info(
            cl.kernel_work_group_info.WORK_GROUP_SIZE,
            self.queue.device
        )

    def _correct_opencl_address_space(self):
        code = self.tp.blocks[-1].code.splitlines()
        code[0] = 'KERNEL ' + code[0]
        self.tp.blocks[-1].code = '\n'.join(code)

    def _massage_arg(self, x, type_info, workgroup_size):
        if isinstance(x, Array):
            return x.dev.data
        elif isinstance(x, LocalMem):
            return x.get(type_info.base_type, workgroup_size)
        else:
            dtype = ctype_to_dtype(type_info.type)
            return np.array([x], dtype=dtype)

    def _get_args(self, args, workgroup_size):
        arg_info = self._func_info['args']
        c_args = []
        for arg, a_info in zip(args, arg_info):
            c_args.append(self._massage_arg(arg, a_info[1], workgroup_size))
        return c_args

    def _get_workgroup_size(self, global_size):
        gs, ls = splay(self.queue, global_size, self._max_work_group_size)
        return gs, ls

    def __call__(self, *args, **kw):
        size = args[0].data.shape
        gs = kw.pop('global_size', size)
        n = np.prod(gs)
        ls = kw.pop('local_size', None)
        if ls is not None:
            local_size = np.prod(ls)
            global_size = ((n + local_size - 1) // local_size) * local_size
            gs = (global_size, )
        else:
            gs, ls = self._get_workgroup_size(n)
        c_args = self._get_args(args, ls[0])
        prepend = [self.queue, gs, ls]
        c_args = prepend + c_args
        if self.backend == 'opencl':
            self.knl(*c_args)
            self.queue.finish()
