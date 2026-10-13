import argparse
import json
import sys
from pathlib import Path

from infocenter import DEFAULT_HOST, DEFAULT_PORT, __version__, config
from infocenter.client import call, ensure_server, healthy, request, resolve_url
from infocenter.paths import home_dir
from infocenter.server import read_meta, serve, stop
from infocenter.store import MailError, normalize_name


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    argv, agent_arg, url_arg, home_arg, as_json = _peel_globals(raw)
    parser = argparse.ArgumentParser(
        prog="ic",
        description="信息中心：agent 之间的异步留言",
        epilog="全局参数可以写在任何位置：--as 名字、--url、--home、--json",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

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
    reply.add_argument("--body", "-b", required=True)
    reply.add_argument("--subject", "-s")
    reply.add_argument("--to")

    thread = sub.add_parser("thread", help="查看自己参与过的回合")
    thread.add_argument("id")

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
    crew_watch = crew_sub.add_parser("watch", help="（内部）收信守护进程，由 crew up 派生")
    crew_watch.add_argument("name")
    crew_watch.add_argument("--dir", required=True)
    crew_watch.add_argument("--poll", type=int, default=300)
    crew_watch.add_argument("--heartbeat", type=int, default=None, help="无信心跳间隔秒；不写则读 config")

    args = parser.parse_args(argv)
    args.agent = agent_arg
    args.url = url_arg
    args.json = as_json
    home = Path(home_arg).expanduser() if home_arg else home_dir()
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
            body = {"body": args.body}
            if args.subject:
                body["subject"] = args.subject
            if args.to:
                body["to"] = args.to
            payload = call(args.url, agent, "POST", f"/v1/messages/{args.id}/reply", body, home=home)
        elif args.cmd == "thread":
            payload = call(args.url, agent, "GET", f"/v1/threads/{args.id}", home=home)
        else:
            parser.error("未知命令")
            return 2
    except MailError as exc:
        sys.stderr.write(f"{exc.code}: {exc.message}\n")
        return 1
    _emit(args.json, args.cmd, payload)
    return 0


def _peel_globals(argv: list[str]):
    agent = url = home = None
    as_json = False
    rest: list[str] = []
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
            rest.append(item)
        index += 1
    return rest, agent, url, home, as_json


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
            crew.watch(home, args.name, str(Path(args.dir).expanduser().resolve()), url, args.poll, args.heartbeat)
            return 0
        if args.crew_cmd == "status":
            payload = crew.status(home, str(Path(args.dir).expanduser().resolve()) if args.dir else None)
        elif args.crew_cmd == "down":
            payload = crew.down(home, str(Path(args.dir).expanduser().resolve()) if args.dir else None)
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


def _emit(as_json: bool, cmd: str, payload) -> None:
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
            print(f"{item['name']}{role}  未读 {item.get('unread', 0)}  最后活动 {item.get('last_seen_local', '')}{desc}")
            if item.get("activity"):
                print(f"    正在: {item['activity']}  [{item.get('activity_at_local', '')}]")
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
        print(f"主题: {payload['subject']}")
        return
    if cmd == "inbox":
        label = {"unread": "未读", "read": "已读", "archived": "归档", "all": "全部"}[payload["status"]]
        print(f"{payload['agent']} 的{label} {payload['count']} 封")
        for message in payload["messages"]:
            mark = "!" if message["priority"] == "high" else " "
            print(f"{mark} {message['id']}  {message.get('created_at_local') or message['created_at']}  来自 {message['from']}")
            print(f"  {message['subject']}")
            if message.get("preview"):
                print(f"  {message['preview']}")
            elif message.get("body"):
                print(f"  {message['body']}")
        return
    if cmd in {"peek", "read", "archive"}:
        print(f"{payload['id']}  {payload['delivery_status'] or '发出'}  来自 {payload['from']}")
        print(f"给: {', '.join(payload['to'])}")
        print(f"主题: {payload['subject']}")
        print()
        print(payload.get("body", ""))
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))
