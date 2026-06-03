from __future__ import annotations

import json as _json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import click

# Force UTF-8 on Windows console so Chinese help text doesn't render as mojibake
# (default code page is GBK / cp936 on zh-CN systems).
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

from . import manager
from .config import AppSpec, ConfigError, find_app, load_config

DEFAULT_CONFIG = Path.home() / ".localctl" / "apps.yaml"


def _load(ctx: click.Context) -> list[AppSpec]:
    explicit: Path | None = ctx.obj.get("config") if ctx.obj else None
    path = explicit or DEFAULT_CONFIG
    if not path.exists():
        raise click.ClickException(
            f"config not found: {path}\ncreate it, or pass -c <path>"
        )
    try:
        return load_config(path)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e


def _resolve(apps: list[AppSpec], names: tuple[str, ...]) -> list[AppSpec]:
    """Resolve each token to an AppSpec. Accepts either an app name or a 1-based
    index from `localctl list`. Special token `all` (or no args) → every app."""
    if not names or names == ("all",):
        return apps
    out: list[AppSpec] = []
    for tok in names:
        if tok.isdigit():
            idx = int(tok)
            if not (1 <= idx <= len(apps)):
                raise click.ClickException(
                    f"index out of range: {idx} (valid: 1..{len(apps)})"
                )
            out.append(apps[idx - 1])
            continue
        try:
            out.append(find_app(apps, tok))
        except KeyError as e:
            raise click.ClickException(e.args[0] if e.args else str(e)) from e
    return out


def _fmt_uptime(s: float | None) -> str:
    if s is None:
        return "-"
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{sec}s"
    return f"{sec}s"


CLI_HELP = """\
localctl —— 无后台守护的本地应用管理 CLI（Node / Python / 任何可执行程序）

\b
配置文件: ~/.localctl/apps.yaml  (用 -c 可指向其他路径)
状态目录: ~/.localctl/<app>/    (pid.json + stdout.log + stderr.log)

\b
应用参数可以是 NAME 或 `localctl list` 里显示的编号:
  localctl start 1            等同 localctl start envision-web (假设 #1 是它)
  localctl stop 1 3 dash-web  混搭也行
  localctl start all          启动全部

\b
常用:
  localctl list                列出所有已配置应用 (带编号)
  localctl start <id>...       启动 (加 --wait 等待就绪)
  localctl stop  <id>...       停止
  localctl restart <id>...     重启
  localctl status [<id>...]    查看状态 (--watch 实时刷新, --json 给脚本)
  localctl logs <id> [-f]      查看输出 (--clear 清空日志)
  localctl reclaim <id|:port>  强制释放端口 (:8000 表示显式端口)
  localctl edit                用 $EDITOR 打开 apps.yaml
  localctl help [command]      显示帮助

任意命令后加 --help 看详细参数。
"""


@click.group(help=CLI_HELP)
@click.option(
    "--config", "-c", type=click.Path(path_type=Path), default=None,
    help="apps.yaml 路径 (默认: ~/.localctl/apps.yaml)",
)
@click.pass_context
def cli(ctx: click.Context, config: Path | None) -> None:
    ctx.ensure_object(dict)
    ctx.obj["config"] = config


@cli.command()
@click.argument("command", required=False)
@click.pass_context
def help(ctx: click.Context, command: str | None) -> None:
    """显示帮助。`localctl help` 看总览, `localctl help start` 看具体命令。"""
    if command is None:
        click.echo(ctx.parent.get_help())
        return
    cmd = cli.get_command(ctx, command)
    if cmd is None:
        raise click.ClickException(f"no such command: {command}")
    sub_ctx = click.Context(cmd, info_name=command, parent=ctx.parent)
    click.echo(cmd.get_help(sub_ctx))


def _report_start(name: str, res: "manager.StartResult") -> None:
    if res.error:
        click.echo(f"{name}: ERROR {res.error}", err=True)
        return
    st = res.status
    plan = res.plan
    tag = "running" if st.running else "failed"
    msg = f"{name}: {tag} (pid={st.pid})"
    if plan and plan.reassigned:
        owner = (
            f" [held by pid={plan.conflict[0]} ({plan.conflict[1]})]"
            if plan.conflict
            else ""
        )
        msg += f"  port: {plan.preferred} -> {plan.chosen}{owner}"
    elif plan:
        msg += f"  port: {plan.chosen}"
    click.echo(msg)


@cli.command()
@click.argument("names", nargs=-1)
@click.option(
    "--wait", "wait_ready", is_flag=True,
    help="等到端口/HTTP 真的就绪再返回 (而不是 spawn 完就返回)",
)
@click.option(
    "-t", "--timeout", default=30.0, show_default=True,
    help="--wait 的超时秒数",
)
@click.pass_context
def start(ctx: click.Context, names: tuple[str, ...], wait_ready: bool, timeout: float) -> None:
    """启动一个或多个应用。

    NAMES 留空或写 `all` = 启动全部。子进程脱离父进程后台运行。
    若配置了 health.auto，端口被占时会自动选用范围内的空闲端口。

    \b
    例:
      localctl start envision-web
      localctl start all
      localctl start dash-web --wait -t 20    # 等到 Vite 真的能响应
    """
    apps = _resolve(_load(ctx), names)
    exit_code = 0
    for app in apps:
        try:
            res = manager.start(app)
        except Exception as e:
            click.echo(f"{app.name}: ERROR {e}", err=True)
            exit_code = 1
            continue
        _report_start(app.name, res)
        if res.error:
            exit_code = 1
            continue
        if wait_ready and res.status.running:
            ready = manager.wait_until_ready(app, timeout=timeout)
            if ready:
                click.echo(f"  {app.name}: ready")
            else:
                click.echo(f"  {app.name}: NOT ready after {timeout:.0f}s", err=True)
                exit_code = 1
    sys.exit(exit_code)


@cli.command()
@click.argument("names", nargs=-1)
@click.pass_context
def stop(ctx: click.Context, names: tuple[str, ...]) -> None:
    """停止一个或多个应用 (或 `all`)。

    会终止整条进程链, 先 SIGTERM (10s 优雅期), 之后 SIGKILL 残留。
    """
    apps = _resolve(_load(ctx), names)
    for app in apps:
        ok = manager.stop(app)
        click.echo(f"{app.name}: {'stopped' if ok else 'not running'}")


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--wait", "wait_ready", is_flag=True, help="等到端口/HTTP 就绪")
@click.option("-t", "--timeout", default=30.0, show_default=True)
@click.pass_context
def restart(ctx: click.Context, names: tuple[str, ...], wait_ready: bool, timeout: float) -> None:
    """重启一个或多个应用 = stop + start。"""
    apps = _resolve(_load(ctx), names)
    for app in apps:
        manager.stop(app)
        try:
            res = manager.start(app)
        except Exception as e:
            click.echo(f"{app.name}: ERROR {e}", err=True)
            continue
        _report_start(app.name, res)
        if wait_ready and res.status.running and not res.error:
            ready = manager.wait_until_ready(app, timeout=timeout)
            click.echo(f"  {app.name}: {'ready' if ready else f'NOT ready after {timeout:.0f}s'}")


def _render_status_table(apps: list[AppSpec]) -> None:
    header = f"{'NAME':<20} {'STATE':<8} {'PID':<8} {'UPTIME':<10} {'PORT':<20} NOTE"
    click.echo(header)
    click.echo("-" * len(header))
    for app in apps:
        st = manager.status(app)
        state = "running" if st.running else "stopped"
        pid = str(st.pid) if st.pid else "-"
        uptime = _fmt_uptime(st.uptime_s)
        if st.port is None:
            port = "-"
        elif st.http_ok is not None:
            port = f"{st.port} {'HTTP' if st.http_ok else 'HTTPx'}"
        elif st.port_listening is None:
            port = str(st.port)
        else:
            port = f"{st.port} {'OK' if st.port_listening else 'DOWN'}"
        note = ""
        if st.running and st.preferred_port and st.port != st.preferred_port:
            note = f"reassigned from {st.preferred_port}"
        elif not st.running and st.port_conflict:
            cp, cn = st.port_conflict
            note = f"port held by pid={cp} ({cn})"
        click.echo(f"{app.name:<20} {state:<8} {pid:<8} {uptime:<10} {port:<20} {note}")


def _status_json(apps: list[AppSpec]) -> str:
    out = []
    for app in apps:
        st = manager.status(app)
        d = asdict(st)
        d["ready"] = manager.is_ready(app, st)
        out.append(d)
    return _json.dumps(out, indent=2, ensure_ascii=False)


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--watch", "-w", is_flag=True, help="实时刷新 (Ctrl+C 退出)")
@click.option("-i", "--interval", default=2.0, show_default=True, help="--watch 的刷新间隔(秒)")
@click.option("--json", "json_out", is_flag=True, help="以 JSON 输出, 便于脚本处理")
@click.pass_context
def status(
    ctx: click.Context, names: tuple[str, ...],
    watch: bool, interval: float, json_out: bool,
) -> None:
    """查看状态: 运行中? PID? 实际端口? 是否就绪?

    \b
    PORT 列含义:
      `<port> OK`   TCP 端口可连接 (默认探活)
      `<port> DOWN` 进程在跑但端口连不上
      `<port> HTTP` 配置了 health.http 且 GET 返回 2xx/3xx
      `<port> HTTPx` HTTP 探活失败
    NOTE 列:
      `reassigned from N`     auto-port 换号了
      `port held by pid=N`    端口被外部进程占用 (start 会失败)
    """
    apps = _resolve(_load(ctx), names)
    if json_out and watch:
        raise click.ClickException("--json 与 --watch 不能同时使用")
    if json_out:
        click.echo(_status_json(apps))
        return
    if not watch:
        _render_status_table(apps)
        return
    try:
        while True:
            click.clear()
            click.echo(f"localctl status  (refresh {interval:.0f}s, Ctrl+C to exit)")
            _render_status_table(apps)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


@cli.command()
@click.argument("target")
@click.option("--yes", "-y", is_flag=True, help="跳过确认提示")
@click.pass_context
def reclaim(ctx: click.Context, target: str, yes: bool) -> None:
    """强制释放端口 (杀掉占用者)。

    \b
    TARGET 三种形式:
      <id>      list 中的编号, 释放对应应用的 health.port
      <name>    应用名, 同上
      :<port>   显式端口号 (冒号前缀, 例如 :8000)
    \b
    例:
      localctl reclaim 5             # 应用 #5 的端口
      localctl reclaim envision-web  # 同样按名取端口
      localctl reclaim :8000         # 直接释放端口 8000
    """
    apps = _load(ctx)
    if target.startswith(":"):
        rest = target[1:]
        if not rest.isdigit():
            raise click.ClickException(f"invalid port: {target!r} (expected ':<digits>')")
        port = int(rest)
        if not (1 <= port <= 65535):
            raise click.ClickException(f"port out of range: {port}")
    elif target.isdigit():
        n = int(target)
        if not (1 <= n <= len(apps)):
            raise click.ClickException(
                f"index out of range: {n} (valid: 1..{len(apps)}). "
                f"For a raw port, use ':{n}'"
            )
        app = apps[n - 1]
        if not app.health.port:
            raise click.ClickException(f"app {app.name} has no health.port configured")
        port = app.health.port
    else:
        try:
            app = find_app(apps, target)
        except KeyError as e:
            raise click.ClickException(e.args[0] if e.args else str(e)) from e
        if not app.health.port:
            raise click.ClickException(f"app {target} has no health.port configured")
        port = app.health.port
    owner = manager._port_owner(port)
    if not owner:
        click.echo(f"port {port}: free (nobody listening)")
        return
    pid, name = owner
    if not yes and not click.confirm(f"kill pid={pid} ({name}) holding port {port}?"):
        click.echo("aborted")
        return
    victim = manager.reclaim_port(port)
    if victim:
        click.echo(f"port {port}: killed pid={victim[0]} ({victim[1]})")
    else:
        click.echo(f"port {port}: no action (already free or no permission)")


@cli.command()
@click.argument("name")
@click.option("--stderr", is_flag=True, help="看 stderr 而非 stdout")
@click.option("-n", "--lines", default=50, help="尾部行数")
@click.option("-f", "--follow", is_flag=True, help="持续跟踪输出")
@click.option("--clear", "clear", is_flag=True, help="清空日志文件 (不打印内容)")
@click.pass_context
def logs(
    ctx: click.Context, name: str,
    stderr: bool, lines: int, follow: bool, clear: bool,
) -> None:
    """查看应用日志 (从 ~/.localctl/<app>/ 读)。

    日志按大小自动滚动 (默认 5MB → .old), --clear 一并清掉 .old。
    """
    app = find_app(_load(ctx), name)
    if clear:
        touched = manager.clear_logs(app)
        click.echo(f"cleared {len(touched)} file(s):")
        for p in touched:
            click.echo(f"  {p}")
        return

    out_p, err_p = manager.log_paths(app)
    path = err_p if stderr else out_p
    if not path.exists():
        click.echo(f"(no log file: {path})")
        return

    out = sys.stdout.buffer
    def _emit(text: str) -> None:
        out.write(text.encode("utf-8", errors="replace"))

    with path.open("rb") as f:
        try:
            f.seek(0, 2)
            end = f.tell()
            chunk = min(end, 64 * 1024)
            f.seek(end - chunk)
            tail = f.read().decode("utf-8", errors="replace").splitlines()
            for line in tail[-lines:]:
                _emit(line + "\n")
            out.flush()
            if follow:
                while True:
                    new = f.readline()
                    if not new:
                        time.sleep(0.3)
                        continue
                    _emit(new.decode("utf-8", errors="replace"))
                    out.flush()
        except KeyboardInterrupt:
            pass


@cli.command(name="list")
@click.pass_context
def list_cmd(ctx: click.Context) -> None:
    """列出 apps.yaml 里所有已配置的应用 (带编号, 不查运行状态)。

    编号可直接用于其他命令: `localctl start 1`, `localctl logs 3 -f`。
    """
    apps = _load(ctx)
    if not apps:
        click.echo("(no apps configured)")
        return
    name_w = max(len(a.name) for a in apps)
    cmd_w  = max(len(" ".join(a.cmd)) for a in apps)
    for i, app in enumerate(apps, start=1):
        cmd_str = " ".join(app.cmd)
        click.echo(f"{i:>2}  {app.name:<{name_w}}  {cmd_str:<{cmd_w}}  ({app.cwd})")


@cli.command()
@click.pass_context
def edit(ctx: click.Context) -> None:
    """用 $EDITOR (Windows 缺省 notepad) 打开 apps.yaml。"""
    path: Path = ctx.obj.get("config") or DEFAULT_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("apps: []\n", encoding="utf-8")
        click.echo(f"created empty config: {path}")
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        editor = "notepad" if sys.platform == "win32" else "vi"
    # EDITOR may include args, e.g. "code -w" or "subl -n -w".
    parts = shlex.split(editor, posix=(sys.platform != "win32"))
    if not parts or not shutil.which(parts[0]):
        raise click.ClickException(
            f"editor not found on PATH: {editor!r}. set $EDITOR to an editor command."
        )
    subprocess.call([*parts, str(path)])
    # Validate after edit so typos surface immediately.
    try:
        load_config(path)
        click.echo("config valid.")
    except ConfigError as e:
        raise click.ClickException(f"config invalid after edit:\n  {e}")


if __name__ == "__main__":
    cli()
