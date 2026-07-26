import os
import re
import sys
import json
import argparse
import requests
import pandas as pd
from tabulate import tabulate

# Region mapping from code to AWS Location name
REGION_MAP = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "af-south-1": "Africa (Cape Town)",
    "ap-east-1": "Asia Pacific (Hong Kong)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-southeast-3": "Asia Pacific (Jakarta)",
    "ap-southeast-4": "Asia Pacific (Melbourne)",
    "ap-south-2": "Asia Pacific (Hyderabad)",
    "ca-central-1": "Canada (Central)",
    "eu-central-1": "Europe (Frankfurt)",
    "eu-west-1": "Europe (Ireland)",
    "eu-west-2": "Europe (London)",
    "eu-south-1": "Europe (Milan)",
    "eu-west-3": "Europe (Paris)",
    "eu-south-2": "Europe (Spain)",
    "eu-north-1": "Europe (Stockholm)",
    "eu-central-2": "Europe (Zurich)",
    "me-south-1": "Middle East (Bahrain)",
    "me-central-1": "Middle East (UAE)",
    "sa-east-1": "South America (Sao Paulo)",
    "us-gov-east-1": "AWS GovCloud (US-East)",
    "us-gov-west-1": "AWS GovCloud (US-West)",
}

def parse_memory(mem_str):
    """Parse memory string (e.g. '16 GiB', '4,096 MB') to float GB."""
    if not mem_str:
        return 0.0
    if isinstance(mem_str, (int, float)):
        return float(mem_str)
    
    mem_str = str(mem_str).replace(",", "").lower()
    match = re.search(r"([0-9.]+)", mem_str)
    if not match:
        return 0.0
    
    val = float(match.group(1))
    # If the string indicates MB, convert to GB
    if "mb" in mem_str or "mib" in mem_str:
        return val / 1024.0
    return val

def normalize_os_or_engine(service, val):
    """Normalize input OS or DB engine to match standard AWS values."""
    if not val:
        return "Linux" if service == "ec2" else "PostgreSQL"
    val_lower = str(val).lower().strip()
    
    if service == "ec2":
        if "windows" in val_lower:
            return "Windows"
        elif "rhel" in val_lower or "red hat" in val_lower:
            return "RHEL"
        elif "suse" in val_lower:
            return "SUSE"
        elif "linux" in val_lower or "ubuntu" in val_lower or "centos" in val_lower or "debian" in val_lower:
            return "Linux"
        return "Linux"  # Default fallback for EC2
        
    elif service == "rds":
        if "postgres" in val_lower:
            return "PostgreSQL"
        elif "mysql" in val_lower:
            return "MySQL"
        elif "mariadb" in val_lower:
            return "MariaDB"
        elif "oracle" in val_lower:
            return "Oracle"
        elif "sql server" in val_lower or "mssql" in val_lower:
            return "SQL Server"
            
    return val

def get_architecture(instance_type, physical_processor=""):
    """Identify if instance type is arm64 (Graviton) or x86_64."""
    if "graviton" in str(physical_processor).lower():
        return "arm64"
    parts = instance_type.split('.')
    if len(parts) > 0:
        family = parts[0]
        match = re.search(r'\d+([a-z]+)', family)
        if match:
            suffix = match.group(1)
            if 'g' in suffix:
                return "arm64"
    return "x86_64"

class PricingEngine:
    def __init__(self, cache_dir=".cache", region="ap-southeast-3", pref_arch="x86_64"):
        self.cache_dir = cache_dir
        self.default_region = region
        self.preferred_architecture = pref_arch
        os.makedirs(cache_dir, exist_ok=True)
        
        self.ec2_cache = {}
        self.ebs_cache = {}
        self.rds_cache = {}
        self.rds_storage_cache = {}
        self.s3_cache = {}
        self.eks_cache = {}
        self.dt_cache = {}
        self.pub_dates = {}
        self.drs_cache = {}

    def get_bulk_url(self, service, region):
        """Build standard public JSON Bulk API URL."""
        return f"https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{service}/current/{region}/index.json"

    def fetch_json_with_cache(self, service, region):
        """Fetch raw JSON file using local cache."""
        cache_file = os.path.join(self.cache_dir, f"{service}_{region}.json")
        if os.path.exists(cache_file):
            print(f"Loading {service} for {region} from cache...")
            with open(cache_file) as f:
                return json.load(f)
        
        url = self.get_bulk_url(service, region)
        print(f"Downloading {service} pricing for {region} from {url}...")
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            data = r.json()
            with open(cache_file, "w") as f:
                json.dump(data, f)
            return data
        except Exception as e:
            print(f"Error downloading bulk data for {service} in {region}: {e}")
            return None

    def initialize_region(self, region):
        """Download and preprocess all required price lists for a region."""
        # EC2 Preprocessing
        ec2_processed = os.path.join(self.cache_dir, f"ec2_{region}_processed.json")
        ebs_processed = os.path.join(self.cache_dir, f"ebs_{region}_processed.json")
        drs_processed = os.path.join(self.cache_dir, f"drs_{region}_processed.json")

        if not os.path.exists(ec2_processed) or not os.path.exists(ebs_processed):
            raw_ec2 = self.fetch_json_with_cache("AmazonEC2", region)
            if raw_ec2:
                print(f"Preprocessing EC2 & EBS pricing for {region}...")
                pub_date = raw_ec2.get("publicationDate", "")
                products = raw_ec2.get("products", {})
                terms = raw_ec2.get("terms", {}).get("OnDemand", {})
                
                instances = {}
                ebs_storage = {}
                
                for sku, product in products.items():
                    attrs = product.get("attributes", {})
                    fam = product.get("productFamily")
                    
                    # EBS Storage Processing
                    if fam == "Storage" and attrs.get("volumeType"):
                        vol_type = attrs.get("volumeType")
                        usage = attrs.get("usagetype", "")
                        
                        sku_terms = terms.get(sku, {})
                        price = None
                        for term_val in sku_terms.values():
                            for dim_val in term_val.get("priceDimensions", {}).values():
                                price_str = dim_val.get("pricePerUnit", {}).get("USD")
                                if price_str is not None:
                                    price = float(price_str)
                                    break
                            break
                        
                        if price is not None:
                            short_name = None
                            if "gp3" in usage.lower(): short_name = "gp3"
                            elif "gp2" in usage.lower(): short_name = "gp2"
                            elif "io2" in usage.lower(): short_name = "io2"
                            elif "piops" in usage.lower() or "io1" in usage.lower(): short_name = "io1"
                            elif "st1" in usage.lower(): short_name = "st1"
                            elif "sc1" in usage.lower(): short_name = "sc1"
                            elif "volumeusage" in usage.lower() and not any(x in usage.lower() for x in ["gp3", "gp2", "io2", "piops", "st1", "sc1"]): short_name = "magnetic"
                            
                            if short_name:
                                ebs_storage[short_name] = price

                    # Compute Instance Processing
                    if fam not in ("Compute Instance", "Compute Instance (bare metal)"):
                        continue
                    if attrs.get("tenancy") != "Shared" or attrs.get("preInstalledSw") != "NA" or attrs.get("capacitystatus") != "Used":
                        continue
                    if attrs.get("marketoption", "OnDemand") != "OnDemand":
                        continue
                        
                    instance_type = attrs.get("instanceType")
                    os_name = attrs.get("operatingSystem")
                    if not instance_type or not os_name:
                        continue
                        
                    sku_terms = terms.get(sku, {})
                    price = None
                    for term_val in sku_terms.values():
                        for dim_val in term_val.get("priceDimensions", {}).values():
                            price_str = dim_val.get("pricePerUnit", {}).get("USD")
                            if price_str is not None:
                                price = float(price_str)
                                break
                        break
                    
                    if price is not None:
                        if instance_type not in instances:
                            vcpu_str = attrs.get("vcpu", "0")
                            try:
                                vcpu = int(vcpu_str)
                            except ValueError:
                                vcpu = 0
                            mem = parse_memory(attrs.get("memory"))
                            instances[instance_type] = {
                                "instanceType": instance_type,
                                "vcpu": vcpu,
                                "memory_gb": mem,
                                "physicalProcessor": attrs.get("physicalProcessor", ""),
                                "architecture": get_architecture(instance_type, attrs.get("physicalProcessor", "")),
                                "prices": {}
                            }
                        instances[instance_type]["prices"][os_name] = price
                
                with open(ec2_processed, "w") as f:
                    json.dump({
                        "publicationDate": pub_date,
                        "instances": list(instances.values())
                    }, f, indent=2)
                with open(ebs_processed, "w") as f:
                    json.dump(ebs_storage, f, indent=2)

        # Load EC2 & EBS Cache
        if os.path.exists(ec2_processed):
            with open(ec2_processed) as f:
                cached = json.load(f)
                if isinstance(cached, dict):
                    self.ec2_cache[region] = cached.get("instances", [])
                    pub_date = cached.get("publicationDate", "")
                else:
                    self.ec2_cache[region] = cached
                    pub_date = ""
                
                if not pub_date:
                    raw_path = os.path.join(self.cache_dir, f"AmazonEC2_{region}.json")
                    if os.path.exists(raw_path):
                        try:
                            with open(raw_path) as rf:
                                head = rf.read(1000)
                                match = re.search(r'"publicationDate"\s*:\s*"([^"]+)"', head)
                                if match:
                                    pub_date = match.group(1)
                        except Exception:
                            pass
                self.pub_dates[f"ec2_{region}"] = pub_date
        if os.path.exists(ebs_processed):
            with open(ebs_processed) as f:
                self.ebs_cache[region] = json.load(f)

        # RDS Preprocessing
        rds_processed = os.path.join(self.cache_dir, f"rds_{region}_processed.json")
        rds_storage_processed = os.path.join(self.cache_dir, f"rds_storage_{region}_processed.json")
        
        if not os.path.exists(rds_processed) or not os.path.exists(rds_storage_processed):
            raw_rds = self.fetch_json_with_cache("AmazonRDS", region)
            if raw_rds:
                print(f"Preprocessing RDS pricing for {region}...")
                pub_date = raw_rds.get("publicationDate", "")
                products = raw_rds.get("products", {})
                terms = raw_rds.get("terms", {}).get("OnDemand", {})
                
                db_instances = []
                rds_storage = {}
                
                for sku, product in products.items():
                    attrs = product.get("attributes", {})
                    fam = product.get("productFamily")
                    
                    # Database Storage Processing
                    if fam == "Database Storage" and attrs.get("volumeType"):
                        vol_type = attrs.get("volumeType")
                        deploy = attrs.get("deploymentOption")
                        
                        sku_terms = terms.get(sku, {})
                        price = None
                        for term_val in sku_terms.values():
                            for dim_val in term_val.get("priceDimensions", {}).values():
                                price_str = dim_val.get("pricePerUnit", {}).get("USD")
                                if price_str is not None:
                                    price = float(price_str)
                                    break
                            break
                        
                        if price is not None:
                            short_name = None
                            vol_lower = vol_type.lower()
                            if "gp3" in vol_lower: short_name = "gp3"
                            elif "gp2" in vol_lower or "general purpose" in vol_lower: short_name = "gp2"
                            elif "io2" in vol_lower: short_name = "io2"
                            elif "io1" in vol_lower or "provisioned iops" in vol_lower: short_name = "io1"
                            elif "magnetic" in vol_lower: short_name = "magnetic"
                            
                            if short_name and deploy:
                                if short_name not in rds_storage:
                                    rds_storage[short_name] = {}
                                rds_storage[short_name][deploy] = price

                    # Database Instance Processing
                    if fam != "Database Instance":
                        continue
                    instance_type = attrs.get("instanceType")
                    engine = attrs.get("databaseEngine")
                    deploy = attrs.get("deploymentOption")
                    if not instance_type or not engine or not deploy:
                        continue
                        
                    sku_terms = terms.get(sku, {})
                    price = None
                    for term_val in sku_terms.values():
                        for dim_val in term_val.get("priceDimensions", {}).values():
                            price_str = dim_val.get("pricePerUnit", {}).get("USD")
                            if price_str is not None:
                                price = float(price_str)
                                break
                        break
                    
                    if price is not None:
                        vcpu_str = attrs.get("vcpu", "0")
                        try:
                            vcpu = int(vcpu_str)
                        except ValueError:
                            vcpu = 0
                        mem = parse_memory(attrs.get("memory"))
                        
                        db_instances.append({
                            "instanceType": instance_type,
                            "databaseEngine": engine,
                            "deploymentOption": deploy,
                            "vcpu": vcpu,
                            "memory_gb": mem,
                            "price": price
                        })
                        
                with open(rds_processed, "w") as f:
                    json.dump({
                        "publicationDate": pub_date,
                        "instances": db_instances
                    }, f, indent=2)
                with open(rds_storage_processed, "w") as f:
                    json.dump(rds_storage, f, indent=2)
                    
        if os.path.exists(rds_processed):
            with open(rds_processed) as f:
                cached = json.load(f)
                if isinstance(cached, dict):
                    self.rds_cache[region] = cached.get("instances", [])
                    pub_date = cached.get("publicationDate", "")
                else:
                    self.rds_cache[region] = cached
                    pub_date = ""
                
                if not pub_date:
                    raw_path = os.path.join(self.cache_dir, f"AmazonRDS_{region}.json")
                    if os.path.exists(raw_path):
                        try:
                            with open(raw_path) as rf:
                                head = rf.read(1000)
                                match = re.search(r'"publicationDate"\s*:\s*"([^"]+)"', head)
                                if match:
                                    pub_date = match.group(1)
                        except Exception:
                            pass
                self.pub_dates[f"rds_{region}"] = pub_date
        if os.path.exists(rds_storage_processed):
            with open(rds_storage_processed) as f:
                self.rds_storage_cache[region] = json.load(f)
        if os.path.exists(drs_processed):
            with open(drs_processed) as f:
                self.drs_cache[region] = json.load(f)
        else:
            self.drs_cache[region] = {"server_hour_rate": 0.028, "storage_gb_month_rate": 0.11}
            with open(drs_processed, "w") as f:
                json.dump(self.drs_cache[region], f, indent=2)

        # S3, EKS, Data Transfer load directly
        raw_s3 = self.fetch_json_with_cache("AmazonS3", region)
        if raw_s3:
            self.s3_cache[region] = raw_s3
            self.pub_dates[f"s3_{region}"] = raw_s3.get("publicationDate", "")
            
        raw_eks = self.fetch_json_with_cache("AmazonEKS", region)
        if raw_eks:
            self.eks_cache[region] = raw_eks
            self.pub_dates[f"eks_{region}"] = raw_eks.get("publicationDate", "")
            
        raw_dt = self.fetch_json_with_cache("AWSDataTransfer", region)
        if raw_dt:
            self.dt_cache[region] = raw_dt
            self.pub_dates[f"data_transfer_{region}"] = raw_dt.get("publicationDate", "")

    def get_ec2_price(self, region, instance_type, os_name="Linux"):
        """Get pricing for explicit EC2 instance type."""
        region_cache = self.ec2_cache.get(region, [])
        for inst in region_cache:
            if inst["instanceType"] == instance_type:
                price = inst["prices"].get(os_name)
                if price is not None:
                    return price, instance_type, inst["vcpu"], inst["memory_gb"]
                # Fallback to Linux if requested OS is missing
                return inst["prices"].get("Linux", 0.0), instance_type, inst["vcpu"], inst["memory_gb"]
        return 0.0, instance_type, 0, 0.0

    def resolve_custom_ec2(self, region, vcpu, memory_gb, os_name="Linux", preferred_arch="x86_64"):
        """Find the cheapest EC2 instance that meets vCPU and Memory specs."""
        region_cache = self.ec2_cache.get(region, [])
        matches = []
        for inst in region_cache:
            # Filter by architecture if specified
            if preferred_arch != "any" and inst["architecture"] != preferred_arch:
                continue
            
            if inst["vcpu"] >= vcpu and inst["memory_gb"] >= memory_gb:
                price = inst["prices"].get(os_name)
                if price is not None:
                    matches.append(inst)
                    
        if not matches:
            # Try matching with any architecture as fallback
            if preferred_arch != "any":
                return self.resolve_custom_ec2(region, vcpu, memory_gb, os_name, "any")
            return 0.0, "unknown", 0, 0.0
            
        # Sort matches by price for the requested OS
        matches.sort(key=lambda x: x["prices"].get(os_name, float('inf')))
        best = matches[0]
        return best["prices"][os_name], best["instanceType"], best["vcpu"], best["memory_gb"]

    def get_ebs_price(self, region, volume_type="gp3"):
        """Get EBS storage GB-month rate."""
        volume_type = volume_type.lower()
        region_cache = self.ebs_cache.get(region, {})
        # Defaults if not found
        defaults = {
            "gp3": 0.096,
            "gp2": 0.120,
            "io1": 0.138,
            "io2": 0.138,
            "st1": 0.054,
            "sc1": 0.018,
            "magnetic": 0.080
        }
        return region_cache.get(volume_type, defaults.get(volume_type, 0.0))

    def get_rds_price(self, region, instance_type, engine="PostgreSQL", deploy="Single-AZ"):
        """Get RDS database instance hourly rate and specs."""
        region_cache = self.rds_cache.get(region, [])
        # Format engine parameter
        engine_lower = engine.lower()
        engine_match = "PostgreSQL"
        if "mysql" in engine_lower:
            engine_match = "MySQL"
        elif "mariadb" in engine_lower:
            engine_match = "MariaDB"
        elif "oracle" in engine_lower:
            engine_match = "Oracle"
        elif "sql server" in engine_lower:
            engine_match = "SQL Server"
            
        for inst in region_cache:
            if inst["instanceType"] == instance_type and inst["deploymentOption"] == deploy:
                if engine_match in inst["databaseEngine"]:
                    return inst["price"], inst["vcpu"], inst["memory_gb"]
        return 0.0, 0, 0.0

    def get_rds_storage_price(self, region, volume_type="gp3", deploy="Single-AZ"):
        """Get RDS storage GB-month rate."""
        volume_type = volume_type.lower()
        region_cache = self.rds_storage_cache.get(region, {})
        
        # Mapping to RDS deployment names
        deploy_map = {
            "single-az": "Single-AZ",
            "multi-az": "Multi-AZ"
        }
        deploy_key = deploy_map.get(deploy.lower(), "Single-AZ")
        
        # Default pricing fallback for RDS storage
        defaults = {
            "gp3": 0.138 if deploy_key == "Single-AZ" else 0.276,
            "gp2": 0.138 if deploy_key == "Single-AZ" else 0.276,
            "io1": 0.138 if deploy_key == "Single-AZ" else 0.276,
            "io2": 0.138 if deploy_key == "Single-AZ" else 0.276,
            "magnetic": 0.110 if deploy_key == "Single-AZ" else 0.220
        }
        
        type_rates = region_cache.get(volume_type, {})
        return type_rates.get(deploy_key, defaults.get(volume_type, 0.0))

    def get_s3_price(self, region, storage_class="Standard"):
        """Get S3 storage class GB-month rate."""
        s3_data = self.s3_cache.get(region)
        if not s3_data:
            # Static fallback rates for Jakarta ap-southeast-3
            fallbacks = {
                "standard": 0.025,
                "infrequent access": 0.0138,
                "standard-ia": 0.0138,
                "one zone-ia": 0.011,
                "glacier": 0.0045,
                "glacier flexible": 0.0045,
                "deep archive": 0.0020,
                "glacier deep archive": 0.0020,
                "intelligent tiering": 0.025
            }
            return fallbacks.get(storage_class.lower(), 0.025)
            
        products = s3_data.get("products", {})
        terms = s3_data.get("terms", {}).get("OnDemand", {})
        
        # Normalize target storage class
        sc_lower = storage_class.lower()
        target_sc = "General Purpose" # default for Standard
        if "infrequent" in sc_lower or "ia" in sc_lower:
            target_sc = "Infrequent Access"
        elif "archive" in sc_lower or "glacier" in sc_lower:
            target_sc = "Archive"
        elif "intelligent" in sc_lower:
            target_sc = "Intelligent-Tiering"
            
        for sku, product in products.items():
            attrs = product.get("attributes", {})
            if product.get("productFamily") == "Storage" and attrs.get("storageClass") == target_sc:
                usage = attrs.get("usagetype", "")
                
                # Check specifics
                if target_sc == "Infrequent Access":
                    if "zia" in sc_lower or "one zone" in sc_lower:
                        if "zia" not in usage.lower(): continue
                    else:
                        if "sia" not in usage.lower(): continue
                elif target_sc == "Archive":
                    if "deep" in sc_lower or "gda" in sc_lower:
                        if "gda" not in usage.lower(): continue
                    else:
                        if "glacierbytehrs" not in usage.lower(): continue
                elif target_sc == "Intelligent-Tiering":
                    if "fa-bytehrs" not in usage.lower(): continue # use frequent access price
                    
                # Extract first price
                sku_terms = terms.get(sku, {})
                for term_val in sku_terms.values():
                    for dim_val in term_val.get("priceDimensions", {}).values():
                        price_str = dim_val.get("pricePerUnit", {}).get("USD")
                        if price_str is not None:
                            return float(price_str)
        return 0.025

    def get_eks_price(self, region):
        """Get EKS hourly cluster fee."""
        eks_data = self.eks_cache.get(region)
        if eks_data:
            products = eks_data.get("products", {})
            terms = eks_data.get("terms", {}).get("OnDemand", {})
            for sku, product in products.items():
                attrs = product.get("attributes", {})
                if "percluster" in attrs.get("usagetype", "").lower():
                    sku_terms = terms.get(sku, {})
                    for term_val in sku_terms.values():
                        for dim_val in term_val.get("priceDimensions", {}).values():
                            price_str = dim_val.get("pricePerUnit", {}).get("USD")
                            if price_str is not None:
                                return float(price_str)
        return 0.10
    
    def get_drs_price(self, region, servers, storage_gb):
        """Get AWS DRS cost based on number of servers and storage GB."""
        drs_data = self.drs_cache.get(region, {})
        if not drs_data:
        # Fallback rates
            drs_data = {"server_hour_rate": 0.028, "storage_gb_month_rate": 0.11}
    
        # Perhitungan biaya server dan storage
        server_cost = servers * drs_data["server_hour_rate"] * 730  # Total server cost per month (servers * hourly_rate * 730 monthly hours)
        storage_cost = storage_gb * drs_data["storage_gb_month_rate"]  # Total storage cost
        total_cost = server_cost + storage_cost  # Total monthly cost
    
        return total_cost, server_cost, storage_cost  # Pastikan tiga nilai dikembalikan


    def get_dt_tiers(self, region):
        """Get tiered egress rates for Data Transfer."""
        dt_data = self.dt_cache.get(region)
        region_name = REGION_MAP.get(region, "Asia Pacific (Jakarta)")
        
        default_tiers = [
            {"begin": 0.0, "end": 10240.0, "price": 0.132}, # up to 10 TB
            {"begin": 10240.0, "end": 51200.0, "price": 0.100}, # next 40 TB
            {"begin": 51200.0, "end": 153600.0, "price": 0.095}, # next 100 TB
            {"begin": 153600.0, "end": float('inf'), "price": 0.090} # over 150 TB
        ]
        
        if not dt_data:
            return default_tiers
            
        products = dt_data.get("products", {})
        terms = dt_data.get("terms", {}).get("OnDemand", {})
        
        matched_sku = None
        for sku, product in products.items():
            attrs = product.get("attributes", {})
            if attrs.get("fromLocation") == region_name and attrs.get("toLocation") == "External" and attrs.get("usagetype", "").endswith("DataTransfer-Out-Bytes"):
                matched_sku = sku
                break
                
        if not matched_sku:
            return default_tiers
            
        sku_terms = terms.get(matched_sku, {})
        tiers = []
        for term_val in sku_terms.values():
            for dim_val in term_val.get("priceDimensions", {}).values():
                price_str = dim_val.get("pricePerUnit", {}).get("USD")
                if price_str is not None:
                    price = float(price_str)
                    begin = float(dim_val.get("beginRange", 0))
                    end_val = dim_val.get("endRange", "Inf")
                    end = float(end_val) if end_val != "Inf" else float('inf')
                    tiers.append({"begin": begin, "end": end, "price": price})
                    
        if not tiers:
            return default_tiers
            
        tiers.sort(key=lambda x: x["begin"])
        return tiers

    def calculate_dt_cost(self, region, size_gb):
        """Calculate tiered egress cost for Data Transfer."""
        # 100 GB Global Free Tier
        billable_gb = max(0.0, size_gb - 100.0)
        if billable_gb <= 0:
            return 0.0, "100 GB Global Free Tier applied (100% Free)"
            
        tiers = self.get_dt_tiers(region)
        remaining = billable_gb
        total_cost = 0.0
        details = []
        
        for tier in tiers:
            if remaining <= 0:
                break
            tier_size = tier["end"] - tier["begin"]
            chunk = min(remaining, tier_size)
            cost = chunk * tier["price"]
            total_cost += cost
            details.append(f"{chunk:.1f} GB @ ${tier['price']}/GB")
            remaining -= chunk
            
        desc = "100 GB Free + " + " + ".join(details)
        return total_cost, desc

def run_calculation(input_file, engine, default_region="ap-southeast-3", pref_arch="x86_64"):
    """Parse input file, calculate costs, and return detailed results DataFrame."""
    # Load and normalize columns
    if input_file.endswith('.xlsx') or input_file.endswith('.xls'):
        df = pd.read_excel(input_file)
    else:
        df = pd.read_csv(input_file)
        
    df.columns = [c.strip() for c in df.columns]
    
    # Check if VM spec format (e.g. from VMware)
    if 'VM' in df.columns and 'CPUs' in df.columns and 'Memory' in df.columns:
        print("Detected VM specification format. Generating detailed EC2 & EBS estimate...")
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
                'vcpu': cpus,
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
                    'vcpu': 0,
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
        if 'vcpu' not in calc_df.columns: calc_df['vcpu'] = 0
        if 'memory_gb' not in calc_df.columns: calc_df['memory_gb'] = 0.0
        if 'os_or_engine' not in calc_df.columns: calc_df['os_or_engine'] = 'Linux'
        if 'size_gb' not in calc_df.columns: calc_df['size_gb'] = 0.0
        if 'quantity' not in calc_df.columns: calc_df['quantity'] = 1
        if 'hours_per_month' not in calc_df.columns: calc_df['hours_per_month'] = 730
        if 'description' not in calc_df.columns: calc_df['description'] = ''
        
        # Fill standard NaN values
        calc_df['region'] = calc_df['region'].fillna(default_region)
        calc_df['type'] = calc_df['type'].fillna('custom')
        calc_df['vcpu'] = calc_df['vcpu'].fillna(0).astype(int)
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
        vcpu = int(row['vcpu']) if 'vcpu' in row else 0
        memory_gb = float(row['memory_gb']) if 'memory_gb' in row else 0.0
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
            
        elif service == 'data_transfer' or service == 'data-transfer':
            monthly_price, calc_note = engine.calculate_dt_cost(region, size_gb)
            unit_price = monthly_price / size_gb if size_gb > 0 else 0.0
        elif service == 'drs':
            servers = qty  # Use 'quantity' as number of servers for DRS
            storage_gb = size_gb  # Use 'size_gb' as storage amount for DRS
            
            # Get costs from engine
            unit_price, server_cost, storage_cost = engine.get_drs_price(region, servers, storage_gb)
            
            # Summarize total monthly price
            monthly_price = server_cost + storage_cost
            calc_note = f"{servers} servers (${server_cost:.2f}) + {storage_gb:.2f} GB Storage (${storage_cost:.2f})"
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

def main():
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
        print("Clearing raw JSON cache...")
        for filename in os.listdir(args.cache_dir):
            if filename.endswith(".json") and not filename.endswith("_processed.json"):
                os.remove(os.path.join(args.cache_dir, filename))
                
    # Initialize Engine
    engine = PricingEngine(cache_dir=args.cache_dir, region=args.region, pref_arch=args.architecture)
    
    # Run calculation
    try:
        results_df = run_calculation(args.input_file, engine, default_region=args.region, pref_arch=args.architecture)
    except Exception as e:
        print(f"Error executing cost calculations: {e}", file=sys.stderr)
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
        'requested_vcpu': 0,
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
            'requested_vcpu': 0,
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
