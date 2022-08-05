# Franka Emika Panda - Unexpected UDP Package Jitter

Script for objective, reproducible timing for
<https://github.com/frankaemika/libfranka/issues/120>

## Physical Equipment

There are 4 devices in these experiments:

- **host** <br/>
    OS: Ubuntu 20.04, whatever kernel

    For controlling experiments on **control** and **robot_fake** via SSH, if they are separate machines (to test physical networking connections).

    Also useful if **host** + **robot_fake** do not have any graphics running.

- **control** <br/>
    OS: Ubuntu 20.04, `RT_PREEMPT` (if `require_realtime` is true)

    For communicating w/ robot (real or fake)

- **robot** <br/>
    Two flavors:

    - **robot_real**: Real Franka Emika Panda robot.
    - **robot_fake**: Machine meant to emulate basic robot. <br/>
    OS: Ubuntu 20.04, `RT_PREEMPT` (if `require_realtime` is true)

For setting up `RT_PREEMPT`, see the excellent [libfranka docs](https://frankaemika.github.io/docs/installation_linux.html#setting-up-the-real-time-kernel).

These scripts are kinda codified in this simple GitHub gist:
<https://gist.github.com/EricCousineau-TRI/91a035538cd9b67a72b771600eaa64f9>

## Networking Connections

- (**host**, **control**) and (**host**, **robot_fake**) <br/>
    Low-speed (WiFi, whatever). Must be connected at all times during
    experiment.
- (**control**, **robot_fake**) and (**control**, **robot_real**) <br/>
    High-speed (Ethernet cable, make it *direct*, no hops / switches). Only
    **one is connected at a time** (see below).

    Both **robot_fake** and **robot_real** should be configured to use Manual
    IP. First, set up **robot_real** per
    [libfranka documentation](https://frankaemika.github.io/docs/getting_started.html#setting-up-the-network).

    Then on **robot_fake** (if separate machine), then add a custom Ethernet
    connection for IPv4 (disable IPv6), and use `Manual` on `172.16.0.1/24` or
    whatever you choose. This means routing as seen from **control** should have
    **no differences**.

Passwordless SSH (e.g. using `ssh-copy-id`) must work among these connections.

For localhost-only testing, this still uses `ssh localhost` (out of laziness).
Make sure you run `ssh localhost` from **host** once to ensure it is in your
trusted hosts.

## Software Requirements

Only tested on Ubuntu 20.04.

Expects `libfranka` prereqs installed already on each machine.

Host machine needs Python >=3.8, with `matplotlib`, `numpy`, `pandas`, and
`PyYAML` installed.

## Running Experiments

First, examine the `class Config` struct in `run.py`. Then examine the different
example YAML files. You can pass them as `./run.py -c <file> ...`, or just hack
`DEFAULT_CONFIG` in `run.py`.

Then, with your config selected, ensure we have experiment stuff setup on the
relevant machines:

```sh
./run.py setup control robot_fake
```

Once this is done, we can run timing.

Connect (**control**, **robot_fake**), ensure the network interface is
configured and talking on high-speed connection, then run:

```sh
./run.py timing robot_fake
```

It will save the relevant plot file.

After this, disonnect (**control**, **robot_fake**) and then connect
(**control**, **robot_real**) using *the same exact network port* (so that
there is no other source of timing variance). Then run:

```sh
./run.py timing robot_real
```

After you're done running experiments for a given setup, you can undo `sudoer`
change by running:
```sh
./run.py cleanup control robot_fake
```
