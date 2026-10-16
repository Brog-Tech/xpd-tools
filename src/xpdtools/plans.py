"""Acquisition plans for XPD beamline at NSLS-II."""

import logging

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
from ophyd_async.core import (
    DetectorTrigger,
    StandardFlyer,
    TriggerInfo,
)
from ophyd_async.epics.adcore import ADBaseIO, AreaDetector
from ophyd_async.fastcs.panda import HDFPanda as PandABox
from ophyd_async.fastcs.panda import PandaPcompDirection

from xpdtools.detectors.utils import get_detector_acq_times

from .flyers import (
    SingleAxisFlyscanController,
    SingleAxisFlyscanInfo,
    construct_fly_info_models,
)
from .motors import RotationMotor, get_encoder_counts_per_rev

logger = logging.getLogger(__name__)

#: Largest value a PandA position can hold. The negative limit is kept symmetric
#: so that negating the encoder value in CALC1 can't overflow.
PANDA_COUNTER_LIMIT = 2**31 - 1


def _fits_counter_range(
    flyer_info: SingleAxisFlyscanInfo, current_enc: int, margin: int
) -> bool:
    """Checsk whether the scan keeps the PandA counter clear of its 32 bit limits."""
    if flyer_info.time_based:
        extent = 1
    else:
        extent = (flyer_info.num_pulses - 1) * int(flyer_info.pulse_step) + int(
            flyer_info.pulse_width
        )
    sign = 1 if flyer_info.direction == PandaPcompDirection.POSITIVE else -1
    last = flyer_info.start + sign * extent
    return all(
        abs(count) + margin <= PANDA_COUNTER_LIMIT for count in (current_enc, last)
    )


def single_axis_flyscan(
    detectors: list[AreaDetector[ADBaseIO]],
    panda: PandABox,
    motor: RotationMotor,
    num_images: int = 1801,
    start: float = 0.0,
    stop: float = 180.0,
    stream_name: str = "tomo",
    acq_time_overhead: float = 0.001,
    time_based: bool = False,
):
    """Perform a single axis flyscan with the given detectors, PandABox, and motor.

    ``start`` and ``stop`` may be any motor positions, in either direction. The
    PandA must count the encoder continuously (``INENC1.RST_ON_Z`` off), and
    position compare 1 must read ``CALC1.OUT``, which increases with the motor.

    Parameters
    ----------
    detectors : list[AreaDetector[ADBaseIO]]
        list of area detectors to use in the scan
    panda : PandABox
        PandABox to use for triggering the detectors and motor
    motor : RotationMotor
        RotationMotor to use for the scan
    num_images : int, optional
        Number of images to acquire, by default 1801
    start : float, optional
        Start motor position of the scan, by default 0.0
    stop : float, optional
        Stop motor position of the scan, by default 180.0
    stream_name : str, optional
        Name of the data stream, by default "tomo"
    acq_time_overhead : float, optional
        Overhead time for acquisition for each frame in seconds, by default 0.001
    time_based : bool, default False
        If true, equally spaced in time triggers. Otherwise, equally spaced in
        position.
    """
    all_detectors = [*detectors, panda]

    # Construct ephemeral flyer for the single axis flyscan
    single_axis_panda_flyer = StandardFlyer(SingleAxisFlyscanController(panda))
    all_devices = [*all_detectors, single_axis_panda_flyer, motor]

    reset_on_z = yield from bps.rd(panda.inenc[1].rst_on_z)  # type: ignore
    if str(reset_on_z).strip().lower() in {"1", "true", "yes", "on"}:
        raise ValueError(
            f"{panda.name} encoder resets on the index mark (INENC1.RST_ON_Z is "
            f"{reset_on_z!r}), so triggers would be lost when the scan crosses it. "
            f"Turn RST_ON_Z off."
        )

    encoder_res = yield from bps.rd(motor.encoder_resolution)
    max_velocity = yield from bps.rd(motor.max_velocity)
    current_position = yield from bps.rd(motor.user_readback)
    current_enc = round((yield from bps.rd(panda.calc[1].out)))  # type: ignore

    acquisition_periods = yield from get_detector_acq_times(detectors)
    max_acq_period = max(acquisition_periods)

    def fly_info_models(current_enc_position: int):
        return construct_fly_info_models(
            num_pulses=num_images,
            max_exposure_time=max_acq_period,
            start_position=start,
            stop_position=stop,
            encoder_resolution=encoder_res,
            max_motor_velocity=max_velocity,
            current_position=current_position,
            current_enc_position=current_enc_position,
            acq_time_overhead=acq_time_overhead,
            time_based=time_based,
        )

    flyer_info, motor_info = fly_info_models(current_enc)

    # Keep a revolution clear of the limit for the run-up and run-down
    margin = get_encoder_counts_per_rev(encoder_res)
    if not _fits_counter_range(flyer_info, current_enc, margin):
        # To refuse the scan instead of re-zeroing the counter, use this instead:
        # raise ValueError(
        #     f"PandA counter is {current_enc}, and the scan would take it past the "
        #     f"32 bit limit where it overflows. Re-zero the encoder counter "
        #     f"(INENC1.SETP) and try again."
        # )
        logger.warning(
            "PandA counter is %d, and the scan would take it past the 32 bit "
            "limit where it overflows. Re-zeroing the encoder counter.",
            current_enc,
        )
        yield from bps.mv(panda.inenc[1].setp, 0)  # type: ignore
        # CALC1 = -INENC1, so the counter now reads 0 at the current position
        current_enc = 0
        flyer_info, motor_info = fly_info_models(current_enc)
        if not _fits_counter_range(flyer_info, current_enc, margin):
            raise ValueError(
                f"Scan from {start} to {stop} is too long for the 32 bit PandA counter."
            )

    det_trigger_info = TriggerInfo(
        number_of_events=num_images,
        trigger=DetectorTrigger.EXTERNAL_EDGE,
    )

    panda_trigger_info = TriggerInfo(
        number_of_events=num_images,
        trigger=DetectorTrigger.EXTERNAL_LEVEL,
    )

    _md = {
        "detectors": [det.name for det in detectors],
        "num_points": num_images,
        "plan_name": "single_axis_flyscan",
        "hints": {},
    }

    @bpp.stage_decorator(all_detectors)
    @bpp.run_decorator(md=_md)
    def _body():
        # TODO: Come up with a way to set a timeout automatically based on the
        # motor move to start position time.
        yield from bps.prepare(motor, motor_info, wait=True)

        # Configure position compare and arm the detectors only once the motor is
        # at its run-up position, so no frames are captured before kickoff.
        yield from bps.prepare(single_axis_panda_flyer, flyer_info, group="arm")
        for det in detectors:
            yield from bps.prepare(det, det_trigger_info, group="arm")
        yield from bps.wait(group="arm")

        # Preparing the PandA arms PCAP, which enables position compare, so it
        # must come after position compare is configured.
        yield from bps.prepare(panda, panda_trigger_info, wait=True)

        yield from bps.declare_stream(*all_detectors, name=stream_name)

        yield from bps.kickoff_all(*all_devices, wait=True)

        flush_period = max(1, max_acq_period)
        yield from bps.collect_while_completing(
            all_devices,
            all_detectors,
            flush_period=flush_period,
            stream_name=stream_name,
        )

    def _cleanup():
        yield from bps.abs_set(motor.motor_stop, 1)

    return (yield from bpp.finalize_wrapper(_body(), _cleanup()))
