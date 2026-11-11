.. currentmodule:: pylabrobot.biorad

pylabrobot.biorad package
=========================

CFX Opus Real-Time PCR System
-----------------------------

.. currentmodule:: pylabrobot.biorad.cfx_opus.cfx_opus

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    CFXOpus
    Identity
    CompletedRead

Protocols
~~~~~~~~~

.. currentmodule:: pylabrobot.biorad.cfx_opus.protocol

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    Protocol
    Hold
    Gradient
    PlateRead
    Cycle
    MeltCurve
    compile_protocol
    expected_plate_reads

Status
~~~~~~

.. currentmodule:: pylabrobot.biorad.cfx_opus.status

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    BlockStatus
    OpticsStatus

Plate reads
~~~~~~~~~~~

.. currentmodule:: pylabrobot.biorad.cfx_opus.plate_read

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    decode_plate_read
    PlateRead
    WellReading

Framing
~~~~~~~

.. currentmodule:: pylabrobot.biorad.cfx_opus.framing

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    encode_command
    parse_reply
    MessageReader
    Message
    Reply
    CFXCommandError
    CFXProtocolError
