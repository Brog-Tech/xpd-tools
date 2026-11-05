"""Building blocks and helpers shared across xpd_tools.

- ``motors``: base classes and mocks for building custom motors
- ``flyers``: flyscan logic used by the plans
- ``session``: IPython/RunEngine helpers for the beamline profiles. It pulls in
  heavy dependencies (IPython, nslsii), so import it explicitly:
  ``from xpd_tools.utils.session import start_beamtime``.
"""
