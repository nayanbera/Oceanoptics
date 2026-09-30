from oceandirect.OceanDirectAPI import OceanDirectAPI
import atexit
import numpy as np
import time
import os

np.seterr(divide='ignore', invalid='ignore')

odapi = OceanDirectAPI()
odapi.find_usb_devices()

odev = odapi.open_device(2)
adv = odev.Advanced

ndata = 1044
dark = np.zeros(ndata)
spectrum = np.ones(ndata)
datadir = '/tmp'
filename = 'test.txt'
reference = np.ones(ndata)
wavelengths = np.ones(ndata)
uv_abs = np.zeros(ndata)
message = 'None'

# Defined here, not only inside their setters. Without these a fresh IOC
# raises NameError on the first save_spectrum() (frame_number) and on the
# first ROI_Int processing driven by the Spectrum FLNK (_roi_start/_roi_stop):
# ROIStart and ROIStop are longouts with no PINI, so their setters never run
# at init even though the records carry a VAL.
frame_number = 1
last_saved_filename = ''
_roi_start = 0
_roi_stop = ndata
_message = 'None'

# Cached hardware metadata used by _hdf1_capture_frame().
# Updated by each setter so we avoid USB round-trips on every spectrum.
# Seeded once at module load so the cache is valid even before PINI records run.
try:
    _cached_itime  = int(odev.get_integration_time())
    _cached_nscans = int(odev.get_scans_to_average())
    _cached_boxcar = int(odev.get_boxcar_width())
except Exception:
    _cached_itime  = 8000
    _cached_nscans = 1
    _cached_boxcar = 0
_cached_tec = float('nan')

def cleanup_function():
    odapi.close_all_devices()
    odapi.shutdown()


def lamp_on(flag):
    """
    flag: True or False
    """
    global message
    adv.set_enable_lamp(flag)

    if flag:
        message = "Lamp is ON"
    else:
        message = "Lamp is OFF"

def get_lamp_status():
    flag = odev.Advanced.get_enable_lamp()
    return flag


def get_dark_spectrum():
    global dark, message
    dark = odev.get_formatted_spectrum()
    message = "Dark spectrum collected"
    return dark


def get_reference_spectrum():
    global reference, message
    reference = odev.get_formatted_spectrum()
    message = "Reference spectrum collected"
    return reference


# --------------------------------------------------
# File I/O status
# --------------------------------------------------
# set_data_dir and set_filename used to fail silently: the reason went into
# the shared Message PV, which the next operation overwrites within a
# second. FileStatus latches the outcome of the most recent file operation
# and carries an alarm severity, so a bad directory shows red in Phoebus and
# is visible to any Channel Access client.
#
# An EMPTY write is treated as "no intent" and leaves the status alone. Both
# setDataDir and setFileName carry PINI, so the IOC processes them once at
# init with an empty VAL; flagging that as an error would boot the IOC into
# a permanent alarm.

FILE_OK = 0
FILE_DIR_MISSING = 1
FILE_DIR_READONLY = 2
FILE_NAME_INVALID = 3
FILE_SAVE_FAILED = 4

# A file name, not a path: save_spectrum() joins this onto datadir, so a
# separator would silently write somewhere else or fail at write time.
_INVALID_NAME_CHARS = "/\\"

_file_status = FILE_OK
_file_error = ''


def _set_file_status(code, detail=''):
    """Latch the outcome of a file operation.

    _file_error is non-empty exactly when the status is not OK, so the two
    records can never disagree about whether something is wrong.
    """
    global _file_status, _file_error
    _file_status = code
    if code == FILE_OK:
        _file_error = ''
    else:
        _file_error = f"{time.strftime('%H:%M:%S')} {detail}"
    return code


def get_file_status():
    """0 OK, 1 dir missing, 2 dir not writable, 3 bad name, 4 save failed."""
    return _file_status


def get_file_error():
    """Text of the current fault, empty when the status is OK."""
    return _file_error


def set_data_dir(dirname):
    global datadir, message

    # Strip BEFORE testing. pydev hands back the raw lso char buffer, NUL
    # padding included, and os.path.isdir() fails on a path with a trailing
    # NUL even when the directory is really there.
    dirname = str(dirname).strip().rstrip("\x00")

    if not dirname:
        return datadir

    # isdir, not exists: a plain file passes exists() and then fails later
    # at save time, which is a far more confusing place to find out.
    if not os.path.isdir(dirname):
        message = f"Error: The directory {dirname} does not exist"
        _set_file_status(FILE_DIR_MISSING, message)
        return datadir

    if not os.access(dirname, os.W_OK | os.X_OK):
        message = f"Error: The directory {dirname} is not writable"
        _set_file_status(FILE_DIR_READONLY, message)
        return datadir

    datadir = dirname
    message = f"Data directory changed to {dirname}"
    _set_file_status(FILE_OK)
    return datadir


def set_filename(fname):
    global filename, frame_number, message

    candidate = str(fname).strip().rstrip("\x00")

    if not candidate:
        return filename

    if any(char in candidate for char in _INVALID_NAME_CHARS):
        message = f"Error: File name must not contain a path: {candidate}"
        _set_file_status(FILE_NAME_INVALID, message)
        return filename

    filename = candidate

    # Start a new frame sequence whenever Bluesky assigns
    # the scan-level filename. Only on a name we accepted -- a rejected
    # write must not restart numbering over the previous scan's files.
    frame_number = 1

    message = f"Filename changed to {filename}"
    _set_file_status(FILE_OK)
    return filename


def save_spectrum():
    global datadir
    global filename
    global frame_number
    global last_saved_filename
    global wavelengths
    global dark
    global reference
    global spectrum
    global message

    data_directory = os.path.abspath(datadir)

    # Preserve an extension supplied by Bluesky.
    base_name, extension = os.path.splitext(filename)

    if not extension:
        extension = ".txt"

    # Find the next unused frame number.
    while True:
        frame_filename = (
            f"{base_name}_F_{frame_number:04d}{extension}"
        )

        full_path = os.path.join(
            data_directory,
            frame_filename,
        )

        if not os.path.exists(full_path):
            break

        frame_number += 1

    header = f"Data saved on {time.asctime()}\n"
    header += "Wavelength(nm) Dark Reference Data"

    output_data = np.vstack((
        wavelengths,
        dark,
        reference,
        spectrum,
    )).T

    try:
        np.savetxt(
            full_path,
            output_data,
            header=header,
            delimiter=" ",
        )

        last_saved_filename = full_path
        message = f"Spectrum saved: {full_path}"
        _set_file_status(FILE_OK)

        # Increment only after successful saving.
        frame_number += 1

    except Exception as exc:
        message = f"Spectrum saving failed: {exc}"
        _set_file_status(FILE_SAVE_FAILED, message)

    return message


def get_uv_vis_abs():
    global spectrum, dark, reference, uv_abs,  message

    uv_abs = -np.log10((np.array(spectrum) - np.array(dark)) /
                       (np.array(reference) - np.array(dark)))
    uv_abs = np.nan_to_num(uv_abs)
    message = "New absorbance collected"
    return list(uv_abs)


def get_spectrum():
    global spectrum, message
    message = "Collecting Spectrum..."
    spectrum = odev.get_formatted_spectrum()
    message = "New spectrum collected"
    _update_mca(spectrum)
    _hdf1_capture_frame(spectrum)
    return spectrum

def get_and_save_spectrum():
    get_spectrum()
    save_spectrum()
    return spectrum

def set_roi_start(value):
    """Set the first included pixel."""
    global _roi_start
    _roi_start = int(value)
    return _roi_start

def set_roi_stop(value):
    """Set the first excluded pixel."""
    global _roi_stop
    _roi_stop = int(value)
    return _roi_stop

def get_roi_intensity():
    """Return the integrated counts inside the selected ROI."""
    global _message

    current_spectrum = np.asarray(spectrum, dtype=float)

    if current_spectrum.size == 0:
        _message = "No spectrum available"
        return 0.0

    start = max(0, min(_roi_start, current_spectrum.size))
    stop = max(start, min(_roi_stop, current_spectrum.size))

    intensity = float(np.sum(current_spectrum[start:stop]))

    return intensity

def get_roi_absorbance():
    """Return the integrated absorbance inside the selected ROI."""
    global _message, uv_abs

    current_absorbance = np.asarray(uv_abs, dtype=float)

    if current_absorbance.size == 0:
        _message = "No spectrum available"
        return 0.0

    start = max(0, min(_roi_start, current_absorbance.size))
    stop = max(start, min(_roi_stop, current_absorbance.size))

    intensity = float(np.sum(current_absorbance[start:stop]))

    return intensity

def get_wavelengths():
    global wavelengths
    wavelengths = odev.get_wavelengths()
    return wavelengths


def get_message():
    global message
    return message


def get_datadir():
    global datadir
    return datadir


def get_filename():
    global filename
    return filename


def set_integration_time(integration_time):
    global _cached_itime
    odev.set_integration_time(integration_time)
    _cached_itime = int(integration_time)


def get_integration_time():
    return odev.get_integration_time()


def get_integration_time_increment():
    return odev.get_integration_time_increment()


def get_scans_to_average():
    return odev.get_scans_to_average()


def set_scans_to_average(num_of_scans):
    global _cached_nscans
    odev.set_scans_to_average(num_of_scans)
    _cached_nscans = int(num_of_scans)


def get_single_strobe_enable():
    return adv.get_single_strobe_enable()


def set_single_strobe_enable(flag):
    adv.set_single_strobe_enable(flag)


def get_continuous_strobe_enable():
    return adv.get_continuous_strobe_enable()


def process_set_data_dir(record):
    """
    EPICS will call this function with a 'record' object containing the waveform data.
    """
    global datadir, message

    data = record.get("INPA", [])
    if not data:
        print("No input data received")
        return 0

    dirname = "".join(chr(c) for c in data if c != 0)

    print(f"Received dirname: {dirname}")

    if os.path.exists(dirname):
        datadir = dirname
        message = f"Directory changed to {dirname}"
    else:
        message = f"Error: Directory does not exist: {dirname}"

    print(message)

    result = [ord(c) for c in datadir.ljust(2048, "\x00")]
    record.put("OUTA", result)

    return 0


# --------------------------------------------------
# Lamp control functions
# --------------------------------------------------
def _pulse_gpio(pin, width=0.5, low_settle=0.1):
    """
    Send LOW -> HIGH -> LOW pulse
    """
    adv.gpio_set_output_enable1(pin, True)

    adv.gpio_set_value1(pin, False)
    time.sleep(low_settle)

    adv.gpio_set_value1(pin, True)
    time.sleep(width)

    adv.gpio_set_value1(pin, False)


def toggle_halogen(val=None):
    global message
    _pulse_gpio(8, width=0.5, low_settle=0.1)
    message = "Halogen Toggled"

def toggle_d2(val=None):
    global message
    _pulse_gpio(7, width=0.5, low_settle=0.1)
    message = "Deuterium Toggled"


# --------------------------------------------------
# Lamp status readback (DH-2000 -> QE Pro GPIO inputs)
# --------------------------------------------------
# Status only. Nothing here writes a pin, sets a direction, or toggles a
# lamp -- the control path above is untouched.
#
# Cable map, continuity-measured on the installed DH2-HPX-CBL-DB15. No
# published pinout exists for that cable, and the documented HR4-CBL-DB15 is
# wired differently, so this table is the only source of truth:
#
#   DB15  signal      J2   GPIO  meaning
#     5   HAL_BAD      9     1   LOW  = halogen fault
#     8   D_BAD        3     2   LOW  = deuterium fault
#     9   D_GOOD      11     3   LOW  = deuterium on; blinks while warming up
#    14   HAL_GOOD    16     4   LOW  = halogen on
#    15   SHTR_STAT   18     5   HIGH = shutter open   (mapping PREDICTED)
#
# All five are open-collector and depend on the pull-up at DB15 pin 1, which
# is already energised here -- the lines idle HIGH. Verified on hardware
# 2026-09-11: each of GPIO 1-4 was seen releasing to HIGH, which an
# open-collector output cannot do on its own, so those four mappings are
# confirmed by measurement. GPIO 5 follows the cable's ascending pattern but
# has not been measured.
#
# Deliberate constraints:
#   * Only gpio_get_value1 / gpio_get_value2 are called. This firmware
#     answers "Command not supported by device." to gpio_get_output_alternate1,
#     and several getters in one try block would discard the level reading
#     along with the unsupported call.
#   * Direction is never set. GPIO 1-4 already read as inputs, and
#     _pulse_gpio only ever touches 7 and 8, so the two never collide.
#   * A failed read returns UNKNOWN, never OFF. A disconnected input reads
#     LOW exactly like an asserted one, so LOW alone is never proof.

HAL_BAD_GPIO = 1
D_BAD_GPIO = 2
D_GOOD_GPIO = 3
HAL_GOOD_GPIO = 4
SHTR_STAT_GPIO = 5

_STATUS_PINS = (HAL_BAD_GPIO, D_BAD_GPIO, D_GOOD_GPIO, HAL_GOOD_GPIO,
                SHTR_STAT_GPIO)

# mbbi states -- keep in step with field(ZRST/ONST/TWST/THST) in the .db
LAMP_OFF, LAMP_ON, LAMP_WARMING, LAMP_UNKNOWN = 0, 1, 2, 3
FAULT_OK, FAULT_ACTIVE, FAULT_UNKNOWN = 0, 1, 2
SHTR_CLOSED, SHTR_OPEN, SHTR_UNKNOWN = 0, 1, 2

_LAMP_TEXT = {LAMP_OFF: "Off", LAMP_ON: "On", LAMP_WARMING: "Warming up",
              LAMP_UNKNOWN: "Unknown"}

# The deuterium warm-up blink measures ~2.1 Hz on this unit (the DH-2000
# manual claims 1 Hz), a period near 0.48 s with a short LOW and long HIGH.
# A burst must span at least one full period to tell "blinking" from
# "steady", otherwise a warming lamp reads On or Off at random per scan.
_STATUS_WINDOW = 0.55
_STATUS_PERIOD = 0.05

# One burst serves every status record in the same processing pass. Keep the
# TTL below the record SCAN rate but well above the few ms needed to process
# them all.
_STATUS_TTL = 0.5

_gpio_bulk = None                  # None = untested; True = use value2
_status_cache = {"t": 0.0, "seen": {}, "last": {}}


def _gpio_levels(pins):
    """Read the given pins once. Returns {pin: bool}, or {} if the read failed.

    Prefers the bitmask call: one USB round trip for every pin rather than one
    each, which matters because a burst repeats this a dozen times.
    """
    global _gpio_bulk
    if _gpio_bulk is None:
        try:
            adv.gpio_get_value2()
            _gpio_bulk = True
        except Exception:
            _gpio_bulk = False
    try:
        if _gpio_bulk:
            mask = adv.gpio_get_value2()
            return dict((p, bool((mask >> p) & 1)) for p in pins)
        return dict((p, bool(adv.gpio_get_value1(p))) for p in pins)
    except Exception:
        return {}


def _gpio_burst(pins, window=_STATUS_WINDOW, period=_STATUS_PERIOD):
    """Sample pins across at least one blink period.

    Returns (seen, last):
        seen[p]  set of levels observed -- two entries means that line is
                 blinking, which is the deuterium warm-up indication.
        last[p]  most recent level, or None if every read of it failed.
    Returns as soon as every pin has shown both levels, so a warming lamp is
    classified without waiting out the whole window.
    """
    seen = dict((p, set()) for p in pins)
    last = dict((p, None) for p in pins)
    deadline = time.time() + window
    while True:
        for p, v in _gpio_levels(pins).items():
            seen[p].add(v)
            last[p] = v
        if all(len(seen[p]) == 2 for p in pins):
            break
        if time.time() >= deadline:
            break
        time.sleep(period)
    return seen, last


def _status_snapshot(force=False):
    """Cached burst shared by all the status readbacks."""
    now = time.time()
    if not force and (now - _status_cache["t"]) < _STATUS_TTL \
            and _status_cache["seen"]:
        return _status_cache["seen"], _status_cache["last"]
    seen, last = _gpio_burst(_STATUS_PINS)
    _status_cache["t"] = time.time()
    _status_cache["seen"] = seen
    _status_cache["last"] = last
    return seen, last


def _lamp_state(good_pin):
    """Off / On / Warming up / Unknown for one lamp, from its GOOD line."""
    seen, _last = _status_snapshot()
    levels = seen.get(good_pin) or set()
    if not levels:
        return LAMP_UNKNOWN            # every read failed; NOT "off"
    if len(levels) == 2:
        return LAMP_WARMING            # blinking
    return LAMP_ON if False in levels else LAMP_OFF


def _fault_state(bad_pin):
    """Faults come from the current level, not from the burst.

    HAL_BAD dips LOW for ~0.3 s when the halogen ignites -- the DH-2000 manual
    notes it flashes briefly at power up, and it was observed doing so. Taking
    "LOW at any point in the window" would latch a fault on every start.
    """
    _seen, last = _status_snapshot()
    level = last.get(bad_pin)
    if level is None:
        return FAULT_UNKNOWN
    return FAULT_ACTIVE if level is False else FAULT_OK


def get_d2_status():
    """0 Off, 1 On, 2 Warming up, 3 Unknown."""
    return _lamp_state(D_GOOD_GPIO)


def get_halogen_status():
    """0 Off, 1 On, 2 Warming up, 3 Unknown."""
    return _lamp_state(HAL_GOOD_GPIO)


def get_d2_fault():
    """0 OK, 1 Fault, 2 Unknown."""
    return _fault_state(D_BAD_GPIO)


def get_halogen_fault():
    """0 OK, 1 Fault, 2 Unknown."""
    return _fault_state(HAL_BAD_GPIO)


def get_shutter_status():
    """0 Closed, 1 Open, 2 Unknown. SHTR_STAT is active HIGH.

    The GPIO 5 mapping is predicted from the cable pattern, not measured.
    Treat it as provisional until a shutter movement is seen to change it.
    """
    _seen, last = _status_snapshot()
    level = last.get(SHTR_STAT_GPIO)
    if level is None:
        return SHTR_UNKNOWN
    return SHTR_OPEN if level else SHTR_CLOSED


def get_lamp_status_text():
    """One-line summary for a status string PV."""
    parts = ["D2: %s" % _LAMP_TEXT[get_d2_status()],
             "Halogen: %s" % _LAMP_TEXT[get_halogen_status()]]
    if get_d2_fault() == FAULT_ACTIVE:
        parts.append("D2 FAULT")
    if get_halogen_fault() == FAULT_ACTIVE:
        parts.append("HALOGEN FAULT")
    return " | ".join(parts)


# --- combined per-lamp indicator --------------------------------------
# One value per lamp for the GUI:
#     0 Off      grey
#     1 On       green   (includes warm-up: the lamp is energised)
#     2 Fault    red     (that lamp's *_BAD line asserted)
#     3 Unknown          no state colour; the LED falls back to magenta
#
# Unknown is deliberately NOT folded into Off. A failed GPIO read must never
# be displayed as a confidently-off lamp. The warm-up distinction is not lost
# either -- LampStatusText still spells out "Warming up".

IND_OFF, IND_ON, IND_FAULT, IND_UNKNOWN = 0, 1, 2, 3


def _indicator(good_pin, bad_pin):
    """Fault outranks on/off, because a faulted lamp may still read as on."""
    seen, last = _status_snapshot()
    if last.get(bad_pin) is False:          # *_BAD is active LOW
        return IND_FAULT
    levels = seen.get(good_pin) or set()
    if not levels:
        return IND_UNKNOWN                  # every read failed
    if len(levels) == 2:
        return IND_ON                       # blinking: warming up
    return IND_ON if False in levels else IND_OFF


def get_d2_indicator():
    """0 Off, 1 On, 2 Fault, 3 Unknown."""
    return _indicator(D_GOOD_GPIO, D_BAD_GPIO)


def get_halogen_indicator():
    """0 Off, 1 On, 2 Fault, 3 Unknown."""
    return _indicator(HAL_GOOD_GPIO, HAL_BAD_GPIO)


# --- one readback per physical line, for EPICS to combine -------------
# 0 clear (not asserted), 1 asserted, 2 unknown (read failed).
#
# The GOOD lines are judged over the whole burst: LOW at any point counts as
# asserted, so the ~2.1 Hz warm-up blink reads as a steady "on" instead of
# flickering. The BAD lines are judged from the current level only, because
# HAL_BAD dips LOW for ~0.3 s whenever the halogen ignites and a burst test
# would latch a fault on every start.
#
# Unknown is never folded into clear. A failed GPIO read must not be
# reported as a confidently-unlit lamp.

# GOOD lines carry four values so the calc record can tell a warming lamp
# from a lit one; BAD lines only need three.
GOOD_CLEAR, GOOD_ON, GOOD_WARMING, GOOD_UNKNOWN = 0, 1, 2, 3
BAD_CLEAR, BAD_FAULT, BAD_UNKNOWN = 0, 1, 2


def _good_line(pin):
    """A GOOD line, judged across the whole burst.

    Blinking is the warm-up indication and needs at least one full period of
    the ~2.1 Hz square wave to distinguish from a steady level, which is what
    the burst provides.
    """
    seen, _last = _status_snapshot()
    levels = seen.get(pin) or set()
    if not levels:
        return GOOD_UNKNOWN            # every read failed; NOT "clear"
    if len(levels) == 2:
        return GOOD_WARMING            # blinking
    return GOOD_ON if False in levels else GOOD_CLEAR


def _bad_line(pin):
    """A BAD line, judged from the current level only.

    HAL_BAD dips LOW for ~0.3 s whenever the halogen ignites -- the DH-2000
    manual notes it flashes briefly at power up -- so testing the burst would
    latch a fault on every start.
    """
    _seen, last = _status_snapshot()
    level = last.get(pin)
    if level is None:
        return BAD_UNKNOWN
    return BAD_FAULT if level is False else BAD_CLEAR


def get_d2_good():
    """D_GOOD, GPIO 3. 0 clear, 1 on, 2 warming up, 3 unknown."""
    return _good_line(D_GOOD_GPIO)


def get_d2_bad():
    """D_BAD, GPIO 2. 0 clear, 1 fault, 2 unknown."""
    return _bad_line(D_BAD_GPIO)


def get_halogen_good():
    """HAL_GOOD, GPIO 4. 0 clear, 1 on, 2 warming up, 3 unknown."""
    return _good_line(HAL_GOOD_GPIO)


def get_halogen_bad():
    """HAL_BAD, GPIO 1. 0 clear, 1 fault, 2 unknown."""
    return _bad_line(HAL_BAD_GPIO)


def read_lamp_status(force=True):
    """Everything at once, for interactive use at the console."""
    _status_snapshot(force=force)
    return {"d2": _LAMP_TEXT[get_d2_status()],
            "halogen": _LAMP_TEXT[get_halogen_status()],
            "d2_fault": get_d2_fault() == FAULT_ACTIVE,
            "halogen_fault": get_halogen_fault() == FAULT_ACTIVE,
            "shutter_open": get_shutter_status() == SHTR_OPEN,
            "text": get_lamp_status_text()}


# ==================================================================
# TEC (Thermo-Electric Cooler) control
# ==================================================================

def get_tec_temp():
    """Current detector temperature in deg C."""
    global _cached_tec
    try:
        _cached_tec = float(adv.get_tec_temperature_degrees_C())
    except Exception:
        _cached_tec = float('nan')
    return _cached_tec


def get_tec_setpoint():
    """TEC setpoint readback in deg C."""
    try:
        return float(adv.get_temperature_setpoint_degrees_C())
    except Exception:
        return float('nan')


def set_tec_setpoint(temp_c):
    """Set TEC temperature setpoint in deg C."""
    global message
    try:
        adv.set_temperature_setpoint_degrees_C(float(temp_c))
        message = f"TEC setpoint: {float(temp_c):.1f} C"
    except Exception as exc:
        message = f"TEC setpoint error: {exc}"


def get_tec_enable():
    """1 if TEC is enabled, 0 if disabled."""
    try:
        return int(adv.get_tec_enable())
    except Exception:
        return 0


def set_tec_enable(flag):
    """Enable (1) or disable (0) the TEC."""
    global message
    try:
        adv.set_tec_enable(bool(int(flag)))
        message = "TEC enabled" if int(flag) else "TEC disabled"
    except Exception as exc:
        message = f"TEC enable error: {exc}"


def get_tec_stable():
    """1 if TEC has reached its setpoint, 0 if still stabilizing."""
    try:
        return int(adv.get_tec_stable())
    except Exception:
        return 0


# ==================================================================
# Boxcar width and correction controls
# ==================================================================

def get_boxcar_width():
    """Current boxcar averaging width (0 = off)."""
    return odev.get_boxcar_width()


def set_boxcar_width(val):
    """Set boxcar averaging width (0 = off, max ~15)."""
    global message, _cached_boxcar
    odev.set_boxcar_width(int(val))
    _cached_boxcar = int(val)
    message = f"Boxcar width: {int(val)}"


_nonlin_enabled = True


def get_nonlin_correct():
    """1 if non-linearity correction is applied during spectrum acquisition."""
    return int(_nonlin_enabled)


def set_nonlin_correct(flag):
    """Enable (1) or disable (0) non-linearity correction."""
    global _nonlin_enabled, message
    _nonlin_enabled = bool(int(flag))
    odev.use_nonlinearity(_nonlin_enabled)
    message = f"Non-linearity correction: {'on' if _nonlin_enabled else 'off'}"


# ==================================================================
# HDF5 plugin (areaDetector HDF1-style)
# ==================================================================

from hdf5_plugin import HDF5Plugin as _HDF5Plugin, _H5PY_OK as _hdf_h5py_ok

hdf1 = _HDF5Plugin()


def _hdf1_capture_frame(spec):
    """Called after every get_spectrum(); delegates to hdf1.

    Uses cached metadata (itime/nscans/boxcar/tec) to avoid USB round-trips
    on every acquisition.  The cache is updated by each setter and by the
    5-second TEC:Temp record scan.
    """
    hdf1.capture_frame(
        spec, dark, reference, wavelengths,
        _cached_itime, _cached_nscans, _cached_boxcar, _cached_tec,
        source_filename=filename,
    )


# --- file path / name ---

def hdf_set_filepath(val):
    global message
    val = str(val).strip().rstrip("\x00")
    if not val:
        return hdf1.file_path
    if not os.path.isdir(val):
        message = f"HDF1 FilePath not found: {val}"
        return hdf1.file_path
    hdf1.file_path = val
    message = f"HDF1 FilePath: {val}"
    return val

def hdf_get_filepath():
    return hdf1.file_path

def hdf_get_filepath_exists():
    return hdf1.get_filepath_exists()

def hdf_set_filename(val):
    global message
    val = str(val).strip().rstrip("\x00")
    if not val:
        return hdf1.file_name
    hdf1.file_name = val
    message = f"HDF1 FileName: {val}"
    return val

def hdf_get_filename():
    return hdf1.file_name

def hdf_set_filenumber(val):
    hdf1.file_number = max(0, int(val))

def hdf_get_filenumber():
    return hdf1.file_number

def hdf_set_autoincrement(val):
    hdf1.auto_increment = bool(int(val))

def hdf_get_autoincrement():
    return int(hdf1.auto_increment)

def hdf_set_filetemplate(val):
    val = str(val).strip().rstrip("\x00")
    if val:
        hdf1.file_template = val

def hdf_get_filetemplate():
    return hdf1.file_template

def hdf_get_fullfilename():
    return hdf1.full_filename

# --- write control ---

def hdf_set_writemode(val):
    hdf1.write_mode = int(val)

def hdf_get_writemode():
    return hdf1.write_mode

def hdf_set_numcapture(val):
    hdf1.num_capture = max(0, int(val))

def hdf_get_numcapture():
    return hdf1.num_capture

def hdf_get_numcaptured():
    return hdf1.num_captured

def hdf_set_capture(val):
    hdf1.set_capture(int(val))

def hdf_get_capture():
    return int(hdf1.capturing)

def hdf_set_autosave(val):
    hdf1.auto_save = bool(int(val))

def hdf_get_autosave():
    return int(hdf1.auto_save)

def hdf_writefile(val=None):
    """Immediate single-shot write regardless of mode / auto_save."""
    global message
    hdf1.write_file_now(
        spectrum, dark, reference, wavelengths,
        odev.get_integration_time(), odev.get_scans_to_average(),
        odev.get_boxcar_width(), get_tec_temp(),
        source_filename=filename,
    )
    message = hdf1.write_message

def hdf_get_write_status():
    return hdf1.write_status

def hdf_get_write_message():
    return hdf1.write_message

def hdf_get_available():
    return int(_hdf_h5py_ok)

# --- XML layout ---

def hdf_set_xml_filename(val):
    global message
    val = str(val).strip().rstrip("\x00")
    hdf1.load_xml(val)
    if val and not hdf1.xml_valid:
        message = f"HDF1 XML error: {hdf1.xml_error}"
    elif val:
        message = f"HDF1 XML loaded: {os.path.basename(val)}"

def hdf_get_xml_filename():
    return hdf1.xml_filename

def hdf_get_xml_valid():
    return int(hdf1.xml_valid)

def hdf_get_xml_error():
    return hdf1.xml_error

# --- NDAttribute setters (user metadata) ---

def hdf_set_attr_sample_name(val):
    hdf1.nd_attrs['SampleName'] = str(val).strip().rstrip("\x00")

def hdf_get_attr_sample_name():
    return hdf1.nd_attrs.get('SampleName', '')

def hdf_set_attr_sample_desc(val):
    hdf1.nd_attrs['SampleDescription'] = str(val).strip().rstrip("\x00")

def hdf_get_attr_sample_desc():
    return hdf1.nd_attrs.get('SampleDescription', '')

def hdf_set_attr_beam_energy(val):
    hdf1.nd_attrs['BeamEnergy'] = float(val)

def hdf_get_attr_beam_energy():
    return float(hdf1.nd_attrs.get('BeamEnergy', 0.0))

def hdf_set_attr_exposure_mode(val):
    hdf1.nd_attrs['ExposureMode'] = str(val).strip().rstrip("\x00")

def hdf_get_attr_exposure_mode():
    return hdf1.nd_attrs.get('ExposureMode', '')


# ==================================================================
# MCA-compatible pydev records (OceanopticsMCA_pydev.db)
# No drvSoftMca needed — all backed by Python.
# ==================================================================

_N_MCA_ROIS = 8
_mca_roi_lo   = [0]   * _N_MCA_ROIS
_mca_roi_hi   = [0]   * _N_MCA_ROIS
_mca_roi_name = ['ROI %d' % i for i in range(_N_MCA_ROIS)]
_mca_calo = 0.0   # energy cal offset (eV at channel 0)
_mca_cals = 1.0   # energy cal slope  (eV / channel)
_mca_acq_start = None  # time.monotonic() when last spectrum was triggered


def mca_get_roi_counts(idx):
    idx = int(idx)
    lo = _mca_roi_lo[idx]
    hi = _mca_roi_hi[idx]
    if hi <= lo:
        return 0.0
    arr = np.asarray(spectrum, dtype=float)
    lo = max(0, min(lo, arr.size))
    hi = max(lo, min(hi, arr.size))
    return float(np.sum(arr[lo:hi]))

def mca_set_roi_lo(idx, val):
    _mca_roi_lo[int(idx)] = int(val)

def mca_set_roi_hi(idx, val):
    _mca_roi_hi[int(idx)] = int(val)

def mca_set_roi_name(idx, val):
    _mca_roi_name[int(idx)] = str(val).strip('\x00')

def mca_set_calo(val):
    global _mca_calo
    _mca_calo = float(val)

def mca_set_cals(val):
    global _mca_cals
    _mca_cals = float(val)

def mca_get_ertm():
    """Elapsed real time: integration_time * scans_to_average (seconds)."""
    return _cached_itime * 1e-6 * max(1, _cached_nscans)

def mca_get_eltm():
    """Live time approximation — same as real time for this detector."""
    return mca_get_ertm()

def mca_get_acqg():
    """Always 0 (Done) — QEPro is synchronous, never mid-acquisition here."""
    return 0


# ==================================================================
# MCA bridge -- push spectrum into drvSoftMca via loopback CA
# ==================================================================
# Initialized lazily after iocInit so the CA server is running.
# Fails silently if pyepics is not installed.
# Override ioc_prefix from the startup script before iocInit:
#   pydev("ioc_prefix = '15ID:UVVis:'")

ioc_prefix = "15ID:UVVis:"
_mca_pv = None
_mca_init_tried = False


def _update_mca(data):
    """Non-blocking CA push of spectrum into the MCA1 waveform record.

    Now targets the pydev MCA1 waveform (OceanopticsMCA_pydev.db) which
    always exists, so the PV connects immediately after iocInit.
    The global `spectrum` array is already updated by get_spectrum() before
    this is called, so the push is redundant for local readers — it mainly
    ensures any external CA monitors on MCA1 see the update.
    """
    global _mca_pv, _mca_init_tried
    if _mca_init_tried and _mca_pv is None:
        return
    if not _mca_init_tried:
        _mca_init_tried = True
        try:
            import epics
            _mca_pv = epics.PV(ioc_prefix + "MCA1", auto_monitor=False)
        except Exception:
            return
    if not _mca_pv.connected:
        return
    try:
        _mca_pv.put(list(data), wait=False)
    except Exception:
        pass


atexit.register(cleanup_function)
