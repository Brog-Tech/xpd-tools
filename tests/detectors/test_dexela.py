from pathlib import Path

import bluesky.plans as bp
import pytest
from bluesky.run_engine import RunEngine
from ophyd_async.core import (
    DetectorTrigger,
    StaticPathProvider,
    TriggerInfo,
    UUIDFilenameProvider,
    init_devices,
    set_mock_value,
)
from ophyd_async.epics.adcore import ADImageMode, ADWriterFactory

from xpd_tools.detectors.dexela import DexelaDetector, DexelaTriggerMode


@pytest.fixture
async def dexela(tmp_path: Path) -> DexelaDetector:
    path_provider = StaticPathProvider(UUIDFilenameProvider(), tmp_path)
    async with init_devices(mock=True):
        dexela = DexelaDetector(
            "DEX:", ADWriterFactory.hdf(path_provider), name="dexela"
        )
    return dexela


@pytest.mark.parametrize(
    ("trigger_info", "trigger_mode", "acquire_period"),
    [
        (
            TriggerInfo(number_of_events=3, livetime=0.5),
            DexelaTriggerMode.INTERNAL_FREE_RUN,
            None,
        ),
        (
            TriggerInfo(number_of_events=3, livetime=0.5, deadtime=0.1),
            DexelaTriggerMode.INTERNAL_FIXED_RATE,
            0.6,
        ),
        (
            TriggerInfo(
                number_of_events=3,
                trigger=DetectorTrigger.EXTERNAL_EDGE,
                livetime=0.5,
                deadtime=0.1,
            ),
            DexelaTriggerMode.EXTERNAL_EDGE_SINGLE,
            None,
        ),
        (
            TriggerInfo(
                number_of_events=3,
                trigger=DetectorTrigger.EXTERNAL_LEVEL,
                deadtime=0.1,
            ),
            DexelaTriggerMode.EXTERNAL_BULB,
            None,
        ),
    ],
)
async def test_prepare_sets_trigger_mode(
    dexela: DexelaDetector,
    trigger_info: TriggerInfo,
    trigger_mode: DexelaTriggerMode,
    acquire_period: float | None,
):
    initial_period = await dexela.driver.acquire_period.get_value()
    await dexela.prepare(trigger_info)

    assert await dexela.driver.trigger_mode.get_value() == trigger_mode
    assert await dexela.driver.num_images.get_value() == 3
    assert await dexela.driver.image_mode.get_value() == ADImageMode.MULTIPLE
    if trigger_info.livetime:
        assert await dexela.driver.acquire_time.get_value() == trigger_info.livetime
    expected_period = initial_period if acquire_period is None else acquire_period
    assert await dexela.driver.acquire_period.get_value() == pytest.approx(
        expected_period
    )


def test_mock_internal_acquisition(RE: RunEngine, dexela: DexelaDetector):
    set_mock_value(dexela.driver.acquire_time, 0.001)
    docs: dict[str, list] = {}
    RE.subscribe(lambda name, doc: docs.setdefault(name, []).append(doc))

    RE(bp.count([dexela]))

    assert docs["stop"][0]["exit_status"] == "success"
    assert len(docs["stream_datum"]) == 1
