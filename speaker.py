"""Optional host audio for the emulated IBM PC internal speaker."""

import threading
import time


class PCSpeaker:
    """Render PIT channel 2 as a stream of square-wave PCM chunks."""

    SAMPLE_RATE = 44100
    CHUNK_SAMPLES = 882  # 20 ms, small enough for tone changes to be audible

    def __init__(self, pit):
        self.pit = pit
        self._gate = 0
        self._closed = False
        self._pygame = None
        self._channel = None
        self._thread = None
        try:
            import pygame
            pygame.mixer.pre_init(self.SAMPLE_RATE, -16, 1, 512)
            pygame.mixer.init()
            self._pygame = pygame
            self._channel = pygame.mixer.Channel(0)
            self._thread = threading.Thread(target=self._run,
                                             name='pc-speaker', daemon=True)
            self._thread.start()
        except Exception:
            self.close()

    @property
    def available(self):
        return self._pygame is not None

    def set_gate(self, value):
        self._gate = value & 0x03

    def _tone(self):
        reload = self.pit.reloads[2]
        if not reload or not (self._gate & 3) == 3:
            return None
        frequency = self.pit.input_clk / reload
        if frequency < 20 or frequency > self.SAMPLE_RATE / 2:
            return None
        amplitude = 7000
        period = self.SAMPLE_RATE / frequency
        samples = bytearray(self.CHUNK_SAMPLES * 2)
        for index in range(self.CHUNK_SAMPLES):
            value = amplitude if (index % period) < period / 2 else -amplitude
            samples[index * 2:index * 2 + 2] = int(value).to_bytes(
                2, 'little', signed=True)
        return self._pygame.mixer.Sound(buffer=bytes(samples))

    def _run(self):
        while not self._closed:
            if self._channel.get_busy():
                time.sleep(0.004)
                continue
            tone = self._tone()
            if tone is not None:
                self._channel.play(tone)
            else:
                time.sleep(0.01)

    def close(self):
        self._closed = True
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=0.2)
        if self._channel is not None:
            self._channel.stop()
        if self._pygame is not None:
            self._pygame.mixer.quit()
