# Mesh internals: IV index and sequence numbers

Why the bridge keeps state on disk and what happens when the counters misbehave. Back to the [README](../README.md).

### Why the IV index matters

Every mesh message is encrypted with the network's current IV index. Send with the wrong one and every node discards the message **without any error** - the lights simply do not react, and nothing in any log says why.

So the bridge does not ask you for it. It learns the value from the Secure Network Beacons the mesh broadcasts, persists it, and refuses to move it backwards (a lower value in a beacon is either stale or a node that never finished an IV update; following it down would break the whole network for us). `IV_INDEX` exists only as a starting floor for the very first run.

A beacon sent during an IV update carries the *next* index rather than the one the network is transmitting with, and the flag that says so is one bit in the beacon.
Taking the number at face value would move the bridge up a year early, and because the value is never lowered it could not move back.
So while that flag is set the bridge keeps transmitting on the index it has, accepts messages on both it and the next one, and adopts the new index only once the network says the update is over.

Because that value is persisted and never lowered, a bogus beacon would be a permanent outage rather than a glitch: every message the bridge built afterwards would be encrypted under an IV index the real network has never heard of, and no amount of restarting would undo it. Beacons are therefore authenticated before they are believed - the network ID has to match and the 64-bit CMAC has to verify under the beacon key derived from the NetKey, so only something that already holds the network key can move the value. Even then, a jump of more than 42 IV updates ahead (over three years at the one-per-96-hours the specification allows) is logged and refused.

### Why the sequence number matters

The same failure mode, for the same reason. Nodes keep a replay-protection list and silently drop any message whose sequence number is not higher than the last one they saw from that source. A counter that goes backwards therefore does not produce an error, it produces lights that ignore the bridge for hours.

The bridge reserves `SEQ_BLOCK` numbers at a time and persists the high-water mark to `STATE_FILE` on the `./data` volume.
The mark is kept **per source address**, because that is the granularity the nodes use.

Saving is ordered so that there is no instant at which a reader would find nothing. The new contents are written to a temporary file and flushed to the platter, the current state file is copied aside to `.bak`, and only then is the temporary file renamed over the original and the directory entry flushed. A rename is atomic, so a reader at any point during a save sees either the whole old file or the whole new one, and a power cut at any point leaves at least one of the two complete. The leftover temporary files are cleaned up on the next start.

- If `/data` is not writable, the bridge **refuses to start** rather than run and lose the counter on the next restart.
- If the state file is gone but `/data` is not empty, it assumes the file was lost rather than never created and skips 100 000 sequence numbers (`SEQ_RECOVERY_JUMP`) to get clear of the old high-water mark.
- If the state file is unreadable - truncated, not JSON, or carrying a number that is not a number - it is treated as absent and the `.bak` is read instead. **The jump applies to that path too.** The backup is by definition behind the file it replaced, so resuming from it without the jump would hand the nodes sequence numbers they have already seen, and the deficit would grow with every restart rather than heal.
- A fresh, empty `/data` starts from a floor of minutes elapsed since 2026-01-01, which rises on its own while the bridge is not running.

**That floor is a cushion, not a guarantee.**
The bridge spends sequence numbers far faster than one a minute, so a cold start with a wiped `/data` lands *below* the mark the nodes remember.
There is no error when that happens: the lights simply ignore the bridge until the counter has climbed back past the old mark, which can take hours.
If you ever reset `/data`, either raise `SEQ_RECOVERY_JUMP` or give the bridge a different `SRC_ADDR`.

### The 24-bit horizon

A mesh sequence number is 24 bits, so one source address has 16 777 215 of them and there is no legal way to wrap around.
How long that lasts depends on how many entities you have and how often they are polled.
Reading one light costs two numbers, not one: a status request goes out as many times as a command does. Nothing on a mesh network is acknowledged, so a single copy lost to a collision is not retried by anything - that light is simply not read again until the next interval, and no log anywhere says a request went missing.
A mesh of 20 lights at the default 30 second `POLL_INTERVAL` therefore burns roughly 80 numbers a minute, about 115 000 a day, which runs the space out in roughly 145 days.
Raising `POLL_INTERVAL` is the one setting that moves this number directly.

Commands cost numbers too, and so do the follow-up polls that confirm them. Those polls are coalesced: the bridge waits two seconds after the *last* command and then reads back everything the burst touched, in one pass. Dragging a brightness slider in Home Assistant sends a command per position, so a twenty-step drag used to be twenty commands and twenty separate read-backs of the same lamp; it is now twenty commands and one. On a group the saving multiplies by the number of members, because a group read-back polls each of them. The intermediate answers were never interesting anyway - only the state after the drag stops is.

With a million numbers left the bridge moves itself to another free unicast address, during an ordinary send and while the old counter still works.
The nodes keep replay records per source address and have no record for the new one, so it starts clean; the old address keeps its mark in the state file, so nothing can go back to it by accident.
The move is logged at `WARNING` and is otherwise invisible: Home Assistant entity ids are built from the *device* address, not from the bridge's, and the proxy filter is re-sent as part of the move so status messages keep reaching us.

A rotation costs each node one entry in its replay list, and that list is a few dozen entries wide at best.
At the rate above this happens about twice a year, which is nothing. **Do not lower the threshold to watch it work** - rotating through a few hundred addresses would fill every node's replay list and is not something a factory reset of the bridge can undo.

Set `SEQ_AUTO_ROTATE=false` to turn it off, in which case the bridge warns instead and stops when the space is gone, and the recovery is to set `SRC_ADDR` to another free unicast address and restart.
`SRC_ADDR` says where the bridge *starts*, not where it must stay: if it names an address whose counter has already been spent, the bridge stays on the one it rotated to and says so in the log, rather than coming up on a dead counter after every restart.

The Bluetooth Mesh specification's own answer is the IV Update procedure, which resets every counter in the network, and this bridge deliberately does **not** implement it.
It is a network-wide operation that takes 96 to 144 hours, is permitted at most once per 192 hours, and belongs to the network's provisioner.
This bridge is a guest in a network that the Häfele Connect app provisioned, so initiating one would be a change made on everybody else's behalf.
The bridge does follow an IV update started by someone else, via the Secure Network Beacons it already listens to.
