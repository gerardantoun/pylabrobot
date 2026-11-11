"""Errors raised by the WCF protocol stack."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
  from pylabrobot.sartorius.ique.driver.wcf.tree import Element


class WcfError(Exception):
  """Base class of the errors this package raises about the peer or the exchange with it."""


class NbfxError(WcfError, ValueError):
  """Data or a tree does not follow the binary XML format ([MC-NBFX])."""


class NbfxIncompleteError(NbfxError):
  """The data ends inside a record; the reader is left where that record starts."""


class FramingError(WcfError):
  """The peer broke the message framing protocol ([MC-NMF]), or the connection ended early."""


class FramingFault(FramingError):
  """The peer answered with a framing Fault record ([MC-NMF] 2.2.5).

  Attributes:
    fault: The fault URI, such as
      ``http://schemas.microsoft.com/ws/2006/05/framing/faults/EndpointNotFound``.
  """

  def __init__(self, fault: str) -> None:
    """Create the error for a fault URI."""
    super().__init__(f"peer refused the connection or message: {fault}")
    self.fault = fault


class ContractError(WcfError):
  """A message does not have the shape its operation declares."""


class ChannelClosedError(WcfError, ConnectionError):
  """The channel is closed, so the request was not sent or its reply will never arrive."""


class SoapFault(WcfError):
  """The service answered a request with a SOAP fault.

  Attributes:
    code: The fault code as (namespace, name), such as the SOAP ``Sender`` or ``Receiver``.
    subcodes: The nested subcodes, outermost first, each as (namespace, name).
    reason: The human-readable explanation.
    detail: The content of the fault's Detail element, when it has exactly one child
      element: for a declared fault, the serialized fault contract.
    action: The Action of the fault message, which names the fault contract.
  """

  def __init__(
    self,
    code: Tuple[str, str],
    subcodes: Tuple[Tuple[str, str], ...],
    reason: str,
    detail: Optional["Element"],
    action: str,
  ) -> None:
    """Create the error from the parts of a fault message."""
    names = "/".join(name for _, name in (code,) + subcodes)
    super().__init__(f"{names}: {reason}")
    self.code = code
    self.subcodes = subcodes
    self.reason = reason
    self.detail = detail
    self.action = action
