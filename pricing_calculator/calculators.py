import os
import re
import logging
import pandas as pd
from typing import Union

from .engine import PricingEngine
from .utils import normalize_os_or_engine, round_fargate_specs, parse_memory
from .config import PRICING_DEFAULTS

logger = logging.getLogger(__name__)

def run_calculation(input_file: str, engine: PricingEngine, default_region: str = "ap-southeast-3", pref_arch: str = "x86_64") -> pd.DataFrame:
    """Parse input file, calculate costs, and return detailed results DataFrame."""
    # Input validation
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"Input file not found: {input_file}")
    
    if not input_file.endswith(('.csv', '.xlsx', '.xls')):
        raise ValueError(f"Unsupported file format: {input_file}. Supported formats: .csv, .xlsx, .xls")
        
    # Load and normalize columns
    if input_file.endswith('.xlsx') or input_file.endswith('.xls'):
        df = pd.read_excel(input_file)
    else:
        df = pd.read_csv(input_file)
        
    df.columns = [c.strip() for c in df.columns]
    
    # Check if VM spec format (e.g. from VMware)
    if 'VM' in df.columns and 'CPUs' in df.columns and 'Memory' in df.columns:
        logger.info("Detected VM specification format. Generating detailed EC2 & EBS estimate...")
        rows = []
        for idx, row in df.iterrows():
            vm_name = str(row['VM']).strip()
            cpus = int(str(row['CPUs']).replace(',', ''))
            
            # parse memory (e.g., '4,096' MB or '8' GB)
            mem_str = str(row['Memory']).replace(',', '').lower()
            mem_num = float(re.search(r'([0-9.]+)', mem_str).group(1))
            memory_gb = mem_num / 1024.0 if mem_num >= 128 else mem_num
            
            # parse storage (Provisioned MiB or size columns)
            storage_gb = 0.0
            if 'Provisioned MiB' in df.columns:
                store_str = str(row['Provisioned MiB']).replace(',', '')
                store_num = float(re.search(r'([0-9.]+)', store_str).group(1))
                storage_gb = store_num / 1024.0
            
            # Add EC2 row
            rows.append({
                'id': f"{vm_name}-compute",
                'service': 'ec2',
                'region': default_region,
                'type': 'custom',
                'vcpu': float(cpus),
                'memory_gb': memory_gb,
                'os_or_engine': 'Linux',
                'size_gb': 0.0,
                'quantity': 1,
                'hours_per_month': 730,
                'description': f"EC2 Instance matching {cpus} vCPU, {memory_gb:.1f} GB RAM for VM {vm_name}"
            })
            
            # Add EBS row if storage exists
            if storage_gb > 0:
                rows.append({
                    'id': f"{vm_name}-storage",
                    'service': 'ebs',
                    'region': default_region,
                    'type': 'gp3',
                    'vcpu': 0.0,
                    'memory_gb': 0.0,
                    'os_or_engine': '',
                    'size_gb': storage_gb,
                    'quantity': 1,
                    'hours_per_month': 730,
                    'description': f"gp3 Volume ({storage_gb:.1f} GB) for VM {vm_name}"
                })
        calc_df = pd.DataFrame(rows)
    else:
        # Standard input template structure
        required_cols = ['id', 'service']
        for col in required_cols:
            if col not in df.columns:
                raise ValueError(f"Required column '{col}' is missing from input file.")
                
        # Fill optional columns with defaults
        calc_df = df.copy()
        if 'region' not in calc_df.columns: calc_df['region'] = default_region
        if 'type' not in calc_df.columns: calc_df['type'] = 'custom'
        if 'vcpu' not in calc_df.columns: calc_df['vcpu'] = 0.0
        if 'memory_gb' not in calc_df.columns: calc_df['memory_gb'] = 0.0
        if 'os_or_engine' not in calc_df.columns: calc_df['os_or_engine'] = 'Linux'
        if 'size_gb' not in calc_df.columns: calc_df['size_gb'] = 0.0
        if 'quantity' not in calc_df.columns: calc_df['quantity'] = 1
        if 'hours_per_month' not in calc_df.columns: calc_df['hours_per_month'] = 730
        if 'description' not in calc_df.columns: calc_df['description'] = ''
        
        # Fill standard NaN values
        calc_df['region'] = calc_df['region'].fillna(default_region)
        calc_df['type'] = calc_df['type'].fillna('custom')
        calc_df['vcpu'] = calc_df['vcpu'].fillna(0.0).astype(float)
        calc_df['memory_gb'] = calc_df['memory_gb'].apply(parse_memory)
        calc_df['os_or_engine'] = calc_df['os_or_engine'].fillna('Linux')
        calc_df['size_gb'] = calc_df['size_gb'].fillna(0.0).astype(float)
        calc_df['quantity'] = calc_df['quantity'].fillna(1).astype(int)
        calc_df['hours_per_month'] = calc_df['hours_per_month'].fillna(730).astype(int)
        calc_df['description'] = calc_df['description'].fillna('')

    # Perform calculations row-by-row
    results = []
    
    # Group by region to initialize caches sequentially
    active_regions = calc_df['region'].unique()
    for reg in active_regions:
        engine.initialize_region(reg)
        
    for idx, row in calc_df.iterrows():
        id_val = row['id']
        service = str(row['service']).lower().strip()
        region = str(row['region']).strip()
        res_type = str(row['type']).strip()
        vcpu = float(row['vcpu']) if 'vcpu' in row and pd.notna(row['vcpu']) else 0.0
        memory_gb = float(row['memory_gb']) if 'memory_gb' in row and pd.notna(row['memory_gb']) else 0.0
        os_or_engine_raw = str(row['os_or_engine']).strip() if 'os_or_engine' in row else ''
        os_or_engine = normalize_os_or_engine(service, os_or_engine_raw)
        size_gb = float(row['size_gb']) if 'size_gb' in row else 0.0
        qty = int(row['quantity']) if 'quantity' in row else 1
        hours = int(row['hours_per_month']) if 'hours_per_month' in row else 730
        desc = str(row['description'])
        
        matched_type = res_type
        requested_vcpu = vcpu
        requested_memory_gb = memory_gb
        matched_vcpu = 0
        matched_memory_gb = 0.0
        unit_price = 0.0
        monthly_price = 0.0
        calc_note = ""
        
        if service == 'ec2':
            # Custom instance spec resolution
            if res_type.lower() == 'custom' or not res_type:
                unit_price, resolved_type, m_vcpu, m_mem = engine.resolve_custom_ec2(
                    region, vcpu, memory_gb, os_or_engine, pref_arch
                )
                matched_type = resolved_type
                matched_vcpu = m_vcpu
                matched_memory_gb = m_mem
                calc_note = f"Cheapest {pref_arch} matching >= {vcpu} vCPU, {memory_gb:.1f} GB RAM ({os_or_engine})"
            else:
                unit_price, _, m_vcpu, m_mem = engine.get_ec2_price(region, res_type, os_or_engine)
                matched_vcpu = m_vcpu
                matched_memory_gb = m_mem
                calc_note = f"Explicit instance type {res_type} ({os_or_engine})"
                
            monthly_price = unit_price * hours * qty
            
            # If storage size is specified on the EC2 line, add default gp3 EBS storage cost
            if size_gb > 0:
                storage_rate = engine.get_ebs_price(region, "gp3")
                storage_cost = storage_rate * size_gb * qty
                monthly_price += storage_cost
                calc_note += f" + gp3 storage ({size_gb:.1f} GB @ ${storage_rate}/GB-mo)"
            
        elif service == 'ebs':
            ebs_type = res_type if res_type and res_type.lower() != 'custom' else 'gp3'
            unit_price = engine.get_ebs_price(region, ebs_type)
            # EBS pricing is per GB-month
            monthly_price = unit_price * size_gb * qty
            calc_note = f"EBS Volume Storage ({ebs_type})"
            
        elif service == 'rds':
            # Database instance pricing
            unit_price, m_vcpu, m_mem = engine.get_rds_price(region, res_type, os_or_engine, "Single-AZ")
            matched_vcpu = m_vcpu
            matched_memory_gb = m_mem
            monthly_price = unit_price * hours * qty
            calc_note = f"RDS DB instance ({os_or_engine} Single-AZ)"
            
            # If storage is specified, also calculate and note the storage cost
            if size_gb > 0:
                storage_rate = engine.get_rds_storage_price(region, "gp3", "Single-AZ")
                storage_cost = storage_rate * size_gb * qty
                monthly_price += storage_cost
                calc_note += f" + gp3 storage ({size_gb:.1f} GB @ ${storage_rate}/GB-mo)"
                
        elif service == 's3':
            unit_price = engine.get_s3_price(region, res_type)
            monthly_price = unit_price * size_gb * qty
            calc_note = f"S3 Storage Class: {res_type}"
            
        elif service == 'eks':
            unit_price = engine.get_eks_price(region)
            monthly_price = unit_price * hours * qty
            calc_note = f"EKS cluster management fee"
            
        elif service in ['eks-fargate', 'eks_fargate']:
            # AWS EKS on Fargate pricing
            # 1. EKS cluster management fee (for 1 cluster)
            cluster_fee = engine.get_eks_price(region) * hours
            
            # 2. Fargate compute pricing for pods
            is_arm = False
            if res_type and ('arm' in res_type.lower() or 'graviton' in res_type.lower()):
                is_arm = True
                
            # Define regional hourly rates for Fargate
            fargate_rates = PRICING_DEFAULTS["fargate_default_rates"]
            region_rates = fargate_rates.get(region, fargate_rates["default"])
            vcpu_base = region_rates["vcpu"]
            mem_base = region_rates["memory"]
                
            if is_arm:
                vcpu_rate = vcpu_base * 0.8
                mem_rate = mem_base * 0.8
                arch_name = "ARM64/Graviton"
            else:
                vcpu_rate = vcpu_base
                mem_rate = mem_base
                arch_name = "x86_64"
                
            billed_vcpu, billed_mem = round_fargate_specs(vcpu, memory_gb)
            
            pod_hours = hours if hours > 0 else 730
            pod_qty = qty if qty > 0 else 1
            
            compute_cost = (billed_vcpu * vcpu_rate + billed_mem * mem_rate) * pod_hours * pod_qty
            
            # Add extra ephemeral storage cost if size_gb > 20
            extra_storage_cost = 0.0
            if size_gb > 20:
                storage_rate = 0.000138 if region == 'ap-southeast-3' else 0.000111
                extra_storage_cost = (size_gb - 20) * storage_rate * pod_hours * pod_qty
                compute_cost += extra_storage_cost
                
            monthly_price = cluster_fee + compute_cost
            unit_price = compute_cost / (pod_qty * pod_hours)
            
            calc_note = (
                f"EKS cluster fee + Fargate pods: {pod_qty} pods running {pod_hours} hrs/mo "
                f"({arch_name}, billed resource/pod: {billed_vcpu} vCPU / {billed_mem} GB RAM after overhead/rounding)"
            )
            if size_gb > 20:
                calc_note += f" + extra storage ({size_gb - 20:.1f} GB @ ${storage_rate:.6f}/GB-hr)"
            
        elif service in ('data_transfer', 'data-transfer'):
            monthly_price, calc_note = engine.calculate_dt_cost(region, size_gb)
            unit_price = monthly_price / size_gb if size_gb > 0 else 0.0
            
        elif service == 'drs':
            servers = qty
            storage_gb = size_gb
            ebs_type = res_type if res_type and res_type.lower() != 'custom' else 'snapshot'
            
            _, server_cost, storage_cost = engine.get_drs_price(
                region, servers, storage_gb, volume_type=ebs_type, hours=hours
            )
            
            unit_price = engine.drs_cache.get(region, {}).get("server_hour_rate", PRICING_DEFAULTS["drs_server_hourly"])
            if ebs_type.lower() == 'snapshot':
                storage_rate = PRICING_DEFAULTS["aws_backup_snapshot_gb_rate"]
            else:
                storage_rate = engine.get_ebs_price(region, ebs_type)
            
            monthly_price = server_cost + storage_cost
            calc_note = f"{servers} servers ({hours} hrs/mo @ ${unit_price:.3f}/hr) + {ebs_type} storage ({storage_gb * qty:.1f} GB @ ${storage_rate:.3f}/GB-mo)"
            
        elif service in ('drs_drill', 'drs-drill'):
            ebs_type = res_type if res_type and res_type.lower() != 'custom' else 'gp3'
            if not res_type or res_type.lower() == 'custom':
                unit_price, resolved_type, m_vcpu, m_mem = engine.resolve_custom_ec2(
                    region, vcpu, memory_gb, os_or_engine, pref_arch
                )
                matched_type = resolved_type
                matched_vcpu = m_vcpu
                matched_memory_gb = m_mem
            else:
                unit_price, _, m_vcpu, m_mem = engine.get_ec2_price(region, res_type, os_or_engine)
                matched_type = res_type
                matched_vcpu = m_vcpu
                matched_memory_gb = m_mem
                
            storage_rate = engine.get_ebs_price(region, ebs_type)
            compute_cost = unit_price * hours * qty
            storage_cost = storage_rate * size_gb * qty * (hours / 730.0)
            monthly_price = compute_cost + storage_cost
            calc_note = f"DR Drill: {matched_type} ({hours} hrs @ ${unit_price:.4f}/hr) + pro-rated {ebs_type} storage ({size_gb * qty:.1f} GB @ ${storage_rate:.3f}/GB-mo)"
            
        elif service in ('backup', 'aws-backup', 'azure-backup'):
            is_azure = False
            if service == 'azure-backup' or 'southeastasia' in region.lower() or 'azure' in res_type.lower():
                is_azure = True
                
            retention_count = 4
            change_rate = 10.0
            
            has_type_numbers = False
            if res_type and res_type.lower() not in ('custom', 'default', 'standard', ''):
                numbers = re.findall(r'\d+', res_type)
                if len(numbers) >= 2:
                    retention_count = int(numbers[0])
                    change_rate = float(numbers[1])
                    has_type_numbers = True
                elif len(numbers) == 1:
                    retention_count = int(numbers[0])
                    has_type_numbers = True
            
            if 'backup_retention' in row and pd.notna(row['backup_retention']):
                retention_count = int(row['backup_retention'])
            elif 'retention' in row and pd.notna(row['retention']):
                retention_count = int(row['retention'])
            elif vcpu > 0 and not has_type_numbers:
                retention_count = int(vcpu)
                
            if 'backup_change_rate' in row and pd.notna(row['backup_change_rate']):
                change_rate = float(row['backup_change_rate'])
            elif 'change_rate' in row and pd.notna(row['change_rate']):
                change_rate = float(row['change_rate'])
            elif memory_gb > 0 and not has_type_numbers:
                change_rate = float(memory_gb)
            
            multiplier = 1.0 + (retention_count - 1) * (change_rate / 100.0)
            total_backup_gb = size_gb * qty * multiplier
            
            if is_azure:
                if size_gb <= 50:
                    instance_fee_rate = 5.0
                elif size_gb <= 500:
                    instance_fee_rate = 10.0
                else:
                    instance_fee_rate = float(((int(size_gb) - 1) // 500 + 1) * 10)
                
                instance_cost = instance_fee_rate * qty
                storage_rate = PRICING_DEFAULTS["azure_backup_storage_gb_rate"]
                storage_cost = total_backup_gb * storage_rate
                monthly_price = instance_cost + storage_cost
                calc_note = f"Azure Backup: {qty} instances (${instance_fee_rate}/mo/inst) + LRS Storage ({total_backup_gb:.1f} GB @ ${storage_rate:.4f}/GB-mo, retention: {retention_count}, change rate: {change_rate}%)"
            else:
                storage_rate = PRICING_DEFAULTS["aws_backup_snapshot_gb_rate"]
                monthly_price = total_backup_gb * storage_rate
                calc_note = f"AWS Backup (EBS Snapshots): {total_backup_gb:.1f} GB @ ${storage_rate:.3f}/GB-mo (retention: {retention_count}, change rate: {change_rate}%)"
                
        elif service in ('efs', 'nfs', 'azure-files', 'azure_files', 'azure-file', 'azure_file'):
            is_azure = False
            if service in ('azure-files', 'azure_files', 'azure-file', 'azure_file') or 'southeastasia' in region.lower() or 'azure' in res_type.lower():
                is_azure = True
                
            if is_azure:
                tier = res_type.lower() if res_type else 'transaction-optimized'
                rates = PRICING_DEFAULTS["azure_files_default_rates"]
                if 'premium' in tier:
                    storage_rate = rates["premium"]
                    tier_name = "Premium LRS"
                elif 'hot' in tier:
                    storage_rate = rates["hot"]
                    tier_name = "Hot LRS"
                elif 'cool' in tier:
                    storage_rate = rates["cool"]
                    tier_name = "Cool LRS"
                else:
                    storage_rate = rates["transaction-optimized"]
                    tier_name = "Transaction Optimized LRS"
                    
                monthly_price = size_gb * qty * storage_rate
                calc_note = f"Azure Files Storage ({tier_name} @ ${storage_rate:.4f}/GB-mo)"
            else:
                storage_class = res_type.lower() if res_type else 'standard'
                efs_rates = engine.efs_cache.get(region, PRICING_DEFAULTS["aws_efs_default_rates"])
                
                if 'ia' in storage_class or 'infrequent' in storage_class:
                    storage_rate = efs_rates.get("ia", 0.0272)
                    sc_name = "Infrequent Access"
                elif 'archive' in storage_class:
                    storage_rate = efs_rates.get("archive", 0.01)
                    sc_name = "Archive"
                else:
                    storage_rate = efs_rates.get("standard", 0.36)
                    sc_name = "Standard"
                    
                monthly_price = size_gb * qty * storage_rate
                calc_note = f"AWS EFS Storage ({sc_name} @ ${storage_rate:.4f}/GB-mo)"
                
        elif service in ('datasync', 'data-sync', 'storage-mover', 'storagemover'):
            is_azure = False
            if service in ('storage-mover', 'storagemover') or 'southeastasia' in region.lower() or 'azure' in res_type.lower():
                is_azure = True
                
            if is_azure:
                monthly_price = 0.0
                calc_note = "Azure Storage Mover: Migration service is Free. Note: Requires deploying a local agent VM on-premises (recommended: 2 vCPU, 4 GB RAM); Target storage (Azure Files/Blob) operations and storage charges apply separately."
            else:
                datasync_rate = PRICING_DEFAULTS["datasync_rate"]
                monthly_price = size_gb * qty * datasync_rate
                calc_note = f"AWS DataSync: {size_gb * qty:.1f} GB transferred (@ ${datasync_rate:.4f}/GB). Note: Inbound to AWS is free of data transfer charges; Requires deploying a local agent VM on-premises (recommended: 4 vCPU, 32 GB RAM); Target storage (S3/EFS/FSx) and API fees apply separately."
                
        elif service in ('alb', 'elb', 'load_balancer'):
            hour_rate, lcu_rate = engine.get_alb_price(region)
            unit_price = hour_rate
            base_cost = hour_rate * hours * qty
            lcu_cost = lcu_rate * max(hours, size_gb) * qty
            monthly_price = base_cost + lcu_cost
            calc_note = f"Application Load Balancer hourly fee (${hour_rate}/hr)"
            if size_gb > hours:
                calc_note += f" + LCU fee based on data processed ({size_gb:.1f} GB @ ${lcu_rate}/LCU-hr)"
            else:
                calc_note += f" + Minimum 1 LCU fee (${lcu_rate}/LCU-hr)"
                
        elif service in ('nat_gateway', 'nat'):
            hour_rate, gb_rate = engine.get_nat_gateway_price(region)
            unit_price = hour_rate
            hourly_cost = hour_rate * hours * qty
            processing_cost = gb_rate * size_gb * qty if size_gb > 0 else 0.0
            monthly_price = hourly_cost + processing_cost
            calc_note = f"NAT Gateway hourly fee (${hour_rate}/hr)"
            if size_gb > 0:
                calc_note += f" + data processing fee ({size_gb:.1f} GB @ ${gb_rate}/GB)"
                
        elif service in ('vpn', 'vpn_connection'):
            vpn_type = res_type if res_type and res_type.lower() != 'custom' else 'site-to-site'
            unit_price = engine.get_vpn_price(region, vpn_type)
            connection_cost = unit_price * hours * qty
            
            dt_cost = 0.0
            dt_rate = 0.0
            if size_gb > 0:
                dt_rate = engine.get_dt_tiers(region)[0]["price"]
                dt_cost = size_gb * qty * dt_rate
                
            monthly_price = connection_cost + dt_cost
            calc_note = f"VPN Connection ({vpn_type} @ ${unit_price}/hr)"
            if size_gb > 0:
                calc_note += f" + data transfer ({size_gb * qty:.1f} GB @ ${dt_rate:.3f}/GB)"
                
        elif service in ('eip', 'public_ip'):
            unit_price = engine.get_public_ip_price(region)
            monthly_price = unit_price * hours * qty
            calc_note = f"Public IPv4 Address / Elastic IP (${unit_price}/hr)"
            
        elif service == "azure":
            matched_type = res_type
            res_lower = res_type.lower()
            if "nat" in res_lower:
                hour_rate, gb_rate = engine.get_azure_nat_gateway_price(region)
                unit_price = hour_rate
                hourly_cost = hour_rate * hours * qty
                processing_cost = gb_rate * size_gb * qty if size_gb > 0 else 0.0
                monthly_price = hourly_cost + processing_cost
                calc_note = f"Azure NAT Gateway hourly fee (${hour_rate}/hr)"
                if size_gb > 0:
                    calc_note += f" + data processing fee ({size_gb:.1f} GB @ ${gb_rate}/GB)"
            elif "vpn" in res_lower:
                unit_price = engine.get_azure_vpn_gateway_price(region, res_type)
                gateway_cost = unit_price * hours * qty
                
                dt_cost = 0.0
                dt_rate = PRICING_DEFAULTS["azure_vpn_egress_gb_rate"]
                if size_gb > 0:
                    dt_cost = size_gb * qty * dt_rate
                    
                monthly_price = gateway_cost + dt_cost
                calc_note = f"Azure VPN Gateway ({res_type} @ ${unit_price}/hr)"
                if size_gb > 0:
                    calc_note += f" + data transfer ({size_gb * qty:.1f} GB @ ${dt_rate:.3f}/GB)"
            elif "ip" in res_lower or "eip" in res_lower:
                unit_price = engine.get_azure_public_ip_price(region)
                monthly_price = unit_price * hours * qty
                calc_note = f"Azure Public IP Address (${unit_price}/hr)"
            elif "asr" in res_lower or "site_recovery" in res_lower or "site-recovery" in res_lower:
                unit_price = PRICING_DEFAULTS["azure_asr_monthly_per_instance"]
                server_cost = qty * unit_price
                storage_rate = engine.get_azure_storage_price(region, "Standard_HDD")
                storage_cost = qty * size_gb * storage_rate
                monthly_price = server_cost + storage_cost
                calc_note = f"{qty} protected instances (${unit_price}/mo-instance) + Standard HDD replication storage ({size_gb * qty:.1f} GB @ ${storage_rate}/GB-mo)"
            elif "vm" in res_lower or "standard" in res_lower:
                unit_price = engine.get_azure_vm_price(region, res_type, os_or_engine)
                monthly_price = unit_price * hours * qty
                calc_note = f"Azure VM SKU: {res_type} ({os_or_engine})"
            elif "storage" in res_lower or "ssd" in res_lower or "hdd" in res_lower or "blob" in res_lower:
                unit_price = engine.get_azure_storage_price(region, res_type)
                monthly_price = unit_price * size_gb * qty
                calc_note = f"Azure Storage Type: {res_type}"
            else:
                if res_lower == "custom" or not res_type:
                    unit_price, resolved_sku, m_vcpu, m_mem = engine.resolve_custom_azure_vm(region, vcpu, memory_gb, os_or_engine)
                    matched_type = resolved_sku
                    matched_vcpu = m_vcpu
                    matched_memory_gb = m_mem
                    monthly_price = unit_price * hours * qty
                    calc_note = f"Cheapest Azure VM matching >= {vcpu} vCPU, {memory_gb:.1f} GB RAM ({os_or_engine})"
                else:
                    unit_price = engine.get_azure_vm_price(region, res_type, os_or_engine)
                    monthly_price = unit_price * hours * qty
                    calc_note = f"Azure VM SKU: {res_type} ({os_or_engine})"
        else:
            calc_note = f"Skipped: Unknown service type '{service}'"
            
        results.append({
            'id': id_val,
            'service': service,
            'region': region,
            'type': res_type,
            'matched_type': matched_type,
            'requested_vcpu': requested_vcpu,
            'requested_memory_gb': requested_memory_gb,
            'matched_vcpu': matched_vcpu,
            'matched_memory_gb': matched_memory_gb,
            'os_or_engine': os_or_engine,
            'size_gb': size_gb,
            'quantity': qty,
            'hours_per_month': hours,
            'unit_price': unit_price,
            'monthly_price': monthly_price,
            'currency': 'USD',
            'notes': calc_note,
            'description': desc
        })
        
    return pd.DataFrame(results)
