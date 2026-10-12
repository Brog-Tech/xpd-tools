"""Bluesky plans for syringe-pump configuration, operation and safe cleanup."""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable, Generator, Sequence

from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp
from ophyd_async.core import wait_for_value

from .pumps import (
    DDSSyringeAxis,
    UltraSyringePump,
    rate_limits,
    rate_unit_converter,
    validate_pump_settings,
    vol_unit_converter,
)

Pump = DDSSyringeAxis | UltraSyringePump


def _rate(value: float | str, unit: str) -> float:
    if isinstance(value, str):
        number, unit = value.split()
        value = float(number)
    return float(value) * rate_unit_converter(unit, "ul/min")


def _target(value: float | str) -> float:
    if isinstance(value, str):
        number, unit = value.split()
        return float(number) * vol_unit_converter(unit, "ml")
    return float(value)


def _count(pumps: Sequence[Pump], **parameters: Sequence) -> None:
    for name, values in parameters.items():
        if len(values) != len(pumps):
            raise ValueError(f"{name} has {len(values)} entries for {len(pumps)} pumps")


def _configure(
    pumps: Sequence[Pump],
    syringe_sizes_ml: Sequence[float],
    target_volumes: Sequence[float | str],
    rates: Sequence[float | str],
    materials: Sequence[str],
    set_targets: Sequence[bool],
    direction: str,
    rate_unit: str,
) -> Generator:
    _count(
        pumps,
        syringe_sizes_ml=syringe_sizes_ml,
        target_volumes=target_volumes,
        rates=rates,
        materials=materials,
        set_targets=set_targets,
    )
    normalized = []
    for pump, size, target, rate, material, set_target in zip(
        pumps,
        syringe_sizes_ml,
        target_volumes,
        rates,
        materials,
        set_targets,
        strict=True,
    ):
        rate_ul_min = _rate(rate, rate_unit)
        target_ml = _target(target) if set_target else None
        validate_pump_settings(pump, material, size, target_ml, rate_ul_min)
        normalized.append((pump, size, target_ml, rate_ul_min, material))

    # Range refresh/read is performed for *every* requested DDS axis before any
    # target or rate write; a wrong syringe on one axis cannot partly configure another.
    for pump, size, _, rate, material in normalized:
        if not rate:
            continue
        if isinstance(pump, DDSSyringeAxis):
            yield from bps.trigger(pump.set_infuse_range, wait=True)
            yield from bps.trigger(pump.set_withdraw_range, wait=True)
            selected = yield from bps.rd(pump.read_min_infuse)
            selected_unit = yield from bps.rd(pump.read_min_infuse_unit)
            expected, _ = rate_limits(pump, material, size)
            if not math.isclose(
                selected * rate_unit_converter(selected_unit, "nl/min"),
                expected,
                rel_tol=1e-3,
                abs_tol=0,
            ):
                raise ValueError(
                    f"{pump.name}: selected DDS syringe does not match "
                    f"{size} mL {material}"
                )
            lower = yield from bps.rd(getattr(pump, f"read_min_{direction}"))
            lower_unit = yield from bps.rd(getattr(pump, f"read_min_{direction}_unit"))
            upper = yield from bps.rd(getattr(pump, f"read_max_{direction}"))
            upper_unit = yield from bps.rd(getattr(pump, f"read_max_{direction}_unit"))
            lower *= rate_unit_converter(lower_unit, "ul/min")
            upper *= rate_unit_converter(upper_unit, "ul/min")
            if not (
                math.isfinite(lower)
                and math.isfinite(upper)
                and 0 < lower <= rate <= upper
            ):
                raise ValueError(f"{pump.name}: {direction} rate outside IOC limits")
        else:
            warnings.warn(
                f"{pump.name}: confirm {size} mL {material} syringe on Ultra panel",
                UserWarning,
                stacklevel=2,
            )

    for pump, _, target, rate, _ in normalized:
        if not rate:
            continue
        if target is not None:
            yield from bps.mv(pump.target_vol_unit, "ml")
            yield from bps.mv(pump.target_vol, target)
        yield from bps.mv(getattr(pump, f"{direction}_rate_unit"), "ul/min")
        yield from bps.mv(getattr(pump, f"{direction}_rate"), rate)


def configure_infusion(
    pumps,
    syringe_sizes_ml,
    target_volumes,
    rates,
    materials,
    set_targets,
    *,
    rate_unit="ul/min",
):
    """Validate every syringe and IOC limit before setting infusion parameters."""
    yield from _configure(
        pumps,
        syringe_sizes_ml,
        target_volumes,
        rates,
        materials,
        set_targets,
        "infuse",
        rate_unit,
    )


def configure_withdrawal(
    pumps,
    syringe_sizes_ml,
    target_volumes,
    rates,
    materials,
    set_targets,
    *,
    rate_unit="ul/min",
):
    """Validate every syringe and IOC limit before setting withdrawal parameters."""
    yield from _configure(
        pumps,
        syringe_sizes_ml,
        target_volumes,
        rates,
        materials,
        set_targets,
        "withdraw",
        rate_unit,
    )


def _start(pumps, rates, direction, rate_unit):
    _count(pumps, rates=rates)
    normalized = [_rate(rate, rate_unit) for rate in rates]
    if any(not math.isfinite(rate) or rate < 0 for rate in normalized):
        raise ValueError("Start rates must be finite and nonnegative")
    for pump, rate in zip(pumps, normalized, strict=True):
        if rate:
            yield from bps.trigger(getattr(pump, f"pump_{direction}"), wait=True)
            expected = "Infusing" if direction == "infuse" else "Withdrawing"
            yield from bps.wait_for(
                [
                    lambda pump=pump, expected=expected: wait_for_value(
                        pump.status, expected, timeout=10
                    )
                ]
            )


def start_infusion(pumps, rates, *, rate_unit="ul/min"):
    """Start pumps with nonzero rates, waiting for IOC status confirmation."""
    yield from _start(pumps, rates, "infuse", rate_unit)


def start_withdrawal(pumps, rates, *, rate_unit="ul/min"):
    """Start withdrawal for pumps with nonzero rates."""
    yield from _start(pumps, rates, "withdraw", rate_unit)


def stop_pumps(pumps):
    """Attempt STOP and status confirmation for every pump, even on failure."""
    failures = []
    for pump in pumps:
        try:
            yield from bps.trigger(pump.pump_stop, wait=True)
            yield from bps.wait_for(
                [
                    lambda pump=pump: wait_for_value(
                        pump.status,
                        lambda status: status not in ("Infusing", "Withdrawing"),
                        timeout=10,
                    )
                ]
            )
        except Exception as exc:
            failures.append(exc)
    if failures:
        raise ExceptionGroup("Pump stop failures", failures)


def enable_pumps(pumps, *, update=".5 second", clear=False):
    """Enable each DDS controller once; leave counter clearing opt-in."""
    controllers = set()
    for pump in pumps:
        if isinstance(pump, DDSSyringeAxis):
            controller = pump.controller
            if controller in controllers:
                continue
            controllers.add(controller)
            yield from bps.mv(controller.communication, "Enabled")
        else:
            yield from bps.mv(pump.communication, "Enabled")
            yield from bps.mv(pump.update_pump, update)
    if clear:
        for pump in pumps:
            yield from bps.trigger(pump.clear_infused, wait=True)
            yield from bps.trigger(pump.clear_withdrawn, wait=True)


def read_pump_metadata(pumps):
    """Return live readback metadata for run-start documents."""
    result = {"pumps": [], "infuse_rate": [], "infuse_rate_unit": [], "pump_status": []}
    for pump in pumps:
        result["pumps"].append(pump.name)
        result["infuse_rate"].append((yield from bps.rd(pump.read_infuse_rate)))
        result["infuse_rate_unit"].append(
            (yield from bps.rd(pump.read_infuse_rate_unit))
        )
        result["pump_status"].append((yield from bps.rd(pump.status)))
    return result


def wait_for_equilibrium(mixer_pump_list, *, ratio=1, tubing_ID_mm=1.016):  # noqa: N803
    """Wait cumulative residence times for successive tubing/flow segments."""
    if (
        not mixer_pump_list
        or not math.isfinite(ratio)
        or ratio < 0
        or tubing_ID_mm <= 0
    ):
        raise ValueError("Nonempty mixer and nonnegative residence geometry required")
    duration = 0.0
    lengths = {"mm": 1, "cm": 10, "m": 1000}
    for length_spec, *pumps in mixer_pump_list:
        length, unit = length_spec.split()
        length_mm = float(length) * lengths[unit]
        if not pumps or not math.isfinite(length_mm) or length_mm < 0:
            raise ValueError("Each mixer segment requires pumps and valid length")
        flow = 0.0
        for pump in pumps:
            if (yield from bps.rd(pump.status)) == "Infusing":
                rate = yield from bps.rd(pump.read_infuse_rate)
                unit = yield from bps.rd(pump.read_infuse_rate_unit)
                flow += rate * rate_unit_converter(unit, "ul/min")
        if not math.isfinite(flow) or flow <= 0:
            raise ValueError("Mixer segment has no positive infusing flow")
        duration += 60 * math.pi * (tubing_ID_mm / 2) ** 2 * length_mm / flow
    yield from bps.sleep(duration * ratio)
    return duration * ratio


def pump_session(
    body_factory: Callable[[], Generator], *, setup: Generator, pumps: list[Pump]
):
    """Finalize every potentially started pump, including on setup failure."""
    failure = None

    def body():
        nonlocal failure
        try:
            yield from setup
            return (yield from body_factory())
        except Exception as exc:
            failure = exc
            raise

    def cleanup():
        try:
            yield from stop_pumps(pumps)
        except Exception as exc:
            if failure is not None:
                raise ExceptionGroup(
                    "Pump session and cleanup failures", [failure, exc]
                ) from None
            raise

    return (yield from bpp.finalize_wrapper(body(), cleanup()))
