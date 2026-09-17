#!/usr/bin/env python3
"""CLI for my-schedule: create / list / get / update / delete / enable / disable."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib_schedule as sched  # noqa: E402


def cmd_create(args: argparse.Namespace) -> None:
    task_id = sched.resolve_task_id(args.task_id)
    if not task_id:
        raise SystemExit(
            "task_id required (--task-id or env TASK_ID). "
            "Schedules must bind to the current conversation room."
        )
    body = {
        "prompt": args.prompt,
        "schedule_expression": args.schedule_expression,
        "task_id": task_id,
        "timezone": args.timezone,
        "title": args.title or "",
        "enabled": not args.disabled,
    }
    sid = sched.resolve_runtime_session_id(args.runtime_session_id)
    if sid:
        body["runtime_session_id"] = sid
    payload = sched.api_request(
        "POST", "/api/schedules", user_id=args.user_id, body=body
    )
    sched.print_json(payload)


def cmd_list(args: argparse.Namespace) -> None:
    query = {}
    task_id = sched.resolve_task_id(args.task_id) if args.task_id or args.this_task else None
    if args.this_task and not task_id:
        raise SystemExit("TASK_ID env not set; cannot filter to this task")
    if task_id:
        query["task_id"] = task_id
    payload = sched.api_request(
        "GET", "/api/schedules", user_id=args.user_id, query=query or None
    )
    sched.print_json(payload)


def cmd_get(args: argparse.Namespace) -> None:
    payload = sched.api_request(
        "GET", f"/api/schedules/{args.job_id}", user_id=args.user_id
    )
    sched.print_json(payload)


def cmd_update(args: argparse.Namespace) -> None:
    body: dict = {}
    if args.prompt is not None:
        body["prompt"] = args.prompt
    expr = args.schedule_expression
    if expr is not None:
        body["schedule_expression"] = expr
    if args.timezone is not None:
        body["timezone"] = args.timezone
    if args.title is not None:
        body["title"] = args.title
    if args.enabled is not None:
        body["enabled"] = args.enabled
    if not body:
        raise SystemExit("Nothing to update")
    payload = sched.api_request(
        "PATCH",
        f"/api/schedules/{args.job_id}",
        user_id=args.user_id,
        body=body,
    )
    sched.print_json(payload)


def cmd_delete(args: argparse.Namespace) -> None:
    payload = sched.api_request(
        "DELETE", f"/api/schedules/{args.job_id}", user_id=args.user_id
    )
    sched.print_json(payload)


def cmd_enable(args: argparse.Namespace) -> None:
    payload = sched.api_request(
        "PATCH",
        f"/api/schedules/{args.job_id}",
        user_id=args.user_id,
        body={"enabled": True},
    )
    sched.print_json(payload)


def cmd_disable(args: argparse.Namespace) -> None:
    payload = sched.api_request(
        "PATCH",
        f"/api/schedules/{args.job_id}",
        user_id=args.user_id,
        body={"enabled": False},
    )
    sched.print_json(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="my-schedule manager")
    parser.add_argument("--user-id", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Create a recurring schedule")
    p_create.add_argument("--prompt", required=True)
    p_create.add_argument(
        "--cron",
        "--schedule-expression",
        dest="schedule_expression",
        required=True,
        help='e.g. "cron(0 8 * * ? *)" or "0 8 * * ? *" or "rate(1 hours)"',
    )
    p_create.add_argument("--timezone", default="Asia/Seoul")
    p_create.add_argument("--title", default="")
    p_create.add_argument("--task-id", default=None)
    p_create.add_argument("--runtime-session-id", default=None)
    p_create.add_argument("--disabled", action="store_true")
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list", help="List schedules")
    p_list.add_argument("--task-id", default=None)
    p_list.add_argument(
        "--this-task",
        action="store_true",
        help="Filter to TASK_ID from env",
    )
    p_list.set_defaults(func=cmd_list)

    p_get = sub.add_parser("get", help="Get one schedule")
    p_get.add_argument("job_id")
    p_get.set_defaults(func=cmd_get)

    p_update = sub.add_parser("update", help="Update a schedule")
    p_update.add_argument("job_id")
    p_update.add_argument("--prompt", default=None)
    p_update.add_argument(
        "--cron",
        "--schedule-expression",
        dest="schedule_expression",
        default=None,
    )
    p_update.add_argument("--timezone", default=None)
    p_update.add_argument("--title", default=None)
    p_update.add_argument("--enabled", type=lambda s: s.lower() in {"1", "true", "yes"}, default=None)
    p_update.set_defaults(func=cmd_update)

    p_del = sub.add_parser("delete", help="Delete a schedule")
    p_del.add_argument("job_id")
    p_del.set_defaults(func=cmd_delete)

    p_en = sub.add_parser("enable", help="Enable a schedule")
    p_en.add_argument("job_id")
    p_en.set_defaults(func=cmd_enable)

    p_dis = sub.add_parser("disable", help="Disable a schedule")
    p_dis.add_argument("job_id")
    p_dis.set_defaults(func=cmd_disable)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
