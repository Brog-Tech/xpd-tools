"""XPD beamline detector interfaces."""

from .dexela import (
    DexelaBinningMode,
    DexelaDetector,
    DexelaDriverIO,
    DexelaFullWellMode,
    DexelaReadoutMode,
    DexelaTriggerMode,
)
from .panda import (
    PandAConfiguration,
    switch_panda_configuration,
)
from .pilatus4 import (
    Pilatus4CompressionAlgo,
    Pilatus4DataSource,
    Pilatus4Detector,
    Pilatus4DriverIO,
    Pilatus4ExtGateMode,
    Pilatus4HDF5Format,
    Pilatus4ROIMode,
    Pilatus4TriggerMode,
)
from .prosilica import (
    ProsilicaDetector,
    ProsilicaDriverIO,
    ProsilicaExposureMode,
    ProsilicaTriggerEvent,
    ProsilicaTriggerMode,
)
from .qepro import QEPro, QEProTEC

__all__ = [
    "DexelaBinningMode",
    "DexelaDetector",
    "DexelaDriverIO",
    "DexelaFullWellMode",
    "DexelaReadoutMode",
    "DexelaTriggerMode",
    "PandAConfiguration",
    "switch_panda_configuration",
    "Pilatus4Detector",
    "Pilatus4CompressionAlgo",
    "Pilatus4DataSource",
    "Pilatus4ExtGateMode",
    "Pilatus4ROIMode",
    "Pilatus4TriggerMode",
    "Pilatus4HDF5Format",
    "Pilatus4DriverIO",
    "ProsilicaDetector",
    "ProsilicaDriverIO",
    "ProsilicaExposureMode",
    "ProsilicaTriggerEvent",
    "ProsilicaTriggerMode",
    "QEPro",
    "QEProTEC",
]
