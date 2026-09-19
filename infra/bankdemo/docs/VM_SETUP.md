# VM setup — Oracle Cloud host for the Hadoop stack

Manual provisioning steps for roadmap B2 / Phase 1 of
[`../IMPLEMENTATION_GUIDE.md`](../IMPLEMENTATION_GUIDE.md) §4.2. Run the parts in order.

| Part | Platform | Steps | When |
|---|---|---|---|
| A | OCI Console (browser) | 1–8 | now |
| B | Local machine (terminal) | 9–11 | now |
| C | VM shell (over SSH) | 12–16 | now |
| D | Tailscale admin console (browser) | 17–19 | now |
| E | VM shell (over SSH) | 20–23 | now |
| F | GitHub (browser) | 24 | now |
| G | Local machine (terminal) | 25–27 | now |
| H | Cloudflare dashboard (browser) | 28–33 | before B6.1, not needed to deploy |

Parts D and E are in this order on purpose: `tailscale up --advertise-tags` fails with
`tag not permitted` unless the tag already exists in the tailnet policy file.

Values used throughout: compartment `bankdemo`, instance name/hostname `bankdemo`,
OS user `bankops`, data mount `/data`.

DNS names on the `inkandinfra.com` zone:

| Hostname | Serves | Configured in |
|---|---|---|
| `bundles.inkandinfra.com` | run bundle `.tar.gz` downloads from R2 | step 30 |
| `api.inkandinfra.com` | demo-broker Worker, `/demo/*` | step 33 (B6.3, optional) |
| `demo.inkandinfra.com` | GitHub Pages demo site | step 33 |
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
5. Write both numbers into `docs/PROGRESS.md` under B2. Use them as the shape size in step 6.

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

Add no other ingress rule. Web UIs (9870, 8088, 18080) are reached by SSH tunnel in step 27.
Keep this rule permanently — it is the break-glass path if `tailscaled` fails to start.

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

## 16. Install the prerequisites

The installer provides these later, but it has not run yet and steps 20–23 need them now:

```bash
sudo dnf install -y dnf-plugins-core jq nmap-ncat
```

| Package | Needed for |
|---|---|
| `dnf-plugins-core` | `dnf config-manager`, used in step 20 |
| `jq` | reading `tailscale status --json` in step 23 |
| `nmap-ncat` | `nc`, used for the port checks in step 23 |

---

# Part D — Tailscale admin console

Do this part before the VM joins the tailnet.

## 17. Create the tailnet

1. Go to [login.tailscale.com](https://login.tailscale.com) and sign in with GitHub or Google.
2. Confirm the tailnet name shown top-left (e.g. `tail1a2b3.ts.net`). The free Personal plan
   covers this.

## 18. Declare the tags

1. Open **Access controls** (`login.tailscale.com/admin/acls/file`).
2. Replace the default policy with the following. The editor is HuJSON — comments and trailing
   commas are legal:

```jsonc
{
  "tagOwners": {
    "tag:bankdemo": ["autogroup:admin"],  // the VM
    "tag:ci":       ["autogroup:admin"],  // ephemeral GitHub Actions runners
  },

  "acls": [
    // CI runners reach the VM on SSH only.
    {"action": "accept", "src": ["tag:ci"], "dst": ["tag:bankdemo:22"]},

    // Required: a tagged machine is owned by the tag, not by you, so the
    // default "members reach their own devices" behaviour no longer applies.
    {"action": "accept", "src": ["autogroup:member"], "dst": ["tag:bankdemo:22"]},
  ],

  "tests": [
    {"src": "tag:ci", "accept": ["tag:bankdemo:22"], "deny": ["tag:bankdemo:9870"]},
  ],
}
```

3. Click **Save**. A rejected save names the offending line — usually a missing comma, or a tag
   used in `acls` but absent from `tagOwners`.

## 19. Create the CI OAuth client

1. **Settings** (top nav) → **OAuth clients** (left sidebar) → **Generate OAuth client…**.

| Field | Value |
|---|---|
| Description | `github-actions-bankdemo` |
| Scopes | expand **Keys** → tick **Auth Keys: Write**, nothing else |
| Tags | `tag:ci` |

2. Click **Generate client**.
3. Copy the **Client ID** and **Client secret** now — the secret is shown once. If you lose it,
   revoke the client and generate a new one.

Both are stored in GitHub in step 24. To rotate later: **Settings → OAuth clients → ⋯ →
Revoke**, generate a replacement with the same scope and tag, update both secrets.

---

# Part E — VM shell

Back on the VM as `opc`.

## 20. Install Tailscale

```bash
sudo dnf config-manager --add-repo https://pkgs.tailscale.com/stable/oracle/9/tailscale.repo
sudo dnf install -y tailscale
rpm -q tailscale --qf '%{NAME} %{VERSION} %{ARCH}\n'   # must print aarch64
```

## 21. Join the tailnet, tagged

```bash
sudo systemctl enable --now tailscaled
sudo tailscale up --advertise-tags=tag:bankdemo --accept-dns=false
```

The second command prints an authentication URL and blocks. Open the URL in a browser signed in
to the tailnet from step 17, click **Connect**, and wait for the terminal to print `Success.`
Re-run the command if you closed the terminal first — it issues a fresh URL.

Both flags are required:

| Flag | Effect |
|---|---|
| `--advertise-tags=tag:bankdemo` | applies the tag from step 18 and disables key expiry |
| `--accept-dns=false` | keeps `bankdemo` resolving to the private IP, which HDFS, Kafka `advertised.listeners` and `fs.defaultFS` all require |

Do not pass `--ssh` (it would replace key auth with tailnet identity) or `--advertise-routes`.

If the tag was omitted, fix it without reinstalling: admin console → **Machines** → the
`bankdemo` row → **⋯ → Edit ACL tags** → tick `tag:bankdemo` → **Save**.

## 22. Protect `tailscaled` from the OOM killer

```bash
sudo mkdir -p /etc/systemd/system/tailscaled.service.d
sudo tee /etc/systemd/system/tailscaled.service.d/oom.conf >/dev/null <<'EOF'
[Service]
OOMScoreAdjust=-900
EOF
sudo systemctl daemon-reload
sudo systemctl restart tailscaled
```

Verify both views:

```bash
systemctl show tailscaled -p OOMScoreAdjust    # OOMScoreAdjust=-900
cat /proc/$(pidof tailscaled)/oom_score_adj    # -900
```

`0` from the first means the drop-in path is wrong — it must be exactly
`/etc/systemd/system/tailscaled.service.d/<name>.conf`, followed by `daemon-reload`.

The restart drops the tailnet link for a second and does not require re-authentication. Record
`tailscaled`'s ~50 MB RSS in the Phase 3.5 measurements (§6.8).

## 23. Verify the tailnet path

**On the VM:**

```bash
tailscale status --self --json | jq '{Online:.Self.Online, Tags:.Self.Tags, IP:.Self.TailscaleIPs[0]}'
```

```json
{ "Online": true, "Tags": ["tag:bankdemo"], "IP": "100.x.y.z" }
```

`Tags: null` means the node is untagged — fix it as in step 21. Write down the `100.x.y.z`
address; step 24 needs it.

```bash
getent hosts bankdemo           # must print 10.0.x.x, never 100.x.y.z
sudo firewall-cmd --list-all    # expect: services: ssh (dhcpv6-client alongside is fine)
```

Do not add `tailscale0` to firewalld's `trusted` zone.

**In the admin console:** **Machines** → the `bankdemo` row shows a `tag:bankdemo` badge and
**Expiry disabled**.

**From your own machine**, with Tailscale installed and signed in to the same tailnet:

```bash
tailscale ping bankdemo
ssh bankops@100.x.y.z

nc -vz -w 5 100.x.y.z 9870      # must time out or be refused
nc -vz -w 5 <public-ip> 22      # from a network outside your /32: must time out

ssh-keyscan -t ed25519 100.x.y.z    # keep the output line for step 24
```

---

# Part F — GitHub

## 24. Store the repository secrets

1. Open `https://github.com/hpk369/ai-qa-agent` → **Settings** tab (repo settings, not your account's).
2. Left sidebar → **Secrets and variables → Actions**.
3. Click **New repository secret** and add each:

| Name | Value |
|---|---|
| `TS_OAUTH_CLIENT_ID` | Client ID from step 19 |
| `TS_OAUTH_SECRET` | Client secret from step 19 |
| `VM_HOST` | the `100.x.y.z` tailnet address from step 23 |
| `VM_USER` | `bankops` |
| `VM_KNOWN_HOSTS` | the full `ssh-keyscan` output line from step 23 |

CI has no public SSH path, so `VM_HOST` here is the tailnet address — not the public IP used in
the local `.env` at step 25.

---

# Part G — Local machine

## 25. Configure the repo

```bash
cd infra/bankdemo
git check-ignore -v .env      # must print a matching .gitignore rule before continuing
cp .env.example .env
```

Set in `.env`:

| Variable | Value |
|---|---|
| `VM_HOST` | the instance's **public** IP |
| `VM_USER` | `bankops` |
| `SSH_KEY` | `~/.ssh/bankdemo` |

## 26. Run the acceptance check

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

## 27. Deploy

```bash
make -C infra/bankdemo deploy      # rsyncs the subtree to the VM and runs install.sh
infra/bankdemo/scripts/tunnel.sh   # forwards 9870, 8088, 18080 over SSH
```

Track B continues with installer stages 00–40 and the Phase 3.5 budget gate
(`../IMPLEMENTATION_GUIDE.md` §6.8).

---

# Part H — Cloudflare dashboard

Needed by B6.1 (the demo feed), not by `make deploy`. Sign in at
[dash.cloudflare.com](https://dash.cloudflare.com).

## 28. Confirm the zone

1. On the dashboard home, confirm `inkandinfra.com` is listed under **Websites** with
   **Status: Active**.
2. Click `inkandinfra.com` → **DNS → Records** and keep this tab available for step 33.

## 29. Create the R2 bucket

1. Left sidebar → **R2 Object Storage**. If prompted, complete the one-time R2 signup (free tier,
   card on file, no charge inside 10 GB).
2. Click **Create bucket**.

| Field | Value |
|---|---|
| Bucket name | `bankdemo-bundles` |
| Location | **Automatic** |
| Default storage class | **Standard** |

3. Click **Create bucket**.

## 30. Attach the public hostname

1. Open the `bankdemo-bundles` bucket → **Settings** tab.
2. Under **Public access → Custom domains**, click **Connect domain**.
3. Enter `bundles.inkandinfra.com` → **Continue** → **Connect domain**.
4. Wait for **Status: Active**. Cloudflare adds the CNAME to the zone automatically — do not add
   one by hand.

Leave **r2.dev subdomain** disabled.

## 31. Set the retention rule

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

## 32. Create the R2 API token

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
4. Add them as repository secrets, navigating as in step 24:

| Name | Value |
|---|---|
| `R2_ACCESS_KEY_ID` | Access Key ID from step 32.3 |
| `R2_SECRET_ACCESS_KEY` | Secret Access Key from step 32.3 |
| `R2_S3_ENDPOINT` | `https://<account-id>.r2.cloudflarestorage.com` |
| `R2_BUCKET` | `bankdemo-bundles` |

## 33. Optional hostnames

**Demo site on `demo.inkandinfra.com`.** `docs/CNAME` and the links in `/README.md` already
point here; these steps make the hostname resolve:

1. Cloudflare → **DNS → Records → Add record**: Type `CNAME`, Name `demo`, Target
   `hpk369.github.io`, Proxy status **DNS only**.
2. GitHub → repo **Settings → Pages** → confirm **Custom domain** reads `demo.inkandinfra.com`
   (`docs/CNAME` sets it) and that the DNS check passes.
3. Wait for GitHub Pages to report the certificate as issued, then tick **Enforce HTTPS**.
4. Only then set the Cloudflare record to **Proxied**, and set **SSL/TLS → Overview** to
   **Full (strict)**. Proxying before the certificate exists causes a redirect loop.

Until step 1 is done the demo stays reachable at `hpk369.github.io/ai-qa-agent`, and the
README link is dead. Do these four steps before merging, or revert the README link.

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

| Symptom | Fix |
|---|---|
| **Out of host capacity** creating the instance | Retry in another availability or fault domain; retry off-peak; loop `oci compute instance launch` with a backoff; or create a new tenancy in another home region. Never substitute an x86 shape — the stack requires aarch64 |
| `dnf: No such command: config-manager` | `dnf-plugins-core` missing — step 16 |
| `tailscale up` → `tag not permitted` | Tag missing from `tagOwners`, or you are not an Owner/Admin — step 18 |
| `tailscale up` appears to hang | Normal; it is waiting for you to open the printed URL — step 21 |
| Console shows the node owned by you, not the tag | `--advertise-tags` omitted — step 21 |
| Tailnet SSH hangs, public SSH works | ACL lacks the `autogroup:member` rule — step 18 |
| Tailnet SSH refused and the ACL looks right | `tailscale0` in firewalld's trusted zone — step 23 |
| `nc` to 9870 over the tailnet connects | ACL wider than step 18 intended, or firewalld as above |
| `OOMScoreAdjust=0` after step 22 | Wrong drop-in path, or `daemon-reload` skipped |
| CI fails at the Tailscale step | OAuth client untagged, wrong scope, or `tag:ci` absent from `tagOwners` — step 19 |
| CI joins but SSH times out | `VM_HOST` is still the public IP — step 24 |
| Node vanished months later | Key expiry; the node was untagged — step 21 |
| `getent hosts bankdemo` returns `100.x.y.z` | `--accept-dns=false` missing — step 21 |
| `bankdemo` resolves to `127.0.0.1` after a reboot | Installer stage `10-os-base.sh` writes `/etc/cloud/cloud.cfg.d/99-bankdemo.cfg` (`preserve_hostname: true`, `manage_etc_hosts: false`). Re-run the stage if the file is missing |
| DataNodes will not register after adding Tailscale | Either of the two rows above |
| Locked out of SSH | Use the `opc` user, then the serial console from step 7 |

For the Phase 9 rebuild drill, which must run unattended: admin console → **Settings → Keys →
Generate auth key**, turn on **Reusable** and **Pre-approved**, set **Tags** to `tag:bankdemo`,
then use `sudo tailscale up --authkey=tskey-auth-... --advertise-tags=tag:bankdemo
--accept-dns=false`. Auth keys expire after 90 days and never go in the repo.

---

# Not covered here

| Topic | Where |
|---|---|
| Installer stages, config rendering, systemd units, HDFS init | `make deploy` onward, `../IMPLEMENTATION_GUIDE.md` §5–§7 |
| Restricted sudoers rule, GitHub Actions run workflow | `../IMPLEMENTATION_GUIDE.md` §11.1, §11.3 (Phase 8) |
| `fail2ban`, `dnf-automatic`, firewalld assertions | `../IMPLEMENTATION_GUIDE.md` §12 (Phase 9) |
| Demo-feed publication workflow that consumes the R2 secrets | `../IMPLEMENTATION_GUIDE.md` §9.6 (B6.1) |
| Cloudflare Tunnel for the Track A Slack webhook | `../../../docs/PHASE1_SETUP.md` §5 |
| Network design rationale and reviewer-access options | `../IMPLEMENTATION_GUIDE.md`, `../../demo-broker/README.md` |

Console labels drift between OCI, Tailscale and Cloudflare UI revisions. If a menu path here
does not match what you see, follow the console and correct this file.
