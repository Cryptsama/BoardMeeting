$root=(Get-Location).Path
$out=Join-Path $root "bm_capability_report.txt"
function IsExcluded($p){ return ($p -match "\\\.venv\\") -or ($p -match "\\__pycache__\\") -or ($p -match "\\dumps\\") -or ($p -match "\\archive\\") -or ($p -match "\\Logs\\") }
$lines=New-Object System.Collections.Generic.List[string]
$lines.Add("BOARDMEETING CAPABILITY REPORT")|Out-Null
$lines.Add("Root: $root")|Out-Null
$lines.Add("Generated: $(Get-Date)")|Out-Null
$lines.Add("")|Out-Null
$lines.Add("=== TOP-LEVEL FOLDERS ===")|Out-Null
Get-ChildItem -Path $root -Directory -Force | Sort-Object Name | ForEach-Object { $lines.Add($_.Name)|Out-Null }
$lines.Add("")|Out-Null
$lines.Add("=== KEY ROOT FILES ===")|Out-Null
$keys=@(".env","app.py","requirements.txt","start.bat","start_local.bat","start_lan.bat","BoardMeeting_Notes.txt")
foreach($k in $keys){ $p=Join-Path $root $k; if(Test-Path $p){ $fi=Get-Item $p; $lines.Add($k+"`t"+[math]::Round($fi.Length/1KB,1)+" KB`t"+$fi.LastWriteTime)|Out-Null } else { $lines.Add($k+"`t[MISSING]")|Out-Null } }
$lines.Add("")|Out-Null
$lines.Add("=== requirements.txt ===")|Out-Null
if(Test-Path ".\requirements.txt"){ Get-Content ".\requirements.txt" | ForEach-Object { $lines.Add($_)|Out-Null } }
$lines.Add("")|Out-Null
$lines.Add("=== .env KEYS (NO VALUES) ===")|Out-Null
if(Test-Path ".\.env"){ Get-Content ".\.env" | ForEach-Object { $t=$_.Trim(); if($t -and -not $t.StartsWith("#") -and $t.Contains("=")){ $key=$t.Split("=",2)[0].Trim(); if($key){ $lines.Add($key)|Out-Null } } } } else { $lines.Add("[NO .env FOUND]")|Out-Null }
$lines.Add("")|Out-Null
$lines.Add("=== FASTAPI ROUTES FOUND (from app.py) ===")|Out-Null
if(Test-Path ".\app.py"){
  $pat='^\s*@app\.(get|post|put|delete|patch|options|head)\(\s*["'']([^"''\)]+)["'']'
  $hits=Select-String -Path ".\app.py" -Pattern $pat
  if(-not $hits){ $lines.Add("[No @app.<method>(...) decorators found]")|Out-Null }
  else { foreach($h in $hits){ $m=[regex]::Match($h.Line,$pat); if($m.Success){ $lines.Add(($m.Groups[1].Value.ToUpper()+"`t"+$m.Groups[2].Value))|Out-Null } } }
  $app=Get-Content ".\app.py" -Raw
  if($app -match "Jinja2Templates"){ $lines.Add("")|Out-Null; $lines.Add("Uses Jinja2Templates (Templates folder)")|Out-Null }
  if($app -match "StaticFiles"){ $lines.Add("Mounts StaticFiles (static folder)")|Out-Null }
} else { $lines.Add("[app.py NOT FOUND IN ROOT]")|Out-Null }
$lines.Add("")|Out-Null
$lines.Add("=== Templates/ ===")|Out-Null
if(Test-Path ".\Templates"){ Get-ChildItem ".\Templates" -File -Force | Sort-Object Name | ForEach-Object { $lines.Add($_.Name)|Out-Null } } else { $lines.Add("[missing]")|Out-Null }
$lines.Add("")|Out-Null
$lines.Add("=== static/ (top 25 largest files) ===")|Out-Null
if(Test-Path ".\static"){ Get-ChildItem ".\static" -Recurse -File -Force | Sort-Object Length -Descending | Select-Object -First 25 | ForEach-Object { $rel=$_.FullName.Replace($root+"\",""); $lines.Add($rel+"`t"+[math]::Round($_.Length/1KB,1)+" KB")|Out-Null } } else { $lines.Add("[missing]")|Out-Null }
$lines.Add("")|Out-Null
$lines.Add("=== MINI FILE TREE (depth<=2, excludes .venv/__pycache__/dumps/archive/Logs) ===")|Out-Null
Get-ChildItem -Path $root -Recurse -Force | Where-Object { -not (IsExcluded $_.FullName) } | ForEach-Object { $rel=$_.FullName.Replace($root+"\",""); if($rel.Split("\").Count -le 3){ if($_.PSIsContainer){ $lines.Add("[DIR]  "+$rel)|Out-Null } else { $lines.Add("       "+$rel)|Out-Null } } }
$lines | Set-Content -Encoding UTF8 $out
Set-Clipboard -Value (Get-Content $out -Raw)
Write-Host "DONE. Saved: $out"
Write-Host "Copied to clipboard — paste it here."
