# Bluetooth: radio selection and link recovery

Back to the [README](../README.md).

### Which radio the bridge uses

Two Bluetooth adapters in the same machine, a metre apart, do not hear the same things. One built into a board, next to the CPU and under a metal can, can be deaf to a mesh node that a USB dongle on a short cable sees comfortably. Nothing in the log would say so: the bridge would scan, find nothing, retry, and look exactly like a network that is switched off.

So `BLE_ADAPTER` is optional, and when it is empty, or it names an adapter that is not present, the bridge decides for itself:

1. It asks BlueZ what adapters the host has.
2. It scans with each of them in turn and watches for the Mesh Proxy advertisement carrying **our** network's identifier, which is the only evidence that a radio can hear this particular network rather than Bluetooth in general.
3. The adapter that hears it, or the one that hears it loudest when several do, is the one it keeps. The choice is logged, with the signal strength each adapter reported.

A host with a single adapter skips steps 2 and 3 and simply uses it. There is nothing to compare it against, and the scan would only spend time before the connection attempt that matters, so a one-radio machine gets no "which adapter hears the mesh" lines in its log.

If none of them hears it, that is reported as its own state rather than as another connection failure, because the two call for completely different things: one means move the aerial, the other means look at the proxy node. A survey that heard nothing has learned nothing, so it does not get to overrule `BLE_ADAPTER`: it keeps the configured radio if there is one, and the bridge is still on it when the mesh comes back.

If BlueZ itself cannot be asked at all, that is survivable but limiting. The bridge logs it once and carries on, because bleak reaches BlueZ by its own route and can still connect, but only the `hciN` form of `BLE_ADAPTER` means anything to it. A `BLE_ADAPTER` holding a MAC address cannot be honoured at all while the adapter list is unavailable, since matching a MAC is exactly what that list is for, and the log says so rather than claiming a radio it is not using.

When you do want to pin a radio, give it the adapter's MAC address rather than `hciN`. The `hciN` names are handed out in the order the kernel finds the radios during boot, so a reboot, a re-plugged dongle or a firmware load that takes a moment longer can move `hci1` onto a different piece of hardware. The configuration still looks right, and the bridge goes quiet. A MAC address cannot do that. Either form is accepted; anything that is neither is refused at start-up rather than at the first scan.

### When the link will not come up

Retrying the same thing is not a strategy, so after three consecutive failures the bridge starts trying different things, cheapest first, each one logged with the reason it is being attempted:

| After | It | Because |
| --- | --- | --- |
| 3 failures | scans three times longer | a proxy that advertises rarely, or at the edge of range, can be missed by a short scan |
| 6 failures | powers the adapter off and on through BlueZ | controllers do wedge, and this is the cheapest thing that unwedges them |
| 9 failures | re-scans every adapter and switches to whichever hears the mesh | the chosen radio may simply not be able to hear this network any more |

It stops there rather than cycling: the last rung already re-checks everything, so repeating the cheaper ones would only add log lines.

The ladder is reset by a link that *held*, not by one that merely connected. A connection that comes up and drops seconds later is what a flapping radio does on every attempt, and treating each of those as a success would keep the count near zero and mean the ladder was never climbed at all. So the counter is cleared only once a link has stayed up for a minute, at the point it eventually goes down, which is the first moment the connection can honestly be called a good one. The one thing a bare success does clear is the longer scan, because finding the proxy is itself the proof that the ordinary scan was long enough.

The second rung needs an adapter it can name, since that is how BlueZ powers one off and on. On the rare path where the adapter list could not be read at all there is no name to use, and the rung says so rather than passing in silence, because a rung that returns quietly reads in the log exactly like one that ran and did not help.
