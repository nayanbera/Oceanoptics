"""
HDF5 file plugin for Ocean Insight QE Pro EPICS IOC.

Implements the areaDetector HDF1 plugin interface using h5py and an XML
layout file to define the HDF5 file structure and metadata attributes.

Write modes
-----------
Single  (0): Each WriteFile / auto_save writes one HDF5 file.
Capture (1): Accumulate NumCapture spectra in memory; write them all to one
             file when Capture goes to 0 or NumCaptured reaches NumCapture.
Stream  (2): Open one file on Capture=1, append each spectrum as it arrives,
             close the file when Capture goes to 0.

XML layout file  (HDF1:XMLFileName)
------------------------------------
Defines the HDF5 structure. Supported element types:

  <group name="...">        HDF5 group; can nest groups and datasets.
  <dataset name="..."       HDF5 dataset. Required attribute: source.
           source="constant|detector|ndattribute"
           value="..."      (for source=constant)
           ndattribute="..."(for source=ndattribute, key into nd_attrs dict)
           type="string|int|float|double" (for constant scalars)
  />
  <attribute name="..."     HDF5 attribute on the enclosing group or dataset.
             source="constant|ndattribute"
             value="..."
             ndattribute="..."
             type="string|int|float"
  />

source="detector" marks the dataset that receives the spectrum array.
det_default="true" is an alias for source="detector".

NDAttributes XML file  (HDF1:AttrXMLFileName)
----------------------------------------------
areaDetector-compatible XML that defines extra metadata attributes collected
from EPICS PVs or literal constants and injected into every HDF5 frame.
Format matches the areaDetector NDAttribute XML schema:

  <Attributes>
    <Attribute name="RingCurrent"
               type="EPICS_PV"
               source="S:SRcurrentAI.VAL"
               dbrtype="DBR_DOUBLE"
               description="Storage ring current (mA)" />
    <Attribute name="Facility"
               type="CONST"
               source="Advanced Photon Source"
               datatype="STRING"
               description="Facility name" />
  </Attributes>

Supported types:
  EPICS_PV  -- caget the PV at each capture; dbrtype controls the Python cast:
               DBR_DOUBLE/DBR_FLOAT → float, DBR_LONG/DBR_SHORT/DBR_ENUM → int,
               DBR_STRING/DBR_CHAR → str.
  CONST     -- literal constant; datatype = STRING | INT | DOUBLE.

Named attributes can then be referenced in the layout XML:
  <dataset name="ring_current" source="ndattribute" ndattribute="RingCurrent" />

NDAttributes dict
-----------------
Priority (highest wins): auto hardware keys > NDAttributes XML > HDF1:Attr PVs.

Auto keys populated on every capture:
  Wavelengths, DarkSpectrum, ReferenceSpectrum, IntegrationTime,
  ScansToAverage, BoxcarWidth, TECTemperature, Timestamp, FrameNumber,
  SourceFilename.
"""

import os
import threading
import time
import xml.etree.ElementTree as ET

import numpy as np

try:
    import h5py
    _H5PY_OK = True
except ImportError:
    h5py = None
    _H5PY_OK = False


# Write mode constants -- match OceanopticsHDF5.db mbbo values
MODE_SINGLE  = 0
MODE_CAPTURE = 1
MODE_STREAM  = 2

# Write status constants -- match OceanopticsHDF5.db mbbi values
STATUS_IDLE    = 0
STATUS_WRITING = 1
STATUS_ERROR   = 2


class HDF5Plugin:
    """areaDetector HDF1-style spectrometer file plugin."""

    def __init__(self):
        self._lock = threading.Lock()

        # --- file naming ---
        self.file_path      = "/tmp"
        self.file_name      = "spectrum"
        self.file_number    = 1
        self.auto_increment = True
        self.file_template  = "%s%s_%04d.h5"   # (path/, name, number)
        self.full_filename  = ""

        # --- write control ---
        self.write_mode    = MODE_SINGLE
        self.num_capture   = 1
        self.num_captured  = 0
        self.capturing     = False
        self.auto_save     = False
        self.write_status  = STATUS_IDLE
        self.write_message = ""

        # --- XML layout ---
        self.xml_filename = ""
        self.xml_valid    = False
        self.xml_error    = ""
        self._xml_root    = None   # parsed ET.Element for <hdf5_layout>

        # --- NDAttributes dict ---
        # User-set keys are preserved across captures; auto keys are refreshed
        # each time.
        self.nd_attrs = {}

        # --- NDAttributes XML (areaDetector-compatible) ---
        self.ndattr_xml_filename = ""
        self.ndattr_xml_valid    = False
        self.ndattr_xml_error    = ""
        self._ndattr_defs        = []   # list of parsed attribute definitions

        # --- stream-mode state ---
        self._stream_file = None
        self._stream_ds   = {}     # name -> h5py.Dataset (extendable)
        self._stream_path = ""

        # --- capture-mode accumulation buffer ---
        self._buf_spectra    = []   # list of np.ndarray
        self._buf_attrs_list = []   # list of nd_attrs snapshots

    # ------------------------------------------------------------------
    # File naming
    # ------------------------------------------------------------------

    def _make_path(self):
        """Full path from template (does NOT advance file_number)."""
        base = os.path.abspath(self.file_path)
        return self.file_template % (base + os.sep, self.file_name, self.file_number)

    def _next_path(self):
        """First unused path, advancing file_number until one is free."""
        path = self._make_path()
        while os.path.exists(path):
            self.file_number += 1
            path = self._make_path()
        return path

    def get_filepath_exists(self):
        return int(os.path.isdir(os.path.abspath(self.file_path)))

    # ------------------------------------------------------------------
    # XML layout loading
    # ------------------------------------------------------------------

    def load_xml(self, xml_path):
        xml_path = str(xml_path).strip().rstrip("\x00")
        self.xml_filename = xml_path
        if not xml_path:
            self._xml_root    = None
            self.xml_valid    = False
            self.xml_error    = ""
            return
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
            # Accept <hdf5_layout> or bare root acting as the layout
            self._xml_root = root
            self.xml_valid = True
            self.xml_error = ""
        except Exception as exc:
            self._xml_root = None
            self.xml_valid = False
            self.xml_error = str(exc)

    # ------------------------------------------------------------------
    # NDAttributes XML loader (areaDetector-compatible)
    # ------------------------------------------------------------------

    def load_ndattr_xml(self, xml_path):
        """Parse an areaDetector-compatible NDAttributes XML file.

        Populates self._ndattr_defs with attribute definitions that are
        evaluated at capture time by _collect_ndattrs().
        """
        xml_path = str(xml_path).strip().rstrip("\x00")
        self.ndattr_xml_filename = xml_path
        if not xml_path:
            self._ndattr_defs        = []
            self.ndattr_xml_valid    = False
            self.ndattr_xml_error    = ""
            return
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
            defs = []
            for elem in root.findall("Attribute"):
                name = elem.get("name", "").strip()
                if not name:
                    continue
                defs.append({
                    "name":        name,
                    "type":        elem.get("type",        "CONST"),
                    "source":      elem.get("source",      ""),
                    "dbrtype":     elem.get("dbrtype",     "DBR_DOUBLE").upper(),
                    "datatype":    elem.get("datatype",    "STRING").upper(),
                    "description": elem.get("description", ""),
                })
            self._ndattr_defs     = defs
            self.ndattr_xml_valid = True
            self.ndattr_xml_error = ""
        except Exception as exc:
            self._ndattr_defs     = []
            self.ndattr_xml_valid = False
            self.ndattr_xml_error = str(exc)

    def _collect_ndattrs(self):
        """Evaluate all NDAttribute definitions and return a dict.

        EPICS_PV attributes call epics.caget() with a 0.5 s timeout.
        CONST attributes use the literal source value.
        Failures are silently skipped so a disconnected PV never blocks
        a spectrum acquisition.
        """
        result = {}
        for defn in self._ndattr_defs:
            name  = defn["name"]
            atype = defn.get("type", "CONST")
            src   = defn.get("source", "")
            try:
                if atype == "EPICS_PV":
                    import epics
                    raw = epics.caget(src, timeout=0.5)
                    if raw is None:
                        continue
                    dbrtype = defn.get("dbrtype", "DBR_DOUBLE")
                    if dbrtype in ("DBR_STRING", "DBR_CHAR"):
                        result[name] = str(raw)
                    elif dbrtype in ("DBR_LONG", "DBR_SHORT", "DBR_ENUM", "DBR_INT"):
                        result[name] = int(raw)
                    else:   # DBR_DOUBLE, DBR_FLOAT
                        result[name] = float(raw)
                else:   # CONST
                    datatype = defn.get("datatype", "STRING")
                    if datatype in ("INT", "LONG"):
                        result[name] = int(src)
                    elif datatype in ("DOUBLE", "FLOAT"):
                        result[name] = float(src)
                    else:
                        result[name] = str(src)
            except Exception:
                pass
        return result

    # ------------------------------------------------------------------
    # NDAttribute snapshot
    # ------------------------------------------------------------------

    def _snap_attrs(self, spectrum, dark, reference, wavelengths,
                    itime, nscans, boxcar, tec_temp, source_filename=""):
        """Merge auto keys over the user-set nd_attrs dict."""
        d = dict(self.nd_attrs)
        d.update(self._collect_ndattrs())
        d.update({
            "Wavelengths":       _as_array(wavelengths),
            "DarkSpectrum":      _as_array(dark),
            "ReferenceSpectrum": _as_array(reference),
            "IntegrationTime":   float(itime),
            "ScansToAverage":    int(nscans),
            "BoxcarWidth":       int(boxcar),
            "TECTemperature":    float(tec_temp),
            "Timestamp":         time.strftime('%Y-%m-%dT%H:%M:%S'),
            "FrameNumber":       self.num_captured + 1,
            "SourceFilename":    source_filename,
        })
        return d

    # ------------------------------------------------------------------
    # HDF5 structure builders
    # ------------------------------------------------------------------

    def _write_hdf5(self, path, spectra, attrs_list):
        """Write one HDF5 file. Must be called with self._lock held."""
        self.write_status  = STATUS_WRITING
        self.write_message = "Writing %s ..." % os.path.basename(path)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with h5py.File(path, 'w') as f:
                if self._xml_root is not None:
                    self._build_xml(f, spectra, attrs_list)
                else:
                    self._build_default(f, spectra, attrs_list)
            self.full_filename = path
            self.write_status  = STATUS_IDLE
            self.write_message = "Wrote %s" % os.path.basename(path)
            if self.auto_increment:
                self.file_number += 1
            return True
        except Exception as exc:
            self.write_status  = STATUS_ERROR
            self.write_message = "Write error: %s" % exc
            return False

    # ---- XML-driven builder ----------------------------------------

    def _build_xml(self, hf, spectra, attrs_list):
        """Write HDF5 using the parsed XML layout."""
        # Use last frame's attrs for scalars/metadata; use all frames for detector.
        attrs = attrs_list[-1] if attrs_list else {}
        spec_data = (np.array(spectra, dtype=np.float64)
                     if len(spectra) > 1
                     else np.array(spectra[0], dtype=np.float64))

        def write_node(parent, elem):
            tag = elem.tag
            if tag == 'group':
                grp = parent.require_group(elem.get('name', 'group'))
                for child in elem:
                    if child.tag == 'attribute':
                        _set_attr(grp, child, attrs)
                    else:
                        write_node(grp, child)
            elif tag == 'dataset':
                name   = elem.get('name', 'data')
                source = elem.get('source', 'constant')
                is_det = (source == 'detector' or
                          elem.get('det_default', 'false').lower() == 'true')
                if is_det:
                    ds = parent.create_dataset(name, data=spec_data)
                elif source == 'ndattribute':
                    key = elem.get('ndattribute', name)
                    val = attrs.get(key, 0.0)
                    ds = parent.create_dataset(name, data=_coerce(val))
                elif source == 'constant':
                    ds = parent.create_dataset(
                        name,
                        data=_coerce_typed(elem.get('value', ''),
                                           elem.get('type', 'string')))
                else:
                    return
                for child in elem:
                    if child.tag == 'attribute':
                        _set_attr(ds, child, attrs)

        for child in self._xml_root:
            write_node(hf, child)

    # ---- default (no XML) builder ----------------------------------

    def _build_default(self, hf, spectra, attrs_list):
        attrs = attrs_list[-1] if attrs_list else {}
        spec_data = (np.array(spectra, dtype=np.float64)
                     if len(spectra) > 1
                     else np.array(spectra[0], dtype=np.float64))

        entry = hf.require_group('entry')
        entry.attrs['NX_class']    = 'NXentry'
        entry.attrs['timestamp']   = str(attrs.get('Timestamp', ''))
        entry.attrs['source_file'] = str(attrs.get('SourceFilename', ''))

        dg = entry.require_group('data')
        dg.attrs['NX_class'] = 'NXdata'
        dg.attrs['signal']   = 'spectrum'
        dg.attrs['axes']     = ['wavelength']

        dg.create_dataset('spectrum', data=spec_data)
        dg['spectrum'].attrs['units']     = 'counts'
        dg['spectrum'].attrs['long_name'] = 'Intensity'

        wl = _as_array(attrs.get('Wavelengths', []))
        dg.create_dataset('wavelength', data=np.array(wl, dtype=np.float64))
        dg['wavelength'].attrs['units']     = 'nm'
        dg['wavelength'].attrs['long_name'] = 'Wavelength'

        dark = _as_array(attrs.get('DarkSpectrum', []))
        dg.create_dataset('dark', data=np.array(dark, dtype=np.float64))
        dg['dark'].attrs['units'] = 'counts'

        ref = _as_array(attrs.get('ReferenceSpectrum', []))
        dg.create_dataset('reference', data=np.array(ref, dtype=np.float64))
        dg['reference'].attrs['units'] = 'counts'

        instr = entry.require_group('instrument')
        instr.attrs['NX_class'] = 'NXinstrument'

        det = instr.require_group('detector')
        det.attrs['NX_class'] = 'NXdetector'
        det.attrs['description'] = 'Ocean Insight QE Pro'

        det.create_dataset('integration_time',
                           data=float(attrs.get('IntegrationTime', 0.0)))
        det['integration_time'].attrs['units'] = 'us'

        det.create_dataset('scans_to_average',
                           data=int(attrs.get('ScansToAverage', 1)))

        det.create_dataset('boxcar_width',
                           data=int(attrs.get('BoxcarWidth', 0)))

        det.create_dataset('tec_temperature',
                           data=float(attrs.get('TECTemperature', float('nan'))))
        det['tec_temperature'].attrs['units'] = 'C'

        # Per-frame timestamps in multi-frame files
        if len(attrs_list) > 1:
            ts_list = [str(a.get('Timestamp', '')) for a in attrs_list]
            entry.create_dataset('frame_timestamps',
                                 data=np.array(ts_list, dtype=h5py.string_dtype()))
            entry.create_dataset('frame_numbers',
                                 data=np.array(
                                     [int(a.get('FrameNumber', i + 1))
                                      for i, a in enumerate(attrs_list)],
                                     dtype=np.int64))

        # All other user-set NDAttributes that weren't placed by the layout
        _reserved = {'Wavelengths', 'DarkSpectrum', 'ReferenceSpectrum',
                     'IntegrationTime', 'ScansToAverage', 'BoxcarWidth',
                     'TECTemperature', 'Timestamp', 'FrameNumber', 'SourceFilename'}
        extra = {k: v for k, v in attrs.items() if k not in _reserved}
        if extra:
            meta = entry.require_group('metadata')
            for k, v in extra.items():
                try:
                    meta.create_dataset(k, data=_coerce(v))
                except Exception:
                    meta.attrs[k] = str(v)

    # ------------------------------------------------------------------
    # Stream-mode helpers
    # ------------------------------------------------------------------

    def _open_stream(self, first_spectrum, nd):
        """Open a new HDF5 file for stream-mode appending."""
        path = self._next_path()
        self._stream_path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        f = h5py.File(path, 'w')

        n = len(first_spectrum)
        entry = f.require_group('entry')
        entry.attrs['NX_class'] = 'NXentry'

        dg = entry.require_group('data')
        dg.attrs['NX_class'] = 'NXdata'
        dg.attrs['signal']   = 'spectrum'

        # Extendable spectrum stack
        self._stream_ds['spectrum'] = dg.create_dataset(
            'spectrum', shape=(0, n), maxshape=(None, n),
            dtype=np.float64, chunks=(1, n))
        self._stream_ds['spectrum'].attrs['units'] = 'counts'

        wl = _as_array(nd.get('Wavelengths', np.ones(n)))
        dg.create_dataset('wavelength', data=np.array(wl, dtype=np.float64))
        dg['wavelength'].attrs['units'] = 'nm'

        dg.create_dataset('dark',
                          data=np.array(_as_array(nd.get('DarkSpectrum', [])),
                                        dtype=np.float64))
        dg.create_dataset('reference',
                          data=np.array(_as_array(nd.get('ReferenceSpectrum', [])),
                                        dtype=np.float64))

        instr = entry.require_group('instrument')
        instr.attrs['NX_class'] = 'NXinstrument'
        det = instr.require_group('detector')
        det.attrs['NX_class'] = 'NXdetector'
        det.create_dataset('integration_time',
                           data=float(nd.get('IntegrationTime', 0.0)))
        det['integration_time'].attrs['units'] = 'us'

        # Per-frame extendable metadata
        self._stream_ds['timestamps'] = entry.create_dataset(
            'frame_timestamps', shape=(0,), maxshape=(None,),
            dtype=h5py.string_dtype())
        self._stream_ds['frame_numbers'] = entry.create_dataset(
            'frame_numbers', shape=(0,), maxshape=(None,), dtype=np.int64)

        self._stream_file = f
        self.full_filename = path
        self.write_status  = STATUS_WRITING
        self.write_message = "Streaming to %s" % os.path.basename(path)

    def _append_stream_frame(self, spectrum, nd):
        i = self._stream_ds['spectrum'].shape[0]
        self._stream_ds['spectrum'].resize(i + 1, axis=0)
        self._stream_ds['spectrum'][i] = np.array(spectrum, dtype=np.float64)

        ts_ds = self._stream_ds.get('timestamps')
        if ts_ds is not None:
            ts_ds.resize(i + 1, axis=0)
            ts_ds[i] = str(nd.get('Timestamp', ''))

        fn_ds = self._stream_ds.get('frame_numbers')
        if fn_ds is not None:
            fn_ds.resize(i + 1, axis=0)
            fn_ds[i] = int(nd.get('FrameNumber', i + 1))

        self._stream_file.flush()

    def _close_stream(self):
        if self._stream_file is not None:
            try:
                self._stream_file.close()
            except Exception:
                pass
            self._stream_file = None
            self._stream_ds   = {}
        self.write_status  = STATUS_IDLE
        self.write_message = "Closed %s" % os.path.basename(self._stream_path)
        if self.auto_increment:
            self.file_number += 1
        self.num_captured = 0
        self.capturing    = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_capture(self, flag):
        """Start (1) or stop/flush (0) a Capture or Stream session."""
        with self._lock:
            flag = bool(int(flag))
            if flag:
                self.capturing    = True
                self.num_captured = 0
                self._buf_spectra.clear()
                self._buf_attrs_list.clear()
                if self.write_mode == MODE_STREAM and self._stream_file is not None:
                    self._close_stream()
            else:
                if self.write_mode == MODE_CAPTURE and self._buf_spectra:
                    self._flush_capture_buffer()
                elif self.write_mode == MODE_STREAM and self._stream_file is not None:
                    self._close_stream()
                else:
                    self.capturing    = False
                    self.num_captured = 0

    def _flush_capture_buffer(self):
        path = self._next_path()
        ok = self._write_hdf5(path, self._buf_spectra, self._buf_attrs_list)
        self._buf_spectra.clear()
        self._buf_attrs_list.clear()
        self.num_captured = 0
        self.capturing    = False
        return ok

    def capture_frame(self, spectrum, dark, reference, wavelengths,
                      itime, nscans, boxcar, tec_temp, source_filename=""):
        """Accept one spectrum frame.

        In Single mode: writes immediately if auto_save or capturing.
        In Capture mode: buffers until NumCapture is reached or Capture=0.
        In Stream mode: appends to the open file.
        """
        if not _H5PY_OK:
            self.write_status  = STATUS_ERROR
            self.write_message = "h5py not installed"
            return

        nd = self._snap_attrs(spectrum, dark, reference, wavelengths,
                               itime, nscans, boxcar, tec_temp, source_filename)

        with self._lock:
            if self.write_mode == MODE_SINGLE:
                if self.capturing or self.auto_save:
                    path = self._next_path()
                    self._write_hdf5(path, [list(spectrum)], [nd])
                    self.num_captured = 1

            elif self.write_mode == MODE_CAPTURE:
                if not self.capturing:
                    return
                self._buf_spectra.append(list(spectrum))
                self._buf_attrs_list.append(nd)
                self.num_captured = len(self._buf_spectra)
                if self.num_capture > 0 and self.num_captured >= self.num_capture:
                    self._flush_capture_buffer()

            elif self.write_mode == MODE_STREAM:
                if not self.capturing:
                    return
                try:
                    if self._stream_file is None:
                        self._open_stream(spectrum, nd)
                    self._append_stream_frame(spectrum, nd)
                    self.num_captured += 1
                    if self.num_capture > 0 and self.num_captured >= self.num_capture:
                        self._close_stream()
                except Exception as exc:
                    self.write_status  = STATUS_ERROR
                    self.write_message = "Stream error: %s" % exc

    def write_file_now(self, spectrum, dark, reference, wavelengths,
                       itime, nscans, boxcar, tec_temp, source_filename=""):
        """Immediate single-shot write regardless of mode / auto_save."""
        if not _H5PY_OK:
            self.write_status  = STATUS_ERROR
            self.write_message = "h5py not installed"
            return
        nd = self._snap_attrs(spectrum, dark, reference, wavelengths,
                               itime, nscans, boxcar, tec_temp, source_filename)
        with self._lock:
            path = self._next_path()
            self._write_hdf5(path, [list(spectrum)], [nd])


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _as_array(val):
    if val is None:
        return []
    if hasattr(val, 'tolist'):
        return val.tolist()
    if isinstance(val, (list, tuple)):
        return list(val)
    return val


def _coerce(val):
    """Convert a Python value to something h5py can store."""
    if isinstance(val, (list, tuple)):
        try:
            return np.array(val, dtype=np.float64)
        except (TypeError, ValueError):
            return np.array([str(v) for v in val], dtype=h5py.string_dtype())
    if isinstance(val, float):
        return np.float64(val)
    if isinstance(val, int):
        return np.int64(val)
    return str(val)


def _coerce_typed(value_str, type_str):
    """Cast a constant value string from XML to the requested type."""
    t = type_str.lower()
    if t in ('int', 'integer'):
        return np.int64(int(value_str))
    if t in ('float', 'double'):
        return np.float64(float(value_str))
    return str(value_str)


def _set_attr(target, elem, nd_attrs):
    """Write one <attribute> element onto an h5py group or dataset."""
    name   = elem.get('name', '')
    source = elem.get('source', 'constant')
    dtype  = elem.get('type', 'string')
    if not name:
        return
    if source == 'constant':
        target.attrs[name] = _coerce_typed(elem.get('value', ''), dtype)
    elif source == 'ndattribute':
        key = elem.get('ndattribute', name)
        val = nd_attrs.get(key, '')
        target.attrs[name] = _coerce(val) if not isinstance(val, str) else val
