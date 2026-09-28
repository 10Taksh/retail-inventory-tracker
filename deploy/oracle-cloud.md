# Deploying on Oracle Cloud Always Free

One Always Free VM runs the whole app: uvicorn serves the FastAPI dashboard, Caddy sits in
front on port 80/443 with a login, and SQLite plus the uploaded invoice PDFs live on the VM's
persistent boot volume. Nothing here costs money except your Gemini API usage.

**What you get:** an Ampere A1 VM with up to 4 OCPUs and 24 GB RAM (or, if A1 capacity is
unavailable, an AMD `VM.Standard.E2.1.Micro` with 1 GB, which is enough), a public IPv4
address, and a disk that survives restarts.

> Already have a VM from another project? You can reuse it. This app installs to
> `/opt/retail-inventory` and uses port 8000 on localhost, **so it clashes with the schedule
> builder** (also on 8000, and both want Caddy's `:80`). Use a fresh VM. Always Free allows
> 2 × E2.1.Micro or up to 4 A1 OCPUs in total.

## 1. Create the account and pick a region

Sign up at <https://www.oracle.com/cloud/free/>. **The home region cannot be changed later**,
and Always Free resources only exist in the home region.

> **Recommended:** upgrade to **Pay As You Go** (Account → Upgrade). You are still not
> charged inside the Always Free limits, but A1 capacity is prioritised and Oracle stops
> reclaiming "idle" VMs, which a small dashboard like this usually is.

## 2. Create the VM

Console → **Compute → Instances → Create instance**.

| Setting | Value |
|---|---|
| Image | **Canonical Ubuntu 24.04** |
| Shape | **Ampere → VM.Standard.A1.Flex**, 1 OCPU / 6 GB (or AMD → `VM.Standard.E2.1.Micro`) |
| Networking | *Create new virtual cloud network*, **Assign a public IPv4 address: Yes** |
| SSH keys | Upload your public key, or generate a pair and **download the private key** |
| Boot volume | default |

Click **Create**. When the state is *Running*, note the **Public IP address**.

*Out of host capacity* for A1 → try another availability domain, try later, or use E2.1.Micro.

## 3. Open ports 80 and 443 in the cloud firewall

Console → **Networking → Virtual cloud networks → your VCN → Subnets → your subnet →
Security Lists → Default Security List → Add Ingress Rules**:

| Source CIDR | IP Protocol | Destination port range |
|---|---|---|
| `0.0.0.0/0` | TCP | `80` |
| `0.0.0.0/0` | TCP | `443` |

The VM's own iptables firewall also blocks these by default; `setup.sh` opens it.

## 4. Get a Gemini API key

Create one at <https://aistudio.google.com/apikey>. You'll paste it on the server in step 6,
never into git.

## 5. SSH in and run the setup script

From Windows PowerShell (fix the key's permissions first if ssh complains they're too open):

```powershell
ssh -i C:\path\to\ssh-key.key ubuntu@<PUBLIC-IP>
```

On the VM:

```bash
curl -fsSL https://raw.githubusercontent.com/10Taksh/retail-inventory-tracker/main/deploy/setup.sh -o setup.sh
sudo bash setup.sh
```

It installs Python, Caddy and sqlite3; opens the VM firewall; adds swap; creates an
`inventory` service user; clones the repo to `/opt/retail-inventory`; builds a virtualenv;
writes `/etc/retail-inventory.env`; starts the `retail-inventory` systemd service and Caddy;
and schedules a nightly backup. At the end it prints:

- the URL to open
- **the login (username `admin` + a generated password). It's shown once, so save it.**
- a warning that `GEMINI_API_KEY` isn't set yet

To pick your own login instead: `sudo AUTH_USER=me AUTH_PASS='a-strong-password' bash setup.sh`.

## 6. Add the Gemini key

```bash
sudo nano /etc/retail-inventory.env
```

Fill in `GEMINI_API_KEY=...`, save (Ctrl+O, Enter, Ctrl+X), then:

```bash
sudo systemctl restart retail-inventory
```

Open `http://<PUBLIC-IP>/`, log in, and upload an invoice PDF. The products appear in the table.

## 7. Optional: a domain and HTTPS

Point an `A` record at the public IP (any registrar, or a free subdomain from DuckDNS), then:

```bash
sudo DOMAIN=inventory.example.com bash /opt/retail-inventory/deploy/setup.sh
```

Caddy gets a Let's Encrypt certificate automatically. The domain is remembered for later runs.
**Do this if you use the site over public Wi-Fi.** On plain HTTP the login is sent unencrypted.

## Day-to-day

| Task | Command |
|---|---|
| Deploy a new version (after `git push`) | `sudo bash /opt/retail-inventory/deploy/setup.sh` |
| App logs | `journalctl -u retail-inventory -f` |
| Web server logs | `sudo tail -f /var/log/caddy/retail-inventory.log` |
| Restart the app | `sudo systemctl restart retail-inventory` |
| Change settings / API key | `sudo nano /etc/retail-inventory.env` then restart |
| Change the login | `sudo AUTH_PASS='new-password' bash /opt/retail-inventory/deploy/setup.sh` |
| Data | `/var/lib/retail-inventory/` (`inventory.db`, `invoices/`) |
| Backups | `/var/lib/retail-inventory/backups/`: nightly at 03:15, last 14 kept |
| Restore the DB | `sudo systemctl stop retail-inventory && zcat backups/inventory-….db.gz \| sudo -u inventory tee /var/lib/retail-inventory/inventory.db >/dev/null && sudo systemctl start retail-inventory` |

## Troubleshooting

- **Page never loads but `curl http://127.0.0.1:8000/api/health` works on the VM** → the
  security list (step 3) or VM iptables isn't open. Re-run `setup.sh`; check
  `sudo iptables -L INPUT -n --line-numbers` shows ACCEPT for 80/443 *above* the REJECT.
- **Upload fails with an API key / 422 error** → `GEMINI_API_KEY` missing or wrong (step 6);
  see `journalctl -u retail-inventory -n 50`.
- **Forgot the password** → set a new one (see *Change the login*).
- **Instance stopped by itself** → idle reclamation on a free-tier account; upgrade (step 1).
