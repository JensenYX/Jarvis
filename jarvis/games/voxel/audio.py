import ctypes
import ctypes.util
from concurrent.futures import ThreadPoolExecutor
import asyncio


class SampleSpec(ctypes.Structure):
    _fields_ = [
        ("format", ctypes.c_int),
        ("rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint8),
    ]


class GameAudio:
    def __init__(self, server):
        self.server = server
        self.lib = None
        self.stream = None
        self.pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="voxel-game-audio"
        )

    def _read(self):
        if self.lib is None:
            self.lib = ctypes.CDLL(
                ctypes.util.find_library("pulse-simple") or "libpulse-simple.so.0"
            )
            self.lib.pa_simple_new.restype = ctypes.c_void_p
            self.lib.pa_simple_new.argtypes = [
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.POINTER(SampleSpec),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int),
            ]
            self.lib.pa_simple_read.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_int),
            ]
            self.lib.pa_simple_free.argtypes = [ctypes.c_void_p]
        if not self.stream:
            error = ctypes.c_int()
            spec = SampleSpec(5, 24000, 2)
            self.stream = self.lib.pa_simple_new(
                self.server.encode(),
                b"voxel-browser",
                2,
                b"game.monitor",
                b"game-only",
                ctypes.byref(spec),
                None,
                None,
                ctypes.byref(error),
            )
            if not self.stream:
                raise RuntimeError("Dedicated game audio unavailable")
        data = ctypes.create_string_buffer(2400 * 2 * 4)
        error = ctypes.c_int()
        if (
            self.lib.pa_simple_read(self.stream, data, len(data), ctypes.byref(error))
            < 0
        ):
            raise RuntimeError("Game audio read failed")
        return data.raw

    async def read(self):
        return await asyncio.get_running_loop().run_in_executor(self.pool, self._read)

    async def close(self):

        def close():
            if self.stream:
                self.lib.pa_simple_free(self.stream)
                self.stream = None

        await asyncio.get_running_loop().run_in_executor(self.pool, close)
        self.pool.shutdown(wait=False)
