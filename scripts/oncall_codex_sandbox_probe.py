#!/usr/bin/env python3
"""Prove, on the server, that the Codex runner's sandbox hides what it must.

Spec: docs/plans/2026-09-20-codex-oncall-phase2-spec.md section 8, step 1.

The sandbox properties are **parsed out of the unit file** rather than typed
in a second time. A hand-copied second list is the classic way for a probe to
pass while the thing it is meant to check has drifted, so there is exactly one
place where the namespace is described:
``deploy/systemd/telegram-kol-oncall-codex.service``.

The probe runs a small shell script under ``systemd-run`` with those exact
properties and prints a PASS/FAIL line per assertion:

  * the worker's environment file, the settings and data directories, /opt/frp,
    /home and /data are unreadable;
  * a machine-wide search for ``*.env``, ``*.session`` and ``auth.json`` turns
    up nothing except ``/root/.codex/auth.json``;
  * the deployed source is readable;
  * the spool and ``/root/.codex`` are writable, and nothing else is.

Usage (as root, on the server):
    python3 -B scripts/oncall_codex_sandbox_probe.py [--unit <path>] [--dry-run]

``--dry-run`` prints the ``systemd-run`` command it would execute and exits 0,
which is also what the unit tests exercise; nothing else here runs locally.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

#: Only these directives describe the namespace. Everything else in the unit
#: (Restart=, ExecStart=, Description=) is about running the service, not
#: about what it can see.
SANDBOX_DIRECTIVES = (
    "AmbientCapabilities",
    "CapabilityBoundingSet",
    "NoNewPrivileges",
    "ProtectSystem",
    "ProtectHome",
    "PrivateTmp",
    "PrivateDevices",
    "ProtectControlGroups",
    "ProtectKernelLogs",
    "ProtectKernelModules",
    "ProtectKernelTunables",
    "ProtectProc",
    "ProcSubset",
    "UMask",
    "TemporaryFileSystem",
    "BindReadOnlyPaths",
    "BindPaths",
)

SPOOL = "/var/lib/telegram-kol-oncall/codex-spool"

PROBE_SCRIPT = r"""
set -u
fail=0
say() { if [ "$1" = 0 ]; then echo "PASS  $2"; else echo "FAIL  $2"; fail=1; fi }

# A TemporaryFileSystem= mount leaves an empty, readable directory behind; what
# matters is that nothing from the real one shows through.
hidden() {
    if [ -f "$1" ] && cat "$1" >/dev/null 2>&1; then
        say 1 "$2 应当看不见，却读到了：$1"
    elif [ -d "$1" ] && [ -n "$(ls -A "$1" 2>/dev/null)" ]; then
        say 1 "$2 应当是空的，却看到了内容：$1"
    else
        say 0 "$2 看不见：$1"
    fi
}

hidden /etc/telegram-kol-worker.env "worker 环境文件"
hidden /etc/telegram-kol-oncall.env "值守环境文件"
hidden /opt/telegram-kol-analyzer/config "应用配置目录"
hidden /opt/telegram-kol-analyzer/data "应用数据目录"
hidden /opt/telegram-kol-analyzer/.git "git 目录"
hidden /opt/frp "frp 目录"
hidden /opt/telegram-kol-releases "历史 release 目录"
hidden /home "home 目录"
hidden /data "data 目录"
hidden /www "www 目录"
hidden /root/.ssh "root 的 ssh 目录"
hidden /etc/pki/tls/private "TLS 私钥目录"
hidden /etc/ssl/private "TLS 私钥目录"

# The overlays are an explicit list, so the list can fall behind the machine.
# Anything at the top level that is not a system tree must show up empty.
for entry in $(ls -A /); do
    case "$entry" in
        bin|boot|dev|etc|lib|lib64|opt|proc|root|run|sbin|sys|tmp|usr|var) continue ;;
    esac
    if [ -d "/$entry" ] && [ -n "$(ls -A "/$entry" 2>/dev/null)" ]; then
        say 1 "根目录下有未遮盖的目录：/$entry"
    elif [ -f "/$entry" ] && [ -s "/$entry" ] && [ -r "/$entry" ]; then
        say 1 "根目录下有未遮盖的文件：/$entry"
    fi
done
say 0 "根目录逐项检查完成"

for sock in /run/docker.sock /run/containerd/containerd.sock /run/systemd/private /run/dbus/system_bus_socket; do
    if [ -e "$sock" ]; then
        say 1 "root 可连接的 socket 仍然可见：$sock"
    else
        say 0 "socket 看不见：$sock"
    fi
done

# Names only, never values: does any other process leak its environment to us?
leaks=0
for environ in /proc/[0-9]*/environ; do
    pid=${environ#/proc/}; pid=${pid%/environ}
    [ "$pid" = "$$" ] && continue
    if tr '\0' '\n' < "$environ" 2>/dev/null | grep -qE '^(DEEPCOIN_|TELEGRAM_API_|TELEGRAM_KOL_[A-Z_]*(TOKEN|KEY|SECRET))'; then
        leaks=$((leaks + 1))
    fi
done
if [ "$leaks" = 0 ]; then
    say 0 "读不到其它进程环境里的密钥"
else
    say 1 "能从 /proc 读到 $leaks 个进程环境里的密钥键名"
fi

if [ -r /opt/telegram-kol-analyzer/src/telegram_kol_research/__init__.py ]; then
    say 0 "部署源码可读"
else
    say 1 "部署源码不可读——runner 无法解释原因码"
fi

# No -xdev: the bind mounts and tmpfs overlays are exactly what needs searching.
# System trees that hold toolchain files such as golang's go.env are pruned.
found=$(find / \( -path /proc -o -path /sys -o -path /dev -o -path /usr -o -path /lib \
        -o -path /lib64 -o -path /bin -o -path /sbin -o -path /boot \) -prune -o \
        \( -name "*.env" -o -name "*.session" -o -name "auth.json" -o -name "id_rsa*" \
        -o -name "id_ed25519*" -o -name "*.db" \) -readable -print 2>/dev/null \
        | grep -v -e '^/root/.codex/' -e '^/opt/telegram-kol-analyzer/.venv/' || true)
if [ -z "$found" ]; then
    say 0 "全盘没有其它可读的密钥形状文件"
else
    say 1 "出现了不该可读的文件：$(echo "$found" | tr '\n' ' ')"
fi

if touch SPOOL_PLACEHOLDER/.probe 2>/dev/null; then
    rm -f SPOOL_PLACEHOLDER/.probe
    say 0 "spool 可写"
else
    say 1 "spool 不可写——runner 无法回写裁决"
fi

if touch /root/.codex/.probe 2>/dev/null; then
    rm -f /root/.codex/.probe
    say 0 "/root/.codex 可写（令牌刷新需要）"
else
    say 1 "/root/.codex 不可写——令牌无法刷新"
fi

for target in /etc/probe /opt/probe /var/probe /srv/probe /tmp/../etc/probe; do
    if touch "$target" 2>/dev/null; then
        rm -f "$target"
        say 1 "不该可写的位置竟然写成功了：$target"
    else
        say 0 "不可写：$target"
    fi
done

exit $fail
"""


def parse_sandbox_properties(unit_text: str) -> list[str]:
    """Every sandbox directive in the unit, in order, as ``Key=value``.

    Comments and the service-management directives are dropped; ``-`` prefixes
    on optional bind paths are kept, because ``systemd-run`` understands them
    and a probe that silently required an optional path would fail for the
    wrong reason.
    """

    properties: list[str] = []
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key.strip() not in SANDBOX_DIRECTIVES:
            continue
        properties.append(f"{key.strip()}={value.strip()}")
    return properties


def build_systemd_run_command(
    properties: list[str], *, script: str, unit_name: str = "oncall-codex-probe"
) -> list[str]:
    command = [
        "systemd-run",
        "--pipe",
        "--wait",
        "--collect",
        "--quiet",
        f"--unit={unit_name}",
        "--property=User=root",
    ]
    for prop in properties:
        command.append(f"--property={prop}")
    command += ["/bin/sh", "-c", script]
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--unit",
        default=str(
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "systemd"
            / "telegram-kol-oncall-codex.service"
        ),
        help="the unit file whose sandbox properties are reused",
    )
    parser.add_argument("--spool", default=SPOOL)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the systemd-run command instead of executing it",
    )
    args = parser.parse_args(argv)

    unit_text = Path(args.unit).read_text(encoding="utf-8")
    properties = parse_sandbox_properties(unit_text)
    if not properties:
        print("FAIL  单元文件里没有解析出任何沙箱属性")
        return 2
    script = PROBE_SCRIPT.replace("SPOOL_PLACEHOLDER", str(args.spool))
    command = build_systemd_run_command(properties, script=script)

    if args.dry_run:
        print("将执行：")
        print(" \\\n  ".join(command[:-1]))
        print(f"  <{len(script)} 字节的探针脚本>")
        return 0
    if shutil.which("systemd-run") is None:
        print("FAIL  找不到 systemd-run（这个脚本只能在服务器上跑）")
        return 2
    done = subprocess.run(command, text=True)
    return int(done.returncode or 0)


if __name__ == "__main__":
    sys.exit(main())
