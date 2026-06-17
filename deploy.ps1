#Requires -Version 5.1
<#
.SYNOPSIS
  Blue-green deploy of the MASE service to AWS ECS Fargate behind an ALB.

.DESCRIPTION
  On each run:
    1. Builds the Docker image IN AWS CODEBUILD (zips the repo -> S3 -> CodeBuild
       builds the Dockerfile and pushes to ECR). No local Docker required.
    2. Registers a new ECS task-definition revision (secrets injected from
       Secrets Manager `mase/app-env`, enumerated dynamically).
    3. Deploys it to the IDLE colour (the target group the ALB is NOT currently
       serving), scales it up, and waits for it to pass health checks.
    4. Flips the ALB listener to the new colour  -> new requests hit the new
       version immediately.
    5. Drains and scales down the OLD colour. In-flight HTTP requests finish
       during the target group's 300s deregistration delay; nothing is hard-killed.

  Idempotent: creates the two API services on first run, updates them thereafter.

.PARAMETER DesiredCount
  Number of API tasks to run on the live colour (default 2, one per AZ).

.PARAMETER Tag
  Image tag to build/push. Defaults to <git-short-sha>-<utc-timestamp>.

.PARAMETER SkipBuild
  Reuse an already-pushed image tag (must pass -Tag); skips the CodeBuild step.

.EXAMPLE
  .\deploy.ps1
  .\deploy.ps1 -DesiredCount 3
  .\deploy.ps1 -SkipBuild -Tag 1a2b3c4-20260606T101500Z
#>
[CmdletBinding()]
param(
    [int]$DesiredCount = 2,
    [string]$Tag,
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ----------------------------------------------------------------------------
# Configuration  (resource IDs from the provisioned infra)
# ----------------------------------------------------------------------------
$AWS         = "C:\Program Files\Amazon\AWSCLIV2\aws.exe"
$Region      = "ap-south-1"
$Account     = "022187637784"
$Cluster     = "mase-cluster"
$EcrRepo     = "$Account.dkr.ecr.$Region.amazonaws.com/mase-service"
$BuildBucket = "mase-build-source-$Account"   # S3 source for CodeBuild
$CodeBuildProject = "mase-build"               # builds image in AWS (no local Docker)
$Family      = "mase-api"
$Container   = "mase-api"
$ContainerPort = 5000
$LogGroup    = "/ecs/mase-service"
$SecretId    = "mase/app-env"
$SecretArn   = "arn:aws:secretsmanager:${Region}:${Account}:secret:mase/app-env-Adtn25"
$ExecRoleArn = "arn:aws:iam::${Account}:role/mase-ecs-task-execution-role"
$TaskRoleArn = "arn:aws:iam::${Account}:role/mase-ecs-task-role"
$ListenerArn = "arn:aws:elasticloadbalancing:${Region}:${Account}:listener/app/mase-alb/176c820e3f56b935/c6710f58972ca338"
$Cpu         = "1024"   # 1 vCPU
$Memory      = "2048"   # 2 GB
$HealthGrace = 180      # seconds before ECS starts counting ALB health failures
$Subnets     = @("subnet-0955d615cd152126d","subnet-0f5049ee68d42de10","subnet-0df401a040a6b54e0")
$TaskSg      = "sg-0067ce3ab08beb09c"

# Colour <-> target group / service mapping
$Colours = @{
    blue  = @{ Tg = "arn:aws:elasticloadbalancing:${Region}:${Account}:targetgroup/mase-blue/71c71534374ec831";  Service = "mase-api-blue"  }
    green = @{ Tg = "arn:aws:elasticloadbalancing:${Region}:${Account}:targetgroup/mase-green/c8b1ab1c4dff2dbf"; Service = "mase-api-green" }
}

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# ----------------------------------------------------------------------------
# 0. Preflight
# ----------------------------------------------------------------------------
Write-Step "Preflight"
& $AWS sts get-caller-identity --query Arn --output text | Out-Host
# No local Docker required — the image is built in AWS CodeBuild.

if (-not $Tag) {
    $sha = $null
    if (Get-Command git -ErrorAction SilentlyContinue) { $sha = (git rev-parse --short HEAD 2>$null) }
    if (-not $sha) { $sha = "notag" }
    $Tag = "$sha-$((Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))"
}
$ImageUri = "${EcrRepo}:${Tag}"
Write-Host "Image: $ImageUri"

# ----------------------------------------------------------------------------
# 1. Build + push image  (in AWS CodeBuild — no local Docker)
# ----------------------------------------------------------------------------
if (-not $SkipBuild) {
    Write-Step "Stage source for CodeBuild"
    # Mirror the repo into a temp dir, excluding heavy/irrelevant content, then
    # zip it. The Dockerfile's .dockerignore still applies inside CodeBuild.
    $stage = Join-Path $env:TEMP "mase-src-$Tag"
    $excludeDirs = @('.git','mcp_output','exports','attached_assets','__pycache__',
                     '.pytest_cache','.local','.config','.claude','.aws','.canvas',
                     '.agents','.venv','venv','.mypy_cache','.pythonlibs')
    robocopy $PSScriptRoot $stage /MIR /XD $excludeDirs /XF *.log /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy staging failed (exit $LASTEXITCODE)" }

    $zip = Join-Path $env:TEMP "mase-src-$Tag.zip"
    if (Test-Path $zip) { Remove-Item -LiteralPath $zip -Force }
    Add-Type -AssemblyName System.IO.Compression            # ZipArchiveMode, ZipArchive
    Add-Type -AssemblyName System.IO.Compression.FileSystem # ZipFile, ZipFileExtensions
    # NOTE: do NOT use ZipFile::CreateFromDirectory here — on .NET Framework
    # (PowerShell 5.1) it writes BACKSLASH path separators, which Linux/CodeBuild
    # treats as part of the filename rather than a directory separator, collapsing
    # every subdirectory (custom_tools/, prompts/, ...) into oddly named root files.
    # Build entries manually with FORWARD slashes. Relative paths are computed with
    # Resolve-Path -Relative (not Substring math) to avoid short(8.3)-vs-long path
    # length mismatches that would truncate entry names.
    $archive = [System.IO.Compression.ZipFile]::Open($zip, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        # Use .NET DirectoryInfo so the base path and each file path come from the
        # SAME resolution (no short-8.3-vs-long mismatch, no Push-Location quirks).
        $dir = New-Object System.IO.DirectoryInfo($stage)
        $baseLen = $dir.FullName.TrimEnd('\').Length + 1
        foreach ($f in $dir.GetFiles('*', [System.IO.SearchOption]::AllDirectories)) {
            $rel = $f.FullName.Substring($baseLen) -replace '\\','/'
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $archive, $f.FullName, $rel,
                [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
    } finally { $archive.Dispose() }
    $zipSize = [Math]::Round((Get-Item $zip).Length / 1MB, 1)
    Write-Host "Source zip: $zip ($zipSize MB, forward-slash entries)"

    Write-Step "Upload source to S3"
    $s3key = "sources/mase-src-$Tag.zip"
    & $AWS s3 cp $zip "s3://$BuildBucket/$s3key" --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "s3 upload failed" }

    Write-Step "Start CodeBuild"
    $buildId = & $AWS codebuild start-build `
        --project-name $CodeBuildProject `
        --source-location-override "$BuildBucket/$s3key" `
        --source-type-override S3 `
        --environment-variables-override "name=IMAGE_TAG,value=$Tag,type=PLAINTEXT" `
        --query "build.id" --output text
    if ($LASTEXITCODE -ne 0) { throw "start-build failed" }
    Write-Host "Build: $buildId"

    Write-Step "Wait for CodeBuild to finish"
    $status = "IN_PROGRESS"
    while ($status -eq "IN_PROGRESS") {
        Start-Sleep -Seconds 15
        $status = & $AWS codebuild batch-get-builds --ids $buildId `
                    --query "builds[0].buildStatus" --output text
        $phase  = & $AWS codebuild batch-get-builds --ids $buildId `
                    --query "builds[0].currentPhase" --output text
        Write-Host ("  status={0} phase={1}" -f $status, $phase)
    }
    if ($status -ne "SUCCEEDED") {
        $logUrl = & $AWS codebuild batch-get-builds --ids $buildId `
                    --query "builds[0].logs.deepLink" --output text
        throw "CodeBuild $status. Logs: $logUrl"
    }
    Write-Host "Image built and pushed: $ImageUri"
}

# ----------------------------------------------------------------------------
# 2. Register a new task-definition revision (secrets enumerated from the secret)
# ----------------------------------------------------------------------------
Write-Step "Register task definition"
$secretString = & $AWS secretsmanager get-secret-value --secret-id $SecretId --query SecretString --output text
if ($LASTEXITCODE -ne 0) { throw "could not read secret $SecretId" }
$secretKeys = ($secretString | ConvertFrom-Json).PSObject.Properties.Name
$secretsJson = ($secretKeys | ForEach-Object {
    '{"name":"' + $_ + '","valueFrom":"' + $SecretArn + ':' + $_ + '::"}'
}) -join ','
Write-Host "Injecting $($secretKeys.Count) secret keys"

$taskDefJson = @"
{
  "family": "$Family",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "$Cpu",
  "memory": "$Memory",
  "executionRoleArn": "$ExecRoleArn",
  "taskRoleArn": "$TaskRoleArn",
  "containerDefinitions": [
    {
      "name": "$Container",
      "image": "$ImageUri",
      "essential": true,
      "portMappings": [{ "containerPort": $ContainerPort, "protocol": "tcp" }],
      "environment": [
        { "name": "HOST", "value": "0.0.0.0" },
        { "name": "PORT", "value": "$ContainerPort" }
      ],
      "secrets": [ $secretsJson ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "$LogGroup",
          "awslogs-region": "$Region",
          "awslogs-stream-prefix": "api"
        }
      },
      "healthCheck": {
        "command": ["CMD-SHELL", "curl -fsS http://127.0.0.1:$ContainerPort/api/health || exit 1"],
        "interval": 30, "timeout": 5, "retries": 3, "startPeriod": 60
      }
    }
  ]
}
"@
$tdFile = Join-Path $env:TEMP "mase-taskdef-$Tag.json"
$taskDefJson | Out-File -FilePath $tdFile -Encoding ascii
$tdArn = & $AWS ecs register-task-definition --cli-input-json "file://$tdFile" --query "taskDefinition.taskDefinitionArn" --output text
if ($LASTEXITCODE -ne 0) { throw "register-task-definition failed" }
Write-Host "Registered $tdArn"

# ----------------------------------------------------------------------------
# 3. Determine live vs idle colour from the ALB listener weights
# ----------------------------------------------------------------------------
# The listener uses a single weighted-forward default action referencing BOTH
# target groups (so both stay associated with the ALB and ECS can attach a
# service to either). The "live" colour is whichever target group has weight > 0.
Write-Step "Determine live/idle colour"
$tgJson = & $AWS elbv2 describe-listeners --listener-arns $ListenerArn `
            --query "Listeners[0].DefaultActions[0].ForwardConfig.TargetGroups" --output json
if ($LASTEXITCODE -ne 0) { throw "describe-listeners failed" }
$liveTgArn = $null
try {
    $tgArr = $tgJson | ConvertFrom-Json
    $liveTgArn = (@($tgArr | Where-Object { [int]$_.Weight -gt 0 }) | Select-Object -First 1).TargetGroupArn
} catch { }
if (-not $liveTgArn) { $liveTgArn = $Colours.blue.Tg }   # cold-start default

if ($liveTgArn -eq $Colours.blue.Tg) { $live = "blue"; $idle = "green" }
else                                 { $live = "green"; $idle = "blue" }
Write-Host "Live colour: $live   ->   deploying to idle colour: $idle"

$idleService = $Colours[$idle].Service
$idleTg      = $Colours[$idle].Tg
$liveService = $Colours[$live].Service

# ----------------------------------------------------------------------------
# 4. Deploy to the idle colour (create service first time, else update)
# ----------------------------------------------------------------------------
Write-Step "Deploy idle service: $idleService"
$svcDesc = & $AWS ecs describe-services --cluster $Cluster --services $idleService `
             --query "services[?status=='ACTIVE'].serviceName" --output text 2>$null

$netCfg = "awsvpcConfiguration={subnets=[$($Subnets -join ',')],securityGroups=[$TaskSg],assignPublicIp=ENABLED}"

if ([string]::IsNullOrWhiteSpace($svcDesc)) {
    Write-Host "Creating service $idleService"
    & $AWS ecs create-service `
        --cluster $Cluster `
        --service-name $idleService `
        --task-definition $tdArn `
        --desired-count $DesiredCount `
        --launch-type FARGATE `
        --network-configuration $netCfg `
        --load-balancers "targetGroupArn=$idleTg,containerName=$Container,containerPort=$ContainerPort" `
        --health-check-grace-period-seconds $HealthGrace `
        --query "service.serviceName" --output text | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "create-service failed" }
} else {
    Write-Host "Updating service $idleService"
    & $AWS ecs update-service `
        --cluster $Cluster `
        --service $idleService `
        --task-definition $tdArn `
        --desired-count $DesiredCount `
        --health-check-grace-period-seconds $HealthGrace `
        --query "service.serviceName" --output text | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "update-service failed" }
}

Write-Step "Wait for idle service to stabilise"
& $AWS ecs wait services-stable --cluster $Cluster --services $idleService
if ($LASTEXITCODE -ne 0) { throw "service did not stabilise" }

Write-Step "Wait for idle target group to be healthy"
$healthy = $false
for ($i = 0; $i -lt 40; $i++) {
    $states = & $AWS elbv2 describe-target-health --target-group-arn $idleTg `
                --query "TargetHealthDescriptions[].TargetHealth.State" --output text
    Write-Host "  target health: $states"
    $arr = @($states -split "\s+" | Where-Object { $_ })
    if ($arr.Count -ge 1 -and (@($arr | Where-Object { $_ -ne 'healthy' }).Count -eq 0)) {
        $healthy = $true; break
    }
    Start-Sleep -Seconds 15
}
if (-not $healthy) { throw "idle target group never became fully healthy; ALB NOT flipped (old version still live)." }

# ----------------------------------------------------------------------------
# 5. Flip the ALB listener to the new colour
# ----------------------------------------------------------------------------
Write-Step "Flip ALB listener -> $idle"
# Weighted-forward swap: the new (idle) colour gets weight 100, old gets 0.
# Both target groups stay attached to the listener so they remain ALB-associated
# for the next deploy.
$newWeightBlue  = if ($idle -eq "blue")  { 100 } else { 0 }
$newWeightGreen = if ($idle -eq "green") { 100 } else { 0 }
$flipAction = '[{"Type":"forward","ForwardConfig":{"TargetGroups":[' +
    '{"TargetGroupArn":"' + $Colours.blue.Tg  + '","Weight":' + $newWeightBlue  + '},' +
    '{"TargetGroupArn":"' + $Colours.green.Tg + '","Weight":' + $newWeightGreen + '}]}}]'
$flipFile = Join-Path $env:TEMP "mase-flip-$Tag.json"
$flipAction | Out-File -FilePath $flipFile -Encoding ascii
& $AWS elbv2 modify-listener --listener-arn $ListenerArn `
    --default-actions "file://$flipFile" --query "Listeners[0].ListenerArn" --output text | Out-Host
if ($LASTEXITCODE -ne 0) { throw "listener flip failed" }
Write-Host "New requests now served by '$idle'."

# ----------------------------------------------------------------------------
# 6. Drain + scale down the old colour
# ----------------------------------------------------------------------------
Write-Step "Drain old colour: $liveService"
$oldActive = & $AWS ecs describe-services --cluster $Cluster --services $liveService `
               --query "services[?status=='ACTIVE'].serviceName" --output text 2>$null
if (-not [string]::IsNullOrWhiteSpace($oldActive)) {
    Write-Host "Scaling $liveService to 0 (300s connection draining lets in-flight requests finish)"
    & $AWS ecs update-service --cluster $Cluster --service $liveService --desired-count 0 `
        --query "service.serviceName" --output text | Out-Host
} else {
    Write-Host "No previous service to drain (first deploy)."
}

# ----------------------------------------------------------------------------
# 7. Sweep worker (standing service that drains the durable sweep_queue)
# ----------------------------------------------------------------------------
# The worker runs the SAME image but `python worker.py` (no HTTP server), so it
# has NO portMappings and NO /api/health healthCheck — that check would fail and
# ECS would kill the task. It keeps heavy sweeps OUT of the web process; restarts
# crash-safely (reclaims stale queue rows on boot). Kept current on every deploy.
Write-Step "Deploy sweep worker"
$workerTaskDefJson = @"
{
  "family": "mase-worker",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "$Cpu",
  "memory": "$Memory",
  "executionRoleArn": "$ExecRoleArn",
  "taskRoleArn": "$TaskRoleArn",
  "containerDefinitions": [
    {
      "name": "mase-worker",
      "image": "$ImageUri",
      "essential": true,
      "command": ["python", "worker.py"],
      "environment": [
        { "name": "DEAL_SWEEP_CONCURRENCY", "value": "2" },
        { "name": "MCP_SERVER_ALLOWLIST", "value": "salesforce,avoma" },
        { "name": "ANTHROPIC_MAX_RETRIES", "value": "8" },
        { "name": "LLM_REQUEST_TIMEOUT_S", "value": "600" },
        { "name": "DEAL_SWEEP_TIMEOUT_S", "value": "2400" },
        { "name": "DEAL_SWEEP_MAX_TRANSIENT_RETRIES", "value": "50" },
        { "name": "DEAL_SWEEP_MAX_TOKENS", "value": "64000" }
      ],
      "secrets": [ $secretsJson ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "$LogGroup",
          "awslogs-region": "$Region",
          "awslogs-stream-prefix": "worker"
        }
      }
    }
  ]
}
"@
$wtdFile = Join-Path $env:TEMP "mase-worker-taskdef-$Tag.json"
$workerTaskDefJson | Out-File -FilePath $wtdFile -Encoding ascii
$wtdArn = & $AWS ecs register-task-definition --cli-input-json "file://$wtdFile" --query "taskDefinition.taskDefinitionArn" --output text
if ($LASTEXITCODE -ne 0) { throw "register worker task-definition failed" }
Write-Host "Registered $wtdArn"

$workerNet = "awsvpcConfiguration={subnets=[$($Subnets -join ',')],securityGroups=[$TaskSg],assignPublicIp=ENABLED}"
$workerExists = & $AWS ecs describe-services --cluster $Cluster --services mase-worker `
                  --query "services[?status=='ACTIVE'].serviceName" --output text 2>$null
if ([string]::IsNullOrWhiteSpace($workerExists)) {
    Write-Host "Creating service mase-worker"
    & $AWS ecs create-service --cluster $Cluster --service-name mase-worker `
        --task-definition $wtdArn --desired-count 1 --launch-type FARGATE `
        --network-configuration $workerNet `
        --query "service.serviceName" --output text | Out-Host
} else {
    Write-Host "Updating service mase-worker"
    & $AWS ecs update-service --cluster $Cluster --service mase-worker `
        --task-definition $wtdArn --desired-count 1 `
        --query "service.serviceName" --output text | Out-Host
}
if ($LASTEXITCODE -ne 0) { throw "worker service deploy failed" }

Write-Step "DONE"
Write-Host "Deployed tag : $Tag"
Write-Host "Live colour  : $idle"
Write-Host "Endpoint     : http://mase-alb-1262623499.ap-south-1.elb.amazonaws.com/api/health"
