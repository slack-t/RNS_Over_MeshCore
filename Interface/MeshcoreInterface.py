# MeshCore Interface for Reticulum Network Stack
# Uses channel messages for reliable RNS transport with auto-configuration and fragmentation

import asyncio
import importlib.util
import os
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
FLAG_FRAGMENTED = 0xFF      # V1: 5-byte header (legacy receive only)
FLAG_FRAGMENTED_V2 = 0xFD   # V2: 4-byte compact header, nibble-packed idx/total
# base85(106 bytes) = 133 chars exactly, fits within MeshCore's 133-char limit
# V2 header: 4 bytes (FLAG + FRAG_ID[2] + packed byte: high nibble=idx, low nibble=total-1)
# Payload per fragment: 106 - 4 = 102 bytes (vs 94 bytes with base64+V1 header: +8.5%)
FRAGMENT_MTU_DEFAULT = 102
FRAGMENT_HEADER_SIZE_V1 = 5
FRAGMENT_HEADER_SIZE_V2 = 4

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

        if self.channel_secret == RNS_CHANNEL_SECRET:
            suggested = os.urandom(16).hex()
            RNS.log(f"[{self.name}] WARNING: Using the default published channel secret. "
                    f"Any node running this software with default settings can read your traffic.", RNS.LOG_WARNING)
            RNS.log(f"[{self.name}] Generate a unique secret and set it on ALL your nodes:", RNS.LOG_WARNING)
            RNS.log(f"[{self.name}]   channel_secret = {suggested}", RNS.LOG_WARNING)

        configured_idx = ifconf.get("channel_idx")
        self.channel_idx = int(configured_idx) if configured_idx is not None else None
        
        self.transport = ifconf.get("transport", "ble").lower()
        self.port = ifconf.get("port", "/dev/ttyUSB0")
        self.baud = int(ifconf.get("baudrate", 115200))
        self.host = ifconf.get("host", "127.0.0.1")
        self.tcp_port = int(ifconf.get("tcp_port", 4403))
        self.ble_name = ifconf.get("ble_name", None)
        self.count_repeat = int(ifconf.get("count_repeat", 1))

        # Interface params
        self.HW_MTU = 500
        self.bitrate = int(ifconf.get("bitrate", 2000))
        self.fragment_mtu = int(ifconf.get("fragment_mtu", FRAGMENT_MTU_DEFAULT))
        # Delay between fragments in seconds
        self.fragment_delay = float(ifconf.get("fragment_delay", 3))
        self.fragment_timeout = int(ifconf.get("fragment_timeout", 180))
        self.fragment_delay_min = float(ifconf.get("fragment_delay_min", 1.0))
        self.fragment_delay_max = float(ifconf.get("fragment_delay_max", 30.0))
        self.delay_step_down = float(ifconf.get("delay_step_down", 0.5))
        self.delay_backoff_factor = float(ifconf.get("delay_backoff_factor", 1.5))
        # Opportunistic sending: send next fragment as soon as previous completes
        # instead of waiting the full adaptive delay. Uses guard_delay as minimum gap.
        self.opportunistic_sending = str(ifconf.get("opportunistic_sending", "false")).lower() == "true"
        self.guard_delay = float(ifconf.get("guard_delay", 0.3))
        # Flood scope: limit message propagation to repeaters allowing this scope.
        # Repeaters must be configured to allow the same scope string.
        self.flood_scope = ifconf.get("flood_scope", None)

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
        self._frag_salt = os.urandom(4)

        # Async TX queue and worker
        self._tx_queue = None
        self._tx_worker_task = None
        self._current_delay = self.fragment_delay

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

        if self.flood_scope:
            result = await self.mesh.commands.set_flood_scope(self.flood_scope)
            if result.type == self._event_type_cls.ERROR:
                RNS.log(f"[{self.name}] Failed to set flood scope '{self.flood_scope}': {result.payload}", RNS.LOG_ERROR)
            else:
                RNS.log(f"[{self.name}] Flood scope set to '{self.flood_scope}'", RNS.LOG_INFO)

        #self.mesh.subscribe(self._event_type_cls.RAW_DATA, self._rx_raw)

        self.mesh.subscribe(self._event_type_cls.CHANNEL_MSG_RECV, self._rx)
        self.mesh.subscribe(self._event_type_cls.ERROR, self._err)
        self.mesh.subscribe(self._event_type_cls.DISCONNECTED, self._err)

        await self.mesh.start_auto_message_fetching()

        with self._lock:
            self.online = True

        mode = "opportunistic" if self.opportunistic_sending else f"adaptive (delay={self.fragment_delay}s)"
        scope_info = f", scope={self.flood_scope}" if self.flood_scope else ""
        RNS.log(f"[{self.name}] MeshCore connected over {self.transport} (channel={self.channel_idx}, mtu={self.fragment_mtu}, mode={mode}{scope_info})", RNS.LOG_INFO)

        # Start async TX queue and worker
        if self._tx_worker_task and not self._tx_worker_task.done():
            self._tx_worker_task.cancel()
            try:
                await self._tx_worker_task
            except asyncio.CancelledError:
                pass
        self._tx_queue = asyncio.Queue()
        self._current_delay = self.fragment_delay
        self._tx_worker_task = asyncio.get_event_loop().create_task(self._tx_worker())

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
        mtu = self.fragment_mtu
        if len(data) <= mtu:
            return [bytes([FLAG_UNFRAGMENTED]) + data]

        fragments = []
        frag_id_bytes = hashlib.md5(self._frag_salt + data).digest()[:2]
        total_chunks = (len(data) + mtu - 1) // mtu

        if total_chunks > 16:
            RNS.log(
                f"[{self.name}] TX: packet ({len(data)}B) requires {total_chunks} fragments "
                f"but V2 header supports max 16 — increase fragment_mtu (current={mtu})",
                RNS.LOG_ERROR,
            )
            return []

        for idx in range(total_chunks):
            start = idx * mtu
            end = min(start + mtu, len(data))
            chunk = data[start:end]
            # V2 compact header: 4 bytes
            # packed byte: high nibble = chunk index (0-15), low nibble = total_chunks-1 (0-15)
            packed = (idx << 4) | (total_chunks - 1)
            header = bytes([FLAG_FRAGMENTED_V2]) + frag_id_bytes + bytes([packed])
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

    def _reassemble_fragment_v2(self, payload: bytes):
        """V2 compact header: FRAG_ID[2] + packed byte (high nibble=idx, low nibble=total-1) + data"""
        if len(payload) < 4:
            return None
        frag_id_key = payload[0:2].hex()
        packed = payload[2]
        chunk_idx = (packed >> 4) & 0x0F
        total_chunks = (packed & 0x0F) + 1
        chunk_data = payload[3:]

        if chunk_idx >= total_chunks:
            RNS.log(f"[{self.name}] RX V2: invalid fragment idx={chunk_idx} total={total_chunks}", RNS.LOG_WARNING)
            return None

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
            expected = set(range(meta["total"]))
            if meta["received"] == expected:
                assembled = b"".join(buf[i] for i in range(meta["total"]))
                del self._fragment_buffers[key]
                del self._fragment_meta[key]
                del self._fragment_timestamps[key]
                return assembled
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

            rx_chan = payload.get("channel_idx")
            if rx_chan != self.channel_idx:
                return

            msg_str = payload.get("text")
            if not msg_str:
                return
            msg_str = self._remove_node_name_from_msg(msg_str)
            try:
                data = base64.b85decode(msg_str)
            except Exception as e:
                RNS.log(f"[{self.name}] RX invalid base85: {e}", RNS.LOG_WARNING)
                return

            if len(data) < 1:
                return

            flags = data[0]
            mesh_payload = data[1:]

            if flags == FLAG_UNFRAGMENTED:
                assembled = mesh_payload
            elif flags == FLAG_FRAGMENTED_V2:
                assembled = self._reassemble_fragment_v2(mesh_payload)
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
        """Queue data for async transmission. Non-blocking."""
        with self._lock:
            if not self.online or self.mesh is None or self.loop is None or self._tx_queue is None:
                return
        try:
            self.loop.call_soon_threadsafe(self._tx_queue.put_nowait, data)
        except Exception as e:
            RNS.log(f"[{self.name}] TX queue error: {e}", RNS.LOG_ERROR)
    async def _send_channel_raw(self, channel_idx: int, msg: str, timestamp: Optional[int] = None) -> Event:
        """
        Send raw bytes to a MeshCore channel, bypassing utf-8 encoding.
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
        Send raw bytes over the air
        """
        
        packet = (
            b"\x19\x00" +
            data
        )
        
        return await self.mesh.commands.send(packet, [self._event_type_cls.OK, self._event_type_cls.ERROR])
    
    async def _tx_worker(self):
        """Async worker: drains TX queue, fragments, sends with interleaved repetition and adaptive pacing."""
        try:
            while not self.detached:
                data = await self._tx_queue.get()

                with self._lock:
                    if not self.online or self.mesh is None:
                        self._tx_queue.task_done()
                        continue

                fragments = self._fragment_outgoing(data)

                for round_num in range(self.count_repeat):
                    for fragment in fragments:
                        # Bitrate pacing
                        if self.bitrate > 0:
                            min_interval = len(fragment) / self.bitrate
                            elapsed = time.time() - self._last_tx
                            if elapsed < min_interval:
                                await asyncio.sleep(min_interval - elapsed)

                        self._last_tx = time.time()
                        tx_start = time.time()
                        success = await self._send_one(fragment)
                        tx_elapsed = time.time() - tx_start

                        with self._lock:
                            self.txb += len(fragment)

                        if self.opportunistic_sending:
                            # Send-completion based pacing: the MeshCore send call
                            # blocks until the radio finishes, so that time already
                            # acts as natural pacing. Only add a small guard delay.
                            if success:
                                remaining = self.guard_delay - tx_elapsed
                                if remaining > 0:
                                    await asyncio.sleep(remaining)
                            else:
                                # Back off on failure even in opportunistic mode
                                self._current_delay = min(
                                    self.fragment_delay_max,
                                    self._current_delay * self.delay_backoff_factor
                                )
                                await asyncio.sleep(self._current_delay)
                        else:
                            # Standard adaptive delay
                            if success:
                                self._current_delay = max(
                                    self.fragment_delay_min,
                                    self._current_delay - self.delay_step_down
                                )
                            else:
                                self._current_delay = min(
                                    self.fragment_delay_max,
                                    self._current_delay * self.delay_backoff_factor
                                )
                            if self._current_delay > 0:
                                await asyncio.sleep(self._current_delay)

                self._tx_queue.task_done()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            RNS.log(f"[{self.name}] TX worker error: {e}\n{traceback.format_exc()}", RNS.LOG_ERROR)
            with self._lock:
                self.online = False

    async def _send_one(self, data) -> bool:
        """Send a single fragment once. Returns True on success, False on error."""
        try:
            msg_str = base64.b85encode(data).decode("ascii")
            result = await self.mesh.commands.send_chan_msg(self.channel_idx, msg_str)
            if result.type == self._event_type_cls.ERROR:
                RNS.log(f"[{self.name}] TX channel error: {result}", RNS.LOG_WARNING)
                return False
            return True
        except Exception as e:
            RNS.log(f"[{self.name}] TX failed: {e}\n{traceback.format_exc()}", RNS.LOG_ERROR)
            with self._lock:
                self.online = False
            return False

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
        return f"{self.name}: {status}, {self.transport}://{location}, channel={chan_str}, MTU={self.HW_MTU} (frag={self.fragment_mtu})"

    def detach(self):
        self.detached = True
        
        with self._lock:
            self.online = False

        if self._tx_worker_task and not self._tx_worker_task.done():
            self._tx_worker_task.cancel()

        if self.loop and self.loop.is_running():
            if self.mesh:
                if self.flood_scope:
                    try:
                        asyncio.run_coroutine_threadsafe(self.mesh.commands.set_flood_scope(None), self.loop)
                    except Exception:
                        pass
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
