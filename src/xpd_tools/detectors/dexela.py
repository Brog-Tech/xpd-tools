"""Dexela detector interface for XPD beamline at NSLS-II."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated as A

from ophyd_async.core import (
    DetectorTriggerLogic,
    DeviceMock,
    SignalDict,
    SignalR,
    SignalRW,
    StandardReadable,
    StrictEnum,
    SupersetEnum,
    callback_on_mock_put,
    default_mock_class,
    set_mock_put_proceeds,
    set_mock_value,
)
from ophyd_async.core import (
    StandardReadableFormat as Format,
)
from ophyd_async.epics.adcore import (
    ADAcquireLogic,
    ADBaseDataType,
    ADBaseIO,
    ADWriterFactory,
    AreaDetector,
    NDFileHDF5IO,
    NDPluginBaseIO,
    prepare_exposures,
    trigger_info_from_driver,
)
from ophyd_async.epics.core import (
    PvSuffix,
)

# Approximate frame readout time of an unbinned Dexela panel (~26 frames/s).
# See https://areadetector.github.io/areaDetector/ADDexela/ADDexela.html#performance-measurements
DEXELA_READOUT_TIME = 0.04


class DexelaTriggerMode(StrictEnum):
    """Trigger modes for the Dexela detector.

    See https://areadetector.github.io/areaDetector/ADDexela/ADDexela.html#implementation-of-standard-driver-parameters
    """

    INTERNAL_FREE_RUN = "Int. Free Run"
    INTERNAL_FIXED_RATE = "Int. Fixed Rate"
    INTERNAL_SOFTWARE = "Int. Software"
    EXTERNAL_EDGE_SINGLE = "Ext. Edge Single"
    EXTERNAL_EDGE_MULTI = "Ext. Edge Multi"
    EXTERNAL_BULB = "Ext. Bulb"


class DexelaBinningMode(SupersetEnum):
    """Binning modes for the Dexela detector.

    The driver only advertises the modes supported by the connected detector.

    See https://areadetector.github.io/areaDetector/ADDexela/ADDexela.html#dexela-specific-parameters
    """

    UNBINNED = "Unbinned"
    X1_Y2 = "1H x 2V"
    X1_Y4 = "1H x 4V"
    X2_Y1 = "2H x 1V"
    X2_Y2 = "2H x 2V"
    X2_Y4 = "2H x 4V"
    X4_Y1 = "4H x 1V"
    X4_Y2 = "4H x 2V"
    X4_Y4 = "4H x 4V"
    X2_Y2_DIGITAL = "2H x 2V digital"


class DexelaFullWellMode(SupersetEnum):
    """Full-well modes for the Dexela detector.

    See https://areadetector.github.io/areaDetector/ADDexela/ADDexela.html#dexela-specific-parameters
    """

    LOW_NOISE = "Low noise"
    HIGH_RANGE = "High range"


class DexelaReadoutMode(StrictEnum):
    """Readout modes for the Dexela detector.

    ``IDLE`` disables continuous detector scrubbing, giving a prompt response
    to triggers at the expense of potentially larger dark current in the first
    few frames.

    See https://areadetector.github.io/areaDetector/ADDexela/ADDexela.html#dexela-specific-parameters
    """

    CONTINUOUS = "Continuous"
    IDLE = "Idle"


class DexelaDriverIO(StandardReadable, ADBaseIO):
    """Dexela driver interface.

    PV names follow ``Dexela.template`` from the ADDexela repository.

    See https://areadetector.github.io/areaDetector/ADDexela/ADDexela.html
    """

    # Standard Driver Parameters
    trigger_mode: A[SignalRW[DexelaTriggerMode], PvSuffix.rbv("TriggerMode")]
    num_images: A[SignalRW[int], PvSuffix.rbv("NumImages")]
    num_images_counter: A[SignalR[int], PvSuffix("NumImagesCounter_RBV")]
    acquire_time: A[SignalRW[float], PvSuffix.rbv("AcquireTime"), Format.CONFIG_SIGNAL]
    acquire_period: A[
        SignalRW[float], PvSuffix.rbv("AcquirePeriod"), Format.CONFIG_SIGNAL
    ]
    max_size_x: A[SignalR[int], PvSuffix("MaxSizeX_RBV")]
    max_size_y: A[SignalR[int], PvSuffix("MaxSizeY_RBV")]
    manufacturer: A[SignalR[str], PvSuffix("Manufacturer_RBV"), Format.CONFIG_SIGNAL]
    model: A[SignalR[str], PvSuffix("Model_RBV"), Format.CONFIG_SIGNAL]
    serial_number: A[SignalR[str], PvSuffix("SerialNumber_RBV"), Format.CONFIG_SIGNAL]
    firmware_version: A[
        SignalR[str], PvSuffix("FirmwareVersion_RBV"), Format.CONFIG_SIGNAL
    ]
    sdk_version: A[SignalR[str], PvSuffix("SDKVersion_RBV"), Format.CONFIG_SIGNAL]
    driver_version: A[SignalR[str], PvSuffix("DriverVersion_RBV"), Format.CONFIG_SIGNAL]

    # Detector Modes
    binning_mode: A[
        SignalRW[DexelaBinningMode],
        PvSuffix.rbv("DEXBinningMode"),
        Format.CONFIG_SIGNAL,
    ]
    full_well_mode: A[
        SignalRW[DexelaFullWellMode],
        PvSuffix.rbv("DEXFullWellMode"),
        Format.CONFIG_SIGNAL,
    ]
    readout_mode: A[
        SignalRW[DexelaReadoutMode],
        PvSuffix.rbv("DEXReadoutMode"),
        Format.CONFIG_SIGNAL,
    ]
    software_trigger: A[SignalRW[bool], PvSuffix("DEXSoftwareTrigger")]

    # Corrections directory
    corrections_dir: A[SignalRW[str], PvSuffix("DEXCorrectionsDir")]

    # Offset corrections (dark current)
    num_offset_frames: A[SignalRW[int], PvSuffix("DEXNumOffsetFrames")]
    current_offset_frame: A[SignalR[int], PvSuffix("DEXCurrentOffsetFrame")]
    acquire_offset: A[SignalRW[bool], PvSuffix("DEXAcquireOffset")]
    use_offset: A[SignalRW[bool], PvSuffix("DEXUseOffset"), Format.CONFIG_SIGNAL]
    offset_available: A[SignalR[bool], PvSuffix("DEXOffsetAvailable")]
    offset_file: A[SignalRW[str], PvSuffix("DEXOffsetFile")]
    load_offset_file: A[SignalRW[bool], PvSuffix("DEXLoadOffsetFile")]
    save_offset_file: A[SignalRW[bool], PvSuffix("DEXSaveOffsetFile")]
    offset_constant: A[
        SignalRW[int], PvSuffix.rbv("DEXOffsetConstant"), Format.CONFIG_SIGNAL
    ]

    # Gain corrections (flat field)
    num_gain_frames: A[SignalRW[int], PvSuffix("DEXNumGainFrames")]
    current_gain_frame: A[SignalR[int], PvSuffix("DEXCurrentGainFrame")]
    acquire_gain: A[SignalRW[bool], PvSuffix("DEXAcquireGain")]
    use_gain: A[SignalRW[bool], PvSuffix("DEXUseGain"), Format.CONFIG_SIGNAL]
    gain_available: A[SignalR[bool], PvSuffix("DEXGainAvailable")]
    gain_file: A[SignalRW[str], PvSuffix("DEXGainFile")]
    load_gain_file: A[SignalRW[bool], PvSuffix("DEXLoadGainFile")]
    save_gain_file: A[SignalRW[bool], PvSuffix("DEXSaveGainFile")]

    # Defect map corrections (bad pixels)
    use_defect_map: A[SignalRW[bool], PvSuffix("DEXUseDefectMap"), Format.CONFIG_SIGNAL]
    defect_map_available: A[SignalR[bool], PvSuffix("DEXDefectMapAvailable")]
    defect_map_file: A[SignalRW[str], PvSuffix("DEXDefectMapFile")]
    load_defect_map_file: A[SignalRW[bool], PvSuffix("DEXLoadDefectMapFile")]


@dataclass
class DexelaTriggerLogic(DetectorTriggerLogic):
    """Trigger logic for Dexela detectors."""

    driver: DexelaDriverIO

    def get_deadtime(self, config_values: SignalDict) -> float:
        return DEXELA_READOUT_TIME

    async def prepare_internal(self, num: int, livetime: float, deadtime: float):
        """Prepare the detector for internally triggered acquisition.

        With no requested deadtime the detector free-runs as fast as the
        exposure and readout allow; otherwise the frame period is fixed.
        """
        trigger_mode = (
            DexelaTriggerMode.INTERNAL_FIXED_RATE
            if deadtime
            else DexelaTriggerMode.INTERNAL_FREE_RUN
        )
        await asyncio.gather(
            self.driver.trigger_mode.set(trigger_mode),
            prepare_exposures(self.driver, num, livetime, deadtime),
        )

    async def prepare_edge(self, num: int, livetime: float):
        """Prepare the detector to take one frame per external trigger edge."""
        await asyncio.gather(
            self.driver.trigger_mode.set(DexelaTriggerMode.EXTERNAL_EDGE_SINGLE),
            prepare_exposures(self.driver, num, livetime),
        )

    async def prepare_level(self, num: int):
        """Prepare the detector to expose while the external trigger is high."""
        await asyncio.gather(
            self.driver.trigger_mode.set(DexelaTriggerMode.EXTERNAL_BULB),
            prepare_exposures(self.driver, num),
        )

    async def default_trigger_info(self):
        """Get default trigger info based on the number of images to acquire."""
        return await trigger_info_from_driver(self.driver.num_images)


class DexelaDetectorMock(DeviceMock["DexelaDetector"]):
    """Mock behaviour that simulates Dexela internally triggered acquisition."""

    async def connect(self, device: "DexelaDetector") -> None:
        """Mock signals to simulate Dexela detector acquisition."""
        set_mock_value(device.driver.array_size_x, 1536)
        set_mock_value(device.driver.array_size_y, 1944)
        set_mock_value(device.driver.data_type, ADBaseDataType.UINT16)

        try:
            hdf = device.get_plugin("hdf", NDFileHDF5IO)
            set_mock_value(hdf.array_size0, 1944)
            set_mock_value(hdf.array_size1, 1536)
            set_mock_value(hdf.file_path_exists, True)
        except (AttributeError, TypeError):
            hdf = None

        set_mock_value(device.driver.acquire_time, 1.0)
        set_mock_value(device.driver.acquire_period, 1.0 + DEXELA_READOUT_TIME)
        set_mock_value(device.driver.num_images, 1)
        set_mock_value(device.driver.num_images_counter, 0)
        set_mock_value(device.driver.trigger_mode, DexelaTriggerMode.INTERNAL_FREE_RUN)
        set_mock_value(device.driver.binning_mode, DexelaBinningMode.UNBINNED)
        set_mock_value(device.driver.full_well_mode, DexelaFullWellMode.LOW_NOISE)

        async def _do_acquisition():
            trigger_mode = await device.driver.trigger_mode.get_value()
            if trigger_mode not in (
                DexelaTriggerMode.INTERNAL_FREE_RUN,
                DexelaTriggerMode.INTERNAL_FIXED_RATE,
            ):
                set_mock_put_proceeds(device.driver.acquire, True)
                return

            num_images = await device.driver.num_images.get_value()
            acquire_time = await device.driver.acquire_time.get_value()
            if trigger_mode == DexelaTriggerMode.INTERNAL_FIXED_RATE:
                acquire_period = await device.driver.acquire_period.get_value()
            else:
                acquire_period = acquire_time + DEXELA_READOUT_TIME

            for i in range(num_images):
                await asyncio.sleep(acquire_time)
                set_mock_value(device.driver.num_images_counter, i + 1)
                if hdf is not None:
                    num_captured = await hdf.num_captured.get_value()
                    set_mock_value(hdf.num_captured, num_captured + 1)
                if i < num_images - 1:
                    await asyncio.sleep(max(acquire_period - acquire_time, 0))

            set_mock_value(device.driver.acquire, False)
            set_mock_put_proceeds(device.driver.acquire, True)

        def _on_acquire_write(value: bool) -> None:
            if value:
                set_mock_put_proceeds(device.driver.acquire, False)
                asyncio.ensure_future(_do_acquisition())

        callback_on_mock_put(device.driver.acquire, _on_acquire_write)


@default_mock_class(DexelaDetectorMock)
class DexelaDetector(AreaDetector[DexelaDriverIO]):
    """Create a Dexela AreaDetector instance.

    Parameters
    ----------
    prefix : str
        EPICS PV prefix for the detector
    writer_factories : ADWriterFactory
        Factories for file writer plugins and their data logics
    driver_suffix : str, optional
        Suffix for the driver PV, by default "cam1:"
    plugins : dict[str, NDPluginBaseIO], optional
        Additional areaDetector plugins to include, by default None
    config_sigs : Sequence[SignalR], optional
        Additional signals to include in configuration, by default ()
    name : str, optional
        Name for the detector device, by default ""
    """

    def __init__(
        self,
        prefix: str,
        *writer_factories: ADWriterFactory,
        driver_suffix="cam1:",
        plugins: dict[str, NDPluginBaseIO] | None = None,
        config_sigs: Sequence[SignalR] = (),
        name: str = "",
    ) -> None:
        driver = DexelaDriverIO(prefix + driver_suffix)
        super().__init__(
            driver,
            prefix,
            *writer_factories,
            acquire_logic=ADAcquireLogic(driver),
            trigger_logic=DexelaTriggerLogic(driver),
            plugins=plugins,
            config_sigs=config_sigs,
            name=name,
        )
