"""JSON-only command line; no credentials, interactive prompts, or AI calls."""
import argparse
import json
from pathlib import Path
import signal
import sys

from .backup import backup, verify_backup
from .runner import atomic_json, collect, interrupt_on_signal, state_lock
from .store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m collector")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--registry", required=True)
    run.add_argument("--state", required=True)
    run.add_argument("--resume")
    run.add_argument("--resume-auto", action="store_true")
    run.add_argument("--source-id", dest="source_ids", action="append", help="Refresh only these enabled sources; retain the full registry. Repeat for multiple sources.")
    for name, default in [("max-items", 8), ("max-total-items", 100), ("max-pages", 2), ("max-bytes", 25_000_000),
                          ("max-artifact-bytes", 50_000_000), ("max-seconds", 600), ("max-requests", 100),
                          ("source-max-bytes", 5_000_000), ("source-max-seconds", 90), ("overlap-days", 14), ("initial-days", 30)]:
        run.add_argument("--" + name, type=int, default=default)
    for command in ["status", "export", "search", "annotate", "backup"]:
        p = sub.add_parser(command)
        p.add_argument("--state", required=True)
        if command in {"export", "backup"}:
            p.add_argument("--output", required=command == "backup")
        if command == "search":
            p.add_argument("query")
            p.add_argument("--limit", type=int, default=20)
        if command == "annotate":
            p.add_argument("--item", required=True)
            p.add_argument("--notes")
            p.add_argument("--tags", help="JSON list of manual tags; omitted preserves current tags")
            p.add_argument("--review-status", choices=["included", "needs_review", "excluded"])
    verify = sub.add_parser("verify-backup")
    verify.add_argument("--path", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            signal.signal(signal.SIGTERM, interrupt_on_signal)
            options = vars(args).copy()
            options.pop("command")
            registry = json.loads(Path(options.pop("registry")).read_text())
            result = collect(registry, **options)
            code = 0 if result["status"] == "ok" else 2
        elif args.command == "backup":
            result, code = backup(args.state, args.output), 0
        elif args.command == "verify-backup":
            result, code = verify_backup(args.path), 0
        else:
            if not (Path(args.state) / "library.sqlite3").is_file():
                raise ValueError("Collector state does not exist")
            with state_lock(args.state):
                store = Store(args.state)
                try:
                    if args.command == "status":
                        result = store.status()
                    elif args.command == "export":
                        result = store.export()
                        if args.output:
                            atomic_json(args.output, result)
                            result = {"schema_version": 1, "status": "ok", "output": str(Path(args.output).resolve()), "items": len(result["items"])}
                    elif args.command == "search":
                        result = {"schema_version": 1, "query": args.query, "items": store.search(args.query, args.limit)}
                    else:
                        tags = json.loads(args.tags) if args.tags is not None else None
                        if tags is not None and (not isinstance(tags, list) or not all(isinstance(t, str) for t in tags)):
                            raise ValueError("--tags must be a JSON string list")
                        store.annotate(args.item, notes=args.notes, tags=tags, review_status=args.review_status)
                        result = {"schema_version": 1, "status": "ok", "item_id": args.item}
                    code = 0
                finally:
                    store.db.close()
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return code
    except KeyboardInterrupt as exc:
        print(json.dumps({"schema_version": 1, "status": "interrupted", "error": str(exc), "resume": "run --resume-auto"}))
        return 130
    except Exception as exc:
        print(json.dumps({"schema_version": 1, "status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
