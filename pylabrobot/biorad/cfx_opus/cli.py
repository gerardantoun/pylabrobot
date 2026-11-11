"""Command-line tools for the Bio-Rad CFX Opus.

``plr dev cfx-opus list`` finds connected Bio-Rad instruments; ``identify`` and ``status`` read
the instrument; ``lid`` opens or closes the lid. These touch hardware, so each opens the device
only when run, never at import.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from typing import Optional, Sequence

from pylabrobot.biorad.cfx_opus.cfx_opus import BIO_RAD_VENDOR_ID, C1000_PRODUCT_ID, CFXOpus


def _add_device_options(parser: argparse.ArgumentParser) -> None:
  parser.add_argument(
    "--product-id",
    type=lambda value: int(value, 0),
    default=C1000_PRODUCT_ID,
    help="USB product ID (default: the CFX96/C1000 family ID; override for the Opus's own ID)",
  )
  parser.add_argument("--serial-number", default=None, help="pick one of several instruments")


def _opus(args: argparse.Namespace) -> CFXOpus:
  return CFXOpus(id_product=args.product_id, serial_number=args.serial_number)


def list_devices(_: argparse.Namespace) -> None:
  """List connected Bio-Rad USB devices, whatever their product ID."""

  import libusb_package  # local import: only the list command needs it
  import usb.core

  backend = libusb_package.get_libusb1_backend()
  found = list(usb.core.find(find_all=True, idVendor=BIO_RAD_VENDOR_ID, backend=backend))
  if not found:
    print(f"No Bio-Rad (vendor {BIO_RAD_VENDOR_ID:#06x}) USB devices found.")
    return
  for device in found:
    print(
      f"{device.idVendor:#06x}:{device.idProduct:#06x}  "
      f"bus {device.bus} address {device.address}  "
      f"serial {device.serial_number!r}"
    )


async def _identify(args: argparse.Namespace) -> None:
  opus = _opus(args)
  await opus.setup()
  try:
    print(json.dumps(asdict(await opus.identify()), indent=2))
  finally:
    await opus.stop()


async def _status(args: argparse.Namespace) -> None:
  opus = _opus(args)
  await opus.setup()
  try:
    blocks = await opus.get_block_statuses()
    optics = await opus.get_optics_status()
    print(
      json.dumps(
        {"blocks": [asdict(block) for block in blocks], "optics": asdict(optics)},
        indent=2,
        default=list,
      )
    )
  finally:
    await opus.stop()


async def _lid(args: argparse.Namespace) -> None:
  opus = _opus(args)
  await opus.setup()
  try:
    if args.action == "open":
      await opus.open_lid()
    else:
      await opus.close_lid()
    print(f"Lid {(await opus.get_status()).lid_position}")
  finally:
    await opus.stop()


def main(argv: Optional[Sequence[str]] = None) -> None:
  parser = argparse.ArgumentParser(prog="plr dev cfx-opus", description=__doc__)
  subparsers = parser.add_subparsers(dest="command", required=True)

  list_parser = subparsers.add_parser("list", help="list connected Bio-Rad USB devices")
  list_parser.set_defaults(run=list_devices, is_async=False)

  identify_parser = subparsers.add_parser("identify", help="read model, serial and firmware")
  _add_device_options(identify_parser)
  identify_parser.set_defaults(run=_identify, is_async=True)

  status_parser = subparsers.add_parser("status", help="read block and optics status")
  _add_device_options(status_parser)
  status_parser.set_defaults(run=_status, is_async=True)

  lid_parser = subparsers.add_parser("lid", help="open or close the lid")
  lid_parser.add_argument("action", choices=["open", "close"])
  _add_device_options(lid_parser)
  lid_parser.set_defaults(run=_lid, is_async=True)

  args = parser.parse_args(list(argv) if argv is not None else None)
  if args.is_async:
    asyncio.run(args.run(args))
  else:
    args.run(args)


if __name__ == "__main__":
  main()
