"""why_net -- v2.7 T1 Why net substrate (P1).

Spec: docs/design/v27-t1-why-net-LOCKED.md (sections 1-5 are frozen).
Only routines/why-net.py writes the store; every other reader goes through
these modules or derived/*.  Pure stdlib.  English/ASCII only.
"""


class WhyRefusal(Exception):
    """A floor refusal: nothing was written.  `reason` is the machine reason
    printed as `signal::WHY_REFUSED reason=<reason>` and maps to exit 3."""

    def __init__(self, reason, **detail):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class LazyModule(object):
    """A module imported on first attribute use (AMEND-2 G4: the frame
    verb's hot path never needs the transcript resolver, whose import
    parses other modules' sources).  Attribute reads go to the real module,
    so a test that patches the real module patches this too."""

    def __init__(self, name):
        self._name, self._mod = name, None

    def __getattr__(self, attr):
        if self._mod is None:
            import importlib
            self._mod = importlib.import_module(self._name)
        return getattr(self._mod, attr)

