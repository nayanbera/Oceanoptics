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
db/                   Built DB files (git-ignored build output)
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

| Requirement | Notes |
|---|---|
| EPICS base ≥ 7 | |
| [PyDevice](https://github.com/kasemir/PyDevice) | pydev device support — clone into `synApps/support/PyDevice` |
| [MCA](https://github.com/epics-modules/mca) | Soft MCA record support |
| [asyn](https://github.com/epics-modules/asyn) | Required by MCA |
| OceanDirect SDK | Vendor Python SDK from Ocean Optics; provides `liboceandirect.so` |
| `h5py`, `numpy`, `scipy` | Python packages in the PyDevice Python environment |

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

Implements the areaDetector HDF1 interface in pure Python. Supports Single, Capture, and Stream write modes. When `Auto Save = Yes` (Single mode), one HDF5 file is written per spectrum acquisition. The NeXus layout is driven by `hdf5_layout.xml`; without an XML file a flat default layout is used.

## License

EPICS Open License (source scaffolding); driver code © 2024 Mrinal Bera, University of Chicago.
