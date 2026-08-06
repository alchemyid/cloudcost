import os
import re
import json
import logging
import requests
from typing import Dict, Any, Tuple, Optional, List

from .config import REGION_MAP, PRICING_DEFAULTS
from .utils import (
    parse_memory,
    normalize_os_or_engine,
    get_architecture,
    extract_price_from_terms
)

logger = logging.getLogger(__name__)

# Precompiled regex patterns for cache headers
PUB_DATE_PATTERN = re.compile(r'"publicationDate"\s*:\s*"([^"]+)"')

class PricingEngine:
    def __init__(self, cache_dir: str = ".cache", region: str = "ap-southeast-3", pref_arch: str = "x86_64"):
        self.cache_dir = cache_dir
        self.default_region = region
        self.preferred_architecture = pref_arch
        os.makedirs(cache_dir, exist_ok=True)
        
        # O(1) keyed caches: { region: { instanceType/SKU: inst_data } }
        self.ec2_cache: Dict[str, Dict[str, Any]] = {}
        self.ebs_cache: Dict[str, Dict[str, float]] = {}
        self.rds_cache: Dict[str, Dict[Tuple[str, str, str], Any]] = {}
        self.rds_storage_cache: Dict[str, Dict[str, Dict[str, float]]] = {}
        self.s3_cache: Dict[str, Any] = {}
        self.eks_cache: Dict[str, Any] = {}
        self.dt_cache: Dict[str, Any] = {}
        self.pub_dates: Dict[str, str] = {}
        self.drs_cache: Dict[str, Dict[str, float]] = {}
        self.vpc_cache: Dict[str, Dict[str, float]] = {}
        self.azure_cache: Dict[str, float] = {}
        self.efs_cache: Dict[str, Dict[str, float]] = {}

    def get_bulk_url(self, service: str, region: str) -> str:
        """Build standard public JSON Bulk API URL."""
        return f"https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/{service}/current/{region}/index.json"

    def fetch_json_with_cache(self, service: str, region: str) -> Optional[Dict[str, Any]]:
        """Fetch raw JSON file using local cache with error handling and logging."""
        cache_file = os.path.join(self.cache_dir, f"{service}_{region}.json")
        if os.path.exists(cache_file):
            logger.info(f"Loading {service} for {region} from cache...")
            try:
                with open(cache_file, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Failed to load cached file for {service} in {region}: {e}. Redownloading...")
                try:
                    os.remove(cache_file)
                except Exception:
                    pass
        
        url = self.get_bulk_url(service, region)
        logger.info(f"Downloading {service} pricing for {region} from {url}...")
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            data = r.json()
            with open(cache_file, "w") as f:
                json.dump(data, f)
            return data
        except Exception as e:
            logger.exception(f"Error downloading bulk data for {service} in {region}: {e}")
            return None

    def initialize_azure_prices(self) -> None:
        """No-op initialization since pricing is fetched on-demand."""
        pass

    def get_azure_vm_price(self, region: str, vm_sku: str, os_name: str = "Linux") -> float:
        """Get Azure VM pricing on-demand with caching."""
        sku_clean = vm_sku.strip()
        region_clean = region.strip().lower().replace(" ", "")
        os_clean = os_name.strip().lower()
        is_windows = "windows" in os_clean
        
        cache_key = f"vm:{region_clean}:{sku_clean}:{is_windows}"
        if cache_key in self.azure_cache:
            return self.azure_cache[cache_key]
            
        azure_cache_file = os.path.join(self.cache_dir, "azure_prices_processed.json")
        processed_data = {}
        if os.path.exists(azure_cache_file):
            try:
                with open(azure_cache_file, "r") as f:
                    processed_data = json.load(f)
                    if cache_key in processed_data:
                        self.azure_cache[cache_key] = processed_data[cache_key]
                        return processed_data[cache_key]
            except Exception as e:
                logger.warning(f"Failed to read Azure cache: {e}")
                
        url = f"https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview&$filter=serviceName eq 'Virtual Machines' and armRegionName eq '{region_clean}' and armSkuName eq '{sku_clean}' and priceType eq 'Consumption'"
        try:
            logger.info(f"Fetching Azure VM pricing from API for {sku_clean} ({os_name}) in {region_clean}...")
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()
            items = data.get("Items", [])
            
            valid_items = []
            for item in items:
                prod_lower = item.get("productName", "").lower()
                meter_lower = item.get("meterName", "").lower()
                
                if "spot" in prod_lower or "spot" in meter_lower:
                    continue
                if "low priority" in prod_lower or "low priority" in meter_lower:
                    continue
                    
                contains_windows = "windows" in prod_lower
                if is_windows == contains_windows:
                    valid_items.append(item)
                    
            price = 0.0
            if valid_items:
                price = float(valid_items[0].get("retailPrice", 0.0))
            else:
                logger.warning(f"No Azure VM SKU matched for {sku_clean} in {region_clean}. Using standard fallback.")
                price = 0.212 if is_windows else 0.12
            
            self.azure_cache[cache_key] = price
            processed_data[cache_key] = price
            try:
                with open(azure_cache_file, "w") as f:
                    json.dump(processed_data, f, indent=2)
            except IOError as e:
                logger.error(f"Failed to write Azure processed cache: {e}")
                
            return price
        except Exception as e:
            logger.exception(f"Error querying Azure pricing API for {sku_clean}: {e}")
            return 0.212 if is_windows else 0.12

    def get_azure_storage_price(self, region: str, storage_type: str = "Standard_SSD") -> float:
        """Get Azure Storage pricing."""
        type_lower = storage_type.lower()
        if "premium" in type_lower:
            return 0.15
        elif "standard ssd" in type_lower or "standard_ssd" in type_lower:
            return 0.096
        elif "standard hdd" in type_lower or "standard_hdd" in type_lower:
            return 0.05
        elif "blob" in type_lower or "hot" in type_lower:
            return 0.02
        return 0.096

    def get_azure_nat_gateway_price(self, region: str) -> Tuple[float, float]:
        """Get Azure NAT Gateway hourly fee and per-GB processing fee."""
        defaults = PRICING_DEFAULTS
        return defaults["azure_nat_gateway_hourly"], defaults["azure_nat_gateway_gb_rate"]

    def get_azure_vpn_gateway_price(self, region: str, vpn_type: str = "basic") -> float:
        """Get Azure VPN Gateway hourly rate based on type."""
        type_lower = vpn_type.lower()
        if "gw1" in type_lower or "standard" in type_lower:
            return 0.19
        return PRICING_DEFAULTS["azure_vpn_gateway_base_hourly"]

    def get_azure_public_ip_price(self, region: str) -> float:
        """Get Azure Public IPv4 address hourly rate."""
        return PRICING_DEFAULTS["azure_public_ip_hourly"]

    def get_alb_price(self, region: str) -> Tuple[float, float]:
        """Get AWS ALB hourly rate and LCU rate."""
        region_lower = region.lower()
        if "jakarta" in region_lower or "ap-southeast-3" in region_lower:
            return 0.0252, 0.008
        elif "singapore" in region_lower or "ap-southeast-1" in region_lower:
            return 0.0243, 0.008
        return 0.0225, 0.008

    def resolve_custom_azure_vm(self, region: str, vcpu: float, memory_gb: float, os_name: str = "Linux") -> Tuple[float, str, int, float]:
        """Find the cheapest Azure VM SKU that meets or exceeds vCPU and Memory specs."""
        azure_vm_specs = {
            "Standard_B1s": {"vcpu": 1, "memory_gb": 1.0},
            "Standard_B1ms": {"vcpu": 1, "memory_gb": 2.0},
            "Standard_B2s": {"vcpu": 2, "memory_gb": 4.0},
            "Standard_B2ms": {"vcpu": 2, "memory_gb": 8.0},
            "Standard_B4ms": {"vcpu": 4, "memory_gb": 16.0},
            "Standard_B8ms": {"vcpu": 8, "memory_gb": 32.0},
            "Standard_D2s_v5": {"vcpu": 2, "memory_gb": 8.0},
            "Standard_D4s_v5": {"vcpu": 4, "memory_gb": 16.0},
            "Standard_D8s_v5": {"vcpu": 8, "memory_gb": 32.0},
            "Standard_D16s_v5": {"vcpu": 16, "memory_gb": 64.0},
            "Standard_F2s_v2": {"vcpu": 2, "memory_gb": 4.0},
            "Standard_F4s_v2": {"vcpu": 4, "memory_gb": 8.0},
            "Standard_F8s_v2": {"vcpu": 8, "memory_gb": 16.0},
            "Standard_F16s_v2": {"vcpu": 16, "memory_gb": 32.0},
            "Standard_E2s_v5": {"vcpu": 2, "memory_gb": 16.0},
            "Standard_E4s_v5": {"vcpu": 4, "memory_gb": 32.0},
            "Standard_E8s_v5": {"vcpu": 8, "memory_gb": 64.0},
            "Standard_E16s_v5": {"vcpu": 16, "memory_gb": 128.0},
        }
        
        matches = []
        for sku, spec in azure_vm_specs.items():
            if spec["vcpu"] >= vcpu and spec["memory_gb"] >= memory_gb:
                price = self.get_azure_vm_price(region, sku, os_name)
                if price > 0:
                    matches.append((price, sku, spec["vcpu"], spec["memory_gb"]))
                    
        if matches:
            matches.sort(key=lambda x: x[0])
            best_price, best_sku, matched_vcpu, matched_mem = matches[0]
            return best_price, best_sku, matched_vcpu, matched_mem
            
        fallback_sku = "Standard_D2s_v5"
        fallback_price = self.get_azure_vm_price(region, fallback_sku, os_name)
        return fallback_price, fallback_sku, 2, 8.0

    def initialize_region(self, region: str) -> None:
        """Download and preprocess all required price lists for a region."""
        self._initialize_ec2_ebs(region)
        self._initialize_rds(region)
        self._initialize_drs(region)
        self._initialize_vpc(region)
        self._initialize_s3_eks_dt(region)
        self._initialize_efs(region)

    def _initialize_ec2_ebs(self, region: str) -> None:
        """Preprocess EC2 and EBS pricing into caches."""
        ec2_processed = os.path.join(self.cache_dir, f"ec2_{region}_processed.json")
        ebs_processed = os.path.join(self.cache_dir, f"ebs_{region}_processed.json")
        
        if not os.path.exists(ec2_processed) or not os.path.exists(ebs_processed):
            raw_ec2 = self.fetch_json_with_cache("AmazonEC2", region)
            if raw_ec2:
                logger.info(f"Preprocessing EC2 & EBS pricing for {region}...")
                pub_date = raw_ec2.get("publicationDate", "")
                products = raw_ec2.get("products", {})
                terms = raw_ec2.get("terms", {}).get("OnDemand", {})
                
                instances = {}
                ebs_storage = {}
                
                for sku, product in products.items():
                    attrs = product.get("attributes", {})
                    fam = product.get("productFamily")
                    
                    if fam == "Storage" and attrs.get("volumeType"):
                        vol_type = attrs.get("volumeType")
                        usage = attrs.get("usagetype", "")
                        price = extract_price_from_terms(terms.get(sku, {}))
                        
                        if price is not None:
                            short_name = None
                            if "gp3" in usage.lower(): short_name = "gp3"
                            elif "gp2" in usage.lower(): short_name = "gp2"
                            elif "io2" in usage.lower(): short_name = "io2"
                            elif "piops" in usage.lower() or "io1" in usage.lower(): short_name = "io1"
                            elif "st1" in usage.lower(): short_name = "st1"
                            elif "sc1" in usage.lower(): short_name = "sc1"
                            elif "volumeusage" in usage.lower() and not any(x in usage.lower() for x in ["gp3", "gp2", "io2", "piops", "st1", "sc1"]):
                                short_name = "magnetic"
                            
                            if short_name:
                                ebs_storage[short_name] = price
                                
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
                        
                    price = extract_price_from_terms(terms.get(sku, {}))
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
                
                try:
                    with open(ec2_processed, "w") as f:
                        json.dump({
                            "publicationDate": pub_date,
                            "instances": list(instances.values())
                        }, f, indent=2)
                    with open(ebs_processed, "w") as f:
                        json.dump(ebs_storage, f, indent=2)
                except IOError as e:
                    logger.error(f"Failed to write EC2/EBS preprocessed cache files: {e}")
                    
        # Load EC2 & EBS Cache (O(1) indexing)
        if os.path.exists(ec2_processed):
            try:
                with open(ec2_processed, "r") as f:
                    cached = json.load(f)
                    pub_date = cached.get("publicationDate", "")
                    instances_list = cached.get("instances", [])
                    # O(1) lookup dictionary
                    self.ec2_cache[region] = {inst["instanceType"]: inst for inst in instances_list}
                    self.pub_dates[f"ec2_{region}"] = pub_date
            except Exception as e:
                logger.error(f"Failed to load processed EC2 cache: {e}")
                
        if os.path.exists(ebs_processed):
            try:
                with open(ebs_processed, "r") as f:
                    self.ebs_cache[region] = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load processed EBS cache: {e}")

    def _initialize_rds(self, region: str) -> None:
        """Preprocess RDS pricing into caches."""
        rds_processed = os.path.join(self.cache_dir, f"rds_{region}_processed.json")
        rds_storage_processed = os.path.join(self.cache_dir, f"rds_storage_{region}_processed.json")
        
        if not os.path.exists(rds_processed) or not os.path.exists(rds_storage_processed):
            raw_rds = self.fetch_json_with_cache("AmazonRDS", region)
            if raw_rds:
                logger.info(f"Preprocessing RDS pricing for {region}...")
                pub_date = raw_rds.get("publicationDate", "")
                products = raw_rds.get("products", {})
                terms = raw_rds.get("terms", {}).get("OnDemand", {})
                
                db_instances = []
                rds_storage = {}
                
                for sku, product in products.items():
                    attrs = product.get("attributes", {})
                    fam = product.get("productFamily")
                    
                    if fam == "Database Storage" and attrs.get("volumeType"):
                        vol_type = attrs.get("volumeType")
                        deploy = attrs.get("deploymentOption")
                        price = extract_price_from_terms(terms.get(sku, {}))
                        
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
                                
                    if fam != "Database Instance":
                        continue
                    instance_type = attrs.get("instanceType")
                    engine = attrs.get("databaseEngine")
                    deploy = attrs.get("deploymentOption")
                    if not instance_type or not engine or not deploy:
                        continue
                        
                    price = extract_price_from_terms(terms.get(sku, {}))
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
                        
                try:
                    with open(rds_processed, "w") as f:
                        json.dump({
                            "publicationDate": pub_date,
                            "instances": db_instances
                        }, f, indent=2)
                    with open(rds_storage_processed, "w") as f:
                        json.dump(rds_storage, f, indent=2)
                except IOError as e:
                    logger.error(f"Failed to write RDS preprocessed cache files: {e}")
                    
        # Load RDS cache files (O(1) lookup dictionary)
        pub_date = ""
        if os.path.exists(rds_processed):
            try:
                with open(rds_processed, "r") as f:
                    cached = json.load(f)
                    instances_list = cached.get("instances", [])
                    # Key: (instanceType, deploymentOption, databaseEngine)
                    self.rds_cache[region] = {
                        (inst["instanceType"], inst["deploymentOption"], inst["databaseEngine"]): inst 
                        for inst in instances_list
                    }
                    pub_date = cached.get("publicationDate", "")
            except Exception as e:
                logger.error(f"Failed to load processed RDS cache: {e}")
                
            if not pub_date:
                raw_path = os.path.join(self.cache_dir, f"AmazonRDS_{region}.json")
                if os.path.exists(raw_path):
                    try:
                        with open(raw_path, "r") as rf:
                            head = rf.read(1000)
                            match = PUB_DATE_PATTERN.search(head)
                            if match:
                                pub_date = match.group(1)
                    except Exception:
                        pass
            self.pub_dates[f"rds_{region}"] = pub_date
            
        if os.path.exists(rds_storage_processed):
            try:
                with open(rds_storage_processed, "r") as f:
                    self.rds_storage_cache[region] = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load processed RDS storage cache: {e}")

    def _initialize_drs(self, region: str) -> None:
        """Preprocess DRS pricing into cache."""
        drs_processed = os.path.join(self.cache_dir, f"drs_{region}_processed.json")
        if not os.path.exists(drs_processed):
            raw_drs = self.fetch_json_with_cache("AWSElasticDisasterRecovery", region)
            pub_date = ""
            server_hour_rate = PRICING_DEFAULTS["drs_server_hourly"]
            if raw_drs:
                logger.info(f"Preprocessing DRS pricing for {region}...")
                pub_date = raw_drs.get("publicationDate", "")
                products = raw_drs.get("products", {})
                terms = raw_drs.get("terms", {}).get("OnDemand", {})
                for sku, product in products.items():
                    if product.get("productFamily") == "Server DR":
                        price = extract_price_from_terms(terms.get(sku, {}))
                        if price is not None:
                            server_hour_rate = price
                            break
            
            ebs_rates = self.ebs_cache.get(region, {})
            storage_rate = ebs_rates.get("gp3", ebs_rates.get("gp2", PRICING_DEFAULTS["aws_backup_snapshot_gb_rate"]))
            
            self.drs_cache[region] = {
                "publicationDate": pub_date,
                "server_hour_rate": server_hour_rate,
                "storage_gb_month_rate": storage_rate
            }
            try:
                with open(drs_processed, "w") as f:
                    json.dump(self.drs_cache[region], f, indent=2)
            except IOError as e:
                logger.error(f"Failed to write DRS processed cache: {e}")
                
        if os.path.exists(drs_processed):
            try:
                with open(drs_processed, "r") as f:
                    data = json.load(f)
                    self.drs_cache[region] = data
                    self.pub_dates[f"drs_{region}"] = data.get("publicationDate", "")
                    
                    # Perform config update check
                    server_rate = data.get("server_hour_rate")
                    if server_rate is not None:
                        old_rate = PRICING_DEFAULTS.get("drs_server_hourly")
                        if old_rate != server_rate:
                            print(f"[PRICE CHANGED] AWS DRS server hourly rate changed from ${old_rate} to ${server_rate}. Updating config.py...")
                            from .config import update_pricing_default
                            update_pricing_default("drs_server_hourly", server_rate)
            except Exception as e:
                logger.error(f"Failed to load processed DRS cache: {e}")

    def _initialize_vpc(self, region: str) -> None:
        """Preprocess VPC networking pricing (NAT Gateway, VPN, Public IP)."""
        vpc_processed = os.path.join(self.cache_dir, f"vpc_{region}_processed.json")
        if not os.path.exists(vpc_processed):
            logger.info(f"Preprocessing VPC & Networking pricing for {region}...")
            raw_ec2 = self.fetch_json_with_cache("AmazonEC2", region)
            nat_hour = 0.059
            nat_gb = 0.059
            if raw_ec2:
                products = raw_ec2.get("products", {})
                terms = raw_ec2.get("terms", {}).get("OnDemand", {})
                for sku, product in products.items():
                    fam = product.get("productFamily")
                    if fam == "NAT Gateway":
                        attrs = product.get("attributes", {})
                        usage = attrs.get("usagetype", "")
                        price = extract_price_from_terms(terms.get(sku, {}))
                        if price is not None:
                            if "natgateway-hours" in usage.lower():
                                nat_hour = price
                            elif "natgateway-bytes" in usage.lower():
                                nat_gb = price
                                
            # Preprocess VPN and Public IP from VPC pricing sheet
            raw_vpc = self.fetch_json_with_cache("AmazonVPC", region)
            pub_date_vpc = ""
            vpn_s2s = PRICING_DEFAULTS["vpn_site_to_site_hourly"]
            vpn_client_end = PRICING_DEFAULTS["vpn_client_endpoint_hourly"]
            vpn_client_conn = PRICING_DEFAULTS["vpn_client_connection_hourly"]
            pub_ip = PRICING_DEFAULTS["public_ip_hourly"]
            
            if raw_vpc:
                pub_date_vpc = raw_vpc.get("publicationDate", "")
                products = raw_vpc.get("products", {})
                terms = raw_vpc.get("terms", {}).get("OnDemand", {})
                for sku, product in products.items():
                    attrs = product.get("attributes", {})
                    fam = product.get("productFamily")
                    if fam in ("IP Address", "VPN Connection"):
                        usage = attrs.get("usagetype", "")
                        price = extract_price_from_terms(terms.get(sku, {}))
                        if price is not None:
                            if "vpn-connectionhours" in usage.lower():
                                vpn_s2s = price
                            elif "clientvpn-endpoint-hours" in usage.lower():
                                vpn_client_end = price
                            elif "clientvpn-connectionhours" in usage.lower():
                                vpn_client_conn = price
                            elif "publicipv4:inuseaddress" in usage.lower() or "publicipv4:idleaddress" in usage.lower():
                                pub_ip = price
            
            vpc_rates = {
                "publicationDate": pub_date_vpc,
                "nat_gateway_hour": nat_hour,
                "nat_gateway_gb": nat_gb,
                "vpn_site_to_site_hour": vpn_s2s,
                "vpn_client_endpoint_hour": vpn_client_end,
                "vpn_client_connection_hour": vpn_client_conn,
                "public_ip_hour": pub_ip
            }
            try:
                with open(vpc_processed, "w") as f:
                    json.dump(vpc_rates, f, indent=2)
            except IOError as e:
                logger.error(f"Failed to write VPC processed cache: {e}")
                
        if os.path.exists(vpc_processed):
            try:
                with open(vpc_processed, "r") as f:
                    self.vpc_cache[region] = json.load(f)
                    self.pub_dates[f"vpc_{region}"] = self.vpc_cache[region].get("publicationDate", "")
                    
                    # Perform config update checks
                    rates = self.vpc_cache[region]
                    checks = [
                        ("vpn_site_to_site_hourly", "vpn_site_to_site_hour"),
                        ("vpn_client_endpoint_hourly", "vpn_client_endpoint_hour"),
                        ("vpn_client_connection_hourly", "vpn_client_connection_hour"),
                        ("public_ip_hourly", "public_ip_hour")
                    ]
                    for config_key, cache_key in checks:
                        new_rate = rates.get(cache_key)
                        if new_rate is not None:
                            old_rate = PRICING_DEFAULTS.get(config_key)
                            if old_rate != new_rate:
                                print(f"[PRICE CHANGED] AWS VPC rate '{config_key}' changed from ${old_rate} to ${new_rate}. Updating config.py...")
                                from .config import update_pricing_default
                                update_pricing_default(config_key, new_rate)
            except Exception as e:
                logger.error(f"Failed to load processed VPC cache: {e}")

    def _initialize_s3_eks_dt(self, region: str) -> None:
        """Initialize S3, EKS, and Data Transfer directly into caches."""
        raw_s3 = self.fetch_json_with_cache("AmazonS3", region)
        if raw_s3:
            self.s3_cache[region] = raw_s3
            self.pub_dates[f"s3_{region}"] = raw_s3.get("publicationDate", "")
            
        raw_eks = self.fetch_json_with_cache("AmazonEKS", region)
        if raw_eks:
            self.eks_cache[region] = raw_eks
            self.pub_dates[f"eks_{region}"] = raw_eks.get("publicationDate", "")
            
            # Check price and update config dynamically
            products = raw_eks.get("products", {})
            terms = raw_eks.get("terms", {}).get("OnDemand", {})
            for sku, product in products.items():
                attrs = product.get("attributes", {})
                if "percluster" in attrs.get("usagetype", "").lower():
                    price = extract_price_from_terms(terms.get(sku, {}))
                    if price is not None:
                        old_rate = PRICING_DEFAULTS.get("eks_cluster_hourly")
                        if old_rate != price:
                            print(f"[PRICE CHANGED] AWS EKS Cluster hourly fee changed from ${old_rate} to ${price}. Updating config.py...")
                            from .config import update_pricing_default
                            update_pricing_default("eks_cluster_hourly", price)
            
        raw_dt = self.fetch_json_with_cache("AWSDataTransfer", region)
        if raw_dt:
            self.dt_cache[region] = raw_dt
            self.pub_dates[f"data_transfer_{region}"] = raw_dt.get("publicationDate", "")

    def _initialize_efs(self, region: str) -> None:
        """Preprocess AWS EFS pricing into cache."""
        efs_processed = os.path.join(self.cache_dir, f"efs_{region}_processed.json")
        if not os.path.exists(efs_processed):
            raw_efs = self.fetch_json_with_cache("AmazonEFS", region)
            efs_rates = dict(PRICING_DEFAULTS["aws_efs_default_rates"])
            if raw_efs:
                logger.info(f"Preprocessing EFS pricing for {region}...")
                pub_date = raw_efs.get("publicationDate", "")
                products = raw_efs.get("products", {})
                terms = raw_efs.get("terms", {}).get("OnDemand", {})
                for sku, product in products.items():
                    attrs = product.get("attributes", {})
                    sc = attrs.get("storageClass", "")
                    ut = attrs.get("usagetype", "")
                    
                    if product.get("productFamily") == "Storage":
                        price = extract_price_from_terms(terms.get(sku, {}))
                        if price is not None:
                            if sc == "General Purpose" and "TimedStorage-ByteHrs" in ut:
                                efs_rates["standard"] = price
                            elif sc == "Infrequent Access" and "IATimedStorage-ByteHrs" in ut:
                                efs_rates["ia"] = price
                            elif sc == "Archive" and "ArchiveTimedStorage-ByteHrs" in ut:
                                efs_rates["archive"] = price
                                
                efs_rates["publicationDate"] = pub_date
            
            try:
                with open(efs_processed, "w") as f:
                    json.dump(efs_rates, f, indent=2)
            except IOError as e:
                logger.error(f"Failed to write EFS processed cache: {e}")
                
        if os.path.exists(efs_processed):
            try:
                with open(efs_processed, "r") as f:
                    data = json.load(f)
                    self.efs_cache[region] = data
                    self.pub_dates[f"efs_{region}"] = data.get("publicationDate", "")
                    
                    # Perform config update checks
                    old_efs_rates = PRICING_DEFAULTS.get("aws_efs_default_rates", {})
                    updated_rates = {}
                    for k in ["standard", "ia", "archive"]:
                        if k in data and data[k] != old_efs_rates.get(k):
                            print(f"[PRICE CHANGED] AWS EFS {k} storage rate changed from ${old_efs_rates.get(k)} to ${data[k]}. Updating config.py...")
                            updated_rates[k] = data[k]
                    if updated_rates:
                        from .config import update_pricing_default
                        update_pricing_default("aws_efs_default_rates", updated_rates)
            except Exception as e:
                logger.error(f"Failed to load processed EFS cache: {e}")

    def get_ec2_price(self, region: str, instance_type: str, os_name: str = "Linux") -> Tuple[float, str, int, float]:
        """Get pricing for explicit EC2 instance type in O(1) time."""
        region_cache = self.ec2_cache.get(region, {})
        inst = region_cache.get(instance_type)
        if inst:
            price = inst["prices"].get(os_name)
            return price if price is not None else 0.0, inst["instanceType"], inst["vcpu"], inst["memory_gb"]
        return 0.0, "unknown", 0, 0.0

    def get_ebs_price(self, region: str, volume_type: str = "gp3") -> float:
        """Get EBS storage GB-month rate."""
        volume_type = volume_type.lower()
        region_cache = self.ebs_cache.get(region, {})
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

    def get_rds_price(self, region: str, instance_type: str, engine: str = "PostgreSQL", deploy: str = "Single-AZ") -> Tuple[float, int, float]:
        """Get RDS database instance hourly rate and specs in O(1) time."""
        region_cache = self.rds_cache.get(region, {})
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
            
        key = (instance_type, deploy, engine_match)
        inst = region_cache.get(key)
        if inst:
            return inst["price"], inst["vcpu"], inst["memory_gb"]
            
        # Safe fallback loop if key formatting doesn't perfectly match (e.g. databaseEngine holds composite version string)
        for (inst_type, dep, db_eng), inst in region_cache.items():
            if inst_type == instance_type and dep == deploy and engine_match in db_eng:
                return inst["price"], inst["vcpu"], inst["memory_gb"]
                
        return 0.0, 0, 0.0

    def get_rds_storage_price(self, region: str, volume_type: str = "gp3", deploy: str = "Single-AZ") -> float:
        """Get RDS storage GB-month rate."""
        volume_type = volume_type.lower()
        region_cache = self.rds_storage_cache.get(region, {})
        
        deploy_map = {
            "single-az": "Single-AZ",
            "multi-az": "Multi-AZ"
        }
        deploy_key = deploy_map.get(deploy.lower(), "Single-AZ")
        
        defaults = {
            "gp3": 0.138 if deploy_key == "Single-AZ" else 0.276,
            "gp2": 0.138 if deploy_key == "Single-AZ" else 0.276,
            "io1": 0.138 if deploy_key == "Single-AZ" else 0.276,
            "io2": 0.138 if deploy_key == "Single-AZ" else 0.276,
            "magnetic": 0.110 if deploy_key == "Single-AZ" else 0.220
        }
        
        type_rates = region_cache.get(volume_type, {})
        return type_rates.get(deploy_key, defaults.get(volume_type, 0.0))

    def get_s3_price(self, region: str, storage_class: str = "Standard") -> float:
        """Get S3 storage class GB-month rate."""
        s3_data = self.s3_cache.get(region)
        if not s3_data:
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
        
        sc_lower = storage_class.lower()
        target_sc = "General Purpose"
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
                    if "fa-bytehrs" not in usage.lower(): continue
                    
                price = extract_price_from_terms(terms.get(sku, {}))
                if price is not None:
                    return price
                    
        return 0.025

    def get_eks_price(self, region: str) -> float:
        """Get EKS hourly cluster fee."""
        eks_data = self.eks_cache.get(region)
        if eks_data:
            products = eks_data.get("products", {})
            terms = eks_data.get("terms", {}).get("OnDemand", {})
            for sku, product in products.items():
                attrs = product.get("attributes", {})
                if "percluster" in attrs.get("usagetype", "").lower():
                    price = extract_price_from_terms(terms.get(sku, {}))
                    if price is not None:
                        return price
        return PRICING_DEFAULTS["eks_cluster_hourly"]

    def get_drs_price(self, region: str, servers: int, storage_gb: float, volume_type: str = "snapshot", hours: int = 730) -> Tuple[float, float, float]:
        """Get AWS DRS cost based on number of servers and storage GB per server."""
        drs_data = self.drs_cache.get(region, {})
        if not drs_data:
            drs_data = {"server_hour_rate": PRICING_DEFAULTS["drs_server_hourly"], "storage_gb_month_rate": PRICING_DEFAULTS["aws_backup_snapshot_gb_rate"]}
    
        server_rate = drs_data.get("server_hour_rate", PRICING_DEFAULTS["drs_server_hourly"])
        if volume_type and volume_type.lower() == 'snapshot':
            storage_rate = PRICING_DEFAULTS["aws_backup_snapshot_gb_rate"]
        else:
            storage_rate = self.get_ebs_price(region, volume_type) if volume_type else drs_data.get("storage_gb_month_rate", PRICING_DEFAULTS["aws_backup_snapshot_gb_rate"])
        
        server_cost = servers * server_rate * hours
        storage_cost = servers * storage_gb * storage_rate
        total_cost = server_cost + storage_cost
        
        return total_cost, server_cost, storage_cost

    def get_nat_gateway_price(self, region: str) -> Tuple[float, float]:
        """Get NAT Gateway hourly fee and per-GB processing fee."""
        vpc_data = self.vpc_cache.get(region, {})
        hour_rate = vpc_data.get("nat_gateway_hour", 0.059)
        gb_rate = vpc_data.get("nat_gateway_gb", 0.059)
        return hour_rate, gb_rate

    def get_vpn_price(self, region: str, vpn_type: str = "site-to-site") -> float:
        """Get VPN hourly rate based on type (site-to-site, client-endpoint, client-connection)."""
        vpc_data = self.vpc_cache.get(region, {})
        type_lower = vpn_type.lower()
        if "client-endpoint" in type_lower:
            return vpc_data.get("vpn_client_endpoint_hour", PRICING_DEFAULTS["vpn_client_endpoint_hourly"])
        elif "client-connection" in type_lower:
            return vpc_data.get("vpn_client_connection_hour", PRICING_DEFAULTS["vpn_client_connection_hourly"])
        else:
            return vpc_data.get("vpn_site_to_site_hour", PRICING_DEFAULTS["vpn_site_to_site_hourly"])

    def get_public_ip_price(self, region: str) -> float:
        """Get Public IPv4 / Elastic IP hourly rate."""
        vpc_data = self.vpc_cache.get(region, {})
        return vpc_data.get("public_ip_hour", PRICING_DEFAULTS["public_ip_hourly"])

    def resolve_custom_ec2(self, region: str, vcpu: float, memory_gb: float, os_name: str = "Linux", preferred_arch: str = "x86_64") -> Tuple[float, str, int, float]:
        """Find the cheapest EC2 instance that meets vCPU and Memory specs in O(1) keyed cache values list."""
        region_cache = self.ec2_cache.get(region, {})
        matches = []
        for inst in region_cache.values():
            if preferred_arch != "any" and inst["architecture"] != preferred_arch:
                continue
            
            if inst["vcpu"] >= vcpu and inst["memory_gb"] >= memory_gb:
                price = inst["prices"].get(os_name)
                if price is not None:
                    matches.append(inst)
                    
        if not matches:
            if preferred_arch != "any":
                return self.resolve_custom_ec2(region, vcpu, memory_gb, os_name, "any")
            return 0.0, "unknown", 0, 0.0
            
        matches.sort(key=lambda x: x["prices"].get(os_name, float('inf')))
        best = matches[0]
        return best["prices"][os_name], best["instanceType"], best["vcpu"], best["memory_gb"]

    def get_dt_tiers(self, region: str) -> List[Dict[str, float]]:
        """Get tiered egress rates for Data Transfer."""
        dt_data = self.dt_cache.get(region)
        region_name = REGION_MAP.get(region, "Asia Pacific (Jakarta)")
        
        default_tiers = [
            {"begin": 0.0, "end": 10240.0, "price": 0.132},
            {"begin": 10240.0, "end": 51200.0, "price": 0.100},
            {"begin": 51200.0, "end": 153600.0, "price": 0.095},
            {"begin": 153600.0, "end": float('inf'), "price": 0.090}
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

    def calculate_dt_cost(self, region: str, size_gb: float) -> Tuple[float, str]:
        """Calculate tiered egress cost for Data Transfer."""
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
