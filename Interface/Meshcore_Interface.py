# MeshCore Interface for Reticulum Network Stack
# Uses channel messages for reliable RNS transport with auto-configuration and fragmentation

import asyncio
import importlib.util
import threading
import time
import hashlib
import traceback
from collections import defaultdict
from typing import Optional
import base64

import RNS
from RNS.Interfaces.Interface import Interface
from meshcore.events import Event

# =============================================================================
# PRE-AGREED CHANNEL PARAMETERS (must match on all RNS nodes!)
# =============================================================================
RNS_CHANNEL_NAME = "RNSTunnel"
RNS_CHANNEL_SECRET = bytes.fromhex("c4d2b6c8254e3b11200f57e95dcb1197") #DON'T USE THIS PUBLIC KEY, YOU WILL RUIN OTHER PEOPLE'S LIVES = 8b3387e9c5cdea6ac9e5edbaa115cd72
RNS_CHANNEL_MAX = 39  # Firmware supports channels 0-39
RNS_CHANNEL_FALLBACK = 39  # Last valid channel if none free


# =============================================================================
# FRAGMENTATION PARAMETERS
# =============================================================================
FLAG_UNFRAGMENTED = 0xFE
FLAG_FRAGMENTED = 0xFF
FRAGMENT_MTU = 100
FRAGMENT_HEADER_SIZE = 5

class MeshCoreInterface(Interface):
    DEFAULT_IFAC_SIZE = 8

    def __init__(self, owner, configuration):
        if importlib.util.find_spec("meshcore") is None:
            RNS.log("The MeshCore interface requires the 'meshcore' module to be installed.", RNS.LOG_CRITICAL)
            RNS.log("Install it with: pip install meshcore", RNS.LOG_CRITICAL)
            RNS.panic()

        from meshcore import EventType, MeshCore
        from meshcore.events import Event
        super().__init__()
        
        # Config
        ifconf = Interface.get_config_obj(configuration)
        self.name = ifconf.get("name", "MeshCore")
        self.owner = owner
        
        self.channel_name = ifconf.get("channel_name", RNS_CHANNEL_NAME)
        secret_hex = ifconf.get("channel_secret", RNS_CHANNEL_SECRET.hex())
        self.channel_secret = bytes.fromhex(secret_hex)
        
        configured_idx = ifconf.get("channel_idx")
        self.channel_idx = int(configured_idx) if configured_idx is not None else None
        
        self.transport = ifconf.get("transport", "ble").lower()
        self.port = ifconf.get("port", "/dev/ttyUSB0")
        self.baud = int(ifconf.get("baudrate", 115200))
        self.host = ifconf.get("host", "127.0.0.1")
        self.tcp_port = int(ifconf.get("tcp_port", 4403))
        self.ble_name = ifconf.get("ble_name", None)
        self.count_repeat = ifconf.get("count_repeat", 1)
        
        # Interface params
        self.HW_MTU = 564
        self.bitrate = int(ifconf.get("bitrate", 2000))
        # 🔑 Задержка между фрагментами в секундах (по умолчанию 0.1 = 100 мс)
        self.fragment_delay = float(ifconf.get("fragment_delay", 20))
        self.fragment_timeout = int(ifconf.get("fragment_timeout", 180))
        
        # State
        self.online = False
        self.detached = False
        self._last_tx = 0
        self._lock = threading.Lock()
        
        # Fragmentation buffers
        self._fragment_buffers = defaultdict(dict)
        self._fragment_meta = {}
        self._fragment_timestamps = {}
        self._recent_packets = {}
        
        # MeshCore refs
        self._meshcore_cls = MeshCore
        self._event_type_cls = EventType
        #global _event_cls
        #self._event_cls = Event

        self.mesh = None
        self.loop = None
        self.thread = None

        self.thread = threading.Thread(target=self._async_thread, daemon=True)
        self.thread.start()

    def _async_thread(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self._connect_loop())
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    async def _connect_loop(self):
        while not self.detached:
            try:
                await self._connect_once()
                return
            except Exception as e:
                with self._lock:
                    self.online = False
                RNS.log(f"[{self.name}] MeshCore connect failed: {e}\n{traceback.format_exc()}", RNS.LOG_ERROR)
                await asyncio.sleep(3)

    async def _find_free_channel(self):
        if self.channel_idx is not None:
            idx = self.channel_idx
            if not 0 <= idx <= RNS_CHANNEL_MAX:
                RNS.log(f"[{self.name}] Configured channel {idx} out of range (0-{RNS_CHANNEL_MAX})", RNS.LOG_ERROR)
                return None
            
            result = await self.mesh.commands.get_channel(idx)
            if result.type == self._event_type_cls.CHANNEL_INFO:
                payload = result.payload
                if (payload.get("channel_name") == self.channel_name and 
                    payload.get("channel_secret") == self.channel_secret):
                    RNS.log(f"[{self.name}] Channel {idx} already configured correctly", RNS.LOG_INFO)
                    return idx
            set_result = await self.mesh.commands.set_channel(idx, self.channel_name, self.channel_secret)
            if set_result.type == self._event_type_cls.OK:
                RNS.log(f"[{self.name}] Claimed configured channel {idx}", RNS.LOG_INFO)
                return idx
            RNS.log(f"[{self.name}] Failed to claim configured channel {idx}", RNS.LOG_DEBUG)
            return None
        
        for idx in reversed(range(RNS_CHANNEL_MAX + 1)):
            try:
                result = await self.mesh.commands.get_channel(idx)
                
                if result.type == self._event_type_cls.CHANNEL_INFO:
                    payload = result.payload
                    name = payload.get("channel_name", "")
                    secret = payload.get("channel_secret", b"")
                    
                    if name == self.channel_name and secret == self.channel_secret:
                        RNS.log(f"[{self.name}] Found our channel {idx}", RNS.LOG_INFO)
                        return idx
                    
                    if name == "" and secret == bytes(16):
                        set_result = await self.mesh.commands.set_channel(idx, self.channel_name, self.channel_secret)
                        if set_result.type == self._event_type_cls.OK:
                            RNS.log(f"[{self.name}] Claimed free channel {idx}", RNS.LOG_INFO)
                            return idx
                        RNS.log(f"[{self.name}] Failed to claim free channel {idx}", RNS.LOG_DEBUG)
                    else:
                        RNS.log(f"[{self.name}] Channel {idx} occupied, skipping", RNS.LOG_DEBUG)
                    continue
                
                set_result = await self.mesh.commands.set_channel(idx, self.channel_name, self.channel_secret)
                if set_result.type == self._event_type_cls.OK:
                    RNS.log(f"[{self.name}] Claimed channel {idx}", RNS.LOG_INFO)
                    return idx
                    
            except Exception as e:
                RNS.log(f"[{self.name}] Error checking channel {idx}: {e}", RNS.LOG_DEBUG)
                continue
        
        RNS.log(f"[{self.name}] No free channel found (0-{RNS_CHANNEL_MAX})", RNS.LOG_WARNING)
        return None

    async def _ensure_channel(self):
        if len(self.channel_secret) != 16:
            RNS.log(f"[{self.name}] Invalid secret length {len(self.channel_secret)} (must be 16 bytes)", RNS.LOG_ERROR)
            return False
        
        channel = await self._find_free_channel()
        
        if channel is not None:
            self.channel_idx = channel
            RNS.log(f"[{self.name}] Using channel {self.channel_idx}", RNS.LOG_INFO)
            return True
        
        fallback = RNS_CHANNEL_FALLBACK
        RNS.log(f"[{self.name}] Falling back to channel {fallback}", RNS.LOG_WARNING)
        
        try:
            result = await self.mesh.commands.set_channel(fallback, self.channel_name, self.channel_secret)
            if result.type == self._event_type_cls.OK:
                self.channel_idx = fallback
                RNS.log(f"[{self.name}] Fallback channel {fallback} configured", RNS.LOG_INFO)
                return True
            else:
                RNS.log(f"[{self.name}] Failed to configure fallback channel: {result.payload}", RNS.LOG_ERROR)
                return False
        except Exception as e:
            RNS.log(f"[{self.name}] Error configuring fallback channel: {e}\n{traceback.format_exc()}", RNS.LOG_ERROR)
            return False

    async def _connect_once(self):
        if self.transport == "serial":
            self.mesh = await self._meshcore_cls.create_serial(self.port, self.baud)
        elif self.transport == "tcp":
            self.mesh = await self._meshcore_cls.create_tcp(self.host, self.tcp_port)
        elif self.transport == "ble":
            self.mesh = await self._open_ble_mesh()
        else:
            raise ValueError(f"Invalid transport '{self.transport}'")

        if self.mesh is None:
            raise IOError("MeshCore returned no connection object")

        if not await self._ensure_channel():
            raise IOError("Failed to configure any channel for RNS")

        #self.mesh.subscribe(self._event_type_cls.RAW_DATA, self._rx_raw)

        self.mesh.subscribe(self._event_type_cls.CHANNEL_MSG_RECV, self._rx)
        self.mesh.subscribe(self._event_type_cls.ERROR, self._err)
        self.mesh.subscribe(self._event_type_cls.DISCONNECTED, self._err)
        
        await self.mesh.start_auto_message_fetching()
        
        with self._lock:
            self.online = True
        
        RNS.log(f"[{self.name}] MeshCore connected over {self.transport} (channel={self.channel_idx})", RNS.LOG_INFO)

    async def _open_ble_mesh(self):
        if self.ble_name:
            try:
                from bleak import BleakScanner
            except ImportError:
                raise ImportError("BLE transport requires 'bleak' package: pip install bleak")

            RNS.log(f"[{self.name}] Scanning for BLE device '{self.ble_name}'...", RNS.LOG_INFO)
            devices = await BleakScanner.discover(timeout=5.0)
            
            for device in devices:
                if device.name == self.ble_name:
                    RNS.log(f"[{self.name}] Found {self.ble_name} @ {device.address}", RNS.LOG_INFO)
                    return await self._meshcore_cls.create_ble(address=device.address)
            
            raise IOError(f"BLE device '{self.ble_name}' not found")
        
        return await self._meshcore_cls.create_ble()

    def _fragment_outgoing(self, data):
        if len(data) <= FRAGMENT_MTU:
            return [bytes([FLAG_UNFRAGMENTED]) + data]
        
        fragments = []
        frag_id_bytes = hashlib.md5(data).digest()[:2]
        total_chunks = (len(data) + FRAGMENT_MTU - 1) // FRAGMENT_MTU
        
        for idx in range(total_chunks):
            start = idx * FRAGMENT_MTU
            end = min(start + FRAGMENT_MTU, len(data))
            chunk = data[start:end]
            
            header = bytes([FLAG_FRAGMENTED]) + frag_id_bytes + bytes([idx, total_chunks])
            fragments.append(header + chunk)
        
        return fragments

    def _reassemble_fragment(self, payload: bytes):
        if len(payload) < 5:
            return None
        frag_id_bytes = payload[0:2]
        frag_id_key = frag_id_bytes.hex()
        
        chunk_idx = payload[2]
        total_chunks = payload[3]
        chunk_data = payload[4:]
        
        key = frag_id_key
        if key not in self._fragment_meta:
            self._fragment_meta[key] = {"total": total_chunks, "received": set()}
            self._fragment_buffers[key] = {}
            self._fragment_timestamps[key] = time.time()
        
        meta = self._fragment_meta[key]
        buf = self._fragment_buffers[key]
        buf[chunk_idx] = chunk_data
        meta["received"].add(chunk_idx)
        
        if len(meta["received"]) == meta["total"]:
            expected_indices = set(range(meta["total"]))
            if meta["received"] == expected_indices:
                assembled = b''.join(buf[i] for i in range(meta["total"]))
                del self._fragment_buffers[key]
                del self._fragment_meta[key]
                del self._fragment_timestamps[key]
                return assembled
            else:
                missing = expected_indices - meta["received"]
                RNS.log(f"[{self.name}] RX frag: missing chunks {missing}", RNS.LOG_DEBUG)
                return None
        return None

    def _is_duplicate_packet(self, data: bytes) -> bool:
        pkt_hash = hashlib.md5(data).hexdigest()
        now = time.time()
        if pkt_hash in self._recent_packets and now - self._recent_packets[pkt_hash] < self.fragment_timeout:
            RNS.log(f"[{self.name}] RX: dropping duplicate packet", RNS.LOG_DEBUG)
            return True
        self._recent_packets[pkt_hash] = now
        return False

    async def _rx_raw(self, event):
        try:
            now = time.time()
            with self._lock:
                expired_keys = [
                    key for key, ts in self._fragment_timestamps.items()
                    if now - ts > self.fragment_timeout
                ]
                for key in expired_keys:
                    RNS.log(f"[{self.name}] RX: cleaning up expired fragment {key}", RNS.LOG_DEBUG)
                    self._fragment_buffers.pop(key, None)
                    self._fragment_meta.pop(key, None)
                    self._fragment_timestamps.pop(key, None)
                expired_dedup = [
                    h for h, ts in self._recent_packets.items()
                    if now - ts > self.fragment_timeout
                ]
                for h in expired_dedup:
                    del self._recent_packets[h]

            print(event)
            data = event.payload
            
            if len(data) < 1:
                return
            
            flags = data[0]
            mesh_payload = data[1:]
            
            if flags == FLAG_UNFRAGMENTED:
                assembled = mesh_payload
            elif flags == FLAG_FRAGMENTED:
                assembled = self._reassemble_fragment(mesh_payload)
            else:
                return
            
            if assembled is None:
                return

            if self._is_duplicate_packet(assembled):
                return

            with self._lock:
                self.rxb += len(assembled)
            self.owner.inbound(assembled, self)

        except Exception as e:
            RNS.log(f"[{self.name}] RX error: {e}\n{traceback.format_exc()}", RNS.LOG_ERROR)
    async def _rx(self, event):
        try:
            now = time.time()
            with self._lock:
                expired_keys = [
                    key for key, ts in self._fragment_timestamps.items()
                    if now - ts > self.fragment_timeout
                ]
                for key in expired_keys:
                    RNS.log(f"[{self.name}] RX: cleaning up expired fragment {key}", RNS.LOG_DEBUG)
                    self._fragment_buffers.pop(key, None)
                    self._fragment_meta.pop(key, None)
                    self._fragment_timestamps.pop(key, None)
                expired_dedup = [
                    h for h, ts in self._recent_packets.items()
                    if now - ts > self.fragment_timeout
                ]
                for h in expired_dedup:
                    del self._recent_packets[h]

            payload = event.payload
            if not isinstance(payload, dict):
                return
            
            if payload.get("channel_idx") != self.channel_idx:
                return
            
            msg_str = payload.get("text")
            if not msg_str:
                return
            msg_str = self._remove_node_name_from_msg(msg_str)
            #print(f"DEBUG {msg_str}")
            try:
                data = base64.b64decode(msg_str, validate=True)
            except Exception as e:
                RNS.log(f"[{self.name}] RX invalid base64: {e}", RNS.LOG_WARNING)
                return
            
            if len(data) < 1:
                return
            
            flags = data[0]
            mesh_payload = data[1:]
            
            if flags == FLAG_UNFRAGMENTED:
                assembled = mesh_payload
            elif flags == FLAG_FRAGMENTED:
                assembled = self._reassemble_fragment(mesh_payload)
            else:
                return
            
            if assembled is None:
                return

            if self._is_duplicate_packet(assembled):
                return

            with self._lock:
                self.rxb += len(assembled)
            self.owner.inbound(assembled, self)

        except Exception as e:
            RNS.log(f"[{self.name}] RX error: {e}\n{traceback.format_exc()}", RNS.LOG_ERROR)

    async def _err(self, event):
        try:
            event_payload = event.payload if event and hasattr(event, "payload") else "unknown"
            
            if (
                isinstance(event_payload, dict) 
                and event_payload.get("error_code") in (1, 2)
                and self.transport == "ble"
            ):
                RNS.log(f"[{self.name}] Transient BLE error (code={event_payload.get('error_code')}), keeping connection", RNS.LOG_DEBUG)
                return
            
            RNS.log(f"[{self.name}] MeshCore event error: {event_payload}", RNS.LOG_ERROR)
            
            with self._lock:
                self.online = False
        except Exception as e:
            RNS.log(f"[{self.name}] _err handler error: {e}\n{traceback.format_exc()}", RNS.LOG_ERROR)

    def process_outgoing(self, data):
        with self._lock:
            if not self.online or self.mesh is None or self.loop is None:
                return

        fragments = self._fragment_outgoing(data)
        
        now = time.time()
        
        for i, fragment in enumerate(fragments):
            # Rate limiting по bitrate
            if self.bitrate > 0:
                min_interval = len(fragment) / self.bitrate
                elapsed = now - self._last_tx
                if elapsed < min_interval:
                    time.sleep(min_interval - elapsed)
                    now = time.time()
            self._last_tx = now

            

            with self._lock:
                self.txb += len(fragment)

            # 🔑 Callback для обработки ошибок
            def _tx_callback(fut, frag_num=i+1):
                try:
                    fut.result()
                except Exception as e:
                    RNS.log(f"[{self.name}] TX async error on fragment {frag_num}: {e}", RNS.LOG_ERROR)
                    with self._lock:
                        self.online = False
            
            # 🔑 Отправляем асинхронно
            if not self.detached and self.loop and self.loop.is_running():
                future = asyncio.run_coroutine_threadsafe(self._send(fragment), self.loop)
                future.add_done_callback(_tx_callback)
            
            # 🔑 Задержка между фрагментами (синхронная)
            if self.fragment_delay > 0:
                time.sleep(self.fragment_delay)
                now = time.time()
    async def _send_channel_raw(self, channel_idx: int, msg: str, timestamp: Optional[int] = None) -> Event:
        """
        Отправляет сырые байты в канал MeshCore, минуя utf-8 кодирование.
        """
        if timestamp is None:
            import time
            timestamp_bytes = int(time.time()).to_bytes(4, "little")
        elif isinstance(timestamp, int):
            timestamp_bytes = timestamp.to_bytes(4, "little")
        else:
            import time
            timestamp_bytes = int(time.time()).to_bytes(4, "little")
        
        packet = (
            b"\x03\x00" +
            channel_idx.to_bytes(1, "little") +
            timestamp_bytes +
            msg.encode("latin-1")
        )
        
        return await self.mesh.commands.send(packet, [self._event_type_cls.OK, self._event_type_cls.ERROR])
    async def _send_raw(self, data: bytes) -> Event:
        """
        Отправляет сырые байты в эфир
        """
        
        packet = (
            b"\x19\x00" +
            data
        )
        
        return await self.mesh.commands.send(packet, [self._event_type_cls.OK, self._event_type_cls.ERROR])
    
    async def _send(self, data):
        """Send RNS packet as channel message"""
        try:
            for i in range(self.count_repeat):
                msg_str = base64.b64encode(data).decode("ascii")
                result = await self.mesh.commands.send_chan_msg(self.channel_idx, msg_str)
                #result = await self._send_channel_raw(self.channel_idx, msg_str)
                #result = await self._send_raw(data)

                if result.type == self._event_type_cls.ERROR:
                    RNS.log(f"[{self.name}] TX channel error: {result}", RNS.LOG_WARNING)
                RNS.log(f"[{self.name}] TX channel result: {result}", RNS.LOG_DEBUG)
                RNS.log(f"[{self.name}] TX data: {msg_str}", RNS.LOG_DEBUG)
            
                
        except Exception as e:
            RNS.log(f"[{self.name}] TX failed: {e}\n{traceback.format_exc()}", RNS.LOG_ERROR)
            raise

    def should_ingress_limit(self):
        return False

    def get_status_string(self):
        status = "Online" if self.online else "Offline"
        if self.transport == "serial":
            location = f"{self.port}@{self.baud}"
        elif self.transport == "tcp":
            location = f"{self.host}:{self.tcp_port}"
        elif self.transport == "ble":
            location = self.ble_name or "BLE:auto"
        else:
            location = "unknown"
        chan_str = f"{self.channel_idx}" if self.channel_idx is not None else "auto"
        return f"{self.name}: {status}, {self.transport}://{location}, channel={chan_str}, MTU={self.HW_MTU} (frag={FRAGMENT_MTU})"

    def detach(self):
        self.detached = True
        
        with self._lock:
            self.online = False
        
        if self.loop and self.loop.is_running():
            if self.mesh:
                try:
                    asyncio.run_coroutine_threadsafe(self.mesh.disconnect(), self.loop)
                except Exception as e:
                    RNS.log(f"[{self.name}] detach disconnect error: {e}\n{traceback.format_exc()}", RNS.LOG_ERROR)
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except Exception as e:
                RNS.log(f"[{self.name}] detach loop stop error: {e}\n{traceback.format_exc()}", RNS.LOG_ERROR)
        
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        
        RNS.log(f"[{self.name}] Detached", RNS.LOG_INFO)

    def _remove_node_name_from_msg(self, text: str):
        original_text = text
        parts = original_text.split(": ")
        if len(parts) >= 2:
            new_text = ": ".join(parts[1:]).strip()
            if new_text:
                text = new_text
            else:
                text = original_text
        return text

    def __str__(self):
        return f"MeshCoreInterface[{self.name}]"


interface_class = MeshCoreInterface
