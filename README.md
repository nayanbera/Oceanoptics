# Oceanoptics — QEPro UV-Vis IOC

EPICS IOC for the Ocean Optics QEPro spectrometer, developed for the **15-ID ASWAXS beamline** at the Advanced Photon Source. Provides a pydev-based IOC with a custom Python driver and an areaDetector-compatible HDF5 file plugin.

## Features

- Live spectrum acquisition via the OceanDirect SDK (`pyOceanOptics.py`)
- UV-Vis absorbance computation (reference/dark corrected)
- Configurable ROI integration (start/stop pixel, intensity, absorbance)
- HDF5 file saving with NeXus XML layout support (areaDetector HDF1-style interface)
- Soft MCA bridge — pushes spectra to an EPICS MCA PV for use with ROI tools
- TEC temperature monitoring and control
- Lamp control (Deuterium / Halogen toggle with status readback via GPIO)
- CS-Studio OPI panel (`QEPro_Control.opi`) and PyDM bob panel (`QEPro_Control.bob`)

## Repository layout

```
OceanopticsApp/Db/    EPICS record definitions (source)
db/                   Built DB files (git-ignored)
iocBoot/iocOceanoptics/
  st_uv_vis.cmd       IOC startup script for the QEPro UV-Vis
  hdf5_layout.xml     NeXus HDF5 layout XML
python/
  pyOceanOptics.py    Main pydev driver (spectrum, absorbance, HDF5, MCA)
  hdf5_plugin.py      Pure-Python areaDetector HDF1-compatible file plugin
  LampControl.py      Deuterium/Halogen lamp GPIO control
  oceandirect/        Thin wrapper around the OceanDirect SDK
QEPro_Control.opi     CS-Studio OPI control panel
QEPro_Control.bob     PyDM BOB control panel
```

## Dependencies

- EPICS base ≥ 7
- [pydev](https://github.com/epicsdeb/pydev) — Python device support for EPICS
- [OceanDirect SDK](https://www.oceanoptics.com/software/) — vendor Python SDK for QEPro
- `h5py` — for HDF5 file writing (optional; plugin degrades gracefully without it)
- `numpy`, `scipy`

## IOC startup

```bash
cd iocBoot/iocOceanoptics
../../bin/linux-x86_64/Oceanoptics st_uv_vis.cmd
```

PV prefix is `15ID:UVVis:` (set in `st_uv_vis.cmd`).

## HDF5 file plugin

Implements the areaDetector HDF1 interface in pure Python. Supports Single, Capture, and Stream write modes. When `Auto Save = Yes` (Single mode), one HDF5 file is written per spectrum acquisition. The NeXus layout is driven by `hdf5_layout.xml`; without an XML file a flat default layout is used.

## License

EPICS Open License (source scaffolding); driver code © 2024 Mrinal Bera, University of Chicago.
