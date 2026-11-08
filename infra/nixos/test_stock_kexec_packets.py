"""Classify bounded packets on the offline stock-kexec fixture's private NIC."""

from contextlib import contextmanager
import os
import select
import struct
import threading


CAPTURE_LIMIT = 256 * 1024
SNAPLEN = 384
EXPECTED = bytes([10, 0, 2, 15])


@contextmanager
def packet_pipe(path):
    os.mkfifo(path, 0o600)
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    stop = threading.Event()
    buffer = bytearray()

    def collect():
        while True:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                if stop.is_set():
                    return
                continue
            data = os.read(fd, 8192)
            buffer.extend(data[:max(0, CAPTURE_LIMIT - len(buffer))])

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    try:
        yield buffer
    finally:
        # The caller stops QEMU before closing its local packet reader.
        stop.set()
        reader.join(timeout=3)
        os.close(fd)
        path.unlink()
        if reader.is_alive():
            raise OSError("private packet reader did not stop")


def dhcp_type(data):
    if len(data) < 240 or data[236:240] != b"\x63\x82\x53\x63":
        return None
    index = 240
    while index < len(data):
        option = data[index]
        index += 1
        if option == 255:
            return None
        if option == 0:
            continue
        if index == len(data):
            return None
        length = data[index]
        index += 1
        if index + length > len(data):
            return None
        if option == 53 and length == 1:
            return data[index]
        index += length
    return None


def classify(frame, late, facts):
    if len(frame) < 14:
        return
    protocol = frame[12:14]
    if protocol == b"\x08\x06" and late and len(frame) >= 42:
        operation = frame[20:22]
        if operation == b"\x00\x01" and frame[38:42] == EXPECTED:
            facts["lateArpRequestsToExpected"] += 1
        if operation == b"\x00\x02" and frame[28:32] == EXPECTED:
            facts["lateArpRepliesFromExpected"] += 1
        return
    if protocol != b"\x08\x00" or len(frame) < 34 or frame[14] >> 4 != 4:
        return
    header = (frame[14] & 15) * 4
    if header < 20 or int.from_bytes(frame[20:22], "big") & 0x1FFF:
        return
    payload = frame[14 + header:]
    if len(payload) < 8:
        return
    source, target = struct.unpack_from(">HH", payload)
    if frame[23] == 17 and {source, target} == {67, 68}:
        message = dhcp_type(payload[8:])
        if source == 68 and message == 1:
            facts["dhcpDiscover"] += 1
        if source == 68 and message == 3:
            facts["dhcpRequest"] += 1
        if source == 67 and message == 5:
            facts["dhcpAckExpected" if payload[24:28] == EXPECTED else "dhcpAckAlternate"] += 1
        if source == 67 and message == 6:
            facts["dhcpNak"] += 1
    elif late and frame[23] == 6 and len(payload) >= 20:
        flags = payload[13] & 0x12
        if target == 22 and frame[30:34] == EXPECTED and flags == 0x02:
            facts["lateTcpSynToExpected"] += 1
        if source == 22 and frame[26:30] == EXPECTED:
            if flags == 0x12:
                facts["lateTcpSynAckFromExpected"] += 1
            offset = (payload[12] >> 4) * 4
            if offset >= 20 and payload[offset:].startswith(b"SSH-2.0-"):
                facts["lateBannerFromExpected"] += 1


def packet_facts(data, late_offset):
    order = {b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">"}.get(bytes(data[:4]))
    if not order or len(data) < 24 or struct.unpack_from(order + "II", data, 16) != (SNAPLEN, 1):
        return None
    facts = dict.fromkeys((
        "packets", "dhcpDiscover", "dhcpRequest", "dhcpAckExpected", "dhcpAckAlternate", "dhcpNak",
        "lateArpRequestsToExpected", "lateArpRepliesFromExpected", "lateTcpSynToExpected",
        "lateTcpSynAckFromExpected", "lateBannerFromExpected",
    ), 0)
    facts["captureTruncated"] = len(data) == CAPTURE_LIMIT
    offset = 24
    while offset + 16 <= len(data):
        captured, original = struct.unpack_from(order + "II", data, offset + 8)
        if captured > min(SNAPLEN, original):
            return None
        end = offset + 16 + captured
        if end > len(data):
            break
        facts["packets"] += 1
        classify(data[offset + 16:end], late_offset is not None and offset >= late_offset, facts)
        offset = end
    facts["completeRecords"] = offset == len(data)
    return facts
