#!/usr/bin/env python3
import sys
from services.scheduler import task_scheduler

print("Checking scheduler status...")
print(f"Scheduler running: {task_scheduler.is_running()}")
print()

if task_scheduler.scheduler:
    jobs = task_scheduler.get_job_info()
    print(f"Total jobs: {len(jobs)}")
    print()

    if jobs:
        print("Scheduled jobs:")
        for job in jobs:
            print(f"  - ID: {job['id']}")
            print(f"    Function: {job['func_name']}")
            print(f"    Next run: {job['next_run_time']}")
            print()
    else:
        print("No jobs scheduled!")
else:
    print("Scheduler not initialized!")
