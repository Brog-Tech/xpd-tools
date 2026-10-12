"""EPICS syringe-pump interfaces and syringe/rate policy."""

from __future__ import annotations

import math
import weakref
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated as A

from ophyd_async.core import SignalR, SignalRW, StandardReadable, TriggerableCommand
from ophyd_async.core import StandardReadableFormat as Format
from ophyd_async.epics.core import EpicsDevice, PvSuffix


class DDSSyringeController(EpicsDevice):
    """Shared DDS controller; an axis must not own or disable its sibling."""

    communication: A[SignalRW[str], PvSuffix("}DISABLE")]
    update_pump: A[SignalRW[str], PvSuffix("}UPDATE.SCAN")]
    set_condition: A[SignalRW[str], PvSuffix("}CONDITION")]
    show_condition: A[SignalR[str], PvSuffix("}CONDITION:RBV")]


class DDSSyringeAxis(StandardReadable, EpicsDevice):
    """One DDS axis; controller ownership remains with the startup profile."""

    status: A[SignalR[str], PvSuffix("STATUS"), Format.UNCACHED_SIGNAL]
    pump_infuse: A[TriggerableCommand, PvSuffix("IRUN")]
    pump_withdraw: A[TriggerableCommand, PvSuffix("WRUN")]
    pump_stop: A[TriggerableCommand, PvSuffix("STOP")]
    target_vol: A[SignalRW[float], PvSuffix("TVOLUME"), Format.CONFIG_SIGNAL]
    target_vol_unit: A[SignalRW[str], PvSuffix("TVOLUME:EGU"), Format.CONFIG_SIGNAL]
    read_target_vol: A[SignalR[float], PvSuffix("TVOLUME:RBV"), Format.UNCACHED_SIGNAL]
    read_target_vol_unit: A[
        SignalR[str], PvSuffix("TVOLUME:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    clear_infused: A[TriggerableCommand, PvSuffix("CIVOLUME")]
    clear_withdrawn: A[TriggerableCommand, PvSuffix("CWVOLUME")]
    read_infused: A[SignalR[float], PvSuffix("IVOLUME:RBV"), Format.UNCACHED_SIGNAL]
    read_withdrawn: A[SignalR[float], PvSuffix("WVOLUME:RBV"), Format.UNCACHED_SIGNAL]
    read_infused_unit: A[
        SignalR[str], PvSuffix("IVOLUME:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    read_withdrawn_unit: A[
        SignalR[str], PvSuffix("WVOLUME:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    infuse_rate: A[SignalRW[float], PvSuffix("IRATE"), Format.CONFIG_SIGNAL]
    infuse_rate_unit: A[SignalRW[str], PvSuffix("IRATE:EGU"), Format.CONFIG_SIGNAL]
    withdraw_rate: A[SignalRW[float], PvSuffix("WRATE"), Format.CONFIG_SIGNAL]
    withdraw_rate_unit: A[SignalRW[str], PvSuffix("WRATE:EGU"), Format.CONFIG_SIGNAL]
    read_infuse_rate: A[
        SignalR[float], PvSuffix("IRATE:RBV"), Format.HINTED_UNCACHED_SIGNAL
    ]
    read_infuse_rate_unit: A[
        SignalR[str], PvSuffix("IRATE:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    read_withdraw_rate: A[SignalR[float], PvSuffix("WRATE:RBV"), Format.UNCACHED_SIGNAL]
    read_withdraw_rate_unit: A[
        SignalR[str], PvSuffix("WRATE:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    max_infuse: A[SignalRW[float], PvSuffix("IRATE:MAX"), Format.CONFIG_SIGNAL]
    max_withdraw: A[SignalRW[float], PvSuffix("WRATE:MAX"), Format.CONFIG_SIGNAL]
    min_infuse: A[SignalRW[float], PvSuffix("IRATE:MIN"), Format.CONFIG_SIGNAL]
    min_withdraw: A[SignalRW[float], PvSuffix("WRATE:MIN"), Format.CONFIG_SIGNAL]
    set_infuse_range: A[TriggerableCommand, PvSuffix("IRATE:LIM.PROC")]
    set_withdraw_range: A[TriggerableCommand, PvSuffix("WRATE:LIM.PROC")]
    read_max_infuse: A[
        SignalR[float], PvSuffix("IRATE:MAX:RBV"), Format.UNCACHED_SIGNAL
    ]
    read_max_infuse_unit: A[
        SignalR[str], PvSuffix("IRATE:MAX:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    read_max_withdraw: A[
        SignalR[float], PvSuffix("WRATE:MAX:RBV"), Format.UNCACHED_SIGNAL
    ]
    read_max_withdraw_unit: A[
        SignalR[str], PvSuffix("WRATE:MAX:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    read_min_infuse: A[
        SignalR[float], PvSuffix("IRATE:MIN:RBV"), Format.UNCACHED_SIGNAL
    ]
    read_min_infuse_unit: A[
        SignalR[str], PvSuffix("IRATE:MIN:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    read_min_withdraw: A[
        SignalR[float], PvSuffix("WRATE:MIN:RBV"), Format.UNCACHED_SIGNAL
    ]
    read_min_withdraw_unit: A[
        SignalR[str], PvSuffix("WRATE:MIN:RBV:EGU"), Format.UNCACHED_SIGNAL
    ]
    LIM: A[SignalRW[str], PvSuffix("IRATE-LIM:ENABLE"), Format.CONFIG_SIGNAL]
    target_time: A[SignalRW[float], PvSuffix("TTIME"), Format.CONFIG_SIGNAL]
    read_target_time: A[SignalR[float], PvSuffix("TTIME:RBV"), Format.UNCACHED_SIGNAL]
    force: A[SignalRW[float], PvSuffix("FORCE"), Format.CONFIG_SIGNAL]
    read_force: A[SignalR[float], PvSuffix("FORCE:RBV"), Format.UNCACHED_SIGNAL]
    read_force_unit: A[SignalR[str], PvSuffix("FORCE:RBV:EGU"), Format.UNCACHED_SIGNAL]

    def __init__(self, prefix: str, controller: DDSSyringeController, name: str = ""):
        self._controller = weakref.ref(controller)
        super().__init__(prefix, name=name)

    @property
    def controller(self) -> DDSSyringeController:
        """The separately connected, shared DDS controller."""
        controller = self._controller()
        if controller is None:
            raise RuntimeError("DDS controller has been released")
        return controller


class UltraSyringePump(StandardReadable, EpicsDevice):
    """Ultra syringe pump; no IOC syringe-size feedback is available."""

    status: A[SignalR[str], PvSuffix("STATUS"), Format.UNCACHED_SIGNAL]
    communication: A[SignalRW[str], PvSuffix("DISABLE"), Format.CONFIG_SIGNAL]
    update_pump: A[SignalRW[str], PvSuffix("UPDATE.SCAN"), Format.CONFIG_SIGNAL]
    pump_infuse: A[TriggerableCommand, PvSuffix("IRUN")]
    pump_withdraw: A[TriggerableCommand, PvSuffix("WRUN")]
    pump_stop: A[TriggerableCommand, PvSuffix("STOP")]
    target_vol: A[SignalRW[float], PvSuffix("TVOLUME"), Format.CONFIG_SIGNAL]
    target_vol_unit: A[SignalRW[str], PvSuffix("TVOLUMEUNITS"), Format.CONFIG_SIGNAL]
    read_target_vol: A[SignalR[float], PvSuffix("TVOLUME:RBV"), Format.UNCACHED_SIGNAL]
    read_target_vol_unit: A[
        SignalR[str], PvSuffix("TVOLUMEUNITS:RBV"), Format.UNCACHED_SIGNAL
    ]
    clear_infused: A[TriggerableCommand, PvSuffix("CLEARINFUSED")]
    clear_withdrawn: A[TriggerableCommand, PvSuffix("CLEARWITHDRAWN")]
    read_infused: A[SignalR[float], PvSuffix("IVOLUME:RBV"), Format.UNCACHED_SIGNAL]
    read_withdrawn: A[SignalR[float], PvSuffix("WVOLUME:RBV"), Format.UNCACHED_SIGNAL]
    read_infused_unit: A[
        SignalR[str], PvSuffix("IVOLUMEUNITS:RBV"), Format.UNCACHED_SIGNAL
    ]
    read_withdrawn_unit: A[
        SignalR[str], PvSuffix("WVOLUMEUNITS:RBV"), Format.UNCACHED_SIGNAL
    ]
    infuse_rate: A[SignalRW[float], PvSuffix("IRATE"), Format.CONFIG_SIGNAL]
    infuse_rate_unit: A[SignalRW[str], PvSuffix("IRATEUNITS"), Format.CONFIG_SIGNAL]
    withdraw_rate: A[SignalRW[float], PvSuffix("WRATE"), Format.CONFIG_SIGNAL]
    withdraw_rate_unit: A[SignalRW[str], PvSuffix("WRATEUNITS"), Format.CONFIG_SIGNAL]
    read_infuse_rate: A[
        SignalR[float], PvSuffix("IRATE:RBV"), Format.HINTED_UNCACHED_SIGNAL
    ]
    read_infuse_rate_unit: A[
        SignalR[str], PvSuffix("IRATEUNITS:RBV"), Format.UNCACHED_SIGNAL
    ]
    read_withdraw_rate: A[SignalR[float], PvSuffix("WRATE:RBV"), Format.UNCACHED_SIGNAL]
    read_withdraw_rate_unit: A[
        SignalR[str], PvSuffix("WRATEUNITS:RBV"), Format.UNCACHED_SIGNAL
    ]
    target_time: A[SignalRW[float], PvSuffix("TTIME"), Format.CONFIG_SIGNAL]
    read_target_time: A[SignalR[float], PvSuffix("TTIME:RBV"), Format.UNCACHED_SIGNAL]
    force: A[SignalRW[float], PvSuffix("FORCE"), Format.CONFIG_SIGNAL]
    read_force: A[SignalR[float], PvSuffix("FORCE:RBV"), Format.UNCACHED_SIGNAL]
    diameter: A[SignalR[float], PvSuffix("DIAMETER"), Format.CONFIG_SIGNAL]


_DDS_RANGES: Mapping[str, Mapping[float, tuple[float, float]]] = MappingProxyType(
    {
        "steel": MappingProxyType(
            {
                2.5: (2.264, 2.351),
                8: (8.728, 9.064),
                20: (35.21, 36.56),
                50: (78.69, 81.72),
            }
        ),
        "glass_H1000": MappingProxyType(
            {
                1: (2.043, 2.121),
                1.25: (2.553, 2.651),
                2.5: (5.106, 5.302),
                5: (10.21, 10.6),
                10: (20.41, 21.2),
                25: (51.04, 53),
                50: (102.1, 106),
            }
        ),
        "plastic_BD": MappingProxyType(
            {
                1: (2.124, 2.206),
                3: (7.09, 7.363),
                5: (13.83, 14.36),
                10: (20.02, 20.79),
                20: (34.91, 36.26),
                30: (44.84, 46.57),
                50: (68.04, 70.66),
            }
        ),
    }
)
_ULTRA_RANGES: Mapping[str, Mapping[float, tuple[float, float]]] = MappingProxyType(
    {
        "steel": MappingProxyType(
            {
                2.5: (3.39588, 3.5265),
                8: (13.0924, 13.596),
                20: (52.8105, 54.8417),
                50: (118.038, 122.578),
                100: (175.769, 182.529),
            }
        ),
        "glass_H1000": MappingProxyType(
            {
                1: (3.06414, 3.18204),
                1.25: (3.82884, 3.97616),
                2.5: (7.65858, 7.95317),
                5: (15.3096, 15.8985),
                10: (30.6218, 31.7996),
                25: (76.5582, 79.5028),
                50: (153.111, 159.0),
                100: (153.111, 159.0),
            }
        ),
        "plastic_BD": MappingProxyType(
            {
                1: (3.18636, 3.30896),
                3: (10.6358, 11.0449),
                5: (20.7422, 21.5401),
                10: (30.036, 31.1913),
                20: (52.3697, 54.384),
                30: (67.2661, 69.8532),
                50: (102.061, 105.986),
                60: (102.061, 105.986),
            }
        ),
    }
)

_VOLUME_ML = MappingProxyType({"pl": 1e-9, "nl": 1e-6, "ul": 1e-3, "ml": 1.0})
_TIME_MIN = MappingProxyType({"sec": 1 / 60, "min": 1.0, "hr": 60.0})


def vol_unit_converter(v0: str, v1: str) -> float:
    """Return the factor converting a volume from v0 to v1."""
    return _VOLUME_ML[v0] / _VOLUME_ML[v1]


def rate_unit_converter(r0: str, r1: str) -> float:
    """Return the factor converting a volume/time rate from r0 to r1."""
    v0, t0 = r0.split("/")
    v1, t1 = r1.split("/")
    return vol_unit_converter(v0, v1) * _TIME_MIN[t1] / _TIME_MIN[t0]


def rate_limits(
    pump: DDSSyringeAxis | UltraSyringePump,
    syringe_material: str,
    syringe_size_ml: float,
) -> tuple[float, float]:
    """Return tabulated (minimum nl/min, maximum ml/min) for a syringe."""
    table = _DDS_RANGES if isinstance(pump, DDSSyringeAxis) else _ULTRA_RANGES
    try:
        return table[syringe_material][syringe_size_ml]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported {syringe_material} syringe size {syringe_size_ml} mL"
        ) from exc


def validate_pump_settings(
    pump: DDSSyringeAxis | UltraSyringePump,
    syringe_material: str,
    syringe_size_ml: float,
    target_ml: float | None,
    rate_ul_min: float,
) -> None:
    """Reject settings beyond the syringe's documented theoretical range."""
    minimum, maximum = rate_limits(pump, syringe_material, syringe_size_ml)
    if target_ml is not None and (
        not math.isfinite(target_ml) or target_ml < 0 or target_ml > syringe_size_ml
    ):
        raise ValueError(
            f"Target {target_ml} mL exceeds syringe capacity {syringe_size_ml} mL"
        )
    if not math.isfinite(rate_ul_min) or rate_ul_min < 0:
        raise ValueError(f"Invalid pump rate {rate_ul_min} ul/min")
    if rate_ul_min and not (
        minimum * rate_unit_converter("nl/min", "ul/min")
        <= rate_ul_min
        <= maximum * rate_unit_converter("ml/min", "ul/min")
    ):
        raise ValueError(f"Rate {rate_ul_min} ul/min outside syringe range")
