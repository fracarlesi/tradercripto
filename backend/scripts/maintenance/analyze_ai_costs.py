#!/usr/bin/env python3
"""
AI Cost Analysis Script (T102)

Analyzes AI API usage and costs to:
- Query historical metrics
- Calculate monthly cost
- Compare to baseline
- Generate CSV report

Usage:
    python backend/scripts/maintenance/analyze_ai_costs.py [--days N] [--output FILE]

Options:
    --days N         Number of days to analyze (default: 30)
    --output FILE    Output CSV file path (default: ai_cost_report.csv)
    --baseline COST  Baseline monthly cost in USD for comparison (default: 1.00)

Example:
    # Analyze last 30 days and generate report
    python backend/scripts/maintenance/analyze_ai_costs.py

    # Analyze last 7 days with custom output
    python backend/scripts/maintenance/analyze_ai_costs.py --days 7 --output weekly_report.csv

    # Compare against $2.00 baseline
    python backend/scripts/maintenance/analyze_ai_costs.py --baseline 2.00
"""

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from services.ai_decision_service import get_decision_cache
from services.infrastructure.usage_tracker import get_usage_tracker
from services.news_feed import get_news_cache_stats


def analyze_current_usage() -> dict:
    """
    Analyze current day's AI usage.

    Returns:
        Dictionary with usage statistics
    """
    tracker = get_usage_tracker()
    usage_stats = tracker.get_usage_stats()
    prev_day_stats = tracker.get_previous_day_stats()

    # Get cache stats
    news_cache_stats = get_news_cache_stats()
    decision_cache = get_decision_cache()
    decision_cache_stats = decision_cache.get_cache_stats()

    return {
        "current_day": usage_stats,
        "previous_day": prev_day_stats,
        "news_cache": news_cache_stats,
        "decision_cache": decision_cache_stats,
    }


def calculate_monthly_projection(daily_cost: float, days_analyzed: int = 1) -> dict:
    """
    Calculate monthly cost projection.

    Args:
        daily_cost: Average daily cost
        days_analyzed: Number of days in analysis

    Returns:
        Dictionary with projections
    """
    # Project to 30 days
    monthly_projection = daily_cost * 30

    # Calculate yearly projection
    yearly_projection = monthly_projection * 12

    return {
        "daily_average": round(daily_cost, 6),
        "monthly_projection": round(monthly_projection, 4),
        "yearly_projection": round(yearly_projection, 2),
    }


def compare_to_baseline(actual_cost: float, baseline_cost: float) -> dict:
    """
    Compare actual cost to baseline.

    Args:
        actual_cost: Actual monthly cost
        baseline_cost: Baseline/target cost

    Returns:
        Dictionary with comparison metrics
    """
    difference = actual_cost - baseline_cost
    percentage = ((actual_cost - baseline_cost) / baseline_cost * 100) if baseline_cost > 0 else 0

    status = "UNDER BUDGET" if difference < 0 else "OVER BUDGET" if difference > 0 else "ON TARGET"

    return {
        "baseline_cost": round(baseline_cost, 4),
        "actual_cost": round(actual_cost, 4),
        "difference": round(difference, 4),
        "percentage": round(percentage, 2),
        "status": status,
    }


def calculate_cache_savings(cache_stats: dict, cost_per_call: float = 0.0003) -> dict:
    """
    Calculate cost savings from caching.

    Args:
        cache_stats: Cache statistics
        cost_per_call: Average cost per API call (default: $0.0003)

    Returns:
        Dictionary with savings metrics
    """
    cache_hits = cache_stats.get("hits", 0)
    cache_misses = cache_stats.get("misses", 0)
    total_requests = cache_hits + cache_misses

    # Calculate what would have been spent without cache
    cost_without_cache = total_requests * cost_per_call

    # Actual cost (only for cache misses)
    actual_cost = cache_misses * cost_per_call

    # Savings
    savings = cost_without_cache - actual_cost

    # Savings percentage
    savings_percentage = (savings / cost_without_cache * 100) if cost_without_cache > 0 else 0

    return {
        "total_requests": total_requests,
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "hit_rate": round(cache_stats.get("hit_rate", 0), 2),
        "cost_without_cache": round(cost_without_cache, 6),
        "actual_cost": round(actual_cost, 6),
        "savings": round(savings, 6),
        "savings_percentage": round(savings_percentage, 2),
    }


def generate_csv_report(analysis_data: dict, output_file: str) -> None:
    """
    Generate CSV report from analysis data.

    Args:
        analysis_data: Dictionary with analysis results
        output_file: Path to output CSV file
    """
    with open(output_file, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)

        # Header
        writer.writerow(["AI Cost Analysis Report"])
        writer.writerow(["Generated:", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
        writer.writerow([])

        # Current Usage
        writer.writerow(["Current Day Usage"])
        writer.writerow(["Metric", "Value"])
        current = analysis_data["current_day"]
        writer.writerow(["Date", current["date"]])
        writer.writerow(["API Calls", current["calls_today"]])
        writer.writerow(["Input Tokens", current["input_tokens_today"]])
        writer.writerow(["Output Tokens", current["output_tokens_today"]])
        writer.writerow(["Total Tokens", current["total_tokens_today"]])
        writer.writerow(["Daily Cost (USD)", f"${current['daily_cost']:.6f}"])
        writer.writerow([])

        # Monthly Projection
        writer.writerow(["Monthly Projection"])
        writer.writerow(["Metric", "Value"])
        projection = analysis_data["projection"]
        writer.writerow(["Daily Average Cost", f"${projection['daily_average']:.6f}"])
        writer.writerow(["Monthly Projection", f"${projection['monthly_projection']:.4f}"])
        writer.writerow(["Yearly Projection", f"${projection['yearly_projection']:.2f}"])
        writer.writerow([])

        # Baseline Comparison
        writer.writerow(["Baseline Comparison"])
        writer.writerow(["Metric", "Value"])
        comparison = analysis_data["comparison"]
        writer.writerow(["Baseline Cost", f"${comparison['baseline_cost']:.4f}"])
        writer.writerow(["Actual Cost", f"${comparison['actual_cost']:.4f}"])
        writer.writerow(["Difference", f"${comparison['difference']:.4f}"])
        writer.writerow(["Percentage", f"{comparison['percentage']:.2f}%"])
        writer.writerow(["Status", comparison["status"]])
        writer.writerow([])

        # Cache Savings - News
        writer.writerow(["News Cache Savings"])
        writer.writerow(["Metric", "Value"])
        news_savings = analysis_data["news_cache_savings"]
        writer.writerow(["Total Requests", news_savings["total_requests"]])
        writer.writerow(["Cache Hits", news_savings["cache_hits"]])
        writer.writerow(["Cache Misses", news_savings["cache_misses"]])
        writer.writerow(["Hit Rate", f"{news_savings['hit_rate']:.2f}%"])
        writer.writerow(["Cost Without Cache", f"${news_savings['cost_without_cache']:.6f}"])
        writer.writerow(["Actual Cost", f"${news_savings['actual_cost']:.6f}"])
        writer.writerow(["Savings", f"${news_savings['savings']:.6f}"])
        writer.writerow(["Savings %", f"{news_savings['savings_percentage']:.2f}%"])
        writer.writerow([])

        # Cache Savings - Decisions
        writer.writerow(["Decision Cache Savings"])
        writer.writerow(["Metric", "Value"])
        decision_savings = analysis_data["decision_cache_savings"]
        writer.writerow(["Total Requests", decision_savings["total_requests"]])
        writer.writerow(["Cache Hits", decision_savings["cache_hits"]])
        writer.writerow(["Cache Misses", decision_savings["cache_misses"]])
        writer.writerow(["Hit Rate", f"{decision_savings['hit_rate']:.2f}%"])
        writer.writerow(["Cost Without Cache", f"${decision_savings['cost_without_cache']:.6f}"])
        writer.writerow(["Actual Cost", f"${decision_savings['actual_cost']:.6f}"])
        writer.writerow(["Savings", f"${decision_savings['savings']:.6f}"])
        writer.writerow(["Savings %", f"{decision_savings['savings_percentage']:.2f}%"])
        writer.writerow([])

        # Recommendations
        writer.writerow(["Recommendations"])
        for rec in analysis_data["recommendations"]:
            writer.writerow([rec])

    print(f"Report generated: {output_file}")


def generate_recommendations(analysis_data: dict) -> list:
    """
    Generate cost optimization recommendations.

    Args:
        analysis_data: Analysis results

    Returns:
        List of recommendation strings
    """
    recommendations = []
    current = analysis_data["current_day"]
    comparison = analysis_data["comparison"]
    news_savings = analysis_data["news_cache_savings"]
    decision_savings = analysis_data["decision_cache_savings"]

    # Cost status
    if comparison["status"] == "OVER BUDGET":
        recommendations.append(
            f"⚠️ Current monthly projection (${comparison['actual_cost']:.2f}) exceeds baseline "
            f"(${comparison['baseline_cost']:.2f}) by {abs(comparison['percentage']):.1f}%"
        )
    elif comparison["status"] == "UNDER BUDGET":
        recommendations.append(
            f"✅ Current costs are {abs(comparison['percentage']):.1f}% under budget - good work!"
        )

    # Cache performance
    if news_savings["hit_rate"] < 80:
        recommendations.append(
            f"💡 News cache hit rate is {news_savings['hit_rate']:.1f}%. "
            f"Consider increasing TTL to improve savings."
        )
    else:
        recommendations.append(
            f"✅ News cache performing well with {news_savings['hit_rate']:.1f}% hit rate, "
            f"saving ${news_savings['savings']:.4f}/day."
        )

    if decision_savings["hit_rate"] < 15:
        recommendations.append(
            f"💡 Decision cache hit rate is {decision_savings['hit_rate']:.1f}%. "
            f"Consider increasing cache window to 15-20 minutes."
        )
    else:
        recommendations.append(
            f"✅ Decision cache is effective with {decision_savings['hit_rate']:.1f}% hit rate."
        )

    # Token usage
    avg_tokens_per_call = (
        current["total_tokens_today"] / current["calls_today"] if current["calls_today"] > 0 else 0
    )
    if avg_tokens_per_call > 2000:
        recommendations.append(
            f"💡 Average {avg_tokens_per_call:.0f} tokens/call is high. Review prompt optimization."
        )

    return recommendations


def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(
        description="Analyze AI API usage and costs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--days", type=int, default=30, help="Number of days to analyze (default: 30)"
    )
    parser.add_argument(
        "--output",
        type=str,
        default="ai_cost_report.csv",
        help="Output CSV file path (default: ai_cost_report.csv)",
    )
    parser.add_argument(
        "--baseline", type=float, default=1.00, help="Baseline monthly cost in USD (default: 1.00)"
    )

    args = parser.parse_args()

    print("=" * 80)
    print("AI Cost Analysis Script (T102)")
    print("=" * 80)
    print()

    # Analyze current usage
    print("Analyzing current usage...")
    usage_data = analyze_current_usage()

    # Calculate projections
    daily_cost = usage_data["current_day"]["daily_cost"]
    projection = calculate_monthly_projection(daily_cost)

    # Compare to baseline
    comparison = compare_to_baseline(projection["monthly_projection"], args.baseline)

    # Calculate cache savings
    news_cache_savings = calculate_cache_savings(
        usage_data["news_cache"],
        cost_per_call=0.0001,  # Approximate cost for news fetch
    )
    decision_cache_savings = calculate_cache_savings(
        usage_data["decision_cache"],
        cost_per_call=0.0003,  # Approximate cost for AI decision
    )

    # Compile analysis
    analysis_data = {
        "current_day": usage_data["current_day"],
        "previous_day": usage_data["previous_day"],
        "projection": projection,
        "comparison": comparison,
        "news_cache_savings": news_cache_savings,
        "decision_cache_savings": decision_cache_savings,
        "recommendations": [],
    }

    # Generate recommendations
    analysis_data["recommendations"] = generate_recommendations(analysis_data)

    # Print summary
    print()
    print("Summary:")
    print(f"  Current Date: {analysis_data['current_day']['date']}")
    print(f"  Daily Cost: ${daily_cost:.6f}")
    print(f"  Monthly Projection: ${projection['monthly_projection']:.4f}")
    print(f"  Baseline: ${args.baseline:.2f}")
    print(f"  Status: {comparison['status']}")
    print()
    print("Cache Performance:")
    print(f"  News Cache Hit Rate: {news_cache_savings['hit_rate']:.1f}%")
    print(f"  News Cache Savings: ${news_cache_savings['savings']:.6f}/day")
    print(f"  Decision Cache Hit Rate: {decision_cache_savings['hit_rate']:.1f}%")
    print(f"  Decision Cache Savings: ${decision_cache_savings['savings']:.6f}/day")
    print()
    print("Recommendations:")
    for rec in analysis_data["recommendations"]:
        print(f"  {rec}")
    print()

    # Generate CSV report
    generate_csv_report(analysis_data, args.output)

    print()
    print("=" * 80)
    print("Analysis complete!")
    print("=" * 80)


if __name__ == "__main__":
    main()
