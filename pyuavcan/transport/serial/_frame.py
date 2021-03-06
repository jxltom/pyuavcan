#
# Copyright (c) 2019 UAVCAN Development Team
# This software is distributed under the terms of the MIT License.
# Author: Pavel Kirienko <pavel.kirienko@zubax.com>
#

from __future__ import annotations
import typing
import struct
import itertools
import dataclasses
import pyuavcan

_VERSION = 0

# Same value represents broadcast node ID when transmitting.
_ANONYMOUS_NODE_ID = 0xFFFF

_HEADER_WITHOUT_CRC_FORMAT = struct.Struct('<'
                                           'BB'   # Version, priority
                                           'HHH'  # source NID, destination NID, data specifier
                                           'QQ'   # Data type hash, transfer-ID
                                           'L')   # Frame index with end-of-transfer flag in the MSB
_CRC_SIZE_BYTES = len(pyuavcan.transport.commons.high_overhead_transport.TransferCRC().value_as_bytes)
_HEADER_SIZE = _HEADER_WITHOUT_CRC_FORMAT.size + _CRC_SIZE_BYTES
assert _HEADER_SIZE == 32


@dataclasses.dataclass(frozen=True)
class SerialFrame(pyuavcan.transport.commons.high_overhead_transport.Frame):
    NODE_ID_MASK     = 4095
    TRANSFER_ID_MASK = 2 ** 64 - 1
    INDEX_MASK       = 2 ** 31 - 1

    NODE_ID_RANGE = range(NODE_ID_MASK + 1)

    FRAME_DELIMITER_BYTE = 0x9E
    ESCAPE_PREFIX_BYTE   = 0x8E

    NUM_OVERHEAD_BYTES_EXCEPT_DELIMITERS_AND_ESCAPING = _HEADER_SIZE + _CRC_SIZE_BYTES

    source_node_id:      typing.Optional[int]
    destination_node_id: typing.Optional[int]
    data_specifier:      pyuavcan.transport.DataSpecifier
    data_type_hash:      int

    def __post_init__(self) -> None:
        if not isinstance(self.priority, pyuavcan.transport.Priority):
            raise TypeError(f'Invalid priority: {self.priority}')  # pragma: no cover

        if self.source_node_id is not None and not (0 <= self.source_node_id <= self.NODE_ID_MASK):
            raise ValueError(f'Invalid source node ID: {self.source_node_id}')

        if self.destination_node_id is not None and not (0 <= self.destination_node_id <= self.NODE_ID_MASK):
            raise ValueError(f'Invalid destination node ID: {self.destination_node_id}')

        if isinstance(self.data_specifier, pyuavcan.transport.ServiceDataSpecifier) and self.source_node_id is None:
            raise ValueError(f'Anonymous nodes cannot use service transfers: {self.data_specifier}')

        if not isinstance(self.data_specifier, pyuavcan.transport.DataSpecifier):
            raise TypeError(f'Invalid data specifier: {self.data_specifier}')  # pragma: no cover

        if not (0 <= self.data_type_hash <= pyuavcan.transport.PayloadMetadata.DATA_TYPE_HASH_MASK):
            raise ValueError(f'Invalid data type hash: {self.data_type_hash}')

        if not (0 <= self.transfer_id <= self.TRANSFER_ID_MASK):
            raise ValueError(f'Invalid transfer-ID: {self.transfer_id}')

        if not (0 <= self.index <= self.INDEX_MASK):
            raise ValueError(f'Invalid frame index: {self.index}')

        if not isinstance(self.payload, memoryview):
            raise TypeError(f'Bad payload type: {type(self.payload).__name__}')  # pragma: no cover

    def compile_into(self, out_buffer: bytearray) -> memoryview:
        """
        Compiles the frame into the specified output buffer, escaping the data as necessary.
        The buffer must be large enough to accommodate the frame header with the payload and CRC,
        including escape sequences.
        :returns: View of the memory from the beginning of the buffer until the end of the compiled frame.
        """
        src_nid = _ANONYMOUS_NODE_ID if self.source_node_id is None else self.source_node_id
        dst_nid = _ANONYMOUS_NODE_ID if self.destination_node_id is None else self.destination_node_id

        if isinstance(self.data_specifier, pyuavcan.transport.MessageDataSpecifier):
            data_spec = self.data_specifier.subject_id
        elif isinstance(self.data_specifier, pyuavcan.transport.ServiceDataSpecifier):
            is_response = self.data_specifier.role == self.data_specifier.Role.RESPONSE
            data_spec = (1 << 15) | ((1 << 14) if is_response else 0) | self.data_specifier.service_id
        else:
            assert False

        index_eot = self.index | ((1 << 31) if self.end_of_transfer else 0)

        header = _HEADER_WITHOUT_CRC_FORMAT.pack(_VERSION,
                                                 int(self.priority),
                                                 src_nid,
                                                 dst_nid,
                                                 data_spec,
                                                 self.data_type_hash,
                                                 self.transfer_id,
                                                 index_eot)
        header += pyuavcan.transport.commons.crc.CRC32C.new(header).value_as_bytes
        assert len(header) == _HEADER_SIZE

        payload_crc_bytes = pyuavcan.transport.commons.crc.CRC32C.new(self.payload).value_as_bytes

        escapees = self.FRAME_DELIMITER_BYTE, self.ESCAPE_PREFIX_BYTE
        out_buffer[0] = self.FRAME_DELIMITER_BYTE
        next_byte_index = 1
        for nb in itertools.chain(header, self.payload, payload_crc_bytes):
            if nb in escapees:
                out_buffer[next_byte_index] = self.ESCAPE_PREFIX_BYTE
                next_byte_index += 1
                nb ^= 0xFF
            out_buffer[next_byte_index] = nb
            next_byte_index += 1

        out_buffer[next_byte_index] = self.FRAME_DELIMITER_BYTE
        next_byte_index += 1

        assert (next_byte_index - 2) >= (len(header) + len(self.payload) + len(payload_crc_bytes))
        return memoryview(out_buffer)[:next_byte_index]

    @staticmethod
    def parse_from_unescaped_image(header_payload_crc_image: memoryview,
                                   timestamp: pyuavcan.transport.Timestamp) -> typing.Optional[SerialFrame]:
        """
        :returns: Frame or None if the image is invalid.
        """
        if len(header_payload_crc_image) < SerialFrame.NUM_OVERHEAD_BYTES_EXCEPT_DELIMITERS_AND_ESCAPING:
            return None

        header = header_payload_crc_image[:_HEADER_SIZE]
        if not pyuavcan.transport.commons.crc.CRC32C.new(header).check_residue():
            return None

        payload_with_crc = header_payload_crc_image[_HEADER_SIZE:]
        if not pyuavcan.transport.commons.crc.CRC32C.new(payload_with_crc).check_residue():
            return None
        payload = payload_with_crc[:-_CRC_SIZE_BYTES]

        # noinspection PyTypeChecker
        version, int_priority, src_nid, dst_nid, int_data_spec, dt_hash, transfer_id, index_eot = \
            _HEADER_WITHOUT_CRC_FORMAT.unpack_from(header)
        if version != _VERSION:
            return None

        src_nid = None if src_nid == _ANONYMOUS_NODE_ID else src_nid
        dst_nid = None if dst_nid == _ANONYMOUS_NODE_ID else dst_nid

        data_specifier: pyuavcan.transport.DataSpecifier
        if int_data_spec & (1 << 15) == 0:
            data_specifier = pyuavcan.transport.MessageDataSpecifier(int_data_spec)
        else:
            if int_data_spec & (1 << 14):
                role = pyuavcan.transport.ServiceDataSpecifier.Role.RESPONSE
            else:
                role = pyuavcan.transport.ServiceDataSpecifier.Role.REQUEST
            service_id = int_data_spec & pyuavcan.transport.ServiceDataSpecifier.SERVICE_ID_MASK
            data_specifier = pyuavcan.transport.ServiceDataSpecifier(service_id, role)

        try:
            return SerialFrame(timestamp=timestamp,
                               priority=pyuavcan.transport.Priority(int_priority),
                               source_node_id=src_nid,
                               destination_node_id=dst_nid,
                               data_specifier=data_specifier,
                               data_type_hash=dt_hash,
                               transfer_id=transfer_id,
                               index=index_eot & SerialFrame.INDEX_MASK,
                               end_of_transfer=index_eot & (1 << 31) != 0,
                               payload=payload)
        except ValueError:
            return None


# ----------------------------------------  TESTS GO BELOW THIS LINE  ----------------------------------------


def _unittest_frame_compile_message() -> None:
    from pyuavcan.transport import Priority, MessageDataSpecifier, Timestamp

    f = SerialFrame(timestamp=Timestamp.now(),
                    priority=Priority.HIGH,
                    source_node_id=SerialFrame.FRAME_DELIMITER_BYTE,
                    destination_node_id=SerialFrame.ESCAPE_PREFIX_BYTE,
                    data_specifier=MessageDataSpecifier(12345),
                    data_type_hash=0xdead_beef_bad_c0ffe,
                    transfer_id=1234567890123456789,
                    index=1234567,
                    end_of_transfer=True,
                    payload=memoryview(b'abcd\x9Eef\x8E'))

    buffer = bytearray(0 for _ in range(1000))
    mv = f.compile_into(buffer)

    assert mv[0] == SerialFrame.FRAME_DELIMITER_BYTE
    assert mv[-1] == SerialFrame.FRAME_DELIMITER_BYTE
    segment = bytes(mv[1:-1])
    assert SerialFrame.FRAME_DELIMITER_BYTE not in segment

    # Header validation
    assert segment[0] == _VERSION
    assert segment[1] == int(Priority.HIGH)
    assert segment[2] == SerialFrame.ESCAPE_PREFIX_BYTE
    assert (segment[3], segment[4]) == (SerialFrame.FRAME_DELIMITER_BYTE ^ 0xFF, 0)
    assert segment[5] == SerialFrame.ESCAPE_PREFIX_BYTE
    assert (segment[6], segment[7]) == (SerialFrame.ESCAPE_PREFIX_BYTE ^ 0xFF, 0)
    assert segment[8:10] == 12345 .to_bytes(2, 'little')
    assert segment[10:18] == 0xdead_beef_bad_c0ffe .to_bytes(8, 'little')
    assert segment[18:26] == 1234567890123456789 .to_bytes(8, 'little')
    assert segment[26:30] == (1234567 + 0x8000_0000).to_bytes(4, 'little')
    # Header CRC here

    # Payload validation
    assert segment[34:38] == b'abcd'
    assert segment[38] == SerialFrame.ESCAPE_PREFIX_BYTE
    assert segment[39] == 0x9E ^ 0xFF
    assert segment[40:42] == b'ef'
    assert segment[42] == SerialFrame.ESCAPE_PREFIX_BYTE
    assert segment[43] == 0x8E ^ 0xFF
    assert segment[44:] == pyuavcan.transport.commons.crc.CRC32C.new(f.payload).value_as_bytes


def _unittest_frame_compile_service() -> None:
    from pyuavcan.transport import Priority, ServiceDataSpecifier, Timestamp

    f = SerialFrame(timestamp=Timestamp.now(),
                    priority=Priority.FAST,
                    source_node_id=SerialFrame.FRAME_DELIMITER_BYTE,
                    destination_node_id=None,
                    data_specifier=ServiceDataSpecifier(123, ServiceDataSpecifier.Role.RESPONSE),
                    data_type_hash=0xdead_beef_bad_c0ffe,
                    transfer_id=1234567890123456789,
                    index=1234567,
                    end_of_transfer=False,
                    payload=memoryview(b''))

    buffer = bytearray(0 for _ in range(50))
    mv = f.compile_into(buffer)

    assert mv[0] == mv[-1] == SerialFrame.FRAME_DELIMITER_BYTE
    segment = bytes(mv[1:-1])
    assert SerialFrame.FRAME_DELIMITER_BYTE not in segment

    # Header validation
    assert segment[0] == _VERSION
    assert segment[1] == int(Priority.FAST)
    assert segment[2] == SerialFrame.ESCAPE_PREFIX_BYTE
    assert (segment[3], segment[4]) == (SerialFrame.FRAME_DELIMITER_BYTE ^ 0xFF, 0)
    assert (segment[5], segment[6]) == (0xFF, 0xFF)
    assert segment[7:9] == ((1 << 15) | (1 << 14) | 123) .to_bytes(2, 'little')
    assert segment[9:17] == 0xdead_beef_bad_c0ffe .to_bytes(8, 'little')
    assert segment[17:25] == 1234567890123456789 .to_bytes(8, 'little')
    assert segment[25:29] == 1234567 .to_bytes(4, 'little')
    # Header CRC here

    # CRC validation
    assert segment[33:] == pyuavcan.transport.commons.crc.CRC32C.new(f.payload).value_as_bytes


def _unittest_frame_parse() -> None:
    from pyuavcan.transport import Priority, MessageDataSpecifier, ServiceDataSpecifier

    ts = pyuavcan.transport.Timestamp.now()

    def get_crc(*blocks: typing.Union[bytes, memoryview]) -> bytes:
        return pyuavcan.transport.commons.crc.CRC32C.new(*blocks).value_as_bytes

    # Valid message with payload
    header = bytes([
        _VERSION,
        int(Priority.LOW),
        0x7B, 0x00,                                         # Source NID        123
        0xC8, 0x01,                                         # Destination NID   456
        0xE1, 0x10,                                         # Data specifier    4321
        0x0D, 0xF0, 0xDD, 0xE0, 0xFE, 0x0F, 0xDC, 0xBA,     # Data type hash    0xbad_c0ffee_0dd_f00d
        0xD2, 0x0A, 0x1F, 0xEB, 0x8C, 0xA9, 0x54, 0xAB,     # Transfer ID       12345678901234567890
        0x31, 0xD4, 0x00, 0x80,                             # Frame index, EOT  54321 with EOT flag set
    ])
    header += get_crc(header)
    assert len(header) == 32
    payload = b'Squeeze mayonnaise onto a hamster'
    f = SerialFrame.parse_from_unescaped_image(memoryview(header + payload + get_crc(payload)), ts)
    assert f == SerialFrame(
        priority=Priority.LOW,
        source_node_id=123,
        destination_node_id=456,
        data_specifier=MessageDataSpecifier(4321),
        data_type_hash=0xbad_c0ffee_0dd_f00d,
        transfer_id=12345678901234567890,
        index=54321,
        end_of_transfer=True,
        payload=memoryview(payload),
        timestamp=ts,
    )

    # Valid service with no payload
    header = bytes([
        _VERSION,
        int(Priority.LOW),
        0x01, 0x00,
        0x00, 0x00,
        0x10, 0xC0,                                         # Response, service ID 16
        0x0D, 0xF0, 0xDD, 0xE0, 0xFE, 0x0F, 0xDC, 0xBA,
        0xD2, 0x0A, 0x1F, 0xEB, 0x8C, 0xA9, 0x54, 0xAB,
        0x31, 0xD4, 0x00, 0x00,
    ])
    header += get_crc(header)
    assert len(header) == 32
    f = SerialFrame.parse_from_unescaped_image(memoryview(header + get_crc(b'')), ts)
    assert f == SerialFrame(
        priority=Priority.LOW,
        source_node_id=1,
        destination_node_id=0,
        data_specifier=ServiceDataSpecifier(16, ServiceDataSpecifier.Role.RESPONSE),
        data_type_hash=0xbad_c0ffee_0dd_f00d,
        transfer_id=12345678901234567890,
        index=54321,
        end_of_transfer=False,
        payload=memoryview(b''),
        timestamp=ts,
    )

    # Valid service with no payload
    header = bytes([
        _VERSION,
        int(Priority.LOW),
        0x01, 0x00,
        0x00, 0x00,
        0x10, 0x80,                                         # Request, service ID 16
        0x0D, 0xF0, 0xDD, 0xE0, 0xFE, 0x0F, 0xDC, 0xBA,
        0xD2, 0x0A, 0x1F, 0xEB, 0x8C, 0xA9, 0x54, 0xAB,
        0x31, 0xD4, 0x00, 0x00,
    ])
    header += get_crc(header)
    assert len(header) == 32
    f = SerialFrame.parse_from_unescaped_image(memoryview(header + get_crc(b'')), ts)
    assert f == SerialFrame(
        priority=Priority.LOW,
        source_node_id=1,
        destination_node_id=0,
        data_specifier=ServiceDataSpecifier(16, ServiceDataSpecifier.Role.REQUEST),
        data_type_hash=0xbad_c0ffee_0dd_f00d,
        transfer_id=12345678901234567890,
        index=54321,
        end_of_transfer=False,
        payload=memoryview(b''),
        timestamp=ts,
    )

    # Too short
    assert SerialFrame.parse_from_unescaped_image(memoryview(header[1:] + get_crc(payload)), ts) is None

    # Bad CRC
    assert SerialFrame.parse_from_unescaped_image(memoryview(header + payload + b'1234'), ts) is None

    # Bad version
    header = bytes([
        _VERSION + 1,
        int(Priority.LOW),
        0xFF, 0xFF,
        0x00, 0x00,
        0xE1, 0x10,
        0x0D, 0xF0, 0xDD, 0xE0, 0xFE, 0x0F, 0xDC, 0xBA,
        0xD2, 0x0A, 0x1F, 0xEB, 0x8C, 0xA9, 0x54, 0xAB,
        0x31, 0xD4, 0x00, 0x00,
    ])
    header += get_crc(header)
    assert len(header) == 32
    assert SerialFrame.parse_from_unescaped_image(memoryview(header + get_crc(b'')), ts) is None

    # Bad fields
    header = bytes([
        _VERSION,
        0x88,
        0xFF, 0xFF,
        0x00, 0xFF,
        0xE1, 0x10,
        0x0D, 0xF0, 0xDD, 0xE0, 0xFE, 0x0F, 0xDC, 0xBA,
        0xD2, 0x0A, 0x1F, 0xEB, 0x8C, 0xA9, 0x54, 0xAB,
        0x31, 0xD4, 0x00, 0x00,
    ])
    header += get_crc(header)
    assert len(header) == 32
    assert SerialFrame.parse_from_unescaped_image(memoryview(header + get_crc(b'')), ts) is None


def _unittest_frame_check() -> None:
    from pytest import raises
    from pyuavcan.transport import Priority, MessageDataSpecifier, ServiceDataSpecifier, Timestamp

    _ = SerialFrame(timestamp=Timestamp.now(),
                    priority=Priority.HIGH,
                    source_node_id=123,
                    destination_node_id=456,
                    data_specifier=MessageDataSpecifier(12345),
                    data_type_hash=0xdead_beef_bad_c0ffe,
                    transfer_id=1234567890123456789,
                    index=1234567,
                    end_of_transfer=False,
                    payload=memoryview(b'abcdef'))

    with raises(ValueError):
        SerialFrame(timestamp=Timestamp.now(),
                    priority=Priority.HIGH,
                    source_node_id=123456,
                    destination_node_id=456,
                    data_specifier=MessageDataSpecifier(12345),
                    data_type_hash=0xdead_beef_bad_c0ffe,
                    transfer_id=1234567890123456789,
                    index=1234567,
                    end_of_transfer=False,
                    payload=memoryview(b'abcdef'))

    with raises(ValueError):
        SerialFrame(timestamp=Timestamp.now(),
                    priority=Priority.HIGH,
                    source_node_id=123,
                    destination_node_id=123456,
                    data_specifier=MessageDataSpecifier(12345),
                    data_type_hash=0xdead_beef_bad_c0ffe,
                    transfer_id=1234567890123456789,
                    index=1234567,
                    end_of_transfer=False,
                    payload=memoryview(b'abcdef'))

    with raises(ValueError):
        SerialFrame(timestamp=Timestamp.now(),
                    priority=Priority.HIGH,
                    source_node_id=None,
                    destination_node_id=456,
                    data_specifier=ServiceDataSpecifier(123, ServiceDataSpecifier.Role.REQUEST),
                    data_type_hash=0xdead_beef_bad_c0ffe,
                    transfer_id=1234567890123456789,
                    index=1234567,
                    end_of_transfer=False,
                    payload=memoryview(b'abcdef'))

    with raises(ValueError):
        SerialFrame(timestamp=Timestamp.now(),
                    priority=Priority.HIGH,
                    source_node_id=None,
                    destination_node_id=None,
                    data_specifier=MessageDataSpecifier(12345),
                    data_type_hash=2 ** 64,
                    transfer_id=1234567890123456789,
                    index=1234567,
                    end_of_transfer=False,
                    payload=memoryview(b'abcdef'))

    with raises(ValueError):
        SerialFrame(timestamp=Timestamp.now(),
                    priority=Priority.HIGH,
                    source_node_id=None,
                    destination_node_id=None,
                    data_specifier=MessageDataSpecifier(12345),
                    data_type_hash=0xdead_beef_bad_c0ffe,
                    transfer_id=-1,
                    index=1234567,
                    end_of_transfer=False,
                    payload=memoryview(b'abcdef'))

    with raises(ValueError):
        SerialFrame(timestamp=Timestamp.now(),
                    priority=Priority.HIGH,
                    source_node_id=None,
                    destination_node_id=None,
                    data_specifier=MessageDataSpecifier(12345),
                    data_type_hash=0xdead_beef_bad_c0ffe,
                    transfer_id=0,
                    index=-1,
                    end_of_transfer=False,
                    payload=memoryview(b'abcdef'))
