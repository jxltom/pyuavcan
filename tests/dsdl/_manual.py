#
# Copyright (c) 2019 UAVCAN Development Team
# This software is distributed under the terms of the MIT License.
# Author: Pavel Kirienko <pavel.kirienko@zubax.com>
#

import numpy
import typing
import logging
import pyuavcan.dsdl


_logger = logging.getLogger(__name__)


# noinspection PyUnusedLocal
def _unittest_manual_a(generated_packages: typing.List[pyuavcan.dsdl.GeneratedPackageInfo]) -> None:
    import test.str
    obj = pyuavcan.dsdl.try_deserialize(
        test.str.A_1_0,
        _compile_serialized_representation(
            # void1
            '0'
            # B union, second field C.1.0[<=2] y
            '1'
            '10'  # length 2 elements
            # First element C.1.0
            '1'  # second field selected uint1 y
            '1'  # y = 1
            # Second element C.1.0
            '0'  # first field selected uint1 x
            '1'  # x = 1
            # B union, first field C.1.0[2] x
            '0'
            # First element C.1.0
            '0'  # first field selected uint1 x
            '0'  # x = 0
            # Second element C.1.0
            '1'  # second field selected uint1 y
            '1'  # y = 1
            # empty B.1.0[<=2] y
            '00'
        )
    )
    assert obj is not None
    assert obj.x[0].x is None
    assert obj.x[0].y is not None
    assert len(obj.x[0].y) == 2
    assert obj.x[0].y[0].x is None
    assert obj.x[0].y[0].y == 1
    assert obj.x[0].y[1].x == 1
    assert obj.x[0].y[1].y is None
    assert obj.x[1].x is not None
    assert obj.x[1].y is None
    assert obj.x[1].x[0].x == 0
    assert obj.x[1].x[0].y is None
    assert obj.x[1].x[1].x is None
    assert obj.x[1].x[1].y == 1
    assert len(obj.y) == 0


# noinspection PyUnusedLocal
def _unittest_manual_heartbeat(generated_packages: typing.List[pyuavcan.dsdl.GeneratedPackageInfo]) -> None:
    import uavcan.node
    obj = pyuavcan.dsdl.try_deserialize(
        uavcan.node.Heartbeat_1_0,
        _compile_serialized_representation(
            _bin(0xefbe_adde, 32),              # uptime dead beef in little-endian byte order
            '10',                               # health caution
            '010',                              # mode maintenance
            '111''11111111''11111111'           # vendor-specific, 19 bits, little-endian
        )
    )
    assert obj is not None
    assert obj.uptime == 0xdeadbeef
    assert obj.health == uavcan.node.Heartbeat_1_0.HEALTH_CAUTION
    assert obj.mode == uavcan.node.Heartbeat_1_0.MODE_MAINTENANCE
    assert obj.vendor_specific_status_code == 0x7FFFF


def _compile_serialized_representation(*binary_chunks: str) -> typing.Iterable[memoryview]:
    s = ''.join(binary_chunks)
    s = s.ljust(len(s) + 8 - len(s) % 8, '0')
    assert len(s) % 8 == 0
    byte_sized_chunks = [s[i:i + 8] for i in range(0, len(s), 8)]
    byte_list = list(map(lambda x: int(x, 2), byte_sized_chunks))
    out = numpy.array(byte_list, dtype=numpy.uint8)
    _logger.debug('Constructed serialized representation: %r --> %s', binary_chunks, out)
    return [out.data]


def _bin(value: int, width: int) -> str:
    out = bin(value)[2:].zfill(width)
    if len(out) != width:
        raise ValueError(f'Value is too wide: {bin(value)} is wider than {width} bits')
    return out