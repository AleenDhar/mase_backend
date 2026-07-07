---
name: aws-cli-access
description: You HAVE working AWS CLI access — use it directly (logs, ECS, etc.) without asking.
---

# AWS CLI access (standing capability — don't re-ask)

You have a working AWS CLI on this machine. Use it for CloudWatch logs, ECS ops, etc. — do NOT
tell the user you can't or ask permission each time.

- Binary: `C:\Program Files\Amazon\AWSCLIV2\aws.exe`
- Run via PowerShell with the Zscaler CA bundle + UTF-8 first:
  `$env:PYTHONUTF8="1"; $env:AWS_CA_BUNDLE="C:\Users\Aleen.Dhar\.aws\corp-ca-bundle.pem"`
- Region `ap-south-1`, cluster `mase-cluster`, services `mase-service` (API) + `mase-worker` (sweeps).
- ALB: http://mase-alb-1262623499.ap-south-1.elb.amazonaws.com ; token = DISPATCH_SECRET (from load_secret).
- Deploy = push to `main` → GitHub Actions renders task-defs (.github/deploy/render_taskdef.py) + blue-green.
