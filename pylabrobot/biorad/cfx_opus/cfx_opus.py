"""Bio-Rad CFX Opus real-time PCR (qPCR) driver.

Drives a CFX Opus directly over its USB Type-B port, with no Bio-Rad software. The instrument
speaks an ASCII command language framed as described in :mod:`pylabrobot.biorad.cfx_opus.framing`:
a protocol is authored one directive at a time, started with ``RemoteRun``, and its plate reads
collected as ``.Plateread`` files while a ``STATUS?`` poll tracks progress.

The command set and framing were reverse-engineered from the related CFX96 / C1000 Touch base (see
the project README for provenance). The CFX Opus shares the command language but its USB product ID
and the details of its replies have not been confirmed against real hardware, so ``setup`` warns
until a run verifies the driver.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional

from pylabrobot.biorad.cfx_opus.framing import (
  CFXCommandError,
  CFXProtocolError,
  Message,
  MessageReader,
  Reply,
  encode_command,
  parse_reply,
)
from pylabrobot.biorad.cfx_opus.plate_read import PlateRead, decode_plate_read
from pylabrobot.biorad.cfx_opus.protocol import Protocol, compile_protocol, expected_plate_reads
from pylabrobot.biorad.cfx_opus.status import (
  BlockStatus,
  OpticsStatus,
  parse_error_list,
  parse_optics_status,
  parse_status,
)
from pylabrobot.io.usb import USB

logger = logging.getLogger(__name__)

#: Bio-Rad's USB vendor ID.
BIO_RAD_VENDOR_ID = 0x0614
#: The CFX96 / C1000 Touch base's product ID. The CFX Opus's own ID is not yet confirmed; set
#: ``id_product`` when it is known.
C1000_PRODUCT_ID = 0x057B

_SUCCESS = "0000"
_RUN_FOLDER = r"\Storage Card\CurrentRun"
_REPORT_FOLDER = r"\Storage Card\PCRunReport"


@dataclass(frozen=True)
class Identity:
  """The instrument's identification, from ``*IDN?``."""

  manufacturer: str
  model: str
  serial_number: str
  firmware_version: str


@dataclass(frozen=True)
class CompletedRead:
  """A plate read collected during a run: its file name, raw bytes and decoded fluorescence."""

  name: str
  data: bytes
  read: PlateRead


class CFXOpus:
  """A Bio-Rad CFX Opus real-time PCR system."""

  def __init__(
    self,
    id_product: int = C1000_PRODUCT_ID,
    serial_number: Optional[str] = None,
    device_address: Optional[int] = None,
    command_timeout: float = 5.0,
  ):
    """Create a driver for one CFX Opus.

    Args:
      id_product: The instrument's USB product ID. Defaults to the CFX96/C1000 family ID; set this
        to the Opus's own ID once confirmed, or to tell two connected instruments apart.
      serial_number: The USB serial number, to pick one of several connected instruments.
      device_address: The USB device address, as an alternative to the serial number.
      command_timeout: Seconds to wait for one command's reply.
    """

    self.io = USB(
      id_vendor=BIO_RAD_VENDOR_ID,
      id_product=id_product,
      serial_number=serial_number,
      device_address=device_address,
      human_readable_device_name="Bio-Rad CFX Opus",
      read_timeout=max(30, int(command_timeout) + 1),
    )
    self.command_timeout = command_timeout
    self._reader = MessageReader()
    self._lock = asyncio.Lock()
    self._verified = False

  async def setup(self) -> None:
    """Open the USB connection and identify the instrument."""

    await self.io.setup()
    self._reader.clear()
    identity = await self.identify()
    logger.info(
      "Connected to %s %s (serial %s, firmware %s)",
      identity.manufacturer,
      identity.model,
      identity.serial_number,
      identity.firmware_version,
    )
    if not self._verified:
      logger.warning(
        "The CFX Opus driver has not been verified against real hardware. Its USB product ID and "
        "reply formats are carried over from the CFX96/C1000 family and may differ. Please report "
        "a successful or failed run so this warning can be removed."
      )

  async def stop(self) -> None:
    """Close the USB connection. Does not stop a run; call :meth:`stop_run` first if one is active."""

    await self.io.stop()

  # --- Command transport -------------------------------------------------------------------------

  async def _read_reply(self, timeout: float) -> Message:
    """Read messages until a command-channel one arrives, and return it.

    Messages on other channels carry unsolicited binary traffic and are discarded so they are
    never mistaken for a reply.
    """

    deadline = time.monotonic() + timeout
    while True:
      for message in self._reader.feed(await self.io.read(timeout=max(1, int(timeout)))):
        if message.channel == 1:
          return message
        logger.debug(
          "Ignoring unsolicited channel %d message: %r", message.channel, message.payload
        )
      if time.monotonic() >= deadline:
        raise CFXProtocolError("Timed out waiting for a command-channel reply")

  async def _command(self, command: str, timeout: Optional[float] = None) -> Reply:
    """Send one command and return its parsed text reply, holding the link for the round trip."""

    timeout = self.command_timeout if timeout is None else timeout
    async with self._lock:
      await self.io.write(encode_command(command), timeout=timeout)
      message = await self._read_reply(timeout)
    if message.pass_through:
      raise CFXProtocolError(f"'{command}' returned a binary payload: {message.payload.hex()}")
    return parse_reply(message.payload)

  async def _action(self, command: str, timeout: Optional[float] = None) -> None:
    """Send a command that reports only a result code, and check it succeeded."""

    reply = await self._command(command, timeout=timeout)
    if reply.code != _SUCCESS:
      raise CFXCommandError(command, reply.code, reply.value)

  async def _query(self, command: str, timeout: Optional[float] = None) -> str:
    """Send a query that reports a value, and return the value."""

    reply = await self._command(command, timeout=timeout)
    if reply.code != _SUCCESS:
      raise CFXCommandError(command, reply.code, reply.value)
    if reply.value is None:
      raise CFXProtocolError(f"'{command}' returned no value: {reply}")
    return reply.value

  # --- Identification and status -----------------------------------------------------------------

  async def identify(self) -> Identity:
    """Read the instrument's manufacturer, model, serial number and firmware version."""

    fields = (await self._query("*IDN?")).split(",")
    if len(fields) != 4 or "BIO-RAD" not in fields[0].upper():
      raise CFXProtocolError(f"Unexpected *IDN? response: {fields}")
    return Identity(fields[0], fields[1], fields[2], fields[3])

  async def get_status(self) -> BlockStatus:
    """Read the block's current status. Use :meth:`get_block_statuses` for a multi-block base."""

    return (await self.get_block_statuses())[0]

  async def get_block_statuses(self) -> List[BlockStatus]:
    """Read every block's status."""

    return parse_status(await self._query("STATUS?"))

  async def get_optics_status(self) -> OpticsStatus:
    """Read the optical head's shuttle and ambient temperatures and any faults."""

    return parse_optics_status(await self._query("RTSTATUS?"))

  async def get_errors(self, block: str = "A") -> List[str]:
    """Read the error list for one block (``"A"`` or ``"B"``)."""

    return list(parse_error_list(await self._query(f"ERRORLIST {block}")))

  # --- Lid -----------------------------------------------------------------------------------------

  async def open_lid(self) -> None:
    """Open the motorised lid, if it is not already open or opening."""

    status = await self.get_status()
    if status.lid_position in ("OPEN", "OPENING"):
      return
    await self._action("LID OPEN")

  async def close_lid(self) -> None:
    """Close the motorised lid, if it is not already closed or closing."""

    status = await self.get_status()
    if status.lid_position in ("CLOSED", "CLOSING"):
      return
    await self._action("LID CLOSE")

  async def identify_block(self, block: int = 1) -> None:
    """Flash a block's indicator so an operator can see which unit is being addressed."""

    await self._action(f"BLOCKID {block}")

  # --- Files ---------------------------------------------------------------------------------------

  async def _list_directory(self, directory: str) -> List[str]:
    """List the files in a directory.

    ``LISTALLFILES`` returns whatever the preceding ``GETFILESLEN`` buffered and ignores its own
    path, so the pair is issued together under the link lock.
    """

    async with self._lock:
      await self.io.write(encode_command(f"GETFILESLEN {directory}"), timeout=self.command_timeout)
      length_message = await self._read_reply(self.command_timeout)
      # An empty or missing directory answers with a binary status payload, not a length. Either
      # way there is nothing to list, and sending LISTALLFILES would return a stale buffer.
      if length_message.pass_through:
        return []
      length = parse_reply(length_message.payload)
      if length.code != _SUCCESS:
        raise CFXCommandError(f"GETFILESLEN {directory}", length.code, length.value)
      if length.value in ("0", None):
        return []
      await self.io.write(encode_command(f"LISTALLFILES {directory}"), timeout=self.command_timeout)
      listing = parse_reply((await self._read_reply(self.command_timeout)).payload)
    if listing.value is None:
      raise CFXProtocolError(f"LISTALLFILES {directory} returned no listing")
    return [name for name in listing.value.split(",") if name]

  async def _get_file(self, path: str) -> bytes:
    """Download a file. ``GETFILE``'s reply is raw bytes, collected against ``GETFILESIZE``."""

    size = int(await self._query(f"GETFILESIZE {path}"))
    if size == 0:
      return b""
    async with self._lock:
      await self.io.write(encode_command(f"GETFILE {path}"), timeout=self.command_timeout)
      data = bytearray()
      deadline = time.monotonic() + max(self.command_timeout, size / 10000)
      read_timeout = max(1, int(self.command_timeout))
      while len(data) < size:
        for message in self._reader.feed(await self.io.read(timeout=read_timeout)):
          if message.channel == 1:
            data += message.payload
        if time.monotonic() >= deadline:
          raise CFXProtocolError(f"Timed out downloading {path} ({len(data)}/{size} bytes)")
    if len(data) != size:
      raise CFXProtocolError(f"Downloaded {len(data)} bytes of {path}, expected {size}")
    return bytes(data)

  async def _delete_file(self, path: str) -> None:
    """Delete a file. A missing file answers with a binary "no such file" payload, also fine."""

    command = f"DELFILE {path}"
    async with self._lock:
      await self.io.write(encode_command(command), timeout=self.command_timeout)
      message = await self._read_reply(self.command_timeout)
    if not message.pass_through:
      reply = parse_reply(message.payload)
      if reply.code != _SUCCESS:
        raise CFXCommandError(command, reply.code, reply.value)

  # --- Running a protocol --------------------------------------------------------------------------

  async def ready(self) -> bool:
    """Whether the instrument will accept a remote run: not locked down and reporting no errors."""

    locked = (await self._query("SUPERLOCKDOWNMODE?")).upper()
    errors = await self._query("ERRORS?")
    return locked == "OFF" and errors == "0"

  async def author_protocol(self, protocol: Protocol, name: str = "PCRUN") -> None:
    """Send a protocol to the instrument, one directive per command.

    The protocol replaces the instrument's single authored-protocol slot; it does not start a run.
    """

    await self._action(f"PROTOCOL '{name}'")
    for directive in compile_protocol(protocol):
      await self._action(directive)

  async def start_run(
    self,
    run_name: str,
    user: str = "pylabrobot",
    sample_id: str = "",
    block: str = "A",
    method: Literal["CALC", "BLOCK"] = "CALC",
    use_lid_heater: bool = True,
  ) -> None:
    """Start the authored protocol running now.

    Author the protocol with :meth:`author_protocol` first. ``run_name`` reaches the instrument
    upper-cased and truncated to 8 characters. None of the text fields may contain ``'``, ``"``,
    ``,`` or ``;``.
    """

    for label, value in (("run name", run_name), ("user", user), ("sample id", sample_id)):
      if any(character in value for character in "'\",;"):
        raise ValueError(f"The {label} may not contain any of ' \" , ; : {value!r}")
    lid = "True" if use_lid_heater else "False"
    await self._action(
      f'RemoteRun "{block}","{lid}","False","{run_name}","{user}","{sample_id}","True","{method}"'
    )

  async def pause_run(self) -> None:
    """Pause the running protocol."""

    await self._action("PAUSE")

  async def resume_run(self) -> None:
    """Resume a paused protocol."""

    await self._action("RESUME")

  async def stop_run(self) -> None:
    """Stop the running protocol. Sending it again acknowledges and releases a finished run."""

    await self._action("CANCEL")

  async def lock_front_panel(self) -> None:
    """Lock the instrument's touchscreen for the duration of a remote session."""

    await self._action("FRONTENDLOCKED ON")

  async def unlock_front_panel(self) -> None:
    """Release the instrument's touchscreen."""

    await self._action("FRONTENDLOCKED OFF")

  async def list_run_files(self) -> List[str]:
    """List the files in the current run folder."""

    return await self._list_directory(_RUN_FOLDER)

  async def get_plate_read(self, name: str) -> CompletedRead:
    """Download and decode one ``.Plateread`` file from the current run folder."""

    data = await self._get_file(_RUN_FOLDER + "\\" + name)
    return CompletedRead(name=name, data=data, read=decode_plate_read(data))

  async def run_protocol(
    self,
    protocol: Protocol,
    run_name: str,
    user: str = "pylabrobot",
    sample_id: str = "",
    poll_interval: float = 2.0,
    start_timeout: float = 30.0,
  ) -> List[CompletedRead]:
    """Run a protocol to completion, returning its plate reads in order.

    Authors the protocol, starts it, and watches ``STATUS?`` — collecting each ``.Plateread`` file
    as its step completes — until the run finishes, then acknowledges it. The heated lid preheats
    before the block moves, so the first minutes show no block movement; this is expected.

    Raises:
      CFXCommandError: The instrument rejected a command.
      TimeoutError: The run did not start within ``start_timeout`` seconds.
      RuntimeError: The run ended early or reported an error.
    """

    if not await self.ready():
      raise RuntimeError("Instrument is locked down or reporting errors; cannot start a run")
    await self._clear_report_folder()
    await self.author_protocol(protocol)
    await self.start_run(run_name=run_name, user=user, sample_id=sample_id, method=protocol.method)

    reads: List[CompletedRead] = []
    seen: Dict[str, bool] = {}
    expected = expected_plate_reads(protocol)
    started = False
    deadline = time.monotonic() + start_timeout
    previous_step = ""

    while True:
      status = await self.get_status()
      state = status.run_state
      if state in ("running", "paused", "finished", "aborted"):
        started = True
      elif not started and time.monotonic() > deadline:
        raise TimeoutError(f"Run '{run_name}' did not start within {start_timeout} s")

      # A step leaving a plate read means its file has landed.
      if previous_step.startswith("PLATEREAD") and not status.step_text.startswith("PLATEREAD"):
        await self._collect_new_reads(reads, seen)
      previous_step = status.step_text

      if status.errors:
        await self.stop_run()
        raise RuntimeError(f"Run '{run_name}' reported errors: {status.errors}")

      if state in ("finished", "aborted"):
        await self._finish_run(reads, seen)
        if state == "aborted":
          raise RuntimeError(f"Run '{run_name}' was aborted at the instrument")
        if len(reads) < expected:
          raise RuntimeError(f"Run '{run_name}' ended with {len(reads)} of {expected} plate reads")
        return reads

      await asyncio.sleep(poll_interval)

  async def _clear_report_folder(self) -> None:
    for name in await self._list_directory(_REPORT_FOLDER):
      await self._delete_file(_REPORT_FOLDER + "\\" + name)

  async def _collect_new_reads(self, reads: List[CompletedRead], seen: Dict[str, bool]) -> None:
    for name in sorted(await self.list_run_files()):
      if name.lower().endswith(".plateread") and name not in seen:
        seen[name] = True
        reads.append(await self.get_plate_read(name))

  async def _finish_run(self, reads: List[CompletedRead], seen: Dict[str, bool]) -> None:
    """Acknowledge a finished run with ``CANCEL``, wait for the release, then collect the last read."""

    await self.stop_run()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
      if (await self.get_status()).run_state == "idle":
        break
      await asyncio.sleep(0.5)
    await self._collect_new_reads(reads, seen)
