"""Codex Traffic Light MXP (Windows port) — command line control tool.

Usage:
  codex-light-mxp [--task <task-id>] [--workspace <path>] [--json] <working|done|waiting|idle|status|clear|quit>
  codex-light-mxp quota --five-hour <0-100> --weekly <0-100> [--json]
  codex-light-mxp quota --stdin [--json]
  codex-light-mxp quota --app-server [--json]
"""
from __future__ import annotations

import json
import os
import sys

# Make the package importable when run as a plain script from any directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from codex_light.core import (  # noqa: E402
    CommandContract,
    LightState,
    StateSnapshot,
    StateStore,
    StateStoreError,
    parse_state,
    resolve_task_id,
    resolve_workspace,
)
from codex_light.quota import CodexAppServerQuotaCollector, QuotaExtractor  # noqa: E402


def usage() -> None:
    sys.stderr.write(
        f"""Usage: {CommandContract.light_command_name} [--task <task-id>] [--workspace <path>] [--json] <working|done|waiting|idle|status|clear|quit>
       {CommandContract.light_command_name} {CommandContract.quota_command_name} --five-hour <0-100> --weekly <0-100> [--json]
       {CommandContract.light_command_name} {CommandContract.quota_command_name} --stdin [--json]
       {CommandContract.light_command_name} {CommandContract.quota_command_name} --app-server [--json]

"""
    )


class CLIOptions:
    def __init__(self):
        self.task_id = None
        self.workspace = None
        self.json = False
        self.stdin = False
        self.app_server = False
        self.five_hour_percent = None
        self.weekly_percent = None
        self.command = None


def parse(arguments: list) -> CLIOptions:
    options = CLIOptions()
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--task":
            index += 1
            if index >= len(arguments):
                raise StateStoreError("--task requires a value")
            options.task_id = arguments[index]
        elif argument == "--workspace":
            index += 1
            if index >= len(arguments):
                raise StateStoreError("--workspace requires a value")
            options.workspace = arguments[index]
        elif argument == "--json":
            options.json = True
        elif argument == "--stdin":
            options.stdin = True
        elif argument == "--app-server":
            options.app_server = True
        elif argument == "--five-hour":
            index += 1
            if index >= len(arguments):
                raise StateStoreError("--five-hour requires an integer")
            try:
                options.five_hour_percent = int(arguments[index])
            except ValueError:
                raise StateStoreError("--five-hour requires an integer")
        elif argument == "--weekly":
            index += 1
            if index >= len(arguments):
                raise StateStoreError("--weekly requires an integer")
            try:
                options.weekly_percent = int(arguments[index])
            except ValueError:
                raise StateStoreError("--weekly requires an integer")
        else:
            if options.command is not None:
                raise StateStoreError("too many commands")
            options.command = argument
        index += 1
    return options


def print_snapshot(snapshot: StateSnapshot, as_json: bool) -> None:
    if as_json:
        print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(snapshot.aggregate_state.value)


def main(argv: list) -> int:
    try:
        options = parse(argv)
    except StateStoreError as exc:
        sys.stderr.write(f"{exc}\n")
        usage()
        return 2

    if options.command is None:
        usage()
        return 2

    store = StateStore()

    try:
        if options.command == "status":
            print_snapshot(store.read(), options.json)
        elif options.command == "clear":
            print_snapshot(store.clear(), options.json)
        elif options.command == CommandContract.quota_command_name:
            if options.app_server:
                snapshot = CodexAppServerQuotaCollector().fetch_and_update(store)
                print_snapshot(snapshot, options.json)
            elif options.stdin:
                input_data = sys.stdin.buffer.read()
                quota = QuotaExtractor.extract(input_data)
                if quota is None:
                    raise StateStoreError(
                        "quota --stdin requires JSON with five-hour and weekly remaining percent"
                    )
                snapshot = store.update_quota(
                    five_hour_percent=quota.five_hour_remaining_percent,
                    weekly_percent=quota.weekly_remaining_percent,
                    source="cli",
                )
                print_snapshot(snapshot, options.json)
            elif options.five_hour_percent is not None and options.weekly_percent is not None:
                snapshot = store.update_quota(
                    five_hour_percent=options.five_hour_percent,
                    weekly_percent=options.weekly_percent,
                    source="cli",
                )
                print_snapshot(snapshot, options.json)
            else:
                raise StateStoreError("quota requires --five-hour and --weekly")
        elif options.command == "quit":
            workspace = resolve_workspace(options.workspace)
            task_id = resolve_task_id(options.task_id, workspace)
            snapshot = store.update_task(
                task_id=task_id,
                state=LightState.quit,
                workspace=workspace,
                source="cli",
                hook_event_name=None,
                message="Codex traffic light: quit",
            )
            print_snapshot(snapshot, options.json)
        else:
            state = parse_state(options.command)
            workspace = resolve_workspace(options.workspace)
            task_id = resolve_task_id(options.task_id, workspace)
            snapshot = store.update_task(
                task_id=task_id,
                state=state,
                workspace=workspace,
                source="cli",
                hook_event_name=None,
                message=f"Codex traffic light: {state.value}",
            )
            print_snapshot(snapshot, options.json)
    except (StateStoreError, OSError) as exc:
        sys.stderr.write(f"{exc}\n")
        usage()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
