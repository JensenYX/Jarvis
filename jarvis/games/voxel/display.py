from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import asyncio
import io
import math
import time

KEYS = {
    "KeyW": "w",
    "KeyA": "a",
    "KeyS": "s",
    "KeyD": "d",
    "Space": "space",
    "ShiftLeft": "Shift_L",
    "ControlLeft": "Control_L",
    "KeyE": "i",
    "KeyI": "i",
    "Escape": "Escape",
    **{f"Digit{i}": str(i) for i in range(1, 10)},
}


class Display:
    def __init__(self, display=":91"):
        self.name = display
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voxel-x11")
        self.capture_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="voxel-capture"
        )
        self.d = None
        self.capture = None
        self.keys = set()
        self.buttons = set()
        self.last_input = time.monotonic()
        self.frames = []

    def _open(self):
        if self.d:
            return
        from Xlib import display

        self.d = display.Display(self.name)

    def _window(self):
        self._open()
        for window in self.d.screen().root.query_tree().children:
            if window.get_wm_class() in {
                ("luanti", "luanti"),
                ("luanti-presentation", "luanti-presentation"),
                ("luanti-camera-test", "luanti-camera-test"),
            }:
                return window
        raise RuntimeError("Luanti window unavailable")

    def _frame(self, quality=65, width=1024):
        from PIL import Image

        start = time.monotonic()
        if self.capture is None:
            import mss

            self.capture = mss.mss(display=self.name)
        shot = self.capture.grab({"left": 0, "top": 0, "width": 1024, "height": 640})
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if width < 1024:
            image = image.resize(
                (width, round(width * 640 / 1024)), Image.Resampling.BILINEAR
            )
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality)
        self.frames.append((time.monotonic() - start) * 1000)
        self.frames = self.frames[-600:]
        return output.getvalue()

    def _release(self):
        if not self.d:
            return
        from Xlib import X
        from Xlib.ext import xtest

        for key in self.keys:
            xtest.fake_input(self.d, X.KeyRelease, key)
        for button in self.buttons:
            xtest.fake_input(self.d, X.ButtonRelease, button)
        self.keys.clear()
        self.buttons.clear()
        self.d.sync()

    def _input(self, event):
        from Xlib import X, XK
        from Xlib.ext import xtest

        kind = event.get("kind")
        if kind == "release":
            self._release()
            return
        window = self._window()
        window.set_input_focus(X.RevertToParent, X.CurrentTime)
        if kind == "key":
            if event.get("code") not in KEYS or type(event.get("down")) is not bool:
                raise ValueError("Game key required")
            code = self.d.keysym_to_keycode(XK.string_to_keysym(KEYS[event["code"]]))
            if event["down"]:
                if code not in self.keys:
                    xtest.fake_input(self.d, X.KeyPress, code)
                    self.keys.add(code)
            elif code in self.keys:
                xtest.fake_input(self.d, X.KeyRelease, code)
                self.keys.remove(code)
        elif kind == "button":
            button = {0: 1, 2: 3}.get(event.get("button"))
            if not button or type(event.get("down")) is not bool:
                raise ValueError("Game mouse button required")
            if event["down"]:
                if button not in self.buttons:
                    xtest.fake_input(self.d, X.ButtonPress, button)
                    self.buttons.add(button)
            elif button in self.buttons:
                xtest.fake_input(self.d, X.ButtonRelease, button)
                self.buttons.remove(button)
        elif kind in {"motion", "pointer"}:
            x, y = (event.get("x"), event.get("y"))
            if not all(
                (isinstance(v, (int, float)) and math.isfinite(v) for v in (x, y))
            ):
                raise ValueError("Finite movement required")
            if kind == "motion":
                xtest.fake_input(
                    self.d,
                    X.MotionNotify,
                    detail=1,
                    x=max(-300, min(300, round(x))),
                    y=max(-300, min(300, round(y))),
                )
            else:
                xtest.fake_input(
                    self.d,
                    X.MotionNotify,
                    x=max(0, min(1023, round(x))),
                    y=max(0, min(639, round(y))),
                )
        elif kind == "wheel":
            button = 4 if event.get("delta", 0) < 0 else 5
            xtest.fake_input(self.d, X.ButtonPress, button)
            xtest.fake_input(self.d, X.ButtonRelease, button)
        else:
            raise ValueError("Unsupported game input")
        self.d.sync()
        self.last_input = time.monotonic()

    async def frame(self, quality=65, width=1024):
        return await asyncio.get_running_loop().run_in_executor(
            self.capture_pool, self._frame, quality, width
        )

    async def input(self, event):
        return await asyncio.get_running_loop().run_in_executor(
            self.pool, self._input, event
        )

    async def release(self):
        return await asyncio.get_running_loop().run_in_executor(
            self.pool, self._release
        )
