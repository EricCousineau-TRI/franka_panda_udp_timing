from collections import defaultdict
from contextlib import contextmanager
import os
import select
import shlex
import signal
import subprocess
import sys
from textwrap import indent

# TODO: Make ctrl+c on inner process raise ctrl+c here?


def eprint(s):
    print(s, file=sys.stderr)


def run(
    args,
    *,
    shell=False,
    use_tty=None,  # to mimic ssh_shell below :/
    check=None,
    capture=False,
    background=False,
    stdout=None,
    stderr=None,
    do_print=True,
    **kwargs,
):
    if shell:
        cmd = args
    else:
        cmd = shlex.join(args)
    if do_print:
        eprint(f"+ {cmd}")
    if background:
        assert not capture
        assert not check, "Cannot have check=True with background=True"
        return subprocess.Popen(
            args, shell=shell, stdout=stdout, stderr=stderr, **kwargs
        )
    else:
        text = None
        if check is None:
            check = True
        if capture:
            assert stdout is None
            stdout = subprocess.PIPE
            if stderr is None:
                stderr = subprocess.STDOUT
            text = True
        out = subprocess.run(
            args,
            shell=shell,
            check=check,
            stdout=stdout,
            stderr=stderr,
            text=text,
            **kwargs,
        )
        if capture:
            if check:
                if out.returncode != 0:
                    print(out.stdout, out.stderr)
            return out.stdout
        else:
            return out


def ssh_shell(
    command,
    *,
    user,
    host,
    check=None,
    use_tty=False,
    background=False,
    capture=False,
):
    # TODO: er, these options got messy.
    if check is None:
        check = not background
    user_host = f"{user}@{host}"
    command = bash_command(command, interactive=use_tty, use_login=False)
    ssh_opts = ["-A", "-o", "BatchMode=yes"]
    if use_tty:
        assert not capture, "Must run with use_tty=False"
        assert not background, "Must run with use_tty=False"
        args = ["ssh", "-tt"] + ssh_opts + [user_host, command]
        # TODO: How to prevent TTY from getting corrupted by programs like
        # tshark (which can clear a line and mess things up)? For now,
        # workaround is to use `reset_tty()`.
        return run(args, check=check)
    elif background:
        # WARNING: We use -t for TTY allocation so that when SSH gets a signal,
        # it gets passed to children. However, we have to reset tty
        # (see below).
        # https://unix.stackexchange.com/questions/40023/get-ssh-to-forward-signals
        args = ["ssh", "-t"] + ssh_opts + [user_host, command]
        return run(
            args,
            background=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    else:
        args = ["ssh"] + ssh_opts + [user_host, command]
        stdout = run(args, capture=True, check=check).strip()
        if capture:
            return stdout
        else:
            print(indent(stdout, f"[{host}] "))


def bash_command(command, *, interactive, use_login):
    # Ensures command is run as bash login shell (so ~/.bashrc is used).
    bash_opts = []
    if use_login:
        bash_opts = ["--login"]
    if interactive:
        bash_opts += ["-i"]
    return shlex.join(["bash"] + bash_opts + ["-c", command])


def signal_processes(
    process_list, sig=signal.SIGINT, block=True, close_streams=True
):
    """
    Robustly sends a singal to processes that are still alive. Ignores status
    codes.

    @param process_list List[Popen] Processes to ensure are sent a signal.
    @param sig Signal to send. Default is `SIGINT`.
    @param block Block until process exits.
    """
    for process in process_list:
        if process.poll() is None:
            process.send_signal(sig)
        if close_streams:
            for stream in [process.stdin, process.stdout, process.stderr]:
                if stream is not None and not stream.closed:
                    stream.close()
    if block:
        for process in process_list:
            if process.poll() is None:
                process.wait()


@contextmanager
def close_processes_context(process_map):
    """Close processes after context exit"""
    try:
        yield
    finally:
        # https://github.com/amoffat/sh/issues/495#issuecomment-801069064
        should_re_raise = False
        while True:
            try:
                signal_processes(process_map.values())
                break
            except KeyboardInterrupt:
                print("(Trying to kill processes...")
                should_re_raise = True
        if should_re_raise:
            raise KeyboardInterrupt


def reset_tty():
    # https://superuser.com/a/1225270/733549
    run(["stty", "sane"], check=True, do_print=False)


@contextmanager
def reset_tty_context():
    try:
        yield
    finally:
        reset_tty()


def read_available(f):
    """
    Reads all available data on a given file. Useful for using PIPE with Popen.
    """
    min_chunk_size = 1024
    timeout = 0.0
    readable, _, _ = select.select([f], [], [f], timeout)
    out = bytes()
    if f not in readable:
        return out
    while True:
        new = os.read(f.fileno(), min_chunk_size)
        out += new
        if len(new) < min_chunk_size:
            break
    return out


class ProcessPoller:
    """Polls process and reads process stdout"""

    def __init__(self, process_map):
        # NOTE: This aliases process_map.
        assert isinstance(process_map, dict)
        self._process_map = process_map
        self._output_map = defaultdict(str)

    def poll(self, *, require_success_or_alive=True, print_stdout=True):
        returncodes = {}
        for name, proc in self._process_map.items():
            # Poll output.
            text = read_available(proc.stdout).decode("utf8")
            if text.endswith("\n"):
                text = text[:-1]
            if len(text) > 0:
                self._output_map[name] += text + "\n"
                if print_stdout:
                    print(indent(text, f"[{name}] "), flush=True)
            returncode = proc.poll()
            if require_success_or_alive:
                if returncode not in [None, 0]:
                    raise RuntimeError(
                        f"Process '{name}' died with {returncode}:\n"
                        + indent(self._output_map[name], "  ")
                    )
            returncodes[name] = returncode
        return returncodes

    def get_output(self, name):
        return self._output_map[name]
