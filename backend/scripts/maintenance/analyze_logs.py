#!/usr/bin/env python3
"""Log analysis script for Bitcoin Trading System (T144).

This script parses JSON structured logs and generates analysis reports:
- Error rates by service
- Slow operations (>1s)
- Summary statistics
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from tabulate import tabulate


class LogAnalyzer:
    """Analyze structured JSON logs."""

    def __init__(self, log_file: Path):
        """Initialize analyzer with log file path.

        Args:
            log_file: Path to JSON log file
        """
        self.log_file = log_file
        self.logs: list[dict[str, Any]] = []
        self.errors_by_service: dict[str, int] = defaultdict(int)
        self.errors_by_operation: dict[str, int] = defaultdict(int)
        self.slow_operations: list[dict[str, Any]] = []
        self.total_logs = 0
        self.total_errors = 0

    def parse_logs(self) -> None:
        """Parse JSON logs from file."""
        print(f"Reading logs from: {self.log_file}")

        with open(self.log_file) as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue

                try:
                    log_entry = json.loads(line)
                    self.logs.append(log_entry)
                    self.total_logs += 1

                    # Count errors by service
                    if log_entry.get("level") in ["ERROR", "CRITICAL"]:
                        self.total_errors += 1
                        service = log_entry.get("service", "unknown")
                        self.errors_by_service[service] += 1

                        # Count errors by operation if available
                        operation = log_entry.get("operation", "unknown")
                        self.errors_by_operation[operation] += 1

                    # Identify slow operations (>1000ms)
                    duration_ms = log_entry.get("duration_ms")
                    if duration_ms and duration_ms > 1000:
                        self.slow_operations.append(
                            {
                                "timestamp": log_entry.get("timestamp"),
                                "service": log_entry.get("service", "unknown"),
                                "operation": log_entry.get("operation", "unknown"),
                                "duration_ms": duration_ms,
                                "message": log_entry.get("message", ""),
                            }
                        )

                except json.JSONDecodeError as e:
                    print(f"Warning: Failed to parse line {line_num}: {e}")
                    continue
                except Exception as e:
                    print(f"Warning: Error processing line {line_num}: {e}")
                    continue

        print(f"Parsed {self.total_logs} log entries")

    def calculate_error_rates(self) -> dict[str, float]:
        """Calculate error rate percentage by service.

        Returns:
            Dict mapping service name to error rate percentage
        """
        error_rates: dict[str, float] = {}

        # Count total logs per service
        service_totals: dict[str, int] = defaultdict(int)
        for log in self.logs:
            service = log.get("service", "unknown")
            service_totals[service] += 1

        # Calculate error rate
        for service, error_count in self.errors_by_service.items():
            total = service_totals.get(service, 0)
            error_rate = (error_count / total * 100) if total > 0 else 0
            error_rates[service] = error_rate

        return error_rates

    def get_time_range(self) -> tuple[str, str]:
        """Get time range of logs.

        Returns:
            Tuple of (earliest_timestamp, latest_timestamp)
        """
        if not self.logs:
            return ("N/A", "N/A")

        timestamps = [log.get("timestamp") for log in self.logs if log.get("timestamp")]
        if not timestamps:
            return ("N/A", "N/A")

        return (min(timestamps), max(timestamps))

    def generate_report(self) -> None:
        """Generate and print analysis report."""
        print("\n" + "=" * 80)
        print("LOG ANALYSIS REPORT")
        print("=" * 80)

        # Time range
        start_time, end_time = self.get_time_range()
        print(f"\nTime Range: {start_time} to {end_time}")

        # Summary statistics
        print("\n" + "-" * 80)
        print("SUMMARY STATISTICS")
        print("-" * 80)
        print(f"Total log entries:       {self.total_logs:,}")
        print(f"Total errors:            {self.total_errors:,}")
        overall_error_rate = (
            (self.total_errors / self.total_logs * 100) if self.total_logs > 0 else 0
        )
        print(f"Overall error rate:      {overall_error_rate:.2f}%")
        print(f"Slow operations (>1s):   {len(self.slow_operations):,}")

        # Error rates by service
        print("\n" + "-" * 80)
        print("ERROR RATES BY SERVICE")
        print("-" * 80)
        error_rates = self.calculate_error_rates()
        if error_rates:
            service_table = []
            for service in sorted(error_rates.keys(), key=lambda s: error_rates[s], reverse=True):
                error_count = self.errors_by_service[service]
                error_rate = error_rates[service]
                service_table.append([service, error_count, f"{error_rate:.2f}%"])

            print(
                tabulate(
                    service_table, headers=["Service", "Errors", "Error Rate"], tablefmt="grid"
                )
            )
        else:
            print("No errors found in logs.")

        # Errors by operation
        print("\n" + "-" * 80)
        print("TOP 10 FAILING OPERATIONS")
        print("-" * 80)
        if self.errors_by_operation:
            operation_table = []
            sorted_operations = sorted(
                self.errors_by_operation.items(), key=lambda x: x[1], reverse=True
            )[:10]
            for operation, count in sorted_operations:
                operation_table.append([operation, count])

            print(tabulate(operation_table, headers=["Operation", "Error Count"], tablefmt="grid"))
        else:
            print("No operation errors found.")

        # Slow operations
        print("\n" + "-" * 80)
        print(f"SLOW OPERATIONS (>{1000}ms)")
        print("-" * 80)
        if self.slow_operations:
            # Sort by duration descending
            sorted_slow = sorted(
                self.slow_operations, key=lambda x: x["duration_ms"], reverse=True
            )[:20]  # Top 20 slowest

            slow_table = []
            for op in sorted_slow:
                slow_table.append(
                    [
                        op["timestamp"][:19] if op["timestamp"] else "N/A",  # Trim microseconds
                        op["service"][:30],  # Truncate long service names
                        op["operation"][:40],  # Truncate long operation names
                        f"{op['duration_ms']:.0f}",
                    ]
                )

            print(
                tabulate(
                    slow_table,
                    headers=["Timestamp", "Service", "Operation", "Duration (ms)"],
                    tablefmt="grid",
                )
            )
        else:
            print("No slow operations found.")

        print("\n" + "=" * 80)
        print("END OF REPORT")
        print("=" * 80)

    def export_csv(self, output_file: Path) -> None:
        """Export analysis results to CSV.

        Args:
            output_file: Path to output CSV file
        """
        import csv

        print(f"\nExporting results to: {output_file}")

        with open(output_file, "w", newline="") as csvfile:
            writer = csv.writer(csvfile)

            # Summary section
            writer.writerow(["SUMMARY STATISTICS"])
            writer.writerow(["Metric", "Value"])
            writer.writerow(["Total log entries", self.total_logs])
            writer.writerow(["Total errors", self.total_errors])
            overall_error_rate = (
                (self.total_errors / self.total_logs * 100) if self.total_logs > 0 else 0
            )
            writer.writerow(["Overall error rate", f"{overall_error_rate:.2f}%"])
            writer.writerow(["Slow operations (>1s)", len(self.slow_operations)])
            writer.writerow([])

            # Errors by service
            writer.writerow(["ERROR RATES BY SERVICE"])
            writer.writerow(["Service", "Errors", "Error Rate"])
            error_rates = self.calculate_error_rates()
            for service in sorted(error_rates.keys(), key=lambda s: error_rates[s], reverse=True):
                error_count = self.errors_by_service[service]
                error_rate = error_rates[service]
                writer.writerow([service, error_count, f"{error_rate:.2f}%"])
            writer.writerow([])

            # Slow operations
            writer.writerow(["SLOW OPERATIONS"])
            writer.writerow(["Timestamp", "Service", "Operation", "Duration (ms)", "Message"])
            sorted_slow = sorted(self.slow_operations, key=lambda x: x["duration_ms"], reverse=True)
            for op in sorted_slow:
                writer.writerow(
                    [
                        op["timestamp"],
                        op["service"],
                        op["operation"],
                        f"{op['duration_ms']:.0f}",
                        op["message"][:100],  # Truncate long messages
                    ]
                )

        print(f"Exported {len(self.slow_operations) + len(self.errors_by_service) + 4} rows to CSV")


def main():
    """Main entry point for log analyzer."""
    parser = argparse.ArgumentParser(
        description="Analyze structured JSON logs from trading system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze logs from Docker container
  docker logs trader_app 2>&1 | grep '^{' > app.log
  python analyze_logs.py app.log

  # Analyze logs with CSV export
  python analyze_logs.py app.log --export report.csv

  # Analyze recent logs only (last 1000 lines)
  tail -1000 app.log | python analyze_logs.py - --export recent_report.csv
        """,
    )

    parser.add_argument(
        "log_file",
        type=str,
        help="Path to JSON log file (use '-' to read from stdin)",
    )
    parser.add_argument(
        "--export",
        "-e",
        type=str,
        metavar="FILE",
        help="Export analysis to CSV file",
    )

    args = parser.parse_args()

    # Handle stdin input
    if args.log_file == "-":
        import tempfile

        temp_log = tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".log")
        print("Reading logs from stdin...")
        for line in sys.stdin:
            temp_log.write(line)
        temp_log.close()
        log_file = Path(temp_log.name)
    else:
        log_file = Path(args.log_file)

    if not log_file.exists():
        print(f"Error: Log file not found: {log_file}")
        sys.exit(1)

    # Analyze logs
    analyzer = LogAnalyzer(log_file)
    analyzer.parse_logs()
    analyzer.generate_report()

    # Export CSV if requested
    if args.export:
        output_file = Path(args.export)
        analyzer.export_csv(output_file)

    # Cleanup temp file if stdin was used
    if args.log_file == "-":
        log_file.unlink()


if __name__ == "__main__":
    main()
