# RNS_Over_MeshCore

Interface for Reticulum Network Stack (RNS) using MeshCore as the underlying networking layer to utilize existing LoRa/mesh hardware.

⚠️ TESTED — BEHAVIOR WARNING

>MeshCore-based firmware on many devices is extremely unstable. Even under perfect conditions, sending a single CHUNK of a Reticulum packet often requires repeating the same command multiple times, and delivery is still not guaranteed. This interface handles fragmentation internally: each outgoing packet is split into CHUNKS with fragment IDs, and incoming fragments are reassembled automatically. Future improvements may include enhanced transport-layer reliability and protocol tagging.

---

## Requirements

- Python 3.10+
- [meshcore](https://pypi.org/project/meshcore/) Python library
- Reticulum Network Stack (`rnsd`)
- Compatible LoRa device with MeshCore firmware

---

## Installation

1. **Install MeshCore Python library:**
   ```bash
   pip install meshcore
   ```

2. **Copy the interface file:**
   - Place `Meshcore_Interface.py` in your Reticulum interfaces folder:
     - **Linux/macOS:** `~/.reticulum/interfaces/`
     - **Windows:** `C:\Users\<YourName>\.reticulum\interfaces\`

3. **Configure the interface** (see below)

4. **Restart `rnsd`**

---

## Configuration

Add the following to your `~/.reticulum/config` file:

```ini
# =========================================================
# ⚠️ WARNING: MeshCore firmware is extremely unstable.
# You will often need to spam the same command multiple times
# just to get one CHUNK of a packet sent. Even then delivery
# is not guaranteed. Test one device at a time, expect failures,
# and do not rely on this for critical communication.
# =========================================================

[[MeshCore]]
   type = MeshCoreInterface
   interface_enabled = true

   # === Transport settings ===
   transport = ble           # Options: ble | serial | tcp
   #port = /dev/ttyUSB0       # Serial port if transport = serial
   #baudrate = 115200         # Serial baudrate
   #host = 127.0.0.1          # TCP host if transport = tcp
   #tcp_port = 4403           # TCP port if transport = tcp
   ble_name = MeshCore-Obdolbus  # BLE device name (optional, auto-scan if empty)

   # === RNS channel settings (DO NOT CHANGE SECRET unless you know what you are doing) ===
   # channel_name = RNSTunnel
   # channel_secret = c4d2b6c8254e3b11200f57e95dcb1197  # 16 bytes hex
   # channel_idx =                                        # Leave empty to auto-select, fallback = 39

   # === Fragmentation / reliability ===
   #count_repeat = 7            # How many times to send each fragment (spam to increase chance of delivery)
   #fragment_timeout = 180       # Timeout for incomplete fragment reassembly (seconds)
   #fragment_delay = 20         # Delay between fragments in seconds — yes, MeshCore is THAT bad
   #bitrate = 2000              # Rate limiting in bytes/sec, 0 = unlimited

```

## Recent Fixes

### Configurable fragment timeout
The `fragment_timeout` setting was previously documented but never actually read from config — the timeout was hardcoded to 180 seconds. It is now properly wired up, so users can tune how long incomplete fragment reassemblies are kept in memory before being discarded. This prevents unbounded memory growth on long-running nodes that receive partial transmissions.

### Bitrate limiter
The bitrate-based pacing between fragments was implemented but the actual delay (`time.sleep`) was commented out, meaning the `bitrate` config setting had no effect. It is now active, enforcing the configured bytes/sec rate limit between fragment transmissions alongside `fragment_delay`.

### Receive-side deduplication
When `count_repeat` is set above 1, every fragment is sent multiple times to improve delivery probability over unreliable MeshCore links. However, this could cause the same fully-reassembled packet to be delivered to RNS more than once. A deduplication layer now tracks recently received packet hashes and silently drops duplicates within the `fragment_timeout` window.

### Corrected documentation defaults
The example config values for `fragment_timeout` (was 3600, actual default 180) and `bitrate` (was 200, actual default 2000) have been corrected to match the code defaults.

---

## Acknowledgements

Special thanks to [HDDen](https://github.com/HDDen/) for their help with the MeshCore integration and debugging.

## License

MIT License — See LICENSE file for details.
