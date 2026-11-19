"""Tests for the shutter devices and plan stubs."""

import pytest
from bluesky.run_engine import RunEngine, call_in_bluesky_event_loop
from bluesky.utils import FailedStatus
from ophyd_async.core import (
    callback_on_mock_execute,
    get_mock_put,
    init_devices,
    set_mock_value,
)

from xpd_tools import shutter as shutter_module
from xpd_tools.shutter import (
    FastShutter,
    FastShutterStatus,
    PPSShutter,
    Shutter,
    ShutterStatus,
    ensure_shutter_closed,
    ensure_shutter_open,
    ensure_shutter_state,
)


@pytest.fixture
async def shutter() -> Shutter:
    async with init_devices(mock=True):
        shutter = Shutter("SH:", name="shutter")
    callback_on_mock_execute(
        shutter.open_cmd, lambda: set_mock_value(shutter.status, ShutterStatus.OPEN)
    )
    callback_on_mock_execute(
        shutter.close_cmd,
        lambda: set_mock_value(shutter.status, ShutterStatus.CLOSED),
    )
    return shutter


async def test_shutter_set_opens_and_closes(shutter: Shutter):
    await shutter.set(True)
    assert await shutter.status.get_value() == ShutterStatus.OPEN
    await shutter.set(False)
    assert await shutter.status.get_value() == ShutterStatus.CLOSED


async def test_shutter_subscription_reports_bool(shutter: Shutter):
    readings: list[bool] = []

    def callback(reading):
        readings.append(reading["shutter"]["value"])

    shutter.subscribe_reading(callback)
    set_mock_value(shutter.status, ShutterStatus.OPEN)
    shutter.clear_sub(callback)
    set_mock_value(shutter.status, ShutterStatus.CLOSED)
    assert readings == [False, True]


def test_ensure_open_refuses_to_actuate_by_default(RE: RunEngine, shutter: Shutter):
    with pytest.raises(ValueError, match="shutter is closed"):
        RE(ensure_shutter_open(shutter))


async def test_ensure_open_and_closed_actuate(
    RE: RunEngine, shutter: Shutter, sleeps: list[float]
):
    RE(ensure_shutter_open(shutter, allow_actuation=True))
    assert await shutter.status.get_value() == ShutterStatus.OPEN
    RE(ensure_shutter_closed(shutter))
    assert await shutter.status.get_value() == ShutterStatus.CLOSED
    assert sleeps == [4.0, 4.0]


def test_ensure_closed_skips_closed_shutter(
    RE: RunEngine, shutter: Shutter, sleeps: list[float]
):
    RE(ensure_shutter_closed(shutter))
    assert sleeps == []


async def test_pps_interlock_acknowledge():
    async with init_devices(mock=True):
        pps = PPSShutter("PPS:", name="pps")
    set_mock_value(pps.interlock_ack.acknowledged, False)
    await pps.interlock_ack.set(True)
    get_mock_put(pps.interlock_ack.request).assert_called_once()
    assert await pps.interlock_ack.acknowledged.get_value()


@pytest.fixture
async def fast() -> FastShutter:
    async with init_devices(mock=True):
        fast = FastShutter(name="fast_shutter")
    return fast


async def test_fast_shutter_pvs(fast: FastShutter):
    assert fast.motor.user_setpoint.source == (
        "mock+ca://XF:28IDC-ES:1{Sh2:Exp-Ax:5}Mtr.VAL"
    )
    assert fast.open_switch.source == "mock+ca://XF:28IDC-ES:1{Sh:Exp}Sw:Opn1-Sts"
    assert fast.closed_switch.source == "mock+ca://XF:28IDC-ES:1{Sh:Exp}Sw:Cls1-Sts"


# The fast shutter's switches read False ("SW Low") when made.
MADE, NOT_MADE = False, True


async def test_fast_shutter_moves_motor(fast: FastShutter):
    assert await fast.status.get_value() == ShutterStatus.CLOSED
    await fast.set(True)
    assert await fast.motor.user_readback.get_value() == -20.0
    assert await fast.open_switch.get_value() == MADE
    assert await fast.closed_switch.get_value() == NOT_MADE
    assert await fast.status.get_value() == ShutterStatus.OPEN
    await fast.set(False)
    assert await fast.motor.user_readback.get_value() == 20.0
    assert await fast.open_switch.get_value() == NOT_MADE
    assert await fast.closed_switch.get_value() == MADE
    assert await fast.status.get_value() == ShutterStatus.CLOSED


@pytest.mark.parametrize(
    ("open_switch", "closed_switch", "position", "expected"),
    [
        (MADE, NOT_MADE, -20.0, FastShutterStatus.OPEN),
        (MADE, NOT_MADE, -19.6, FastShutterStatus.OPEN),
        (NOT_MADE, MADE, 20.0, FastShutterStatus.CLOSED),
        # Neither or both made: no state is shown.
        (NOT_MADE, NOT_MADE, -20.0, FastShutterStatus.UNKNOWN),
        (NOT_MADE, NOT_MADE, 20.0, FastShutterStatus.UNKNOWN),
        (MADE, MADE, -20.0, FastShutterStatus.UNKNOWN),
        # Switches and motor disagree.
        (MADE, NOT_MADE, 20.0, FastShutterStatus.UNKNOWN),
        (NOT_MADE, MADE, 0.0, FastShutterStatus.UNKNOWN),
    ],
)
async def test_fast_shutter_status(
    fast: FastShutter,
    open_switch: bool,
    closed_switch: bool,
    position: float,
    expected: FastShutterStatus,
):
    set_mock_value(fast.motor.user_readback, position)
    set_mock_value(fast.open_switch, open_switch)
    set_mock_value(fast.closed_switch, closed_switch)
    assert await fast.status.get_value() == expected


@pytest.mark.parametrize("desired", [True, False])
@pytest.mark.parametrize(
    ("open_switch", "closed_switch", "position"),
    [(NOT_MADE, NOT_MADE, -20.0), (NOT_MADE, NOT_MADE, 20.0), (MADE, MADE, 20.0)],
)
def test_ensure_actuates_unknown_fast_shutter(
    RE: RunEngine,
    fast: FastShutter,
    sleeps: list[float],
    desired: bool,
    open_switch: bool,
    closed_switch: bool,
    position: float,
):
    # An unknown state is never taken as the desired one: set() runs its switch
    # wait and position check whichever way it is asked to go.
    set_mock_value(fast.motor.user_readback, position)
    set_mock_value(fast.open_switch, open_switch)
    set_mock_value(fast.closed_switch, closed_switch)
    RE(ensure_shutter_state(fast, desired, allow_actuation=True))
    expected = FastShutterStatus.OPEN if desired else FastShutterStatus.CLOSED
    assert call_in_bluesky_event_loop(fast.status.get_value()) == expected
    get_mock_put(fast.motor.user_setpoint).assert_called_once_with(
        -20.0 if desired else 20.0
    )


def test_ensure_stops_when_switches_and_motor_disagree(
    RE: RunEngine, fast: FastShutter, sleeps: list[float]
):
    # The switches show open but the motor is at the closed angle: the plan stops
    # with both readings rather than trusting either.
    set_mock_value(fast.motor.user_readback, 20.0)
    set_mock_value(fast.open_switch, MADE)
    set_mock_value(fast.closed_switch, NOT_MADE)
    with pytest.raises(FailedStatus) as info:
        RE(ensure_shutter_open(fast, allow_actuation=True))
    assert "disagree" in str(info.value.__cause__ or info.value)


async def test_unnamed_fast_shutter_opens_and_closes():
    # An unnamed device would leave every child named "", which the switch wait
    # and the derived status must not depend on.
    fast = FastShutter()
    await fast.connect(mock=True)
    assert fast.name == ""
    assert fast.open_switch.name != fast.closed_switch.name
    await fast.set(True)
    assert await fast.status.get_value() == FastShutterStatus.OPEN
    await fast.set(False)
    assert await fast.status.get_value() == FastShutterStatus.CLOSED


def test_ensure_checks_every_unnamed_shutter(RE: RunEngine):
    # Statuses of unnamed shutters must not overwrite each other.
    async def _make() -> tuple[Shutter, Shutter]:
        closed, opened = Shutter("A:"), Shutter("B:")
        await closed.connect(mock=True)
        await opened.connect(mock=True)
        set_mock_value(closed.status, ShutterStatus.CLOSED)
        set_mock_value(opened.status, ShutterStatus.OPEN)
        return closed, opened

    closed, opened = call_in_bluesky_event_loop(_make())
    with pytest.raises(ValueError, match="is closed"):
        RE(ensure_shutter_open([closed, opened]))


async def test_fast_shutter_repeat_set_does_not_move(fast: FastShutter):
    await fast.set(True)
    await fast.set(True)
    await fast.set(False)
    await fast.set(False)
    puts = get_mock_put(fast.motor.user_setpoint).call_args_list
    assert [c.args[0] for c in puts] == [-20.0, 20.0]


async def test_fast_shutter_reports_position_disagreement(fast: FastShutter):
    # The switches show closed, but the motor is not at the closed angle.
    set_mock_value(fast.motor.user_readback, 5.0)
    set_mock_value(fast.closed_switch, MADE)
    set_mock_value(fast.open_switch, NOT_MADE)
    with pytest.raises(RuntimeError, match="motor is at 5.0, not 20.0.*disagree"):
        await fast.set(False)


async def test_fast_shutter_fault_state_is_moved(fast: FastShutter):
    # Both switches made shows neither state, so closing still moves.
    set_mock_value(fast.motor.user_readback, -20.0)
    set_mock_value(fast.open_switch, MADE)
    set_mock_value(fast.closed_switch, MADE)
    await fast.set(False)
    get_mock_put(fast.motor.user_setpoint).assert_called_once_with(20.0)
    assert await fast.status.get_value() == ShutterStatus.CLOSED


async def test_fast_shutter_open_fails_without_switch(
    fast: FastShutter, monkeypatch: pytest.MonkeyPatch
):
    # The motor reports its move done, but the switches never change.
    async def _move_without_switch(_motor: object, _position: float) -> None:
        pass

    monkeypatch.setattr(shutter_module, "DEFAULT_TIMEOUT", 0.1)
    monkeypatch.setattr(type(fast.motor), "set", _move_without_switch)
    with pytest.raises(TimeoutError, match="did not show open"):
        await fast.set(True)


def test_fast_shutter_rejects_overlapping_positions():
    with pytest.raises(ValueError, match="further apart"):
        FastShutter(open_position=0.0, closed_position=0.2, tolerance=0.5)


def test_ensure_open_actuates_fast_shutter(
    RE: RunEngine, fast: FastShutter, sleeps: list[float]
):
    RE(ensure_shutter_open(fast, allow_actuation=True))
    RE(ensure_shutter_closed(fast))
    puts = get_mock_put(fast.motor.user_setpoint).call_args_list
    assert [c.args[0] for c in puts] == [-20.0, 20.0]


def test_fast_shutter_sleeps_for_its_short_settle_time(
    RE: RunEngine, fast: FastShutter, sleeps: list[float]
):
    assert FastShutter.settle_time == 0.05
    RE(ensure_shutter_open(fast, allow_actuation=True))
    assert sleeps == [0.05]


def test_explicit_sleep_time_overrides_settle_time(
    RE: RunEngine, fast: FastShutter, sleeps: list[float]
):
    RE(ensure_shutter_open(fast, allow_actuation=True, sleep_time=1.0))
    assert sleeps == [1.0]


def test_mixed_shutters_sleep_for_the_slowest_actuated(
    RE: RunEngine, fast: FastShutter, shutter: Shutter, sleeps: list[float]
):
    RE(ensure_shutter_open([fast, shutter], allow_actuation=True))
    assert sleeps == [Shutter.settle_time]
