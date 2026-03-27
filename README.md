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
   #fragment_mtu = 100            # Max payload bytes per fragment (test higher values for speed)
   #fragment_delay = 3            # Starting delay between fragments (seconds), adapts automatically
   #fragment_delay_min = 1        # Minimum adaptive delay (seconds)
   #fragment_delay_max = 30       # Maximum adaptive delay (seconds)
   #delay_step_down = 0.5         # Seconds subtracted from delay on each successful send
   #delay_backoff_factor = 1.5    # Multiplier applied to delay on each failed send
   #fragment_timeout = 180        # Timeout for incomplete fragment reassembly (seconds)
   #bitrate = 2000                # Rate limiting in bytes/sec, 0 = unlimited
   #opportunistic_sending = false # Send next fragment as soon as previous completes
   #guard_delay = 0.3             # Minimum gap between sends in opportunistic mode (seconds)
   #flood_scope =                 # Limit propagation to repeaters allowing this scope (requires firmware >1.14)

```

### Adaptive Delay

The delay between fragment transmissions is no longer fixed. The `fragment_delay` value serves as the **starting point**, and the interface automatically adjusts the effective delay based on link quality:

- **On successful send**: the delay decreases by `delay_step_down` seconds (default 0.5s), down to `fragment_delay_min` (default 1s).
- **On failed send** (MeshCore returns an error): the delay increases by a factor of `delay_backoff_factor` (default 1.5x), up to `fragment_delay_max` (default 30s).
- **On reconnection**: the delay resets to the configured `fragment_delay` starting value.

This means the interface will automatically speed up on stable links and back off on noisy or congested ones.

| Parameter | Default | Description |
|---|---|---|
| `fragment_mtu` | 102 | Max payload bytes per fragment. Do not exceed 102 if you want repeater forwarding (base85 + 4-byte header = 106 bytes = 133 chars exactly) |
| `fragment_delay` | 3 | Starting delay between fragment sends (seconds) |
| `fragment_delay_min` | 1 | Floor for adaptive delay (seconds) |
| `fragment_delay_max` | 30 | Ceiling for adaptive delay (seconds) |
| `delay_step_down` | 0.5 | Seconds subtracted per successful send |
| `delay_backoff_factor` | 1.5 | Multiplier per failed send |
| `count_repeat` | 1 | Number of full rounds to send all fragments |
| `fragment_timeout` | 180 | Seconds before incomplete reassemblies are discarded |
| `bitrate` | 2000 | Rate limit in bytes/sec (0 = unlimited) |
| `opportunistic_sending` | false | When enabled, sends the next fragment as soon as the previous send completes instead of waiting the full adaptive delay. Falls back to adaptive backoff on errors |
| `guard_delay` | 0.3 | Minimum gap between fragment sends in opportunistic mode (seconds). Only used when `opportunistic_sending = true` |
| `flood_scope` | *(none)* | Limit message propagation to repeaters that allow this scope string. Requires MeshCore firmware >1.14. Repeaters must be configured with the same scope via `region` CLI commands |

### Opportunistic Sending

By default, the interface waits a fixed (adaptive) delay between each fragment send. With `opportunistic_sending = true`, the interface instead sends the next fragment **as soon as the previous `send` call returns**. Since MeshCore's send command blocks until the radio finishes transmitting, the radio's own transmit time acts as natural pacing — no artificial delay needed.

A small `guard_delay` (default 0.3s) prevents overwhelming the device if sends return instantly (e.g., over TCP transport). On errors, the interface falls back to adaptive backoff to avoid flooding a congested link.

This mode is best suited for **reliable links** where MeshCore consistently delivers fragments. On unreliable links, stick with the default adaptive delay mode.

### Fragment MTU

The `fragment_mtu` setting controls how many payload bytes each fragment carries.

**Encoding:** Fragments are encoded with base85 (RFC 1924), which has 25% overhead versus base64's 33%. MeshCore channel messages have a ~133-character text limit. base85(106 bytes) = exactly 133 chars. The 4-byte V2 fragment header uses 4 of those bytes, leaving **102 bytes max payload** that repeaters will forward. The default is set to 102.

Do not increase `fragment_mtu` above 102 if you want fragments to be relayed by repeaters. Higher values will work for direct node-to-node links but will be silently dropped by repeaters.

The V2 fragment header supports up to 16 fragments per packet. With `fragment_mtu = 102` and RNS's 500-byte MTU, a maximum packet requires 5 fragments — well within this limit. Only reduce `fragment_mtu` below ~32 if you need more than 16 fragments, which should never be necessary in practice.

### Flood Scope

MeshCore firmware >1.14 supports **region scopes** that limit which repeaters relay flood messages. By setting `flood_scope`, the interface tags all outgoing messages with a scope identifier. Only repeaters configured to allow that scope will forward the traffic.

This is useful to prevent RNS tunnel traffic from flooding the entire MeshCore mesh — only repeaters that explicitly opt in will relay it.

**Node configuration** (in `~/.reticulum/config`):
```ini
flood_scope = rnstunnel
```

**Repeater configuration** (via MeshCore CLI on each repeater):
```
region put rnstunnel
region allowf rnstunnel
```

All RNS nodes must use the same `flood_scope` value, and all repeaters in the path must allow that scope. If `flood_scope` is not set, messages use standard flood routing with no scope restriction.

### Interleaved Repetition

When `count_repeat` is greater than 1, fragments are sent in **full interleaved rounds** rather than per-fragment clusters. For example, with 3 fragments (A, B, C) and `count_repeat = 3`:

- **Before**: A, A, A, B, B, B, C, C, C
- **Now**: A, B, C, A, B, C, A, B, C

This spreads copies of each fragment across time, making transmission more resilient to burst interference. If a brief radio collision destroys two consecutive sends, it's more likely to lose one copy of two different fragments (recoverable) rather than all copies of the same fragment (not recoverable).

### Example Configurations

**Opportunistic mode** (reliable link, maximum speed):
```ini
[[MeshCore]]
   type = MeshCoreInterface
   interface_enabled = true
   transport = serial
   port = /dev/ttyUSB0
   opportunistic_sending = true
   guard_delay = 0.3
```

**Fast local testing** (TCP transport, low latency):
```ini
[[MeshCore]]
   type = MeshCoreInterface
   interface_enabled = true
   transport = tcp
   host = 127.0.0.1
   tcp_port = 4403
   opportunistic_sending = true
   guard_delay = 0.1
```

**Unstable LoRa link** (high repetition, conservative delays):
```ini
[[MeshCore]]
   type = MeshCoreInterface
   interface_enabled = true
   transport = ble
   ble_name = MeshCore-MyNode
   count_repeat = 3
   fragment_delay = 10
   fragment_delay_min = 3
   fragment_delay_max = 30
```

**Fixed delay** (disable adaptive behavior, same speed always):
```ini
[[MeshCore]]
   type = MeshCoreInterface
   interface_enabled = true
   transport = serial
   port = /dev/ttyUSB0
   fragment_delay = 3
   fragment_delay_min = 3
   fragment_delay_max = 3
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

## Recent Changes

### base85 encoding and compact V2 fragment header
Fragments are now encoded with **base85** (Python stdlib `base64.b85encode`) instead of base64. base85 has 25% overhead versus base64's 33%, and the V2 fragment header is 4 bytes (down from 5) using nibble-packed chunk index and total count.

Combined effect: **102 bytes of payload per fragment** instead of 94 — an 8.5% improvement. base85(106 bytes) = exactly 133 characters, which is the MeshCore repeater forwarding limit. The V2 header (`FLAG_FRAGMENTED_V2 = 0xFD`) supports up to 16 fragments per packet, which is sufficient for all normal RNS traffic with the default MTU.

**Note:** This is a protocol change. All nodes on the same channel must run the same version. V1 (base64) nodes cannot decode V2 (base85) messages and vice versa. The receiver still accepts V1 fragmented packets for diagnostic purposes.

### Corrected HW_MTU
The `HW_MTU` reported to RNS is now 500, matching the [RNS standard MTU](https://reticulum.network/manual/understanding.html). The previous value of 564 was incorrect.

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

Thank you [terminator513](https://github.com/terminator513/) for the initial work on this!

## License

MIT License — See LICENSE file for details.
