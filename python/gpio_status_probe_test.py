"""Read-only GPIO observer for locating the DH-2000 lamp-status lines.

WHY THIS IS OBSERVE-ONLY
------------------------
Which QE Pro GPIO (if any) carries D_GOOD / HAL_GOOD through the installed
DH2-HPX-CBL-DB15 is NOT established -- no published pinout exists for that
cable. So this script never changes a pin direction, never writes a pin, and
never toggles a lamp. It samples what is already there and reports which
lines look genuinely connected.

VALIDATION RULE
---------------
D_GOOD and HAL_GOOD are ACTIVE LOW, so a disconnected input that floats or
is pulled low reads exactly like "lamp on". A single LOW sample therefore
proves nothing. A pin is only reported as usable once it has been observed
in BOTH states -- positive evidence the line is driven rather than dangling.
Pins configured as outputs are reported as such and never interpreted, since
reading one back tells you what you drove, not what the lamp is doing.

USAGE
-----
  From inside the IOC's Python (preferred -- no competing USB connection).
  The UV-Vis control module already holds `adv = odev.Advanced`:
      from gpio_status_probe import watch
      watch(adv)                      # runs until Ctrl-C

  Standalone (only when the IOC does NOT hold the device). The IOC opens
  device 2, so name it explicitly:
      python gpio_status_probe.py --device 2

  Three entry points:
      watch(adv)         live monitor, runs until Ctrl-C. Use this while you
                         switch the lamps on and off by hand. Announces the
                         first HIGH on each line, which is what proves the
                         pull-up is live and the cable mapping is right.
      probe_status(adv)  same reading over a fixed --seconds window.
      probe(adv)         sweep every GPIO looking for driven lines. Only
                         needed if the cable map ever has to be re-derived.
"""

import argparse
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# DH2-HPX-CBL-DB15 wire map -- continuity-measured on the installed cable.
#
# There is NO published pinout for this cable. Only HR4-CBL-DB15 is documented
# and it is wired differently, so the table below is the only source of truth.
# Do not substitute the HR4 table.
#
#   DB15  signal                      J2   GPIO   how known
#   ----  --------------------------  ---  -----  ---------------------------
#     2   D2 toggle       (to lamp)    26     7   existing, working output
#     3   Halogen toggle  (to lamp)    28     8   existing, working output
#     5   HAL_BAD         (from lamp)   9     1   measured
#     8   D_BAD           (from lamp)   3     2   measured
#     9   D_GOOD          (from lamp)  11     3   measured
#    13   Lamp Enable     (to lamp)    25    --   measured; OUT OF SCOPE.
#                                                 J2 25 is a dedicated Lamp
#                                                 Enable output, not a GPIO,
#                                                 and we do not drive it --
#                                                 the GPIO 7/8 toggles above
#                                                 are the control path.
#    14   HAL_GOOD        (from lamp)  16     4   measured
#    15   SHTR_STAT       (from lamp)  18     5   PREDICTED, not measured
#
# The cable routes the DH-2000's five status outputs, in ascending DB15 order,
# onto GPIO 1-5. Four of the five are measured; the fifth follows the pattern.
#
# GPIO index -> J2 contact (identical in QE Pro MNL-0000 Rev 0 and MNL-1009
# Rev A): 0=7, 1=9, 2=3, 3=11, 4=16, 5=18, 6=22, 7=26, 8=28, 9=30.
# ---------------------------------------------------------------------------

# db15 -> (signal, j2, gpio, from_lamp, measured)
CABLE_MAP = {
    2:  ("D2_TOGGLE",  26, 7,    False, True),
    3:  ("HAL_TOGGLE", 28, 8,    False, True),
    5:  ("HAL_BAD",     9, 1,    True,  True),
    8:  ("D_BAD",       3, 2,    True,  True),
    9:  ("D_GOOD",     11, 3,    True,  True),
    13: ("LAMP_ENABLE", 25, None, False, True),
    14: ("HAL_GOOD",   16, 4,    True,  True),
    15: ("SHTR_STAT",  18, 5,    True,  False),
}

# Write-only toggle outputs driven by the existing, working lamp control code.
# These are NOT readbacks: reading one returns whatever was last driven onto
# it, which says nothing about the lamp. They are never sampled or interpreted
# -- this dict exists only to label a pin that turns up configured as an
# output during pre-flight.
KNOWN_OUTPUTS = {7: "D2 toggle (pulse)", 8: "Halogen toggle (pulse)"}

# The four additional signals this script exists to read, plus the predicted
# fifth. Interpretation is only ever printed for a pin that passed the
# both-states test, so a floating or unpowered LOW is never read as "lamp on".
# gpio -> (name, active_level, text when asserted, text when not asserted,
#          mapping measured?)
CONFIRMED_INPUTS = {
    1: ("HAL_BAD",   "LOW",  "halogen FAULT",
        "halogen healthy (says nothing about on/off)",            True),
    2: ("D_BAD",     "LOW",  "deuterium FAULT",
        "deuterium healthy (says nothing about on/off)",           True),
    3: ("D_GOOD",    "LOW",  "deuterium ON",  "deuterium OFF",    True),
    4: ("HAL_GOOD",  "LOW",  "halogen ON",    "halogen OFF",      True),
    5: ("SHTR_STAT", "HIGH", "shutter OPEN",  "shutter CLOSED",   False),
}

# The four measured status lines, in reporting order.
STATUS_GPIOS = [3, 1, 4, 2]          # D_GOOD, HAL_BAD, HAL_GOOD, D_BAD
PREDICTED_GPIOS = [5]                # SHTR_STAT -- opt in with include_predicted

# DB15 pin 1 must be pulled up externally for ANY of the five outputs above to
# read meaningfully; they are open-collector. The DH-2000 manual requires
# 2.6-5 V there. Use 3.3 V: the QE Pro manual states no voltage tolerance for
# GPIO pins, and VOUT (J2 12/14) is listed only as "output power pin" with no
# voltage -- the 4.5-5.5 V figure elsewhere in that manual is the external
# power-supply accessory's rating, not a VOUT spec.
PULLUP_NOTE = ("DB15 pin 1 pull-up (2.6-5 V, use 3.3 V) must be energised or "
               "every line below sits at a meaningless steady LOW.")


# Which GPIO getters this device actually implements. Probed once, cached.
# Some QE Pro firmware answers "Command not supported by device." to one or
# more of these while still honouring the setters the lamp toggles use.
_CAPS = None

_CAP_PROBES = (
    ("value1",     "gpio_get_value1(bit)"),
    ("value2",     "gpio_get_value2()"),
    ("enable1",    "gpio_get_output_enable1(bit)"),
    ("enable2",    "gpio_get_output_enable2()"),
    ("alternate1", "gpio_get_output_alternate1(bit)"),
)


def _capabilities(adv, pins, force=False):
    """Probe each GPIO getter once. Value None means supported.

    Read-only: every call here is a getter. Cached, so the per-sample loop
    does not pay for it.
    """
    global _CAPS
    if _CAPS is not None and not force:
        return _CAPS
    bit = pins[0]
    calls = {
        "value1": lambda: adv.gpio_get_value1(bit),
        "value2": lambda: adv.gpio_get_value2(),
        "enable1": lambda: adv.gpio_get_output_enable1(bit),
        "enable2": lambda: adv.gpio_get_output_enable2(),
        "alternate1": lambda: adv.gpio_get_output_alternate1(bit),
    }
    caps = {}
    for key, _label in _CAP_PROBES:
        try:
            calls[key]()
            caps[key] = None
        except Exception as exc:
            caps[key] = repr(exc)
    _CAPS = caps
    return caps


def _caps_report(caps):
    """Human summary plus whether any value read is possible at all."""
    lines = []
    for key, label in _CAP_PROBES:
        err = caps.get(key)
        lines.append("  %-28s %s" % (label, "OK" if err is None
                                     else "UNSUPPORTED  %s" % err))
    can_read = caps.get("value1") is None or caps.get("value2") is None
    can_dir = caps.get("enable1") is None or caps.get("enable2") is None
    return "\n".join(lines), can_read, can_dir


def _snapshot(adv, pins, caps=None):
    """One read of value/direction/alternate per pin. Never writes.

    Each field is read independently, so an unsupported getter degrades that
    field only. "error" refers strictly to the VALUE read -- the levels are
    the measurement; direction and alternate are context. A direction that
    cannot be read comes back as None (unknown), never as False.
    """
    if caps is None:
        caps = _capabilities(adv, pins)

    # Bulk variants: one USB round trip covering every pin.
    vmask = dmask = None
    if caps.get("value1") is not None and caps.get("value2") is None:
        try:
            vmask = adv.gpio_get_value2()
        except Exception:
            vmask = None
    if caps.get("enable1") is not None and caps.get("enable2") is None:
        try:
            dmask = adv.gpio_get_output_enable2()
        except Exception:
            dmask = None

    row = {}
    for bit in pins:
        value, error = None, None
        if caps.get("value1") is None:
            try:
                value = bool(adv.gpio_get_value1(bit))
            except Exception as exc:
                error = repr(exc)
        elif vmask is not None:
            value = bool((vmask >> bit) & 1)
        else:
            error = "no supported level read: %s" % caps.get("value1")

        is_output = None
        if caps.get("enable1") is None:
            try:
                is_output = bool(adv.gpio_get_output_enable1(bit))
            except Exception:
                is_output = None
        elif dmask is not None:
            is_output = bool((dmask >> bit) & 1)

        alternate = None                  # cosmetic: never an error
        if caps.get("alternate1") is None:
            try:
                alternate = bool(adv.gpio_get_output_alternate1(bit))
            except Exception:
                alternate = None

        row[bit] = {"value": value, "is_output": is_output,
                    "alternate": alternate, "error": error}
    return row


def probe(adv, seconds=30.0, period=0.05):
    """Sample every GPIO and report which lines are actually driven.

    period defaults to 50 ms (20 Hz) so the 1 Hz D_GOOD startup oscillation
    gets about 20 samples per cycle.
    """
    try:
        count = adv.get_gpio_pin_count()
    except Exception as exc:
        print("FATAL: get_gpio_pin_count() failed: %r" % (exc,))
        return None

    pins = list(range(count))
    print("GPIO pin count reported by device: %d" % count)
    print("Sampling %.0f s at %.0f Hz (1 Hz signal -> ~%.0f samples/cycle)\n"
          % (seconds, 1.0 / period, 1.0 / period))

    seen_high = dict((b, False) for b in pins)
    seen_low = dict((b, False) for b in pins)
    edges = dict((b, 0) for b in pins)
    errors = dict((b, 0) for b in pins)

    first = _snapshot(adv, pins)
    last = dict((b, first[b]["value"]) for b in pins)

    print("Initial pin configuration (read-only):")
    for b in pins:
        s = first[b]
        if s["error"]:
            print("  GPIO %-2d ERROR %s" % (b, s["error"]))
            continue
        role = KNOWN_OUTPUTS.get(b, "")
        print("  GPIO %-2d dir=%s alt=%s value=%s%s"
              % (b,
                 "OUT" if s["is_output"] else "IN ",
                 "Y" if s["alternate"] else "N",
                 "H" if s["value"] else "L",
                 "   <- " + role if role else ""))
    print("")

    t_end = time.time() + seconds
    while time.time() < t_end:
        row = _snapshot(adv, pins)
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        changed = []
        for b in pins:
            s = row[b]
            if s["error"]:
                errors[b] += 1
                continue
            if s["value"]:
                seen_high[b] = True
            else:
                seen_low[b] = True
            if last[b] is not None and s["value"] != last[b]:
                edges[b] += 1
                changed.append(b)
            last[b] = s["value"]
        if changed:
            bits = "".join(
                "?" if row[b]["error"] else ("H" if row[b]["value"] else "L")
                for b in pins)
            print("%s  %s   transition on GPIO %s" % (stamp, bits, changed))
        time.sleep(period)

    print("\n" + "=" * 68)
    print("VERDICT  (only BOTH-states pins are trustworthy as status inputs)")
    print("=" * 68)
    usable = []
    for b in pins:
        if errors[b]:
            verdict = "ERROR on %d reads - cannot assess" % errors[b]
        elif first[b]["is_output"]:
            verdict = ("OUTPUT - not a status input (%s)"
                       % KNOWN_OUTPUTS.get(b, "driven by something"))
        elif seen_high[b] and seen_low[b]:
            verdict = ("LIVE - both states seen, %d transitions -> USABLE"
                       % edges[b])
            usable.append(b)
        elif seen_high[b]:
            verdict = "always HIGH - unverified (idle pull-up, or nothing attached)"
        else:
            verdict = "always LOW - UNVERIFIED, do NOT read as 'lamp on'"
        print("  GPIO %-2d %s" % (b, verdict))


    print("")
    print("INTERPRETED STATUS")
    for bit in sorted(CONFIRMED_INPUTS):
        name, active, on_txt, off_txt, verified = CONFIRMED_INPUTS[bit]
        low_txt = on_txt if active == "LOW" else off_txt
        high_txt = off_txt if active == "LOW" else on_txt
        tag = "" if verified else "  [mapping PREDICTED, not measured]"
        if bit not in pins:
            print("  %-9s GPIO %d absent on this device" % (name, bit))
        elif errors[bit]:
            print("  %-9s READ ERROR - status unknown" % name)
        elif first[bit]["is_output"]:
            print("  %-9s GPIO %d is an OUTPUT - refusing to interpret" % (name, bit))
        elif not (seen_high[bit] and seen_low[bit]):
            stuck = "LOW" if seen_low[bit] else "HIGH"
            print("  %-9s UNVERIFIED (stuck %s) - no pull-up or not connected;"
                  " NOT reporting a state%s" % (name, stuck, tag))
        else:
            now = last[bit]
            print("  %-9s %-4s -> %s%s" % (name, "HIGH" if now else "LOW",
                                           high_txt if now else low_txt, tag))
    print("\nUsable candidate input channels: %s" % (usable or "NONE FOUND"))
    if not usable:
        print("  No GPIO changed state. Either the status lines are not routed\n"
              "  through this cable, the pull-up supply (DH-2000 contact 1) is\n"
              "  absent, or nothing was switched during the observation window.")
    return {"pins": pins, "usable": usable, "edges": edges,
            "seen_high": seen_high, "seen_low": seen_low, "errors": errors}


# ---------------------------------------------------------------------------
# Targeted probe of the four measured status lines
# ---------------------------------------------------------------------------
#
# CONFIDENCE MODEL -- why a bare LOW is never enough
# --------------------------------------------------
# All five DH-2000 status outputs are open-collector: the lamp can only ever
# pull a line DOWN. The only thing that can make one read HIGH is the external
# pull-up on DB15 pin 1. Two consequences drive every verdict below:
#
#   1. If NO line is ever seen HIGH during the window, the pull-up is not
#      proven present, and every LOW is meaningless -- an unpowered or
#      disconnected input looks exactly like "lamp on". Nothing is reported.
#
#   2. Once ANY line has been seen HIGH, the pull-up is proven. A line that
#      has itself been seen HIGH at least once is proven connected AND
#      released, so its current level is trustworthy -> CONFIRMED.
#      A line that has sat LOW for the whole window is still ambiguous:
#      continuously asserted, or shorted/miswired to ground -> PROBABLE only.
#
# D_GOOD additionally oscillates at ~1 Hz while the deuterium lamp warms up,
# so a detected ~1 Hz square wave is reported as WARMING UP rather than ON.
# Sampling at 20 Hz gives ~20 samples per cycle.
#
# This function never writes a pin and never changes a direction. A line found
# configured as an output is reported and skipped, not reconfigured.

# ---------------------------------------------------------------------------
# Shared rules -- used by both the fixed-window probe and the live monitor so
# the two can never drift apart.
# ---------------------------------------------------------------------------

def _oscillation_hz(times):
    """Frequency of a square wave, from its edge timestamps. None if too few."""
    if len(times) < 4:
        return None
    gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
    mean_gap = sum(gaps) / len(gaps)
    if mean_gap <= 0:
        return None
    return 0.5 / mean_gap                 # two edges per cycle


def _is_osc(hz):
    """True for roughly the 1 Hz deuterium warm-up blink."""
    return hz is not None and 0.6 <= hz <= 1.6


def _verdict(bit, level, pullup_proven, seen_high, seen_low, errors, hz):
    """Confidence + human text for one status line. See CONFIDENCE MODEL above.

    Order matters: read failures outrank everything, then the pull-up gate,
    then oscillation, then the both-states proof.
    """
    name, active, on_txt, off_txt, measured = CONFIRMED_INPUTS[bit]
    asserted = (level is False) if active == "LOW" else (level is True)

    if errors and not seen_high and not seen_low:
        return "ERROR", "read failed on every sample"
    if errors:
        return "ERROR", "%d failed reads -- treat with suspicion" % errors
    if not pullup_proven:
        return "UNVALIDATED", "no pull-up proven; NOT reporting a state"
    if _is_osc(hz):
        if name == "D_GOOD":
            return "CONFIRMED", ("deuterium WARMING UP (~%.2f Hz oscillation)"
                                 % hz)
        return "CONFIRMED", ("oscillating at ~%.2f Hz -- unexpected on this"
                             " line" % hz)
    if seen_high:
        # This line was itself seen to release and be pulled up: proven live.
        return "CONFIRMED", (on_txt if asserted else off_txt)
    # Pull-up exists but this line never let go: asserted, or shorted to
    # ground. Those are indistinguishable, so do not claim certainty.
    return "PROBABLE", ("%s  (or line shorted to ground -- it never released)"
                        % on_txt)


def _where(bit):
    """'DB15 9 -> J2 11 -> GPIO 3' for a status GPIO."""
    db15 = [d for d, v in CABLE_MAP.items() if v[2] == bit]
    if not db15:
        return "GPIO %d (not in cable map)" % bit
    return "DB15 %d -> J2 %d -> GPIO %d" % (db15[0], CABLE_MAP[db15[0]][1], bit)


def _preflight(adv, pins, title, subtitle=None):
    """Report pin configuration without changing it.

    Returns (usable_pins, first_snapshot, skipped_outputs, preflight_errors)
    or None if the device cannot be interrogated. A pin that fails to read is
    KEPT -- it may recover, and a signal missing from the report would look
    just like a lamp that is off. A pin configured as an output is dropped,
    never reconfigured.
    """
    try:
        count = adv.get_gpio_pin_count()
    except Exception as exc:
        print("FATAL: get_gpio_pin_count() failed: %r" % (exc,))
        print("Cannot confirm these GPIOs exist; refusing to read blind.")
        return None

    missing = [b for b in pins if b >= count]
    if missing:
        print("WARNING: device reports %d GPIOs; skipping out-of-range %s"
              % (count, missing))
        pins = [b for b in pins if b < count]
    if not pins:
        print("FATAL: none of the status GPIOs exist on this device.")
        return None

    print("=" * 70)
    print(title)
    print("=" * 70)
    print(PULLUP_NOTE)
    if subtitle:
        print(subtitle)
    print("")

    caps = _capabilities(adv, pins, force=True)
    report, can_read, can_dir = _caps_report(caps)
    print("GPIO getters supported by this device:")
    print(report)
    if not can_read:
        print("")
        print("FATAL: this device supports neither gpio_get_value1 nor")
        print("  gpio_get_value2, so no pin level can be read at all. The")
        print("  lamp toggles still work because they only SET pins. Status")
        print("  reading is not possible through this API on this firmware.")
        return None
    if not can_dir:
        print("")
        print("NOTE: pin direction cannot be read on this device, so it can")
        print("  not be verified that GPIO %s are inputs. Levels are still" % pins)
        print("  readable. Nothing is reconfigured either way.")
    print("")

    first = _snapshot(adv, pins, caps)
    print("Pre-flight:")
    usable_pins = []
    skipped_outputs = {}
    preflight_errors = {}
    for b in pins:
        s = first[b]
        name = CONFIRMED_INPUTS[b][0]
        where = _where(b)
        if s["error"]:
            print("  %-9s %-28s READ ERROR %s -- will keep trying"
                  % (name, where, s["error"]))
            preflight_errors[b] = s["error"]
            usable_pins.append(b)
        elif s["is_output"] is True:
            reason = KNOWN_OUTPUTS.get(b, "driven by something")
            print("  %-9s %-28s configured as OUTPUT -- skipping, will not"
                  " reconfigure (%s)" % (name, where, reason))
            skipped_outputs[b] = reason
        else:
            # is_output False, or None when the device cannot report direction
            dirtext = "input" if s["is_output"] is False else "dir unknown"
            print("  %-9s %-28s %s, currently %s"
                  % (name, where, dirtext, "HIGH" if s["value"] else "LOW"))
            usable_pins.append(b)
    print("")
    if not usable_pins:
        print("FATAL: no status line is readable as an input.")
        return None
    return usable_pins, first, skipped_outputs, preflight_errors


def _final_report(usable_pins, last, seen_high, seen_low, errors, edges,
                  edge_times, skipped_outputs, osc_window=3.0):
    """Print the verdict block and per-lamp summary. Returns the results dict.

    osc_window  only edges within this many seconds of the end of observation
                count towards the blink test. Judging it over the whole
                session would keep reporting WARMING UP long after a lamp had
                warmed up and settled, since the warm-up edges never expire.
    """
    pullup_proven = any(seen_high[b] for b in usable_pins)
    t_ref = time.time()

    print("\n" + "=" * 70)
    print("RESULT")
    print("=" * 70)
    if not pullup_proven:
        print("PULL-UP NOT PROVEN: no line was ever HIGH.")
        print("  DB15 pin 1 is most likely not energised (or no status line is")
        print("  actually routed through this cable). Every reading below is")
        print("  UNVALIDATED -- an unpowered input reads LOW exactly like an")
        print("  asserted one. No lamp state is being reported.")
    else:
        print("PULL-UP PROVEN: at least one line read HIGH, which an")
        print("  open-collector output cannot cause. DB15 pin 1 is energised.")
    print("")

    results = {}
    for b in usable_pins:
        name, active, on_txt, off_txt, measured = CONFIRMED_INPUTS[b]
        tag = "" if measured else "   [mapping PREDICTED, not measured]"
        recent = [t for t in edge_times[b] if t_ref - t <= osc_window]
        hz = _oscillation_hz(recent)
        conf, state = _verdict(b, last[b], pullup_proven, seen_high[b],
                               seen_low[b], errors[b], hz)
        print("  %-9s %-11s %s%s" % (name, conf, state, tag))
        results[name] = {"gpio": b, "confidence": conf, "state": state,
                         "level": last[b], "edges": edges[b], "hz": hz,
                         "hz_session": _oscillation_hz(edge_times[b]),
                         "seen_high": seen_high[b], "seen_low": seen_low[b],
                         "errors": errors[b], "mapping_measured": measured}

    for b, reason in sorted(skipped_outputs.items()):
        name = CONFIRMED_INPUTS[b][0]
        results[name] = {"gpio": b, "confidence": "OUTPUT",
                         "state": "configured as an output (%s)" % reason,
                         "level": None, "edges": 0, "hz": None,
                         "seen_high": False, "seen_low": False, "errors": 0,
                         "mapping_measured": CONFIRMED_INPUTS[b][4]}

    print("")
    print("LAMPS")
    for lamp, good, bad in (("Deuterium", "D_GOOD", "D_BAD"),
                            ("Halogen", "HAL_GOOD", "HAL_BAD")):
        g = results.get(good)
        f = results.get(bad)
        if g is None:
            print("  %-10s UNKNOWN  (%s not sampled)" % (lamp, good))
            continue
        if g["confidence"] in ("UNVALIDATED", "ERROR", "OUTPUT"):
            print("  %-10s UNKNOWN  (%s: %s)" % (lamp, good, g["state"]))
            continue
        fault = (f is not None and f["confidence"] in ("CONFIRMED", "PROBABLE")
                 and f["level"] is False)
        verdict = g["state"]
        if fault:
            verdict += "  + FAULT asserted on %s" % bad
        print("  %-10s %-9s %s" % (lamp, g["confidence"], verdict))

    return results


def probe_status(adv, seconds=60.0, period=0.05, include_predicted=False,
                 heartbeat=1.0):
    """Read the four measured DH-2000 status lines for a fixed window.

    adv               Spectrometer.Advanced of an ALREADY-OPEN device.
    seconds           observation window.
    period            sample interval; 0.05 s = 20 Hz, ~20 samples per cycle
                      of the 1 Hz deuterium warm-up indication.
    include_predicted also sample GPIO 5 (SHTR_STAT), whose mapping is
                      predicted from the cable pattern but not measured.
    heartbeat         seconds between timestamped raw lines; transitions are
                      always printed regardless.

    For an open-ended session where you flip the lamp switches by hand, use
    watch() instead. Read-only throughout.
    """
    pins = list(STATUS_GPIOS)
    if include_predicted:
        pins += PREDICTED_GPIOS

    pre = _preflight(
        adv, pins,
        "DH-2000 STATUS PROBE  (read-only: no writes, no direction changes)",
        "Window %.0f s at %.0f Hz  (~%.0f samples per 1 Hz cycle)"
        % (seconds, 1.0 / period, 1.0 / period))
    if pre is None:
        return None
    usable_pins, first, skipped_outputs, preflight_errors = pre
    caps = _capabilities(adv, usable_pins)

    seen_high = dict((b, False) for b in usable_pins)
    seen_low = dict((b, False) for b in usable_pins)
    edges = dict((b, 0) for b in usable_pins)
    edge_times = dict((b, []) for b in usable_pins)
    errors = dict((b, 1 if b in preflight_errors else 0) for b in usable_pins)
    last = dict((b, first[b]["value"]) for b in usable_pins)

    def raw_bits(row):
        return " ".join(
            "%s=%s" % (CONFIRMED_INPUTS[b][0],
                       "?" if row[b]["error"] else ("H" if row[b]["value"] else "L"))
            for b in usable_pins)

    print("Timestamped samples (transitions, plus a line every %.1f s):"
          % heartbeat)
    t0 = time.time()
    t_end = t0 + seconds
    next_beat = t0
    while True:
        now = time.time()
        if now >= t_end:
            break
        row = _snapshot(adv, usable_pins, caps)
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        changed = []
        for b in usable_pins:
            s = row[b]
            if s["error"]:
                errors[b] += 1
                continue
            if s["value"]:
                seen_high[b] = True
            else:
                seen_low[b] = True
            if last[b] is not None and s["value"] != last[b]:
                edges[b] += 1
                edge_times[b].append(now)
                changed.append(CONFIRMED_INPUTS[b][0])
            last[b] = s["value"]
        if changed or now >= next_beat:
            note = ("  <- transition: %s" % ", ".join(changed)) if changed else ""
            print("  %s  %s%s" % (stamp, raw_bits(row), note))
            while next_beat <= now:
                next_beat += heartbeat
        time.sleep(period)

    return _final_report(usable_pins, last, seen_high, seen_low, errors,
                         edges, edge_times, skipped_outputs)


# ---------------------------------------------------------------------------
# Live monitor -- for flipping the lamp switches by hand
# ---------------------------------------------------------------------------
#
# Runs until Ctrl-C. Prints a line the moment any status line changes, and
# announces two milestones as they happen:
#
#   * PULL-UP PROVEN     the first HIGH seen on any line. Nothing but the
#                        external pull-up can produce a HIGH, so this is
#                        positive proof DB15 pin 1 is energised.
#   * LINE CONFIRMED     the first HIGH on a particular line, which proves
#                        that line is connected, correctly mapped, and
#                        releases. This is what validates the cable map --
#                        including the predicted SHTR_STAT entry.
#
# The 1 Hz deuterium warm-up blink would otherwise flood the log with two
# lines a second, so a line oscillating in that band is collapsed into one
# summary line every couple of seconds until it settles.
#
# Read-only: no pin written, no direction changed, no lamp toggled. You do the
# toggling by hand, at the front panel or with your existing toggle PVs.

def watch(adv, period=0.05, heartbeat=5.0, include_predicted=False,
          stop_after=None, osc_report_every=2.0, osc_window=3.0):
    """Monitor the status lines until Ctrl-C while you switch the lamps.

    period           sample interval; 0.05 s = 20 Hz.
    heartbeat        seconds between idle "still here" lines.
    include_predicted also watch GPIO 5 (SHTR_STAT), mapping predicted.
    stop_after       optional seconds cap; None means run until Ctrl-C.
    osc_report_every rate-limit for a line blinking at ~1 Hz.
    osc_window       seconds of edge history used to detect blinking.

    Returns the same results dict as probe_status(). Read-only throughout.
    """
    pins = list(STATUS_GPIOS)
    if include_predicted:
        pins += PREDICTED_GPIOS

    limit = "until Ctrl-C" if stop_after is None else ("for %.0f s" % stop_after)
    pre = _preflight(
        adv, pins,
        "DH-2000 LIVE STATUS MONITOR  (read-only: nothing is driven)",
        "Sampling at %.0f Hz %s." % (1.0 / period, limit))
    if pre is None:
        return None
    usable_pins, first, skipped_outputs, preflight_errors = pre
    caps = _capabilities(adv, usable_pins)

    seen_high = dict((b, False) for b in usable_pins)
    seen_low = dict((b, False) for b in usable_pins)
    edges = dict((b, 0) for b in usable_pins)
    edge_times = dict((b, []) for b in usable_pins)
    errors = dict((b, 1 if b in preflight_errors else 0) for b in usable_pins)
    last = dict((b, first[b]["value"]) for b in usable_pins)
    recent = dict((b, []) for b in usable_pins)
    osc_last = dict((b, 0.0) for b in usable_pins)
    confirmed = set()
    pullup_announced = False

    if not any(first[b]["value"] for b in usable_pins):
        print("All lines are LOW right now. If the DB15 pin 1 pull-up is not")
        print("energised, switching the lamps will change NOTHING here -- both")
        print("on and off read LOW. A single HIGH is what proves otherwise.")
        print("")

    print("GO AHEAD: turn the lamps on and off. Ctrl-C when finished.")
    print("-" * 70)

    def raw_bits(row):
        return " ".join(
            "%s=%s" % (CONFIRMED_INPUTS[b][0],
                       "?" if row[b]["error"] else ("H" if row[b]["value"] else "L"))
            for b in usable_pins)

    t0 = time.time()
    next_beat = t0 + heartbeat
    try:
        while True:
            now = time.time()
            if stop_after is not None and now - t0 >= stop_after:
                break
            row = _snapshot(adv, usable_pins, caps)
            stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]

            for b in usable_pins:
                s = row[b]
                if s["error"]:
                    errors[b] += 1
                    print("  %s  %-9s READ ERROR %s"
                          % (stamp, CONFIRMED_INPUTS[b][0], s["error"]))
                    continue

                if s["value"]:
                    if not pullup_announced:
                        pullup_announced = True
                        print("")
                        print("*** PULL-UP PROVEN at %s -- a line read HIGH,"
                              " which the lamp" % stamp)
                        print("    cannot cause. DB15 pin 1 is energised and"
                              " these readings are real.")
                        print("")
                    if b not in confirmed:
                        confirmed.add(b)
                        name, _a, _on, _off, measured = CONFIRMED_INPUTS[b]
                        note = ("" if measured
                                else "  (this VALIDATES the predicted mapping)")
                        print("*** %s CONFIRMED LIVE -- %s released and pulled"
                              " up%s" % (name, _where(b), note))
                    seen_high[b] = True
                else:
                    seen_low[b] = True

                if last[b] is None or s["value"] == last[b]:
                    last[b] = s["value"]
                    continue

                # a real transition
                edges[b] += 1
                edge_times[b].append(now)
                recent[b].append(now)
                recent[b] = [t for t in recent[b] if now - t <= osc_window]
                prev, last[b] = last[b], s["value"]

                if len(recent[b]) >= 4:
                    # blinking; collapse to a periodic summary
                    if now - osc_last[b] >= osc_report_every:
                        osc_last[b] = now
                        hz = _oscillation_hz(recent[b])
                        conf, state = _verdict(b, s["value"], True,
                                               seen_high[b], seen_low[b],
                                               errors[b], hz)
                        print("  %s  %-9s blinking ~%.2f Hz   %s"
                              % (stamp, CONFIRMED_INPUTS[b][0],
                                 hz if hz else 0.0, state))
                    continue

                conf, state = _verdict(b, s["value"], pullup_announced,
                                       seen_high[b], seen_low[b], errors[b],
                                       None)
                print("  %s  %-9s %s->%s  %-42s %s"
                      % (stamp, CONFIRMED_INPUTS[b][0],
                         "H" if prev else "L", "H" if s["value"] else "L",
                         state, conf))

            if now >= next_beat:
                print("  %s  %s" % (stamp, raw_bits(row)))
                while next_beat <= now:
                    next_beat += heartbeat
            time.sleep(period)
    except KeyboardInterrupt:
        print("\n\nstopped by user after %.0f s" % (time.time() - t0))

    return _final_report(usable_pins, last, seen_high, seen_low, errors,
                         edges, edge_times, skipped_outputs)


def _standalone(seconds, period, index, mode, include_predicted):
    from oceandirect.OceanDirectAPI import OceanDirectAPI

    api = OceanDirectAPI()
    api.find_usb_devices()
    ids = api.get_device_ids()
    print("Device ids visible: %s" % (ids,))
    if not ids:
        print("FATAL: no device found.")
        return

    dev_id = ids[0] if index is None else index
    print("Opening device id %s (abort now if the IOC already owns it)\n" % dev_id)
    try:
        dev = api.open_device(dev_id)
    except Exception as exc:
        print("FATAL: open_device(%s) failed: %r" % (dev_id, exc))
        print("If the IOC holds the spectrometer, import probe_status() into "
              "the IOC instead of running this standalone.")
        return
    try:
        print("Serial: %s" % dev.get_serial_number())
        if mode == "watch":
            watch(dev.Advanced, period=period,
                  include_predicted=include_predicted)
        elif mode == "status":
            probe_status(dev.Advanced, seconds=seconds, period=period,
                         include_predicted=include_predicted)
        else:
            probe(dev.Advanced, seconds=seconds, period=period)
    finally:
        api.close_device(dev_id)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Read-only QE Pro GPIO observer")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--period", type=float, default=0.05)
    ap.add_argument("--device", type=int, default=None,
                    help="device id; default is the first one found. The "
                         "UV-Vis IOC opens device 2, so pass --device 2 to "
                         "be sure of hitting the same spectrometer.")
    ap.add_argument("--mode", choices=("watch", "status", "survey"),
                    default="watch",
                    help="watch: monitor the status lines until Ctrl-C while "
                         "you switch the lamps by hand (default). "
                         "status: same reading, fixed --seconds window. "
                         "survey: sweep every GPIO to hunt for driven lines.")
    ap.add_argument("--include-predicted", action="store_true",
                    help="also sample GPIO 5 (SHTR_STAT), mapping predicted "
                         "from the cable pattern but not measured")
    a = ap.parse_args()
    _standalone(a.seconds, a.period, a.device, a.mode, a.include_predicted)
