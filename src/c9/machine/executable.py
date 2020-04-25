"""The C9 Machine Executable class

Think about this like a Lisp Image - contains all definitions and data required.
You can fire up a machine with one of these, and then evaluate things (run
functions, inspect data, etc).

"""

from dataclasses import dataclass


@dataclass
class Executable:
    locations: dict
    foreign: dict
    code: list
    # data: dict  # TODO

    def listing(self):
        res = " /\n"
        for i, instr in enumerate(self.code):
            if i in self.locations.values():
                funcname = next(
                    k for k in self.locations.keys() if self.locations[k] == i
                )
                res += f" | ;; {funcname}:\n"
            res += f" | {i:4} | {instr}\n"
        res += " \\\n"
        return res
