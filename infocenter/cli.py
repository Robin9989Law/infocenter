import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from infocenter import DEFAULT_HOST, DEFAULT_PORT, __version__, config
from infocenter.client import call, ensure_server, healthy, request, resolve_url
from infocenter.paths import home_dir
from infocenter.server import read_meta, serve, stop
from infocenter.store import MailError, beijing_stamp, normalize_name


# C-16：全局参数分两段处理。
# - 子命令之前的全局参数由 `_peel_globals` 只在「遇到子命令前」剥离，不再在任意位置无脑
#   剥离（旧实现会把「值恰为 --json/--as/--url/--home」的实参也吞掉）。
# - 子命令之后的全局参数交给 argparse 的 parent parser，写在子命令前/后都认，保持
#   README 里「全局参数可以写在任何位置」的约定。
_GLOBAL = argparse.ArgumentParser(add_help=False)
_GLOBAL.add_argument("--as", dest="agent", default=None, help="以某个身份操作（默认取 INFOCENTER_AGENT，否则 human）")
_GLOBAL.add_argument("--url", default=None, help="信息中心地址")
_GLOBAL.add_argument("--home", default=None, help="信息中心主目录")
_GLOBAL.add_argument("--json", action="store_true", help="JSON 输出")


# M6-4 列表收口：默认只留在办、暂停，以及这段时间内离场的。
# 时间界与计划里「离场超过 1 小时」相同，但只藏列表，不删行。
_RECENT_LEFT_SECONDS = 3600
_RECENT_LEFT_TEXT = "1 小时"
_PRESENT_WINDOW_STATES = {"active", "paused"}
_DEPARTED_WINDOW_STATES = {"left", "offline"}


def _parse_utc(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _window_kept_by_default(item: dict, now: datetime) -> bool:
    state = item.get("state") or ""
    if state in _PRESENT_WINDOW_STATES:
        return True
    if state not in _DEPARTED_WINDOW_STATES:
        return False
    moment = _parse_utc(item.get("left_at")) or _parse_utc(item.get("last_seen_at"))
    if moment is None:
        return False
    return (now - moment).total_seconds() <= _RECENT_LEFT_SECONDS


def _collapse_windows(payload: dict, now: datetime | None = None) -> dict:
    """默认列表。服务端 `state` 是精确匹配，没有离场时间参数，所以整表拉回再滤。"""
    rows = list(payload.get("windows") or [])
    moment = now or datetime.now(timezone.utc)
    kept = [row for row in rows if _window_kept_by_default(row, moment)]
    out = dict(payload)
    out["windows"] = kept
    out["count"] = len(kept)
    out["collapsed"] = True
    out["hidden"] = len(rows) - len(kept)
    return out


class _SubParser(argparse.ArgumentParser):
    """所有（含嵌套）子命令都带上全局参数，使子命令之后的 `--as/--url/--home/--json` 可用。"""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("parents", [_GLOBAL])
        super().__init__(*args, **kwargs)


def _peel_globals(argv: list[str]):
    """只在子命令之前剥离全局参数。

    返回 (剩余 argv, agent, url, home, json)。遇到第一个非全局 token（即子命令）立即停止，
    后面的内容原样交给 argparse（子命令带 parent parser，会自己认全局参数）。
    """
    agent = url = home = None
    as_json = False
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--json":
            as_json = True
        elif item in {"--as", "--url", "--home"}:
            index += 1
            if index >= len(argv):
                raise SystemExit(f"{item} 需要一个值")
            if item == "--as":
                agent = argv[index]
            elif item == "--url":
                url = argv[index]
            else:
                home = argv[index]
        elif item.startswith("--as=") or item.startswith("--url=") or item.startswith("--home="):
            key, value = item.split("=", 1)
            if key == "--as":
                agent = value
            elif key == "--url":
                url = value
            else:
                home = value
        else:
            break
        index += 1
    return argv[index:], agent, url, home, as_json


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    argv, agent_arg, url_arg, home_arg, as_json = _peel_globals(raw)
    parser = argparse.ArgumentParser(
        prog="ic",
        description="信息中心：agent 之间的异步留言",
        epilog="全局参数可以写在任何位置：--as 名字、--url、--home、--json",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True, parser_class=_SubParser)

    serve_p = sub.add_parser("serve", help="启动 HTTP 服务")
    serve_p.add_argument("--host", default=DEFAULT_HOST)
    serve_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_p.add_argument("--detach", action="store_true")

    sub.add_parser("stop", help="停止后台服务")
    sub.add_parser("status", help="查看服务和未读数，不会自动启动")
    agents_p = sub.add_parser("agents", help="列出或配置接入的 agent")
    agents_p.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove"])
    agents_p.add_argument("name", nargs="?", default=None, help="add/remove 的 agent 名字")
    agents_p.add_argument("--description", "-d", default="", help="add 时的描述")
    agents_p.add_argument("--role", default="", help="add 时的角色，coordinator 或留空")

    reg = sub.add_parser("register", help="注册或更新自己")
    reg.add_argument("--description", default=None)
    reg.add_argument("--activity", default=None, help="一句话状态自报：你在做什么，coordinator 据此调配")

    send = sub.add_parser("send", help="发一封留言")
    send.add_argument("--to", action="append", required=True, help="收件人。可重复。* 表示除自己以外的所有人")
    send.add_argument("--subject", "-s", required=True)
    send.add_argument("--body", "-b")
    send.add_argument("--body-file")
    send.add_argument("--priority", default="normal", choices=["normal", "high"])
    send.add_argument("--workspace")
    send.add_argument("--reply-to")

    inbox = sub.add_parser("inbox", help="看收件箱，不签收")
    inbox.add_argument("--status", default="unread", choices=["unread", "read", "archived", "all"])
    inbox.add_argument("--limit", type=int, default=20)
    inbox.add_argument("--full", action="store_true")
    inbox.add_argument("--workspace")

    peek = sub.add_parser("peek", help="看一封信，不签收")
    peek.add_argument("id")
    read = sub.add_parser("read", help="签收自己的那一份")
    read.add_argument("id")
    archive = sub.add_parser("archive", help="归档自己的那一份")
    archive.add_argument("id")

    reply = sub.add_parser("reply", help="回复一封信")
    reply.add_argument("id")
    # pi 2026-10-15：`--body` 进 shell 会被反引号当命令执行。与 send 对称，正文可从文件读。
    reply.add_argument("--body", "-b")
    reply.add_argument("--body-file")
    reply.add_argument("--subject", "-s")
    reply.add_argument("--to")

    thread = sub.add_parser("thread", help="查看自己参与过的回合")
    thread.add_argument("id")

    case = sub.add_parser("case", help="查看或续写一份案卷（办事追踪）")
    case.add_argument("id", help="案卷编号或线程 id")
    case.add_argument("--note", default=None, help="写回一行节点")
    case.add_argument("--status", default=None, help="顺手改案卷状态")
    case.add_argument("--wbs", default=None, help="给案卷打 WBS 路径（如 防混写/锁与闸）")
    case.add_argument("--milestone-add", dest="milestone_add", default=None,
                      help="加一条里程碑（**只有 coordinator 能改**）")
    case.add_argument("--milestone-owner", dest="milestone_owner", default=None, help="里程碑负责人")
    case.add_argument("--milestone-done", dest="milestone_done", default=None,
                      help="勾掉第 N 条里程碑（下标从 0 起；加 ! 前缀=撤回完成，如 '!0'）")

    cases = sub.add_parser("cases", help="列出案卷")
    cases.add_argument("--workspace", default=None)
    cases.add_argument("--status", default=None)
    cases.add_argument("--owner", default=None, help="主办身份")
    cases.add_argument("--wbs", default=None, help="只看这个 WBS（含其子路径）")
    cases.add_argument("--limit", type=int, default=20)

    task = sub.add_parser("task", help="任务队列（工作包）：建 / 看 / 开工 / 改状态 / 调优先级")
    task.add_argument("action", choices=["add", "show", "start", "set", "update", "bump"], help="子动作")
    task.add_argument("id", nargs="?", default=None, help="任务编号（add 不填）")
    task.add_argument("status", nargs="?", default=None, help="set 的目标状态：doing|blocked|done|stopped")
    task.add_argument("--title", default=None)
    task.add_argument("--owner", default=None, help="谁负责（默认=自己）")
    task.add_argument("--workspace", default=None, help="任务所属项目目录（必填）")
    task.add_argument("--deps", default=None, help="依赖：path@sha256:<hex>（多条用空格或分号）")
    task.add_argument("--acceptance", default=None, help="一行验收（≤200 字）")
    task.add_argument("--case", dest="case_id", default=None, help="挂到哪个案卷（控制包）")
    task.add_argument("--supersedes", default=None, help="取代哪条已终结的旧任务线")
    task.add_argument("--reason", default=None, help="blocked 必带一行原因")
    task.add_argument("--handoff", default=None, help="停任务必带的交接段（做到哪/产物在哪/别踩什么坑）")
    task.add_argument("--by", type=int, default=1, help="bump 的增量")
    task.add_argument("--priority", type=int, default=None)

    audit = sub.add_parser("audit", help="机器验证留痕：哈希链 + 重放是否与当前状态一致")
    audit.add_argument("--task", default=None, help="只验这一个任务")
    audit.add_argument("--quiet", action="store_true", help="静默，只用退出码（给 git 钩子用）")
    audit.add_argument("--anchor", action="store_true",
                       help="把当前链头写成**外锚**文件（要连同 git 一起提交；库被整表重做时能查出来）")
    audit.add_argument("--anchor-file", dest="anchor_file", default=".infocenter-audit-anchor",
                       help="外锚文件路径（默认 ./.infocenter-audit-anchor，放在仓库里随提交进 git）")

    guard = sub.add_parser("guard", help="提交闸：工作区改动是否都被写锁覆盖（程序防火墙）")
    guard.add_argument("--path", dest="paths", action="append", default=None,
                       help="只查这些路径；不填=自动取 git 工作区的改动文件")
    guard.add_argument("--quiet", action="store_true", help="静默，只用退出码")
    guard.add_argument("--any-holder", action="store_true",
                       help="只要求有锁覆盖（git 钩子用）；默认要求是你自己的锁")
    guard.add_argument("--staged", action="store_true",
                       help="只查**暂存区**（git 钩子用：闸门要盯「这次提交了什么」）；默认查整棵工作区（更严）")
    guard.add_argument("--install-hook", action="store_true",
                       help="把本仓库的提交闸钩子装上（git config core.hooksPath .githooks），幂等")

    queue = sub.add_parser("queue", help="拿不到钥匙时把要改的事记下来（进只增链，并写入自己的 TODO）")
    queue.add_argument("path", nargs="?", default=None, help="要改的文件（不填＝列全部排队）")
    queue.add_argument("--note", default=None, help="我要改什么（提议内容）")
    queue.add_argument("--patch-file", dest="patch_file", default=None, help="改动补丁（可选，从文件读）")
    queue.add_argument("--merge", action="store_true",
                       help="合并排队的改动（**只有拿钥匙的人**或 coordinator 能做）")
    queue.add_argument("--id", dest="ids", action="append", default=None,
                       help="只合并这几条（q<编号>；可重复给，也可逗号分隔：--id q271,q272）")
    queue.add_argument("--task", default=None, help="提议关联的任务")
    queue.add_argument("--no-todo", action="store_true",
                       help="只在库里留提议，不往自己的 TODO-<名字>.md 里写一行（默认**要写**：别忘了）")

    locks = sub.add_parser("locks", help="看现在谁锁了什么（写锁）")
    lock = sub.add_parser("lock", help="拿写锁：同一时间只有一个 agent 能改同一路径")
    lock.add_argument("paths", nargs="*", help="要锁的路径（相对路径会解析成绝对路径）")
    lock.add_argument("--ttl", type=int, default=None, help="锁的有效秒数（默认 1800，最短 30，最长 12h）")
    lock.add_argument("--task", dest="task_id", default=None, help="这次改的是哪个任务")
    lock.add_argument("--note", default=None, help="为什么锁")
    lock.add_argument("--force", action="store_true", help="强制抢占（仅 coordinator）")
    lock.add_argument("--changed", action="store_true",
                      help="锁住 git 工作区里所有改动文件（可与显式路径并用，取并集）")

    unlock = sub.add_parser("unlock", help="放写锁（默认只放自己的）")
    unlock.add_argument("paths", nargs="*", help="要放的路径；不填 = 放掉自己的全部锁")
    unlock.add_argument("--force", action="store_true", help="清场（仅 coordinator）")

    tasks = sub.add_parser("tasks", help="列出任务队列")
    tasks.add_argument("--mine", action="store_true", help="只看我的（按 workspace 隔离，默认 cwd）")
    tasks.add_argument("--owner", default=None)
    tasks.add_argument("--workspace", default=None)
    tasks.add_argument("--status", default=None)
    tasks.add_argument("--all", action="store_true", help="含被取代的旧线")
    tasks.add_argument("--limit", type=int, default=50)

    windows = sub.add_parser(
        "windows",
        help=f"列出办公室（默认：在办、暂停、{_RECENT_LEFT_TEXT}内离场；--all 看全）",
    )
    windows.add_argument("--agent", dest="window_agent", default=None, help="只看这个身份")
    windows.add_argument("--workspace", default=None, help="只看这个项目目录")
    windows.add_argument(
        "--state",
        default=None,
        help="按状态精确筛选（active、paused、left）。指定后列出该状态的全部，不再按离场时间收口",
    )
    windows.add_argument(
        "--all",
        action="store_true",
        help=f"列出全部办公室，包含离场超过 {_RECENT_LEFT_TEXT}的。不指定时默认隐藏这些",
    )

    mcp = sub.add_parser("mcp", help="以 stdio MCP 运行")
    mcp.add_argument("--agent", dest="mcp_agent", default=None, help="固定身份。不写则每个工具调用自己带 agent")
    mcp.add_argument("--no-notify", action="store_true", help="不推送到信提醒，工具结果也不带未读尾巴（工作会话用）")

    crew = sub.add_parser("crew", help="团队编排：拉起 agent、发启动信、查看、清理")
    crew_sub = crew.add_subparsers(dest="crew_cmd", required=True)
    crew_up = crew_sub.add_parser("up", help="按 config.json 逐 agent 拉起")
    crew_up.add_argument("--dir", required=True, help="项目目录")
    crew_up.add_argument("--dry-run", action="store_true", help="只预览将执行的命令")
    crew_up.add_argument("--only", default="", help="只拉部分 agent，逗号分隔")
    crew_kick = crew_sub.add_parser("kickoff", help="以 coordinator 身份发启动信")
    crew_kick.add_argument("--plan", required=True, help="分工文件路径")
    crew_kick.add_argument("--dir", required=True, help="项目目录，会记进 workspace")
    crew_kick.add_argument("--subject", default=None)
    crew_kick.add_argument("--from", dest="sender", default=None, help="多项目时指定发信身份，默认取 config 里第一个 coordinator")
    crew_status = crew_sub.add_parser("status", help="查看守护进程与 tmux 状态")
    crew_status.add_argument("--dir", default=None, help="只看某个项目")
    crew_down = crew_sub.add_parser("down", help="杀守护进程、关 tmux 会话")
    crew_down.add_argument("--dir", default=None, help="只清理某个项目")
    crew_down.add_argument("--only", default="", help="只停这些身份，逗号分隔，和 up --only 对称")
    for action, help_text in (
        ("pause", "暂停该身份的办公室和未封卷案卷"),
        ("resume", "恢复该身份已暂停的办公室和案卷"),
        ("abort", "中止该身份名下未封卷案卷"),
        ("close", "办结该身份名下未封卷案卷"),
    ):
        crew_action = crew_sub.add_parser(action, help=help_text)
        crew_action.add_argument("name", help="身份")
        crew_action.add_argument("--dir", default=None, help="只处理这个项目")
        crew_action.add_argument("--note", default=None, help="写进案卷的一行说明")
    crew_watch = crew_sub.add_parser("watch", help="（内部）收信守护进程，由 crew up 派生")
    crew_watch.add_argument("name")
    crew_watch.add_argument("--dir", required=True)
    crew_watch.add_argument("--poll", type=int, default=300)
    crew_watch.add_argument("--heartbeat", type=int, default=None, help="无信心跳间隔秒；不写则读 config")
    crew_watch.add_argument("--window", default=None, help="显式窗口号；不传则新开，不继承 INFOCENTER_WINDOW")

    sync_p = sub.add_parser("sync", help="经共享目录的 JSON 文件交换留言，不启动服务")
    sync_sub = sync_p.add_subparsers(dest="sync_cmd", required=True)
    for name, help_text in (
        ("export", "把本机信和回执写到自己的 outbox"),
        ("import", "从其他节点的 outbox 导入"),
        ("once", "先导出，再导入"),
    ):
        sync_cmd = sync_sub.add_parser(name, help=help_text)
        sync_cmd.add_argument("--dir", required=True, help="项目目录")

    args = parser.parse_args(argv)
    # 子命令之后给的全局参数由 parent parser 填进 args；之前给的由 _peel_globals 取出。
    args.agent = args.agent if args.agent is not None else agent_arg
    args.url = args.url if args.url is not None else url_arg
    args.home = args.home if args.home is not None else home_arg
    args.json = args.json or as_json
    home = Path(args.home).expanduser() if args.home else home_dir()
    try:
        if args.cmd == "serve":
            return serve(args.host, args.port, home, args.detach)
        if args.cmd == "stop":
            return stop(home)
        if args.cmd == "mcp":
            from infocenter.mcp_server import run_mcp

            run_mcp(_mcp_identity(args), args.url, home, notify=not args.no_notify)
            return 0
        if args.cmd == "crew":
            return _crew(args, home)
        if args.cmd == "sync":
            return _sync(args, home)
        agent = args.agent or _env_agent()
        if args.cmd == "status":
            return _status(args, home, agent)
        if args.cmd == "agents":
            if args.action == "list":
                payload = call(args.url, agent, "GET", "/v1/agents", home=home)
            elif args.action == "add":
                if not args.name:
                    parser.error("agents add 需要一个名字")
                entry = config.add(home, args.name, args.description, role=args.role)
                body = {"description": entry["description"]}
                if entry.get("role"):
                    body["role"] = entry["role"]
                payload = call(
                    args.url,
                    entry["name"],
                    "PUT",
                    f"/v1/agents/{entry['name']}",
                    body,
                    home=home,
                )
            else:
                if not args.name:
                    parser.error("agents remove 需要一个名字")
                name = normalize_name(args.name)
                payload = call(args.url, agent, "DELETE", f"/v1/agents/{name}", {}, home=home)
                payload["config_removed"] = config.remove(home, name)
        elif args.cmd == "register":
            body = {}
            if args.description is not None:
                body["description"] = args.description
            if args.activity is not None:
                body["activity"] = args.activity
            payload = call(args.url, agent, "PUT", f"/v1/agents/{agent}", body, home=home)
        elif args.cmd == "send":
            payload = call(
                args.url,
                agent,
                "POST",
                "/v1/messages",
                {
                    "to": ",".join(args.to),
                    "subject": args.subject,
                    "body": _read_body(args.body, args.body_file),
                    "priority": args.priority,
                    "workspace": args.workspace,
                    "reply_to": args.reply_to,
                },
                home=home,
            )
        elif args.cmd == "inbox":
            payload = call(
                args.url,
                agent,
                "GET",
                "/v1/inbox",
                query={
                    "status": args.status,
                    "limit": str(args.limit),
                    "full": "1" if args.full else "0",
                    "workspace": args.workspace,
                },
                home=home,
            )
        elif args.cmd == "peek":
            payload = call(args.url, agent, "GET", f"/v1/messages/{args.id}", home=home)
        elif args.cmd == "read":
            payload = call(args.url, agent, "POST", f"/v1/messages/{args.id}/read", {}, home=home)
        elif args.cmd == "archive":
            payload = call(args.url, agent, "POST", f"/v1/messages/{args.id}/archive", {}, home=home)
        elif args.cmd == "reply":
            body = {"body": _read_body(args.body, args.body_file)}
            if args.subject:
                body["subject"] = args.subject
            if args.to:
                body["to"] = args.to
            payload = call(args.url, agent, "POST", f"/v1/messages/{args.id}/reply", body, home=home)
        elif args.cmd == "thread":
            payload = call(args.url, agent, "GET", f"/v1/threads/{args.id}", home=home)
        elif args.cmd == "case":
            if args.milestone_add:
                body = {"name": args.milestone_add}
                if args.milestone_owner:
                    body["owner"] = args.milestone_owner
                payload = call(args.url, agent, "POST", f"/v1/cases/{args.id}/milestone", body, home=home)
            elif args.milestone_done is not None:
                raw = str(args.milestone_done).strip()
                done = not raw.startswith("!")
                body = {"index": int(raw.lstrip("!")), "done": done}
                if args.note:
                    body["note"] = args.note
                payload = call(
                    args.url, agent, "POST", f"/v1/cases/{args.id}/milestone-done", body, home=home
                )
            elif args.note:
                body = {"note": args.note, "action": "note"}
                if args.status:
                    body["status"] = args.status
                payload = call(args.url, agent, "POST", f"/v1/cases/{args.id}/nodes", body, home=home)
            elif args.wbs is not None:
                body = {"wbs_path": args.wbs}
                payload = call(args.url, agent, "POST", f"/v1/cases/{args.id}/wbs", body, home=home)
            else:
                payload = call(args.url, agent, "GET", f"/v1/cases/{args.id}", home=home)
        elif args.cmd == "cases":
            payload = call(
                args.url,
                agent,
                "GET",
                "/v1/cases",
                query={
                    "workspace": args.workspace,
                    "status": args.status,
                    "owner": args.owner,
                    "wbs": args.wbs,
                    "limit": str(args.limit),
                },
                home=home,
            )
        elif args.cmd == "audit":
            if args.anchor:
                return _write_audit_anchor(args, agent, home)
            query = {"task": args.task}
            anchor = _read_audit_anchor(_anchor_path(args.anchor_file))
            if anchor:
                query.update(
                    {
                        "anchor_seq": anchor["seq"],
                        "anchor_hash": anchor["hash"],
                        "anchor_at": anchor.get("at", ""),
                    }
                )
            payload = call(args.url, agent, "GET", "/v1/audit", query=query, home=home)
        elif args.cmd == "guard":
            import subprocess

            if args.install_hook:
                return _install_commit_hook(subprocess)
            paths = args.paths
            if not paths:
                paths = (_staged_paths if getattr(args, "staged", False) else _changed_paths)(subprocess)
                if not paths:
                    payload = {"ok": True, "checked": 0, "covered": [], "unlocked": [], "locked_by_others": []}
                    if not args.quiet:
                        print("提交闸：工作区无改动，放行")
                    return 0
            payload = call(
                args.url, agent, "POST", "/v1/guard",
                {"paths": paths, "require_mine": not args.any_holder}, home=home,
            )
        elif args.cmd == "lock":
            import subprocess

            lock_paths = list(args.paths or [])
            if args.changed:
                # `--changed` 与显式路径是**并集**（不是二选一）：既要锁住所有改动，
                # 也要能顺手锁住"马上要写、但还没改"的文件（如外锚文件）。
                seen = set(lock_paths)
                lock_paths += [path for path in _changed_paths(subprocess) if path not in seen]
                if not lock_paths:
                    if not args.quiet:
                        print("工作区没有改动，无需加锁")
                    return 0
            payload = call(
                args.url, agent, "POST", "/v1/locks",
                {
                    "paths": lock_paths,
                    "ttl": args.ttl,
                    "task_id": args.task_id,
                    "note": args.note,
                    "force": bool(args.force),
                    # --changed 的语义是"锁我这批脏文件"：别人锁着的脏文件不是我该拿的，跳过并报告
                    "skip_locked": bool(args.changed),
                },
                home=home,
            )
        elif args.cmd == "unlock":
            payload = call(
                args.url, agent, "POST", "/v1/locks/release",
                {"paths": args.paths or None, "force": bool(args.force)},
                home=home,
            )
        elif args.cmd == "queue":
            if args.merge:
                payload = call(
                    args.url, agent, "POST", "/v1/locks/queue/merge",
                    {"path": args.path, "ids": _split_ids(args.ids)}, home=home,
                )
            elif args.note or args.patch_file:
                payload = call(
                    args.url, agent, "POST", "/v1/locks/queue",
                    {"path": args.path, "note": args.note, "patch_file": args.patch_file,
                     "task": args.task},
                    home=home,
                )
                if not args.no_todo:
                    holder = payload.get("held_by")
                    note = (payload.get("note") or "").strip().replace("\n", " ")
                    if len(note) > 80:
                        note = note[:80] + "…"
                    where = f"（钥匙在 {holder} 手里）" if holder else "（现在没人拿钥匙，直接 lock 就能做）"
                    line = (
                        f"改 {payload.get('path')}：{note[:80]}{where}"
                        f" —— 提议 {payload.get('id')}；轮到这条时 `ic lock <路径>` **能拿到钥匙就做**"
                    )
                    # 只读 TODO 才去拿自己这份文件的钥匙；可写路径不碰锁，避免改现有测试的行为。
                    todo_url = args.url
                    todo_path = _todo_path(agent)
                    if todo_path.exists() and not (todo_path.stat().st_mode & 0o200):
                        todo_url = ensure_server(args.url, home)
                    payload["todo_file"] = _todo_add(agent, line, url=todo_url, home=home)
            else:
                payload = call(
                    args.url, agent, "GET", "/v1/locks/queue",
                    query={"path": args.path}, home=home,
                )
        elif args.cmd == "locks":
            payload = call(args.url, agent, "GET", "/v1/locks", home=home)
        elif args.cmd == "task":
            if args.action == "add":
                body = {
                    "title": args.title,
                    "owner": args.owner,
                    "workspace": args.workspace,
                    "deps": args.deps,
                    "acceptance": args.acceptance,
                    "case_id": args.case_id,
                    "supersedes": args.supersedes,
                }
                if args.priority is not None:
                    body["priority"] = args.priority
                payload = call(
                    args.url, agent, "POST", "/v1/tasks",
                    {key: value for key, value in body.items() if value is not None}, home=home,
                )
            elif args.action == "show":
                payload = call(args.url, agent, "GET", f"/v1/tasks/{args.id}", home=home)
            elif args.action == "start":
                payload = call(args.url, agent, "POST", f"/v1/tasks/{args.id}/start", {}, home=home)
            elif args.action == "set":
                body = {"status": args.status}
                if args.reason:
                    body["reason"] = args.reason
                if args.handoff:
                    body["handoff"] = args.handoff
                payload = call(args.url, agent, "POST", f"/v1/tasks/{args.id}/status", body, home=home)
            elif args.action == "update":
                fields = {
                    "title": args.title,
                    "owner": args.owner,
                    "deps": args.deps,
                    "acceptance": args.acceptance,
                    "case_id": args.case_id,
                    "priority": args.priority,
                }
                payload = call(
                    args.url, agent, "POST", f"/v1/tasks/{args.id}/update",
                    {key: value for key, value in fields.items() if value is not None}, home=home,
                )
            else:  # bump
                payload = call(args.url, agent, "POST", f"/v1/tasks/{args.id}/bump", {"by": args.by}, home=home)
        elif args.cmd == "tasks":
            owner = args.owner
            workspace = args.workspace
            if args.mine:
                owner = agent
                workspace = workspace or str(Path.cwd())
            payload = call(
                args.url,
                agent,
                "GET",
                "/v1/tasks",
                query={
                    "owner": owner,
                    "workspace": workspace,
                    "status": args.status,
                    "all": "1" if args.all else "0",
                    "limit": str(args.limit),
                },
                home=home,
            )
        elif args.cmd == "windows":
            payload = call(
                args.url,
                agent,
                "GET",
                "/v1/windows",
                query={
                    "agent": args.window_agent,
                    "workspace": args.workspace,
                    "state": args.state,
                },
                home=home,
            )
            # 显式 --state / --all 保持原语义。默认不改传 state=active：
            # 那样近期离场不在返回里，客户端滤不出来。
            if not args.all and not args.state:
                payload = _collapse_windows(payload)
        else:
            parser.error("未知命令")
            return 2
    except MailError as exc:
        sys.stderr.write(f"{exc.code}: {exc.message}\n")
        return 1
    if args.cmd == "audit" and getattr(args, "quiet", False):
        # 给 git 钩子用：一个字都不打，只给退出码
        return 0 if isinstance(payload, dict) and payload.get("ok") else 1
    _emit(args.json, args.cmd, payload, args=args, agent=agent, home=home)
    if args.cmd in ("guard", "audit") and isinstance(payload, dict) and not payload.get("ok"):
        return 1
    return 0


ANCHOR_HEADER = (
    "# 留痕外锚（链头）：这一行连同 git 一起提交。库被整表重做 / 链被改时，`ic audit` 靠它查出来。\n"
)


def _split_ids(values) -> list[str]:
    """`--id q271` / `--id q271 --id q272` / `--id q271,q272` 都认。

    argparse 用 `action="append"`：逗号写法会整串进来（`"q271,q272"`），
    直接下发会让 store 拿它去 `int()` ⇒ 500。在这里拆开，人怎么写都行。
    """
    out = []
    for chunk in values or []:
        out.extend(part.strip() for part in str(chunk).split(",") if part.strip())
    return out


def _todo_path(agent: str) -> Path:
    """提出者自己的 TODO 文件（与 crew 的约定一致：`TODO-<名字>.md`，在项目目录里）。"""
    return Path.cwd() / f"TODO-{agent}.md"


def _todo_add(agent: str, line: str, *, url=None, home=None) -> str:
    """**把"拿不到钥匙、先别忘"的事写进自己的 TODO**（human 定稿：等的 agent 的 todo 得有这个事）。

    写进文件（不是只留库里）才叫"别忘了"：agent 的心跳提示词本来就是"读自己的 TODO-<名字>.md
    → 推进下一件"，所以这一行会被它下一次心跳捡起来 —— 轮到那条时**能拿到钥匙就做**。

    文件还不存在、或本来就可写：直接写，不拿锁（现有可写路径的测试保持原行为）。
    文件已是只读（放下钥匙后停在 444）：先 `lock` 自己的 `TODO-<名字>.md`，拿得到钥匙
    服务端才会把它 chmod 成可写，写完 `unlock` 放回只读。拿不到就停，**不要自己 chmod 硬写**。
    """
    path = _todo_path(agent)
    entry = f"- [ ] {line}"
    try:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if entry in text:
                return str(path)
            if not text.endswith("\n"):
                text += "\n"
            body = text + entry + "\n"
            if path.stat().st_mode & 0o200:
                path.write_text(body, encoding="utf-8")
            else:
                _write_readonly_todo(agent, path, body, url=url, home=home)
        else:
            path.write_text(f"# TODO · {agent}\n\n{entry}\n", encoding="utf-8")
    except MailError:
        raise
    except OSError as exc:
        raise MailError("todo_unwritable", f"写不进 TODO 文件 {path}：{exc}", 500) from None
    return str(path)


def _write_readonly_todo(agent: str, path: Path, text: str, *, url=None, home=None) -> None:
    """只读 TODO：先拿这把钥匙，拿到才写，写完放回。拿不到就停，自己不 chmod。"""
    if not url:
        raise MailError(
            "todo_unwritable",
            f"写不进 TODO 文件 {path}：文件只读，没有可用的信息中心地址去拿钥匙，已停，没有改权限",
            409,
        )
    target = str(path.resolve())
    try:
        call(
            url, agent, "POST", "/v1/locks",
            {"paths": [target], "note": "写入自己的 TODO"},
            home=home,
        )
    except MailError as exc:
        raise MailError(
            "todo_unwritable",
            f"写不进 TODO 文件 {path}：拿不到钥匙（{exc.code}），已停，没有改权限",
            exc.status or 409,
        ) from None
    try:
        if not (path.stat().st_mode & 0o200):
            raise MailError(
                "todo_unwritable",
                f"写不进 TODO 文件 {path}：拿到钥匙后仍不可写，已停，没有再改权限",
                500,
            )
        path.write_text(text, encoding="utf-8")
    finally:
        try:
            call(url, agent, "POST", "/v1/locks/release", {"paths": [target]}, home=home)
        except MailError:
            pass


def _anchor_path(value) -> "Path":
    """外锚文件路径：相对路径一律以**仓库根**为准。

    pi 实测：按 cwd 解析时，在子目录跑 `ic audit` 会报「外锚：无」＝**锚校验被静默跳过**
    （整表重做查不出来），`ic audit --anchor` 还会在子目录另写一个锚文件、把根锚抛下。
    """
    path = Path(value)
    if path.is_absolute():
        return path
    import subprocess

    top = _git_toplevel(subprocess)
    return (Path(top) / path) if top else path


def _read_audit_anchor(path) -> dict | None:
    """读外锚文件（没有/读不懂都当没有，不拦人）。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and len(parts[1]) == 64:
            return {"seq": int(parts[0]), "hash": parts[1], "at": " ".join(parts[2:])}
    return None


def _write_audit_anchor(args, agent: str, home=None) -> int:
    """把当前链头写成外锚文件。**链不干净就拒绝打锚**（不能给坏链盖章）。"""
    payload = call(args.url, agent, "GET", "/v1/audit", query={"task": args.task}, home=home)
    if not payload.get("ok"):
        print("留痕校验不通过 ⇒ 拒绝打外锚（打锚前链必须是好的）")
        return 1
    seq, tip = payload.get("tip_seq") or 0, payload.get("tip_hash") or ""
    if not seq or not tip:
        print("还没有任何痕迹，没什么可锚的")
        return 0
    # 洗锚防护（cursor）：**先拿现有锚验一次**，对不上就拒绝覆盖——
    # 否则伪造过的链会被 `--anchor` 就地"盖章"，把旧锚作废。
    existing = _read_audit_anchor(_anchor_path(args.anchor_file))
    if existing:
        checked = call(
            args.url, agent, "GET", "/v1/audit",
            query={
                "task": args.task,
                "anchor_seq": existing["seq"],
                "anchor_hash": existing["hash"],
                "anchor_at": existing.get("at", ""),
            },
            home=home,
        )
        if not (checked.get("anchor") or {}).get("ok"):
            print("**拒绝打锚**：现有外锚对不上（" + str((checked.get("anchor") or {}).get("why") or "未知") + "）")
            print("  说明库里的链在这之后被改过/重做过。先查清再打锚，别把旧锚作废。")
            return 1
    stamp = beijing_stamp()
    path = _anchor_path(args.anchor_file)
    path.write_text(ANCHOR_HEADER + f"{seq} {tip} {stamp}\n", encoding="utf-8")
    print(f"外锚已写入 {path}")
    print(f"  seq {seq}  {tip[:16]}…  （{stamp} 北京时间）")
    print("  库内痕迹已被钉住：链被改/被截短/整表重做，`ic audit` 都会报「外锚对不上」。")
    print("  **记得 git 提交这个文件**——提交之前，锚还不算在库外。")
    return 0


def _install_commit_hook(subprocess, cwd=None) -> int:
    """装提交闸钩子：新 clone 一条命令装上，别让第 2 道闸变成「可选项」。幂等。"""
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True)
    if top.returncode != 0 or not top.stdout.strip():
        print("这里不是 git 仓库（或没装 git），提交闸装不了")
        return 1
    root = top.stdout.strip()
    hook = Path(root) / ".githooks" / "pre-commit"
    if not hook.exists():
        print(f"仓库里没有自带钩子 {hook}，装不了")
        return 1
    hook.chmod(hook.stat().st_mode | 0o755)
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=root, capture_output=True, text=True)
    got = subprocess.run(
        ["git", "config", "core.hooksPath"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    if got != ".githooks":
        print(f"装上了但读回来是 {got!r}，请手工执行：git config core.hooksPath .githooks")
        return 1
    print(f"提交闸已装（幂等）：core.hooksPath = .githooks，钩子 {hook}")
    print("  以后每次 git commit / merge 都会自动跑两道：`ic guard --quiet --any-holder --staged` + `ic audit --quiet`。")
    return 0


def _hook_warning() -> str:
    """钩子没装就冒一句——闸门只在「有人手动跑 ic guard」时生效，这必须让人知道。"""
    import subprocess

    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0 or not top.stdout.strip():
        return ""
    got = subprocess.run(
        ["git", "config", "core.hooksPath"], cwd=top.stdout.strip(), capture_output=True, text=True
    ).stdout.strip()
    if got == ".githooks":
        return ""
    return "  ⚠ 本仓库没装提交闸钩子：现在只有显式跑 `ic guard` 才拦得住（`ic guard --install-hook` 装上）"


def _git_toplevel(subprocess) -> str:
    """仓库根（**一切 git 路径都以它为基准**，绝不用 cwd——子目录跑会算出假路径）。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _git_paths(subprocess, args: list[str], *, status_in_record: bool) -> list[str]:
    """跑一条 git 路径类命令，返回**绝对路径**（`-z` 输出，天然不转义）。

    三条铁律（pi 2026-10-15 实测报的洞，坏起来是**静默**的，不是报错）：
      ① **不用** `--porcelain` 的默认文本输出：`core.quotepath` 默认 true，非 ASCII（如中文目录）
         会被写成 `"\351\234\200…"`（带引号 + 反斜杠八进制）⇒ 解析出来是**不存在的路径**，
         于是 lock 与 guard 用同一个假 key、互相自洽、**照样放行**，互斥却已经失效；
         故用 `-z` + `-c core.quotepath=false`。
      ② 路径是**仓库根**相对 ⇒ 以 `_git_toplevel()` 为基准拼，**不用 cwd**。
      ③ `-z` 下两种输出形态不一样：`--porcelain` 是 `XY␠路径` 一条记录（rename 时紧跟一条
         `原路径`），而 `--name-only` 是每条记录就是一个路径 ⇒ 用 `status_in_record` 区分，
         不许再按 `" -> "` 猜（文件名本身含 ` -> ` 会切错）。
    """
    from pathlib import Path as _P

    top = _git_toplevel(subprocess)
    if not top:
        return []
    try:
        out = subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    records = [r for r in out.stdout.decode("utf-8", "surrogateescape").split("\0") if r]
    paths: list[str] = []
    index = 0
    while index < len(records):
        if status_in_record:
            record = records[index]
            index += 1
            code, name = record[:2], record[3:]
            if code[:1] in {"R", "C"} or code[1:2] in {"R", "C"}:
                index += 1  # 紧跟一条"原路径"记录，跳过
            if "D" in code or not name:
                continue  # 删除的不算：没有可锁的文件
        else:
            name = records[index]
            index += 1
        paths.append(str((_P(top) / name).resolve()))
    return paths


def _changed_paths(subprocess) -> list[str]:
    """工作区里被改/新增的文件（绝对路径，**不转义、按仓库根**）。`ic lock --changed` 用。"""
    return _git_paths(
        subprocess, ["status", "--porcelain", "-z", "-uall"], status_in_record=True
    )


def _staged_paths(subprocess) -> list[str]:
    """**暂存区**里被改/新增的文件（绝对路径）。`ic guard --staged` 用：闸门要盯的是
    "这次提交了什么"，而不是整棵工作区（cursor 实测：一个未跟踪的 junk.tmp 就能让空提交失败）。"""
    return _git_paths(
        subprocess,
        ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"],
        status_in_record=False,
    )


def _mcp_identity(args) -> str | None:
    import os

    if args.mcp_agent:
        return args.mcp_agent
    if args.agent:
        return args.agent
    env = os.environ.get("INFOCENTER_AGENT", "").strip()
    return env or None


def _env_agent() -> str:
    import os

    return os.environ.get("INFOCENTER_AGENT", "").strip() or "human"


def _read_body(body: str | None, body_file: str | None) -> str:
    if body and body_file:
        raise MailError("invalid_body", "--body 和 --body-file 只能用一个")
    if body_file:
        return Path(body_file).read_text(encoding="utf-8")
    if body is None:
        raise MailError("invalid_body", "需要 --body 或 --body-file")
    return body


def _status(args, home: Path, agent: str) -> int:
    url = resolve_url(args.url, home)
    meta = read_meta(home) or {}
    if not healthy(url):
        print(f"未启动（{url}）")
        return 1
    try:
        agents = request(url, "GET", "/v1/agents", agent).get("agents") or []
    except Exception as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    unread = {item["name"]: item.get("unread", 0) for item in agents}
    mine = unread.get(agent, 0)
    if args.json:
        print(json.dumps({
            "url": url, "pid": meta.get("pid"), "agent": agent,
            "unread": mine, "unread_by_agent": {name: count for name, count in unread.items() if count},
        }, ensure_ascii=False))
        return 0
    pid = meta.get("pid")
    print(url)
    if pid:
        print(f"pid {pid}")
    print(f"你是 {agent}，未读 {mine}")
    backlog = sorted((name, count) for name, count in unread.items() if count)
    if backlog:
        print("各家未读: " + ", ".join(f"{name} {count}" for name, count in backlog))
    else:
        print("各家未读: 都清零了")
    return 0


def _crew(args, home: Path) -> int:
    from infocenter import crew

    try:
        if args.crew_cmd == "watch":
            url = resolve_url(args.url, home)
            return crew.watch(
                home,
                args.name,
                str(Path(args.dir).expanduser().resolve()),
                url,
                args.poll,
                args.heartbeat,
                window=args.window,
            )
        if args.crew_cmd == "status":
            payload = crew.status(home, str(Path(args.dir).expanduser().resolve()) if args.dir else None)
        elif args.crew_cmd == "down":
            payload = crew.down(
                home,
                str(Path(args.dir).expanduser().resolve()) if args.dir else None,
                args.only,
            )
        elif args.crew_cmd in {"pause", "resume", "abort", "close"}:
            project = str(Path(args.dir).expanduser().resolve()) if args.dir else None
            if project and not Path(project).is_dir():
                raise MailError("no_dir", f"没有这个目录：{project}")
            url = ensure_server(args.url, home)
            payload = crew.control(home, url, args.crew_cmd, args.name, project, args.note)
        else:
            project = str(Path(args.dir).expanduser().resolve())
            if not Path(project).is_dir():
                raise MailError("no_dir", f"没有这个目录：{project}")
            url = ensure_server(args.url, home)
            if args.crew_cmd == "up":
                payload = crew.up(home, url, project, args.only, args.dry_run)
            else:
                payload = crew.kickoff(home, url, args.plan, project, args.subject, args.sender)
    except MailError as exc:
        sys.stderr.write(f"{exc.code}: {exc.message}\n")
        return 1
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("\n".join(payload.get("lines") or []))
    return 0


_SYNC_EXIT_2 = {
    "node_not_configured",
    "invalid_node",
    "invalid_dir",
    "invalid_peers",
    "invalid_outbox",
}
_SYNC_COUNT_KEYS = (
    "exported",
    "imported",
    "receipts_out",
    "receipts_in",
    "skipped",
    "rejected",
    "conflict",
)


def _sync(args, home: Path) -> int:
    from infocenter.sync import run_sync

    try:
        payload = run_sync(home, args.dir, args.sync_cmd)
    except MailError as exc:
        sys.stderr.write(f"{exc.code}: {exc.message}\n")
        return 2 if exc.code in _SYNC_EXIT_2 else 1
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(" ".join(f"{key}={payload[key]}" for key in _SYNC_COUNT_KEYS))
    return 0


def _emit(as_json: bool, cmd: str, payload, args=None, agent=None, home=None) -> None:
    if cmd == "agents" and isinstance(payload, dict) and "agents" not in payload:
        if "config_removed" in payload:
            if payload.get("config_removed") or payload.get("removed"):
                print(f"已移除 {payload['name']}")
            else:
                print(f"{payload['name']} 本来就不在名单里")
        else:
            suffix = f"（{payload['description']}）" if payload.get("description") else ""
            print(f"已添加 {payload['name']}{suffix}")
        return
    if cmd == "agents" and isinstance(payload, dict) and "agents" in payload and not as_json:
        for item in payload["agents"]:
            role = " [coordinator]" if item.get("role") == "coordinator" else ""
            desc = f"（{item['description']}）" if item.get("description") else ""
            print(f"{item['name']}{role}  未读 {item.get('unread', 0)}  最后活动 {item.get('last_seen_beijing', '')}{desc}")
            if item.get("activity"):
                print(f"    正在: {item['activity']}  [{item.get('activity_at_beijing', '')}]")
            for office in item.get("windows") or []:
                label = office.get("state_label") or office.get("state") or ""
                where = office.get("workspace") or ""
                print(f"    办公室 {office.get('window_id')}  {label}  {where}".rstrip())
        return
    if as_json or cmd in {"agents", "thread"}:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if cmd == "register":
        print(f"已登记 {payload['name']}")
        if payload.get("description"):
            print(payload["description"])
        return
    if cmd == "send" or cmd == "reply":
        print(f"已送出 {payload['id']}")
        print(f"给: {', '.join(payload['to'])}")
        if payload.get("created_at_beijing"):
            print(f"时间: {payload['created_at_beijing']}（北京时间）")
        print(f"主题: {payload['subject']}")
        return
    if cmd == "inbox":
        label = {"unread": "未读", "read": "已读", "archived": "归档", "all": "全部"}[payload["status"]]
        print(f"{payload['agent']} 的{label} {payload['count']} 封")
        for message in payload["messages"]:
            mark = "!" if message["priority"] == "high" else " "
            print(f"{mark} {message['id']}  {message.get('created_at_beijing') or message['created_at']}  来自 {message['from']}")
            print(f"  {message['subject']}")
            if message.get("preview"):
                print(f"  {message['preview']}")
            elif message.get("body"):
                print(f"  {message['body']}")
        return
    if cmd == "case" and not as_json:
        title = payload.get("title") or payload.get("subject")
        if title:
            print(title)
        print(f"WBS: {payload.get('wbs_path') or '（未归类）'}")
        if payload.get("human_note"):
            mark = "  ⚠ 人话过期（这之后又发生了别的事，该重写）" if payload.get("human_stale") else ""
            print(
                f"人话（{payload.get('human_by')} · {payload.get('human_at_beijing')}）: "
                f"{payload['human_note']}{mark}"
            )
        else:
            print("人话: 还没有（看板上的说法只能由 coordinator 用 `ic case <id> --note \"…\"` 写）")
        stones = payload.get("milestones") or []
        if stones:
            print(f"里程碑 {payload.get('milestones_done', 0)}/{len(stones)}：")
            for index, item in enumerate(stones):
                mark = "✔" if item["done"] else "·"
                who = f"  （{item['owner']}）" if item.get("owner") else ""
                print(f"  {index}. {mark} {item['name']}{who}")
        else:
            print("里程碑：无（`ic case <id> --milestone-add \"名字\"`，只有 coordinator 能改）")
        print(payload.get("text") or json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if cmd == "cases" and not as_json:
        rows = payload.get("cases") or []
        print(f"案卷 {payload.get('count', len(rows))} 份")
        for item in rows:
            owner = item.get("owner_agent") or item.get("owner") or "未认领"
            wbs = item.get("wbs_path") or "未归类"
            total = item.get("milestones_total") or 0
            reached = f"  里程碑 {item.get('milestones_done', 0)}/{total}" if total else ""
            marks = []
            if item.get("human_stale"):
                marks.append("人话过期")
            if item.get("stalled"):
                marks.append(f"停滞 {item.get('stalled_hours')}h")
            stale = ("  ⚠ " + " / ".join(marks)) if marks else ""
            print(
                f"{item.get('id')}  {item.get('status_label') or item.get('status')}  {owner}  "
                f"[{wbs}]  {item.get('title')}{reached}{stale}"
            )
        return
    if cmd == "audit" and not as_json:
        if payload.get("ok"):
            print(f"留痕校验：通过（{payload.get('events', 0)} 行痕迹 / {payload.get('tasks', 0)} 个任务，链完整、重放一致）")
        else:
            print(f"留痕校验：**不通过**（{payload.get('events', 0)} 行痕迹 / {payload.get('tasks', 0)} 个任务）")
            for item in payload.get("chain_broken") or []:
                print(f"  链断: seq {item['seq']}  任务 {item['task_id']}  {item['why']}")
            for item in payload.get("mismatches") or []:
                if item.get("field"):
                    print(f"  对不上: 任务 {item['task_id']}  字段 {item['field']}  痕迹={item['replay']!r} 现状={item['row']!r}")
                else:
                    print(f"  对不上: 任务 {item['task_id']}  {item['why']}")
        anchor = payload.get("anchor")
        if anchor:
            if anchor.get("ok"):
                print(f"  外锚: 对上（seq {anchor['seq']}，{anchor.get('at') or '未记时间'}）")
                if anchor.get("unwitnessed"):
                    print(
                        f"       锚点之后还有 {anchor['unwitnessed']} 行没被见证"
                        f"（不受锚保护，建议再打一次：`ic audit --anchor`）"
                    )
            else:
                print(f"  外锚: **对不上** —— {anchor.get('why')}")
        else:
            print("  外锚: 无（`ic audit --anchor` 打一个，并把它提交进 git，才挡得住整表重做）")
        return
    if cmd == "guard" and not as_json:
        if payload.get("ok"):
            print(f"提交闸：放行（{payload.get('checked', 0)} 个改动都有锁）")
        else:
            print(f"提交闸：**拦住**（查了 {payload.get('checked', 0)} 个）")
            for path in payload.get("unlocked") or []:
                print(f"  没有锁: {path}")
            for item in payload.get("locked_by_others") or []:
                print(f"  被别人锁着: {item['path']}  ← {item['holder']}")
            print("  规矩：写文件前先 `ic lock <路径>`；拿不到就 `ic queue <路径> --note \"要改什么\"`")
            print("       （写进你自己的 TODO），然后去做别的。不要发信、不要硬写、不要干等。")
        warn = _hook_warning()
        if warn:
            print(warn)
        return
    if cmd == "lock" and not as_json:
        rows = payload.get("locks") or []
        print(
            f"已拿到 {len(rows)} 个写锁（{payload.get('agent')}，到 "
            f"{payload.get('expires_at_beijing') or payload.get('expires_at')} 过期）——只有你能改这些路径"
            f"（续期：重复执行同一命令即可）"
        )
        for item in rows:
            tail = f"（任务 {item['task_id']}）" if item.get("task_id") else ""
            print(f"  {item['path']}{tail}")
        for item in payload.get("skipped") or []:
            print(f"  ⏭ 跳过（{item['agent']} 锁着）：{item['path']}")
        for item in payload.get("queued") or []:
            first = item.get("first") or "?"
            mine = "（就是你）" if first == payload.get("agent") else ""
            print(f"  📥 {item['path']} 还有 {item['count']} 条待改（各自照自己的 TODO 轮）")
        return
    if cmd == "unlock" and not as_json:
        count = payload.get("released_count")
        if count is None:
            count = len(payload.get("released") or [])
        suffix = "（强制清场）" if payload.get("force") else ""
        print(f"已放锁 {count} 个{suffix}")
        for item in payload.get("pending_left") or []:
            print(f"  📥 {item['path']} 还有 {item['count']} 条待改（各人照自己的 TODO 轮，不必互相说）")
        return
    if cmd == "queue" and not as_json:
        if payload.get("path") and "merged" in payload:
            print(
                f"已合并 {len(payload.get('merged') or [])} 条（{payload.get('path')}）；"
                f"这个文件还剩 {payload.get('remaining', 0)} 条排队"
            )
            return
        if payload.get("id") and payload.get("note") is not None and "seq" in payload:
            holder = payload.get("held_by")
            print(f"已记下 {payload['id']}（{payload['path']}）")
            print(f"  内容：{payload.get('note') or '（见补丁）'}")
            if holder:
                print(f"  现在钥匙在 {holder} 手里（它做完直接放钥匙）")
            if not holder:
                print(f"  现在没人拿钥匙：直接 `ic lock {payload['path']}` 就做，别等")
            if payload.get("todo_file"):
                print(f"  ✍️ 已写进你的 TODO：{payload['todo_file']}")
                print("     先去做别的（别干等）；照你自己的 TODO 往下走，**轮到这条时能拿到钥匙就做**")
            print("  → 谁提谁落：这条是你的活，钥匙到手就落")
            return
        pending = payload.get("pending") or []
        total = payload.get("count", len(pending))
        print(f"待改清单 {total} 条（不排队、不通知：各人照自己的 TODO 轮，能做就做）")
        for item in pending:
            who = item.get("held_by")
            print(
                f"  {item.get('id')}  {item.get('path')}  ← {item.get('agent')}"
                f"（{item.get('at_beijing','')}）"
            )
            print(f"      要改：{item.get('note') or '（见补丁）'}")
            if who:
                print(f"      → 钥匙在 {who} 手里：能拿到就做，拿不到就跳过、做你 TODO 的下一件")
        if not pending:
            print("  （没有排队；`ic queue <路径> --note \"我要改什么\"` 可以提一条）")
        return
    if cmd == "locks" and not as_json:
        rows = payload.get("locks") or []
        print(f"写锁 {payload.get('count', len(rows))} 个")
        for item in rows:
            task = f"（任务 {item['task_id']}）" if item.get("task_id") else ""
            left = int(item.get("expires_in_seconds") or 0)
            queued = int(item.get("queued") or 0)
            soon = "  ⚠ 快到期：重复 `ic lock <路径>` 即续期" if left <= 300 else ""
            print(
                f"  {item['path']}  ← {item['agent']}{task}  到 "
                f"{item.get('expires_at_beijing') or item['expires_at']}（剩 {left // 60} 分）{soon}"
                + (f"  📥 有 {queued} 条待改（`ic queue {item['path']}` 看）" if queued else "")
            )
        return
    if cmd == "tasks" and not as_json:
        rows = payload.get("tasks") or []
        total = payload.get("total", len(rows))
        print(f"任务 {total} 条（活线 {payload.get('count', len(rows))}）")
        for item in rows:
            mark = "!" if item.get("priority") else " "
            print(f"{mark} {item.get('id')}  {str(item.get('status')):<8} {str(item.get('owner')):<9} {item.get('title')}")
            if item.get("deps"):
                print(f"    依赖: {item['deps']}")
            if item.get("acceptance"):
                print(f"    验收: {item['acceptance']}")
            if item.get("blocked_reason"):
                print(f"    卡在: {item['blocked_reason']}")
        for old in payload.get("superseded") or []:
            tail = f"；交接: {old['handoff']}" if old.get("handoff") else ""
            print(
                f"  ← {old.get('id')}  {old.get('status')}  {old.get('title')}"
                f"（被 {old.get('superseded_by') or '?'} 取代{tail}）"
            )
        return
    if cmd == "task" and not as_json:
        print(
            f"{payload.get('id')}  {payload.get('title')}  [{payload.get('status')}]"
            f"  owner={payload.get('owner')}  priority={payload.get('priority', 0)}"
        )
        if payload.get("created_at_beijing"):
            print(f"时间: 建于 {payload['created_at_beijing']}，动过 {payload.get('updated_at_beijing')}（北京时间）")
        if payload.get("case_id"):
            print(f"控制包: {payload['case_id']}")
        if payload.get("deps"):
            print(f"依赖: {payload['deps']}")
        if payload.get("acceptance"):
            print(f"验收: {payload['acceptance']}")
        if payload.get("blocked_reason"):
            print(f"卡在: {payload['blocked_reason']}")
        if payload.get("handoff"):
            print(f"交接: {payload['handoff']}")
        chain = payload.get("supersedes_chain") or []
        if chain:
            print(f"第 {payload.get('attempt')} 次尝试；此前：")
            for prev in chain:
                extra = f"  交接: {prev['handoff'][:60]}" if prev.get("handoff") else ""
                print(f"  ← {prev.get('id')}  {prev.get('status')}  {prev.get('title')}{extra}")
        if payload.get("superseded_by"):
            print(f"已被 {payload['superseded_by']} 取代")
        return
    if cmd == "windows" and not as_json:
        rows = payload.get("windows") or []
        count = payload.get("count", len(rows))
        hidden = payload.get("hidden") or 0
        if payload.get("collapsed") and hidden:
            print(
                f"办公室 {count} 间（默认只列在办、暂停和 {_RECENT_LEFT_TEXT}内离场，"
                f"另有 {hidden} 间未列出；--all 看全）"
            )
        else:
            print(f"办公室 {count} 间")
        for item in rows:
            label = item.get("state_label") or item.get("state") or ""
            print(
                f"{item.get('window_id')}  {item.get('agent')}  {label}  "
                f"在办 {item.get('case_count', 0)}  {item.get('workspace') or ''}".rstrip()
            )
        return
    if cmd in {"peek", "read", "archive"}:
        print(f"{payload['id']}  {payload['delivery_status'] or '发出'}  来自 {payload['from']}")
        print(f"给: {', '.join(payload['to'])}")
        if payload.get("created_at_beijing"):
            print(f"时间: {payload['created_at_beijing']}（北京时间）")
        print(f"主题: {payload['subject']}")
        print()
        print(payload.get("body", ""))
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))
