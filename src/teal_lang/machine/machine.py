"""The Teal virtual machine

To implement closures, just note - a closure is just an unnamed function with
some bindings. Those bindings may be explicit, or not, but are taken from the
current lexical environment. Lexical bindings are introduced by function
definitions, or let-bindings.

"""

import builtins
import importlib
import logging
import os
import sys
import time
from functools import singledispatchmethod
from io import StringIO
from typing import Any, Dict, List

from . import types as mt
from .controller import Controller
from .executable import Executable
from .instruction import Instruction
from .instructionset import *
from .probe import Probe
from .state import State
from .controller import ActivationRecord

LOG = logging.getLogger(__name__)


class ImportPyError(Exception):
    """Error importing some code from Python"""


class TealRuntimeError(Exception):
    """Error while executing Teal code"""


class UnhandledError(Exception):
    """Some Teal code signaled an error which was not handled"""


class RunMachineError(Exception):
    """An error occurred while running the machine"""


class ForeignError(Exception):
    pass


def traverse(o, tree_types=(list, tuple)):
    """Traverse an arbitrarily nested list"""
    if isinstance(o, tree_types):
        for value in o:
            for subvalue in traverse(value, tree_types):
                yield subvalue
    else:
        yield o


def import_python_function(fnname, modname):
    """Load function

    If modname is None, fnname is taken from __builtins__ (e.g. 'print')

    PYTHONPATH must be set up already.
    """
    if modname == "__builtins__":
        if not os.getenv("ENABLE_IMPORT_BUILTIN", False):
            raise ImportPyError("Cannot import from builtins")
        m = builtins
    else:
        spec = importlib.util.find_spec(modname)
        if not spec:
            raise ImportPyError(f"Could not find module `{modname}`")
        m = spec.loader.load_module()

    fn = getattr(m, fnname)
    LOG.debug("Loaded %s", fn)
    return fn


class TlMachine:
    """Virtual Machine to execute Teal bytecode.

    The machine operates in the context of a Controller. There may be multiple
    machines connected to the same controller. All machines share the same
    executable, defined by the controller.

    There is one Machine per compute node. There may be multiple compute nodes.

    When run normally, the Machine starts executing instructions from the
    beginning until the instruction pointer reaches the end.

    """

    builtins = {
        "wait": Wait,
        "print": Print,
        "sleep": Sleep,
        "atomp": Atomp,
        "nullp": Nullp,
        "list": List,
        "conc": Conc,
        "append": Append,
        "first": First,
        "rest": Rest,
        # "future": Future,
        "nth": Nth,
        "==": Eq,
        "+": Plus,
        # "-": Minus
        "*": Multiply,
        ">": GreaterThan,
        "<": LessThan,
        "parse_float": ParseFloat,
        "signal": Signal,
    }

    def __init__(self, vmid, invoker):
        self.vmid = vmid
        self.invoker = invoker
        self.dc = invoker.data_controller
        self.state = self.dc.get_state(self.vmid)
        self.probe = self.dc.get_probe(self.vmid)
        self.exe = self.dc.executable
        self._foreign = {
            name: import_python_function(val.identifier, val.module)
            for name, val in self.exe.bindings.items()
            if isinstance(val, mt.TlForeignPtr)
        }
        LOG.debug("locations %s", self.exe.locations.keys())
        LOG.debug("foreign %s", self._foreign.keys())
        # No entrypoint argument - just set the IP in the state

    def error(self, original, msg):
        # TODO stacktraces
        if original:
            raise TealRuntimeError(msg) from original
        else:
            raise TealRuntimeError(msg)

    @property
    def stopped(self):
        return self.state.stopped

    @property
    def instruction(self):
        return self.exe.code[self.state.ip]

    def step(self):
        """Execute the current instruction and increment the IP"""
        if self.state.ip >= len(self.exe.code):
            self.error(None, "Instruction Pointer out of bounds")
        self.probe.on_step(self)
        instr = self.exe.code[self.state.ip]
        self.state.ip += 1
        self.evali(instr)

    def run(self):
        """Step through instructions until stopped, or an error occurs

        ERRORS: If one occurs, then:
        - store it in the data controller for analysis later
        - stop execution
        - raise it

        There are two "expected" kinds of errors - a Foreign function error, and
        a Rust "panic!" style error (general error).
        """
        self.probe.on_run(self)
        exc = None

        while not self.state.stopped:
            try:
                self.step()
            except ForeignError as exc:
                self.dc.foreign_error(self.vmid, exc)
                self.state.stopped = True
            except UnhandledError as exc:
                self.dc.teal_error(self.vmid, exc)
                self.state.stopped = True
            except Exception as exc:
                self.dc.unexpected_error(self.vmid, exc)
                self.state.stopped = True

        self.probe.on_stopped(self)
        self.dc.stop(self.vmid, self.state, self.probe)
        if exc:
            # TODO assign to state.error
            raise RunMachineError(exc) from exc

    @singledispatchmethod
    def evali(self, i: Instruction):
        """Evaluate instruction"""
        raise NotImplementedError(i)

    @evali.register
    def _(self, i: Bind):
        """Bind the top value on the data stack to a name"""
        ptr = str(i.operands[0])
        try:
            val = self.state.ds_peek(0)
        except IndexError as exc:
            # FIXME this should be a compile time check
            self.error(exc, "Missing argument to Bind!")
        if not isinstance(val, mt.TlType):
            raise TypeError(val)
        self.state.bindings[ptr] = val

    @evali.register
    def _(self, i: PushB):
        """Push the value bound to a name onto the data stack"""
        # The value on the stack must be a Symbol, which is used to find a
        # function to call. Binding precedence:
        #
        # local binding -> exe global bindings -> builtins
        sym = i.operands[0]
        if not isinstance(sym, mt.TlSymbol):
            raise ValueError(sym, type(sym))

        ptr = str(sym)
        if ptr in self.state.bindings:
            val = self.state.bindings[ptr]
        elif ptr in self.exe.bindings:
            val = self.exe.bindings[ptr]
        elif ptr in TlMachine.builtins:
            val = mt.TlInstruction(ptr)
        else:
            # FIXME should be a compile time check
            raise NameError(f"'{ptr}' is not defined")

        self.state.ds_push(val)

    @evali.register
    def _(self, i: PushV):
        val = i.operands[0]
        self.state.ds_push(val)

    @evali.register
    def _(self, i: Pop):
        self.state.ds_pop()

    @evali.register
    def _(self, i: Jump):
        distance = i.operands[0]
        self.state.ip += distance

    @evali.register
    def _(self, i: JumpIf):
        distance = i.operands[0]
        a = self.state.ds_pop()
        # "true" means anything that's not False or Null
        if not isinstance(a, (mt.TlNull, mt.TlFalse)):
            self.state.ip += distance

    @evali.register
    def _(self, i: Return):
        current_arec, new_arec = self.dc.pop_arec(self.state.current_arec_ptr)
        # Only return in the same thread
        if current_arec.vmid == self.vmid and current_arec.call_site:
            self.probe.on_return(self)
            self.state.current_arec_ptr = current_arec.dynamic_chain  # the new AR
            self.state.ip = current_arec.call_site + 1
            self.state.bindings = new_arec.bindings
        else:
            self.state.stopped = True
            value = self.state.ds_peek(0)
            LOG.info(f"{self.vmid} Returning value: {value}")
            value, continuations = self.dc.finish(self.vmid, value)
            for machine in continuations:
                self.probe.log(
                    f"{self.vmid}: setting machine {machine} value to {value}"
                )
                self.dc.set_future_value(machine, 0, value)
                # FIXME - invoke all of the machines except the last one. That
                # one, just run in this context. Save one invocation. Caution:
                # tricky with Lambda timeouts.
                self.invoker.invoke(machine)

    @evali.register
    def _(self, i: Call):
        # Arguments for the function must already be on the stack
        num_args = i.operands[0]
        # The value to call will have been retrieved earlier by PushB.
        fn = self.state.ds_pop()
        self.probe.on_enter(self, str(fn))

        if isinstance(fn, mt.TlFunctionPtr):
            self.state.bindings = {}
            arec = ActivationRecord(
                function=fn,
                vmid=self.vmid,
                dynamic_chain=self.state.current_arec_ptr,
                call_site=self.state.ip - 1,
                bindings=self.state.bindings,
                ref_count=1,
            )
            self.state.current_arec_ptr = self.dc.push_arec(self.vmid, arec)
            self.state.ip = self.exe.locations[fn.identifier]

        elif isinstance(fn, mt.TlForeignPtr):
            foreign_f = self._foreign[fn.identifier]
            args = tuple(reversed([self.state.ds_pop() for _ in range(num_args)]))
            self.probe.log(f"{self.vmid}--> {foreign_f} {args}")
            # TODO automatically wait for the args? Somehow mark which one we're
            # waiting for in the continuation

            py_args = list(map(mt.to_py_type, args))

            # capture Python's standard output
            sys.stdout = capstdout = StringIO()
            try:
                py_result = foreign_f(*py_args)
            except Exception as e:
                raise ForeignError(e)
            finally:
                sys.stdout = sys.__stdout__

            out = capstdout.getvalue()
            self.dc.write_stdout(out)

            result = mt.to_teal_type(py_result)
            self.state.ds_push(result)

        elif isinstance(fn, mt.TlInstruction):
            instr = TlMachine.builtins[fn](num_args)
            self.evali(instr)

        else:
            # FIXME this should be a compile time check
            self.error(None, f"Don't know how to call {fn} ({type(fn)})")

    @evali.register
    def _(self, i: ACall):
        # Arguments for the function must already be on the stack
        # ACall can *only* call functions in self.locations (unlike Call)
        num_args = i.operands[0]
        fn_ptr = self.state.ds_pop()

        if not isinstance(fn_ptr, mt.TlFunctionPtr):
            raise ValueError(fn_ptr)

        if fn_ptr.identifier not in self.exe.locations:
            # FIXME this should be a compile time check
            self.error(None, f"Function `{fn_ptr}` doesn't exist")

        args = reversed([self.state.ds_pop() for _ in range(num_args)])
        machine = self.dc.thread_machine(
            self.state.current_arec_ptr, self.state.ip, fn_ptr, args
        )
        self.invoker.invoke(machine)
        future = mt.TlFuturePtr(machine)

        self.probe.log(f"Fork {self} => {future}")
        self.state.ds_push(future)

    @evali.register
    def _(self, i: Wait):
        offset = 0  # TODO cleanup - no more offset!
        val = self.state.ds_peek(offset)

        if isinstance(val, mt.TlFuturePtr):
            resolved, result = self.dc.get_or_wait(self.vmid, val, self.state)
            if resolved:
                LOG.info(f"{self.vmid} Finished waiting for {val}, got {result}")
                self.state.ds_set(offset, result)
            else:
                LOG.info(f"{self.vmid} waiting for {val}")
                assert self.state.stopped

        elif isinstance(val, list) and any(
            isinstance(elt, mt.TlFuturePtr) for elt in traverse(val)
        ):
            # The programmer is responsible for waiting on all elements
            # of lists.
            # NOTE - we don't try to detect futures hidden in other
            # kinds of structured data, which could cause runtime bugs!
            self.error(error, "Waiting on a list that contains futures!")

        else:
            # Not an exception. This can happen if a wait is generated for a
            # normal function call. ie the value already exists.
            pass

    ## "builtins":

    @evali.register
    def _(self, i: Atomp):
        val = self.state.ds_pop()
        self.state.ds_push(tl_bool(not isinstance(val, list)))

    @evali.register
    def _(self, i: Nullp):
        val = self.state.ds_pop()
        isnull = isinstance(val, mt.TlNull) or len(val) == 0
        self.state.ds_push(tl_bool(isnull))

    @evali.register
    def _(self, i: List):
        num_args = i.operands[0]
        elts = [self.state.ds_pop() for _ in range(num_args)]
        self.state.ds_push(mt.TlList(reversed(elts)))

    @evali.register
    def _(self, i: Conc):
        b = self.state.ds_pop()
        a = self.state.ds_pop()

        # Null is interpreted as the empty list for b
        b = mt.TlList([]) if isinstance(b, mt.TlNull) else b

        if not isinstance(b, mt.TlList):
            self.error(None, f"b ({b}, {type(b)}) is not a list")

        if isinstance(a, mt.TlList):
            self.state.ds_push(mt.TlList(a + b))
        else:
            self.state.ds_push(mt.TlList([a] + b))

    @evali.register
    def _(self, i: Append):
        b = self.state.ds_pop()
        a = self.state.ds_pop()

        a = mt.TlList([]) if isinstance(a, mt.TlNull) else a

        if not isinstance(a, mt.TlList):
            self.error(None, f"{a} ({type(a)}) is not a list")

        self.state.ds_push(mt.TlList(a + [b]))

    @evali.register
    def _(self, i: First):
        lst = self.state.ds_pop()
        if not isinstance(lst, mt.TlList):
            self.error(None, f"{lst} ({type(lst)}) is not a list")
        self.state.ds_push(lst[0])

    @evali.register
    def _(self, i: Rest):
        lst = self.state.ds_pop()
        if not isinstance(lst, mt.TlList):
            self.error(None, f"{lst} ({type(lst)}) is not a list")
        self.state.ds_push(lst[1:])

    @evali.register
    def _(self, i: Nth):
        n = self.state.ds_pop()
        lst = self.state.ds_pop()
        if not isinstance(lst, mt.TlList):
            self.error(None, f"{lst} ({type(lst)}) is not a list")
        self.state.ds_push(lst[n])

    @evali.register
    def _(self, i: Plus):
        a = self.state.ds_pop()
        b = self.state.ds_pop()
        cls = new_number_type(a, b)
        self.state.ds_push(cls(a + b))

    @evali.register
    def _(self, i: Multiply):
        a = self.state.ds_pop()
        b = self.state.ds_pop()
        cls = new_number_type(a, b)
        self.state.ds_push(cls(a * b))

    @evali.register
    def _(self, i: Eq):
        a = self.state.ds_pop()
        b = self.state.ds_pop()
        self.state.ds_push(tl_bool(a == b))

    @evali.register
    def _(self, i: GreaterThan):
        a = self.state.ds_pop()
        b = self.state.ds_pop()
        self.state.ds_push(tl_bool(a > b))

    @evali.register
    def _(self, i: LessThan):
        a = self.state.ds_pop()
        b = self.state.ds_pop()
        self.state.ds_push(tl_bool(a < b))

    @evali.register
    def _(self, i: ParseFloat):
        x = self.state.ds_pop()
        self.state.ds_push(mt.TlFloat(float(x)))

    @evali.register
    def _(self, i: Sleep):
        t = self.state.ds_peek(0)
        time.sleep(t)

    @evali.register
    def _(self, i: Print):
        # Leave the value in the stack - print() 'returns' the value printed
        val = self.state.ds_peek(0)
        # This should take a vmid - data stored is a tuple (vmid, str)
        # Could also store a timestamp...
        self.dc.write_stdout(str(val) + "\n")

    @evali.register
    def _(self, i: Signal):
        msg = self.state.ds_peek(0)
        val = self.state.ds_peek(1)
        self.dc.write_stdout(f"\n{val.upper()}: {msg}\n")
        if str(val) == "error":
            raise UnhandledError(msg)
        # other kinds of signals don't need special handling

    def __repr__(self):
        return f"<Machine {id(self)}>"


def tl_bool(val):
    """Make a Teal bool-ish from val"""
    return mt.TlTrue() if val is True else mt.TlFalse()


def new_number_type(a, b):
    """The number type to use on operations of two numbers"""
    if isinstance(a, mt.TlFloat) or isinstance(b, mt.TlFloat):
        return mt.TlFloat
    else:
        return mt.TlInt
