"""A client for Windows Communication Foundation services over ``net.tcp``, in pure Python.

The package implements the Microsoft Open Specifications that a ``NetTcpBinding`` endpoint
without transport security speaks:

- ``nmf``: .NET Message Framing ([MC-NMF]), the records that carry messages over TCP;
- ``nbfx``: .NET Binary Format: XML Data Structure ([MC-NBFX]), with the trees of ``tree``
  and the typed values of ``values``;
- ``dictionary``: the string table of [MC-NBFS] and the per-connection one of [MC-NBFSE];
- ``soap``: SOAP 1.2 envelopes with WS-Addressing 1.0 headers;
- ``datacontract`` and ``contract``: how the DataContractSerializer lays out .NET values
  and the messages of a service's operations;
- ``channel``: the clients, :class:`DuplexChannel` for an endpoint with a session and
  callbacks and :class:`StreamedClient` for one with streamed transfer.
"""

from pylabrobot.sartorius.ique.driver.wcf.channel import DuplexChannel, StreamedClient
from pylabrobot.sartorius.ique.driver.wcf.contract import (
  STREAM,
  Operation,
  ServiceContract,
  read_fault_detail,
)
from pylabrobot.sartorius.ique.driver.wcf.datacontract import (
  BOOL,
  BYTE,
  BYTES,
  DATETIME,
  DECIMAL,
  DOUBLE,
  FLOAT,
  GUID,
  INT,
  LONG,
  SBYTE,
  SHORT,
  STRING,
  TIMESPAN,
  UINT,
  ULONG,
  USHORT,
  VERSION,
  ArrayOf,
  DataContract,
  DataObject,
  DictionaryOf,
  EnumType,
  ListOf,
  Nullable,
  XmlType,
  clr_namespace,
)
from pylabrobot.sartorius.ique.driver.wcf.errors import (
  ChannelClosedError,
  ContractError,
  FramingError,
  FramingFault,
  NbfxError,
  NbfxIncompleteError,
  SoapFault,
  WcfError,
)
from pylabrobot.sartorius.ique.driver.wcf.values import DateTime, Single, TimeSpan

__all__ = [
  "ArrayOf",
  "BOOL",
  "BYTE",
  "BYTES",
  "ChannelClosedError",
  "ContractError",
  "DATETIME",
  "DECIMAL",
  "DOUBLE",
  "DataContract",
  "DataObject",
  "DateTime",
  "DictionaryOf",
  "DuplexChannel",
  "EnumType",
  "FLOAT",
  "FramingError",
  "FramingFault",
  "GUID",
  "INT",
  "LONG",
  "ListOf",
  "NbfxError",
  "NbfxIncompleteError",
  "Nullable",
  "Operation",
  "SBYTE",
  "SHORT",
  "STREAM",
  "STRING",
  "ServiceContract",
  "Single",
  "SoapFault",
  "StreamedClient",
  "TIMESPAN",
  "TimeSpan",
  "UINT",
  "ULONG",
  "USHORT",
  "VERSION",
  "WcfError",
  "XmlType",
  "clr_namespace",
  "read_fault_detail",
]
