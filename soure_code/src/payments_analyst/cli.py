"""Small CLI: generation works without any Supabase project or credentials."""

import argparse
from datetime import date
import json
import sys
from pathlib import Path
from uuid import UUID

from .data import DatasetConfig, generate
from .validation import validate


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Supabase-only synthetic payment data foundation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("preview", "seed", "analyse-preview"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--seed", type=int, default=42)
        command_parser.add_argument("--orders-per-day", type=int, default=320)
        command_parser.add_argument("--start-date", type=date.fromisoformat, default=date(2026, 9, 20))
        command_parser.add_argument("--scenario", choices=["incidents", "healthy"], default="incidents")
    subparsers.add_parser("migrate")
    subparsers.add_parser("status")
    subparsers.add_parser("configure-runtime")
    subparsers.add_parser("cache-check", help="Check Upstash using an expiring synthetic probe; no tokens printed.")
    revoke_parser = subparsers.add_parser("access-revoke", help="Disable an existing access assignment.")
    revoke_parser.add_argument("--user-id", type=UUID, required=True)
    subparsers.add_parser("access-merchants", help="List merchant IDs for future customer assignments.")
    access_parser = subparsers.add_parser("access-grant", help="Assign an existing Supabase user; owner-operated only.")
    access_parser.add_argument("--user-id", type=UUID, required=True)
    access_parser.add_argument("--role", choices=["pine_admin", "customer"], required=True)
    access_parser.add_argument("--customer-name")
    access_parser.add_argument("--dataset-id", type=UUID)
    access_parser.add_argument("--merchant-id", type=UUID, action="append", default=[])
    evaluation_parser = subparsers.add_parser("evaluate-queries", help="Run Gemini/Supabase known-answer checks and save evidence.")
    evaluation_parser.add_argument("--dataset-id", type=UUID, required=True)
    evaluation_parser.add_argument("--user-id", type=UUID, help="Assigned Pine Labs admin when RBAC is active.")
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--preview", action="store_true", help="Explicitly enable read-only synthetic previews.")
    serve_parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    try:
        if args.command == "cache-check":
            from .cache import get_cache
            result = get_cache().check()
        elif args.command == "access-revoke":
            from .access_admin import revoke_access
            result = revoke_access(args.user_id)
        elif args.command == "access-merchants":
            from .access_admin import list_merchants
            result = {"merchants": list_merchants()}
        elif args.command == "access-grant":
            from .access_admin import grant_access
            result = grant_access(args.user_id, args.role, customer_name=args.customer_name,
                                  dataset_id=args.dataset_id, merchant_ids=args.merchant_id)
        elif args.command == "evaluate-queries":
            from .query_evaluation import run_evaluation
            from .repository import SupabaseRepository
            from .cache import UpstashCache
            repository = SupabaseRepository(cache=UpstashCache()) # Benchmark must execute real queries, never cache hits.
            from .access import access_mode
            if access_mode() == "rbac" and (not args.user_id or (repository.access_profile(args.user_id) or {}).get("role") != "pine_admin"):
                raise ValueError("RBAC benchmarks require --user-id of an assigned Pine Labs admin.")
            def progress(case):
                print(f"{case['id']}: {case['status']} - {case.get('detail', '')}", flush=True)
            report = run_evaluation(args.dataset_id, repository, on_case=progress, user_id=args.user_id)
            saved = repository.save_query_evaluation(report, user_id=args.user_id)
            destination = Path(__file__).resolve().parents[2] / ".tmp" / "query-evaluation-latest.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(saved, indent=2), encoding="utf-8")
            print(json.dumps({"id": saved["id"], "status": report["status"], "passed": report["passed"],
                              "total": report["total"], "report_file": str(destination)}, indent=2))
            return 0 if report["status"] == "PASS" else 1
        elif args.command == "serve":
            import os
            import uvicorn
            if args.preview:
                os.environ["ENABLE_SYNTHETIC_PREVIEW"] = "true"
            uvicorn.run("payments_analyst.api:app", host="127.0.0.1", port=args.port)
            return 0
        elif args.command in {"preview", "seed", "analyse-preview"}:
            config = DatasetConfig(seed=args.seed, start_date=args.start_date,
                                   orders_per_day=args.orders_per_day, scenario=args.scenario)
            data = generate(config)
            validate(data)
            if args.command == "preview":
                result = data.summary()
            elif args.command == "analyse-preview":
                from .analytics import analyse
                from .contracts import AnalysisRequest
                from .preview import snapshot_from_generated
                report = analyse(snapshot_from_generated(data), AnalysisRequest(dataset_id=data.version["id"]))
                result = {"source": report["source"], "sql_executed": False,
                          "calculation_version": report["calculation_version"],
                          "segments": [{k: segment[k] for k in ("segment_id", "rank", "status",
                                       "gap_percentage_points", "net_impact_interval_paise")}
                                       for segment in report["segments"]], "caveats": report["caveats"]}
            else:
                from .database import import_dataset
                result = import_dataset(data)
        elif args.command == "migrate":
            from .database import migrate
            result = {"applied_migrations": migrate()}
        elif args.command == "configure-runtime":
            from .database import configure_runtime
            result = configure_runtime()
        else:
            from .database import database_status
            result = database_status()
        print(json.dumps(result, indent=2, default=str))
        return 0
    except (ValueError, ModuleNotFoundError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        # Provider exceptions may include connection details; do not print them.
        print(f"Database operation failed ({type(error).__name__}). Check your Supabase connection, "
              "permissions and migration state. Credentials were not logged.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
