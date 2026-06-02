"""
test_hardware.py, manual hardware component tests.

Run with:
    cd ~/moneymaker
    sudo ~/moneymaker/venv/bin/python3 tests/test_hardware.py

At the start of each test:  press Enter to run, or type 's' to skip.
During any step:            press Enter to skip to the next step.
At any time:                press Ctrl+C to exit.
"""

import sys
import time
import os
import select

# ── Path setup ────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ══════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════

def pause(seconds: float = 1.5, message: str = "") -> None:
    if message:
        print(f"    → {message}")
    print(f"    (waiting {seconds}s — press Enter to skip)", end="\r", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], seconds)
    if ready:
        sys.stdin.readline()
    print(" " * 60, end="\r")


def ask_to_begin(test_name: str) -> bool:
    """
    Ask the user whether to run a test.
    Returns True to run, False to skip.
    """
    print()
    response = input(f"  Press Enter to begin {test_name}, or type 's' to skip: ")
    if response.strip().lower() == "s":
        print("  Skipped.")
        return False
    return True


def section(title: str) -> None:
    print()
    print("=" * 55)
    print(f"  {title}")
    print("=" * 55)


def step(description: str) -> None:
    print(f"\n  [ ] {description}")


def ok(description: str = "") -> None:
    msg = "  [✓] OK"
    if description:
        msg += f" — {description}"
    print(msg)


# ══════════════════════════════════════════════════════════════
#  TEST 1 — MAX7219 8-Digit Display
# ══════════════════════════════════════════════════════════════

def test_display() -> None:
    section("TEST 1: MAX7219 8-Digit Display")
    print("  Wiring check:")
    print("    Display VCC  → Pi Pin 2  (5V)")
    print("    Display GND  → Pi Pin 6  (GND)")
    print("    Display DIN  → Pi Pin 19 (GPIO 10 / MOSI)")
    print("    Display CS   → Pi Pin 24 (GPIO 8  / CE0)")
    print("    Display CLK  → Pi Pin 23 (GPIO 11 / SCLK)")

    if not ask_to_begin("display test"):
        return

    from luma.led_matrix.device import max7219
    from luma.core.interface.serial import spi, noop
    from luma.core.virtual import sevensegment

    try:
        serial = spi(port=0, device=0, gpio=noop())
        device = max7219(serial, cascaded=1, block_orientation=0, rotate=0)
        seg    = sevensegment(device)
        print("  Display initialised successfully")
    except Exception as exc:
        print(f"  FAILED to initialise display: {exc}")
        print("  Check wiring and that SPI is enabled (raspi-config → Interface Options → SPI)")
        return

    step("All segments ON — display should show '88888888'")
    seg.text = "88888888"
    pause(2)
    ok()

    step("All segments OFF — display should be blank")
    seg.text = "        "
    pause(1)
    ok()

    step("Showing 'HELLO   '")
    seg.text = "HELLO   "
    pause(2)
    ok()

    step("Showing 'MONEY   '")
    seg.text = "MONEY   "
    pause(2)
    ok()

    step("Showing number 12345678")
    seg.text = "12345678"
    pause(2)
    ok()

    step("Showing P&L format: 'P  +0.0 '")
    seg.text = "P  +0.0 "
    pause(2)
    ok()

    step("Scrolling 'MONEYMAKER' across display")
    text   = "MONEYMAKER"
    padded = "        " + text + "        "
    for i in range(len(padded) - 7):
        seg.text = padded[i:i + 8]
        time.sleep(0.25)
    ok()

    step("Brightness test — cycling from dim to bright")
    for brightness in range(0, 16, 2):
        device.contrast(brightness * 16)
        seg.text = f"BRI {brightness:2d} "
        pause(0.3)
    device.contrast(128)
    ok()

    seg.text = "DISP OK "
    pause(1)
    print("\n  Display test complete.")


# ══════════════════════════════════════════════════════════════
#  TEST 2 — WS2812D RGB LED
# ══════════════════════════════════════════════════════════════

def test_led() -> None:
    section("TEST 2: WS2812D RGB LED")
    print("  Wiring check:")
    print("    LED VCC  → Pi Pin 1  (3.3V)")
    print("    LED GND  → Pi Pin 20 (GND)")
    print("    LED DIN  → Pi Pin 12 (GPIO 18)")
    print("    LED DOUT → unconnected")

    if not ask_to_begin("LED test"):
        return

    import board
    import neopixel

    try:
        pixel = neopixel.NeoPixel(
            board.D18, 1, brightness=0.3, auto_write=True)
        print("  LED initialised successfully")
    except Exception as exc:
        print(f"  FAILED to initialise LED: {exc}")
        print("  Check wiring. DIN must be on GPIO 18 (Pin 12).")
        return

    colours = [
        ((0,   255, 0),   "RED"),
        ((255, 0,   0),   "GREEN"),
        ((0,   0,   255), "BLUE"),
        ((180, 255, 0),   "YELLOW"),
        ((0,   255, 150), "PINK (paper mode idle)"),
        ((80,  80,  80),  "DIM WHITE (real mode idle)"),
        ((180, 255, 0),   "AMBER (working)"),
    ]

    for colour, name in colours:
        step(f"LED should be {name}")
        pixel[0] = colour
        pause(1.5)
        ok()

    step("LED flashing GREEN x3 (trade profit signal)")
    for _ in range(3):
        pixel[0] = (255, 0, 0) 
        time.sleep(0.2)
        pixel[0] = (0, 0, 0)
        time.sleep(0.2)

    step("LED flashing RED x3 (trade loss signal)")
    for _ in range(3):
        pixel[0] = (0, 255, 0)   
        time.sleep(0.2)
        pixel[0] = (0, 0, 0)
        time.sleep(0.2)

    step("LED slow red pulse (error state)")
    for _ in range(3):
        pixel[0] = (0, 255, 0) 
        time.sleep(0.5)
        pixel[0] = (0, 0, 0)
        time.sleep(0.5)

    step("LED OFF")
    pixel[0] = (0, 0, 0)
    pause(0.5)
    ok()

    print("\n  LED test complete.")

# ══════════════════════════════════════════════════════════════
#  TEST 3 — Combined display + LED
# ══════════════════════════════════════════════════════════════

def test_combined() -> None:
    section("COMBINED TEST: Display + LED together")
    print("  Verifies both components work simultaneously")

    if not ask_to_begin("combined test"):
        return

    from luma.led_matrix.device import max7219
    from luma.core.interface.serial import spi, noop
    from luma.core.virtual import sevensegment
    import board
    import neopixel

    try:
        serial = spi(port=0, device=0, gpio=noop())
        device = max7219(serial, cascaded=1, block_orientation=0, rotate=0)
        seg    = sevensegment(device)
        pixel  = neopixel.NeoPixel(
            board.D18, 1, brightness=0.3,
            auto_write=True, pixel_order=neopixel.GRB
        )
    except Exception as exc:
        print(f"  FAILED to initialise hardware: {exc}")
        return

    scenarios = [
        ("STARTING", (180, 255, 0),  "Starting up — amber LED, STARTING on display"),
        ("CONNECT ", (180, 255, 0),  "Connecting — amber LED"),
        ("ONLINE  ", (255, 0,   0),  "Connected — green LED"),
        ("MKT OPEN", (255, 0,   0),  "Market open — green LED"),
        ("EVALUATE", (180, 255, 0),  "Evaluating — amber LED"),
        ("BUY ERIC", (255, 0,   0),  "Buy signal — green LED"),
        ("P  +123 ", (0,   0,   0),  "Idle paper mode — LED off, P&L on display"),
        ("OFFLINE ", (0,   255, 0),  "Offline — red LED"),
        ("MKT CLSD", (0,   0,   0),  "Market closed — LED off"),
        ("GOODBYE ", (0,   0,   0),  "Shutdown"),
    ]

    for display_text, led_colour, description in scenarios:
        step(description)
        seg.text  = display_text
        pixel[0]  = led_colour
        pause(2)
        ok()

    seg.text  = "        "
    pixel[0]  = (0, 0, 0)
    print("\n  Combined test complete.")

# ══════════════════════════════════════════════════════════════
#  TEST 4 — Pushbuttons (YES, NO, MODE)
# ══════════════════════════════════════════════════════════════

def test_buttons() -> None:
    section("TEST 3: Pushbuttons — YES, NO, MODE")
    print("  Wiring check:")
    print("    YES button  lug 1 → Pi Pin 36 (GPIO 16)")
    print("    YES button  lug 2 → Pi Pin 39 (GND)")
    print("    NO button   lug 1 → Pi Pin 38 (GPIO 20)")
    print("    NO button   lug 2 → Pi Pin 25 (GND)")
    print("    MODE button lug 1 → Pi Pin 40 (GPIO 21)")
    print("    MODE button lug 2 → Pi Pin 14 (GND)")

    if not ask_to_begin("button test"):
        return

    import RPi.GPIO as GPIO

    PINS = {
        "YES":  16,
        "NO":   20,
        "MODE": 21,
    }

    try:
        GPIO.setmode(GPIO.BCM)
        for name, pin in PINS.items():
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        print("  Buttons initialised successfully")
    except Exception as exc:
        print(f"  FAILED to initialise buttons: {exc}")
        GPIO.cleanup()
        return

    print()
    print("  Press each button when prompted.")
    print("  You have 10 seconds per button. Press Enter to skip.")

    for name, pin in PINS.items():
        step(f"Press the {name} button now (10s)...")
        detected = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready, _, _ = select.select([sys.stdin], [], [], 0.02)
            if ready:
                sys.stdin.readline()
                print(f"  Skipped {name} button.")
                break
            if GPIO.input(pin) == GPIO.LOW:
                detected = True
                break
            time.sleep(0.01)
        if detected:
            ok(f"{name} button detected")
            while GPIO.input(pin) == GPIO.LOW:
                time.sleep(0.02)
            time.sleep(0.2)
        elif not ready:
            print(f"  [✗] TIMEOUT — {name} button not detected")
            print(f"       Check GPIO {pin} wiring")

    GPIO.cleanup()
    print("\n  Button test complete.")

# ══════════════════════════════════════════════════════════════
#  TEST 5 — Power Switch
# ══════════════════════════════════════════════════════════════

def test_power_switch() -> None:
    section("TEST 5: Power Switch")
    print(" Wiring check:")
    print(" Switch lug 1 (common) → Pi Pin 37 (GPIO 26)")
    print(" Switch lug 2 (switched) → Pi Pin 34 or 39 (GND)")
    
    if not ask_to_begin("power switch test"):
        return

    import RPi.GPIO as GPIO
    PIN = 26

    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        print(" Power switch GPIO initialised successfully")
    except Exception as exc:
        print(f" FAILED to initialise: {exc}")
        GPIO.cleanup()
        return

    from hardware.power import PowerManager 
    ACTIVE_STATE = PowerManager.PIN_ACTIVE_STATE  

    step("Reading current switch position...")
    state = GPIO.input(PIN)
    
    if state == ACTIVE_STATE:
        ok(f"Switch is currently **OFF** (GPIO = {state})")
        current_label = "OFF"
        target_label = "ON"
    else:
        ok(f"Switch is currently **ON** (GPIO = {state})")
        current_label = "ON"
        target_label = "OFF"

    step(f"Flip switch to {target_label} (10s). Press Enter to skip.")
    detected = False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ready, _, _ = select.select([sys.stdin], [], [], 0.05)
        if ready:
            sys.stdin.readline()
            print(" Skipped.")
            break
        if GPIO.input(PIN) == (ACTIVE_STATE if target_label == "OFF" else (not ACTIVE_STATE)):
            detected = True
            break
        time.sleep(0.05)

    if detected:
        ok(f"Successfully flipped to {target_label}")
    else:
        print(" [✗] No change detected")

    step(f"Flip switch back to {current_label}...")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if GPIO.input(PIN) != (ACTIVE_STATE if target_label == "OFF" else (not ACTIVE_STATE)):
            ok(f"Switch restored to {current_label}")
            break
        time.sleep(0.05)

    GPIO.cleanup()
    print("\n Power switch test complete.")

# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main() -> None:
    print()
    print("╔═══════════════════════════════════════════════════════╗")
    print("║         MONEYMAKER — Hardware Test Suite              ║")
    print("╚═══════════════════════════════════════════════════════╝")
    print()
    print("  Active tests:")
    print("    1. MAX7219 8-digit display")
    print("    2. WS2812D RGB LED")
    print("    3. Combined display + LED")
    print("    4. Pushbuttons (YES, NO, MODE)")
    print("    5. Power switch")
    print()
    print("  At each test:  press Enter to run, type 's' to skip")
    print("  During steps:  press Enter to skip to next step")
    print("  Any time:      Ctrl+C to exit")

    try:
        test_display()
        test_led()
        test_combined()
        test_buttons()
        test_power_switch()

        section("ALL TESTS COMPLETE")
        print()

    except KeyboardInterrupt:
        print("\n\n  Test interrupted by user.")
    except Exception as exc:
        print(f"\n  UNEXPECTED ERROR: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            import RPi.GPIO as GPIO
            GPIO.cleanup()
        except Exception:
            pass


if __name__ == "__main__":
    main()