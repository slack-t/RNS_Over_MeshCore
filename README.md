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

   # === RNS channel settings ===
   # IMPORTANT: Change channel_secret to a unique value! The default is published
   # in the source code — anyone using it can read your traffic. Generate one with:
   #   python3 -c "import os; print(os.urandom(16).hex())"
   # Then set the SAME secret on ALL your RNS-over-MeshCore nodes.
   # channel_name = RNSTunnel
   # channel_secret = <your-unique-16-byte-hex-secret>
   # channel_idx =                                        # Leave empty to auto-select, fallback = 39

   # === Fragmentation / reliability ===
   #count_repeat = 1              # Number of full interleaved rounds to send all fragments
   #fragment_delay = 20           # Starting delay between fragments (seconds), adapts automatically
   #fragment_delay_min = 2        # Minimum adaptive delay (seconds)
   #fragment_delay_max = 60       # Maximum adaptive delay (seconds)
   #delay_step_down = 0.5         # Seconds subtracted from delay on each successful send
   #delay_backoff_factor = 1.5    # Multiplier applied to delay on each failed send
   #fragment_timeout = 180        # Timeout for incomplete fragment reassembly (seconds)
   #bitrate = 2000                # Rate limiting in bytes/sec, 0 = unlimited

```

### Adaptive Delay

The delay between fragment transmissions is no longer fixed. The `fragment_delay` value serves as the **starting point**, and the interface automatically adjusts the effective delay based on link quality:

- **On successful send**: the delay decreases by `delay_step_down` seconds (default 0.5s), down to `fragment_delay_min` (default 2s).
- **On failed send** (MeshCore returns an error): the delay increases by a factor of `delay_backoff_factor` (default 1.5x), up to `fragment_delay_max` (default 60s).
- **On reconnection**: the delay resets to the configured `fragment_delay` starting value.

This means the interface will automatically speed up on stable links and back off on noisy or congested ones.

| Parameter | Default | Description |
|---|---|---|
| `fragment_delay` | 20 | Starting delay between fragment sends (seconds) |
| `fragment_delay_min` | 2 | Floor for adaptive delay (seconds) |
| `fragment_delay_max` | 60 | Ceiling for adaptive delay (seconds) |
| `delay_step_down` | 0.5 | Seconds subtracted per successful send |
| `delay_backoff_factor` | 1.5 | Multiplier per failed send |
| `count_repeat` | 1 | Number of full rounds to send all fragments |
| `fragment_timeout` | 180 | Seconds before incomplete reassemblies are discarded |
| `bitrate` | 2000 | Rate limit in bytes/sec (0 = unlimited) |

### Interleaved Repetition

When `count_repeat` is greater than 1, fragments are sent in **full interleaved rounds** rather than per-fragment clusters. For example, with 3 fragments (A, B, C) and `count_repeat = 3`:

- **Before**: A, A, A, B, B, B, C, C, C
- **Now**: A, B, C, A, B, C, A, B, C

This spreads copies of each fragment across time, making transmission more resilient to burst interference. If a brief radio collision destroys two consecutive sends, it's more likely to lose one copy of two different fragments (recoverable) rather than all copies of the same fragment (not recoverable).

### Example Configurations

**Fast local testing** (TCP transport, low latency):
```ini
[[MeshCore]]
   type = MeshCoreInterface
   interface_enabled = true
   transport = tcp
   host = 127.0.0.1
   tcp_port = 4403
   fragment_delay = 2
   fragment_delay_min = 0.5
   fragment_delay_max = 10
```

**Unstable LoRa link** (high repetition, conservative delays):
```ini
[[MeshCore]]
   type = MeshCoreInterface
   interface_enabled = true
   transport = ble
   ble_name = MeshCore-MyNode
   count_repeat = 3
   fragment_delay = 30
   fragment_delay_min = 10
   fragment_delay_max = 60
```

**Fixed delay** (disable adaptive behavior, same speed always):
```ini
[[MeshCore]]
   type = MeshCoreInterface
   interface_enabled = true
   transport = serial
   port = /dev/ttyUSB0
   fragment_delay = 20
   fragment_delay_min = 20
   fragment_delay_max = 20
```

## Security

### Channel Secret

All RNS traffic is encapsulated in a MeshCore channel message encrypted with a shared secret. **Any node that knows the secret can read all tunnelled RNS traffic on that channel.**

The default secret shipped in the source code is publicly known. If you don't change it, your traffic is visible to anyone running this software with default settings. The interface will log a warning on startup if the default secret is detected.

To generate a unique secret:
```bash
python3 -c "import os; print(os.urandom(16).hex())"
```

Set the resulting hex string as `channel_secret` in `~/.reticulum/config` on **all** your RNS-over-MeshCore nodes. They must all use the same secret to communicate.

### Fragment ID Salt

Fragment IDs are salted with 4 bytes of per-session randomness, so the same payload sent in different sessions or from different nodes produces different fragment IDs. This reduces the risk of fragment reassembly corruption from ID collisions across concurrent senders.

---

## Recent Fixes

### Configurable fragment timeout
The `fragment_timeout` setting was previously documented but never actually read from config — the timeout was hardcoded to 180 seconds. It is now properly wired up, so users can tune how long incomplete fragment reassemblies are kept in memory before being discarded. This prevents unbounded memory growth on long-running nodes that receive partial transmissions.

### Bitrate limiter
The bitrate-based pacing between fragments was implemented but the actual delay (`time.sleep`) was commented out, meaning the `bitrate` config setting had no effect. It is now active, enforcing the configured bytes/sec rate limit between fragment transmissions alongside `fragment_delay`.

### Receive-side deduplication
When `count_repeat` is set above 1, every fragment is sent multiple times to improve delivery probability over unreliable MeshCore links. However, this could cause the same fully-reassembled packet to be delivered to RNS more than once. A deduplication layer now tracks recently received packet hashes and silently drops duplicates within the `fragment_timeout` window.

### Corrected documentation defaults
The example config values for `fragment_timeout` (was 3600, actual default 180) and `bitrate` (was 200, actual default 2000) have been corrected to match the code defaults.

### Non-blocking send queue
`process_outgoing` previously blocked the RNS thread with `time.sleep()` calls for fragment pacing — a 6-fragment packet at 20s delay would block for over 2 minutes. The send path now uses an async queue: `process_outgoing` returns immediately, and an async worker in the background handles fragmentation, pacing, and transmission without stalling RNS.

### Adaptive fragment delay
The inter-fragment delay is no longer fixed. It starts at the configured `fragment_delay` value and automatically decreases on successful sends (down to `fragment_delay_min`) or increases on errors (up to `fragment_delay_max`). This allows the interface to find the fastest reliable speed for the current link conditions. See the [Adaptive Delay](#adaptive-delay) section above for configuration details.

### Interleaved repetition
When `count_repeat > 1`, fragments are now sent in full rounds (A,B,C,A,B,C) instead of per-fragment clusters (A,A,A,B,B,B). This provides better temporal diversity against burst interference on the LoRa medium. See [Interleaved Repetition](#interleaved-repetition) above for details.

### Default secret warning
The interface now logs a prominent warning on startup if the default published channel secret is in use, along with a randomly generated alternative that can be copy-pasted into the config. See [Security](#security) above.

### Per-session fragment ID salt
Fragment IDs are now salted with 4 bytes of per-session randomness (`os.urandom(4)`), reducing the probability of fragment ID collisions when multiple nodes send the same data or across restarts.

---

## Acknowledgements

Special thanks to [HDDen](https://github.com/HDDen/) for their help with the MeshCore integration and debugging.

## License

MIT License — See LICENSE file for details.
