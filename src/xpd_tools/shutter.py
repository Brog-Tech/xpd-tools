"""Shutter devices and plan stubs, ported from hextools."""

import asyncio
import random
from collections.abc import Callable, Hashable

from bluesky import plan_stubs as bps
from bluesky.protocols import Reading, Subscribable
from ophyd_async.core import (
    DEFAULT_TIMEOUT,
    AsyncMovable,
    AsyncStatus,
    Device,
    DeviceMock,
    StrictEnum,
    callback_on_mock_execute,
    callback_on_mock_put,
    default_mock_class,
    derived_signal_r,
    observe_signals_value,
    set_mock_value,
    wait_for_value,
)
from ophyd_async.epics.core import (
    EpicsDevice,
    epics_signal_r,
    epics_signal_rw,
    epics_triggerable_command,
)
from ophyd_async.epics.motor import Motor


class ShutterStatus(StrictEnum):
    """Shutter position status reported by EPICS."""

    OPEN = "Open"
    CLOSED = "Not Open"


class MockShutter(DeviceMock["Shutter"]):
    """Mock shutter that flips status after a random 0-3 s actuation delay."""

    async def connect(self, device: "Shutter") -> None:
        set_mock_value(device.status, ShutterStatus.CLOSED)

        async def _actuate(target: ShutterStatus) -> None:
            await asyncio.sleep(random.uniform(0.0, 3.0))
            set_mock_value(device.status, target)

        async def _open() -> None:
            await _actuate(ShutterStatus.OPEN)

        async def _close() -> None:
            await _actuate(ShutterStatus.CLOSED)

        callback_on_mock_execute(device.open_cmd, _open)
        callback_on_mock_execute(device.close_cmd, _close)


@default_mock_class(MockShutter)
class Shutter(EpicsDevice, Subscribable[bool], AsyncMovable[bool]):
    """Shutter device.

    Parameters
    ----------
    prefix : str
        The PV prefix of the shutter, e.g. ``XF:28IDA-PPS{PSh}``.
    name : str, default ""
        The name of the device.

    Attributes
    ----------
    status : EpicsSignalRO[bool]
        Readback of the shutter status (True for open, False for closed)
    open_cmd : EpicsTriggerableCommand
        Command to open the shutter
    close_cmd : EpicsTriggerableCommand
        Command to close the shutter
    settle_time : float
        Seconds the ensure_shutter_* plans sleep after actuating this shutter,
        once its status reports the new state (class attribute; may be
        overridden per instance).
    """

    settle_time: float = 4.0

    def __init__(self, prefix: str, name: str = ""):
        super().__init__(prefix, name=name)
        self.status = epics_signal_r(ShutterStatus, f"{prefix}Pos-Sts")
        self.open_cmd = epics_triggerable_command(f"{prefix}Cmd:Opn-Cmd")
        self.close_cmd = epics_triggerable_command(f"{prefix}Cmd:Cls-Cmd")
        # Maps a user callback to the status-signal wrapper registered for it.
        self._sub_translators: dict[
            Callable[[dict[str, Reading[bool]]], None],
            Callable[[dict[str, Reading[ShutterStatus]]], None],
        ] = {}

    @AsyncStatus.wrap
    async def set(self, value: bool):
        """Set the state of the shutter.

        Parameters
        ----------
        value : bool
            The desired state of the shutter (True for open, False for closed)

        Returns
        -------
        AsyncStatus
            An object representing the status of the set operation.
        """
        await self._command(value)
        await wait_for_value(
            self.status,
            ShutterStatus.OPEN if value else ShutterStatus.CLOSED,
            timeout=DEFAULT_TIMEOUT,
        )

    async def _command(self, value: bool) -> None:
        """Send the open (True) or close (False) command."""
        await (self.open_cmd if value else self.close_cmd).execute()

    def subscribe_reading(
        self, function: Callable[[dict[str, Reading[bool]]], None]
    ) -> None:
        """Subscribe to changes in the shutter status.

        Parameters
        ----------
        function : Callable[[dict[str, Reading[bool]]], None]
            A function to call with the new shutter status (as a dictionary of readings)
        """

        def _translate(readings: dict[str, Reading[ShutterStatus]]) -> None:
            """Translate readings from ShutterStatus to bool."""
            # The status signal emits a single reading; re-key it under the shutter.
            translated: dict[str, Reading[bool]] = {}
            for reading in readings.values():
                translated[self.name] = {
                    **reading,
                    "value": reading["value"] == ShutterStatus.OPEN,
                }
            function(translated)

        self._sub_translators[function] = _translate
        self.status.subscribe_reading(_translate)

    def clear_sub(self, function: Callable[[dict[str, Reading[bool]]], None]) -> None:
        """Remove a subscription previously passed to `subscribe_reading`.

        Parameters
        ----------
        function : Callable[[dict[str, Reading[bool]]], None]
            The callback to remove.
        """
        translator = self._sub_translators.pop(function)
        self.status.clear_sub(translator)


#: The raw value of a fast shutter position switch when the shutter is in that
#: position: the switches read "SW Low" (False) when made.
FAST_SHUTTER_SWITCH_MADE = False


def _made(raw: bool) -> bool:
    """Whether a fast shutter position switch is made, from its raw value."""
    return raw == FAST_SHUTTER_SWITCH_MADE


class FastShutterStatus(StrictEnum):
    """Fast shutter status; equal to the `ShutterStatus` member of the same value.

    ``UNKNOWN`` is anything the switches and motor do not positively agree on, so
    `ensure_shutter_state` actuates it whether it is asked to open or close.
    """

    OPEN = "Open"
    CLOSED = "Not Open"
    UNKNOWN = "Unknown"


def _fast_shutter_status(
    open_switch: bool,
    closed_switch: bool,
    position: float,
    open_position: float,
    closed_position: float,
    tolerance: float,
) -> FastShutterStatus:
    """Combine the fast shutter's raw switch values and motor position.

    Open or closed only when that end's switch is made, the other switch is not,
    and the motor is within ``tolerance`` of that end; anything else, including
    both or neither switch made or a motor that disagrees, is unknown.
    """
    if (
        _made(open_switch)
        and not _made(closed_switch)
        and abs(position - open_position) <= tolerance
    ):
        return FastShutterStatus.OPEN
    if (
        _made(closed_switch)
        and not _made(open_switch)
        and abs(position - closed_position) <= tolerance
    ):
        return FastShutterStatus.CLOSED
    return FastShutterStatus.UNKNOWN


class MockFastShutter(DeviceMock["FastShutter"]):
    """Mock fast shutter that starts closed, with instant moves.

    The switch at each end is made when the motor reaches it.
    """

    async def connect(self, device: "FastShutter") -> None:
        def _update_switches(readings: dict[str, Reading[float]]) -> None:
            (reading,) = readings.values()
            position = reading["value"]
            for switch, switch_position in (
                (device.open_switch, device.open_position),
                (device.closed_switch, device.closed_position),
            ):
                made = position == switch_position
                set_mock_value(
                    switch,
                    FAST_SHUTTER_SWITCH_MADE if made else not FAST_SHUTTER_SWITCH_MADE,
                )

        set_mock_value(device.motor.user_setpoint, device.closed_position)
        set_mock_value(device.motor.user_readback, device.closed_position)
        device.motor.user_readback.subscribe_reading(_update_switches)


@default_mock_class(MockFastShutter)
class FastShutter(Shutter):
    """The XPD-D fast shutter, driven by moving its rotation motor.

    The IOC's open/close command records are not used: the shutter moves its
    motor to the open or closed angle itself. Its state is read from the
    physical position switches, which read "SW Low" when made; the motor
    position is only a cross-check.

    The status is open or closed only when that end's switch is made, the other
    is not, and the motor is within ``tolerance`` of that end; anything else is
    ``UNKNOWN``, which `ensure_shutter_state` always actuates. After every open
    or close the motor position must agree with the switches, otherwise an error
    names both readings.

    Parameters
    ----------
    prefix : str, default "XF:28IDC-ES:1{Sh2:Exp-Ax:5}"
        The PV prefix of the shutter axis; the motor record is ``{prefix}Mtr``.
    switch_prefix : str, default "XF:28IDC-ES:1{Sh:Exp}"
        The PV prefix of the position switches ``Sw:Opn1-Sts`` and
        ``Sw:Cls1-Sts``.
    open_position : float, default -20.0
        The motor position, in degrees, at which the shutter is open.
    closed_position : float, default 20.0
        The motor position, in degrees, at which the shutter is closed.
    tolerance : float, default 0.5
        How far, in degrees, the motor may be from a position and still agree
        with the switches.
    name : str, default ""
        The name of the device.

    Attributes
    ----------
    motor : Motor
        The shutter's rotation motor.
    open_switch : EpicsSignalR[bool]
        The open position switch, False ("SW Low") when made.
    closed_switch : EpicsSignalR[bool]
        The closed position switch, False ("SW Low") when made.
    status : SignalR[FastShutterStatus]
        Open or closed only when both switches and the motor agree, otherwise
        ``UNKNOWN``.
    settle_time : float
        Seconds the ensure_shutter_* plans sleep after actuating; very short
        because the status already comes from the position switches.
    """

    settle_time: float = 0.05

    def __init__(
        self,
        prefix: str = "XF:28IDC-ES:1{Sh2:Exp-Ax:5}",
        switch_prefix: str = "XF:28IDC-ES:1{Sh:Exp}",
        open_position: float = -20.0,
        closed_position: float = 20.0,
        tolerance: float = 0.5,
        name: str = "",
    ):
        if abs(open_position - closed_position) <= tolerance:
            raise ValueError(
                "open_position and closed_position must be further apart than "
                "tolerance."
            )
        self.open_position = open_position
        self.closed_position = closed_position
        self.tolerance = tolerance
        # Skip Shutter.__init__, whose PVs this shutter does not have.
        EpicsDevice.__init__(self, prefix, name=name)
        self.motor = Motor(f"{prefix}Mtr")
        self.open_switch = epics_signal_r(bool, f"{switch_prefix}Sw:Opn1-Sts")
        self.closed_switch = epics_signal_r(bool, f"{switch_prefix}Sw:Cls1-Sts")
        self.status = derived_signal_r(
            _fast_shutter_status,
            open_switch=self.open_switch,
            closed_switch=self.closed_switch,
            position=self.motor.user_readback,
            open_position=open_position,
            closed_position=closed_position,
            tolerance=tolerance,
        )
        self._sub_translators = {}
        # Name the children now that they exist (see set_name).
        self.set_name(name)

    def set_name(self, name: str, *, child_name_separator: str | None = None) -> None:
        """Name the shutter, giving its children distinct names even if unnamed.

        The derived ``status`` keys its inputs by name, so the unnamed children
        an unnamed device gets (all ``""``) would overwrite each other and the
        status would never update. Unnamed, each child is named after its
        attribute instead.
        """
        super().set_name(name, child_name_separator=child_name_separator)
        if not name:
            for attr_name, child in self.children():
                child.set_name(attr_name, child_name_separator=child_name_separator)

    async def _command(self, value: bool) -> None:
        """Open or close, confirmed by the switches, then check the position.

        The requested state counts as shown only when its switch is made and the
        other switch is not. Does not move if the switches already show it:
        moving to where the motor already is changes no switch.

        Raises
        ------
        TimeoutError
            If the switches do not show the requested state within
            ``DEFAULT_TIMEOUT`` of the move.
        RuntimeError
            If the motor position does not agree with the switches.
        """
        target = self.open_position if value else self.closed_position
        state = "open" if value else "closed"
        made, other = (
            (self.open_switch, self.closed_switch)
            if value
            else (self.closed_switch, self.open_switch)
        )

        async def _shown() -> None:
            # Keyed by identity: unnamed switches share the name "".
            made_value: bool | None = None
            other_value: bool | None = None
            async for signal, switch_value in observe_signals_value(made, other):
                if signal is made:
                    made_value = switch_value
                else:
                    other_value = switch_value
                if (
                    made_value is not None
                    and other_value is not None
                    and _made(made_value)
                    and not _made(other_value)
                ):
                    return

        made_now, other_now = await asyncio.gather(made.get_value(), other.get_value())
        if not (_made(made_now) and not _made(other_now)):
            await self.motor.set(target)
            try:
                await asyncio.wait_for(_shown(), timeout=DEFAULT_TIMEOUT)
            except TimeoutError as exc:
                raise TimeoutError(
                    f"{self.name}: the motor moved to {target}, but the switches "
                    f"did not show {state} within {DEFAULT_TIMEOUT} s. "
                    f"{await self._describe()}"
                ) from exc
        await self._check_position_agrees(target, state)

    async def _check_position_agrees(self, target: float, state: str) -> None:
        position = await self.motor.user_readback.get_value()
        if abs(position - target) > self.tolerance:
            raise RuntimeError(
                f"{self.name}: the switches show {state}, but "
                f"the motor is at {position}, not {target} (tolerance "
                f"{self.tolerance}). The switches and motor position disagree; "
                f"check the shutter before taking data. {await self._describe()}"
            )

    async def _describe(self) -> str:
        """Both readbacks, for error messages."""
        open_switch, closed_switch, position = await asyncio.gather(
            self.open_switch.get_value(),
            self.closed_switch.get_value(),
            self.motor.user_readback.get_value(),
        )
        return (
            f"Open switch {'made' if _made(open_switch) else 'not made'}, closed "
            f"switch {'made' if _made(closed_switch) else 'not made'}, motor at "
            f"{position}."
        )


class MockInterlockAcknowledge(DeviceMock["InterlockAcknowledge"]):
    """Mock interlock whose status follows the acknowledge request at once."""

    async def connect(self, device: "InterlockAcknowledge") -> None:
        acknowledged = device.acknowledged
        set_mock_value(acknowledged, True)
        callback_on_mock_put(
            device.request, lambda value, **_: set_mock_value(acknowledged, value)
        )


@default_mock_class(MockInterlockAcknowledge)
class InterlockAcknowledge(Device, AsyncMovable[bool]):
    """A PPS shutter's interlock acknowledge.

    Setting True acknowledges the interlock and waits until the PPS reports it
    acknowledged; nothing is written if it already is.

    Attributes
    ----------
    request : EpicsSignalRW[bool]
        The acknowledge request (``Intlk:Ack-Sel``).
    acknowledged : EpicsSignalR[bool]
        Whether the interlock is acknowledged (``Intlk:Ack-Sts``).
    """

    def __init__(self, prefix: str, name: str = ""):
        self.request = epics_signal_rw(bool, f"{prefix}Intlk:Ack-Sel")
        self.acknowledged = epics_signal_r(bool, f"{prefix}Intlk:Ack-Sts")
        super().__init__(name=name)

    @AsyncStatus.wrap
    async def set(self, value: bool):
        if await self.acknowledged.get_value() == value:
            return
        await self.request.set(value)
        await wait_for_value(self.acknowledged, value, timeout=DEFAULT_TIMEOUT)


class PPSShutter(Shutter):
    """PPS shutter whose interlock must be acknowledged after it trips.

    Attributes
    ----------
    interlock_ack : InterlockAcknowledge
        Move it to True to acknowledge the interlock.
    """

    def __init__(self, prefix: str, name: str = ""):
        self.interlock_ack = InterlockAcknowledge(prefix)
        super().__init__(prefix, name=name)


def ensure_shutter_state(
    shutter: Shutter | list[Shutter],
    desired_state: bool,
    allow_actuation: bool = False,
    group: Hashable | None = None,
    wait: bool = True,
    sleep_time: float | None = None,
):
    """Plan stub to guarantees that the shutter(s) is/are in the desired state.

    Parameters
    ----------
    shutter : Shutter | list[Shutter]
        shutter(s) to guarantee the state of.
    desired_state : bool
        the state of the shutter (True for open, False for closed)
    allow_actuation : bool, default False
        whether to allow the plan to actuate the shutter if it is not in the
        desired state
    group : Hashable | None, optional
        the Bluesky group to use for the actuation, if any
    wait : bool, default True
        whether to wait for the shutter to reach the desired state after actuation
    sleep_time : float | None, optional
        the time to sleep to ensure the shutter has reached the desired state.
        By default the longest `Shutter.settle_time` of the shutters that were
        actuated is used.
    """
    shutters = shutter if isinstance(shutter, list) else [shutter]

    # Listed in order rather than keyed by name: unnamed shutters share "".
    shutter_statuses = []
    for s in shutters:
        shutter_statuses.append((yield from bps.rd(s.status)))

    # Anything but the desired state, such as a fast shutter's UNKNOWN, counts
    # as not there.
    target = ShutterStatus.OPEN if desired_state else ShutterStatus.CLOSED
    actuated: list[Shutter] = []
    for s, status in zip(shutters, shutter_statuses, strict=True):
        if status != target:
            if allow_actuation:
                yield from bps.abs_set(s, desired_state, group=group, wait=False)
                actuated.append(s)
            else:
                raise ValueError(
                    f"Shutter {s.name} is {status.name.lower()}, but"
                    f" the desired state is {'open' if desired_state else 'closed'}!"
                )

    if wait and actuated:
        yield from bps.wait(group=group, timeout=DEFAULT_TIMEOUT)

        # It seems that the shutter status in EPICS is not reliable enough to
        # consider the shutter fully opened/closed, so wait some extra time.
        if sleep_time is None:
            sleep_time = max(s.settle_time for s in actuated)
        yield from bps.sleep(sleep_time)


def ensure_shutter_open(
    shutter: Shutter | list[Shutter],
    allow_actuation: bool = False,
    group: Hashable | None = None,
    wait: bool = True,
    sleep_time: float | None = None,
):
    """Plan stub to guarantee that the shutter(s) is/are open.

    Parameters
    ----------
    shutter : Shutter | list[Shutter]
        shutter(s) to guarantee the state of.
    allow_actuation : bool, default False
        whether to allow the plan to actuate the shutter if it is not in the
        desired state
    group : Hashable | None, optional
        the Bluesky group to use for the actuation, if any
    wait : bool, default True
        whether to wait for the shutter to reach the desired state after actuation
    sleep_time : float | None, optional
        the time to sleep to ensure the shutter has reached the desired state.
        By default the longest `Shutter.settle_time` of the shutters that were
        actuated is used.
    """
    yield from ensure_shutter_state(
        shutter,
        True,
        allow_actuation=allow_actuation,
        wait=wait,
        group=group,
        sleep_time=sleep_time,
    )


def ensure_shutter_closed(
    shutter: Shutter | list[Shutter],
    allow_actuation: bool = True,
    group: Hashable | None = None,
    wait: bool = True,
    sleep_time: float | None = None,
):
    """Plan stub to guarantee that the shutter is closed.

    Parameters
    ----------
    shutter : Shutter | list[Shutter]
        shutter(s) to guarantee the state of.
    allow_actuation : bool, default True
        whether to allow the plan to actuate the shutter if it is not in the
        desired state
    group : Hashable | None, optional
        the Bluesky group to use for the actuation, if any
    wait : bool, default True
            whether to wait for the shutter to reach the desired state after actuation
    sleep_time : float | None, optional
        the time to sleep to ensure the shutter has reached the desired state.
        By default the longest `Shutter.settle_time` of the shutters that were
        actuated is used.
    """
    yield from ensure_shutter_state(
        shutter,
        False,
        allow_actuation=allow_actuation,
        wait=wait,
        group=group,
        sleep_time=sleep_time,
    )
