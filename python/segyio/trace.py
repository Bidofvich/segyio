import collections
import contextlib
import itertools
import sys
try: from future_builtins import zip
except ImportError: pass

import numpy as np

from .line import HeaderLine
from .field import Field
from .utils import castarray

class Sequence(collections.Sequence):

    # unify the common optimisations and boilerplate of Trace, RawTrace, and
    # Header, which all obey the same index-oriented interface, and all share
    # length and wrap-around properties.
    #
    # It provides a useful negative-wrap index method which deals
    # appropriately with IndexError and python2-3 differences.

    def __init__(self, length):
        self.length = length

    def __len__(self):
        """x.__len__() <==> len(x)"""
        return self.length

    def __iter__(self):
        """x.__iter__() <==> iter(x)"""
        # __iter__ has a reasonable default implementation from
        # collections.Sequence. It's essentially this loop:
        # for i in range(len(self)): yield self[i]
        # However, in segyio that means the double-buffering, buffer reuse does
        # not happen, which is *much* slower (the allocation of otherwised
        # reused numpy objects takes about half the execution time), so
        # explicitly implement it as [:]
        return self[:]

    def wrapindex(self, i):
        if i < 0:
            i += len(self)

        if not 0 <= i < len(self):
            # in python2, int-slice comparison does not raise a type error,
            # (but returns False), so force a type-error if this still isn't an
            # int-like.
            _ = i + 0
            raise IndexError('trace index out of range')

        return i

class Trace(Sequence):
    """
    The Trace implements the array interface, where every array element, the
    data trace, is a numpy.ndarray. As all arrays, it can be random accessed,
    iterated over, and read strided. Data is read lazily from disk, so
    iteration does not consume much memory. If you want eager reading, use
    Trace.raw.

    This mode gives access to reading and writing functionality for traces.
    The primary data type is ``numpy.ndarray``. Traces can be accessed
    individually or with python slices, and writing is done via assignment.

    Notes
    -----
    .. versionadded:: 1.1

    .. versionchanged:: 1.6
        common list operations (collections.Sequence)

    Examples
    --------
    Read all traces in file f and store in a list:

    >>> l = [numpy.copy(tr) for tr in trace[:]]

    Do numpy operations on a trace:

    >>> tr = trace[10]
    >>> tr = tr * 2
    >>> tr = tr - 100
    >>> avg = numpy.average(tr)

    Double every trace value and write to disk. Since accessing a trace
    gives a numpy value, to write to the respective trace we need its index:

    >>> for i, tr in enumerate(trace):
    ...     tr = tr * 2
    ...     trace[i] = tr

    """

    def __init__(self, filehandle, dtype, tracecount, samples, readonly):
        super(Trace, self).__init__(tracecount)
        self.filehandle = filehandle
        self.dtype = dtype
        self.shape = samples
        self.readonly = readonly

    def __getitem__(self, i):
        """trace[i]

        ith trace of the file, starting at 0. trace[i] returns a numpy array,
        and changes to this array will *not* be reflected on disk.

        When i is a slice, a generator of numpy arrays is returned.

        Parameters
        ----------
        i : int or slice

        Returns
        -------
        trace : numpy.ndarray of dtype or generator of numpy.ndarray of dtype

        Notes
        -----
        .. versionadded:: 1.1

        Behaves like [] for lists.

        .. note::

            This operator reads lazily from the file, meaning the file is read
            on ``next()``, and only one trace is fixed in memory. This means
            segyio can run through arbitrarily large files without consuming
            much memory, but it is potentially slow if the goal is to read the
            entire file into memory. If that is the case, consider using
            `trace.raw`, which reads eagerly.

        Examples
        --------
        Read every other trace:

        >>> for tr in trace[::2]:
        ...     print(tr)

        Read all traces, last-to-first:

        >>> for tr in trace[::-1]:
        ...     tr.mean()

        Read a single value. The second [] is regular numpy array indexing, and
        supports all numpy operations, including negative indexing and slicing:

        >>> trace[0][0]
        1490.2
        >>> trace[0][1]
        1490.8
        >>> trace[0][-1]
        1871.3
        >>> trace[-1][100]
        1562.0
        """

        try:
            i = self.wrapindex(i)
            buf = np.zeros(self.shape, dtype = self.dtype)
            return self.filehandle.gettr(buf, i, 1, 1)

        except TypeError:
            def gen():
                # double-buffer the trace. when iterating over a range, we want
                # to make sure the visible change happens as late as possible,
                # and that in the case of exception the last valid trace was
                # untouched. this allows for some fancy control flow, and more
                # importantly helps debugging because you can fully inspect and
                # interact with the last good value.
                x = np.zeros(self.shape, dtype=self.dtype)
                y = np.zeros(self.shape, dtype=self.dtype)

                for j in range(*i.indices(len(self))):
                    self.filehandle.gettr(x, j, 1, 1)
                    x, y = y, x
                    yield y

            return gen()

    def __setitem__(self, i, val):
        """trace[i] = val

        Write the ith trace of the file, starting at 0. It accepts any
        array_like, but val must be at least as big as the underlying data
        trace.

        If val is longer than the underlying trace, it is essentially
        truncated.

        For the best performance, val should be a numpy.ndarray of sufficient
        size and same dtype as the file. segyio will warn on mismatched types,
        and attempt a conversion for you.

        Data is written immediately to disk. If writing multiple traces at
        once, and a write fails partway through, the resulting file is left in
        an unspecified state.

        Parameters
        ----------
        i   : int or slice
        val : array_like

        Notes
        -----
        .. versionadded:: 1.1

        Behaves like [] for lists.

        Examples
        --------
        Write a single trace:

        >>> trace[10] = list(range(1000))

        Write multiple traces:

        >>> trace[10:15] = np.array([cube[i] for i in range(5)])

        Write multiple traces with stride:

        >>> trace[10:20:2] = np.array([cube[i] for i in range(5)])

        """
        if isinstance(i, slice):
            for j, x in zip(range(*i.indices(len(self))), val):
                self[j] = x

            return

        xs = castarray(val, self.dtype)

        # TODO:  check if len(xs) > shape, and optionally warn on truncating
        # writes
        self.filehandle.puttr(self.wrapindex(i), xs)

    def __repr__(self):
        return "Trace(traces = {}, samples = {})".format(len(self), self.shape)

    @property
    def raw(self):
        """
        An eager version of Trace

        Returns
        -------
        raw : RawTrace
        """
        return RawTrace(self.filehandle,
                        self.dtype,
                        len(self),
                        self.shape,
                        self.readonly,
                       )

    @property
    @contextlib.contextmanager
    def ref(self):
        """
        A write-back version of Trace

        Returns
        -------
        ref : RefTrace
            `ref` is returned in a context manager, and must be in a ``with``
            statement

        Notes
        -----
        .. versionadded:: 1.6

        Examples
        --------
        >>> with trace.ref as ref:
        ...     ref[10] += 1.617
        """

        x = RefTrace(self.filehandle,
                     self.dtype,
                     len(self),
                     self.shape,
                     self.readonly,
                    )
        yield x
        x.flush()

class RawTrace(Trace):
    """
    Behaves exactly like trace, except reads are done eagerly and returned as
    numpy.ndarray, instead of generators of numpy.ndarray.
    """
    def __init__(self, *args):
        super(RawTrace, self).__init__(*args)

    def __getitem__(self, i):
        """trace[i]

        Eagerly read the ith trace of the file, starting at 0. trace[i] returns
        a numpy array, and changes to this array will *not* be reflected on
        disk.

        When i is a slice, this returns a 2-dimensional numpy.ndarray .

        Parameters
        ----------
        i : int or slice

        Returns
        -------
        trace : numpy.ndarray of dtype

        Notes
        -----
        .. versionadded:: 1.1

        Behaves like [] for lists.

        .. note::

            Reading this way is more efficient if you know you can afford the
            extra memory usage. It reads the requested traces immediately to
            memory.

        """
        try:
            i = self.wrapindex(i)
            buf = np.zeros(self.shape, dtype = self.dtype)
            return self.filehandle.gettr(buf, i, 1, 1)
        except TypeError:
            indices = i.indices(len(self))
            start, _, step = indices
            length = len(range(*indices))
            buf = np.empty((length, self.shape), dtype = self.dtype)
            return self.filehandle.gettr(buf, start, step, length)


def fingerprint(x):
    return hash(bytes(x.data))

class RefTrace(Trace):
    """
    Behaves like trace, except changes to the returned numpy arrays *are*
    reflected on disk. Operations have to be in-place on the numpy array, so
    assignment on a trace will not work.

    This feature exists to support code like::

        >>> with ref as r:
        ...     for x, y in zip(r, src):
        ...         numpy.copyto(x, y + 10)

    This class is not meant to be instantiated directly, but returned by
    :attr:`Trace.ref`. This feature requires a context manager, to guarantee
    modifications are written back to disk.
    """
    def __init__(self, *args):
        super(RefTrace, self).__init__(*args)
        self.refs = {}

    def flush(self):
        """
        Commit cached writes to the file handle. Does not flush libc buffers or
        notifies the kernel, so these changes may not immediately be visible to
        other processes.

        Updates the fingerprints whena writes happen, so successive ``flush()``
        invocations are no-ops.

        It is not necessary to call this method in user code.

        Notes
        -----
        .. versionadded:: 1.6

        This method is not intended as user-oriented functionality, but might
        be useful in certain contexts to provide stronger guarantees.
        """
        garbage = []
        for i, (x, signature) in self.refs.items():
            if sys.getrefcount(x) == 3:
                garbage.append(i)

            if fingerprint(x) == signature: continue

            self.filehandle.puttr(i, x)
            signature = fingerprint(x)


        # to avoid too many resource leaks, when this dict is the only one
        # holding references to already-produced traces, clear them
        for i in garbage:
            del self.refs[i]

    def fetch(self, i, buf = None):
        if buf is None:
            buf = np.zeros(self.shape, dtype = self.dtype)

        try:
            self.filehandle.gettr(buf, i, 1, 1)
        except IOError:
            if not self.readonly:
                # if the file is opened read-only and this happens, there's no
                # way to actually write and the error is an actual error
                buf.fill(0)
            else: raise

        return buf

    def __getitem__(self, i):
        """trace[i]

        Read the ith trace of the file, starting at 0. trace[i] returns a numpy
        array, but unlike Trace, changes to this array *will* be reflected on
        disk. The modifications must happen to the actual array (views are ok),
        so in-place operations work, but assignments will not::

            >>> with ref as ref:
            ...     x = ref[10]
            ...     x += 1.617 # in-place, works
            ...     numpy.copyto(x, x + 10) # works
            ...     x = x + 10 # re-assignment, won't change the original x

        Works on newly created files that has yet to have any traces written,
        which opens up a natural way of filling newly created files with data.
        When getting unwritten traces, a trace filled with zeros is returned.

        Parameters
        ----------
        i : int or slice

        Returns
        -------
        trace : numpy.ndarray of dtype

        Notes
        -----
        .. versionadded:: 1.6

        Behaves like [] for lists.

        Examples
        --------
        Merge two files with a binary operation. Relies on python3 iterator
        zip:

        >>> with ref as ref:
        ...     for x, lhs, rhs in zip(ref, L, R):
        ...         numpy.copyto(x, lhs + rhs)

        Create a file and fill with data (the repeated trace index):

        >>> f = create()
        >>> with f.trace.ref as ref:
        ...     for i, x in enumerate(ref):
        ...         x.fill(i)
        """
        try:
            i = self.wrapindex(i)

            # we know this class is only used in context managers, so we know
            # refs don't escape (with expectation of being written), so
            # preserve all refs yielded with getitem(int)
            #
            # using ref[int] is problematic and pointless, we need to handle
            # this scenario gracefully:
            # with f.trace.ref as ref:
            #     x = ref[10]
            #     x[5] = 0
            #     # invalidate other refs
            #     y = ref[11]
            #     y[6] = 1.6721
            #
            #     # if we don't preserve returned individual getitems, this
            #     # write is lost
            #     x[5] = 52
            #
            # for slices, we know that references terminate with every
            # iteration anyway, multiple live references cannot happen

            if i in self.refs:
                return self.refs[i][0]

            x = self.fetch(i)
            self.refs[i] = (x, fingerprint(x))
            return x

        except TypeError:
            def gen():
                x = np.zeros(self.shape, dtype = self.dtype)

                try:
                    for j in range(*i.indices(len(self))):
                        x = self.fetch(j, x)
                        y = fingerprint(x)

                        yield x

                        if not fingerprint(x) == y:
                            self.filehandle.puttr(j, x)

                finally:
                    # the last yielded item is available after the loop, so
                    # preserve it and check if it's been updated on exit
                    self.refs[j] = (x, y)

            return gen()

class Header(Sequence):
    """Interact with segy in header mode

    This mode gives access to reading and writing functionality of headers,
    both in individual (trace) mode and line mode. The returned header
    implements a dict_like object with a fixed set of keys, given by the SEG-Y
    standard.

    The Header implements the array interface, where every array element, the
    data trace, is a numpy.ndarray. As all arrays, it can be random accessed,
    iterated over, and read strided. Data is read lazily from disk, so
    iteration does not consume much memory.

    Notes
    -----
    .. versionadded:: 1.1

    .. versionchanged:: 1.6
        common list operations (collections.Sequence)

    """
    def __init__(self, segy):
        self.segy = segy
        super(Header, self).__init__(segy.tracecount)

    def __getitem__(self, i):
        """header[i]

        ith header of the file, starting at 0.

        Parameters
        ----------
        i : int or slice

        Returns
        -------
        field : Field
            dict_like header

        Notes
        -----
        .. versionadded:: 1.1

        Behaves like [] for lists.

        Examples
        --------
        Reading a header:

        >>> header[10]

        Read a field in the first 5 headers:

        >>> [x[25] for x in header[:5]]
        [1, 2, 3, 4]

        Read a field in every other header:

        >>> [x[37] for x in header[::2]]
        [1, 3, 1, 3, 1, 3]
        """
        try:
            i = self.wrapindex(i)
            return Field.trace(traceno = i, segy = self.segy)

        except TypeError:
            def gen():
                # double-buffer the header. when iterating over a range, we
                # want to make sure the visible change happens as late as
                # possible, and that in the case of exception the last valid
                # header was untouched. this allows for some fancy control
                # flow, and more importantly helps debugging because you can
                # fully inspect and interact with the last good value.
                x = Field.trace(None, self.segy)
                buf = bytearray(x.buf)
                for j in range(*i.indices(len(self))):
                    # skip re-invoking __getitem__, just update the buffer
                    # directly with fetch, and save some initialisation work
                    buf = x.fetch(buf, j)
                    x.buf[:] = buf
                    x.traceno = j
                    yield x

            return gen()

    def __setitem__(self, i, val):
        """header[i] = val

        Write the ith header of the file, starting at 0. Unlike data traces
        (which return numpy.ndarrays), changes to returned headers being
        iterated over *will* be reflected on disk.

        Parameters
        ----------
        i   : int or slice
        val : Field or array_like of dict_like

        Notes
        -----
        .. versionadded:: 1.1

        Behaves like [] for lists

        Examples
        --------
        Copy a header to a different trace:

        >>> header[28] = header[29]

        Write multiple fields in a trace:

        >>> header[10] = { 37: 5, TraceField.INLINE_3D: 2484 }

        Set a fixed set of values in all headers:

        >>> for x in header[:]:
        ...     x[37] = 1
        ...     x.update({ TraceField.offset: 1, 2484: 10 })

        Write a field in multiple headers

        >>> for x in header[:10]:
        ...     x.update({ TraceField.offset : 2 })

        Write a field in every other header:

        >>> for x in header[::2]:
        ...     x.update({ TraceField.offset : 2 })
        """

        x = self[i]

        try:
            x.update(val)
        except AttributeError:
            if isinstance(val, Field) or isinstance(val, dict):
                val = itertools.repeat(val)

            for h, v in zip(x, val):
                h.update(v)

    @property
    def iline(self):
        """
        Headers, accessed by inline

        Returns
        -------
        line : HeaderLine
        """
        return HeaderLine(self, self.segy.iline, 'inline')

    @iline.setter
    def iline(self, value):
        """Write iterables to lines

        Examples:
            Supports writing to *all* crosslines via assignment, regardless of
            data source and format. Will respect the sample size and structure
            of the file being assigned to, so if the argument traces are longer
            than that of the file being written to the surplus data will be
            ignored. Uses same rules for writing as `f.iline[i] = x`.
        """
        for i, src in zip(self.segy.ilines, value):
            self.iline[i] = src

    @property
    def xline(self):
        """
        Headers, accessed by crossline

        Returns
        -------
        line : HeaderLine
        """
        return HeaderLine(self, self.segy.xline, 'crossline')

    @xline.setter
    def xline(self, value):
        """Write iterables to lines

        Examples:
            Supports writing to *all* crosslines via assignment, regardless of
            data source and format. Will respect the sample size and structure
            of the file being assigned to, so if the argument traces are longer
            than that of the file being written to the surplus data will be
            ignored. Uses same rules for writing as `f.xline[i] = x`.
        """

        for i, src in zip(self.segy.xlines, value):
            self.xline[i] = src
