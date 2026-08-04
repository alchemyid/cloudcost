# AWS Cloud Cost Pricing Calculator

A robust Python command-line application that reads infrastructure requirements from a CSV or Excel file, queries regional pricing data from the AWS Price List API, and outputs a detailed monthly cost estimate report.

The application works **100% offline** by downloading and caching AWS bulk pricing database sheets locally, meaning it does **not require any AWS account or credentials** to run.

---

## Supported Services & Features

| Service | Feature Description | Input Configuration Details |
| :--- | :--- | :--- |
| **EC2** (Compute) | Matches `custom` vCPU & RAM requirements to the cheapest instance type in the region, or looks up explicit types (e.g. `t3.medium`). Supports OS billing for **Linux**, **Windows**, **RHEL**, and **SUSE**. | `service = ec2`<br>Provide `vcpu` & `memory_gb` (for custom specs matching), or type (e.g., `t3.medium`). Provide `os_or_engine` (e.g., `Ubuntu Linux (64-bit)`).<br>*(Optional)* Add `size_gb` to include attached `gp3` storage pricing in the row cost. |
| **EBS** (Storage) | Calculates monthly EBS storage costs for **gp3**, **gp2**, **io1**, **io2**, **st1**, **sc1**, and **magnetic** volumes. | `service = ebs`<br>Provide volume class in `type` (e.g., `gp3`) and volume size in `size_gb`. |
| **RDS** (Database) | Calculates database instance cost by database engine (**PostgreSQL**, **MySQL**, **MariaDB**, **Oracle**, **SQL Server**) and automatically computes gp3 Single-AZ database storage costs if storage size is specified. | `service = rds`<br>Provide instance type in `type` (e.g., `db.t3.medium`), database engine in `os_or_engine` (e.g., `PostgreSQL 14`), and database storage in `size_gb`. |
| **S3** (Object Storage)| Calculates object storage costs for **Standard**, **Infrequent Access (Standard-IA)**, **One Zone-IA**, **Glacier**, **Glacier Deep Archive**, and **Intelligent Tiering** classes. | `service = s3`<br>Provide storage class in `type` (e.g., `Standard`) and storage size in `size_gb`. |
| **EKS** (Kubernetes) | Estimates the hourly Kubernetes control plane management fee ($0.10/hour). | `service = eks`<br>Provide number of clusters in `quantity` (defaults to 1). |
| **Data Transfer** | Computes tiered egress bandwidth costs to the internet, applying region-specific rates (e.g. Jakarta ap-southeast-3 tiers) and automatically discounting the **100 GB global free tier**. | `service = data_transfer`<br>Provide egress volume in `size_gb`. |
| **DRS** (Disaster Recovery) | Calculates AWS Elastic Disaster Recovery fee (per server replication hour) and staging storage costs (dynamic EBS pricing based on the configured volume type, defaulting to `gp3`). | `service = drs`<br>Provide replication storage size per server in `size_gb`, number of servers in `quantity`, volume type in `type` (e.g., `gp3`), and replication hours in `hours_per_month` (defaults to 730 for 24/7). |
| **DRS Drill** (Recovery Test) | Calculates the pro-rated cost of launching recovery drill instances (EC2 compute + pro-rated EBS storage) during a DR drill test. | `service = drs_drill` or `drs-drill`<br>Provide vCPU/memory specs (or instance type in `type`), drill storage size per server in `size_gb`, number of drill servers in `quantity`, and drill hours in `hours_per_month` (e.g., `20`). |
| **NAT Gateway** | Calculates the hourly fee per NAT Gateway + data processing fee per GB. | `service = nat` or `nat_gateway`<br>Provide NAT Gateway hours in `hours_per_month` (defaults to 730 for 24/7), number of gateways in `quantity` (defaults to 1), and data processed in `size_gb` to include per-GB charges. |
| **ALB** (Load Balancer) | Calculates AWS Application Load Balancer fixed hourly fee + Load Balancer Capacity Units (LCU) fee based on the volume of processed data. | `service = alb` or `elb` or `load_balancer`<br>Provide processed data in `size_gb` to calculate LCU costs, number of load balancers in `quantity`, and billing hours in `hours_per_month` (defaults to 730). |
| **VPN Connection** | Site-to-Site VPN or Client VPN connection costs. | `service = vpn` or `vpn_connection`<br>Provide VPN type in `type` (`site-to-site` for Site-to-Site VPN, `client-endpoint` for Client VPN Endpoints, or `client-connection` for Client VPN Connections), and connection hours in `hours_per_month` (defaults to 730 for 24/7). |
| **Public Static IP (Elastic IP)** | Billed per public IPv4 address per hour (standard AWS charge of $0.005/hr). | `service = eip` or `public_ip`<br>Provide IP status in `type` (`in-use` or `idle`), number of IPs in `quantity`, and billing hours in `hours_per_month` (defaults to 730). |
| **Azure Services** (Virtual Machines & Storage) | Estimates Azure VM instances (Standard PAYG rates fetched dynamically via Azure Prices API) and Azure Storage volumes (Managed Disks, Blob Storage) using regional rates. | `service = azure`<br>For VMs: provide SKU in `type` (e.g. `Standard_D2s_v5`), operating system in `os_or_engine` (`Linux` or `Windows`).<br>For Storage: provide disk or storage type in `type` (e.g., `Premium_SSD`, `Standard_SSD`, `Standard_HDD`, `Blob Storage`) and size in `size_gb`. |
| **Backup / Snapshots** | Calculates monthly backup costs for AWS (EBS Snapshots) and Azure (VM Backup) with user-configurable retention and incremental change rates. | `service = backup`, `aws-backup`, or `azure-backup`<br>Provide source VM/volume size in `size_gb`, quantity in `quantity`. Configure retention count and incremental change rate % in the `type` column using a readable string (e.g., `3-retention-100%` or `4-copies-10%`). |

---

## Installation & Setup

1. **Prerequisites:** Python 3.8+ installed.
2. **Virtual Environment Setup:**
   ```bash
   # Create a virtual environment
   python3 -m venv pyenv
   
   # Activate the environment
   source pyenv/bin/activate
   
   # Install required dependencies
   pip install pandas openpyxl requests tabulate
   ```

---

## How to Run the Calculator

Execute the script from the workspace directory by passing the path of your input spreadsheet:

```bash
# Process a CSV file (generates template_cost_estimate.csv)
./pyenv/bin/python pricing_calculator.py template.csv

# Process an Excel file (generates template_cost_estimate.xlsx)
./pyenv/bin/python pricing_calculator.py template.xlsx

# Change target region and force matching with an ARM64 (Graviton) architecture (default is x86_64)
./pyenv/bin/python pricing_calculator.py template.csv --region ap-southeast-1 --architecture arm64

# Force cache clear and redownload pricing databases
./pyenv/bin/python pricing_calculator.py template.csv --clear-cache
```

### CLI Arguments
* `input_file`: Path to the input CSV or Excel file.
* `--output`: Optional custom name/path for the output report file.
* `--region`: Default AWS region code to use (default: `ap-southeast-3` - Jakarta).
* `--architecture`: Preferred processor architecture for custom specs matching (`x86_64`, `arm64`, or `any`) (default: `x86_64`).
* `--cache-dir`: Directory where AWS price database sheets are saved (default: `.cache`).
* `--clear-cache`: Clears both raw and preprocessed pricing files from the cache directory to force a fresh download of the latest databases from the AWS Price List API.

### Caching Mechanism

To optimize performance and enable 100% offline usage, the calculator implements a two-tier caching mechanism:
1. **First-time Query / Cold Start**: The engine downloads the raw pricing database for each active service from the AWS Price List API bulk endpoint and stores it locally under `--cache-dir` as `{Service}_{region}.json`. It then parses the relevant pricing dimensions and structures them into a lightweight file named `{service}_{region}_processed.json`.
2. **Subsequent Queries / Cache Hit**: The engine directly reads the processed and cached prices, avoiding any network calls or repetitive large-file processing.
3. **Forced Cache Update**: When the `--clear-cache` parameter is passed, all cached raw and processed files in `--cache-dir` are deleted. On the next execution, the calculator will fetch fresh pricing sheets from AWS and rebuild the caches.

---

## Backup & Snapshot Cost Estimation

The `backup` (AWS) and `azure-backup` (Azure) services allow you to estimate snapshot storage costs. You configure this by writing a policy string in the `type` column (e.g., `4-snapshots-5%` or `1-snapshot-100%`).

### Understanding the Parameters:
1. **Retention Count (The first number)**: The total number of recovery points (snapshots) kept in storage **at any given time**. 
   * Regardless of frequency (daily, weekly, or monthly), only the snapshots currently stored in the cloud are billed.
   * If you set **`1-snapshot`**: You only keep 1 snapshot. When the next backup is created (e.g., in week 2), the older backup (from week 1) is deleted, meaning you are only billed for **exactly 1 snapshot** at any time.
   * If you set **`3-snapshots`**: You keep 3 snapshots. When the 4th backup is created, the 1st one is deleted, meaning you are billed for **exactly 3 snapshots** at any time.
2. **Incremental Change Rate % (The second number)**: The percentage of data that changes between backups.
   * **100% (Full Copies)**: No incremental compression. Each snapshot is a full copy. (e.g., keeping 3 full snapshots of a 100 GB volume will bill for $3 \times 100\text{ GB} = 300\text{ GB}$).
   * **10% (Incremental)**: Standard cloud backup behavior. The first snapshot is full (100% size), and subsequent snapshots only store changed blocks (e.g., keeping 3 incremental snapshots of a 100 GB volume with 10% change rate will bill for $100\text{ GB} + 10\text{ GB} + 10\text{ GB} = 120\text{ GB}$).

---

## Input Formats

The calculator automatically supports **two input formats**:

### 1. Direct VM Specification Import (e.g., from VMware)
If the calculator detects columns named `VM`, `CPUs`, `Memory`, and `Provisioned MiB`, it automatically parses the file as a custom virtual machine list.
* **VM**: Handled as the instance ID/name.
* **CPUs**: Handled as target vCPUs.
* **Memory**: Handled as target RAM (automatically detects if in MB or GB, e.g. `4,096` MB maps to `4.0` GB).
* **Provisioned MiB**: Handled as storage size (automatically converted to GB, e.g. `225,400` MiB maps to `220.1` GB).

For each VM row in this format, the calculator generates **two items** in the output report:
1. **EC2 Compute row:** Resolved to the cheapest instance meeting or exceeding the CPUs and Memory requirements.
2. **EBS Storage row:** Calculated as standard attached `gp3` storage volume.

### 2. Standard Calculator Format
A template CSV with these columns is provided as [template.csv](file:///Users/girirahayu/Documents/cloud_pricing/template.csv) and [template.xlsx](file:///Users/girirahayu/Documents/cloud_pricing/template.xlsx).

| Column Name | Status | Description | Default / Fallback Value |
| :--- | :--- | :--- | :--- |
| `id` | **Mandatory** | Unique row identifier (e.g. `web-server`, `db-storage`) | *(None - must be provided)* |
| `service` | **Mandatory** | Cloud service code (`ec2`, `ebs`, `rds`, `s3`, `eks`, `data_transfer`, `drs`, `nat`, `vpn`, `eip`, `azure`, `backup`, `aws-backup`, `azure-backup`) | *(None - must be provided)* |
| `region` | **Optional** | Cloud region code (e.g., `ap-southeast-3`, `southeastasia`, `us-east-1`) | `ap-southeast-3` (AWS) / `southeastasia` (Azure) |
| `type` | **Optional** | Instance type (e.g., `t3.medium`, `Standard_D2s_v5`), volume type (`gp3`), or storage class (`Standard`). Leave blank or enter `custom` for EC2/Azure VM specs matching. | `custom` |
| `vcpu` | **Optional** | Number of requested vCPUs (used for compute specs matching).<br>**For Backup:** Serves as **retention count** (number of recovery points to keep). | `0` (Compute)<br>`4` (Backup default) |
| `memory_gb` | **Optional** | Gigabytes of requested RAM (used for compute specs matching).<br>**For Backup:** Serves as **incremental change rate %** per snapshot. | `0.0` (Compute)<br>`10.0` (Backup default) |
| `os_or_engine` | **Optional** | Operating System for EC2/Azure (`Linux`, `Windows`, `RHEL`, `SUSE`) or Database Engine for RDS (`PostgreSQL`, `MySQL`, `MariaDB`, `Oracle`, `SQL Server`).<br>**Smart Mapping:** Accepts vendor names (e.g., `Ubuntu Linux (64-bit)` $\rightarrow$ `Linux`, `Microsoft Windows Server 2022` $\rightarrow$ `Windows`, `PostgreSQL 14` $\rightarrow$ `PostgreSQL`). | `Linux` (for EC2/Azure)<br>`PostgreSQL` (for RDS) |
| `size_gb` | **Optional** | Storage size in GB (for EBS, RDS storage, S3, Azure storage) or egress volume in GB (for Data Transfer / NAT Gateway processing) | `0.0` |
| `quantity` | **Optional** | Number of instances/volumes | `1` |
| `hours_per_month` | **Optional** | Monthly operational hours (24/7 is 730 hours) | `730` |
| `description` | **Optional** | Optional text note | `""` |

---

## Output Reports

The generated Excel/CSV cost estimate reports include the following columns:
* **User Input Fields:** `id`, `service`, `region`, `type`, `os_or_engine`, `size_gb`, `quantity`, `hours_per_month`, `description`.
* **Requested specs vs Fitted specs:**
  * `requested_vcpu` & `requested_memory_gb`: The original requirements entered by the user.
  * `matched_vcpu` & `matched_memory_gb`: The actual capacity of the matched AWS cloud instance (e.g., requesting 4 vCPU/4 GB matches `c7i-flex.xlarge` which has 4 vCPU/8.0 GB RAM).
* **Cost Details:**
  * `matched_type`: The resolved instance type/class (e.g. `t3.small`).
  * `unit_price`: The hourly billing rate or GB-month storage rate.
  * `monthly_price`: The calculated monthly charge in USD.
  * `notes`: Comprehensive explanations (e.g., `"Cheapest x86_64 matching >= 4 vCPU, 4.0 GB RAM (Linux)"` or tiered egress details).

### Summary & Audit Rows
The exported reports automatically append two audit rows at the end of the data lines:
1. **`TOTAL_ESTIMATED_MONTHLY_COST`**: Records the total sum of all calculated monthly costs.
2. **`AWS_PRICING_DATABASE_VERSION`**: Lists the exact database publication dates from AWS for every service used, establishing a robust audit trail.
