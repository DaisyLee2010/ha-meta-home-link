"""BlueZ BLE peripheral for Muse app pairing (Linux / HA OS only).

Same service/characteristic UUIDs and advertising name as Portal
Muse / the SDK's ``ble_server.py``. Uses the host BlueZ over the
system D-Bus (add-on: ``host_dbus: true``). ``dbus-next`` is imported
lazily so this module — and its tests — load without Bluetooth.
"""
from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)

SERVICE_UUID = "7fdd3d1c-38ea-46cf-8b46-314ecf5f240c"
RX_UUID = "4d593029-28a2-4a6e-a1f0-3c2d5e8f9b01"  # mobile -> gadget (write)
TX_UUID = "d75dc4ca-7b2b-4e9c-8f0a-1d2e3f4a5b6c"  # gadget -> mobile (notify)
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"

APP_PATH = "/ai/muse/gadget"
ADV_PATH = "/ai/muse/gadget/advertisement"


class BleUnavailable(RuntimeError):
    """BlueZ / D-Bus / adapter cannot advertise a pairing peripheral."""


class BluezPairingPeripheral:
    """Advertises MuseGadgetXXXXXX and pipes GATT packets to a controller."""

    def __init__(self, ble_name: str, controller_factory) -> None:
        self._ble_name = ble_name
        self._controller_factory = controller_factory  # (send, mtu, disconnect) -> controller
        self.controller = None
        self._bus = None
        self._mtu = 23
        self._tx_value = b""
        self._notifying = False
        self._send_queue: asyncio.Queue[bytes] | None = None
        self._sender_task: asyncio.Task | None = None

    # ---------------------------------------------------------- transport
    def _send_packets(self, packets: list[bytes]) -> None:
        if self._send_queue is not None:
            for packet in packets:
                self._send_queue.put_nowait(bytes(packet))

    def _mtu_value(self) -> int:
        return self._mtu

    def _disconnect(self, delay: float) -> None:
        asyncio.ensure_future(self._delayed_stop(delay))

    async def _delayed_stop(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self.stop()

    async def _sender(self) -> None:
        # BlueZ notifies by emitting PropertiesChanged on the TX char.
        while True:
            packet = await self._send_queue.get()  # type: ignore[union-attr]
            self._tx_value = packet
            if self._bus is not None:
                try:
                    self._bus.send(_properties_changed(APP_PATH + "/service0/char1",
                                                       self._tx_value))
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("BLE notify failed", exc_info=True)
            await asyncio.sleep(0.05)  # pace like Portal Muse (50 ms)

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        try:
            from dbus_next.aio import MessageBus
            from dbus_next.constants import BusType
        except ImportError as err:
            raise BleUnavailable("dbus-next is not installed") from err
        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as err:  # noqa: BLE001
            raise BleUnavailable(f"cannot reach the system D-Bus: {err}") from err

        self._send_queue = asyncio.Queue()
        self.controller = self._controller_factory(
            self._send_packets, self._mtu_value, self._disconnect)
        self._register_gatt_application()
        await self._register_advertisement()
        self._sender_task = asyncio.ensure_future(self._sender())
        _LOGGER.info("BLE pairing open as %s", self._ble_name)

    async def stop(self) -> None:
        if self._sender_task is not None:
            self._sender_task.cancel()
            self._sender_task = None
        if self.controller is not None:
            self.controller.on_disconnect()
        if self._bus is not None:
            try:
                from dbus_next import Message, MessageType
                for manager, path, method in (
                    ("org.bluez.GattManager1", "/org/bluez/hci0", "UnregisterApplication"),
                    ("org.bluez.LEAdvertisingManager1", "/org/bluez/hci0", "UnregisterAdvertisement"),
                ):
                    await self._bus.call(Message(
                        destination="org.bluez", path=path, interface=manager,
                        member=method, signature="o",
                        body=[APP_PATH if "Gatt" in manager else ADV_PATH],
                        message_type=MessageType.METHOD_CALL))
            except Exception:  # noqa: BLE001
                pass
            self._bus.disconnect()
            self._bus = None

    # ------------------------------------------------------------ BlueZ glue
    def _register_gatt_application(self) -> None:
        """Export GATT service/chars + ObjectManager on the system bus."""
        from dbus_next import Variant
        from dbus_next.service import ServiceInterface, method, dbus_property
        from dbus_next.constants import PropertyAccess

        outer = self

        class ObjectManager(ServiceInterface):
            def __init__(self) -> None:
                super().__init__("org.freedesktop.DBus.ObjectManager")

            @method()
            def GetManagedObjects(self) -> "a{oa{sa{sv}}}":
                return {
                    APP_PATH + "/service0": {
                        "org.bluez.GattService1": {
                            "UUID": Variant("s", SERVICE_UUID),
                            "Primary": Variant("b", True),
                            "Includes": Variant("ao", []),
                        }
                    },
                    APP_PATH + "/service0/char0": {
                        "org.bluez.GattCharacteristic1": {
                            "UUID": Variant("s", RX_UUID),
                            "Service": Variant("o", APP_PATH + "/service0"),
                            "Flags": Variant("as", ["write", "write-without-response"]),
                        }
                    },
                    APP_PATH + "/service0/char1": {
                        "org.bluez.GattCharacteristic1": {
                            "UUID": Variant("s", TX_UUID),
                            "Service": Variant("o", APP_PATH + "/service0"),
                            "Flags": Variant("as", ["read", "notify"]),
                            "Value": Variant("ay", outer._tx_value),
                        }
                    },
                }

        class GattApplication(ServiceInterface):
            def __init__(self) -> None:
                super().__init__("org.bluez.GattApplication1")

        class RxChar(ServiceInterface):
            def __init__(self) -> None:
                super().__init__("org.bluez.GattCharacteristic1")

            @method()
            def WriteValue(self, value: "ay", options: "a{sv}") -> None:
                if outer.controller is not None:
                    outer.controller.on_packet(bytes(value))

            @method()
            def ReadValue(self, options: "a{sv}") -> "ay":
                return b""

        class TxChar(ServiceInterface):
            def __init__(self) -> None:
                super().__init__("org.bluez.GattCharacteristic1")

            @method()
            def ReadValue(self, options: "a{sv}") -> "ay":
                return outer._tx_value

            @method()
            def StartNotify(self) -> None:
                outer._notifying = True

            @method()
            def StopNotify(self) -> None:
                outer._notifying = False

            @dbus_property(access=PropertyAccess.READ)
            def Value(self) -> "ay":
                return outer._tx_value

        assert self._bus is not None
        self._bus.export(APP_PATH, GattApplication())
        self._bus.export(APP_PATH, ObjectManager())
        self._bus.export(APP_PATH + "/service0/char0", RxChar())
        self._bus.export(APP_PATH + "/service0/char1", TxChar())

    async def _register_advertisement(self) -> None:
        from dbus_next import Message, MessageType, Variant
        from dbus_next.service import ServiceInterface

        ble_name = self._ble_name

        class Advertisement(ServiceInterface):
            def __init__(self) -> None:
                super().__init__("org.bluez.LEAdvertisement1")

            # Properties are read by BlueZ via Properties.GetAll; dbus-next
            # exposes @dbus_property members on export.
            from dbus_next.service import dbus_property
            from dbus_next.constants import PropertyAccess

            @dbus_property(access=PropertyAccess.READ)
            def Type(self) -> "s":
                return "peripheral"

            @dbus_property(access=PropertyAccess.READ)
            def ServiceUUIDs(self) -> "as":
                return [SERVICE_UUID]

            @dbus_property(access=PropertyAccess.READ)
            def LocalName(self) -> "s":
                return ble_name

            @dbus_property(access=PropertyAccess.READ)
            def Discoverable(self) -> "b":
                return True

        assert self._bus is not None
        self._bus.export(ADV_PATH, Advertisement())
        reply = await self._bus.call(Message(
            destination="org.bluez", path="/org/bluez/hci0",
            interface="org.bluez.LEAdvertisingManager1",
            member="RegisterAdvertisement", signature="oa{sv}",
            body=[ADV_PATH, {}], message_type=MessageType.METHOD_CALL))
        if reply.message_type.name == "ERROR":
            raise BleUnavailable(f"BlueZ refused the advertisement: {reply.body}")
        reply = await self._bus.call(Message(
            destination="org.bluez", path="/org/bluez/hci0",
            interface="org.bluez.GattManager1",
            member="RegisterApplication", signature="oa{sv}",
            body=[APP_PATH, {}], message_type=MessageType.METHOD_CALL))
        if reply.message_type.name == "ERROR":
            raise BleUnavailable(f"BlueZ refused the GATT app: {reply.body}")


def _properties_changed(path: str, value: bytes):
    from dbus_next import Message, Variant
    return Message.new_signal(
        path, "org.freedesktop.DBus.Properties", "PropertiesChanged",
        "sa{sv}as", ["org.bluez.GattCharacteristic1",
                     {"Value": Variant("ay", value)}, []])
