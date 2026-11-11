"""String dictionaries of the .NET Binary Format for SOAP.

[MC-NBFS] fixes a table of strings common in SOAP messages; a document refers to one by an
even number, twice its position in the table. [MC-NBFSE] adds a table per connection and
direction: each envelope starts with the strings its sender adds, and they are referred to
by the odd numbers 1, 3, 5 and so on, in the order announced, for the rest of the connection.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from pylabrobot.sartorius.ique.driver.wcf.errors import NbfxError, NbfxIncompleteError
from pylabrobot.sartorius.ique.driver.wcf.nbfx import decode_int31, encode_int31
from pylabrobot.sartorius.ique.driver.wcf.values import DictionaryString

# The table of [MC-NBFS] section 2.1, in key order. The comments give the key of the string
# that follows them.
STATIC_STRINGS: Tuple[str, ...] = (
  # 0x000
  "mustUnderstand",
  "Envelope",
  "http://www.w3.org/2003/05/soap-envelope",
  "http://www.w3.org/2005/08/addressing",
  "Header",
  "Action",
  "To",
  "Body",
  "Algorithm",
  "RelatesTo",
  "http://www.w3.org/2005/08/addressing/anonymous",
  "URI",
  "Reference",
  "MessageID",
  "Id",
  "Identifier",
  # 0x020
  "http://schemas.xmlsoap.org/ws/2005/02/rm",
  "Transforms",
  "Transform",
  "DigestMethod",
  "DigestValue",
  "Address",
  "ReplyTo",
  "SequenceAcknowledgement",
  "AcknowledgementRange",
  "Upper",
  "Lower",
  "BufferRemaining",
  "http://schemas.microsoft.com/ws/2006/05/rm",
  "http://schemas.xmlsoap.org/ws/2005/02/rm/SequenceAcknowledgement",
  "SecurityTokenReference",
  "Sequence",
  # 0x040
  "MessageNumber",
  "http://www.w3.org/2000/09/xmldsig#",
  "http://www.w3.org/2000/09/xmldsig#enveloped-signature",
  "KeyInfo",
  "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
  "http://www.w3.org/2001/04/xmlenc#",
  "http://schemas.xmlsoap.org/ws/2005/02/sc",
  "DerivedKeyToken",
  "Nonce",
  "Signature",
  "SignedInfo",
  "CanonicalizationMethod",
  "SignatureMethod",
  "SignatureValue",
  "DataReference",
  "EncryptedData",
  # 0x060
  "EncryptionMethod",
  "CipherData",
  "CipherValue",
  "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
  "Security",
  "Timestamp",
  "Created",
  "Expires",
  "Length",
  "ReferenceList",
  "ValueType",
  "Type",
  "EncryptedHeader",
  "http://docs.oasis-open.org/wss/oasis-wss-wssecurity-secext-1.1.xsd",
  "RequestSecurityTokenResponseCollection",
  "http://schemas.xmlsoap.org/ws/2005/02/trust",
  # 0x080
  "http://schemas.xmlsoap.org/ws/2005/02/trust#BinarySecret",
  "http://schemas.microsoft.com/ws/2006/02/transactions",
  "s",
  "Fault",
  "MustUnderstand",
  "role",
  "relay",
  "Code",
  "Reason",
  "Text",
  "Node",
  "Role",
  "Detail",
  "Value",
  "Subcode",
  "NotUnderstood",
  # 0x0a0
  "qname",
  "",
  "From",
  "FaultTo",
  "EndpointReference",
  "PortType",
  "ServiceName",
  "PortName",
  "ReferenceProperties",
  "RelationshipType",
  "Reply",
  "a",
  "http://schemas.xmlsoap.org/ws/2006/02/addressingidentity",
  "Identity",
  "Spn",
  "Upn",
  # 0x0c0
  "Rsa",
  "Dns",
  "X509v3Certificate",
  "http://www.w3.org/2005/08/addressing/fault",
  "ReferenceParameters",
  "IsReferenceParameter",
  "http://www.w3.org/2005/08/addressing/reply",
  "http://www.w3.org/2005/08/addressing/none",
  "Metadata",
  "http://schemas.xmlsoap.org/ws/2004/08/addressing",
  "http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous",
  "http://schemas.xmlsoap.org/ws/2004/08/addressing/fault",
  "http://schemas.xmlsoap.org/ws/2004/06/addressingex",
  "RedirectTo",
  "Via",
  "http://www.w3.org/2001/10/xml-exc-c14n#",
  # 0x0e0
  "PrefixList",
  "InclusiveNamespaces",
  "ec",
  "SecurityContextToken",
  "Generation",
  "Label",
  "Offset",
  "Properties",
  "Cookie",
  "wsc",
  "http://schemas.xmlsoap.org/ws/2004/04/sc",
  "http://schemas.xmlsoap.org/ws/2004/04/security/sc/dk",
  "http://schemas.xmlsoap.org/ws/2004/04/security/sc/sct",
  "http://schemas.xmlsoap.org/ws/2004/04/security/trust/RST/SCT",
  "http://schemas.xmlsoap.org/ws/2004/04/security/trust/RSTR/SCT",
  "RenewNeeded",
  # 0x100
  "BadContextToken",
  "c",
  "http://schemas.xmlsoap.org/ws/2005/02/sc/dk",
  "http://schemas.xmlsoap.org/ws/2005/02/sc/sct",
  "http://schemas.xmlsoap.org/ws/2005/02/trust/RST/SCT",
  "http://schemas.xmlsoap.org/ws/2005/02/trust/RSTR/SCT",
  "http://schemas.xmlsoap.org/ws/2005/02/trust/RST/SCT/Renew",
  "http://schemas.xmlsoap.org/ws/2005/02/trust/RSTR/SCT/Renew",
  "http://schemas.xmlsoap.org/ws/2005/02/trust/RST/SCT/Cancel",
  "http://schemas.xmlsoap.org/ws/2005/02/trust/RSTR/SCT/Cancel",
  "http://www.w3.org/2001/04/xmlenc#aes128-cbc",
  "http://www.w3.org/2001/04/xmlenc#kw-aes128",
  "http://www.w3.org/2001/04/xmlenc#aes192-cbc",
  "http://www.w3.org/2001/04/xmlenc#kw-aes192",
  "http://www.w3.org/2001/04/xmlenc#aes256-cbc",
  "http://www.w3.org/2001/04/xmlenc#kw-aes256",
  # 0x120
  "http://www.w3.org/2001/04/xmlenc#des-cbc",
  "http://www.w3.org/2000/09/xmldsig#dsa-sha1",
  "http://www.w3.org/2001/10/xml-exc-c14n#WithComments",
  "http://www.w3.org/2000/09/xmldsig#hmac-sha1",
  "http://www.w3.org/2001/04/xmldsig-more#hmac-sha256",
  "http://schemas.xmlsoap.org/ws/2005/02/sc/dk/p_sha1",
  "http://www.w3.org/2001/04/xmlenc#ripemd160",
  "http://www.w3.org/2001/04/xmlenc#rsa-oaep-mgf1p",
  "http://www.w3.org/2000/09/xmldsig#rsa-sha1",
  "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
  "http://www.w3.org/2001/04/xmlenc#rsa-1_5",
  "http://www.w3.org/2000/09/xmldsig#sha1",
  "http://www.w3.org/2001/04/xmlenc#sha256",
  "http://www.w3.org/2001/04/xmlenc#sha512",
  "http://www.w3.org/2001/04/xmlenc#tripledes-cbc",
  "http://www.w3.org/2001/04/xmlenc#kw-tripledes",
  # 0x140
  "http://schemas.xmlsoap.org/2005/02/trust/tlsnego#TLS_Wrap",
  "http://schemas.xmlsoap.org/2005/02/trust/spnego#GSS_Wrap",
  "http://schemas.microsoft.com/ws/2006/05/security",
  "dnse",
  "o",
  "Password",
  "PasswordText",
  "Username",
  "UsernameToken",
  "BinarySecurityToken",
  "EncodingType",
  "KeyIdentifier",
  "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary",
  "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#HexBinary",
  "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Text",
  (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-x509-token-profile-1.0#X509SubjectKeyIdentifier"
  ),
  # 0x160
  "http://docs.oasis-open.org/wss/oasis-wss-kerberos-token-profile-1.1#GSS_Kerberosv5_AP_REQ",
  "http://docs.oasis-open.org/wss/oasis-wss-kerberos-token-profile-1.1#GSS_Kerberosv5_AP_REQ1510",
  "http://docs.oasis-open.org/wss/oasis-wss-saml-token-profile-1.0#SAMLAssertionID",
  "Assertion",
  "urn:oasis:names:tc:SAML:1.0:assertion",
  "http://docs.oasis-open.org/wss/oasis-wss-rel-token-profile-1.0.pdf#license",
  "FailedAuthentication",
  "InvalidSecurityToken",
  "InvalidSecurity",
  "k",
  "SignatureConfirmation",
  "TokenType",
  "http://docs.oasis-open.org/wss/oasis-wss-soap-message-security-1.1#ThumbprintSHA1",
  "http://docs.oasis-open.org/wss/oasis-wss-soap-message-security-1.1#EncryptedKey",
  "http://docs.oasis-open.org/wss/oasis-wss-soap-message-security-1.1#EncryptedKeySHA1",
  "http://docs.oasis-open.org/wss/oasis-wss-saml-token-profile-1.1#SAMLV1.1",
  # 0x180
  "http://docs.oasis-open.org/wss/oasis-wss-saml-token-profile-1.1#SAMLV2.0",
  "http://docs.oasis-open.org/wss/oasis-wss-saml-token-profile-1.1#SAMLID",
  "AUTH-HASH",
  "RequestSecurityTokenResponse",
  "KeySize",
  "RequestedTokenReference",
  "AppliesTo",
  "Authenticator",
  "CombinedHash",
  "BinaryExchange",
  "Lifetime",
  "RequestedSecurityToken",
  "Entropy",
  "RequestedProofToken",
  "ComputedKey",
  "RequestSecurityToken",
  # 0x1a0
  "RequestType",
  "Context",
  "BinarySecret",
  "http://schemas.xmlsoap.org/ws/2005/02/trust/spnego",
  " http://schemas.xmlsoap.org/ws/2005/02/trust/tlsnego",
  "wst",
  "http://schemas.xmlsoap.org/ws/2004/04/trust",
  "http://schemas.xmlsoap.org/ws/2004/04/security/trust/RST/Issue",
  "http://schemas.xmlsoap.org/ws/2004/04/security/trust/RSTR/Issue",
  "http://schemas.xmlsoap.org/ws/2004/04/security/trust/Issue",
  "http://schemas.xmlsoap.org/ws/2004/04/security/trust/CK/PSHA1",
  "http://schemas.xmlsoap.org/ws/2004/04/security/trust/SymmetricKey",
  "http://schemas.xmlsoap.org/ws/2004/04/security/trust/Nonce",
  "KeyType",
  "http://schemas.xmlsoap.org/ws/2004/04/trust/SymmetricKey",
  "http://schemas.xmlsoap.org/ws/2004/04/trust/PublicKey",
  # 0x1c0
  "Claims",
  "InvalidRequest",
  "RequestFailed",
  "SignWith",
  "EncryptWith",
  "EncryptionAlgorithm",
  "CanonicalizationAlgorithm",
  "ComputedKeyAlgorithm",
  "UseKey",
  "http://schemas.microsoft.com/net/2004/07/secext/WS-SPNego",
  "http://schemas.microsoft.com/net/2004/07/secext/TLSNego",
  "t",
  "http://schemas.xmlsoap.org/ws/2005/02/trust/RST/Issue",
  "http://schemas.xmlsoap.org/ws/2005/02/trust/RSTR/Issue",
  "http://schemas.xmlsoap.org/ws/2005/02/trust/Issue",
  "http://schemas.xmlsoap.org/ws/2005/02/trust/SymmetricKey",
  # 0x1e0
  "http://schemas.xmlsoap.org/ws/2005/02/trust/CK/PSHA1",
  "http://schemas.xmlsoap.org/ws/2005/02/trust/Nonce",
  "RenewTarget",
  "CancelTarget",
  "RequestedTokenCancelled",
  "RequestedAttachedReference",
  "RequestedUnattachedReference",
  "IssuedTokens",
  "http://schemas.xmlsoap.org/ws/2005/02/trust/Renew",
  "http://schemas.xmlsoap.org/ws/2005/02/trust/Cancel",
  "http://schemas.xmlsoap.org/ws/2005/02/trust/PublicKey",
  "Access",
  "AccessDecision",
  "Advice",
  "AssertionID",
  "AssertionIDReference",
  # 0x200
  "Attribute",
  "AttributeName",
  "AttributeNamespace",
  "AttributeStatement",
  "AttributeValue",
  "Audience",
  "AudienceRestrictionCondition",
  "AuthenticationInstant",
  "AuthenticationMethod",
  "AuthenticationStatement",
  "AuthorityBinding",
  "AuthorityKind",
  "AuthorizationDecisionStatement",
  "Binding",
  "Condition",
  "Conditions",
  # 0x220
  "Decision",
  "DoNotCacheCondition",
  "Evidence",
  "IssueInstant",
  "Issuer",
  "Location",
  "MajorVersion",
  "MinorVersion",
  "NameIdentifier",
  "Format",
  "NameQualifier",
  "Namespace",
  "NotBefore",
  "NotOnOrAfter",
  "saml",
  "Statement",
  # 0x240
  "Subject",
  "SubjectConfirmation",
  "SubjectConfirmationData",
  "ConfirmationMethod",
  "urn:oasis:names:tc:SAML:1.0:cm:holder-of-key",
  "urn:oasis:names:tc:SAML:1.0:cm:sender-vouches",
  "SubjectLocality",
  "DNSAddress",
  "IPAddress",
  "SubjectStatement",
  "urn:oasis:names:tc:SAML:1.0:am:unspecified",
  "xmlns",
  "Resource",
  "UserName",
  "urn:oasis:names:tc:SAML:1.1:nameid-format:WindowsDomainQualifiedName",
  "EmailName",
  # 0x260
  "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
  "u",
  "ChannelInstance",
  "http://schemas.microsoft.com/ws/2005/02/duplex",
  "Encoding",
  "MimeType",
  "CarriedKeyName",
  "Recipient",
  "EncryptedKey",
  "KeyReference",
  "e",
  "http://www.w3.org/2001/04/xmlenc#Element",
  "http://www.w3.org/2001/04/xmlenc#Content",
  "KeyName",
  "MgmtData",
  "KeyValue",
  # 0x280
  "RSAKeyValue",
  "Modulus",
  "Exponent",
  "X509Data",
  "X509IssuerSerial",
  "X509IssuerName",
  "X509SerialNumber",
  "X509Certificate",
  "AckRequested",
  "http://schemas.xmlsoap.org/ws/2005/02/rm/AckRequested",
  "AcksTo",
  "Accept",
  "CreateSequence",
  "http://schemas.xmlsoap.org/ws/2005/02/rm/CreateSequence",
  "CreateSequenceRefused",
  "CreateSequenceResponse",
  # 0x2a0
  "http://schemas.xmlsoap.org/ws/2005/02/rm/CreateSequenceResponse",
  "FaultCode",
  "InvalidAcknowledgement",
  "LastMessage",
  "http://schemas.xmlsoap.org/ws/2005/02/rm/LastMessage",
  "LastMessageNumberExceeded",
  "MessageNumberRollover",
  "Nack",
  "netrm",
  "Offer",
  "r",
  "SequenceFault",
  "SequenceTerminated",
  "TerminateSequence",
  "http://schemas.xmlsoap.org/ws/2005/02/rm/TerminateSequence",
  "UnknownSequence",
  # 0x2c0
  "http://schemas.microsoft.com/ws/2006/02/tx/oletx",
  "oletx",
  "OleTxTransaction",
  "PropagationToken",
  "http://schemas.xmlsoap.org/ws/2004/10/wscoor",
  "wscoor",
  "CreateCoordinationContext",
  "CreateCoordinationContextResponse",
  "CoordinationContext",
  "CurrentContext",
  "CoordinationType",
  "RegistrationService",
  "Register",
  "RegisterResponse",
  "ProtocolIdentifier",
  "CoordinatorProtocolService",
  # 0x2e0
  "ParticipantProtocolService",
  "http://schemas.xmlsoap.org/ws/2004/10/wscoor/CreateCoordinationContext",
  "http://schemas.xmlsoap.org/ws/2004/10/wscoor/CreateCoordinationContextResponse",
  "http://schemas.xmlsoap.org/ws/2004/10/wscoor/Register",
  "http://schemas.xmlsoap.org/ws/2004/10/wscoor/RegisterResponse",
  "http://schemas.xmlsoap.org/ws/2004/10/wscoor/fault",
  "ActivationCoordinatorPortType",
  "RegistrationCoordinatorPortType",
  "InvalidState",
  "InvalidProtocol",
  "InvalidParameters",
  "NoActivity",
  "ContextRefused",
  "AlreadyRegistered",
  "http://schemas.xmlsoap.org/ws/2004/10/wsat",
  "wsat",
  # 0x300
  "http://schemas.xmlsoap.org/ws/2004/10/wsat/Completion",
  "http://schemas.xmlsoap.org/ws/2004/10/wsat/Durable2PC",
  "http://schemas.xmlsoap.org/ws/2004/10/wsat/Volatile2PC",
  "Prepare",
  "Prepared",
  "ReadOnly",
  "Commit",
  "Rollback",
  "Committed",
  "Aborted",
  "Replay",
  "http://schemas.xmlsoap.org/ws/2004/10/wsat/Commit",
  "http://schemas.xmlsoap.org/ws/2004/10/wsat/Rollback",
  "http://schemas.xmlsoap.org/ws/2004/10/wsat/Committed",
  "http://schemas.xmlsoap.org/ws/2004/10/wsat/Aborted",
  "http://schemas.xmlsoap.org/ws/2004/10/wsat/Prepare",
  # 0x320
  "http://schemas.xmlsoap.org/ws/2004/10/wsat/Prepared",
  "http://schemas.xmlsoap.org/ws/2004/10/wsat/ReadOnly",
  "http://schemas.xmlsoap.org/ws/2004/10/wsat/Replay",
  "http://schemas.xmlsoap.org/ws/2004/10/wsat/fault",
  "CompletionCoordinatorPortType",
  "CompletionParticipantPortType",
  "CoordinatorPortType",
  "ParticipantPortType",
  "InconsistentInternalState",
  "mstx",
  "Enlistment",
  "protocol",
  "LocalTransactionId",
  "IsolationLevel",
  "IsolationFlags",
  "Description",
  # 0x340
  "Loopback",
  "RegisterInfo",
  "ContextId",
  "TokenId",
  "AccessDenied",
  "InvalidPolicy",
  "CoordinatorRegistrationFailed",
  "TooManyEnlistments",
  "Disabled",
  "ActivityId",
  "http://schemas.microsoft.com/2004/09/ServiceModel/Diagnostics",
  "http://docs.oasis-open.org/wss/oasis-wss-kerberos-token-profile-1.1#Kerberosv5APREQSHA1",
  "http://schemas.xmlsoap.org/ws/2002/12/policy",
  "FloodMessage",
  "LinkUtility",
  "Hops",
  # 0x360
  "http://schemas.microsoft.com/net/2006/05/peer/HopCount",
  "PeerVia",
  "http://schemas.microsoft.com/net/2006/05/peer",
  "PeerFlooder",
  "PeerTo",
  "http://schemas.microsoft.com/ws/2005/05/routing",
  "PacketRoutable",
  "http://schemas.microsoft.com/ws/2005/05/addressing/none",
  "http://schemas.microsoft.com/ws/2005/05/envelope/none",
  "http://www.w3.org/2001/XMLSchema-instance",
  "http://www.w3.org/2001/XMLSchema",
  "nil",
  "type",
  "char",
  "boolean",
  "byte",
  # 0x380
  "unsignedByte",
  "short",
  "unsignedShort",
  "int",
  "unsignedInt",
  "long",
  "unsignedLong",
  "float",
  "double",
  "decimal",
  "dateTime",
  "string",
  "base64Binary",
  "anyType",
  "duration",
  "guid",
  # 0x3a0
  "anyURI",
  "QName",
  "time",
  "date",
  "hexBinary",
  "gYearMonth",
  "gYear",
  "gMonthDay",
  "gDay",
  "gMonth",
  "integer",
  "positiveInteger",
  "negativeInteger",
  "nonPositiveInteger",
  "nonNegativeInteger",
  "normalizedString",
  # 0x3c0
  "ConnectionLimitReached",
  "http://schemas.xmlsoap.org/soap/envelope/",
  "actor",
  "faultcode",
  "faultstring",
  "faultactor",
  "detail",
)

_STATIC_KEYS: Dict[str, int] = {value: 2 * index for index, value in enumerate(STATIC_STRINGS)}

# The number of bytes of strings a .NET peer accepts from one sender over one connection
# (the MaxSessionSize of its binary encoder).
DEFAULT_MAX_SESSION_SIZE = 2048


class StaticString(DictionaryString):
  """A string of the [MC-NBFS] table, written as its fixed even key."""

  __slots__ = ()

  def __new__(cls, value: str) -> "StaticString":
    """Create the string; it must be one of ``STATIC_STRINGS``."""
    if value not in _STATIC_KEYS:
      raise ValueError(f"{value!r} is not in the [MC-NBFS] string table")
    return super().__new__(cls, value)


_STATIC: Tuple[StaticString, ...] = tuple(StaticString(value) for value in STATIC_STRINGS)


def static_lookup(key: int) -> DictionaryString:
  """Return the string for a key in a document without a session table ([MC-NBFS])."""
  if key % 2 or key // 2 >= len(_STATIC):
    raise NbfxError(f"dictionary key {key} is not in the [MC-NBFS] string table")
  return _STATIC[key // 2]


def static_key(value: DictionaryString) -> Optional[int]:
  """Return the key to write in a document without a session table ([MC-NBFS])."""
  return _STATIC_KEYS[value] if isinstance(value, StaticString) else None


class ReaderSession:
  """The strings one peer has announced on a connection ([MC-NBFSE] 2.1)."""

  def __init__(self) -> None:
    """Create an empty table."""
    self._strings: List[DictionaryString] = []

  def lookup(self, key: int) -> DictionaryString:
    """Return the string for a key: an even one from the static table, an odd one from here."""
    if key % 2 == 0:
      return static_lookup(key)
    if key // 2 >= len(self._strings):
      raise NbfxError(f"dictionary key {key} was never announced by the peer")
    return self._strings[key // 2]

  def read_string_table(self, envelope: bytes) -> bytes:
    """Take in the StringTable at the head of an envelope and return the document after it.

    Raises:
      NbfxError: The table does not fit its declared size or the envelope.
    """
    try:
      size, position = decode_int31(envelope, 0)
      end = position + size
      if end > len(envelope):
        raise NbfxError("string table is larger than its envelope")
      strings: List[DictionaryString] = []
      while position < end:
        length, position = decode_int31(envelope, position)
        if position + length > end:
          raise NbfxError("string runs past the end of the string table")
        strings.append(DictionaryString(envelope[position : position + length].decode("utf-8")))
        position += length
    except NbfxIncompleteError as error:
      raise NbfxError(f"truncated string table: {error}") from error
    except UnicodeDecodeError as error:
      raise NbfxError(f"malformed string in string table: {error}") from error
    self._strings += strings
    return envelope[end:]


class WriterSession:
  """The strings this side has announced on a connection, within the peer's size limit.

  A string is added the first time a document uses it, as long as it fits what is left of
  the limit; the first string that does not fit closes the table for good, as a .NET
  writer does. Strings that are not in the table are written as plain characters.
  """

  def __init__(self, max_size: int = DEFAULT_MAX_SESSION_SIZE) -> None:
    """Create an empty table.

    Args:
      max_size: The total size of announced strings the peer accepts, counting each
        string's UTF-8 bytes and its length prefix.
    """
    self._remaining = max_size
    self._keys: Dict[str, int] = {}
    self._pending: List[str] = []

  def key_of(self, value: DictionaryString) -> Optional[int]:
    """Return the key to write for a string, adding it to the table if there is room."""
    if isinstance(value, StaticString):
      return _STATIC_KEYS[value]
    key = self._keys.get(value)
    if key is not None or self._remaining == 0:
      return key
    size = len(value.encode("utf-8"))
    required = size + len(encode_int31(size))
    if required > self._remaining:
      self._remaining = 0
      return None
    self._remaining -= required
    key = 2 * len(self._keys) + 1
    self._keys[str(value)] = key
    self._pending.append(str(value))
    return key

  def string_table(self) -> bytes:
    """Return the StringTable that announces the strings added since the last call.

    It goes at the head of the next envelope sent, whether or not that envelope is the one
    that added the strings.
    """
    strings = bytearray()
    for value in self._pending:
      data = value.encode("utf-8")
      strings += encode_int31(len(data)) + data
    self._pending.clear()
    return encode_int31(len(strings)) + bytes(strings)
