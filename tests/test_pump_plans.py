"""RunEngine pump behavior without contacting EPICS hardware."""

import pytest
from bluesky import plan_stubs as bps
from ophyd_async.core import (
    callback_on_mock_execute,
    callback_on_mock_put,
    init_devices,
    set_mock_value,
)

from xpd_tools.pump_plans import (
    configure_infusion,
    configure_withdrawal,
    pump_session,
    read_pump_metadata,
    start_infusion,
    start_withdrawal,
    stop_pumps,
    wait_for_equilibrium,
)
from xpd_tools.pumps import DDSSyringeAxis, DDSSyringeController, UltraSyringePump


@pytest.fixture
async def pumps():
    async with init_devices(mock=True):
        controller = DDSSyringeController("DDS:1", name="DDS1")
        first = DDSSyringeAxis("DDS:1-Ax:A}", controller, name="DDS1_p1")
        second = DDSSyringeAxis("DDS:1-Ax:B}", controller, name="DDS1_p2")
        ultra = UltraSyringePump("Ultra:1}:", name="Pump_Ultra1")
    for pump in (first, second, ultra):
        set_mock_value(pump.status, "Idle")
        for direction in ("infuse", "withdraw"):
            callback_on_mock_put(
                getattr(pump, f"{direction}_rate"),
                lambda value, pump=pump, direction=direction: set_mock_value(
                    getattr(pump, f"read_{direction}_rate"), value
                ),
            )
            callback_on_mock_put(
                getattr(pump, f"{direction}_rate_unit"),
                lambda value, pump=pump, direction=direction: set_mock_value(
                    getattr(pump, f"read_{direction}_rate_unit"), value
                ),
            )
        callback_on_mock_execute(
            pump.pump_infuse, lambda pump=pump: set_mock_value(pump.status, "Infusing")
        )
        callback_on_mock_execute(
            pump.pump_withdraw,
            lambda pump=pump: set_mock_value(pump.status, "Withdrawing"),
        )
        callback_on_mock_execute(
            pump.pump_stop, lambda pump=pump: set_mock_value(pump.status, "Idle")
        )
    for pump in (first, second):
        for direction in ("infuse", "withdraw"):
            set_mock_value(getattr(pump, f"read_min_{direction}"), 78.69)
            set_mock_value(getattr(pump, f"read_min_{direction}_unit"), "nl/min")
            set_mock_value(getattr(pump, f"read_max_{direction}"), 81.72)
            set_mock_value(getattr(pump, f"read_max_{direction}_unit"), "ml/min")
    return first, second, ultra


def test_configure_start_zero_and_metadata(RE, pumps):
    first, second, _ = pumps
    RE(
        configure_infusion(
            [first, second],
            [50, 50],
            ["30 ml", "30 ml"],
            ["100 ul/min", "0 ul/min"],
            ["steel", "steel"],
            [True, True],
        )
    )
    assert RE(bps.rd(first.infuse_rate)).plan_result == 100
    assert RE(bps.rd(first.infuse_rate_unit)).plan_result == "ul/min"
    assert RE(bps.rd(first.target_vol)).plan_result == 30
    RE(start_infusion([first, second], [100, "0 ul/min"]))
    assert RE(read_pump_metadata([first, second])).plan_result == {
        "pumps": ["DDS1_p1", "DDS1_p2"],
        "infuse_rate": [100, 0],
        "infuse_rate_unit": ["ul/min", ""],
        "pump_status": ["Infusing", "Idle"],
    }
    RE(stop_pumps([first, second]))
    assert RE(bps.rd(first.status)).plan_result == "Idle"


def test_preflight_rejects_wrong_syringe_and_limit(RE, pumps):
    first, second, _ = pumps
    set_mock_value(second.read_min_infuse, 35.21)
    with pytest.raises(ValueError, match="selected DDS syringe"):
        RE(
            configure_infusion(
                [first, second],
                [50, 50],
                ["30 ml"] * 2,
                [100, 100],
                ["steel"] * 2,
                [True] * 2,
            )
        )
    assert RE(bps.rd(first.infuse_rate)).plan_result == 0
    with pytest.raises(ValueError, match="outside syringe range"):
        RE(configure_infusion([first], [50], ["30 ml"], [90000], ["steel"], [True]))


def test_ultra_and_withdrawal(RE, pumps):
    first, _, ultra = pumps
    with pytest.warns(UserWarning, match="Ultra panel"):
        RE(configure_infusion([ultra], [50], ["30 ml"], [100], ["steel"], [True]))
    RE(configure_withdrawal([first], [50], ["30 ml"], [100], ["steel"], [True]))
    RE(start_withdrawal([first], [100]))
    assert RE(bps.rd(first.status)).plan_result == "Withdrawing"
    assert RE(bps.rd(first.read_withdraw_rate)).plan_result == 100
    RE(stop_pumps([first]))


def test_session_stops_on_success_failure_and_cleanup_failure(RE, pumps):
    first, second, _ = pumps

    def setup():
        yield from bps.null()

    def body():
        yield from start_infusion([first], [100])
        yield from bps.null()

    RE(pump_session(body, setup=setup(), pumps=[first]))
    assert RE(bps.rd(first.status)).plan_result == "Idle"

    def failing():
        yield from start_infusion([first, second], [100, 100])
        raise RuntimeError("acquisition failed")

    with pytest.raises(RuntimeError, match="acquisition failed"):
        RE(pump_session(failing, setup=setup(), pumps=[first, second]))
    assert RE(bps.rd(second.status)).plan_result == "Idle"
    tracked = []

    def failing_setup():
        tracked.append(first)
        yield from start_infusion([first], [100])
        raise RuntimeError("setup failed")

    with pytest.raises(RuntimeError, match="setup failed"):
        RE(pump_session(body, setup=failing_setup(), pumps=tracked))
    assert RE(bps.rd(first.status)).plan_result == "Idle"

    def failed_stop():
        raise RuntimeError("STOP failed")

    callback_on_mock_execute(first.pump_stop, failed_stop)
    with pytest.raises(ExceptionGroup, match="Pump stop failures"):
        RE(stop_pumps([first, second]))
    assert RE(bps.rd(second.status)).plan_result == "Idle"
    with pytest.raises(ExceptionGroup, match="Pump session and cleanup failures"):
        RE(pump_session(failing, setup=setup(), pumps=[first, second]))


def test_equilibrium_segment_flow(RE, pumps):
    first, second, _ = pumps
    set_mock_value(first.status, "Infusing")
    set_mock_value(first.read_infuse_rate, 100)
    set_mock_value(first.read_infuse_rate_unit, "ul/min")
    duration = RE(wait_for_equilibrium([["0 cm", first]], ratio=1)).plan_result
    assert duration == 0
    with pytest.raises(ValueError, match="no positive infusing flow"):
        RE(wait_for_equilibrium([["10 cm", second]], ratio=1))


def test_flow_session_reads_after_start_and_stops(RE, pumps):
    from pathlib import Path

    namespace = {}
    startup = (
        Path(__file__).resolve().parents[1]
        / "xpd-profile-collection/startup/97-xray_uvvis.py"
    )
    exec(compile(startup.read_text(), str(startup), "exec"), namespace)
    ultra = pumps[-1]
    observed = []

    def acquisition():
        observed.append((yield from read_pump_metadata([ultra])))
        yield from bps.null()

    with pytest.warns(UserWarning, match="Ultra panel"):
        RE(
            namespace["steady_state_flow"](
                acquisition,
                [ultra],
                [100],
                syringe_list=[50],
                target_vol_list=["30 ml"],
                set_target_list=[True],
                syringe_mater_list=["steel"],
                mixer_lengths_cm=[0],
                resident_t_ratio=0,
            )
        )
    assert observed[0]["pump_status"] == ["Infusing"]
    assert observed[0]["infuse_rate"] == [100]
    assert RE(bps.rd(ultra.status)).plan_result == "Idle"

    def fail_sleep(_):
        raise RuntimeError("wash interrupted")
        yield

    original_sleep = namespace["bps"].sleep
    namespace["bps"].sleep = fail_sleep
    try:
        with pytest.warns(UserWarning, match="Ultra panel"):
            with pytest.raises(RuntimeError, match="wash interrupted"):
                RE(
                    namespace["_wash_loop"](
                        [ultra],
                        [50],
                        [100],
                        1,
                        syringe_mater_list=["steel"],
                        target_vol_list=["30 ml"],
                        set_target_list=[True],
                        rate_unit="ul/min",
                    )
                )
    finally:
        namespace["bps"].sleep = original_sleep
    assert RE(bps.rd(ultra.status)).plan_result == "Idle"
