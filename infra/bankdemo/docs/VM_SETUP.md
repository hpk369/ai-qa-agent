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

## 4. Security list — TCP 22 and nothing else

This is the one network control that matters. `../CLAUDE.md` makes it a hard constraint: only
TCP 22 is reachable from outside, and every web UI (NameNode 9870, ResourceManager 8088,
Spark History 18080) is reached by SSH tunnel instead.

**Where:** your VCN → **Security Lists** → **Default Security List** → **Ingress Rules**.

Delete anything that isn't SSH, then confirm exactly one ingress rule remains:

| Source CIDR | Protocol | Dest. port | Notes |
|---|---|---|---|
| `<your IP>/32` | TCP | 22 | Preferred. Find yours with `curl -s ifconfig.me` |

**If you intend to use the GitHub Actions trigger** (`../IMPLEMENTATION_GUIDE.md` §11.3), the
source must be `0.0.0.0/0` — GitHub's hosted-runner IP ranges are enormous and change
constantly, so pinning them is not practical. That is a real exposure, and the spec
compensates for it deliberately: key-only auth (§8), `fail2ban` with a 1-hour ban (§12), and
a dedicated CI key restricted with `no-port-forwarding,no-agent-forwarding,no-X11-forwarding`
in `authorized_keys`. Accept those compensations or skip the Actions trigger; don't open the
CIDR and then skip the hardening.

Leave the egress rule alone — the installer downloads Hadoop, Spark and Kafka.

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
