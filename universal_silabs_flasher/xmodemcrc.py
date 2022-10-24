import typing
import asyncio
import logging
import dataclasses

import zigpy.types

from .common import crc16_ccitt

_LOGGER = logging.getLogger(__name__)

BLOCK_SIZE = 128


class Symbol(zigpy.types.enum8):
    SOH = 0x01  # Start of Header
    EOT = 0x04  # End of Transmission
    CAN = 0x18  # Cancel
    ETB = 0x17  # End of Transmission Block
    ACK = 0x06  # Acknowledge
    NAK = 0x15  # Not Acknowledge


@dataclasses.dataclass(frozen=True)
class Xmodem128CRCPacket:
    number: zigpy.types.uint8_t
    payload: bytes

    def serialize(self) -> bytes:
        return (
            bytes([Symbol.SOH, self.number, 0xFF - self.number])
            + self.payload
            + crc16_ccitt(self.payload).to_bytes(2, "big")
        )


class ReceiverCancelled(Exception):
    pass


async def send_xmodem128_crc_data(
    data: bytes,
    *,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamReader,
    max_failures: int,
) -> None:
    for attempt in range(max_failures + 1):
        # Send off the data
        _LOGGER.debug("Sending data %r (attempt %d)", data, attempt)
        writer.write(data)
        await writer.drain()

        # And wait for a response
        rsp_byte = await reader.readexactly(1)
        _LOGGER.debug("Got response: %r", rsp_byte)

        if rsp_byte[0] == Symbol.ACK:
            return
        elif rsp_byte[0] == Symbol.NAK:
            _LOGGER.debug("Got a NAK, retrying")

            if attempt >= max_failures:
                raise ValueError(f"Received {max_failures} consecutive failures")
        elif rsp_byte[0] == Symbol.CAN:
            raise ReceiverCancelled()
        else:
            raise ValueError(f"Invalid response: {rsp_byte!r}")


async def send_xmodem128_crc(
    data: bytes,
    *,
    transport: asyncio.Transport,
    max_failures: int = 3,
    progress_callback: typing.Callable[[int, int], typing.Any] | None = None,
) -> None:
    if len(data) % BLOCK_SIZE != 0:
        raise ValueError(f"Data length must be divisible by {BLOCK_SIZE}: {len(data)}")

    loop = asyncio.get_running_loop()

    reader = asyncio.StreamReader(limit=65536, loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)

    # Swap protocols
    old_protocol = transport.get_protocol()
    transport.set_protocol(protocol)

    try:
        # Read until the first ASCII "C"
        await reader.readuntil(b"C")

        if progress_callback is not None:
            progress_callback(0, len(data))

        # FIXME: ensure any subsequent "C"s have been cleared so they do not interfere
        reader._buffer.clear()

        for index in range(0, len(data) // BLOCK_SIZE):
            packet = Xmodem128CRCPacket(
                number=(index + 1) & 0xFF,  # `seq` starts at 1 and then wraps
                payload=data[BLOCK_SIZE * index : BLOCK_SIZE * (index + 1)],
            )

            # Send off the packet
            await send_xmodem128_crc_data(
                data=packet.serialize(),
                reader=reader,
                writer=writer,
                max_failures=max_failures,
            )

            if progress_callback is not None:
                progress_callback((index + 1) * BLOCK_SIZE, len(data))

        # Once we are done, finalize the transmission
        await send_xmodem128_crc_data(
            data=bytes([Symbol.EOT]),
            reader=reader,
            writer=writer,
            max_failures=max_failures,
        )
    finally:
        # Reset the old protocol
        transport.set_protocol(old_protocol)