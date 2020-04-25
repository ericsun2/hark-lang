"""Machine Probe"""


class Probe:
    """A machine debug probe"""

    count = 0

    def __init__(self, *, max_steps=500):
        self._max_steps = max_steps
        self._step = 0
        Probe.count += 1
        self._name = f"P{Probe.count}"
        self.logs = []
        self.early_stop = False

    @classmethod
    def with_logs(cls, logs):
        probe = cls()
        probe.logs = logs
        return probe

    def on_run(self, m):
        pass

    def log(self, text):
        self.logs.append(f"*** <{self._name}> {text}")

    def on_enter(self, m, fn_name: str):
        self.log(f"===> {fn_name}")

    def on_return(self, m):
        self.log(f"<===")

    def on_step(self, m):
        self._step += 1
        preface = f"[step={self._step}, ip={m.state.ip}] {m.instruction}"
        data = list(m.state._ds)
        self.log(f"{preface:40.40} | {data}")
        # self.logs.append("Data: " + str(tuple(m.state._ds)))
        if self._step >= self._max_steps:
            self.log(f"MAX STEPS ({self._max_steps}) REACHED!! ***")
            self.early_stop = True
            m._stopped = True

    def on_stopped(self, m):
        kind = "Terminated" if m.terminated else "Stopped"
        self.logs.append(f"*** <{self._name}> {kind} after {self._step} steps. ***")
        self.logs.append(m.state.to_table())
