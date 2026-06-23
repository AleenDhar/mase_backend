<#
  Tracked-opp datalake backfill (PowerShell so it uses the Windows cert store and
  works behind Zscaler — no AWS deploy needed).

  For each opp in the tracked list (the deal-engine book, ~444 opps), query Avoma
  for that opp's calls (by 18-char crm_opportunity_id) over the last 2 years and
  upsert each call's header + transcript + AI notes into the `datalake` Supabase.

  Resumable: completed opps are checkpointed to a local file, so a restart skips them.
  Run:  pwsh/powershell -File scripts\datalake_backfill_tracked.ps1   (loops all opps)
  Test: set $env:DL_MAX_OPPS=3 first.
#>
$ErrorActionPreference = "Continue"
$repo = "C:\Users\Aleen.Dhar\Downloads\Agent-Salesforce-Link (1)\Agent-Salesforce-Link"
$base = "http://mase-alb-1262623499.ap-south-1.elb.amazonaws.com"
$secrets = Get-Content (Join-Path $repo ".datalake_secrets.env")
$dlUrl = (($secrets | Where-Object {$_ -like 'DATALAKE_URL=*'}) -replace 'DATALAKE_URL=','').Trim()
$dlKey = (($secrets | Where-Object {$_ -like 'DATALAKE_SERVICE_KEY=*'}) -replace 'DATALAKE_SERVICE_KEY=','').Trim()
$deTok = (Select-String -Path "C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local" -Pattern '^DEAL_ENGINE_TOKEN=(.+)$').Matches[0].Groups[1].Value.Trim()
$avTok = "ifi116h6e8:2p7r6khoxqojr5638sld"
$lookbackDays = 730
$throttleMs = 250
$ckpt = Join-Path $env:TEMP "datalake_tracked_done.txt"
$maxOpps = if ($env:DL_MAX_OPPS) { [int]$env:DL_MAX_OPPS } else { 0 }

function To18($id15) {
  if (-not $id15 -or $id15.Length -lt 15) { return $id15 }
  $map = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"; $s = ""
  for ($c = 0; $c -lt 3; $c++) {
    $v = 0
    for ($i = 0; $i -lt 5; $i++) { $ch = $id15[$c*5+$i]; if ($ch -ge 'A' -and $ch -le 'Z') { $v = $v -bor (1 -shl $i) } }
    $s += $map[$v]
  }
  return $id15.Substring(0,15) + $s
}
function AvGet($path, $params) {
  $qs = ($params.GetEnumerator() | ForEach-Object { "$($_.Key)=$([uri]::EscapeDataString([string]$_.Value))" }) -join '&'
  $u = "https://api.avoma.com/v1$path" + $(if ($qs) { "?$qs" } else { "" })
  for ($try = 0; $try -lt 6; $try++) {
    try { return Invoke-RestMethod -Uri $u -Headers @{Authorization="Bearer $avTok"} -Method Get -TimeoutSec 50 }
    catch {
      $sc = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
      if ($sc -eq 429) { Start-Sleep -Seconds ([Math]::Min([Math]::Pow(2,$try), 30)); continue }
      return $null
    }
  }
  return $null
}
function DlUpsert($table, $rows, $onConflict) {
  if (-not $rows -or $rows.Count -eq 0) { return }
  $body = ConvertTo-Json @($rows) -Depth 25
  $headers = @{ apikey=$dlKey; Authorization="Bearer $dlKey"; "Content-Type"="application/json"; Prefer="resolution=merge-duplicates,return=minimal" }
  for ($try = 0; $try -lt 4; $try++) {
    try { Invoke-RestMethod -Uri "$dlUrl/rest/v1/$table`?on_conflict=$onConflict" -Method Post -Headers $headers -Body $body -TimeoutSec 90 | Out-Null; return }
    catch { if ($try -eq 3) { Write-Output "  [upsert $table] ERR: $($_.Exception.Message)" } else { Start-Sleep -Seconds 2 } }
  }
}
function FlattenTranscript($tr) {
  $t = $tr.transcript
  if ($t -is [array]) {
    return (($t | ForEach-Object {
      $spk = if ($_.speaker) { $_.speaker } elseif ($_.speaker_id) { $_.speaker_id } else { "" }
      $txt = if ($_.transcript) { $_.transcript } elseif ($_.text) { $_.text } else { "" }
      $(if ($spk) { "${spk}: " } else { "" }) + $txt
    }) -join "`n")
  }
  return (ConvertTo-Json $t -Depth 20 -Compress)
}
function MeetingRow($m) {
  $assoc = $m.crm_associations
  $opp = ($assoc | Where-Object { $_.crm_obj_type -eq 'oppo' } | Select-Object -First 1).crm_obj_id
  $acc = ($assoc | Where-Object { $_.crm_obj_type -eq 'account' } | Select-Object -First 1).crm_obj_id
  $purpose = if ($m.purpose -is [string]) { $m.purpose } elseif ($m.purpose.label) { $m.purpose.label } else { $null }
  $outcome = if ($m.outcome -is [string]) { $m.outcome } elseif ($m.outcome.label) { $m.outcome.label } else { $null }
  # NOTE: omit the text[] columns (attendee_emails/domains) from PS upsert to avoid
  # PowerShell's single-element-array JSON quirk; keep full attendees in jsonb.
  return [ordered]@{
    uuid=$m.uuid; subject=$m.subject; start_at=$m.start_at; end_at=$m.end_at;
    duration=$m.duration; state=$m.state; recording_state=$m.recording_state;
    transcript_ready=$m.transcript_ready; notes_ready=$m.notes_ready;
    is_call=$m.is_call; is_internal=$m.is_internal; organizer_email=$m.organizer_email;
    attendees=$m.attendees; crm_opportunity_id=$opp; crm_account_id=$acc;
    purpose=$purpose; outcome=$outcome; url=$m.url; created=$m.created; modified=$m.modified; raw=$m
  }
}

# --- tracked opps ---
$book = Invoke-RestMethod -Uri "$base/api/deal-engine/opportunities?slim=1" -Headers @{Authorization="Bearer $deTok"} -TimeoutSec 60
$opps = if ($book.records) { @($book.records) } elseif ($book.opportunities) { @($book.opportunities) } elseif ($book.data) { @($book.data) } else { @($book) }
$oppIds = @($opps | ForEach-Object { $_.opp_id } | Where-Object { $_ })
$done = if (Test-Path $ckpt) { @(Get-Content $ckpt) } else { @() }
$todo = @($oppIds | Where-Object { $done -notcontains $_ })
if ($maxOpps -gt 0) { $todo = @($todo | Select-Object -First $maxOpps) }
$now = (Get-Date).ToUniversalTime(); $to = $now.AddDays(1).ToString("yyyy-MM-ddTHH:mm:ssZ"); $frm = $now.AddDays(-$lookbackDays).ToString("yyyy-MM-ddTHH:mm:ssZ")
Write-Output "[TRACKED-BACKFILL] $($todo.Count) opps to do ($($done.Count) done) | window=${lookbackDays}d"
$totM = 0; $totT = 0; $n = 0
foreach ($opp15 in $todo) {
  $n++
  $opp18 = To18($opp15)
  $page = 1; $mCount = 0; $tCount = 0; $rows = @()
  while ($true) {
    $d = AvGet "/meetings/" @{ from_date=$frm; to_date=$to; o="-start_at"; page=$page; page_size=100; crm_opportunity_ids=$opp18; include_crm_associations="true" }
    Start-Sleep -Milliseconds $throttleMs
    if (-not $d) { break }
    $res = @($d.results)
    if ($res.Count -eq 0) { break }
    foreach ($m in $res) { if ($m.uuid) { $rows += (MeetingRow $m); $mCount++ } }
    if (-not $d.next) { break }
    $page++
  }
  if ($rows.Count -gt 0) { DlUpsert "avoma_meetings" $rows "uuid" }
  # transcripts + insights for ready meetings
  foreach ($m in $rows) {
    $u = $m.uuid; $tu = $m.raw.transcription_uuid
    if (-not ($m.transcript_ready -or $tu -or $m.notes_ready)) { continue }
    if ($tu) {
      $tr = AvGet "/transcriptions/$tu/" @{}; Start-Sleep -Milliseconds $throttleMs
      if ($tr) { DlUpsert "avoma_transcripts" @([ordered]@{ meeting_uuid=$u; transcription_uuid=$tu; transcript=$tr.transcript; transcript_text=(FlattenTranscript $tr); speakers=$tr.speakers; vtt_url=$tr.transcription_vtt_url }) "meeting_uuid"; $tCount++ }
    }
    $ins = AvGet "/meetings/$u/insights/" @{}; Start-Sleep -Milliseconds $throttleMs
    if ($ins) {
      $notes = $ins.ai_notes
      $notesText = if ($notes) { ConvertTo-Json $notes -Depth 15 -Compress } else { $null }
      DlUpsert "avoma_insights" @([ordered]@{ meeting_uuid=$u; ai_notes=$notes; ai_notes_text=$notesText; keywords=$ins.keywords }) "meeting_uuid"
    }
  }
  Add-Content -Path $ckpt -Value $opp15
  $totM += $mCount; $totT += $tCount
  if ($n % 10 -eq 0 -or $mCount -gt 0) { Write-Output "[TRACKED-BACKFILL] $n/$($todo.Count) opp=$opp15 meetings=$mCount transcripts=$tCount | totals m=$totM t=$totT" }
}
Write-Output "[TRACKED-BACKFILL] DONE. opps=$n meetings=$totM transcripts=$totT"
