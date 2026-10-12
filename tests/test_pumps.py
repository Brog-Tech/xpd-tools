"""Offline EPICS source and pump-policy checks."""

import math

import pytest
from ophyd_async.core import init_devices, set_mock_value

from xpd_tools.pumps import (
    DDSSyringeAxis,
    DDSSyringeController,
    UltraSyringePump,
    rate_limits,
    rate_unit_converter,
    validate_pump_settings,
    vol_unit_converter,
)


@pytest.mark.asyncio
async def test_pump_sources_and_readables():
    async with init_devices(mock=True):
        controller = DDSSyringeController("XF:28IDC-ES:1{Pump:Syrng-DDS:1", name="DDS1")
        axis = DDSSyringeAxis(
            "XF:28IDC-ES:1{Pump:Syrng-DDS:1-Ax:A}", controller, name="DDS1_p1"
        )
        ultra = UltraSyringePump(
            "XF:28IDC-ES:1{Pump:Syrng-Ultra:1}:", name="Pump_Ultra1"
        )
    assert axis.controller is controller
    for pump, suffix in ((axis, "DDS:1-Ax:A}"), (ultra, "Ultra:1}:")):
        set_mock_value(pump.status, "Idle")
        set_mock_value(pump.read_infuse_rate, 100.0)
        set_mock_value(pump.read_infuse_rate_unit, "ul/min")
        readings = await pump.read()
        assert readings[pump.status.name]["value"] == "Idle"
        assert readings[pump.read_infuse_rate.name]["value"] == 100.0
        source = (await pump.status.describe())[pump.status.name]["source"]
        assert source.endswith(f"{suffix}STATUS")
        assert pump.infuse_rate.name in await pump.read_configuration()
        assert pump.pump_infuse.name not in readings
    assert (await controller.communication.describe())[controller.communication.name][
        "source"
    ].endswith("DDS:1}DISABLE")
    assert (await axis.read_min_infuse.describe())[axis.read_min_infuse.name][
        "source"
    ].endswith("DDS:1-Ax:A}IRATE:MIN:RBV")
    assert (await ultra.read_infuse_rate_unit.describe())[
        ultra.read_infuse_rate_unit.name
    ]["source"].endswith("Ultra:1}:IRATEUNITS:RBV")


def test_syringe_policy():
    pump = UltraSyringePump("mock:", name="ultra")
    assert rate_limits(pump, "steel", 50) == (118.038, 122.578)
    assert math.isclose(rate_unit_converter("ml/hr", "ul/min"), 1000 / 60)
    assert math.isclose(vol_unit_converter("nl", "ml"), 1e-6)
    validate_pump_settings(pump, "steel", 50, 30, 100)
    for rate in (-1, float("nan"), 200000):
        with pytest.raises(ValueError):
            validate_pump_settings(pump, "steel", 50, 30, rate)
    with pytest.raises(ValueError):
        validate_pump_settings(pump, "steel", 50, 51, 100)
