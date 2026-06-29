# Deploying the Webhook Server (Oracle Cloud Always-Free)

End-to-end steps to run the `agent/` FastAPI webhook server on a free Oracle
Cloud VM and wire GitHub Actions to it. Every gotcha hit during the first
deploy is called out inline so this is reproducible.

The flow being built:
`push to main/bug/* → GitHub Actions runs pytest → POST result to this server`.

---

## 1. Create the VM

Oracle Cloud Console → **Compute → Instances → Create**:

- **Image:** Canonical Ubuntu 22.04
- **Shape:** `VM.Standard.A1.Flex` (Ampere ARM), 1 OCPU / 6 GB — must show
  **"Always Free-eligible"**. If "out of capacity", try another Availability
  Domain or the AMD `VM.Standard.E2.1.Micro`.
- **Networking:** create a new VCN + **public subnet**.
- **SSH keys:** "Generate a key pair for me" → **download the private key**
  (Oracle names it `ssh-key-YYYY-MM-DD.key`). No key = locked out.

> Always Free never charges. A card is taken for identity only (a ~$1 hold
> that reverses in a few days). Never click "Upgrade to Paid".

### Assign a public IP

If the create wizard's "Assign public IPv4" toggle was greyed:

1. Instance → **Networking → Attached VNICs** → the primary VNIC.
2. If you see a **"Connect public subnet to internet"** quick action, click
   **Connect** (creates the internet gateway + route).
3. **IP administration → IPv4 addresses** → edit the primary row →
   **Ephemeral public IP** → Update.

Note the public IP (referred to below as `<EC2_IP>`).

---

## 2. Open port 8000 (TWO firewalls)

Both are required — the cloud firewall *and* the VM's own iptables.

### a) Oracle Security List (cloud firewall)

Subnet → **Security Lists → Default Security List → Add Ingress Rules**:

- Stateless: **off**
- Source CIDR: `0.0.0.0/0`
- IP Protocol: **TCP**
- Destination Port Range: `8000`

### b) VM iptables — **must sit before the REJECT**

Oracle Ubuntu images ship an iptables ruleset ending in a catch-all REJECT.
A naive append lands *after* it and does nothing. Insert *before* the REJECT
(it was line 5 on a fresh image — check with `-L --line-numbers`):

```bash
sudo iptables -I INPUT 5 -p tcp --dport 8000 -j ACCEPT
sudo iptables -L INPUT -n --line-numbers   # 8000 ACCEPT must be ABOVE the REJECT
sudo netfilter-persistent save
```

---

## 3. SSH in

From your machine (path = wherever the key downloaded):

```bash
ssh -i ssh-key-YYYY-MM-DD.key ubuntu@<EC2_IP>
```

On Windows PowerShell, lock the key perms first or SSH refuses it:
```powershell
icacls ssh-key-YYYY-MM-DD.key /inheritance:r /grant:r "$($env:USERNAME):R"
```

---

## 4. Install & run the server

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv git
git clone https://github.com/RayirthDinesh/Self-Healing-Autonomous-DevOps-Agent.git
cd Self-Healing-Autonomous-DevOps-Agent
git checkout main          # the agent/ dir lives on main, NOT the bug/* branches
cd agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### .env — write it WITHOUT a BOM

`load_dotenv()` reads `WEBHOOK_SECRET` from `.env`. A UTF-8 BOM makes the key
parse as `﻿WEBHOOK_SECRET`, so the real var stays unset and every request
401s. PowerShell `Set-Content -Encoding utf8` adds a BOM — use Linux `printf`
on the box instead:

```bash
printf 'WEBHOOK_SECRET=<your-secret>\n' > .env
```

### Smoke test

```bash
python main.py            # → Uvicorn running on http://0.0.0.0:8000
curl http://localhost:8000/health        # {"status":"ok"}
```

---

## 5. Run as a service (survives reboot / logout)

```bash
sudo tee /etc/systemd/system/sre-agent.service > /dev/null <<'EOF'
[Unit]
Description=SRE Agent Webhook Server
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/Self-Healing-Autonomous-DevOps-Agent/agent
ExecStart=/home/ubuntu/Self-Healing-Autonomous-DevOps-Agent/agent/.venv/bin/python main.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

pkill -f "python main.py"          # free the port if the smoke-test is still up
sudo systemctl daemon-reload
sudo systemctl enable --now sre-agent
sudo systemctl status sre-agent --no-pager
```

Logs: `sudo journalctl -u sre-agent -f`

---

## 6. GitHub secrets

Repo → **Settings → Secrets and variables → Actions** → add **both**:

| Name | Value |
|------|-------|
| `WEBHOOK_URL` | `http://<EC2_IP>:8000/webhook` |
| `WEBHOOK_SECRET` | same string as the box `.env` |

Both are required — the workflow sends `X-Webhook-Secret`, and the server 401s
without a match.

---

## 7. The httptools gotcha (important)

`ci.yml` posts the payload with `curl`. For bodies > 1 KB (the pytest logs are
several KB) curl adds `Expect: 100-continue`. uvicorn's **default h11 parser
mishandles this over real network latency** — the body arrives after uvicorn
has already replied, so FastAPI sees an empty body and returns **422
Unprocessable Entity** (with `Invalid HTTP request received` in the log). It
works fine from localhost because there's no latency to expose the race.

Fix is server-side and client-agnostic: use the **httptools** parser.
`agent/requirements.txt` pins `uvicorn[standard]` (bundles httptools) and
`main.py` runs `uvicorn.run(..., http="httptools")`. The workflow also sends
`-H "Expect:"` as defense in depth.

Verify over WAN (not localhost) with a large body:
```bash
curl -sS -X POST http://<EC2_IP>:8000/webhook \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: <secret>" \
  -d '{"repo":"r","branch":"b","commit_sha":"c","workflow_run_id":"1","test_logs":"'"$(head -c 6000 /dev/zero | tr '\0' X)"'","status":"failure"}'
# → {"received":true,"action":"triggering agent"}
```

---

## 8. End-to-end check

1. `sudo journalctl -u sre-agent -f` on the box.
2. Push to (or re-run) a `bug/*` branch → CI fails → server logs
   `=== CI FAILURE ===` with branch, commit, and the full pytest traceback.
3. Push to `main` (green) → `CI success ... no action needed`.

---

## Operational notes

- **Public IP changes** on instance stop/start. Either keep it running or
  attach a **Reserved (static) public IP**, then update the `WEBHOOK_URL`
  secret.
- Plain HTTP — the secret travels in a cleartext header. Fine for a portfolio
  demo; for real use, front it with HTTPS (nginx/Caddy + Let's Encrypt).
- `Invalid HTTP request received` lines with no client IP are internet bots
  probing port 8000 — harmless noise, not CI traffic.
