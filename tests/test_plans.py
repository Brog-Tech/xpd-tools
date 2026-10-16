"""Tests for the single_axis_flyscan plan."""

import asyncio
from pathlib import Path

import pytest
from bluesky.run_engine import RunEngine
from ophyd_async.core import (
    Device,
    DeviceVector,
    SignalR,
    SignalRW,
    StaticPathProvider,
    UUIDFilenameProvider,
    callback_on_mock_put,
    init_devices,
    set_mock_value,
)
from ophyd_async.core._mock_signal_utils import (  # noqa: PLC2701
    _get_mock_signal_backend,
)
from ophyd_async.epics.adcore import ADWriterFactory, NDFileHDF5IO
from ophyd_async.fastcs.panda import (
    DatasetTable,
    HDFPanda,
    PandaHdf5DatasetType,
    PandaPcompDirection,
)
from panda_sim import ContinuousCounter, PcompSim

from xpdtools.detectors.pilatus4 import Pilatus4Detector
from xpdtools.motors import RotationMotor
from xpdtools.plans import single_axis_flyscan

NUM_IMAGES = 11
SCAN_START = 0.0
SCAN_STOP = 180.0
ENCODER_RESOLUTION = 360 / 400_000


class CalcBlock(Device):
    out: SignalR[float]
    out_dataset: SignalRW[str]
    out_units: SignalRW[str]
    out_scale: SignalRW[float]
    out_offset: SignalRW[float]


class InencBlock(Device):
    setp: SignalRW[int]
    rst_on_z: SignalRW[str]


class XpdPanda(HDFPanda):
    """HDFPanda with the blocks that are only introspected on the real device."""

    calc: DeviceVector[CalcBlock]
    inenc: DeviceVector[InencBlock]


class SimulatedPanda:
    """Drive mock PandA, motor and detector signals from a PandA simulation.

    The PandA counter follows every motor setpoint write, and a simulated
    position compare block produces captures when the counter passes its trigger
    positions while PCAP is armed.
    """

    def __init__(self, devices, initial_position: float, initial_counter: int):
        pilatus, panda, motor = devices
        self.panda = panda
        self.position = panda.calc[1].out
        self.hdf = pilatus.get_plugin("hdf", NDFileHDF5IO)
        self.counter = ContinuousCounter(
            ENCODER_RESOLUTION, initial_position, initial_counter
        )
        self.pcomp = PcompSim()
        self.pulses_per_pcomp_pulse = 1
        self.captured = 0
        self.setpoint_writes: list[int] = []

        set_mock_value(motor.user_readback, initial_position)
        set_mock_value(self.position, self.counter.value)
        set_mock_value(panda.inenc[1].rst_on_z, "0")
        set_mock_value(panda.data.directory_exists, 1)
        set_mock_value(
            panda.data.datasets,
            DatasetTable(name=["enc1"], dtype=[PandaHdf5DatasetType.FLOAT_64]),
        )
        set_mock_value(panda.pcap.active, False)
        set_mock_value(panda.pcomp[1].active, False)
        set_mock_value(panda.data.num_captured, 0)

        pcomp = panda.pcomp[1]
        callback_on_mock_put(pcomp.start, lambda v: setattr(self.pcomp, "start", v))
        callback_on_mock_put(pcomp.step, lambda v: setattr(self.pcomp, "step", v))
        callback_on_mock_put(pcomp.width, lambda v: setattr(self.pcomp, "width", v))
        callback_on_mock_put(pcomp.pulses, lambda v: setattr(self.pcomp, "pulses", v))
        callback_on_mock_put(
            pcomp.dir,
            lambda v: setattr(
                self.pcomp, "direction", 1 if v == PandaPcompDirection.POSITIVE else -1
            ),
        )
        callback_on_mock_put(
            panda.pulse[1].pulses,
            lambda v: setattr(self, "pulses_per_pcomp_pulse", v),
        )
        callback_on_mock_put(panda.pcap.arm, self._on_arm)
        self.position_scaling: dict[str, float] = {}
        for key in ("out_scale", "out_offset"):
            callback_on_mock_put(
                getattr(panda.calc[1], key),
                lambda v, key=key: self.position_scaling.__setitem__(key, v),
            )
        callback_on_mock_put(panda.inenc[1].setp, self._on_setpoint)

        backend = _get_mock_signal_backend(motor.user_setpoint)
        self._original_motor_cb = backend._mock_put_callback
        callback_on_mock_put(motor.user_setpoint, self._on_motor_setpoint)

    def _on_setpoint(self, value: int):
        self.setpoint_writes.append(value)
        self.counter.set_counter(value)
        set_mock_value(self.position, self.counter.value)

    def _on_arm(self, value: bool):
        set_mock_value(self.panda.pcap.active, value)
        # PCOMP1.ENABLE is wired to PCAP.ACTIVE
        if value:
            self.pcomp.enable(self.counter.value)
        else:
            self.pcomp.disable()
        set_mock_value(self.panda.pcomp[1].active, value)

    def _on_motor_setpoint(self, value: float):
        new_pulses = self.pcomp.observe(self.counter.move_to(value))
        set_mock_value(self.position, self.counter.value)
        if new_pulses:
            asyncio.ensure_future(
                self._simulate_captures(new_pulses * self.pulses_per_pcomp_pulse)
            )
        if self._original_motor_cb:
            return self._original_motor_cb(value)

    async def _simulate_captures(self, captures: int):
        for _ in range(captures):
            await asyncio.sleep(0.005)
            self.captured += 1
            set_mock_value(self.panda.data.num_captured, self.captured)
            set_mock_value(self.hdf.num_captured, self.captured)
        if self.pcomp.done:
            set_mock_value(self.panda.pcomp[1].active, False)


@pytest.fixture
async def devices(tmp_path: Path):
    path_provider = StaticPathProvider(
        UUIDFilenameProvider(), tmp_path, create_dir_depth=-2
    )
    async with init_devices(mock=True):
        panda = XpdPanda("PANDA:", path_provider)
        motor = RotationMotor("MOT:", name="rot_motor")
        pilatus1 = Pilatus4Detector(
            "DET1:",
            ADWriterFactory.hdf(path_provider),
            name="pilatus1",
        )
    # Set mock values for fast test execution
    set_mock_value(motor.encoder_resolution, ENCODER_RESOLUTION)
    set_mock_value(motor.max_velocity, 1000.0)
    # Gives a 5 degree run-up at 1000 deg/s
    set_mock_value(motor.acceleration_time, 0.01)
    set_mock_value(pilatus1.driver.acquire_time, 0.001)
    set_mock_value(pilatus1.driver.acquire_period, 0.002)

    return pilatus1, panda, motor


def _run_flyscan(RE, devices, start, stop, time_based=False):
    pilatus1, panda, motor = devices
    docs: dict[str, list] = {}

    def collect_doc(name, doc):
        docs.setdefault(name, []).append(doc)

    RE(
        single_axis_flyscan(
            detectors=[pilatus1],
            panda=panda,
            motor=motor,
            num_images=NUM_IMAGES,
            start=start,
            stop=stop,
            time_based=time_based,
        ),
        collect_doc,
    )
    return docs


@pytest.mark.parametrize(
    "time_based",
    [True, False],
)
def test_single_axis_flyscan(RE: RunEngine, devices, time_based: bool):
    """Test that single_axis_flyscan runs to completion and emits correct documents."""
    sim = SimulatedPanda(devices, initial_position=0.0, initial_counter=0)

    docs = _run_flyscan(RE, devices, SCAN_START, SCAN_STOP, time_based)
    assert sim.pcomp.errors == []

    # --- Verify core document structure ---
    assert "start" in docs
    assert "stop" in docs
    assert len(docs["start"]) == 1
    assert len(docs["stop"]) == 1

    start_doc = docs["start"][0]
    assert start_doc["plan_name"] == "single_axis_flyscan"
    assert start_doc["num_points"] == NUM_IMAGES
    assert start_doc["detectors"] == ["pilatus1"]

    stop_doc = docs["stop"][0]
    assert stop_doc["exit_status"] == "success"
    assert stop_doc["run_start"] == start_doc["uid"]

    # --- Verify descriptor ---
    assert "descriptor" in docs
    descriptors = docs["descriptor"]
    assert len(descriptors) >= 1
    # The "tomo" stream descriptor should contain data keys for pilatus1 and enc1
    tomo_descriptor = next(d for d in descriptors if d.get("name") == "tomo")
    assert "pilatus1" in tomo_descriptor["data_keys"]
    assert "enc1" in tomo_descriptor["data_keys"]

    # Pilatus data key should be external stream with correct shape
    pilatus_dk = tomo_descriptor["data_keys"]["pilatus1"]
    assert pilatus_dk["external"] == "STREAM:"
    assert pilatus_dk["shape"] == [1, 720, 1280]

    # PandA enc1 data key should be external stream (scalar)
    enc1_dk = tomo_descriptor["data_keys"]["enc1"]
    assert enc1_dk["external"] == "STREAM:"

    # --- Verify stream_resource documents ---
    assert "stream_resource" in docs
    stream_resources = docs["stream_resource"]

    # Should have at least 2: one for pilatus HDF, one for panda HDF
    pilatus_sr = next(sr for sr in stream_resources if sr["data_key"] == "pilatus1")
    panda_sr = next(sr for sr in stream_resources if sr["data_key"] == "enc1")

    # Pilatus stream resource checks
    assert pilatus_sr["mimetype"] == "application/x-hdf5"
    assert pilatus_sr["uri"].endswith(".h5")
    assert pilatus_sr["parameters"]["dataset"] == "/entry/data/data"
    assert "chunk_shape" in pilatus_sr["parameters"]

    # PandA stream resource checks
    assert panda_sr["mimetype"] == "application/x-hdf5"
    assert panda_sr["uri"].endswith(".h5")
    assert panda_sr["parameters"]["dataset"] == "/enc1"
    assert panda_sr["parameters"]["chunk_shape"] == (1024,)

    # --- Verify stream_datum documents ---
    assert "stream_datum" in docs
    stream_datums = docs["stream_datum"]

    # Filter datums by stream_resource uid
    pilatus_datums = [
        sd for sd in stream_datums if sd["stream_resource"] == pilatus_sr["uid"]
    ]
    panda_datums = [
        sd for sd in stream_datums if sd["stream_resource"] == panda_sr["uid"]
    ]

    assert len(pilatus_datums) >= 1
    assert len(panda_datums) >= 1

    # Verify indices cover all frames (start=0, stop=NUM_IMAGES)
    pilatus_last = pilatus_datums[-1]
    assert pilatus_last["indices"]["stop"] == NUM_IMAGES

    panda_last = panda_datums[-1]
    assert panda_last["indices"]["stop"] == NUM_IMAGES

    # Verify seq_nums are present and valid
    for datum in stream_datums:
        assert "seq_nums" in datum
        assert datum["seq_nums"]["start"] >= 0
        assert datum["seq_nums"]["stop"] > datum["seq_nums"]["start"]


@pytest.mark.parametrize(
    "initial_position, initial_counter, start, stop",
    [
        # The reported failure: back to 1 deg after a scan ended past 361 deg
        (366.0, 0, 1.0, 361.0),
        # Full revolution, starting next to the old Z mark
        (100.0, 0, 0.0, 360.0),
        # More than one revolution, negative direction
        (0.0, 0, 710.0, -10.0),
        # Multi-revolution motor positions, far from the current position
        (946.0, 0, -725.0, -365.0),
        # Arbitrary counter offset, unrelated to the motor position
        (0.0, -1_234_567_890, 90.0, 0.0),
    ],
)
def test_single_axis_flyscan_any_angle(
    RE: RunEngine, devices, initial_position, initial_counter, start, stop
):
    """All triggers are produced wherever the scan is and whatever the counter."""
    sim = SimulatedPanda(devices, initial_position, initial_counter)

    docs = _run_flyscan(RE, devices, start, stop)

    assert sim.pcomp.errors == []
    assert sim.pcomp.fired == NUM_IMAGES
    assert sim.setpoint_writes == []
    assert docs["stop"][0]["exit_status"] == "success"
    # The captured angle is the motor position at each trigger
    scale, offset = (
        sim.position_scaling["out_scale"],
        sim.position_scaling["out_offset"],
    )
    assert sim.pcomp.start * scale + offset == pytest.approx(start, abs=1e-3)


@pytest.mark.parametrize(
    "initial_counter, start, stop",
    [
        # Scan would run the counter through the 32 bit overflow
        (2**31 - 300_000, 0.0, 360.0),
    ],
)
def test_single_axis_flyscan_rezeroes_counter_near_overflow(
    RE: RunEngine, devices, initial_counter, start, stop
):
    sim = SimulatedPanda(devices, 100.0, initial_counter)

    docs = _run_flyscan(RE, devices, start, stop)

    assert sim.setpoint_writes == [0]
    assert sim.pcomp.errors == []
    assert sim.pcomp.fired == NUM_IMAGES
    assert docs["stop"][0]["exit_status"] == "success"


@pytest.mark.parametrize("value", ["1", "Yes", True, 1])
def test_single_axis_flyscan_refuses_reset_on_z(RE: RunEngine, devices, value):
    pilatus1, panda, motor = devices
    sim = SimulatedPanda(devices, 100.0, 0)
    set_mock_value(panda.inenc[1].rst_on_z, str(value))

    with pytest.raises(ValueError, match="RST_ON_Z"):
        _run_flyscan(RE, devices, 0.0, 360.0)
    # Fails before moving the motor
    assert sim.counter.position == 100.0
