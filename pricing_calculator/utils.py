import re
import logging
from typing import Union, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

# Precompiled regex patterns
MEMORY_PATTERN = re.compile(r"([0-9.]+)")
FAMILY_SUFFIX_PATTERN = re.compile(r"\d+([a-z]+)")

def parse_memory(mem_str: Union[str, int, float]) -> float:
    """Parse memory string (e.g. '16 GiB', '4,096 MB') to float GB."""
    if not mem_str:
        return 0.0
    if isinstance(mem_str, (int, float)):
        return float(mem_str)
    
    try:
        mem_clean = str(mem_str).replace(",", "").lower()
        match = MEMORY_PATTERN.search(mem_clean)
        if not match:
            logger.warning(f"Could not parse memory value: {mem_str}")
            return 0.0
        
        val = float(match.group(1))
        if "mb" in mem_clean or "mib" in mem_clean:
            return val / 1024.0
        return val
    except Exception as e:
        logger.error(f"Error parsing memory '{mem_str}': {e}", exc_info=True)
        return 0.0

def normalize_os_or_engine(service: str, val: Optional[str]) -> str:
    """Normalize input OS or DB engine to match standard AWS values."""
    service_lower = str(service).lower().strip()
    if not val:
        return "PostgreSQL" if service_lower == "rds" else "Linux"
    val_lower = str(val).lower().strip()
    
    if service_lower == "rds":
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
    else:
        # Treat as compute OS (Linux, Windows, RHEL, SUSE)
        if "windows" in val_lower:
            return "Windows"
        elif "rhel" in val_lower or "red hat" in val_lower:
            return "RHEL"
        elif "suse" in val_lower:
            return "SUSE"
        elif "linux" in val_lower or "ubuntu" in val_lower or "centos" in val_lower or "debian" in val_lower:
            return "Linux"
        return "Linux"

def get_architecture(instance_type: str, physical_processor: str = "") -> str:
    """Identify if instance type is arm64 (Graviton) or x86_64."""
    if "graviton" in str(physical_processor).lower():
        return "arm64"
    parts = instance_type.split('.')
    if len(parts) > 0:
        family = parts[0]
        match = FAMILY_SUFFIX_PATTERN.search(family)
        if match:
            suffix = match.group(1)
            if 'g' in suffix:
                return "arm64"
    return "x86_64"

def round_fargate_specs(requested_vcpu: float, requested_mem_gb: float) -> Tuple[float, float]:
    """
    Round vCPU and Memory configurations up to Fargate specs with 256MB overhead.
    Fargate EKS overhead is 256MB (0.256 GB) per pod.
    """
    mem_with_overhead = requested_mem_gb + 0.256
    
    if requested_vcpu <= 0.25:
        billed_vcpu = 0.25
        if mem_with_overhead <= 0.5:
            billed_mem = 0.5
        elif mem_with_overhead <= 1.0:
            billed_mem = 1.0
        else:
            billed_mem = 2.0
    elif requested_vcpu <= 0.5:
        billed_vcpu = 0.5
        billed_mem = max(1.0, min(4.0, float(int(mem_with_overhead + 0.99))))
    elif requested_vcpu <= 1.0:
        billed_vcpu = 1.0
        billed_mem = max(2.0, min(8.0, float(int(mem_with_overhead + 0.99))))
    elif requested_vcpu <= 2.0:
        billed_vcpu = 2.0
        billed_mem = max(4.0, min(16.0, float(int(mem_with_overhead + 0.99))))
    else:
        billed_vcpu = 4.0
        billed_mem = max(8.0, min(30.0, float(int(mem_with_overhead + 0.99))))
    return billed_vcpu, billed_mem

def extract_price_from_terms(sku_terms: Dict[str, Any]) -> Optional[float]:
    """Extract USD price per unit from OnDemand SKU terms."""
    if not sku_terms:
        return None
    for term_val in sku_terms.values():
        price_dimensions = term_val.get("priceDimensions", {})
        for dim_val in price_dimensions.values():
            price_str = dim_val.get("pricePerUnit", {}).get("USD")
            if price_str is not None:
                return float(price_str)
    return None
