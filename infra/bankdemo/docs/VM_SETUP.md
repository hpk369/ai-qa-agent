# VM setup — provisioning the Oracle Cloud host for the Hadoop stack

This is the **[HUMAN]** half of roadmap phase B2, and the whole of Phase 1 in
[`../IMPLEMENTATION_GUIDE.md`](../IMPLEMENTATION_GUIDE.md) §4.2. Nothing in
`infra/bankdemo/` can be built or deployed until this is done, and none of it is
automatable — OCI resource creation is deliberately excluded from what Claude Code may do
(`../CLAUDE.md`, safety rules).

Budget about an hour, most of it waiting on the console. Two steps can destroy things
(§6 formats a disk, §8 edits sudoers), and both are flagged where they occur.

**Read §0 before you provision anything.** One unverified assumption sits underneath every
memory number in the spec, and it is much cheaper to check now than to discover in B3.

---

## 0. Verify the shape allowance first

`../IMPLEMENTATION_GUIDE.md` §1.3 assumes **2 OCPU / 12 GB** is the Always Free Ampere A1
ceiling. Historically the allowance has been **4 OCPU / 24 GB**, and the spec's claim of a
reduction is unverified. This matters more than any other decision here:

- **If you actually get 4 OCPU / 24 GB**, say so before B3 starts. The entire §2.3 memory
  budget, the Phase 3.5 gate, the 3072 MB YARN cap, the single-Spark-app-at-a-time
  constraint, and most of the phase-timing pressure in §8.2 exist *only* because of the
  12 GB figure. They should be re-derived rather than inherited.
- **If it really is 2 OCPU / 12 GB**, the spec is calibrated correctly and you change nothing.

**Where to check:** OCI Console → **Limits, Quotas and Usage** (under Governance &
Administration) → filter Service = **Compute** → look for the `standard-a1-core-count` and
`standard-a1-memory-count` limits in your home region. Those numbers are the truth; the
marketing page is not.

Either way, record what you found in `docs/PROGRESS.md` under B2. It is the justification
for every memory number that follows.

---

## 1. An Oracle Cloud account, and a guardrail against accidental spend

**Where:** [cloud.oracle.com](https://cloud.oracle.com) → Start for free.

Signup requires a credit card for identity verification. Always Free resources do not charge
it, but two things can:

- **Upgrading to Pay As You Go.** The console offers this persistently. Don't. If you have
  already upgraded, Always Free resources stay free, but anything *over* the allowance bills.
- **Provisioning outside the Always Free envelope** — a bigger shape, a third block volume, a
  second boot volume. The console marks eligible options **"Always Free-eligible"**. If that
  badge is missing, the thing costs money.

Set a budget alert now rather than later: **Billing & Cost Management → Budgets → Create
Budget**, scope it to your root compartment, set the amount to $1 and the alert threshold to
100% of actual spend. It will never fire if everything stays inside the allowance, which is
exactly the signal you want.

**Pick your home region carefully** — it cannot be changed, and Ampere A1 capacity varies a
lot between regions (see §11). A region with chronic A1 shortage makes §5 a lottery.

## 2. A compartment

Compartments are OCI's isolation boundary. Using a dedicated one keeps this project's
resources separable and makes cleanup a single operation.

**Where:** **Identity & Security → Compartments → Create Compartment**.

| Field | Value |
|---|---|
| Name | `bankdemo` |
| Description | Hadoop triage rig — ai-qa-agent Track B |
| Parent compartment | your root compartment |

Every later step has a compartment selector. Set it to `bankdemo` each time — the console
defaults back to root more often than you'd expect.

## 3. Network

**Where:** **Networking → Virtual Cloud Networks → Start VCN Wizard → Create VCN with
Internet Connectivity**.

| Field | Value |
|---|---|
| VCN name | `bankdemo-vcn` |
| Compartment | `bankdemo` |
| VCN CIDR | `10.0.0.0/16` (default) |
| Public subnet CIDR | `10.0.0.0/24` (default) |
| Private subnet CIDR | leave as-is; unused |

The wizard creates the internet gateway, route table and default security list. Accept its
defaults — the only thing to change is the ingress rule, next.

## 4. Security list — exactly one ingress rule

This is the one network control that matters. `../CLAUDE.md` makes it a hard constraint: only
TCP 22 is reachable from outside, and every web UI (NameNode 9870, ResourceManager 8088,
Spark History 18080) is reached by SSH tunnel instead.

**Where:** your VCN → **Security Lists** → **Default Security List** → **Ingress Rules**.

Delete every ingress rule except this one:

| Source CIDR | Protocol | Dest. port | Why |
|---|---|---|---|
| `<your IP>/32` | TCP | 22 | Your SSH, every web-UI tunnel, and break-glass access. Find yours with `curl -s ifconfig.me` |

**That is the whole list.** Not "at minimum" — exactly. Every other thing that talks to this
host dials *out* from it:

| Capability | How it reaches the VM | Ingress needed |
|---|---|---|
| GitHub Actions run trigger (§11.3) | Tailscale — the VM joins the tailnet outbound | none |
| Hourly demo-feed harvest (§9.6) | Tailscale, same path | none |
| Demo broker polling (§9.7, optional) | VM → Worker over HTTPS, every 2 min | none |
| Live progress streaming (optional) | VM → Worker over HTTPS | none |
| R2 tarball upload | performed by the Actions runner, never by the VM | none |
| Installer downloads | outbound to Apache, Maven, dnf repos | none |

If you find yourself adding a second ingress rule, something has gone wrong with the design
rather than with the firewall — come back to this table first.

### Egress

Leave the default allow-all egress rule alone. It carries: dnf repos (OL9, EPEL, Tailscale),
the Hadoop/Spark/Kafka tarballs and the PostgreSQL JDBC driver, Tailscale (UDP 41641, falling
back to DERP relays over TCP 443), and — only if the optional broker is deployed — HTTPS to the
Cloudflare Worker.

### Verify

```bash
# Expect exactly one ingress rule, TCP 22, your address.
oci network security-list get --security-list-id <ocid> \
  --query 'data."ingress-security-rules"[].{src:source,proto:protocol,port:"tcp-options".destination-port-range.min}'

# From anywhere that is not your IP, this must hang and time out rather than connect:
nc -vz -w 5 <public-ip> 22
```

### Why not the alternatives

Recorded so they don't get quietly revisited:

| | Approach | Verdict |
|---|---|---|
| **A** | Self-hosted GitHub runner on the VM | **Rejected.** `hpk369/ai-qa-agent` is public and forkable, and GitHub advises against self-hosted runners on public repos because fork pull requests can execute on them. `run-incident.yml` is `workflow_dispatch`-only so it would be safe *today*, but it is one future `pull_request`-triggered workflow with the wrong `runs-on:` label away from being a remote-code-execution path onto the host. F16 would also OOM-kill the runner mid-run |
| **B** | Tailscale, outbound-only | **Chosen** — §4a |
| **C** | Open `0.0.0.0/0` on 22 and compensate | **Rejected.** It was the only reason `0.0.0.0/0` ever appeared here. Pinning GitHub's ranges instead is impractical: they are published at `api.github.com/meta` under `actions`, but there are thousands of CIDRs that rotate and OCI caps ingress rules per security list in the low hundreds |

One thing worth understanding about OCI specifically, because it trips people up: security list
rules are **purely additive allow-rules** — no deny, no precedence, no "more specific wins".
`0.0.0.0/0` is a superset of any `/32`, so listing both would not restrict anything. It would
just imply a restriction that isn't there, which is worse than not having the rule at all.

## 4a. Tailscale — the outbound-only path for CI

Do this after §5–§7, once the instance exists and `bankops` can log in. It is listed here so
the network decision stays in one place.

**Do the steps in this order.** A tag has to exist in the policy file *before* any machine can
advertise it — running `tailscale up --advertise-tags=tag:bankdemo` against a tailnet that has
never heard of `tag:bankdemo` fails with `tag not permitted`, and the error does not say that
the fix is in a web console you haven't opened yet.

### Step 1 — create a tailnet

**Where:** [login.tailscale.com](https://login.tailscale.com) → sign in with GitHub or Google.

The free Personal plan covers this comfortably (100 devices, 3 users). Signing in creates your
tailnet automatically; its name appears top-left, like `tail1a2b3.ts.net`.

### Step 2 — define the two tags

Tags are declared in the tailnet policy file, not in a tags UI. Until a tag is declared there,
it does not exist.

**Where:** admin console → **Access controls** (direct link:
`login.tailscale.com/admin/acls/file`).

You will see the default policy, which allows everything:

```jsonc
{
	"acls": [
		{"action": "accept", "src": ["*"], "dst": ["*:*"]},
	],
}
```

Replace it with the following. The editor is HuJSON, so comments and trailing commas are
legal — keep the comments, they are the explanation for whoever reads this in a year:

```jsonc
{
  // Who is allowed to apply each tag. autogroup:admin = any Owner/Admin
  // of this tailnet, i.e. you. A tag must appear here before any machine
  // or OAuth client can advertise it.
  "tagOwners": {
    "tag:bankdemo": ["autogroup:admin"],  // the VM itself
    "tag:ci":       ["autogroup:admin"],  // ephemeral GitHub Actions runners
  },

  "acls": [
    // CI runners reach the VM on SSH only -- not 9870, not 8088, not ICMP.
    {"action": "accept", "src": ["tag:ci"], "dst": ["tag:bankdemo:22"]},

    // You reach the VM on SSH. This rule is REQUIRED: once a machine is
    // tagged it is owned by the tag rather than by you, so the default
    // "members can reach their own devices" behaviour no longer applies.
    {"action": "accept", "src": ["autogroup:member"], "dst": ["tag:bankdemo:22"]},
  ],

  // Assertions the editor checks on save. If a future edit breaks either
  // of these, the save is rejected instead of silently locking you out.
  "tests": [
    {"src": "tag:ci", "accept": ["tag:bankdemo:22"], "deny": ["tag:bankdemo:9870"]},
  ],
}
```

Click **Save**. If it refuses, the error names the line — usually a missing comma or a tag
used in `acls` that is absent from `tagOwners`.

Note what this policy does *not* allow: `tag:ci` cannot reach port 9870 or 8088, so a
compromised CI token cannot browse your Hadoop UIs. The `tests` block is what keeps that true
through later edits.

### Step 3 — install on the VM and advertise the tag

Only now will `--advertise-tags` succeed.

**3.1 Add the repository and install.** Tailscale publishes an Oracle Linux 9 repo with
aarch64 RPMs:

```bash
sudo dnf config-manager --add-repo https://pkgs.tailscale.com/stable/oracle/9/tailscale.repo
sudo dnf install -y tailscale
rpm -qi tailscale | head -5          # confirm arch is aarch64
```

The repo file sets `gpgcheck=1` and points at Tailscale's signing key; `-y` accepts the key
import. If you would rather eyeball it first, drop the `-y` and compare the fingerprint the
prompt shows against the one on `pkgs.tailscale.com`.

**3.2 Start the daemon.**

```bash
sudo systemctl enable --now tailscaled
systemctl is-active tailscaled       # active
```

`enable` matters: the node state lives in `/var/lib/tailscale/tailscaled.state` and the daemon
rejoins the tailnet by itself after a reboot, with no re-authentication. The §12 reboot test
should confirm that rather than assume it.

**3.3 Join the tailnet, tagged.**

```bash
sudo tailscale up --advertise-tags=tag:bankdemo --accept-dns=false
```

It prints an authentication URL. This is a headless box, so copy the URL into a browser signed
in to the same tailnet, approve, and the command returns.

Flags, and one that is deliberately absent:

| Flag | Why |
|---|---|
| `--advertise-tags=tag:bankdemo` | Applies the tag from step 2. Also **disables key expiry** — untagged nodes drop off the tailnet after ~6 months, which on a cron-driven box is a failure you would notice long after it happened |
| `--accept-dns=false` | See the warning below. Non-negotiable on this host |
| ~~`--ssh`~~ | **Not used.** Tailscale SSH would replace key-based auth with tailnet identity. We keep OpenSSH so the path still works if Tailscale is ever removed, and so `VM_SSH_KEY` stays the authentication mechanism (§11.3) |
| ~~`--advertise-routes`~~ | **Not used.** Nothing behind this host needs reaching |

The device name on the tailnet comes from the system hostname, so it appears as `bankdemo` and
gets the MagicDNS name `bankdemo.<tailnet>.ts.net`.

> ⚠️ **`--accept-dns=false` is not optional on this host.** Tailscale's MagicDNS adds the
> tailnet as a DNS search domain, which would make the bare name `bankdemo` resolve to a
> `100.x.y.z` tailnet address. HDFS, Kafka's `advertised.listeners`, and `fs.defaultFS` all
> reference `bankdemo` and need the **private IP**. The `/etc/hosts` entry from §12 wins over
> DNS under the default `nsswitch` order, so this is belt-and-braces — but the failure mode if
> both protections are missing is DataNode registration breaking in a way that looks nothing
> like a DNS problem.

**3.4 Leave `tailscale0` out of firewalld's trusted zone.**

Tailscale's own documentation often suggests
`firewall-cmd --zone=trusted --add-interface=tailscale0`. **Don't, on this host.** Trusting the
interface would let any tailnet device reach *any* port on the VM, moving the entire
enforcement boundary onto the Tailscale ACL.

Nothing here needs that. firewalld's default `public` zone already permits TCP 22, which is the
only port anything on the tailnet is supposed to reach — so leaving `tailscale0` untrusted
gives you a second layer behind the ACL at zero cost. If a future ACL edit accidentally widened
`tag:ci` to `:*`, firewalld would still refuse everything but SSH.

Confirm the posture:

```bash
sudo firewall-cmd --get-active-zones      # tailscale0 should NOT appear under "trusted"
sudo firewall-cmd --list-all              # public zone, services: ssh (dhcpv6-client is fine)
```

If SSH over the tailnet later times out while the public path works, this is the first thing to
check — but the fix is a targeted `--add-port`, never `--add-interface=tailscale0`, and per
`../CLAUDE.md` it gets documented in RUNBOOK rather than waved through.

**3.5 If the node joined untagged.** Easy to do by forgetting the flag. Fix it in the console
rather than reinstalling: **Machines** → the `bankdemo` row → **⋯** → **Edit ACL tags** → check
`tag:bankdemo` → Save. Then `sudo tailscale status --self --json | jq .Self.Tags` to confirm.

**3.6 For the rebuild drill.** §12's drill requires an unattended rebuild, and step 3.3 is
interactive. Generate a **pre-authorized, tagged, reusable** auth key for that:
**Settings → Keys → Generate auth key**, with *Reusable* on, *Pre-approved* on, and Tags set to
`tag:bankdemo`. Then the rebuild is:

```bash
sudo tailscale up --authkey=tskey-auth-... --advertise-tags=tag:bankdemo --accept-dns=false
```

Auth keys expire after 90 days maximum, so treat this as a rebuild-day artefact you regenerate,
not a stored credential. It does **not** go in `secrets.env` or anywhere in the repo.

### Step 4 — protect `tailscaled` from F16

F16 deliberately drives the host into memory exhaustion (`../IMPLEMENTATION_GUIDE.md` §10.3).
`tailscaled` is now an access path and a ~50 MB unprotected process, which makes it a plausible
OOM-killer target at exactly the moment you would want to log in and look.

```bash
sudo mkdir -p /etc/systemd/system/tailscaled.service.d
printf '[Service]\nOOMScoreAdjust=-900\n' | sudo tee /etc/systemd/system/tailscaled.service.d/oom.conf
sudo systemctl daemon-reload
sudo systemctl restart tailscaled
```

Restarting drops the tailnet connection for a second or two and does **not** require
re-authentication — the state file survives.

Verify both that systemd knows about it and that the running process actually has it:

```bash
systemctl show tailscaled -p OOMScoreAdjust      # OOMScoreAdjust=-900
cat /proc/$(pidof tailscaled)/oom_score_adj      # -900
```

**Why -900 and not -1000.** The values form a deliberate ordering under memory pressure:

| Process | `oom_score_adj` | Intent |
|---|---|---|
| `sshd` | −1000 | Effectively immune. The break-glass path must survive anything |
| orchestrator (`bankdemo run`) | −900 | Set by the process itself (§8.2) so a run can still revert and clean up |
| `tailscaled` | −900 | Same tier as the orchestrator — protected, but `sshd` still wins |
| NameNode, ResourceManager, PostgreSQL | −500 | Protected enough that F16 hits a YARN container first (§2.3) |
| YARN containers, `stress-ng` | default | The intended casualties |

If `tailscaled` is killed anyway, you have not lost the box: the `/32` ingress rule on TCP 22 is
still there, which is the whole reason Break-glass below says to keep it.

`tailscaled`'s ~50 MB RSS comes out of the OS/page-cache headroom line in §2.3. It does not
threaten the budget, but record it in the Phase 3.5 measurements (§6.8) rather than letting it
turn up later as unexplained drift.

### Step 5 — an OAuth client for CI

GitHub Actions joins the tailnet as a short-lived node on each run. It authenticates with an
OAuth client, which mints an ephemeral auth key per job.

**5.1 Create it.** Admin console → **Settings** → **OAuth clients** → **Generate OAuth client**.

| Field | Value |
|---|---|
| Description | `github-actions-bankdemo` |
| Scope | **Auth Keys** → **Write** (nothing else — it does not need device or DNS scopes) |
| Tags | `tag:ci` |

Two constraints the form enforces, both of which trace back to step 2:

- **A tag is mandatory.** An OAuth client cannot exist untagged, because the tag is what the
  ephemeral runner node inherits, and that inheritance is what makes the `tag:ci` ACL rule
  apply to it.
- **The tag must already be in `tagOwners`.** If `tag:ci` is missing from the policy file, the
  dropdown will not offer it.

The client ID and secret both start with `tskey-client-`. **The secret is shown once** — copy
it before closing the dialog.

**5.2 Store them as repository secrets.** GitHub → repo → **Settings** → **Secrets and
variables** → **Actions** → **New repository secret**:

```
TS_OAUTH_CLIENT_ID    tskey-client-...
TS_OAUTH_SECRET       tskey-client-...-...
```

**5.3 Why an OAuth client rather than a plain auth key.** Auth keys expire after 90 days
maximum. A CI path that dies quarterly with an opaque `failed to authenticate` is worse than
one that never dies — especially on a project you return to between job applications. OAuth
client secrets do not expire; they are revoked explicitly when you want them gone.

**5.4 Ephemeral nodes clean up after themselves.** `tailscale/github-action` requests an
ephemeral key, so each runner node deregisters when the job ends. Without that you would
accumulate one dead machine per workflow run in the console.

**5.5 Rotation.** Revoke under **Settings → OAuth clients → ⋯ → Revoke**, generate a
replacement with the same scope and tag, and update both repository secrets. There is no
overlap window, so do it when no workflow is mid-run.

### Step 6 — verify

**6.1 On the VM.**

```bash
# Online, and carrying the tag. Tags: ["tag:bankdemo"] is the line that matters.
tailscale status --self --json | jq '{Online:.Self.Online, Tags:.Self.Tags, IP:.Self.TailscaleIPs[0]}'

# Connection quality. A direct UDP path is normal; "relayed" means DERP over 443,
# which works but adds latency -- usually an egress rule or NAT issue.
tailscale netcheck

# THE critical one: this must be the PRIVATE IP (10.0.x.x), never 100.x.y.z.
getent hosts bankdemo
```

That last check is the one that silently breaks HDFS. Run it again after every reboot as part
of the §12 test.

**6.2 In the admin console.** **Machines** → the `bankdemo` row should show the `tag:bankdemo`
badge and **Expiry disabled**. If it shows an expiry date, the node is untagged — go back to
step 3.5, because the box will fall off the tailnet in six months.

**6.3 From your laptop, once it is also on the tailnet.**

```bash
tailscale ping bankdemo              # reports direct or via DERP
ssh bankops@100.x.y.z                # the tailnet IP from 6.1

# The ACL should permit 22 and nothing else. This must FAIL:
nc -vz -w 5 100.x.y.z 9870
```

That last command is the real test of step 2's policy. If it connects, either the ACL is wider
than intended or `tailscale0` ended up in firewalld's trusted zone (step 3.4).

**6.4 Capture the workflow secrets.** `VM_HOST` for `../IMPLEMENTATION_GUIDE.md` §11.3 is the
**tailnet** address, not the public IP:

```bash
# Run from a machine already on the tailnet -- the VM does not accept public SSH from CI.
ssh-keyscan -t ed25519 100.x.y.z
```

Use the `100.x.y.z` address rather than the MagicDNS name for `VM_HOST`. Both work, but the IP
removes a DNS dependency from the CI path, and `VM_KNOWN_HOSTS` has to match whatever form the
workflow connects to.

**6.5 Confirm the public path is unchanged.** Tailscale should have added no inbound exposure:

```bash
# Still exactly one ingress rule (§4), and from off-net this still times out:
nc -vz -w 5 <public-ip> 22
```

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `tailscale up` → `tag not permitted` | Tag missing from `tagOwners`, or you are not an Owner/Admin of the tailnet | Step 2 |
| Console shows the node owned by *you* rather than by the tag | `--advertise-tags` was omitted | Step 3.5 |
| SSH over the tailnet hangs; public SSH works | ACL lacks the `autogroup:member` rule — tagging transferred ownership away from you | Step 2, second ACL rule |
| SSH over the tailnet refused, ACL looks right | firewalld | Step 3.4 |
| `getent hosts bankdemo` returns `100.x.y.z` | `--accept-dns=false` missing, and `/etc/hosts` lacks the entry | Step 3.3 + §12 |
| DataNodes won't register after adding Tailscale | Same root cause as above | As above |
| CI fails at the Tailscale step | OAuth client has no tag, wrong scope, or `tag:ci` absent from `tagOwners` | Step 5.1 |
| CI joins but SSH times out | ACL `src` is not `tag:ci`, or `VM_HOST` is still the public IP | Steps 2 and 6.4 |
| Node vanished from the tailnet months later | Key expiry — the node was untagged | Step 3.5, then re-auth |

### Break-glass

**Keep the `/32` ingress rule.** If Tailscale is your only path in and `tailscaled` fails to
start after a kernel update, you are locked out of a box with no console access configured.
The `/32` costs nothing and is your own normal path in anyway. OCI's serial console is the
second fallback — worth enabling once under instance details → **Console connection** so you
find out it works before you need it.

### What this does not give you

Tailscale makes the VM reachable **by machines you have authorised**. It does not, and is not
meant to, let anyone else run the stack — see §14.

## 5. The instance

**Where:** **Compute → Instances → Create Instance**.

| Field | Value |
|---|---|
| Name | `bankdemo` |
| Compartment | `bankdemo` |
| Image | **Oracle Linux 9** — click *Change image*, and confirm the **aarch64** build |
| Shape | **VM.Standard.A1.Flex**, 2 OCPU / 12 GB (or whatever §0 established) |
| VCN / subnet | `bankdemo-vcn` / the **public** subnet |
| Public IPv4 address | **Assign** |
| SSH keys | paste the public key from below |

**The image must be aarch64.** A1 is Ampere, and the spec downloads
`hadoop-<ver>-aarch64.tar.gz` specifically. An x86 image on an A1 shape isn't offered, but an
x86 *shape* is easy to pick by accident if A1 capacity is unavailable and you take what the
console suggests instead.

**The hostname must be `bankdemo`.** Every rendered config, the Kafka `advertised.listeners`,
`fs.defaultFS`, and the systemd units all reference it by name. The instance *display name*
sets the initial hostname, so getting it right here saves a fixup later.

Generate a dedicated keypair first — don't reuse a personal key for a box that will also hold
a CI key:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/bankdemo -C "bankdemo-oci"
cat ~/.ssh/bankdemo.pub          # paste this into the console
```

Under **Boot volume**, leave the defaults (46.6 GB is the Always Free boot size).

If you hit **"Out of host capacity"**, see §11 — it's common and it isn't your mistake.

Once it's running, note the **public IP** from the instance details page and confirm you can
get in:

```bash
ssh -i ~/.ssh/bankdemo opc@<public-ip>      # opc is the default user on Oracle Linux images
```

## 6. The block volume — attach, then format carefully

All service data lives on `/data`: HDFS NameNode and DataNode directories, the loop-filesystem
images, Kafka logs, run bundles. 50 GB is the spec's suggestion and fits the Always Free block
storage allowance alongside the boot volume.

**Create it:** **Storage → Block Volumes → Create Block Volume**.

| Field | Value |
|---|---|
| Name | `bankdemo-data` |
| Compartment | `bankdemo` |
| Size | 50 GB |
| Performance | **Balanced** (Always Free-eligible) |

**Attach it:** instance details → **Attached block volumes** → **Attach block volume**.

| Field | Value |
|---|---|
| Volume | `bankdemo-data` |
| Attachment type | **Paravirtualized** |
| Access | Read/write |

Paravirtualized matters: iSCSI attachment requires running a set of `iscsiadm` commands the
console hands you, and paravirtualized needs none of that.

### Format it — read this part before running it

> ⚠️ **`mkfs` on the wrong device destroys the instance.** Kernel names (`/dev/sdb`) are not
> stable across reboots or attachment order, and the boot volume sits on the same controller.
> `../IMPLEMENTATION_GUIDE.md` §4.2 originally said `mkfs.xfs /dev/sdb`; that was corrected to
> the procedure below for exactly this reason.

Oracle Linux images ship `oci-utils`, which maintains stable symlinks under
`/dev/oracleoci/`. Use those, and confirm before writing:

```bash
# 1. Look at what's actually attached. The 50G device should have no mountpoint
#    and no partitions; the boot volume will be ~46.6G and mounted on /.
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,LABEL

# 2. Resolve the consistent path and check it points where you expect.
ls -l /dev/oracleoci/
DEV=/dev/oracleoci/oraclevdb
readlink -f "$DEV"                    # cross-check against lsblk's 50G device

# 3. Confirm it is empty. Output here means STOP and re-check.
sudo blkid "$DEV" || echo "no filesystem found — safe to format"

# 4. Only now:
sudo mkfs.xfs "$DEV"
```

### Mount it by UUID

Mount by UUID, never by device name, and use `nofail` so a missing volume doesn't strand the
instance at boot:

```bash
sudo mkdir -p /data
UUID=$(sudo blkid -s UUID -o value /dev/oracleoci/oraclevdb)
echo "UUID=$UUID  /data  xfs  defaults,nofail  0  2" | sudo tee -a /etc/fstab
sudo mount -a
df -h /data                            # expect ~50G, mounted
```

The installer's `20-storage.sh` creates the loop-filesystem images inside `/data` later. It
expects `/data` to already be a mountpoint with at least 30 GB free — that's a preflight check
(§5.1), and it will refuse to proceed otherwise.

## 7. The `bankops` user

The stack's application user. The installer adds its group memberships and the restricted
sudoers rule later; this step only has to get it far enough to SSH in and run the installer.

```bash
sudo useradd -m bankops
sudo install -d -m 700 -o bankops -g bankops /home/bankops/.ssh
sudo cp /home/opc/.ssh/authorized_keys /home/bankops/.ssh/authorized_keys
sudo chown bankops:bankops /home/bankops/.ssh/authorized_keys
sudo chmod 600 /home/bankops/.ssh/authorized_keys
```

## 8. Bootstrap sudo — temporary, and replaced in Phase 8

> ⚠️ **This grants `bankops` unrestricted root.** It exists so the installer can run at all.
> `../IMPLEMENTATION_GUIDE.md` §11.1 replaces it with a restricted `Cmnd_Alias` and then
> deletes this file. Don't leave it in place after Phase 8.

Always validate a sudoers file before installing it — a syntax error here can lock you out of
sudo entirely:

```bash
echo 'bankops ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/99-bankops-bootstrap
sudo visudo -cf /etc/sudoers.d/99-bankops-bootstrap     # must print "parsed OK"
sudo chmod 440 /etc/sudoers.d/99-bankops-bootstrap
```

**Keep the `opc` user as break-glass access.** It retains the image's default sudo, and §11.1
requires you to confirm `ssh opc@VM sudo -v` still works *before* the bootstrap file is
removed. If the restricted rule turns out to be wrong, `opc` is how you fix it.

## 9. Local configuration

On your own machine, in the repo:

```bash
cd infra/bankdemo
cp .env.example .env
```

| Variable | Value |
|---|---|
| `VM_HOST` | the instance's public IP |
| `VM_USER` | `bankops` |
| `SSH_KEY` | `~/.ssh/bankdemo` |

`.env` is gitignored (`../CLAUDE.md`: never commit secrets). Verify that before you fill it in:
`git check-ignore -v infra/bankdemo/.env` should print a matching rule.

A host alias makes the tunnel and deploy commands shorter — add to `~/.ssh/config`:

```
Host bankdemo
    HostName <public-ip>
    User bankops
    IdentityFile ~/.ssh/bankdemo
```

## 10. Verify

This is the Phase 1 acceptance check from `../IMPLEMENTATION_GUIDE.md` §4.2. Run it from your
machine and paste the output into `docs/PROGRESS.md`:

```bash
ssh bankops@<public-ip> 'uname -m; nproc; free -g; df -h /data; head -2 /etc/os-release'
```

| Expect | Meaning |
|---|---|
| `aarch64` | Ampere, matching the Hadoop tarball the installer fetches |
| `2` (or `4`) | OCPU count — must match what §0 established |
| ~11 GB total (or ~23) | `free -g` rounds down; the preflight check reads `/proc/meminfo` in KiB instead |
| `/data` ~50 G | Block volume mounted |
| `Oracle Linux Server 9.x` | The preflight requires ID `ol`, major version 9 |

Also confirm sudo works without a password prompt, since the installer depends on it:

```bash
ssh bankops@<public-ip> 'sudo -n true && echo "sudo OK"'
```

If all five lines match, B2's `[HUMAN]` portion is done and Claude Code can begin installer
stages 00–40.

---

## 11. When A1 capacity isn't available

**"Out of host capacity"** on instance creation is the single most common blocker here, and
it is a real shortage in the region, not a misconfiguration. Options, roughly in order:

1. **Retry in a different availability domain or fault domain.** In multi-AD regions the
   console lets you pick; capacity differs between them.
2. **Retry at a different time.** Capacity is released as other tenancies free it. Off-peak
   hours for the region's timezone are meaningfully better.
3. **Script the retry.** The OCI CLI's `oci compute instance launch` can be looped with a
   backoff — creating an instance is idempotent enough that a failed launch costs nothing.
4. **Consider a different home region** — but only before you have resources there, since the
   home region is permanent.

Do **not** work around it by taking an x86 shape. The entire stack is specified for aarch64,
and the Always Free x86 shapes (1/8 OCPU, 1 GB) cannot run any of this.

## 12. Two things the installer fixes that you should know about now

Both are already handled in `../IMPLEMENTATION_GUIDE.md` §5.2, but they surface as confusing
failures if the installer is ever run partially, so they're worth recognising:

- **cloud-init rewrites the hostname and `/etc/hosts` on every boot.** Left alone, `bankdemo`
  gets remapped to `127.0.0.1`, which breaks DataNode registration and WebHDFS redirects —
  days later, looking like an HDFS fault. Stage `10-os-base.sh` writes
  `/etc/cloud/cloud.cfg.d/99-bankdemo.cfg` with `preserve_hostname: true` and
  `manage_etc_hosts: false` to stop it. The §12 reboot test is what proves it held.
- **`bankdemo` must resolve to the private IP, not loopback.** Same root cause, and it's the
  first thing to check if HDFS behaves strangely after a reboot:
  `getent hosts bankdemo` should return `10.0.0.x`, never `127.0.0.1`.

## 13. What happens next

Once §10 passes:

```bash
make -C infra/bankdemo deploy      # rsync the subtree to the VM and run install.sh
```

Web UIs are never exposed — reach them through the tunnel:

```bash
infra/bankdemo/scripts/tunnel.sh   # forwards 9870, 8088, 18080 over SSH
```

Then B2 continues with installer stages 00–40, the stack build, and the **Phase 3.5 budget
gate** (`../IMPLEMENTATION_GUIDE.md` §6.8) — which is where you find out whether §0's answer
and §2.3's memory table actually agree with the hardware.

## 14. Letting other people evaluate the project

Worth separating from the network question above, because they get conflated easily.

**Nobody outside the repo can trigger a run on your VM through the options in §4.**
`workflow_dispatch` requires write access to the repository — a visitor to the demo site, or
anyone who forks the repo, cannot dispatch it. Tailscale narrows reachability further still.

A visitor-facing trigger exists only as the optional broker in
[`../../demo-broker/README.md`](../../demo-broker/README.md), and it does not change this: the
VM still accepts no inbound connection. It polls the broker outbound, yields to every other
caller on the box, accepts no visitor-supplied arguments, and is capped per IP and per day.

That is the correct design, not a gap to close. `bankdemo run` executes as root via sudo, takes
an exclusive `flock` (so a single stranger blocks every other run, including your cron), accepts
`--seed` and `--faults` arguments, and runs on a free-tier box that holds your answer keys in
`/var/lib/bankdemo/keys/`. Exposing that to the public internet would be a bad idea however it
was authenticated.

What reviewers can actually do, in increasing order of effort:

| Effort | What they get | Status |
|---|---|---|
| None | The [GitHub Pages demo](https://hpk369.github.io/ai-qa-agent/) — client-side simulation of severity classification and the Slack Block Kit output | **Exists** |
| ~2 min | `docker compose --profile lite up` — runs the real `agent/` triage code against the Postgres/Kafka mock. This is why the lite path is kept permanently (`/ROADMAP.md` §2) | **Exists** |
| None | A **random recent real incident**, served instantly from the live demo feed — cron generates a fresh one six times a day, each with different randomly-selected faults (`../IMPLEMENTATION_GUIDE.md` §9.6) | **Roadmap B6.1** |
| ~20 min | Download the **full bundle** for any run in the feed and triage it themselves: read `ticket.json`, work the evidence, fill in `RCA_TEMPLATE.yaml`, then compare against the published postmortem | **Roadmap B6.1** |
| ~1 hour | Provision their own Always Free VM and run this document plus `make deploy`. The installer is idempotent and the Phase 9 rebuild drill (`../IMPLEMENTATION_GUIDE.md` §12) exists precisely to prove a stranger can do this | **Roadmap B6** |

Rows two and three are the ones worth building deliberately.

The **live feed** is the closest thing to "let visitors run the stack", and it is better than
the literal version: a real request-a-run button would make them wait ~13 minutes for the run
to finish, whereas a visitor cannot tell whether the incident they were handed was generated on
their click or ninety minutes ago. Instant, and no public trigger to secure.

The **downloadable bundle** gives a reviewer the genuine artefact — real YARN container logs,
real `dfsadmin` output, a real alert timeline — without any infrastructure, and lets them check
their own triage against the published postmortem. Strictly better evidence than a screenshot,
because they can grep it.

Publishing bundles is safe by construction: §9.1's redaction strips credentials, and §10.2's
salted fault selection means the embedded seed does not reveal the answer key. Both of those
properties are load-bearing here — verify them before the first release rather than after.

---

## What this does not cover

- **Anything Claude Code can do.** Installer stages, config rendering, systemd units, HDFS
  formatting and cluster init are all automated from `make deploy` onward. This document stops
  where automation starts.
- **The restricted sudoers rule** (§11.1) and the **GitHub Actions secrets** (§11.3) — both are
  Phase 8 `[HUMAN]` steps, done after the stack works, and documented there rather than here.
- **`fail2ban`, `dnf-automatic`, firewalld assertions** — Phase 9 hardening, applied by the
  installer, not by hand.
- **Verification that any of this works.** Written against the spec and OCI's documented
  behaviour; no VM has been provisioned from it yet. Console labels in particular drift between
  UI revisions — if a menu path here doesn't match what you see, trust the console and correct
  this file.
