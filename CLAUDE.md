# Oceanoptics IOC — Claude Context

## Commit rules

- **Never** add `Co-Authored-By:` trailers to commit messages.

## What this project is

A pydev-based EPICS IOC for the Ocean Optics QEPro UV-Vis spectrometer at the 15-ID ASWAXS beamline (APS). The IOC runs on a Linux beamline computer. All acquisition logic lives in Python (`pyOceanOptics.py`); EPICS records delegate to Python functions via `@pydev` OUT/INP fields.

## Architecture

```
EPICS record (PROC / CA write)
        │
        ▼  @pydev field
pyOceanOptics.py          ← main driver, loaded via pydev("from pyOceanOptics import *")
  ├─ OceanDirectAPI        ← vendor Python SDK (oceandirect/), wraps liboceandirect.so
  ├─ hdf5_plugin.py        ← HDF5Plugin class, areaDetector HDF1-compatible
  └─ LampControl.py        ← GPIO D2/Halogen lamp control (RPi-style GPIO)

PV prefix: $(P):$(D):  →  e.g. 15ID:UVVis:
Set in: iocBoot/iocOceanoptics/st_uv_vis.cmd
```

## Key files

| File | Purpose |
|---|---|
| `python/pyOceanOptics.py` | All pydev functions — acquisition, absorbance, file save, lamp control, TEC, HDF5 bridge, MCA push |
| `python/hdf5_plugin.py` | `HDF5Plugin` — pure-Python areaDetector HDF1-style file plugin (Single/Capture/Stream modes) |
| `python/LampControl.py` | D2/Halogen lamp GPIO toggle (used by `toggle_d2`, `toggle_halogen`) |
| `python/oceandirect/` | Thin wrapper around the OceanDirect vendor SDK |
| `OceanopticsApp/Db/OceanopticsPV.db` | Core EPICS records (Spectrum, UVVis, ROI, lamp, file) |
| `OceanopticsApp/Db/OceanopticsQEPro.db` | TEC, boxcar, nonlinearity, file-status records |
| `OceanopticsApp/Db/OceanopticsHDF5.db` | HDF5 plugin PVs (file path, mode, AutoSave, Capture, etc.) |
| `iocBoot/iocOceanoptics/st_uv_vis.cmd` | IOC startup — sets PYTHONPATH, loads DBs, calls pydev init |
| `iocBoot/iocOceanoptics/hdf5_layout.xml` | NeXus HDF5 layout XML consumed by `HDF5Plugin` |
| `QEPro_Control.opi` | CS-Studio OPI control panel |
| `QEPro_Control.bob` | PyDM BOB control panel |

## EPICS record → Python function mapping

### Acquisition chain (OceanopticsPV.db)

```
Spectrum waveform  SCAN=Passive  FLNK → UVVis.PROC
  → get_spectrum()   (collects raw counts, calls _update_mca + _hdf1_capture_frame)

UVVis waveform     SCAN=Passive  FLNK → ROI_Int.PROC
  → get_uv_vis_abs()  (computes (ref - spectrum) / ref)

ROI_Int            SCAN=Passive  FLNK → ROI_Abs.PROC
  → get_roi_intensity()

ROI_Abs            SCAN=Passive
  → get_roi_absorbance()
```

The `Spectrum` record is triggered by the ophyd device writing to `Spectrum.PROC` with `put_complete=True`, which blocks until the entire FLNK chain (including HDF5 write) completes.

### TEC / QEPro settings (OceanopticsQEPro.db)

| PV | Python function |
|---|---|
| `TEC:Temp` (5 s scan) | `get_tec_temp()` |
| `TEC:TempSP` → `TEC:TempSP_RBV` | `set_tec_setpoint()` / `get_tec_setpoint()` |
| `TEC:Enable` → `TEC:Enable_RBV` | `set_tec_enable()` / `get_tec_enable()` |
| `TEC:Stable` (5 s scan) | `get_tec_stable()` |
| `setBoxcarWidth` → `BoxcarWidth` | `set_boxcar_width()` / `get_boxcar_width()` |

### HDF5 plugin (OceanopticsHDF5.db)

| PV | Python function |
|---|---|
| `HDF1:FilePath` | `hdf_set_filepath()` |
| `HDF1:FileName` | `hdf_set_filename()` |
| `HDF1:AutoSave` (bo, FLNK→AutoSave_RBV) | `hdf_set_autosave()` |
| `HDF1:AutoSave_RBV` (bi, Passive) | `hdf_get_autosave()` |
| `HDF1:WriteMode` | `hdf_set_writemode()` → `HDF5Plugin.write_mode` |
| `HDF1:Capture` (bo, FLNK→Capture_RBV) | `hdf_set_capture()` |
| `HDF1:Capture_RBV` (bi, 1 s scan) | `hdf_get_capture()` |
| `HDF1:WriteFile` | `hdf_writefile()` — immediate single write |

## `HDF5Plugin` internals

```
HDF5Plugin.write_mode: MODE_SINGLE | MODE_CAPTURE | MODE_STREAM
```

- **Single + auto_save=True**: `capture_frame()` calls `_write_hdf5()` immediately — one HDF5 file per `get_spectrum()` call. **This is synchronous and blocks the FLNK chain.**
- **Capture mode**: buffers frames in `_buf_spectra`; flushes when `num_captured >= num_capture` or when `set_capture(0)` is called.
- **Stream mode**: keeps an h5py file open via `_stream_file`; appends each frame with `_append_stream_frame()`. Closes on `set_capture(0)` or when `num_capture` is reached.
- All write paths hold `_lock` (threading.Lock). Never call `_write_hdf5` or `_flush_capture_buffer` from outside the lock.
- The NeXus XML layout (`hdf5_layout.xml`) is parsed once by `hdf_set_xml_filename()` into `HDF5Plugin._xml_root`. Without a valid XML file, `_build_default()` writes a flat layout.

## Lamp control — GPIO details

- D2 lamp: toggled by `toggle_d2()` via a GPIO pin pulse.
- Halogen: toggled by `toggle_halogen()`.
- Status is read by `_gpio_burst()` — samples GPIO pins for `_STATUS_WINDOW = 0.55 s`. **This is intentionally slow** (lamp status settling) and is only called from the status-polling path, NOT from the acquisition FLNK chain.
- `_status_snapshot()` caches result for `_STATUS_TTL = 0.5 s` to avoid redundant GPIO reads.

## MCA bridge

`_update_mca(data)` pushes the spectrum array to a loopback CA PV (`ioc_prefix + "MCA1"`) via `epics.caput`. Called non-blocking (`wait=False`) inside `get_spectrum()`. The `ioc_prefix` global must be set in `st_uv_vis.cmd` before `iocInit` via `pydev("ioc_prefix = 'P:D:'")`).

## Known issues / non-obvious decisions

- **Synchronous HDF5 write blocks acquisition**: `get_spectrum()` calls `_hdf1_capture_frame()` which may call `_write_hdf5()` synchronously (Single+auto_save mode). This blocks the entire Spectrum→UVVis→ROI FLNK chain for the duration of the disk write. For fast scanning, consider Stream mode or disabling auto_save and writing at scan end.

- **`OceanopticsPV.db` is only in the build-output `db/` dir** (gitignored). The source is in `OceanopticsApp/Db/`. After any DB change, rebuild (`make`) to regenerate `db/`.

- **`AutoSave_RBV` is SCAN=Passive**: it only updates when the `AutoSave` bo FLNK fires. The `AutoSave` bo record has `FLNK → AutoSave_RBV` — without this FLNK the readback never updates after a CA write (and the OPI LED stays stale).

- **OceanDirect SDK not in git**: `python/oceandirect/lib/liboceandirect.so` is the vendor binary and is gitignored. Must be copied manually from the Ocean Insight OceanDirect SDK download.

- **USB device index**: `odapi.open_device(2)` in `pyOceanOptics.py` is hardcoded. Run `python -c "from oceandirect.OceanDirectAPI import OceanDirectAPI; a=OceanDirectAPI(); a.find_usb_devices(); print(a.get_device_ids())"` to list connected devices and adjust the index.

- **Python lib path**: the src `Makefile` uses `PYTHON_LIB_DIR` (optional, set in `configure/CONFIG_SITE.local`) rather than a hardcoded conda path. If the linker cannot find `libpythonX.Y.so`, set this variable.

- **`drvSoftMca` commented out**: `OceanopticsMCA.db` and the `drvSoftMcaConfigure` line in `st_uv_vis.cmd` are commented out because `drvSoftMca` is not compiled into the current MCA build. Uncomment when a compatible MCA build is available.

- **Working directory at iocInit**: `st_uv_vis.cmd` changes into `${TOP}/iocBoot/${IOC}` before `iocInit`. `hdf_set_xml_filename` must use a path relative to `${TOP}` (the `cd` happens before pydev init but after the `hdf_set_xml_filename` call, so it uses `${TOP}` as base).

## Running locally

```bash
cd iocBoot/iocOceanoptics
../../bin/linux-x86_64/Oceanoptics st_uv_vis.cmd
```

Edit `ioc_prefix` in `st_uv_vis.cmd` and the USB device index in `pyOceanOptics.py` for a new beamline.
