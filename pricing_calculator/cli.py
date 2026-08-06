import os
import sys
import argparse
import logging
import pandas as pd
from tabulate import tabulate

from .engine import PricingEngine
from .calculators import run_calculation

logger = logging.getLogger(__name__)

def main():
    # Setup basic logging to stdout/stderr
    logging.basicConfig(
        level=logging.WARNING,  # Default level for dependencies
        format='%(levelname)s: %(message)s'
    )
    # Customize our modules to show INFO logs for tracking download/preprocessing
    logging.getLogger('pricing_calculator').setLevel(logging.INFO)

    parser = argparse.ArgumentParser(description="AWS Cloud Cost Pricing Calculator")
    parser.add_argument("input_file", help="Path to the input CSV or Excel file")
    parser.add_argument("--output", help="Path to save the output CSV or Excel file (optional)")
    parser.add_argument("--region", default="ap-southeast-3", help="Default region to use (default: ap-southeast-3)")
    parser.add_argument("--architecture", choices=["x86_64", "arm64", "any"], default="x86_64",
                        help="Preferred architecture for custom specs matching (default: x86_64)")
    parser.add_argument("--cache-dir", default=".cache", help="Cache directory (default: .cache)")
    parser.add_argument("--clear-cache", action="store_true", help="Delete cached raw JSON sheets before running")
    
    args = parser.parse_args()
    
    if args.clear_cache:
        logger.info("Clearing all cached pricing files...")
        if os.path.exists(args.cache_dir):
            for filename in os.listdir(args.cache_dir):
                if filename.endswith(".json"):
                    try:
                        os.remove(os.path.join(args.cache_dir, filename))
                    except Exception as e:
                        logger.error(f"Error removing cached file {filename}: {e}")
                 
    # Initialize Engine
    engine = PricingEngine(cache_dir=args.cache_dir, region=args.region, pref_arch=args.architecture)
    engine.initialize_azure_prices()
    
    # Run calculation
    try:
        results_df = run_calculation(args.input_file, engine, default_region=args.region, pref_arch=args.architecture)
    except Exception as e:
        logger.exception(f"Error executing cost calculations: {e}")
        sys.exit(1)
        
    # Output to stdout summary
    print("\n" + "="*80)
    print(" AWS CLOUD COST ESTIMATE SUMMARY ".center(80, "#"))
    print("="*80)
    
    # Get unique publication dates for display
    pub_dates_str = []
    for key, date in engine.pub_dates.items():
        if date:
            short_date = date.split('T')[0]
            parts = key.split('_')
            svc = parts[0].upper()
            reg = parts[1] if len(parts) > 1 else ""
            pub_dates_str.append(f"{svc} ({reg}): {short_date}" if reg else f"{svc}: {short_date}")
            
    if pub_dates_str:
        print(f"Pricing Database Version: {', '.join(pub_dates_str)}")
        print("="*80)
        
    console_cols = [
        'id', 'service', 'matched_type', 'requested_vcpu', 'requested_memory_gb', 
        'matched_vcpu', 'matched_memory_gb', 'quantity', 'monthly_price', 'notes'
    ]
    # Filter out metadata row from display if it exists (for console)
    display_df = results_df[results_df['service'] != 'metadata'].copy()
    display_df = display_df[console_cols]
    display_df['monthly_price'] = display_df['monthly_price'].map(lambda x: f"${x:,.2f}")
    
    # Rename for a clean tabulate representation on the screen
    display_df.rename(columns={
        'requested_vcpu': 'req_vcpu',
        'requested_memory_gb': 'req_mem',
        'matched_vcpu': 'fit_vcpu',
        'matched_memory_gb': 'fit_mem'
    }, inplace=True)
    
    print(tabulate(display_df, headers='keys', tablefmt='pretty', showindex=False))
    
    total_monthly = results_df['monthly_price'].sum()
    print(f"\nTotal Monthly Estimated Cost: ${total_monthly:,.2f} USD")
    print("="*80)
    
    # Append total cost row and metadata row at the end of the saved dataframe so they appear in Excel/CSV
    rows_to_append = []
    
    total_row = {
        'id': 'TOTAL_ESTIMATED_MONTHLY_COST',
        'service': 'total',
        'region': '',
        'type': '',
        'matched_type': '',
        'requested_vcpu': 0.0,
        'requested_memory_gb': 0.0,
        'matched_vcpu': 0,
        'matched_memory_gb': 0.0,
        'os_or_engine': '',
        'size_gb': 0.0,
        'quantity': 0,
        'hours_per_month': 0,
        'unit_price': 0.0,
        'monthly_price': total_monthly,
        'currency': 'USD',
        'notes': f"Total Monthly Estimated Cost: ${total_monthly:,.2f} USD",
        'description': 'Sum of all monthly cost estimate lines.'
    }
    rows_to_append.append(total_row)
    
    if pub_dates_str:
        meta_row = {
            'id': 'AWS_PRICING_DATABASE_VERSION',
            'service': 'metadata',
            'region': '',
            'type': '',
            'matched_type': '',
            'requested_vcpu': 0.0,
            'requested_memory_gb': 0.0,
            'matched_vcpu': 0,
            'matched_memory_gb': 0.0,
            'os_or_engine': '',
            'size_gb': 0.0,
            'quantity': 0,
            'hours_per_month': 0,
            'unit_price': 0.0,
            'monthly_price': 0.0,
            'currency': 'USD',
            'notes': f"AWS price databases publication dates: {', '.join(pub_dates_str)}",
            'description': 'This estimate was generated using AWS pricing datasets published on these dates.'
        }
        rows_to_append.append(meta_row)
        
    results_df = pd.concat([results_df, pd.DataFrame(rows_to_append)], ignore_index=True)
    
    # Save output if specified
    out_file = args.output
    if not out_file:
        base, ext = os.path.splitext(args.input_file)
        out_file = f"{base}_cost_estimate{ext}"
        
    print(f"Saving detailed report to {out_file}...")
    if out_file.endswith('.xlsx') or out_file.endswith('.xls'):
        results_df.to_excel(out_file, index=False)
    else:
        results_df.to_csv(out_file, index=False)
        
    print("Done!")

if __name__ == "__main__":
    main()
