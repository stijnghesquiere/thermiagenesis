"""Run the synchronous Modbus traffic in pythermiagenesis off the HA loop."""

import asyncio
from functools import partial

from homeassistant.core import HomeAssistant
from pythermiagenesis import ThermiaConnectionError
from pythermiagenesis import ThermiaGenesis


class ThermiaModbusClient:
    """Serialize complete operations on a single ThermiaGenesis client."""

    def __init__(self, hass: HomeAssistant, host, port, kind, *, delay=0.1):
        self._hass = hass
        self._create_client = partial(
            ThermiaGenesis, host, port=port, kind=kind, delay=delay, max_registers=16
        )
        self._thermia = None
        self._lock = asyncio.Lock()
        self._closing = False

    async def async_read(self, registers):
        """Read the selected registers in the executor."""
        return await self._async_run(
            partial(ThermiaGenesis.async_update, only_registers=tuple(registers))
        )

    async def async_write(self, register, value):
        """Write a coil or holding register in the executor."""
        await self._async_run(ThermiaGenesis.async_set, register, value)

    async def async_close(self):
        """Reject new work and wait for the worker to finish closing its socket."""
        self._closing = True
        async with self._lock:
            pass

    async def _async_run(self, operation, *args):
        await self._lock.acquire()
        try:
            if self._closing or self._hass.is_stopping:
                raise ConnectionError("Modbus client is closing")
            job = self._hass.async_add_executor_job(self._run, operation, *args)
        except BaseException:
            self._lock.release()
            raise

        # Cancelling the caller cannot interrupt synchronous socket I/O.
        # Keep the lock until the worker has finished and closed the socket.
        job.add_done_callback(self._finish_job)
        return await asyncio.shield(job)

    def _finish_job(self, job):
        self._lock.release()
        if not job.cancelled():
            job.exception()  # Retrieve errors even if the caller was cancelled.

    def _run(self, operation, *args):
        if self._thermia is None:
            self._thermia = self._create_client()
        try:
            # The library awaits pacing sleeps and uses blocking Modbus methods.
            return asyncio.run(operation(self._thermia, *args))
        except ThermiaConnectionError as error:
            raise ConnectionError(error.message or str(error)) from error
        finally:
            # The pinned library has no public close and can skip its own
            # socket close when an exception escapes an operation.
            self._thermia._client.close()
