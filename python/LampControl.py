import time

# Track lamp states in software
d2_state = False
halogen_state = False


def _pulse_gpio(pin, width=0.1):
    """
    Send a LOW->HIGH->LOW pulse to a GPIO pin
    """
    adv.gpio_set_output_enable1(pin, True)

    adv.gpio_set_value1(pin, False)
    time.sleep(0.05)

    adv.gpio_set_value1(pin, True)   # rising edge
    time.sleep(width)

    adv.gpio_set_value1(pin, False)


# -------------------------
# Deuterium lamp control
# -------------------------

def toggle_deuterium():
    global d2_state
    _pulse_gpio(7)
    d2_state = not d2_state
    return d2_state


def set_deuterium(flag):
    """
    Turn deuterium lamp ON/OFF
    """
    global d2_state

    if flag != d2_state:
        _pulse_gpio(7)
        d2_state = flag

    return d2_state


def get_deuterium_state():
    return d2_state


# -------------------------
# Halogen lamp control
# -------------------------

def toggle_halogen():
    global halogen_state
    _pulse_gpio(8)
    halogen_state = not halogen_state
    return halogen_state


def set_halogen(flag):
    """
    Turn halogen lamp ON/OFF
    """
    global halogen_state

    if flag != halogen_state:
        _pulse_gpio(8)
        halogen_state = flag

    return halogen_state


def get_halogen_state():
    return halogen_state