# Oceanoptics — QEPro UV-Vis IOC

EPICS IOC for the Ocean Optics QEPro spectrometer, developed for the **15-ID ASWAXS beamline** at the Advanced Photon Source. Provides a pydev-based IOC with a custom Python driver and an areaDetector-compatible HDF5 file plugin.

## Features

- Live spectrum acquisition via the OceanDirect SDK (`pyOceanOptics.py`)
- UV-Vis absorbance computation (reference/dark corrected)
- Configurable ROI integration (start/stop pixel, intensity, absorbance)
- HDF5 file saving with NeXus XML layout support (areaDetector HDF1-style interface)
- **NDAttributes XML** — areaDetector-compatible XML file to inject arbitrary EPICS PV values (ring current, undulator energy, sample temperature, …) as metadata into every HDF5 frame
- **Autosave** — all configurable PVs (acquisition parameters, ROIs, MCA calibration, HDF5 settings, dark/reference spectra) are preserved across IOC restarts via the synApps `save_restore` module
- **Soft MCA** — pydev MCA bridge with 8 configurable ROIs, energy calibration, real-time/live-time readback, and auto-update via EPICS FLNK chain
- TEC temperature monitoring and control
- Lamp control (Deuterium / Halogen toggle with status readback via GPIO)
- CS-Studio / Phoebus OPI panel (`QEPro_Control.opi`)

## Repository layout

```
OceanopticsApp/Db/
  OceanopticsPV.db        Spectrum, absorbance, ROI, lamp records
  OceanopticsMCA_pydev.db Soft MCA (8 ROIs, calibration, status) — pydev, no drvSoftMca
  OceanopticsHDF5.db      HDF5 file plugin PVs (path, mode, XML, NDAttr XML, metadata)
  OceanopticsQEPro.db     Integration time, boxcar, scans-to-average, TEC
  auto_settings.req       autosave request file — all settable PVs

iocBoot/iocOceanoptics/
  st_uv_vis.cmd           IOC startup script (autosave + pydev init)
  hdf5_layout.xml         NeXus / NXcanSAS HDF5 layout XML
  NDAttributes.xml        NDAttributes XML (areaDetector-compatible; PV metadata)

python/
  pyOceanOptics.py        Main pydev driver (spectrum, absorbance, HDF5, MCA)
  hdf5_plugin.py          Pure-Python areaDetector HDF1-compatible file plugin
  LampControl.py          Deuterium/Halogen lamp GPIO control
  oceandirect/            Thin wrapper around the OceanDirect SDK

autosave/                 Runtime save files written by save_restore (git-ignored)
QEPro_Control.opi         Phoebus / CS-Studio OPI control panel
```

## Dependencies

| Requirement | Notes |
|---|---|
| EPICS base ≥ 7 | |
| [PyDevice](https://github.com/kasemir/PyDevice) | pydev device support — clone into `synApps/support/PyDevice` |
| [MCA](https://github.com/epics-modules/mca) | Soft MCA record support |
| [asyn](https://github.com/epics-modules/asyn) | Required by MCA |
| [autosave](https://github.com/epics-modules/autosave) | synApps save_restore — version in `configure/RELEASE` |
| OceanDirect SDK | Vendor Python SDK from Ocean Optics; provides `liboceandirect.so` |
| `h5py`, `numpy`, `scipy` | Python packages in the PyDevice Python environment |
| `pyepics` | Required for NDAttributes EPICS_PV collection at HDF5 capture time |

---

## Installation inside synApps/support

### 1. Clone the repository

```bash
cd /path/to/synApps/support
git clone https://github.com/nayanbera/Oceanoptics
```

### 2. Install the OceanDirect SDK

The Python package (`oceandirect/sdk_properties.py`) always looks for the shared library at a fixed relative path:

```
python/oceandirect/lib/liboceandirect.so   (Linux)
python/oceandirect/lib/liboceandirect.dylib  (macOS)
```

**Option A — copy/symlink the library there** (recommended):

```bash
mkdir -p Oceanoptics/python/oceandirect/lib
# Copy:
cp /path/to/sdk/liboceandirect.so Oceanoptics/python/oceandirect/lib/
# Or symlink if the SDK is already installed elsewhere on the machine:
ln -s /existing/path/to/liboceandirect.so Oceanoptics/python/oceandirect/lib/liboceandirect.so
```

**Option B — edit `sdk_properties.py`** to point at the existing installation directly:

```python
# At the bottom of python/oceandirect/sdk_properties.py, replace the last line with:
oceandirect_dll = "/usr/local/lib/liboceandirect.so"   # adjust to your actual path
```

Install the required Python packages into the Python environment used by PyDevice:

```bash
pip install h5py numpy scipy
```

### 3. Edit `configure/RELEASE`

Open `Oceanoptics/configure/RELEASE` and adjust the paths to match your synApps layout:

```makefile
SUPPORT = /path/to/synApps/support

ASYN     = $(SUPPORT)/asyn-R4-44-2      # adjust version tag
MCA      = $(SUPPORT)/mca-R7-10         # adjust version tag
PYDEVICE = $(SUPPORT)/PyDevice
AUTOSAVE = $(SUPPORT)/autosave-R5-11-2  # adjust version tag

EPICS_BASE = /usr/local/epics/base      # adjust to your EPICS base
```

### 4. Set the Python lib directory (optional)

If the linker cannot find `libpythonX.Y.so` at build time, create `configure/CONFIG_SITE.local` and add:

```makefile
PYTHON_LIB_DIR = /path/to/conda/envs/your-env/lib
```

### 5. Build

```bash
cd Oceanoptics
make
```

Build products go into `bin/`, `lib/`, `db/`, `dbd/` — all git-ignored.

### 6. Configure the startup script

Edit `iocBoot/iocOceanoptics/st_uv_vis.cmd`:

```bash
# Change the PV prefix to match your beamline
pydev("ioc_prefix = 'XX:YY:'")
```

The USB device index (`odapi.open_device(2)` in `pyOceanOptics.py`) may also need adjusting — run `python -c "from oceandirect.OceanDirectAPI import OceanDirectAPI; a=OceanDirectAPI(); a.find_usb_devices(); print(a.get_device_ids())"` to list connected devices.

### 7. Start the IOC

```bash
cd iocBoot/iocOceanoptics
../../bin/linux-x86_64/Oceanoptics st_uv_vis.cmd
```

PV prefix is set in `st_uv_vis.cmd` (default `15ID:UVVis:`).

---

## HDF5 file plugin

Implements the areaDetector HDF1 interface in pure Python (`hdf5_plugin.py`). Supports Single, Capture, and Stream write modes. When `Auto Save = Yes` (Single mode), one HDF5 file is written per spectrum acquisition. The NeXus layout is driven by `hdf5_layout.xml`; without an XML file a flat default layout is used.

### NDAttributes XML

A second XML file (`NDAttributes.xml`, set via `HDF1:AttrXMLFileName`) follows the areaDetector `NDAttribute` schema and injects extra metadata into every HDF5 frame. Supported attribute types:

| type | description |
|---|---|
| `EPICS_PV` | Reads the named CA PV at capture time (0.5 s timeout, skipped if disconnected). `dbrtype` controls the Python cast: `DBR_DOUBLE`/`DBR_FLOAT` → float, `DBR_LONG`/`DBR_SHORT`/`DBR_ENUM` → int, `DBR_STRING`/`DBR_CHAR` → str. |
| `CONST` | Literal constant; `datatype` = STRING / INT / DOUBLE. |

Named attributes can then be referenced in `hdf5_layout.xml`:
```xml
<dataset name="ring_current" source="ndattribute" ndattribute="RingCurrent" />
```

Example entries for APS beamline PVs are provided in `iocBoot/iocOceanoptics/NDAttributes.xml` (commented out by default).

## Soft MCA (`OceanopticsMCA_pydev.db`)

Provides an MCA-compatible PV set without requiring `drvSoftMca`:

- `MCA1` — spectrum waveform (1044 points); updated via EPICS FLNK chain on every acquisition
- `MCA1_ERTM` / `MCA1_ELTM` / `MCA1_ACQG` — real time, elapsed time, acquiring status
- `MCA1_CALO` / `MCA1_CALS` — energy calibration (offset / slope)
- `MCA1_R0`…`MCA1_R7` — 8 ROI count readbacks; `R{N}LO`, `R{N}HI`, `R{N}NM` for limits and labels

The FLNK chain (`MCA1 → MCA1_FAN → MCA1_FAN2`) auto-updates all status and ROI records after every spectrum acquisition with zero CPU polling overhead.

## Autosave

All configurable PVs are saved to `autosave/auto_settings.sav` every 30 seconds and restored on IOC restart. The `autosave` module from synApps must be present; the version is set in `configure/RELEASE` (default `autosave-R5-11-2` — adjust to match your synApps tree). The save file directory (`$(TOP)/autosave/`) is created automatically by the startup script.

## License

EPICS Open License (source scaffolding); driver code © 2024 Mrinal Bera, University of Chicago.
