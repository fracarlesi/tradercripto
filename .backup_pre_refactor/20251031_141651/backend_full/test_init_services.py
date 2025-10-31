#!/usr/bin/env python3
import sys
import traceback

print("Testing service initialization...")
print("="*60)

try:
    from services.startup import initialize_services
    print("✓ Imported initialize_services")

    print("\nAttempting to initialize services...")
    initialize_services()
    print("✓ Services initialized successfully!")

    print("\nChecking scheduler status...")
    from services.scheduler import task_scheduler
    print(f"  Scheduler running: {task_scheduler.is_running()}")

    if task_scheduler.scheduler:
        jobs = task_scheduler.get_job_info()
        print(f"  Total jobs: {len(jobs)}")

        if jobs:
            print("\n  Scheduled jobs:")
            for job in jobs:
                print(f"    - {job['id']}: {job['func_name']}")
        else:
            print("  ⚠ No jobs scheduled!")

except Exception as e:
    print(f"\n✗ ERROR during initialization:")
    print(f"  {type(e).__name__}: {e}")
    print("\nFull traceback:")
    traceback.print_exc()
    sys.exit(1)
