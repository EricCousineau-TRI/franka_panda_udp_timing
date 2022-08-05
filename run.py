#!/usr/bin/env python3

"""
See neighboring README.
"""

import argparse
from functools import partial
import os
from textwrap import dedent
import time

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml

import defs  # isort: skip

DEFAULT_CONFIG = "./localhost.yaml"
# DEFAULT_CONFIG = "./realtime.yaml"
# DEFAULT_CONFIG = "./soft.yaml"


class Config:
    # fmt: off
    # N.B. not a dataclass, just a simple data container.
    from_to = {
        # source -> dest: ip address of dest
        # - ssh stuff - these should be hostnames (use zeroconf / avahi if need be)
        # - can also make host localhost
        ("host", "control"): "<fill>",
        ("host", "robot_fake"): "<fill>",  # only valid for fake robot
        # routing via static IP addresses
        # for simplicity, these must be IP addresses (hard to programmaticaly
        # determine).
        ("control", "robot"): "<fill>",
    }
    # If user names are None, assumed to be same as host's current user.
    control_user = "<fill>"
    robot_fake_user = "<fill>"

    # Make sure this works w/ target real robot version.
    libfranka_version = "0.7.1"
    # patch derived from personal branch:
    # https://github.com/EricCousineau-TRI/libfranka/tree/feature-mock-server
    libfranka_timing_patch = "0b4ab5958517b86595acddd49299e62f59e477e1"

    require_realtime = True
    ping_time = 1.0
    capture_time = 10.0

    # scratch dir for host, control, and fake robot results and build artifacts
    scratch_dir = "/tmp/franka_timing"
    # see below
    nopasswd_file = "/etc/sudoers.d/ping-and-tshark-nopasswd"
    # fmt: on


def assert_realtime(run_remote):
    # Make sure it's realtime RT_PREEMPT.
    uname = run_remote("uname -a", capture=True)
    assert " PREEMPT_RT " in uname, uname


def build_libfranka(config, run_remote, franka_targets):
    # fetch code and build specific target.
    run_remote(
        dedent(
            fr"""
        rm -rf {config.scratch_dir}
        mkdir -p {config.scratch_dir}
        cd {config.scratch_dir}
        git clone -o upstream https://github.com/frankaemika/libfranka
        cd libfranka
        git checkout {config.libfranka_version}
        # n.b. github allows connected forks to fetch each others unique
        # commits.
        git fetch upstream {config.libfranka_timing_patch}
        git cherry-pick FETCH_HEAD
        git submodule update --init
        mkdir build && cd build
        cmake ..
        make -j$(nproc) {' '.join(franka_targets)}
        """
        ),
        # Just for ease of viewing output.
        use_tty=True,
    )


def setup_control(config, run_remote, *, is_robot_fake):
    # Install tshark (TUI for Wireshark), and enable `sudo {ping,tshark} *` to
    # avoid needing it in timed loop.
    run_remote(
        dedent(
            fr"""
        sudo apt install tshark

        sudo tee {config.nopasswd_file} <<EOF
        # Installed by franka timing experiment stuff.
        {config.control_user}  ALL = (ALL) NOPASSWD: /usr/bin/tshark *, /usr/bin/ping *, /usr/bin/killall -v tshark
        EOF
        sudo chmod 440 {config.nopasswd_file}
        """
        ),
        # For `sudo` prompt.
        use_tty=True,
    )
    # Check realtime.
    if config.require_realtime:
        assert_realtime(run_remote)
    else:
        # TODO(eric.cousineau): Use /etc/security/limit.d/{custom-name}?
        print(
            dedent(
                """
            WARNING: Make sure you have rtprio usable by users for a
            non-realtime system. For the sake of testing, you can add the
            following line using `sudoedit /etc/security/limits.conf`

                *                -       rtprio         90

            Consider reverting this once you are done experimenting.
            """
        ))
    # Download code and build.
    targets = ["read_robot_state"]
    if is_robot_fake:
        targets.append("run_mock_server")
    build_libfranka(config, run_remote, targets)


def setup_robot_fake(config, run_remote):
    if config.require_realtime:
        assert_realtime(run_remote)
    build_libfranka(config, run_remote, ["run_mock_server"])


def cleanup_control(config, run_remote):
    # Remove sudoer modification.
    run_remote(
        dedent(
            fr"""
        sudo rm -f {config.nopasswd_file}
        """
        ),
        use_tty=True,
    )


def make_tshark_udp_to_pcap_command(device, pcap_file):
    # Record to pcap first, then convert to CSV.
    # https://www.linuxquestions.org/questions/linux-networking-3/tshark-gives-permission-denied-writing-to-any-file-in-home-dir-650952/
    return dedent(
        fr"""
    touch {pcap_file}
    chmod o=rw {pcap_file}
    sudo tshark -i {device} -f 'udp' -w {pcap_file}
    """
    )


def make_tshark_pcap_to_csv_command(pcap_file, csv_file):
    # Similar to wireshark's CSV export, but with different field names.
    # https://osqa-ask.wireshark.org/questions/2935/creating-a-csv-file-with-tshark/
    # https://newspaint.wordpress.com/2021/01/18/selecting-fields-to-display-in-tshark/
    return dedent(
        fr"""
    tshark -r {pcap_file} \
        -T fields -E header=y -E separator=, -E quote=d \
        -e frame.number -e frame.time_relative \
        -e ip.src -e ip.dst \
        -e data.len \
        > {csv_file}
    """
    )


def copy_and_plot_timing(config, title, csv_file, plot_file):
    os.makedirs(config.scratch_dir, exist_ok=True)
    user_host = f"{config.control_user}@{config.from_to['host', 'control']}"
    defs.run(f"scp {user_host}:{csv_file} {csv_file}", shell=True)

    df = pd.read_csv(csv_file)
    # I think the max size datagram is the status message.
    max_size_only = df["data.len"] == np.max(df["data.len"])
    ts = np.asarray(df[max_size_only]["frame.time_relative"])
    ts = ts[ts <= config.capture_time]
    dts = np.diff(ts)
    plt.plot(ts[1:], dts)
    plt.ylabel("dt (sec)")
    plt.xlabel("time (sec)")
    plt.xlim(0, config.capture_time)
    plt.ylim(bottom=0)
    plt.title(title)
    plt.savefig(plot_file)
    print("saved; view with")
    print(f"  eog {plot_file}")


def timing(
    config,
    run_control,
    run_robot_fake,
    file_basename,
    are_control_and_fake_robot_same=False,
):
    run_mock_server = (
        f"{config.scratch_dir}/libfranka/build/test/run_mock_server"
    )
    read_robot_state = (
        f"{config.scratch_dir}/libfranka/build/examples/read_robot_state"
    )
    robot_address = config.from_to["control", "robot"]
    if are_control_and_fake_robot_same and run_robot_fake is not None:
        robot_address = "127.0.0.1"
    file_noext = f"{config.scratch_dir}/{file_basename}"
    pcap_file = f"{file_noext}.pcap"
    csv_file = f"{file_noext}.csv"
    plot_file = f"{file_noext}.png"
    ping_file = f"{file_noext}.ping.txt"

    # run ping test.
    if config.ping_time > 0.0:
        ping_msec = int(config.ping_time * 1000)
        ping_cmd = f"sudo ping {robot_address} -i 0.001 -D -c {ping_msec} -s 1200 | tail -n 3"
        ping_results = run_control(ping_cmd, capture=True)
        print(ping_results)
        with open(ping_file, "w") as f:
            f.write(ping_results + "\n")

    # Get device name.
    eth_device_control_to_robot = run_control(
        dedent(
            fr"""
        ip route get {robot_address} | grep -P -o '(?<=\bdev )\w+\b'
        """
        ),
        capture=True,
    )

    if config.require_realtime:
        robot_realtime_flag = "kEnforce"
    else:
        robot_realtime_flag = "kIgnore"

    # Manually increase process priority.
    process_prefix = "chrt -r 20 "

    process_map = {}
    poller = defs.ProcessPoller(process_map)
    with defs.close_processes_context(process_map):
        if run_robot_fake is not None:
            # be sure to advertise traffic on proper ethernet device (via IP address).
            process_map["run_mock_server"] = run_robot_fake(
                f"{process_prefix}{run_mock_server} {robot_address}",
                background=True,
            )

        # Run libfranka client-side binary.
        process_map["read_robot_state"] = run_control(
            f"{process_prefix}{read_robot_state} {robot_address} {robot_realtime_flag}",
            background=True,
        )

        # Capture UDP packets to compute timing.
        process_map["tshark"] = run_control(
            make_tshark_udp_to_pcap_command(
                eth_device_control_to_robot, pcap_file,
            ),
            background=True,
        )

        # Wait for tshark to spin up.
        while "Capturing" not in poller.get_output("tshark"):
            poller.poll()
            time.sleep(0.1)

        print(f"Capturing for {config.capture_time}sec...")
        t_end = time.time() + config.capture_time
        while time.time() < t_end:
            poller.poll()
            time.sleep(0.1)

    # N.B. This is *very* dumb. See TODO in `ssh_shell`.
    run_control("sudo killall -v tshark")
    run_control("killall -v read_robot_state || :")
    if run_robot_fake is not None:
        run_robot_fake("killall -v run_mock_server || :")

    run_control(make_tshark_pcap_to_csv_command(pcap_file, csv_file))
    copy_and_plot_timing(config, file_basename, csv_file, plot_file)


def remap_from_to(raw):
    # dunno how to make tuples keys in yaml / json.
    out = {}
    for raw in raw:
        k = (raw["from"], raw["to"])
        v = raw["address"]
        out[k] = v
    return out


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-c", "--config", type=str, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command")

    sub = subparsers.add_parser("setup")
    sub.add_argument("remotes", type=str, nargs="+")

    sub = subparsers.add_parser("timing")
    sub.add_argument("robot", type=str, choices=["robot_fake", "robot_real"])

    sub = subparsers.add_parser("cleanup")
    sub.add_argument("remotes", type=str, nargs="+")

    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    config = Config()
    with open(args.config, "r") as f:
        new_config = yaml.safe_load(f)
    new_config["from_to"] = remap_from_to(new_config["from_to"])
    for k, v in new_config.items():
        assert hasattr(config, k)
        setattr(config, k, v)
    if config.control_user is None:
        config.control_user = os.environ["USER"]
    if config.robot_fake_user is None:
        config.robot_fake_user = os.environ["USER"]

    os.makedirs(config.scratch_dir, exist_ok=True)

    are_control_and_fake_robot_same = (
        config.from_to["host", "control"]
        == config.from_to["host", "robot_fake"]
    )

    run_control = partial(
        defs.ssh_shell,
        user=config.control_user,
        host=config.from_to["host", "control"],
    )
    run_robot_fake = partial(
        defs.ssh_shell,
        user=config.robot_fake_user,
        host=config.from_to["host", "robot_fake"],
    )

    if args.command == "setup":
        assert len(args.remotes) > 0
        assert set(args.remotes) <= {"control", "robot_fake"}
        if are_control_and_fake_robot_same:
            setup_control(config, run_control, is_robot_fake=True)
        else:
            if "control" in args.remotes:
                setup_control(config, run_control, is_robot_fake=False)
            if "robot_fake" in args.remotes:
                setup_robot_fake(config, run_robot_fake)

    elif args.command == "timing":
        if args.robot == "robot_fake":
            timing(
                config,
                run_control,
                run_robot_fake,
                args.robot,
                are_control_and_fake_robot_same,
            )
        elif args.robot == "robot_real":
            timing(
                config, run_control, None, args.robot,
            )
        else:
            assert False

    elif args.command == "cleanup":
        assert len(args.remotes) > 0
        assert set(args.remotes) <= {"control", "robot_fake"}
        if "control" in args.remotes:
            cleanup_control(config, run_control)
        elif "robot_fake" in args.remotes:
            # No cleanup necessary.
            pass


assert __name__ == "__main__"
main()
