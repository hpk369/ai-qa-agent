# VM setup — Oracle Cloud host for the Hadoop stack

Manual provisioning steps for roadmap B2 / Phase 1 of
[`../IMPLEMENTATION_GUIDE.md`](../IMPLEMENTATION_GUIDE.md) §4.2. Run the parts in order.

| Part | Platform | Steps | When |
|---|---|---|---|
| A | OCI Console (browser) | 1–8 | now |
| B | Local machine (terminal) | 9–11 | now |
| C | VM shell (over SSH) | 12–16 | now |
| D | Tailscale admin console (browser) | 17–19 | now |
| E | GitHub (browser) | 20 | now |
| F | Local machine (terminal) | 21–23 | now |
| G | Cloudflare dashboard (browser) | 24–29 | before B6.1, not needed to deploy |

Values used throughout: compartment `bankdemo`, instance name/hostname `bankdemo`,
OS user `bankops`, data mount `/data`.

DNS names on the `inkandinfra.com` zone:

| Hostname | Serves | Configured in |
|---|---|---|
| `bundles.inkandinfra.com` | run bundle `.tar.gz` downloads from R2 | step 26 |
| `api.inkandinfra.com` | demo-broker Worker, `/demo/*` | step 29 (B6.3, optional) |
| `demo.inkandinfra.com` | GitHub Pages demo site | step 29 (optional) |
| `triage.inkandinfra.com` | Track A Slack webhook over Cloudflare Tunnel | `../../../docs/PHASE1_SETUP.md` §5 |

---

# Part A — OCI Console

Sign in at [cloud.oracle.com](https://cloud.oracle.com). If you have no account, click
**Start for free** and complete signup (a credit card is required for identity verification).

Every page in this part has a **Compartment** selector in the left sidebar or form. Set it to
`bankdemo` after step 2 — it resets to root between pages.

## 1. Record the Ampere A1 limits

1. Click the **☰ hamburger menu** (top left).
2. Go to **Governance & Administration → Limits, Quotas and Usage**.
3. Set **Service** = `Compute`, **Scope** = your home region.
4. Filter for `standard-a1-core-count` and `standard-a1-memory-count`.
5. Write both numbers into `docs/PROGRESS.md` under B2. Use them as the shape size in step 5.

## 2. Create a budget alert

1. **☰ Menu → Billing & Cost Management → Budgets**.
2. Click **Create Budget**.

| Field | Value |
|---|---|
| Budget scope | Compartment |
| Target compartment | your root compartment |
| Name | `bankdemo-guard` |
| Monthly budget amount | `1` |
| Alert rule — threshold type | Actual spend |
| Alert rule — threshold | `100` % |
| Email recipients | your address |

3. Click **Create**.

## 3. Create the compartment

1. **☰ Menu → Identity & Security → Compartments**.
2. Click **Create Compartment**.

| Field | Value |
|---|---|
| Name | `bankdemo` |
| Description | Hadoop triage rig — ai-qa-agent Track B |
| Parent compartment | your root compartment |

3. Click **Create Compartment**.

## 4. Create the network

1. **☰ Menu → Networking → Virtual Cloud Networks**.
2. Set the **Compartment** selector to `bankdemo`.
3. Click **Start VCN Wizard** → select **Create VCN with Internet Connectivity** → **Start VCN Wizard**.

| Field | Value |
|---|---|
| VCN name | `bankdemo-vcn` |
| Compartment | `bankdemo` |
| VCN CIDR block | `10.0.0.0/16` (default) |
| Public subnet CIDR block | `10.0.0.0/24` (default) |
| Private subnet CIDR block | leave default |

4. Click **Next** → **Create**. Wait for all resources to report **Completed**.

## 5. Set the ingress rule

Get your public IP first (on your own machine): `curl -s ifconfig.me`

1. **☰ Menu → Networking → Virtual Cloud Networks** → click `bankdemo-vcn`.
2. In the left **Resources** panel, click **Security Lists**.
3. Click **Default Security List for bankdemo-vcn**.
4. In the left **Resources** panel, click **Ingress Rules**.
5. Select every existing rule → **Remove**.
6. Click **Add Ingress Rules** and add exactly one:

| Field | Value |
|---|---|
| Stateless | unchecked |
| Source Type | CIDR |
| Source CIDR | `<your-ip>/32` |
| IP Protocol | TCP |
| Source Port Range | leave blank |
| Destination Port Range | `22` |
| Description | Admin SSH and web-UI tunnels |

7. Click **Add Ingress Rules**.
8. Click **Egress Rules** and confirm the default `0.0.0.0/0` all-protocols rule is present. Leave it unchanged.

Add no other ingress rule. Web UIs (9870, 8088, 18080) are reached by SSH tunnel in step 23.

## 6. Create the instance

1. **☰ Menu → Compute → Instances** → set **Compartment** to `bankdemo` → click **Create instance**.
2. Fill in **Basic information**:

| Field | Value |
|---|---|
| Name | `bankdemo` |
| Create in compartment | `bankdemo` |

3. Under **Image and shape**, click **Change image** → select **Oracle Linux** → **Oracle Linux 9** → confirm the build is **aarch64** → **Select image**.
4. Click **Change shape** → **Ampere** tab → **VM.Standard.A1.Flex** → set OCPUs and memory to the values from step 1 → **Select shape**. Confirm the **Always Free-eligible** badge is shown.
5. Under **Primary VNIC information**:

| Field | Value |
|---|---|
| Primary network | Select existing virtual cloud network → `bankdemo-vcn` |
| Subnet | the **public** subnet |
| Public IPv4 address | **Assign a public IPv4 address** |

6. Under **Add SSH keys**, select **Paste public keys** and leave the field open — generate the key in step 9, paste it, then return here.
7. Under **Boot volume**, leave all defaults.
8. Click **Create**. Wait for **State: Running**.
9. On the instance details page, copy the **Public IP address**.

If creation fails with **Out of host capacity**, see [Troubleshooting](#troubleshooting).

## 7. Enable the serial console

1. On the instance details page, open the left **Resources** panel → **Console connection**.
2. Click **Create local connection** → paste the same public key → **Create console connection**.

## 8. Create and attach the block volume

1. **☰ Menu → Storage → Block Volumes** → **Create Block Volume**.

| Field | Value |
|---|---|
| Name | `bankdemo-data` |
| Compartment | `bankdemo` |
| Availability domain | same AD as the instance |
| Volume size | `50` GB |
| Volume performance | **Balanced** |

2. Click **Create Block Volume**. Wait for **State: Available**.
3. **☰ Menu → Compute → Instances** → click `bankdemo`.
4. Left **Resources** panel → **Attached block volumes** → **Attach block volume**.

| Field | Value |
|---|---|
| Volume | Select volume → `bankdemo-data` |
| Attachment type | **Paravirtualized** |
| Access | Read/write |

5. Click **Attach**. Wait for **State: Attached**.

---

# Part B — Local machine

## 9. Generate the SSH keypair

```bash
ssh-keygen -t ed25519 -f ~/.ssh/bankdemo -C "bankdemo-oci"
cat ~/.ssh/bankdemo.pub
```

Paste the printed key into the console fields in steps 6.6 and 7.2.

## 10. Add an SSH host alias

Append to `~/.ssh/config`:

```
Host bankdemo
    HostName <public-ip>
    User bankops
    IdentityFile ~/.ssh/bankdemo
```

## 11. Log in as `opc`

```bash
ssh -i ~/.ssh/bankdemo opc@<public-ip>
```

Stay in this session for Part C.

---

# Part C — VM shell

Run every command in this part on the VM, logged in as `opc`.

## 12. Format the block volume

```bash
# 1. List devices. The 50G device must show no mountpoint and no partitions.
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,LABEL

# 2. Resolve the stable device path and cross-check it against the 50G device above.
ls -l /dev/oracleoci/
DEV=/dev/oracleoci/oraclevdb
readlink -f "$DEV"

# 3. Confirm the device is empty. Any output means STOP — re-check step 2.
sudo blkid "$DEV" || echo "no filesystem found — safe to format"

# 4. Format.
sudo mkfs.xfs "$DEV"
```

## 13. Mount `/data`

```bash
sudo mkdir -p /data
UUID=$(sudo blkid -s UUID -o value /dev/oracleoci/oraclevdb)
echo "UUID=$UUID  /data  xfs  defaults,nofail  0  2" | sudo tee -a /etc/fstab
sudo mount -a
df -h /data          # expect ~50G mounted on /data
```

## 14. Create the `bankops` user

```bash
sudo useradd -m bankops
sudo install -d -m 700 -o bankops -g bankops /home/bankops/.ssh
sudo cp /home/opc/.ssh/authorized_keys /home/bankops/.ssh/authorized_keys
sudo chown bankops:bankops /home/bankops/.ssh/authorized_keys
sudo chmod 600 /home/bankops/.ssh/authorized_keys
```

## 15. Grant bootstrap sudo

```bash
echo 'bankops ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/99-bankops-bootstrap
sudo visudo -cf /etc/sudoers.d/99-bankops-bootstrap    # must print "parsed OK"
sudo chmod 440 /etc/sudoers.d/99-bankops-bootstrap
```

Phase 8 (`../IMPLEMENTATION_GUIDE.md` §11.1) replaces this file with a restricted rule. Leave
the `opc` user and its default sudo in place until then.

## 16. Install Tailscale

```bash
sudo dnf config-manager --add-repo https://pkgs.tailscale.com/stable/oracle/9/tailscale.repo
sudo dnf install -y tailscale
sudo systemctl enable --now tailscaled

# --accept-dns=false is required: bankdemo must keep resolving to the private IP.
sudo tailscale up --advertise-tags=tag:bankdemo --accept-dns=false
```

Open the printed URL in a browser and approve the machine.

Then protect the daemon from the memory-exhaustion fault (F16):

```bash
sudo mkdir -p /etc/systemd/system/tailscaled.service.d
printf '[Service]\nOOMScoreAdjust=-900\n' | sudo tee /etc/systemd/system/tailscaled.service.d/oom.conf
sudo systemctl daemon-reload && sudo systemctl restart tailscaled
tailscale status
```

Record `tailscaled`'s RSS (~50 MB) in the Phase 3.5 measurements (§6.8).

---

# Part D — Tailscale admin console

Sign in at [login.tailscale.com](https://login.tailscale.com).

## 17. Approve the tags

1. Go to **Access controls** (top nav).
2. Replace the policy file's `tagOwners` and `acls` blocks with:

```jsonc
{
  "tagOwners": {
    "tag:bankdemo": ["autogroup:admin"],
    "tag:ci":       ["autogroup:admin"]
  },
  "acls": [
    { "action": "accept", "src": ["tag:ci"], "dst": ["tag:bankdemo:22"] },
    { "action": "accept", "src": ["autogroup:member"], "dst": ["tag:bankdemo:22"] }
  ]
}
```

3. Click **Save**.

## 18. Confirm the node

1. Go to **Machines**.
2. Confirm `bankdemo` is listed with tag `tag:bankdemo` and **Expiry: disabled**.

## 19. Create the CI OAuth client

1. Go to **Settings → OAuth clients** → **Generate OAuth client**.
2. Set **Scopes**: `auth_keys` → **Write**.
3. Set **Tags**: `tag:ci`.
4. Click **Generate client** and copy both the **Client ID** and **Client secret** now.

---

# Part E — GitHub

## 20. Store the CI secrets

1. Open `https://github.com/hpk369/ai-qa-agent` → **Settings** tab.
2. Left sidebar → **Secrets and variables → Actions**.
3. Click **New repository secret** and add each:

| Name | Value |
|---|---|
| `TS_OAUTH_CLIENT_ID` | the Client ID from step 19 |
| `TS_OAUTH_SECRET` | the Client secret from step 19 |

---

# Part F — Local machine

## 21. Configure the repo

```bash
cd infra/bankdemo
git check-ignore -v .env      # must print a matching .gitignore rule before continuing
cp .env.example .env
```

Set in `.env`:

| Variable | Value |
|---|---|
| `VM_HOST` | the instance's public IP |
| `VM_USER` | `bankops` |
| `SSH_KEY` | `~/.ssh/bankdemo` |

## 22. Run the acceptance check

```bash
ssh bankops@<public-ip> 'uname -m; nproc; free -g; df -h /data; head -2 /etc/os-release'
ssh bankops@<public-ip> 'sudo -n true && echo "sudo OK"'
```

| Field | Expected |
|---|---|
| `uname -m` | `aarch64` |
| `nproc` | the OCPU count from step 1 |
| `free -g` | ~1 GB below the memory figure from step 1 |
| `df -h /data` | ~50 G |
| `/etc/os-release` | `Oracle Linux Server 9.x` |
| sudo check | `sudo OK` |

Paste the output into `docs/PROGRESS.md` under B2. All six lines must match before continuing.

## 23. Deploy

```bash
make -C infra/bankdemo deploy      # rsyncs the subtree to the VM and runs install.sh
infra/bankdemo/scripts/tunnel.sh   # forwards 9870, 8088, 18080 over SSH
```

Track B continues with installer stages 00–40 and the Phase 3.5 budget gate
(`../IMPLEMENTATION_GUIDE.md` §6.8).

---

# Part G — Cloudflare dashboard

Needed by B6.1 (the demo feed), not by `make deploy`. Sign in at
[dash.cloudflare.com](https://dash.cloudflare.com).

## 24. Confirm the zone

1. On the dashboard home, confirm `inkandinfra.com` is listed under **Websites** with
   **Status: Active**.
2. Click `inkandinfra.com` → **DNS → Records** and keep this tab available for step 29.

## 25. Create the R2 bucket

1. Left sidebar → **R2 Object Storage**. If prompted, complete the one-time R2 signup (free tier,
   card on file, no charge inside 10 GB).
2. Click **Create bucket**.

| Field | Value |
|---|---|
| Bucket name | `bankdemo-bundles` |
| Location | **Automatic** |
| Default storage class | **Standard** |

3. Click **Create bucket**.

## 26. Attach the public hostname

1. Open the `bankdemo-bundles` bucket → **Settings** tab.
2. Under **Public access → Custom domains**, click **Connect domain**.
3. Enter `bundles.inkandinfra.com` → **Continue** → **Connect domain**.
4. Wait for **Status: Active**. Cloudflare adds the CNAME to the zone automatically — do not add
   one by hand.

Leave **r2.dev subdomain** disabled.

## 27. Set the retention rule

1. Same **Settings** tab → **Object lifecycle rules** → **Add rule**.

| Field | Value |
|---|---|
| Rule name | `expire-bundles` |
| Apply to | All objects in the bucket |
| Action | **Delete uploaded objects** |
| Age | `10` days after upload |

2. Click **Add rule**.

At six cron runs a day this holds ~60 objects (~3.5 GB). R2 lifecycle rules expire by age, not
by object count.

## 28. Create the R2 API token

1. **R2 Object Storage → API → Manage API tokens** → **Create API token**.

| Field | Value |
|---|---|
| Token name | `bankdemo-feed-publisher` |
| Permissions | **Object Read & Write** |
| Specify bucket(s) | `bankdemo-bundles` only |
| TTL | Forever |

2. Click **Create API token**.
3. Copy the **Access Key ID**, the **Secret Access Key** and the **S3 endpoint** now — the secret
   is shown once.
4. Go to `https://github.com/hpk369/ai-qa-agent` → **Settings → Secrets and variables → Actions**
   → **New repository secret** and add:

| Name | Value |
|---|---|
| `R2_ACCESS_KEY_ID` | Access Key ID from step 28.3 |
| `R2_SECRET_ACCESS_KEY` | Secret Access Key from step 28.3 |
| `R2_S3_ENDPOINT` | `https://<account-id>.r2.cloudflarestorage.com` |
| `R2_BUCKET` | `bankdemo-bundles` |

## 29. Optional hostnames

**Demo site on `demo.inkandinfra.com`** — do this only if you also update the demo links in
`/README.md`:

1. GitHub → repo **Settings → Pages → Custom domain** → enter `demo.inkandinfra.com` → **Save**.
2. Cloudflare → **DNS → Records → Add record**: Type `CNAME`, Name `demo`, Target
   `hpk369.github.io`, Proxy status **DNS only**.
3. Wait for GitHub Pages to report the certificate as issued, then tick **Enforce HTTPS**.
4. Only then set the Cloudflare record to **Proxied**, and set **SSL/TLS → Overview** to
   **Full (strict)**. Proxying before the certificate exists causes a redirect loop.

**Broker on `api.inkandinfra.com`** — B6.3 only, after the Worker in
[`../../demo-broker/README.md`](../../demo-broker/README.md) is deployed:

1. **Workers & Pages** → select the broker Worker → **Settings → Domains & Routes** → **Add →
   Custom domain** → `api.inkandinfra.com` → **Add domain**.
2. Left sidebar → **Turnstile** → **Add widget**.

| Field | Value |
|---|---|
| Widget name | `bankdemo-demo-request` |
| Hostnames | `demo.inkandinfra.com`, `hpk369.github.io` |
| Widget mode | Managed |

3. Copy the **Site Key** into the demo page and store the **Secret Key** as the Worker secret
   `TURNSTILE_SECRET_KEY`.

---

# Troubleshooting

**"Out of host capacity" in step 6.** Try in this order:

1. Re-run **Create instance** selecting a different availability domain or fault domain.
2. Retry during the region's off-peak hours.
3. Loop the CLI with a backoff: `oci compute instance launch ...`.
4. Create a new tenancy in a different home region (the home region cannot be changed once set).

Do not substitute an x86 shape — the stack requires aarch64.

**`bankdemo` resolves to `127.0.0.1` after a reboot.** Check with `getent hosts bankdemo`; it
must return `10.0.0.x`. Installer stage `10-os-base.sh` writes
`/etc/cloud/cloud.cfg.d/99-bankdemo.cfg` (`preserve_hostname: true`, `manage_etc_hosts: false`)
to prevent this. Re-run the stage if the file is missing.

**Locked out of SSH.** Use the `opc` user, then the serial console created in step 7.

---

# Not covered here

| Topic | Where |
|---|---|
| Installer stages, config rendering, systemd units, HDFS init | `make deploy` onward, `../IMPLEMENTATION_GUIDE.md` §5–§7 |
| Restricted sudoers rule, GitHub Actions run secrets | `../IMPLEMENTATION_GUIDE.md` §11.1, §11.3 (Phase 8) |
| `fail2ban`, `dnf-automatic`, firewalld assertions | `../IMPLEMENTATION_GUIDE.md` §12 (Phase 9) |
| Network design rationale and reviewer-access options | `../IMPLEMENTATION_GUIDE.md`, `../../demo-broker/README.md` |
| Demo-feed publication workflow that consumes the R2 secrets | `../IMPLEMENTATION_GUIDE.md` §9.6 (B6.1) |
| Cloudflare Tunnel for the Track A Slack webhook | `../../../docs/PHASE1_SETUP.md` §5 |

Console labels drift between OCI UI revisions. If a menu path here does not match what you see,
follow the console and correct this file.
